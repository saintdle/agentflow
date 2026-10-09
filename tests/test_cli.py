from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib
from importlib import resources as importlib_resources
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli
from agentflow import execution_limits as execution_limits_backend
from tests import _state_home  # noqa: F401  # external controller authority


def _controller_authority(root: Path, workflow_root: str, lease) -> str:
    _, credentials = cli._controller_credentials(
        argparse.Namespace(
            root=str(root), workflow_root=workflow_root, resume_key_file=""
        ),
        lease,
    )
    return credentials["authority_secret"]


def _seed_controller_lease(root: Path, *, controller: str = "agentflow-controller", workflow_root: str = ""):
    """Acquire a real controller lease so fencing tests exercise the actual
    on-disk Lease schema (epoch/acquired_at/heartbeat_at/resume_secret_hash),
    not a hand-rolled partial dict that only the old raw-json check accepted.
    Uses the same per-workflow-root namespaced path (AFREL-036) the real CLI
    computes, so _verify_launch_authority/_controller_fence find it."""
    state_path = cli._controller_state_dir(root, workflow_root) / "state.json"
    rc = cli.controller_backend.RootController(str(root), controller, state_path=state_path)
    lease = rc.acquire()
    _controller_authority(root, workflow_root, lease)
    return lease


# A real Herdr 0.7.4 agent_started response body (pane + provider session id).
def _agent_started_stdout(provider: str, *, session_id: str = "sess-1", pane_id: str = "pane-1",
                          agent_session: object = "__default__") -> str:
    agent: dict = {"pane_id": pane_id, "agent": provider}
    if agent_session == "__default__":
        agent["agent_session"] = {"source": "herdr", "agent": provider, "kind": "id", "value": session_id}
    else:
        agent["agent_session"] = agent_session
    return json.dumps({"id": "cli:agent:start", "result": {"type": "agent_started", "agent": agent}})


class ValidLaunch:
    """A fully valid, authenticated provider launch fixture.

    Sets up a real git repo, a complete task bead (launch route + acceptance
    matrix + opaque claim token), a real controller lease, a persisted exact
    claim identity, the REAL from-bead handoff artifact, and the pinned
    handoff/root-preflight digests -- everything a genuine ``herdr launch``
    needs to reach (and pass) the provider spawn. Only the external Beads
    boundary is patched (via ``beads_patches``); git, the handoff pipeline,
    the controller lease/fence, the return channel, and the argv are all real.
    """

    def __init__(self, root: Path, *, provider: str = "claude", model: str = "claude-sonnet-5",
                 effort: str = "medium", role: str = "coding",
                 controller: str = "agentflow-controller", seed_lease: bool = True,
                 claim_token: str = "opaque-claim-token-0123456789abcdef0123456789ab",
                 gitless: bool = False) -> None:
        self.root = root
        self.provider = provider
        self.model = model
        self.effort = effort
        self.role = role
        self.controller = controller
        self.workflow_root = "wf-root"
        self.task_id = "task-1"
        self.actor = controller
        self.gitless = gitless
        self.claim_token = claim_token
        self.session_name = "agentflow-task-1"
        self.acceptance = {
            "version": 1, "task_id": self.task_id,
            "rows": [{"id": "R1", "outcome": "smoke passes", "owner": "eng",
                      "lane": "static", "planned_evidence": "unit test", "status": "planned"}],
        }
        self.task_issue = {
            "id": self.task_id, "title": "Bounded task", "description": "Do the bounded thing.",
            "status": "open", "assignee": self.actor,
            "acceptance_criteria": "task-1 is done when the smoke test passes",
            "parent": self.workflow_root,
            "metadata": {"agentflow": {
                "launch": {"provider": provider, "model": model, "effort": effort, "role": role},
                "lane": "external", "tool_profile": "shell-write",
                "output_boundary": ".", "context": ["README.md"],
                "budget": ["20 minutes; one retry; stop on blocker"],
                "acceptance": self.acceptance,
                "root": self.workflow_root, "task": self.task_id, "actor": self.actor,
                "claim_id": "claim-1", "claim_token": claim_token,
                "checks": ["python3 -m unittest"],
            }},
        }
        self.root_issue = {"id": self.workflow_root, "status": "open", "metadata": {"agentflow": {}}}
        self._init_git()
        # ``seed_lease`` is for direct ``herdr launch`` tests that need a
        # pre-existing controller lease, a persisted exact claim, and the
        # pinned handoff/root-preflight digests. The controller-driven
        # lifecycle test leaves these unset so the controller itself acquires
        # the lease and materializes the handoff (a fresh owner cannot adopt a
        # pre-seeded lease owned by a different owner_id).
        self.lease = None
        self.handoff_path = self.handoff = self.root_preflight_sha256 = None
        if seed_lease:
            self.lease = self._seed_lease()
            self._persist_claim()
            self.handoff_path, self.handoff, self.root_preflight_sha256 = self._materialize()

    def _init_git(self) -> None:
        r = str(self.root)
        if not self.gitless:
            subprocess.run(["git", "init", "-b", "main", r], capture_output=True, check=True)
            subprocess.run(["git", "-C", r, "config", "user.email", "t@example.test"], capture_output=True, check=True)
            subprocess.run(["git", "-C", r, "config", "user.name", "Test"], capture_output=True, check=True)
        (self.root / "README.md").write_text("root\n", encoding="utf-8")
        claude_settings = self.root / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_bytes(
            cli.packaged_resources.item(
                "templates", "project", "claude-settings.json"
            ).read_bytes()
        )
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/validate.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
        if not self.gitless:
            subprocess.run(["git", "-C", r, "add", "-A"], capture_output=True, check=True)
            subprocess.run(["git", "-C", r, "commit", "-m", "init"], capture_output=True, check=True)

    def _seed_lease(self):
        state_path = cli._controller_state_dir(self.root, self.workflow_root) / "state.json"
        rc = cli.controller_backend.RootController(str(self.root), self.controller, state_path=state_path)
        lease = rc.acquire()
        self.authority_secret = _controller_authority(
            self.root, self.workflow_root, lease
        )
        return lease

    def _persist_claim(self) -> None:
        cli._private_atomic_json(
            cli._claim_state_path(self.root, self.task_id),
            {"root": self.workflow_root, "task": self.task_id, "actor": self.actor,
             "claim_id": "claim-1", "claim_token": self.claim_token, "token": self.claim_token},
        )

    def get_issue(self, cwd, issue_id):
        return self.root_issue if issue_id == self.workflow_root else self.task_issue

    @contextlib.contextmanager
    def beads_patches(self):
        with mock.patch.object(cli.beads_backend, "get_issue", side_effect=self.get_issue), \
             mock.patch.object(cli.beads_backend, "root_descendants", return_value=[self.task_issue]), \
             mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
             mock.patch.object(cli.beads_backend, "update_agentflow_metadata"):
            yield

    def _materialize(self):
        # Provider binaries are external integration dependencies. Keep this
        # unit fixture hermetic while still exercising the complete handoff
        # and root-preflight contract.
        with self.beads_patches(), mock.patch.object(
            cli.shutil, "which", side_effect=lambda command: f"/fake/{command}"
        ):
            handoff_path = cli._materialize_launch_handoff(
                self.root, self.task_id, self.provider, role=self.role)
            handoff = cli.provider_argv_backend.validate_confined_handoff(
                handoff_path, root=self.root, provider=self.provider, task_id=self.task_id)
            _, digest = cli._run_actual_root_preflight(
                root=self.root, workflow_root=self.workflow_root, task_id=self.task_id,
                actor=self.actor, claim=self.claim_token, lease=self.lease.token,
                session_name=self.session_name, provider=self.provider, role=self.role,
                model=self.model, effort=self.effort, handoff=handoff)
        return handoff_path, handoff, digest

    def launch_args(self, **overrides) -> argparse.Namespace:
        defaults = dict(
            root=str(self.root), session_name=self.session_name, agent_name=self.task_id,
            task=self.task_id, claim=self.claim_token, lease=self.lease.token,
            provider=self.provider, role=self.role, model=self.model, effort=self.effort,
            session_id="", policy="", state_path="", dry_run=False, json=True,
            workflow_root=self.workflow_root, actor=self.actor,
            handoff=str(self.handoff_path),
            handoff_content_sha256=self.handoff.content_sha256,
            handoff_manifest_sha256=self.handoff.manifest_sha256,
            handoff_preflight_sha256=self.handoff.preflight_sha256,
            root_preflight_sha256=self.root_preflight_sha256,
            acceptance_ids=("R1",),
            _authority_secret=getattr(self, "authority_secret", ""),
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def provider_command(self, name):
        if name == "herdr":
            return "/usr/bin/herdr"
        if name == self.provider:
            return "/usr/bin/" + self.provider
        return None

    def herdr_run(self, *, stdout: str | None = None, returncode: int = 0, stderr: str = "",
                  capture: dict | None = None, on_spawn=None,
                  claude_version: str = "2.1.281 (Claude Code)\n"):
        """A subprocess.run side_effect that intercepts ONLY the herdr spawn.

        Real git subprocesses (used by the handoff pipeline and preflight) pass
        through untouched; the `herdr agent start ...` invocation returns a
        canned agent_started body so no real Herdr binary is required. This is
        the exact argv production passes to Herdr, so `capture` records it.
        """
        real_run = subprocess.run
        body = stdout if stdout is not None else _agent_started_stdout(self.provider)

        def _run(argv, **kwargs):
            if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["status", "server"]:
                return subprocess.CompletedProcess(argv, 0, stdout="status: running\ncompatible: yes\n", stderr="")
            if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["integration", "status"]:
                return subprocess.CompletedProcess(argv, 0, stdout="codex: current (v1)\n", stderr="")
            if (
                argv
                and Path(str(argv[0])).name == "claude"
                and list(argv[1:]) == ["--version"]
            ):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=claude_version, stderr=""
                )
            if argv and Path(str(argv[0])).name == "herdr" and "agent" in argv and "start" in argv:
                if capture is not None:
                    capture["argv"] = list(argv)
                # A real provider hook emits native lifecycle metadata before
                # Agentflow accepts its result. Mirror that process boundary
                # instead of bypassing the production model-fidelity gate.
                try:
                    payload = json.loads(body)
                    session_id = str(
                        payload["result"]["agent"]["agent_session"]["value"]
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    session_id = ""
                if session_id:
                    cli.events_backend.record_event_safely(
                        cli.events_backend.EventSpool(cli._state_dir() / "events.jsonl"),
                        cli.events_backend.normalize_event(
                            self.provider,
                            {"event": "session.start", "session_id": session_id, "model": self.model},
                            event_id=f"fixture-model-{self.root}-{session_id}",
                        ),
                    )
                if on_spawn is not None:
                    on_spawn(list(argv))
                return subprocess.CompletedProcess(argv, returncode, stdout=body, stderr=stderr)
            return real_run(argv, **kwargs)

        return _run


class AgentflowTests(unittest.TestCase):
    def test_controller_rejects_synthetic_task_files(self) -> None:
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(
                ["controller", "start", "--root", "/tmp/root", "--tasks", "/tmp/tasks.json"]
            )

    def test_valid_typed_launch_reaches_herdr_with_supported_argv(self) -> None:
        """Fix #1/#2: a fully valid authenticated launch reaches the Herdr
        spawn (no NameError crash at the return-channel mint) and passes the
        exact supported `herdr agent start <agent-name> ... -- <provider>`
        argv -- never the unsupported `herdr --session <name> ...` selector."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            capture: dict = {}
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            argv = capture["argv"]
            # Supported API: `herdr agent start <agent-name> ...`. No --session.
            self.assertEqual(argv[:4], ["/usr/bin/herdr", "agent", "start", "task-1"])
            self.assertNotIn("--session", argv)
            self.assertIn("--", argv)
            provider_tail = argv[argv.index("--") + 1:]
            self.assertEqual(provider_tail[:1], ["/usr/bin/claude"])
            self.assertEqual(
                provider_tail[provider_tail.index("--fallback-model") + 1],
                fixture.model,
            )
            self.assertEqual(provider_tail.count("--fallback-model"), 1)
            self.assertIn("--cwd", argv)
            self.assertIn("--no-focus", argv)
            # The durable Agentflow session name lives in Agentflow state,
            # not in Herdr's argv.
            state = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))
            record = state["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")
            self.assertEqual(record["herdr_session"], fixture.session_name)
            self.assertEqual(record["binding"]["session_id"], "sess-1")
            self.assertEqual(record["return_channel"]["state"], "issued")
            # Fix #3: the return contract is bound to the controller
            # incarnation and its reusable-name owner.
            contract = json.loads(Path(record["return_channel"]["contract_path"]).read_text(encoding="utf-8"))
            self.assertEqual(contract["continuity_id"], fixture.lease.continuity_id)
            self.assertEqual(contract["controller_id"], fixture.controller)
            self.assertEqual(contract["model"], fixture.model)
            self.assertEqual(contract["effort"], fixture.effort)

    def test_herdr_provider_receives_controller_state_home_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state_home = root.parent / f"{root.name}-controller-runtime-state"
            with mock.patch.dict(
                os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}, clear=False,
            ):
                fixture = ValidLaunch(root)
                capture: dict = {}
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)

                argv = capture["argv"]
                herdr_env: dict[str, str] = {}
                for index, argument in enumerate(argv[:-1]):
                    if argument == "--env":
                        key, _, value = argv[index + 1].partition("=")
                        herdr_env[key] = value
                self.assertEqual(herdr_env["AGENTFLOW_STATE_HOME"], str(state_home.resolve()))
                self.assertEqual(
                    cli.events_backend.attested_model(
                        cli.events_backend.EventSpool(state_home / "events.jsonl"),
                        fixture.provider, "sess-1",
                    ),
                    fixture.model,
                )

    def test_nonsterile_launch_packages_and_tracks_claude_model_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"AGENTFLOW_STATE_HOME": str(Path(temporary) / "state")},
                clear=False,
            ):
                fixture = ValidLaunch(Path(temporary).resolve())
                session_id = "nonsterile-claude-session"
                settings_path = fixture.root / ".claude/settings.json"
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                # A project fallback chain must not let Claude serve an
                # unreported model for one turn (PostModelSwitch is not fired
                # for that case). Herdr overrides it with the exact primary.
                settings["fallbackModel"] = "claude-opus-4.8,claude-sonnet-4.6"
                settings_path.write_text(
                    json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                )
                hooks = settings.get("hooks", {})
                template = json.loads(
                    cli.packaged_resources.item(
                        "templates", "project", "claude-settings.json"
                    ).read_text(encoding="utf-8")
                )
                root_mirror = json.loads(
                    (Path(__file__).resolve().parents[1]
                     / "templates/project/claude-settings.json").read_text(encoding="utf-8")
                )
                self.assertEqual(root_mirror, template)
                for event_name in ("SessionStart", "PostModelSwitch"):
                    self.assertTrue(
                        any(
                            handler.get("type") == "command"
                            and handler.get("command")
                            == f"~/.local/bin/agentflow hook --provider claude --event {event_name}"
                            for entry in hooks.get(event_name, [])
                            for handler in entry.get("hooks", [])
                        ),
                        f"project settings lack the controlled {event_name} hook",
                    )

                captured: dict = {}
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(
                         cli.subprocess,
                         "run",
                         side_effect=fixture.herdr_run(
                             stdout=_agent_started_stdout("claude", session_id=session_id),
                             capture=captured,
                         ),
                     ):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                provider_tail = captured["argv"][captured["argv"].index("--") + 1:]
                fallback_index = provider_tail.index("--fallback-model")
                self.assertEqual(provider_tail[fallback_index + 1], fixture.model)
                self.assertNotIn("claude-opus-4.8,claude-sonnet-4.6", provider_tail)
                # Agentflow validates existing project settings but never
                # overwrites the user's configured chain.
                self.assertEqual(
                    json.loads(settings_path.read_text(encoding="utf-8"))["fallbackModel"],
                    "claude-opus-4.8,claude-sonnet-4.6",
                )

                switch_payload = {
                    "hook_event_name": "PostModelSwitch",
                    "session_id": session_id,
                    "timestamp": "2026-09-30T09:01:00.000Z",
                    "cwd": str(fixture.root),
                    "source": "fallback",
                    "from_model": fixture.model,
                    "to_model": "claude-sonnet-4.6",
                }
                with mock.patch("sys.stdin", io.StringIO(json.dumps(switch_payload))), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        cli.hook(argparse.Namespace(provider="claude", event="PostModelSwitch")),
                        0,
                    )
                with self.assertRaisesRegex(ValueError, "provider model mismatch"):
                    cli._require_attested_model("claude", session_id, fixture.model)
                cli._require_attested_model("claude", session_id, "claude-sonnet-4.6")

    def test_nonsterile_launch_blocks_missing_claude_lifecycle_hook_before_spawn(self) -> None:
        for missing_event in ("SessionStart", "PostModelSwitch"):
            with self.subTest(event=missing_event), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTFLOW_STATE_HOME": str(Path(temporary) / "state")},
                    clear=False,
                ):
                    fixture = ValidLaunch(Path(temporary).resolve())
                    settings_path = fixture.root / ".claude/settings.json"
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                    settings["hooks"].pop(missing_event, None)
                    settings_path.write_text(
                        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                    )
                    payloads: list[dict] = []
                    captured: dict = {}
                    with fixture.beads_patches(), \
                         mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                         mock.patch.object(
                             cli.subprocess,
                             "run",
                             side_effect=fixture.herdr_run(capture=captured),
                         ), \
                         mock.patch.object(
                             cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)
                         ):
                        self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                    self.assertNotIn("argv", captured)
                    self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())
                    self.assertEqual(len(payloads), 1)
                    self.assertIn(missing_event, payloads[0]["error"])

    def test_nonsterile_launch_requires_claude_post_model_switch_capability_before_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"AGENTFLOW_STATE_HOME": str(Path(temporary) / "state")},
                clear=False,
            ):
                fixture = ValidLaunch(Path(temporary).resolve())
                payloads: list[dict] = []
                captured: dict = {}
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(
                         cli.subprocess,
                         "run",
                         side_effect=fixture.herdr_run(
                             capture=captured, claude_version="2.1.250 (Claude Code)\n"
                         ),
                     ), \
                     mock.patch.object(
                         cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)
                     ):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                self.assertNotIn("argv", captured)
                self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())
                self.assertEqual(len(payloads), 1)
                self.assertIn("2.1.251 or newer", payloads[0]["error"])

    def test_nonsterile_launch_blocks_settings_that_disable_claude_hooks(self) -> None:
        for disabling_setting in ("disableAllHooks", "allowManagedHooksOnly"):
            with self.subTest(setting=disabling_setting), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTFLOW_STATE_HOME": str(Path(temporary) / "state")},
                    clear=False,
                ):
                    fixture = ValidLaunch(Path(temporary).resolve())
                    settings_path = fixture.root / ".claude/settings.json"
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                    settings[disabling_setting] = True
                    settings_path.write_text(
                        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                    )
                    payloads: list[dict] = []
                    captured: dict = {}
                    with fixture.beads_patches(), \
                         mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                         mock.patch.object(
                             cli.subprocess,
                             "run",
                             side_effect=fixture.herdr_run(capture=captured),
                         ), \
                         mock.patch.object(
                             cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)
                         ):
                        self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                    self.assertNotIn("argv", captured)
                    self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())
                    self.assertEqual(len(payloads), 1)
                    self.assertIn(disabling_setting, payloads[0]["error"])

    def test_nonsterile_launch_blocks_known_hook_suppression_sources_before_spawn(self) -> None:
        cases = (
            ("project-local", ".claude/settings.local.json", "disableAllHooks"),
            ("user", "claude-config/settings.json", "disableAllHooks"),
            ("managed", "managed/managed-settings.json", "allowManagedHooksOnly"),
        )
        for label, relative_path, setting_name in cases:
            with self.subTest(source=label), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                managed_path = base / "managed/managed-settings.json"
                env = {
                    "AGENTFLOW_STATE_HOME": str(base / "state"),
                    "CLAUDE_CONFIG_DIR": str(base / "claude-config"),
                }
                with mock.patch.dict(os.environ, env, clear=False):
                    fixture = ValidLaunch(base / "workspace")
                    settings_path = (
                        fixture.root / relative_path
                        if label == "project-local"
                        else base / relative_path
                    )
                    settings_path.parent.mkdir(parents=True, exist_ok=True)
                    settings_path.write_text(
                        json.dumps({setting_name: True}) + "\n", encoding="utf-8"
                    )
                    payloads: list[dict] = []
                    captured: dict = {}
                    with fixture.beads_patches(), \
                         mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                         mock.patch.object(
                             cli, "_claude_managed_settings_paths",
                             return_value=(managed_path,) if label == "managed" else (),
                         ), \
                         mock.patch.object(
                             cli.subprocess,
                             "run",
                             side_effect=fixture.herdr_run(capture=captured),
                         ), \
                         mock.patch.object(
                             cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)
                         ):
                        self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                    self.assertNotIn("argv", captured)
                    self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())
                    self.assertEqual(len(payloads), 1)
                    self.assertIn(setting_name, payloads[0]["error"])

    def test_nonsterile_launch_checks_main_checkout_local_settings_from_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(
                os.environ,
                {
                    "AGENTFLOW_STATE_HOME": str(base / "state"),
                    "CLAUDE_CONFIG_DIR": str(base / "claude-config"),
                },
                clear=False,
            ):
                fixture = ValidLaunch(base / "main")
                (fixture.root / ".claude/settings.local.json").write_text(
                    json.dumps({"disableAllHooks": True}) + "\n", encoding="utf-8"
                )
                worktree = base / "linked-worktree"
                subprocess.run(
                    ["git", "-C", str(fixture.root), "worktree", "add", "--detach", str(worktree), "HEAD"],
                    capture_output=True, check=True,
                )
                with mock.patch.object(cli, "_claude_managed_settings_paths", return_value=()):
                    with self.assertRaisesRegex(ValueError, "project-local.*disableAllHooks"):
                        cli._require_claude_model_switch_hooks(worktree)

    def test_nonsterile_launch_rechecks_claude_hooks_before_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"AGENTFLOW_STATE_HOME": str(Path(temporary) / "state")},
                clear=False,
            ):
                fixture = ValidLaunch(Path(temporary).resolve())
                settings_path = fixture.root / ".claude/settings.json"
                payloads: list[dict] = []
                captured: dict = {}

                def remove_post_model_switch_after_initial_check(**_kwargs):
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                    settings["hooks"].pop("PostModelSwitch", None)
                    settings_path.write_text(
                        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
                    )
                    # Keep the already pinned root-preflight digest stable so
                    # the next fenced contract check reaches hook validation.
                    return {}, fixture.root_preflight_sha256

                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(
                         cli,
                         "_run_actual_root_preflight",
                         side_effect=remove_post_model_switch_after_initial_check,
                     ), \
                     mock.patch.object(
                         cli.subprocess,
                         "run",
                         side_effect=fixture.herdr_run(capture=captured),
                     ), \
                     mock.patch.object(
                         cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)
                     ):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                self.assertNotIn("argv", captured)
                self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())
                self.assertEqual(len(payloads), 1)
                self.assertIn("PostModelSwitch", payloads[0]["error"])

    def test_sterile_typed_launch_keeps_authority_in_root_and_spawns_in_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace")
            fixture.task_issue["metadata"]["agentflow"].update({
                "lane": "external",
                "tool_profile": "shell-readonly",
                "context": ["README.md"],
                "budget": ["10 minutes; one retry; stop on blocker"],
                "output_boundary": ".",
            })
            with fixture.beads_patches(), mock.patch.object(
                cli.shutil, "which", side_effect=lambda command: f"/fake/{command}"
            ):
                fixture.handoff_path = cli._materialize_launch_handoff(
                    fixture.root, fixture.task_id, fixture.provider, role=fixture.role
                )
            # The fixture's full handoff already passed source preflight. Its
            # declared context is packaged without repository instructions.
            stage = base / "sterile"
            with mock.patch.object(
                cli, "_provider_command", return_value="/fake/codex"
            ):
                packaged_path = cli._package_handoff_sterile(
                    fixture.handoff_path, stage
                )
            packaged = cli.provider_argv_backend.validate_confined_handoff(
                packaged_path, root=stage, provider=fixture.provider, task_id=fixture.task_id
            )
            with fixture.beads_patches(), mock.patch.object(
                cli.shutil, "which", side_effect=lambda command: f"/fake/{command}"
            ):
                _, digest = cli._run_actual_root_preflight(
                    root=fixture.root, workflow_root=fixture.workflow_root,
                    task_id=fixture.task_id, actor=fixture.actor,
                    claim=fixture.claim_token, lease=fixture.lease.token,
                    session_name=fixture.session_name, provider=fixture.provider,
                    role=fixture.role, model=fixture.model, effort=fixture.effort,
                    handoff=packaged, execution_root=stage,
                )
            capture: dict = {}
            args = fixture.launch_args(
                handoff=str(packaged_path), execution_root=str(stage),
                handoff_content_sha256=packaged.content_sha256,
                handoff_manifest_sha256=packaged.manifest_sha256,
                handoff_preflight_sha256=packaged.preflight_sha256,
                root_preflight_sha256=digest,
            )
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                self.assertEqual(cli.herdr_launch(args), 0)
            argv = capture["argv"]
            self.assertEqual(argv[argv.index("--cwd") + 1], str(stage))
            state = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text())
            self.assertEqual(state["sessions"][fixture.task_id]["execution_root"], str(stage))

    def test_failed_spawn_is_private_and_retryable_with_actual_identity(self) -> None:
        """A failing Herdr spawn must persist a private, retryable failure (no
        provider stdout/stderr leaked into state), and a retry over the failed
        reservation must succeed with a real bound identity."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            state_file = fixture.root / ".agentflow/herdr/sessions.json"
            calls = {"n": 0}
            real_run = subprocess.run

            def flaky_run(argv, **kwargs):
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["status", "server"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="status: running\ncompatible: yes\n", stderr="")
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["integration", "status"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="codex: current (v1)\n", stderr="")
                if argv and Path(str(argv[0])).name == "claude" and list(argv[1:]) == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="2.1.281 (Claude Code)\n", stderr=""
                    )
                if argv and Path(str(argv[0])).name == "herdr" and "agent" in argv and "start" in argv:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return subprocess.CompletedProcess(
                            argv, 9, stdout="secret output SECRETVALUE", stderr="private token TOKENVALUE")
                    return subprocess.CompletedProcess(argv, 0, stdout=_agent_started_stdout(fixture.provider), stderr="")
                return real_run(argv, **kwargs)

            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=flaky_run):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                first = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(first["sessions"]["task-1"]["status"], "failed")
                raw = state_file.read_text(encoding="utf-8")
                self.assertNotIn("SECRETVALUE", raw)
                self.assertNotIn("TOKENVALUE", raw)
                # Model a version-skewed history that retained both numbered
                # launches but omitted the optional top-level attempt field.
                # The next reservation must use the normalized highwater.
                state = json.loads(state_file.read_text(encoding="utf-8"))
                failed = state["sessions"]["task-1"]
                failed.pop("attempt", None)
                failed["launch_id"] = "launch-two"
                failed["attempts"] = [
                    {"attempt": 1, "launch_id": "launch-one",
                     "workflow_root": fixture.workflow_root, "status": "failed"},
                    {"attempt": 2, "launch_id": "launch-two",
                     "workflow_root": fixture.workflow_root, "status": "failed"},
                ]
                failed.pop("binding", None)
                failed.pop("return_channel", None)
                cli._private_atomic_json(state_file, state)
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")
            self.assertTrue(record["binding"]["launch_id"])
            self.assertEqual(record["binding"]["launch_id"], record["launch_id"])
            self.assertEqual(record["binding"]["session_id"], "sess-1")
            self.assertEqual(oct(state_file.stat().st_mode & 0o777), "0o600")
            self.assertEqual(record["attempt"], 3)
            self.assertEqual(record["workflow_root"], fixture.workflow_root)
            self.assertEqual(
                {event["workflow_root"] for event in record["attempts"]},
                {fixture.workflow_root},
            )
            self.assertEqual(
                len({event["launch_id"] for event in record["attempts"]}), 3,
            )
            self.assertEqual(cli.execution_backend.summarize_attempts([record])[0], 3)

    def test_launch_fails_closed_on_stale_lease_and_never_spawns(self) -> None:
        """Stale authority: a different owner takes the lease over (rotating
        the token/epoch/incarnation) before launch. The now-stale lease token
        must fail the launch closed through the public CLI boundary and never
        spawn a provider."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            state_path = cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json"
            intruder = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path, stale_after=0.01)
            time.sleep(0.05)
            intruder.acquire(takeover=True)  # rotates the on-disk lease
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run") as run:
                # argv[0] name is never "herdr" here, so a spawn would be a real
                # git call at most; assert no herdr spawn happens at all.
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
            for call in run.call_args_list:
                argv = call.args[0] if call.args else []
                self.assertNotEqual(Path(str(argv[0])).name if argv else "", "herdr")

    def test_launch_fails_closed_on_tampered_claim_and_never_spawns(self) -> None:
        """A claim token that is not the exact persisted opaque claim must fail
        closed with no spawn."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run") as run:
                args = fixture.launch_args(claim="opaque-claim-token-WRONG9999999999999999999999999")
                self.assertEqual(cli.herdr_launch(args), 2)
            for call in run.call_args_list:
                argv = call.args[0] if call.args else []
                self.assertNotEqual(Path(str(argv[0])).name if argv else "", "herdr")
            self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())

    def test_launch_fails_closed_on_tampered_handoff_digest_and_never_spawns(self) -> None:
        """A pinned handoff digest that does not match the artifact must fail
        closed with no spawn."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run") as run:
                args = fixture.launch_args(handoff_content_sha256="0" * 64)
                self.assertEqual(cli.herdr_launch(args), 2)
            for call in run.call_args_list:
                argv = call.args[0] if call.args else []
                self.assertNotEqual(Path(str(argv[0])).name if argv else "", "herdr")
            self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())

    def test_launch_fails_closed_on_incomplete_authority_and_never_spawns(self) -> None:
        """Incomplete launch authority (no typed handoff / no pinned digests)
        must fail closed before any spawn."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run") as run:
                args = fixture.launch_args(handoff="")
                self.assertEqual(cli.herdr_launch(args), 2)
            for call in run.call_args_list:
                argv = call.args[0] if call.args else []
                self.assertNotEqual(Path(str(argv[0])).name if argv else "", "herdr")
            self.assertFalse((fixture.root / ".agentflow/herdr/sessions.json").exists())

    def test_launch_pre_commit_fence_rejects_a_real_concurrent_takeover(self) -> None:
        """AFREL-027: a REAL thread runs acquire(takeover=True) while
        herdr_launch is inside its spawn critical section holding the
        controller lock. The takeover must not complete until the fence
        releases; whoever then wins the lock, the record must never be torn
        (a committed binding alongside a failed status, or ok with no
        binding)."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            state_path = cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json"
            entered = threading.Event()
            release = threading.Event()
            real_run = subprocess.run

            def slow_run(argv, **kwargs):
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["status", "server"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="status: running\ncompatible: yes\n", stderr="")
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["integration", "status"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="codex: current (v1)\n", stderr="")
                if argv and Path(str(argv[0])).name == "claude" and list(argv[1:]) == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="2.1.281 (Claude Code)\n", stderr=""
                    )
                if argv and Path(str(argv[0])).name == "herdr" and "agent" in argv and "start" in argv:
                    entered.set()
                    release.wait(timeout=5)
                    return subprocess.CompletedProcess(argv, 0, stdout=_agent_started_stdout(fixture.provider), stderr="")
                return real_run(argv, **kwargs)

            takeover_done = threading.Event()

            def attempt_takeover() -> None:
                entered.wait(timeout=5)
                intruder = cli.controller_backend.RootController(
                    str(fixture.root), "intruder", state_path=state_path, stale_after=0.01)
                time.sleep(0.05)
                try:
                    intruder.acquire(takeover=True)
                finally:
                    takeover_done.set()

            launch_result: dict = {}

            def do_launch() -> None:
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(cli.subprocess, "run", side_effect=slow_run):
                    launch_result["code"] = cli.herdr_launch(fixture.launch_args())

            launcher = threading.Thread(target=do_launch)
            taker = threading.Thread(target=attempt_takeover)
            try:
                launcher.start()
                self.assertTrue(entered.wait(timeout=5))
                taker.start()
                # The takeover cannot complete while herdr_launch holds the
                # fence across its spawn critical section.
                self.assertFalse(takeover_done.wait(timeout=0.3))
                release.set()
                launcher.join(timeout=5)
            finally:
                release.set()
                taker.join(timeout=5)
            self.assertTrue(takeover_done.is_set())
            state = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))
            record = state["sessions"]["task-1"]
            if launch_result["code"] == 0:
                self.assertEqual(record["status"], "launched")
                self.assertIsNotNone(record["binding"])
            else:
                self.assertEqual(record["status"], "failed")
                self.assertIsNone(record["binding"])

    def test_launch_persists_identity_pending_for_nullable_agent_session(self) -> None:
        """AFREL-025: a real, live pane with agent_session=null (e.g. Codex)
        must persist identity_pending, not a retryable failure, and a retry
        while the pane is live must be rejected rather than spawn a duplicate."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            state_file = fixture.root / ".agentflow/herdr/sessions.json"
            pending = _agent_started_stdout(fixture.provider, agent_session=None)
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(stdout=pending)):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["status"], "identity_pending")
            self.assertEqual(record["pane_id"], "pane-1")
            self.assertIsNone(record["binding"])
            self.assertEqual(record["workflow_root"], fixture.workflow_root)
            self.assertEqual(record["attempts"][0]["workflow_root"], fixture.workflow_root)
            self.assertEqual(record["attempts"][0]["launch_id"], record["launch_id"])
            # A retry while the pane is live must be rejected, not spawn again.
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run") as run:
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
            for call in run.call_args_list:
                argv = call.args[0] if call.args else []
                self.assertNotEqual(Path(str(argv[0])).name if argv else "", "herdr")

    def test_resolve_pending_identity_polls_real_agent_get_schema(self) -> None:
        """AFREL-025: resolution re-queries `herdr agent get` (the real
        agent_info schema) and upgrades identity_pending to launched."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_path = root / ".agentflow/herdr/sessions.json"
            cli._private_atomic_json(state_path, {
                "schema": "agentflow.herdr", "version": 1,
                "sessions": {"task-1": {
                    "root": str(root), "task_id": "task-1", "claim_id": "claim-1",
                    "lease_id": "lease-1", "provider": "codex", "launch_id": "launch-1",
                    "status": "identity_pending", "pane_id": "pane-real",
                    "result": None, "binding": None, "attempt": 1, "attempts": [],
                }},
            })
            resolved = mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "result": {
                        "type": "agent_info",
                        "agent": {
                            "pane_id": "pane-real", "agent": "codex",
                            "agent_session": {
                                "source": "herdr:codex", "agent": "codex",
                                "kind": "id", "value": "now-resolved-session",
                            },
                        },
                    },
                }),
                stderr="",
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/herdr"), \
                 mock.patch.object(cli.subprocess, "run", return_value=resolved) as run:
                changed = cli._resolve_pending_identity(root, "task-1")
            self.assertTrue(changed)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][-1], "pane-real")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            record = state["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")
            self.assertEqual(record["binding"]["session_id"], "now-resolved-session")
            self.assertEqual(record["binding"]["pane_id"], "pane-real")
            self.assertEqual(cli.execution_backend.summarize_attempts([record])[0], 1)

    def test_resolve_pending_identity_stays_pending_when_still_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_path = root / ".agentflow/herdr/sessions.json"
            cli._private_atomic_json(state_path, {
                "schema": "agentflow.herdr", "version": 1,
                "sessions": {"task-1": {
                    "root": str(root), "task_id": "task-1", "claim_id": "claim-1",
                    "lease_id": "lease-1", "provider": "codex", "launch_id": "launch-1",
                    "status": "identity_pending", "pane_id": "pane-real",
                    "result": None, "binding": None, "attempt": 1, "attempts": [],
                }},
            })
            still_pending = mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "result": {
                        "type": "agent_info",
                        "agent": {"pane_id": "pane-real", "agent": "codex", "agent_session": None},
                    },
                }),
                stderr="",
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/herdr"), \
                 mock.patch.object(cli.subprocess, "run", return_value=still_pending):
                changed = cli._resolve_pending_identity(root, "task-1")
            self.assertFalse(changed)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["sessions"]["task-1"]["status"], "identity_pending")

    def test_verify_launch_authority_separates_workspace_and_workflow_root(self) -> None:
        """AFREL-012: workspace_root (filesystem) and workflow_root (Beads id)
        are distinct; a legitimate Beads-root claim must not be rejected just
        because it is not equal to the filesystem workspace path, and Beads
        itself -- not the local cache -- is the authority."""
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1",
                "parent": workflow_root,
                "status": "in_progress",
                "assignee": "writer",
                "metadata": {
                    "agentflow": {
                        "root": workflow_root, "task": "task-1",
                        "actor": "writer", "claim_id": "claim-1",
                        "claim_token": "opaque-claim-token-012345678901234567890123",
                    }
                },
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                identity = cli._verify_launch_authority(
                    workspace_root, "task-1", "opaque-claim-token-012345678901234567890123", "lease-1",
                    workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                )
            self.assertEqual(identity["root"], workflow_root)
            self.assertEqual(identity["actor"], "writer")

    def test_verify_launch_authority_rejects_beads_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1", "parent": workflow_root, "status": "in_progress", "assignee": "writer",
                "metadata": {"agentflow": {"root": "some-other-root", "task": "task-1", "claim_id": "claim-1"}},
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                with self.assertRaises(ValueError):
                    cli._verify_launch_authority(
                        workspace_root, "task-1", "claim-1", "lease-1",
                        workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                    )

    def test_verify_launch_authority_rejects_unrelated_root_ancestry(self) -> None:
        """AFREL-029: metadata claims the approved root, but the issue's
        real Beads parent is a different root entirely -- ancestry, not
        copied metadata, must decide."""
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1", "parent": "unrelated-root", "status": "in_progress", "assignee": "writer",
                "metadata": {
                    "agentflow": {
                        "root": workflow_root, "task": "task-1", "claim_id": "claim-1",
                        "claim_token": f"{workflow_root}/task-1/writer",
                    }
                },
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                with self.assertRaises(ValueError):
                    cli._verify_launch_authority(
                        workspace_root, "task-1", "claim-1", "lease-1",
                        workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                    )

    def test_verify_launch_authority_rejects_reassigned_task(self) -> None:
        """AFREL-029: the claim/metadata look right, but Beads now shows a
        different real assignee -- the task was reassigned or intruded on."""
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1", "parent": workflow_root, "status": "in_progress", "assignee": "intruder",
                "metadata": {
                    "agentflow": {
                        "root": workflow_root, "task": "task-1", "claim_id": "claim-1",
                        "claim_token": f"{workflow_root}/task-1/writer",
                    }
                },
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                with self.assertRaises(ValueError):
                    cli._verify_launch_authority(
                        workspace_root, "task-1", "claim-1", "lease-1",
                        workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                    )

    def test_verify_launch_authority_rejects_forged_metadata_actor(self) -> None:
        """AFREL-037: exact reproduction -- approved-root task assigned
        `writer` (ancestry, status, and claim all check out), but
        metadata.agentflow.actor says `intruder`, and the requested actor
        is `writer`. Ancestry/assignee alone is not enough; the stored
        actor field itself must also equal the requested actor."""
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1", "parent": workflow_root, "status": "in_progress", "assignee": "writer",
                "metadata": {
                    "agentflow": {
                        "root": workflow_root, "task": "task-1", "actor": "intruder",
                        "claim_id": "claim-1", "claim_token": f"{workflow_root}/task-1/writer",
                    }
                },
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                with self.assertRaises(ValueError):
                    cli._verify_launch_authority(
                        workspace_root, "task-1", "claim-1", "lease-1",
                        workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                    )

    def test_verify_launch_authority_rejects_claim_not_bound_to_requested_actor(self) -> None:
        """AFREL-037: the claim token is real and the stored actor matches,
        but it was minted for a DIFFERENT actor than the one requested --
        an actor-neutral claim replay must not be accepted."""
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            issue = {
                "id": "task-1", "parent": workflow_root, "status": "in_progress", "assignee": "writer",
                "metadata": {
                    "agentflow": {
                        "root": workflow_root, "task": "task-1", "actor": "writer",
                        "claim_id": "claim-1", "claim_token": f"{workflow_root}/task-1/someone-else",
                    }
                },
            }
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue):
                with self.assertRaises(ValueError):
                    cli._verify_launch_authority(
                        workspace_root, "task-1", "claim-token-not-supplied", "lease-1",
                        workflow_root=workflow_root, beads_cwd=workspace_root, actor="writer",
                    )

    def test_verify_launch_authority_requires_explicit_actor_for_workflow_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace_root = Path(temp).resolve()
            workflow_root = "coding-agents-mol-example"
            namespaced_state = cli._controller_state_dir(workspace_root, workflow_root) / "state.json"
            namespaced_state.parent.mkdir(parents=True)
            namespaced_state.write_text(
                json.dumps({"lease": {"root": str(workspace_root), "token": "lease-1"}}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                cli._verify_launch_authority(
                    workspace_root, "task-1", "claim-1", "lease-1",
                    workflow_root=workflow_root, beads_cwd=workspace_root, actor="",
                )

    def test_root_acceptance_rejects_closed_status_without_a_matrix(self) -> None:
        """AFREL-011: a closed root is never proof by itself."""
        for status in ("closed", "done", "completed"):
            self.assertFalse(cli._root_acceptance_passed({"status": status}))

    def test_root_acceptance_rejects_loose_status_strings(self) -> None:
        for loose_status in ("complete", "approved", "passed"):
            issue = {
                "status": "open",
                "metadata": {"agentflow": {"acceptance": {"status": loose_status}}},
            }
            self.assertFalse(cli._root_acceptance_passed(issue))

    def test_root_acceptance_rejects_matrix_bound_to_another_root(self) -> None:
        issue = {
            "id": "root-a",
            "metadata": {"agentflow": {"acceptance": {
                "version": 1,
                "task_id": "root-b",
                "rows": [{
                    "id": "R1", "outcome": "done", "owner": "controller",
                    "lane": "static", "planned_evidence": "test",
                    "status": "passed", "actual_evidence": "other-root evidence",
                }],
            }}},
        }
        self.assertFalse(cli._root_acceptance_passed(issue))

    def test_root_acceptance_requires_canonical_passed_or_evidenced_waived(self) -> None:
        matrix = {
            "version": 1, "task_id": "t1",
            "rows": [
                {
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                },
                {
                    "id": "R2", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "waived", "note": "approved exception",
                    "approval_ref": "approval-1",
                },
            ],
        }
        issue = {"id": "t1", "status": "open", "metadata": {"agentflow": {"acceptance": matrix}}}
        # A waiver is authorized ONLY by a separate, finalized decision bead
        # carrying typed approval metadata bound to this exact root/row -- not
        # by a closed bead whose title merely says "Waiver approval".
        approval_bead = {
            "id": "approval-1", "status": "closed", "title": "Waiver approval",
            "metadata": {"agentflow": {"waiver_approval": {
                "schema": "agentflow.waiver-approval@1",
                "decision": "approved",
                "approval_ref": "approval-1",
                "workflow_root": "t1",
                "task": "t1",
                "acceptance_id": "R2",
                "approved_by": "release-manager",
                "approved_at": "2026-07-01T00:00:00Z",
            }}},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            controller = cli.controller_backend.RootController(
                str(root), "release-controller",
                state_path=cli._controller_state_dir(root, "t1") / "state.json",
            )
            lease = controller.acquire()
            authority_secret = _controller_authority(root, "t1", lease)
            controller.approve_waiver(
                workflow_root="t1", task="t1", acceptance_id="R2",
                approval_ref="approval-1", approved_by="release-manager",
                approved_at="2026-07-01T00:00:00Z",
                authority_secret=authority_secret, lease=lease,
            )
            with mock.patch.object(
                cli.beads_backend, "get_issue", return_value=approval_bead,
            ):
                self.assertTrue(cli._root_acceptance_passed(issue, beads_cwd=root))

    def test_root_acceptance_rejects_waived_without_note_or_passed_without_evidence(self) -> None:
        base_row = {"id": "R1", "outcome": "x", "owner": "o", "lane": "static", "planned_evidence": "e"}
        waived_no_note = {
            "version": 1, "task_id": "t1",
            "rows": [{**base_row, "status": "waived"}],
        }
        passed_no_evidence = {
            "version": 1, "task_id": "t1",
            "rows": [{**base_row, "status": "passed"}],
        }
        for matrix in (waived_no_note, passed_no_evidence):
            issue = {"status": "open", "metadata": {"agentflow": {"acceptance": matrix}}}
            self.assertFalse(cli._root_acceptance_passed(issue))

    def test_root_acceptance_rejects_any_planned_row(self) -> None:
        matrix = {
            "version": 1, "task_id": "t1",
            "rows": [
                {
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                },
                {
                    "id": "R2", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "planned",
                },
            ],
        }
        issue = {"status": "open", "metadata": {"agentflow": {"acceptance": matrix}}}
        self.assertFalse(cli._root_acceptance_passed(issue))

    def test_herdr_result_rejects_secret_like_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_path = root / ".agentflow/herdr/sessions.json"
            state_path.parent.mkdir(parents=True)
            binding = {
                "root": str(root), "task_id": "task-1", "claim_id": "claim-1", "lease_id": "lease-1",
                "launch_id": "launch-1", "pane_id": "pane-1", "provider": "codex",
                "session_id": "session-1", "created_at": "now", "launched_at": "now",
            }
            cli._private_atomic_json(state_path, {"schema": "agentflow.herdr", "version": 1, "sessions": {"task-1": {"binding": binding}}})
            result_file = root / "result.json"
            result_file.write_text(json.dumps({
                "task_id": "task-1", "launch_id": "launch-1", "provider": "codex",
                "session_id": "session-1", "outcome": "completed",
                "evidence": [{"api_key": "do-not-store"}],
            }), encoding="utf-8")
            result = cli.herdr_result(argparse.Namespace(
                root=str(root), task="task-1", file=str(result_file), launch_id="",
                provider="", session_id="", outcome="", state_path="", json=True,
                _controller_ingest=True,
            ))
            self.assertEqual(result, 2)
            self.assertNotIn("do-not-store", state_path.read_text(encoding="utf-8"))
    def test_hook_logs_without_prompt_content_or_domain_routing(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "secret-session-id",
            "cwd": str(Path.home() / "work/repo"),
            "model": "test-model",
            "prompt": "Rework this private training lab",
        }
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state, "AGENTFLOW_STATE_HOME": ""}
        ), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(payload))
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = cli.hook(argparse.Namespace(provider="codex", event=""))
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), "")
            event_text = (Path(state) / "agentflow/events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("Rework this", event_text)
            self.assertNotIn("secret-session-id", event_text)
            self.assertIn("test-model", event_text)

    def test_session_start_hook_is_generic(self) -> None:
        payload = {"hook_event_name": "SessionStart", "prompt": "Rework a training lab"}
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state, "AGENTFLOW_STATE_HOME": ""}
        ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, mock.patch.object(cli.beads_backend, "prime", return_value=""):
            self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)
            response = json.loads(stdout.getvalue())
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("project-owned domain skills", context)
            self.assertNotIn("training", context.lower())
            self.assertNotIn("private", context.lower())

    def test_session_start_hook_injects_beads_only_when_active(self) -> None:
        payload = {"hook_event_name": "SessionStart", "cwd": "/tmp/project"}
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state, "AGENTFLOW_STATE_HOME": ""}
        ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, mock.patch.object(
            cli.beads_backend, "prime", return_value="BEADS PRIME CONTEXT"
        ) as prime:
            self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)
            response = json.loads(stdout.getvalue())
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertIn("BEADS PRIME CONTEXT", context)
            self.assertIn("untrusted task data", context)
            self.assertIn("never present a bare bead ID", context)
            prime.assert_called_once()

    def test_copilot_session_hook_uses_additional_context_json(self) -> None:
        payload = {"hookEventName": "sessionStart", "cwd": "/tmp/project"}
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state, "AGENTFLOW_STATE_HOME": ""}
        ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, mock.patch.object(cli.beads_backend, "prime", return_value="bead context"):
            self.assertEqual(cli.hook(argparse.Namespace(provider="copilot", event="")), 0)
            response = json.loads(stdout.getvalue())
            self.assertIn("bead context", response["additionalContext"])

    def test_handoff_has_terse_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "handoff.md"
            args = argparse.Namespace(
                to="claude",
                title="Bounded change",
                goal="Observable outcome",
                lane="external",
                base="main@abc123",
                dependency=["#11"],
                done_when=["Focused test passes"],
                context=["src/example.py"],
                require_skill=["project-domain-skill"],
                constraint=["No API changes"],
                check=["python3 -m unittest"],
                budget=["20 minutes; one retry"],
                issue="#12",
                branch="agent/12",
                out=str(output),
            )
            self.assertEqual(cli.handoff_create(args), 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("Observable outcome", text)
            self.assertIn("no raw logs", text)
            self.assertIn("#12", text)
            self.assertIn("Execution lane: external", text)
            self.assertIn("Base: main@abc123", text)
            self.assertIn("20 minutes; one retry", text)
            self.assertIn("BLOCKED: <one decision or dependency>", text)
            self.assertIn("the user may attach directly", text)
            self.assertIn("`project-domain-skill`", text)
            self.assertIn("Next request:", text)
            self.assertIn("do not present a bare task or bead ID", text)
            manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["lane"], "external")
            self.assertEqual(manifest["task_id"], "#12")
            self.assertFalse(manifest["delegation_allowed"])
            self.assertEqual(manifest["required_skills"], ["project-domain-skill"])

    def test_acceptance_matrix_and_external_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "source.md"
            context.write_text("bounded source\n", encoding="utf-8")
            matrix = root / "acceptance.json"
            acceptance_args = argparse.Namespace(
                task="LAB-1",
                row=[
                    "A1::check rejects unsolved state::writer::local-runtime::fresh cluster result",
                    "A2::hosted setup succeeds::controller::hosted-runtime::hosted CI URL",
                ],
                out=str(matrix),
            )
            self.assertEqual(cli.acceptance_create(acceptance_args), 0)
            self.assertEqual(cli.acceptance_validate(argparse.Namespace(file=str(matrix))), 0)
            self.assertEqual(
                cli.acceptance_set(
                    argparse.Namespace(
                        file=str(matrix),
                        id="A1",
                        status="passed",
                        evidence="runtime-result.md",
                        note="",
                    )
                ),
                0,
            )
            self.assertEqual(cli.acceptance_validate(argparse.Namespace(file=str(matrix))), 0)

            output = root / "handoff.md"
            handoff_args = argparse.Namespace(
                to="claude",
                title="Review lab",
                goal="Return reproducible findings",
                task_id="LAB-1-REVIEW",
                task_class="focused-review",
                lane="external",
                tool_profile="shell-readonly",
                output_boundary=str(root),
                require_tool=[],
                allow_delegation=False,
                return_type="review",
                max_ai_credits=None,
                acceptance_matrix=str(matrix),
                base="",
                dependency=[],
                done_when=["Review returns a verdict"],
                context=[str(context)],
                constraint=["Read-only"],
                check=["test -s source.md"],
                budget=["20 minutes; one retry; stop on blocker"],
                issue="#1",
                branch="",
                out=str(output),
                cwd=str(root),
            )
            self.assertEqual(cli.handoff_create(handoff_args), 0)
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd="", require_matrix=True)
                )
            self.assertEqual(result, 0)
            handoff_text = output.read_text(encoding="utf-8")
            self.assertIn("| A1 | check rejects unsolved state", handoff_text)
            self.assertIn("stable ID, severity, class", handoff_text)

    def test_external_preflight_rejects_implicit_tool_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "source.md"
            context.write_text("source\n", encoding="utf-8")
            output = root / "handoff.md"
            args = argparse.Namespace(
                to="codex",
                title="External task",
                goal="Return evidence",
                task_id="TASK-2",
                task_class="focused-review",
                lane="external",
                tool_profile="provider-default",
                output_boundary=str(root),
                require_tool=[],
                allow_delegation=False,
                return_type="result",
                max_ai_credits=None,
                acceptance_matrix="",
                base="",
                dependency=[],
                done_when=["Evidence returned"],
                context=[str(context)],
                constraint=[],
                check=[],
                budget=["10 minutes; no retry"],
                issue="",
                branch="",
                out=str(output),
                cwd=str(root),
            )
            self.assertEqual(cli.handoff_create(args), 0)
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd="", require_matrix=False)
                )
            self.assertEqual(result, 2)

    def test_gitless_external_handoff_does_not_require_fake_git_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            context = root / "source.md"
            context.write_text("source\n", encoding="utf-8")
            output = root / ".agentflow/handoffs/gitless.md"
            args = argparse.Namespace(
                to="codex",
                title="Gitless task",
                goal="Return one artifact.",
                task_id="DIR-1",
                task_class="implementation",
                role="writer",
                lane="external",
                tool_profile="shell-write",
                output_boundary=str(root),
                require_tool=[],
                require_skill=[],
                allow_delegation=False,
                return_type="result",
                max_ai_credits=None,
                acceptance_matrix="",
                base="",
                dependency=[],
                done_when=["Artifact exists"],
                context=[str(context)],
                constraint=[],
                check=[],
                budget=["10 minutes; no retry; stop on blocker"],
                issue="",
                branch="",
                out=str(output),
                cwd=str(root),
            )
            self.assertEqual(cli.handoff_create(args), 0)
            manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workspace_kind"], "directory")
            self.assertEqual(manifest["base"], "")
            self.assertIn(".agentflow/tmp/", (root / ".gitignore").read_text(encoding="utf-8"))
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"):
                self.assertEqual(
                    cli.handoff_preflight(
                        argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                    ),
                    0,
                )

    def test_gitless_shared_server_request_uses_safe_embedded_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = {
                "path": str(root / ".beads"),
                "prefix": "work",
                "database_path": str(root / ".beads/embeddeddolt"),
                "_agentflow_gitless": True,
            }
            args = argparse.Namespace(
                path=str(root),
                prefix="",
                mode="shared-server",
                tracked=False,
                refresh_formulas=False,
                refresh_context=False,
            )
            with mock.patch.object(
                cli.beads_backend, "initialize", return_value=("created", workspace)
            ) as initialize, mock.patch.object(
                cli, "_install_beads_formula", return_value=("created", root / ".beads/formula")
            ), mock.patch.object(
                cli, "_install_beads_prime", return_value=("created", root / ".beads/PRIME.md")
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(cli.beads_init(args), 0)
            initialize.assert_called_once_with(
                root,
                mode="embedded",
                stealth=True,
                prefix=cli._slug(root.name),
                gitless=True,
            )
            self.assertIn("Gitless folders can run parallel workers", stdout.getvalue())
            self.assertIn("workspace directory (no Git)", stdout.getvalue())
            self.assertFalse((root / ".git").exists())

    def test_provider_aware_skill_resolution_uses_project_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            contents = {
                "codex": (
                    Path(".agents/skills"),
                    b"---\nname: domain-skill\ndescription: Codex canonical.\n---\n",
                ),
                "claude": (
                    Path(".claude/skills"),
                    b"---\nname: domain-skill\ndescription: Claude adapter.\n---\n",
                ),
                "copilot": (
                    Path(".github/skills"),
                    b"---\nname: domain-skill\ndescription: Copilot adapter.\n---\n",
                ),
            }
            for base, content in contents.values():
                entrypoint = root / base / "domain-skill/SKILL.md"
                entrypoint.parent.mkdir(parents=True)
                entrypoint.write_bytes(content)

            with mock.patch.object(cli, "_repository_root", return_value=root):
                for provider, (base, content) in contents.items():
                    resolved, errors = cli._resolve_required_skills(
                        provider, root, ["domain-skill"]
                    )
                    self.assertEqual(errors, [])
                    self.assertEqual(resolved[0]["entrypoint"], str(root / base / "domain-skill/SKILL.md"))
                    self.assertEqual(
                        resolved[0]["entrypoint_sha256"],
                        cli.hashlib.sha256(content).hexdigest(),
                    )
                    self.assertEqual(resolved[0]["digest_schema"], "agentflow.skill-pin@2")
                    self.assertEqual(resolved[0]["package_count"], 1)
                    self.assertEqual(resolved[0]["file_count"], 1)

    def test_registered_external_skill_resolution_is_exact_and_provider_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            root = directory / "project"
            external = directory / "external/domain-skill"
            root.mkdir()
            external.mkdir(parents=True)
            (external / "SKILL.md").write_text(
                "---\nname: domain-skill\ndescription: External.\n---\n",
                encoding="utf-8",
            )
            shared = cli.project_config_backend.default_data()
            local = cli.project_config_backend.default_local_data()
            local["skills"] = [{
                "name": "domain-skill",
                "path": str(external),
                "providers": ["claude"],
            }]
            cli.project_config_backend.write_layer(root, shared, local=False)
            cli.project_config_backend.write_layer(root, local, local=True)
            claude_link = root / ".claude/skills/domain-skill"
            claude_link.parent.mkdir(parents=True)
            claude_link.symlink_to(external, target_is_directory=True)

            resolved, errors = cli._resolve_required_skills(
                "claude", root, ["domain-skill"]
            )
            self.assertEqual(errors, [])
            self.assertEqual(resolved[0]["source"], str(external / "SKILL.md"))
            self.assertEqual(resolved[0]["registered_source"], str(external))

            codex_link = root / ".agents/skills/domain-skill"
            codex_link.parent.mkdir(parents=True)
            codex_link.symlink_to(external, target_is_directory=True)
            resolved, errors = cli._resolve_required_skills(
                "codex", root, ["domain-skill"]
            )
            self.assertEqual(resolved, [])
            self.assertTrue(any("not approved for codex" in error for error in errors))

    def test_external_skill_resolution_rejects_unregistered_retarget_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            root = directory / "project"
            approved = directory / "external/approved"
            retargeted = directory / "external/retargeted"
            sibling = directory / "external/sibling"
            root.mkdir()
            for source in (approved, retargeted, sibling):
                source.mkdir(parents=True)
                (source / "SKILL.md").write_text(
                    "---\nname: domain-skill\ndescription: External.\n---\n",
                    encoding="utf-8",
                )
            shared = cli.project_config_backend.default_data()
            cli.project_config_backend.write_layer(root, shared, local=False)
            link = root / ".claude/skills/domain-skill"
            link.parent.mkdir(parents=True)
            link.symlink_to(retargeted, target_is_directory=True)

            resolved, errors = cli._resolve_required_skills(
                "claude", root, ["domain-skill"]
            )
            self.assertEqual(resolved, [])
            self.assertTrue(any("is not registered" in error for error in errors))

            local = cli.project_config_backend.default_local_data()
            local["skills"] = [{
                "name": "domain-skill",
                "path": str(approved),
                "providers": ["claude"],
            }]
            cli.project_config_backend.write_layer(root, local, local=True)
            resolved, errors = cli._resolve_required_skills(
                "claude", root, ["domain-skill"]
            )
            self.assertEqual(resolved, [])
            self.assertTrue(any("does not match registered source" in error for error in errors))

            link.unlink()
            link.symlink_to(approved, target_is_directory=True)
            (approved / "SKILL.md").write_text(
                "---\nname: domain-skill\ndescription: External.\n---\n"
                "Read `../sibling/SKILL.md`.\n",
                encoding="utf-8",
            )
            resolved, errors = cli._resolve_required_skills(
                "claude", root, ["domain-skill"]
            )
            self.assertEqual(resolved, [])
            self.assertTrue(any("escapes approved skill roots" in error for error in errors))

    def test_claude_adapter_pin_covers_canonical_skill_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            adapter = root / ".claude/skills/domain-skill/SKILL.md"
            canonical = root / ".agents/skills/domain-skill/SKILL.md"
            reference = canonical.parent / "references/guidance.md"
            adapter.parent.mkdir(parents=True)
            reference.parent.mkdir(parents=True)
            adapter.write_text(
                "---\nname: domain-skill\ndescription: Claude adapter.\n---\n"
                "Read `../../../.agents/skills/domain-skill/SKILL.md` completely.\n",
                encoding="utf-8",
            )
            canonical.write_text(
                "---\nname: domain-skill\ndescription: Canonical.\n---\n",
                encoding="utf-8",
            )
            reference.write_text("first\n", encoding="utf-8")

            with mock.patch.object(cli, "_repository_root", return_value=root):
                first, errors = cli._resolve_required_skills("claude", root, ["domain-skill"])
                self.assertEqual(errors, [])
                self.assertEqual(first[0]["package_count"], 2)
                self.assertEqual(first[0]["file_count"], 3)
                entrypoint_digest = first[0]["entrypoint_sha256"]
                package_digest = first[0]["sha256"]

                reference.write_text("second\n", encoding="utf-8")
                second, errors = cli._resolve_required_skills("claude", root, ["domain-skill"])
                self.assertEqual(errors, [])
                self.assertEqual(second[0]["entrypoint_sha256"], entrypoint_digest)
                self.assertNotEqual(second[0]["sha256"], package_digest)

    def test_transitive_skill_references_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            adapter = root / ".claude/skills/domain-skill/SKILL.md"
            canonical = root / ".agents/skills/domain-skill/SKILL.md"
            adapter.parent.mkdir(parents=True)
            canonical.parent.mkdir(parents=True)
            adapter.write_text(
                "---\nname: domain-skill\ndescription: Claude adapter.\n---\n"
                "Read `../../../.agents/skills/domain-skill/SKILL.md`.\n",
                encoding="utf-8",
            )
            canonical.write_text(
                "---\nname: domain-skill\ndescription: Canonical.\n---\n"
                "Read `../../../.claude/skills/domain-skill/SKILL.md`.\n",
                encoding="utf-8",
            )
            with mock.patch.object(cli, "_repository_root", return_value=root):
                resolved, errors = cli._resolve_required_skills(
                    "claude", root, ["domain-skill"]
                )
            self.assertEqual(resolved, [])
            self.assertTrue(any("cyclic transitive skill reference" in error for error in errors))

            canonical.write_text(
                "---\nname: domain-skill\ndescription: Canonical.\n---\n"
                "Read `../../../outside/SKILL.md`.\n",
                encoding="utf-8",
            )
            with mock.patch.object(cli, "_repository_root", return_value=root):
                resolved, errors = cli._resolve_required_skills(
                    "claude", root, ["domain-skill"]
                )
            self.assertEqual(resolved, [])
            self.assertTrue(
                any("transitive skill entrypoint is unavailable" in error for error in errors)
            )

            canonical.write_text(
                "---\nname: domain-skill\ndescription: Canonical.\n---\n"
                "Read `../../../../outside/SKILL.md`.\n",
                encoding="utf-8",
            )
            with mock.patch.object(cli, "_repository_root", return_value=root):
                resolved, errors = cli._resolve_required_skills(
                    "claude", root, ["domain-skill"]
                )
            self.assertEqual(resolved, [])
            self.assertTrue(
                any("escapes approved skill roots" in error for error in errors)
            )

    def test_skill_package_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            skill = root / ".agents/skills/domain-skill/SKILL.md"
            target = root / "shared.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: domain-skill\ndescription: Canonical.\n---\n",
                encoding="utf-8",
            )
            target.write_text("shared\n", encoding="utf-8")
            (skill.parent / "shared.md").symlink_to(target)
            with mock.patch.object(cli, "_repository_root", return_value=root):
                resolved, errors = cli._resolve_required_skills(
                    "codex", root, ["domain-skill"]
                )
            self.assertEqual(resolved, [])
            self.assertTrue(
                any("unsupported symlink" in error for error in errors)
            )

    def test_preflight_pins_required_skill_and_launch_rejects_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            context = root / "source.md"
            context.write_text("source\n", encoding="utf-8")
            skill = root / ".claude/skills/domain-skill/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: domain-skill\ndescription: Test.\n---\n", encoding="utf-8")
            output = root / "handoff.md"
            args = argparse.Namespace(
                to="claude",
                title="External task",
                goal="Return evidence",
                task_id="TASK-SKILL",
                task_class="focused-review",
                lane="external",
                tool_profile="shell-readonly",
                output_boundary=str(root),
                require_tool=[],
                require_skill=["domain-skill"],
                allow_delegation=False,
                return_type="result",
                max_ai_credits=None,
                acceptance_matrix="",
                base="",
                dependency=[],
                done_when=["Evidence returned"],
                context=[str(context)],
                constraint=[],
                check=[],
                budget=["10 minutes; no retry; stop on blocker"],
                issue="",
                branch="",
                out=str(output),
                cwd=str(root),
            )
            self.assertEqual(cli.handoff_create(args), 0)
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), mock.patch.object(
                cli, "_repository_root", return_value=root
            ):
                self.assertEqual(
                    cli.handoff_preflight(
                        argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                    ),
                    0,
                )
            manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["resolved_skills"][0]["entrypoint"], str(skill))
            self.assertEqual(
                manifest["resolved_skills"][0]["entrypoint_sha256"],
                cli.hashlib.sha256(skill.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["resolved_skills"][0]["digest_schema"],
                "agentflow.skill-pin@2",
            )
            self.assertEqual(manifest["preflight"]["cwd"], str(root))

            manifest["required_skills"] = ["missing-domain-skill"]
            output.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), mock.patch.object(
                cli, "_repository_root", return_value=root
            ), mock.patch.object(cli.subprocess, "call") as launch:
                self.assertEqual(
                    cli.handoff_launch(
                        argparse.Namespace(
                            provider="claude",
                            file=str(output),
                            cwd=str(root),
                            print_command=False,
                        )
                    ),
                    2,
                )
                launch.assert_not_called()
            failed_manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(failed_manifest["resolved_skills"], [])
            self.assertNotIn("preflight", failed_manifest)

    def test_handoff_rejects_unsafe_skill_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "handoff.md"
            args = argparse.Namespace(
                to="codex",
                title="Unsafe skill",
                goal="Do not traverse paths",
                issue="",
                branch="",
                done_when=[],
                context=[],
                require_skill=["../outside"],
                constraint=[],
                check=[],
                budget=[],
                out=str(output),
            )
            self.assertEqual(cli.handoff_create(args), 2)
            self.assertFalse(output.exists())

    def test_handoff_from_bead_materializes_transient_prompt_and_durable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cli.subprocess.run(
                ["git", "init", "-b", "main", str(root)],
                capture_output=True,
                text=True,
                check=True,
            )
            cli.subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                capture_output=True, text=True, check=True,
            )
            cli.subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Agentflow Test"],
                capture_output=True, text=True, check=True,
            )
            cli.subprocess.run(
                ["git", "-C", str(root), "commit", "--allow-empty", "-m", "base"],
                capture_output=True, text=True, check=True,
            )
            output = root / ".agentflow/tmp/handoffs/work-1-codex.md"
            issue = {
                "id": "work-1",
                "title": "Bounded bead",
                "description": "Produce the observable result.",
                "acceptance_criteria": "Focused test passes",
                "status": "open",
                "metadata": {
                    "agentflow": {
                        "required_skills": [],
                        "checks": ["python3 -m unittest"],
                    }
                },
            }
            args = argparse.Namespace(
                bead="work-1",
                to="codex",
                cwd=str(root),
                task_class="implementation",
                role="writer",
                lane="native",
                tool_profile="provider-default",
                output_boundary=str(root),
                require_tool=[],
                require_skill=[],
                allow_delegation=False,
                return_type="result",
                max_ai_credits=None,
                base="",
                branch="",
                context=[],
                constraint=[],
                check=[],
                budget=[],
                out=str(output),
            )
            with mock.patch.object(
                cli.beads_backend, "get_issue", return_value=issue
            ), mock.patch.object(
                cli.beads_backend, "update_agentflow_metadata"
            ) as update:
                self.assertEqual(cli.handoff_from_bead(args), 0)
            manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["state_backend"], "beads")
            self.assertEqual(manifest["bead_id"], "work-1")
            self.assertEqual(manifest["checks"], ["python3 -m unittest"])
            prompt = output.read_text(encoding="utf-8")
            self.assertIn("bead:work-1", prompt)
            self.assertIn("untrusted task data", prompt)
            self.assertIn("Bounded bead (work-1)", prompt)
            update.assert_called_once()

    def test_handoff_from_bead_ignores_stale_git_identity_in_gitless_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            issue = {
                "id": "work-dir-1",
                "title": "Bounded directory task",
                "description": "Produce the observable result.",
                "acceptance_criteria": "Focused test passes",
                "status": "open",
                "metadata": {
                    "agentflow": {
                        "base": "old-branch@deadbeef",
                        "branch": "old-branch",
                        "checks": ["python3 -m unittest"],
                    }
                },
            }
            output = root / ".agentflow/tmp/handoffs/work-dir-1-codex.md"
            args = argparse.Namespace(
                bead="work-dir-1", to="codex", cwd=str(root),
                task_class="implementation", role="writer", lane="native",
                tool_profile="provider-default", output_boundary=str(root),
                require_tool=[], require_skill=[], allow_delegation=False,
                return_type="result", max_ai_credits=None, base="", branch="",
                context=[], constraint=[], check=[], budget=[], out=str(output),
            )
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"):
                self.assertEqual(cli.handoff_from_bead(args), 0)

            self.assertFalse((root / ".git").exists())
            manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workspace_kind"], "directory")
            self.assertEqual(manifest["workspace_contract"]["root"], str(root))
            self.assertEqual(manifest["workspace_contract"]["base"], None)
            self.assertEqual(manifest["base"], "")
            self.assertEqual(manifest["branch"], "")

    def test_beads_explain_translates_blocked_ids(self) -> None:
        issue = {
            "id": "work-review",
            "title": "Pedagogy review",
            "status": "open",
            "assignee": "",
            "labels": ["af:stage:review"],
            "dependencies": [
                {
                    "id": "work-code",
                    "title": "Implement learner journey",
                    "status": "in_progress",
                    "dependency_type": "blocks",
                }
            ],
        }
        with mock.patch.object(cli.beads_backend, "workspace", return_value={"path": "/tmp"}), mock.patch.object(
            cli.beads_backend, "get_issue", return_value=issue
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = cli.beads_explain(
                argparse.Namespace(bead=["work-review"], cwd="/tmp")
            )
        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("Pedagogy review (work-review)", output)
        self.assertIn("Status: blocked by dependencies", output)
        self.assertIn("Implement learner journey (work-code)", output)
        self.assertIn("Next request:", output)

    def test_worker_pull_claims_scoped_labelled_work(self) -> None:
        root = {"id": "root", "title": "Approved workflow"}
        issue = {
            "id": "work-1",
            "title": "Review lifecycle",
            "status": "in_progress",
            "assignee": "reviewer-1",
            "labels": ["af:stage:review", "af:cap:lifecycle"],
        }
        with mock.patch.object(cli.beads_backend, "workspace", return_value={"path": "/tmp"}), mock.patch.object(
            cli.beads_backend, "get_issue", side_effect=[root, issue]
        ), mock.patch.object(
            cli.beads_backend, "claim_ready", return_value=issue
        ) as claim, mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = cli.worker_pull(
                argparse.Namespace(
                    root="root",
                    stage="review",
                    actor="reviewer-1",
                    capability=["lifecycle"],
                    cwd="/tmp",
                    json=False,
                    once=True,
                )
            )
        self.assertEqual(result, 0)
        claim.assert_called_once_with(
            Path("/tmp").resolve(),
            parent="root",
            labels=["af:stage:review", "af:cap:lifecycle"],
            actor="reviewer-1",
        )
        self.assertIn("Review lifecycle (work-1)", stdout.getvalue())
        self.assertIn("Next request:", stdout.getvalue())

    def test_claim_ready_uses_atomic_shared_claim_after_empty_assigned_queue(self) -> None:
        empty = cli.subprocess.CompletedProcess([], 0, "[]", "")
        claimed = cli.subprocess.CompletedProcess(
            [],
            0,
            json.dumps([{"id": "work-1", "title": "Ready work"}]),
            "",
        )
        with mock.patch.object(
            cli.beads_backend, "run", side_effect=[empty, claimed]
        ) as run:
            issue = cli.beads_backend.claim_ready(
                Path("/tmp"),
                parent="root",
                labels=["af:stage:review"],
                actor="reviewer-1",
            )
        self.assertEqual(issue["id"], "work-1")
        first_arguments = run.call_args_list[0].args
        second_arguments = run.call_args_list[1].args
        self.assertIn("--assignee", first_arguments)
        self.assertNotIn("--claim", first_arguments)
        self.assertIn("--unassigned", second_arguments)
        self.assertIn("--claim", second_arguments)

    def test_create_issue_prevents_parent_queue_label_inheritance(self) -> None:
        created = cli.subprocess.CompletedProcess(
            [], 0, json.dumps({"id": "fix-1", "title": "Fix review"}), ""
        )
        with mock.patch.object(cli.beads_backend, "run", return_value=created) as run:
            issue = cli.beads_backend.create_issue(
                Path("/tmp"),
                title="Fix review",
                description="Correct the finding.",
                acceptance="Regression passes.",
                parent="root",
                labels=["af:stage:fix"],
            )
        self.assertEqual(issue["id"], "fix-1")
        self.assertIn("--no-inherit-labels", run.call_args.args)

    def test_review_route_fix_returns_work_to_original_writer(self) -> None:
        review = {
            "id": "review-1",
            "title": "Lifecycle review",
            "status": "in_progress",
            "parent": "root",
            "metadata": {"agentflow": {}},
        }
        writer = {
            "id": "writer-1",
            "title": "Implement setup lifecycle",
            "status": "closed",
            "parent": "root",
            "assignee": "writer-agent",
            "labels": ["af:stage:code", "af:cap:training"],
        }
        fix = {"id": "fix-1", "title": "Fix R1 from Lifecycle review"}
        with mock.patch.object(
            cli.beads_backend, "get_issue", side_effect=[review, writer]
        ), mock.patch.object(
            cli.beads_backend, "create_issue", return_value=fix
        ) as create, mock.patch.object(
            cli.beads_backend, "update_issue"
        ) as update_issue, mock.patch.object(
            cli.beads_backend, "update_agentflow_metadata"
        ), mock.patch.object(
            cli.beads_backend, "add_comment"
        ), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = cli.review_route_fix(
                argparse.Namespace(
                    review="review-1",
                    writer="writer-1",
                    finding="R1",
                    title="",
                    description="Correct the lifecycle failure.",
                    acceptance="Focused regression passes.",
                    assignee="",
                    cwd="/tmp",
                )
            )
        self.assertEqual(result, 0)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["assignee"], "writer-agent")
        self.assertEqual(kwargs["dependencies"], ["blocks:review-1"])
        self.assertIn("af:stage:fix", kwargs["labels"])
        self.assertIn("af:cap:training", kwargs["labels"])
        update_issue.assert_called_once_with(
            Path("/tmp").resolve(), "review-1", status="open"
        )
        self.assertIn("Next request:", stdout.getvalue())

    def test_ci_watch_closes_passing_gate_without_model_session(self) -> None:
        issue = {
            "id": "ci-1",
            "title": "Deterministic CI",
            "status": "open",
            "parent": "root",
            "labels": ["af:stage:ci"],
        }
        with mock.patch.object(
            cli.beads_backend, "get_issue", return_value=issue
        ), mock.patch.object(
            cli, "_github_pr_checks", return_value=("passed", {"pass": 3}, [], "")
        ), mock.patch.object(
            cli.beads_backend, "update_agentflow_metadata"
        ), mock.patch.object(
            cli.beads_backend, "add_comment"
        ), mock.patch.object(
            cli.beads_backend, "close_issue"
        ) as close, mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = cli.ci_watch(
                argparse.Namespace(
                    bead="ci-1",
                    pr="42",
                    repo="owner/repo",
                    writer="writer-1",
                    assignee="",
                    required=False,
                    once=True,
                    interval_seconds=30,
                    timeout_seconds=60,
                    cwd="/tmp",
                )
            )
        self.assertEqual(result, 0)
        close.assert_called_once()
        self.assertIn("CI PASSED", stdout.getvalue())
        self.assertIn("Next request:", stdout.getvalue())

    def test_github_pr_checks_classifies_machine_readable_buckets(self) -> None:
        cases = [
            (
                [{"name": "unit", "bucket": "pass"}],
                "passed",
                [],
            ),
            (
                [
                    {"name": "unit", "bucket": "pass"},
                    {"name": "integration", "bucket": "pending"},
                ],
                "pending",
                [],
            ),
            (
                [
                    {"name": "unit", "bucket": "fail"},
                    {"name": "lint", "bucket": "cancel"},
                ],
                "failed",
                ["unit", "lint"],
            ),
        ]
        for checks, expected_status, expected_failures in cases:
            completed = cli.subprocess.CompletedProcess(
                [], 1 if expected_status == "failed" else 0, json.dumps(checks), ""
            )
            with self.subTest(expected_status=expected_status), mock.patch.object(
                cli.shutil, "which", return_value="/opt/homebrew/bin/gh"
            ), mock.patch.object(cli.subprocess, "run", return_value=completed):
                status, _, failures, error = cli._github_pr_checks(
                    pr="42", repo="owner/repo", required=False
                )
            self.assertEqual(status, expected_status)
            self.assertEqual(failures, expected_failures)
            self.assertEqual(error, "")

    def test_ci_watch_routes_failure_once(self) -> None:
        issue = {
            "id": "ci-1",
            "title": "Deterministic CI",
            "status": "open",
            "parent": "root",
            "labels": ["af:stage:ci"],
        }
        triage = {"id": "triage-ci", "title": "Triage failing CI for PR #42"}
        with mock.patch.object(
            cli.beads_backend, "get_issue", return_value=issue
        ), mock.patch.object(
            cli,
            "_github_pr_checks",
            return_value=("failed", {"fail": 1}, ["unit"], ""),
        ), mock.patch.object(
            cli, "_ci_failure_bead", return_value=triage
        ) as route, mock.patch.object(
            cli.beads_backend, "update_agentflow_metadata"
        ), mock.patch.object(
            cli.beads_backend, "update_issue"
        ), mock.patch.object(
            cli.beads_backend, "add_comment"
        ), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = cli.ci_watch(
                argparse.Namespace(
                    bead="ci-1",
                    pr="42",
                    repo="owner/repo",
                    writer="writer-1",
                    assignee="",
                    required=False,
                    once=True,
                    interval_seconds=30,
                    timeout_seconds=60,
                    cwd="/tmp",
                )
            )
        self.assertEqual(result, 1)
        route.assert_called_once()
        self.assertIn(
            "ROUTED Triage failing CI for PR #42 (triage-ci)", stdout.getvalue()
        )
        self.assertIn("Next request:", stdout.getvalue())

    def test_ci_failure_creates_unassigned_dedicated_triage_work(self) -> None:
        ci_issue = {
            "id": "ci-1",
            "title": "Deterministic CI",
            "status": "open",
            "parent": "root",
            "metadata": {"agentflow": {}},
        }
        writer = {
            "id": "writer-1",
            "title": "Implement feature",
            "status": "closed",
            "parent": "root",
            "assignee": "expensive-writer",
        }
        created = {"id": "triage-1", "title": "Triage failing CI for PR #42"}
        with mock.patch.object(
            cli.beads_backend, "get_issue", return_value=writer
        ), mock.patch.object(
            cli.beads_backend, "create_issue", return_value=created
        ) as create:
            result = cli._ci_failure_bead(
                task_cwd=Path("/tmp"),
                ci_issue=ci_issue,
                pr="42",
                repo="owner/repo",
                writer_id="writer-1",
                assignee_override="",
                failed_names=["unit"],
            )
        self.assertEqual(result["id"], "triage-1")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["assignee"], "")
        self.assertIn("af:stage:ci-triage", kwargs["labels"])
        self.assertIn("af:role:ci", kwargs["labels"])
        self.assertNotIn("af:stage:fix", kwargs["labels"])
        self.assertEqual(kwargs["dependencies"], ["blocks:ci-1"])
        self.assertEqual(
            kwargs["metadata"]["agentflow"]["original_writer"], "writer-1"
        )

    def test_local_exclude_keeps_transient_agentflow_files_out_of_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cli.subprocess.run(
                ["git", "init", "-b", "main", str(root)],
                capture_output=True,
                text=True,
                check=True,
            )
            state, path = cli._ensure_git_local_exclude(root)
            self.assertEqual(state, "created")
            self.assertIsNotNone(path)
            self.assertTrue(
                cli._git_ignores(root, root / ".agentflow/tmp/handoffs/example.md")
            )
            second, _ = cli._ensure_git_local_exclude(root)
            self.assertEqual(second, "unchanged")
            assert path is not None
            self.assertEqual(path.read_text(encoding="utf-8").count(cli.GITIGNORE_BEGIN), 1)

    def test_acceptance_can_use_bead_metadata(self) -> None:
        data = {
            "version": 1,
            "task_id": "work-2",
            "created_at": "now",
            "rows": [
                {
                    "id": "A1",
                    "outcome": "passes",
                    "owner": "writer",
                    "lane": "static",
                    "planned_evidence": "test",
                    "status": "planned",
                    "actual_evidence": "",
                    "note": "",
                }
            ],
        }
        issue = {"metadata": {"agentflow": {"acceptance": data}}}
        with mock.patch.object(
            cli.beads_backend, "get_issue", return_value=issue
        ), mock.patch.object(cli.beads_backend, "update_agentflow_metadata") as update:
            self.assertEqual(
                cli.acceptance_validate(argparse.Namespace(file="bead:work-2", cwd=".")),
                0,
            )
            self.assertEqual(
                cli.acceptance_set(
                    argparse.Namespace(
                        file="bead:work-2",
                        cwd=".",
                        id="A1",
                        status="passed",
                        evidence="test output",
                        note="",
                    )
                ),
                0,
            )
            update.assert_called_once()

    def test_usage_and_review_records_capture_yield(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": temp, "AGENTFLOW_STATE_HOME": ""}
        ):
            usage_args = argparse.Namespace(
                provider="copilot",
                auth_mode="subscription",
                plan="business",
                model="claude-sonnet",
                effort="high",
                fast_mode="off",
                project="training",
                task="LAB-1-REVIEW",
                role="reviewer",
                task_class="focused-review",
                remaining=None,
                remaining_before=80.0,
                remaining_after=79.5,
                credits_used=50,
                window="monthly",
                reset_at="",
                elapsed_seconds=120,
                retries=1,
                check=["review schema validated"],
                files=4,
                bytes=4096,
                findings=3,
                accepted_findings=2,
                outcome="completed",
                source="/usage",
                note="",
            )
            self.assertEqual(cli.usage_record(usage_args), 0)
            usage = json.loads(
                (Path(temp) / "agentflow/usage.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(usage["task"], "LAB-1-REVIEW")
            self.assertEqual(usage["accepted_findings"], 2)
            self.assertEqual(usage["remaining_percent"], 79.5)

            review_path = Path(temp) / "reviews.jsonl"
            review_args = argparse.Namespace(
                task="LAB-1-REVIEW",
                finding="F-001",
                severity="high",
                finding_class="correctness",
                status="accepted",
                evidence="source.md:4",
                reproduction="focused check fails",
                expected="pass",
                actual="fail",
                correction="change predicate",
                gate="local-runtime",
                confidence="high",
                owner="writer",
                note="",
                out=str(review_path),
            )
            self.assertEqual(cli.review_record(review_args), 0)
            review = json.loads(review_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(review["status"], "accepted")
            self.assertEqual(review["authoritative_gate"], "local-runtime")

    def test_init_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            agents = target / "AGENTS.md"
            gitignore = target / ".gitignore"
            agents.write_text("user rules\n", encoding="utf-8")
            gitignore.write_text("*.env\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(target))), 0)
            self.assertEqual(agents.read_text(encoding="utf-8"), "user rules\n")
            self.assertFalse((target / ".codex/hooks.json").exists())
            self.assertTrue((target / ".claude/settings.json").is_file())
            first_gitignore = gitignore.read_text(encoding="utf-8")
            self.assertTrue(first_gitignore.startswith("*.env\n"))
            self.assertIn(".agentflow/handoffs/", first_gitignore)
            self.assertIn(".agentflow/tmp/", first_gitignore)
            self.assertIn(".agentflow/logs/", first_gitignore)
            self.assertIn(".agentflow/worktrees/", first_gitignore)

            self.assertEqual(cli.init_project(argparse.Namespace(path=str(target))), 0)
            self.assertEqual(gitignore.read_text(encoding="utf-8"), first_gitignore)
            self.assertEqual(first_gitignore.count(cli.GITIGNORE_BEGIN), 1)

    def test_init_enables_metadata_memory_for_new_git_and_gitless_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            git_root = Path(temp) / "git-project"
            git_root.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(git_root)], check=True,
                capture_output=True, text=True,
            )
            gitless_root = Path(temp) / "gitless-project"
            gitless_root.mkdir()

            for target in (git_root, gitless_root):
                # An uninitialized workspace stays safely disabled at runtime;
                # only `init` opts its newly created config in.
                self.assertFalse(
                    cli.project_config_backend.load_for_dispatch(target)["memory"]["enabled"]
                )
                with self.subTest(workspace="git" if (target / ".git").exists() else "gitless"), \
                     mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(cli.main(["init", str(target)]), 0)
                config_path = target / ".agentflow/config.json"
                config_bytes = config_path.read_bytes()
                data = json.loads(config_bytes)
                self.assertTrue(data["memory"]["enabled"])
                self.assertFalse(data["memory"]["on_prompt"])
                # Re-running init must not rewrite a user's chosen value.
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(cli.main(["init", str(target)]), 0)
                self.assertEqual(config_path.read_bytes(), config_bytes)

    def test_init_no_memory_opt_out_applies_only_to_new_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "new-project"
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.main(["init", str(root), "--no-memory"]), 0)
            data = json.loads((root / ".agentflow/config.json").read_text(encoding="utf-8"))
            self.assertFalse(data["memory"]["enabled"])
            self.assertFalse(data["memory"]["on_prompt"])
            path = root / ".agentflow/config.json"
            opted_out_bytes = path.read_bytes()
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.main(["init", str(root), "--no-memory"]), 0)
            self.assertEqual(path.read_bytes(), opted_out_bytes)

            # Init remains non-overwriting even when the opt-out flag is repeated.
            data["memory"]["enabled"] = True
            chosen_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
            path.write_bytes(chosen_bytes)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.main(["init", str(root), "--no-memory"]), 0)
            self.assertEqual(path.read_bytes(), chosen_bytes)

    def test_init_preserves_existing_memory_settings_and_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for enabled in (False, True):
                with self.subTest(enabled=enabled):
                    root = Path(temp) / f"existing-{enabled}"
                    root.mkdir()
                    config = cli.project_config_backend.default_data()
                    config["memory"]["enabled"] = enabled
                    cli.project_config_backend.write_layer(root, config, local=False)
                    path = root / ".agentflow/config.json"
                    before = path.read_bytes()
                    with mock.patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(cli.main(["init", str(root)]), 0)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(
                        cli.project_config_backend.load(root)["memory"]["enabled"], enabled
                    )

            # A valid schema-v1 config that predates memory is not silently
            # upgraded or activated by init.
            legacy_root = Path(temp) / "legacy-project"
            legacy_root.mkdir()
            legacy = cli.project_config_backend.default_data()
            legacy.pop("memory")
            legacy_bytes = (json.dumps(legacy, indent=2, sort_keys=True) + "\n").encode()
            legacy_path = legacy_root / ".agentflow/config.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_bytes(legacy_bytes)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.main(["init", str(legacy_root)]), 0)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
            loaded = cli.project_config_backend.load(legacy_root)
            self.assertNotIn("memory", json.loads(legacy_path.read_text(encoding="utf-8")))
            self.assertFalse(cli.project_config_backend.memory_settings(loaded)["enabled"])

    def test_init_preserves_local_memory_override_while_seeding_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for enabled in (False, True):
                with self.subTest(local_enabled=enabled):
                    root = Path(temp) / f"local-{enabled}"
                    root.mkdir()
                    local = cli.project_config_backend.default_local_data()
                    local["memory"] = dict(cli.project_config_backend.DEFAULT_MEMORY)
                    local["memory"]["enabled"] = enabled
                    cli.project_config_backend.write_layer(root, local, local=True)
                    local_path = root / ".agentflow/config.local.json"
                    local_bytes = local_path.read_bytes()
                    with mock.patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(cli.main(["init", str(root)]), 0)
                    shared = json.loads((root / ".agentflow/config.json").read_text(encoding="utf-8"))
                    self.assertTrue(shared["memory"]["enabled"])
                    self.assertEqual(local_path.read_bytes(), local_bytes)
                    self.assertEqual(
                        cli.project_config_backend.load(root)["memory"]["enabled"], enabled
                    )
                    with mock.patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(cli.main(["init", str(root)]), 0)
                    self.assertEqual(local_path.read_bytes(), local_bytes)

    def test_init_refuses_malformed_gitignore_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            (target / ".gitignore").write_text(cli.GITIGNORE_BEGIN + "\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(target))), 2)
            self.assertEqual(
                (target / ".gitignore").read_text(encoding="utf-8"), cli.GITIGNORE_BEGIN + "\n"
            )
            self.assertFalse((target / ".claude/settings.json").exists())

    def test_init_preserves_existing_codex_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            hook = target / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text(
                cli.packaged_resources.text("templates", "user", "codex-hooks.json"),
                encoding="utf-8",
            )
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(target))), 0)
            self.assertTrue(hook.exists())

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            hook = target / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text('{"description":"custom","hooks":{}}\n', encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(target))), 0)
            self.assertTrue(hook.exists())


def _controller_args(root: Path, **overrides: object) -> argparse.Namespace:
    defaults = dict(
        root=str(root), controller="agentflow-controller", state_path="",
        checkpoint_path="", stale_after=300.0, takeover=False,
        workflow_root="wf-root", resume_token="", resume_key_file="", json=True, cwd=str(root),
        # Keep test lock files in their temporary workspace. Production CLI
        # users cannot configure this internal-only injection point.
        _supervisor_lock_path=str(
            root / ".agentflow/test-controller-locks"
            / f"{cli._controller_namespace(str(overrides.get('workflow_root', 'wf-root')))}.lock"
        ),
        # Tests exercise one deterministic transition at a time; the
        # autonomous loop itself (AFREL-020) is covered by its own
        # dedicated tests further down.
        once=True, poll_interval=0.01, deadline=5.0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_controller_json(func, args) -> dict:
    buffer = io.StringIO()
    with mock.patch("sys.stdout", buffer):
        func(args)
    return json.loads(buffer.getvalue())


def _expired_preidentity_fixture(base: Path) -> tuple[ValidLaunch, object, dict, Path, dict]:
    """Create one real signed identity-pending launch and its halted checkpoint."""
    fixture = ValidLaunch(base / "workspace", seed_lease=False)
    fixture.task_issue["status"] = "in_progress"
    fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
        "deadline_seconds": 600, "max_retries": 1,
    }
    fixture.lease = fixture._seed_lease()
    fixture._persist_claim()
    fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
    with fixture.beads_patches(), \
         mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
         mock.patch.object(
             cli.subprocess, "run",
             side_effect=fixture.herdr_run(
                 stdout=_agent_started_stdout(
                     fixture.provider, pane_id="pane-pending", agent_session=None,
                 ),
             ),
         ), contextlib.redirect_stdout(io.StringIO()):
        if cli.herdr_launch(fixture.launch_args()) != 0:
            raise AssertionError("fixture provider launch did not reach identity_pending")

    state_path = fixture.root / ".agentflow/herdr/sessions.json"
    herdr_state = json.loads(state_path.read_text(encoding="utf-8"))
    record = herdr_state["sessions"][fixture.task_id]
    if record.get("status") != "identity_pending":
        raise AssertionError("fixture did not persist identity_pending")
    attempts = json.loads(json.dumps(record.get("attempts", [])))
    contract_path = Path(record["return_channel"]["contract_path"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    fixture.contract = contract
    # The authenticated launch is aged past its original one-shot deadline
    # while preserving a valid controller MAC and the existing ledger row.
    contract["deadline_epoch"] = time.time() - 10
    contract["authority_hmac"] = cli._authority_mac(
        fixture.authority_secret, contract, domain="return-contract-v1",
    )
    cli._private_atomic_json(contract_path, contract)
    with cli._herdr_transaction(state_path) as state:
        current = state["sessions"][fixture.task_id]
        current["deadline_epoch"] = contract["deadline_epoch"]
        channel = current["return_channel"]
        channel["contract_binding"] = contract
        channel["contract_sha256"] = cli._canonical_json_digest(contract)
    ledger_path = cli._execution_limit_ledger_path(fixture.root, fixture.workflow_root)
    key = cli._execution_ledger_key(fixture.root, fixture.workflow_root, fixture.task_id)
    with cli._execution_limit_ledger_transaction(ledger_path) as ledger:
        entry = ledger["entries"][key]
        entry["deadline_epoch"] = contract["deadline_epoch"]
        entry["authority_hmac"] = cli._authority_mac(
            fixture.authority_secret, entry, domain="execution-limit-ledger-v1",
        )
    controller = cli.controller_backend.RootController(
        str(fixture.root), fixture.controller,
        state_path=cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json",
        checkpoint_path=cli._controller_state_dir(fixture.root, fixture.workflow_root) / "checkpoint.json",
    )
    controller.reserve_active_task({
        "task": fixture.task_id, "root": str(fixture.root),
        "actor": fixture.controller, "claim_id": "claim-1",
    }, lease=fixture.lease)
    controller.bind_active_task(
        fixture.task_id, session_id="", state="identity_pending", lease=fixture.lease,
    )
    controller.halt(
        "blocked",
        f"USER_ACTION_REQUIRED: task {fixture.task_id} provider identity never resolved within 1800s",
        lease=fixture.lease,
    )
    return fixture, controller, contract, ledger_path, {"attempts": attempts, "state_path": state_path}


class PreidentityRecoveryCommandTests(unittest.TestCase):
    def _rewrite_deadline(self, fixture: ValidLaunch, contract: dict, ledger_path: Path, deadline: float) -> None:
        contract["deadline_epoch"] = deadline
        contract["authority_hmac"] = cli._authority_mac(
            fixture.authority_secret, contract, domain="return-contract-v1",
        )
        contract_path = Path(contract["contract_path"])
        cli._private_atomic_json(contract_path, contract)
        state_path = fixture.root / ".agentflow/herdr/sessions.json"
        with cli._herdr_transaction(state_path) as state:
            record = state["sessions"][fixture.task_id]
            record["deadline_epoch"] = deadline
            channel = record["return_channel"]
            channel["contract_binding"] = contract
            channel["contract_sha256"] = cli._canonical_json_digest(contract)
        key = cli._execution_ledger_key(fixture.root, fixture.workflow_root, fixture.task_id)
        with cli._execution_limit_ledger_transaction(ledger_path) as ledger:
            entry = ledger["entries"][key]
            entry["deadline_epoch"] = deadline
            entry["authority_hmac"] = cli._authority_mac(
                fixture.authority_secret, entry, domain="execution-limit-ledger-v1",
            )

    def _args(self, fixture: ValidLaunch, *, task: str | None = None, launch_id: str | None = None,
              pane_id: str | None = None, workflow_root: str | None = None,
              controller: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            root=str(fixture.root), workflow_root=workflow_root or fixture.workflow_root,
            controller=controller or fixture.controller, state_path="", checkpoint_path="",
            stale_after=300.0, task=task or fixture.task_id,
            launch_id=launch_id or str(fixture.contract["launch_id"] if hasattr(fixture, "contract") else ""),
            pane_id=pane_id or "pane-pending", json=True,
            _supervisor_lock_path=str(
                fixture.root / ".agentflow/test-controller-locks/recovery.lock"
            ),
        )

    def _run_recovery(self, fixture: ValidLaunch, args: argparse.Namespace, *, pane_response: str | None = None,
                      pane_returncode: int = 1, descendants_override: list | None = None,
                      legacy_scope: bool = False) -> tuple[dict, mock.Mock]:
        output = json.dumps({
            "error": {"code": "pane_not_found", "message": "pane missing"},
            "id": "cli:pane:get",
        }) if pane_response is None else pane_response
        pane_probe = mock.Mock(return_value=subprocess.CompletedProcess(
            ["herdr", "pane", "get", args.pane_id], pane_returncode,
            stdout=output, stderr="",
        ))
        descendants_patch = (
            mock.patch.object(cli.beads_backend, "root_descendants", return_value=descendants_override)
            if descendants_override is not None else contextlib.nullcontext()
        )
        with fixture.beads_patches(), descendants_patch, \
             mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
             mock.patch.object(cli.subprocess, "run", pane_probe):
            runner = (
                cli.controller_retire_superseded_legacy_preidentity
                if legacy_scope else cli.controller_recover_preidentity
            )
            payload = _run_controller_json(runner, args)
        return payload, pane_probe

    def _legacy_fixture(self, base: Path):
        fixture, controller, contract, ledger_path, baseline = _expired_preidentity_fixture(base)
        fixture.task_issue["status"] = "blocked"
        fixture.task_issue["metadata"]["agentflow"]["launch"].pop("execution_limits", None)
        for field in ("execution_limits", "deadline_epoch", "attempt", "max_attempts"):
            contract.pop(field, None)
        contract["authority_hmac"] = cli._authority_mac(
            fixture.authority_secret, contract, domain="return-contract-v1",
        )
        contract_path = Path(contract["contract_path"])
        cli._private_atomic_json(contract_path, contract)
        with cli._herdr_transaction(baseline["state_path"]) as state:
            record = state["sessions"][fixture.task_id]
            for field in (
                "execution_limits", "deadline_epoch", "max_attempts",
                "execution_limit_capabilities",
            ):
                record.pop(field, None)
            channel = record["return_channel"]
            channel["contract_binding"] = contract
            channel["contract_sha256"] = cli._canonical_json_digest(contract)
        ledger_path.unlink(missing_ok=True)
        checkpoint = controller._load_checkpoint()
        checkpoint.update({
            "state": "draining", "status": "draining", "terminal": False,
            "terminal_reason": "accumulated and truncated prior halt history",
        })
        cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint)
        fixture.contract = contract
        return fixture, controller, contract, ledger_path, baseline

    def test_recovery_revokes_channel_then_clears_pointer_and_allows_only_explicit_distinct_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, contract, ledger_path, baseline = _expired_preidentity_fixture(base)
                fixture.contract = contract
                old_attempts = baseline["attempts"]
                checkpoint_before_cancel = controller._load_checkpoint()
                # Exercise the pre-dormant legacy state: clean release had no
                # stored proof hash, so recovery must use only the exact
                # signed launch plus the protected canonical credential.
                controller.release(fixture.lease)
                released = json.loads(controller.state_path.read_text(encoding="utf-8"))
                released.pop("dormant_lease", None)
                cli._private_atomic_json(controller.state_path, released)
                args = self._args(fixture)
                result, pane_probe = self._run_recovery(fixture, args)
                self.assertTrue(result["ok"], result)
                self.assertEqual(pane_probe.call_count, 2)
                checkpoint = controller._load_checkpoint()
                self.assertEqual(checkpoint["task"], str(fixture.root))
                self.assertEqual(checkpoint["active_tasks"], [])
                self.assertTrue(checkpoint["terminal"])
                self.assertTrue(checkpoint["last_check"].startswith("cancelled expired preidentity launch "))
                state_path = baseline["state_path"]
                herdr_state = json.loads(state_path.read_text(encoding="utf-8"))
                record = herdr_state["sessions"][fixture.task_id]
                self.assertEqual(record["status"], "cancelled_preidentity")
                self.assertEqual(record["return_channel"]["state"], "revoked")
                self.assertIsNone(record["result"])
                self.assertEqual(record["attempts"], old_attempts)
                key = cli._execution_ledger_key(fixture.root, fixture.workflow_root, fixture.task_id)
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                entry = ledger["entries"][key]
                self.assertEqual(entry["status"], "expired")
                self.assertEqual(entry["attempt"], 1)
                self.assertEqual(entry["deadline_epoch"], contract["deadline_epoch"])
                self.assertEqual(entry["launch_identity"]["launch_id"], contract["launch_id"])
                with self.assertRaisesRegex(ValueError, "deadline expired"):
                    cli._reserve_execution_attempt(
                        fixture.root, fixture.workflow_root, fixture.task_id,
                        limits=execution_limits_backend.parse_limits(contract["execution_limits"]),
                        policy_max_attempts=entry["max_attempts"],
                        authority_secret=fixture.authority_secret,
                        claim_id=contract["claim_id"], lease_id=contract["lease_id"],
                        launch_id="retry-must-not-launch",
                        controller_id=contract["controller_id"],
                        continuity_id=contract["continuity_id"],
                        lease_epoch=contract["lease_epoch"],
                    )
                still_expired = json.loads(ledger_path.read_text(encoding="utf-8"))["entries"][key]
                self.assertEqual(still_expired["attempt"], entry["attempt"])
                self.assertEqual(still_expired["deadline_epoch"], entry["deadline_epoch"])

                # A valid late inbox cannot consume a revoked channel.
                channel = record["return_channel"]
                result_path = Path(channel["result_path"])
                result_path.write_text(json.dumps({"outcome": "completed", "acceptance_results": []}), encoding="utf-8")
                marker = {
                    "schema": "agentflow.result-submission@1",
                    "contract_sha256": cli._file_sha256(Path(channel["contract_path"])),
                    "result_sha256": cli._file_sha256(result_path),
                    "submitted_at": cli._now(),
                }
                cli._private_atomic_json(Path(channel["submission_file"]), marker)
                late = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
                self.assertEqual(late.status, "pending")
                current = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                self.assertEqual(current["return_channel"]["state"], "revoked")
                self.assertIsNone(current["result"])

                # Simulate a crash after the Herdr tombstone/ledger write but
                # before checkpoint pointer clearing. Retry is idempotent and
                # uses the same active lease without a pane query.
                cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint_before_cancel)
                retried, retry_probe = self._run_recovery(fixture, args)
                self.assertTrue(retried["ok"], retried)
                self.assertEqual(retry_probe.call_count, 0)
                self.assertEqual(controller._load_checkpoint()["active_tasks"], [])

                # Explicit reopening identifies one distinct ready child;
                # stale in-progress work is ignored only while its signed
                # cancellation and expired ledger record still verify.
                ready = dict(fixture.task_issue, id="task-2", status="open", assignee="")
                ready["parent"] = fixture.workflow_root
                ready["metadata"] = {"agentflow": {}}
                def get_issue(_cwd, issue_id):
                    if issue_id == fixture.workflow_root:
                        return fixture.root_issue
                    if issue_id == fixture.task_id:
                        return fixture.task_issue
                    if issue_id == "task-2":
                        return ready
                    raise AssertionError(issue_id)
                def run_beads(_cwd, *argv):
                    if argv[0] == "ready":
                        rows = [] if "--assignee" in argv else [{"id": "task-2"}]
                        return subprocess.CompletedProcess(["bd", *argv], 0, json.dumps(rows), "")
                    if argv[0] == "update" and argv[1] == "task-2":
                        ready.update({"status": "in_progress", "assignee": fixture.controller})
                        return subprocess.CompletedProcess(["bd", *argv], 0, "", "")
                    raise AssertionError(argv)
                current_state = json.loads(controller.state_path.read_text(encoding="utf-8"))
                current_lease = cli.controller_backend.Lease.from_dict(current_state["lease"])
                credentials = cli._read_controller_credentials(cli._resume_key_path(self._args(fixture)))
                resume_args = _controller_args(
                    fixture.root, workflow_root=fixture.workflow_root,
                    once=True,
                    _supervisor_lock_path=str(fixture.root / ".agentflow/test-controller-locks/resume.lock"),
                )
                resume_args.continue_after_cancelled_preidentity = fixture.task_id
                resume_args.continue_task = "task-2"
                with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue, ready]), \
                     mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                     mock.patch.object(cli.beads_backend, "run", side_effect=run_beads), \
                     mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                     mock.patch.object(cli, "_persist_claim_identity"), \
                     mock.patch.object(cli, "_bind_current_controller_sessions"), \
                     mock.patch.object(cli, "_dispatch_via_herdr", return_value=lambda _task: {
                         "state": "running", "session_id": "session-task-2",
                     }):
                    resumed = _run_controller_json(cli.controller_resume, resume_args)
                self.assertTrue(resumed["ok"], resumed)
                self.assertEqual(resumed["result"]["state"], "running")
                self.assertEqual(resumed["result"]["checkpoint"]["task"], "task-2")
                self.assertNotEqual(resumed["result"]["checkpoint"]["task"], fixture.task_id)
                self.assertEqual(current_lease.continuity_id, resumed["lease"]["continuity_id"])

    def test_recovery_rejects_unknown_panes_missing_ledger_and_wrong_task_binding_without_clearing(self) -> None:
        cases = (
            "unknown-pane", "live-pane", "missing-ledger", "unexpired-deadline",
            "wrong-root", "wrong-task", "wrong-controller", "wrong-continuity",
            "bad-signature", "wrong-claim", "committed-binding", "committed-result",
            "sibling-active",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    fixture, controller, _contract, ledger_path, baseline = _expired_preidentity_fixture(base)
                    fixture.contract = _contract
                    if case == "missing-ledger":
                        ledger_path.unlink()
                    if case == "unexpired-deadline":
                        self._rewrite_deadline(fixture, _contract, ledger_path, time.time() + 3600)
                    if case == "wrong-claim":
                        fixture.task_issue["metadata"]["agentflow"]["claim_id"] = "wrong-claim"
                    if case == "committed-binding":
                        path = baseline["state_path"]
                        with cli._herdr_transaction(path) as herdr:
                            herdr["sessions"][fixture.task_id]["binding"] = {"session_id": "committed"}
                    if case == "committed-result":
                        with cli._herdr_transaction(baseline["state_path"]) as herdr:
                            herdr["sessions"][fixture.task_id]["result"] = {"outcome": "completed"}
                    if case == "wrong-continuity":
                        credential_path = cli._resume_key_path(self._args(fixture))
                        raw_credential = json.loads(credential_path.read_text(encoding="utf-8"))
                        raw_credential["continuity_id"] = "foreign-continuity"
                        cli._private_atomic_json(credential_path, raw_credential)
                    if case == "bad-signature":
                        contract_path = Path(_contract["contract_path"])
                        bad_contract = json.loads(contract_path.read_text(encoding="utf-8"))
                        bad_contract["authority_hmac"] = "0" * 64
                        cli._private_atomic_json(contract_path, bad_contract)
                        with cli._herdr_transaction(baseline["state_path"]) as herdr:
                            channel = herdr["sessions"][fixture.task_id]["return_channel"]
                            channel["contract_binding"] = bad_contract
                            channel["contract_sha256"] = cli._canonical_json_digest(bad_contract)
                    if case == "sibling-active":
                        result, pane_probe = self._run_recovery(
                            fixture, self._args(fixture), descendants_override=[
                                fixture.task_issue,
                                {"id": "sibling", "status": "in_progress", "parent": fixture.workflow_root},
                            ],
                        )
                        self.assertFalse(result["ok"])
                        self.assertEqual(pane_probe.call_count, 0)
                        self.assertEqual(controller._load_checkpoint()["active_tasks"][0]["task"], fixture.task_id)
                        continue
                    if case == "wrong-root":
                        args = self._args(fixture, workflow_root="foreign-root")
                    elif case == "wrong-task":
                        args = self._args(fixture, task="foreign-task")
                    elif case == "wrong-controller":
                        args = self._args(fixture, controller="foreign-controller")
                    else:
                        args = self._args(fixture)
                    pane_response = None
                    pane_returncode = 1
                    if case == "unknown-pane":
                        pane_response = json.dumps({"error": {"code": "daemon_offline"}, "id": "cli:pane:get"})
                    elif case == "live-pane":
                        pane_response = json.dumps({"id": "cli:pane:get", "result": {"pane_id": "pane-pending"}})
                        pane_returncode = 0
                    result, pane_probe = self._run_recovery(
                        fixture, args, pane_response=pane_response,
                        pane_returncode=pane_returncode,
                    )
                    self.assertFalse(result["ok"], (case, result))
                    if case in {"unknown-pane", "live-pane"}:
                        self.assertEqual(pane_probe.call_count, 1)
                    else:
                        self.assertEqual(pane_probe.call_count, 0)
                    current = json.loads(baseline["state_path"].read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                    self.assertEqual(current["status"], "identity_pending")
                    self.assertEqual(current["return_channel"]["state"], "issued")
                    self.assertEqual(controller._load_checkpoint()["active_tasks"][0]["task"], fixture.task_id)

    def test_recovery_command_and_distinct_continuation_flags_are_registered(self) -> None:
        parser = cli.build_parser()
        recovery = parser.parse_args([
            "controller", "recover-preidentity", "--root", "/tmp/workspace",
            "--workflow-root", "wf", "--task", "task", "--launch-id", "launch",
            "--pane-id", "pane",
        ])
        self.assertIs(recovery.func, cli.controller_recover_preidentity)
        legacy = parser.parse_args([
            "controller", "retire-superseded-legacy-preidentity", "--root", "/tmp/workspace",
            "--workflow-root", "wf", "--task", "task", "--launch-id", "launch", "--pane-id", "pane",
        ])
        self.assertIs(legacy.func, cli.controller_retire_superseded_legacy_preidentity)
        resume = parser.parse_args([
            "controller", "resume", "--root", "/tmp/workspace", "--workflow-root", "wf",
            "--continue-after-cancelled-preidentity", "task", "--continue-task", "child",
        ])
        self.assertEqual(resume.continue_after_cancelled_preidentity, "task")
        self.assertEqual(resume.continue_task, "child")

    def test_retirement_rejects_checkpoint_override_before_any_mutation(self) -> None:
        for legacy_scope in (False, True):
            with self.subTest(legacy_scope=legacy_scope), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    factory = self._legacy_fixture if legacy_scope else _expired_preidentity_fixture
                    fixture, controller, contract, _ledger_path, baseline = factory(base)
                    fixture.contract = contract
                    args = self._args(fixture)
                    shadow_checkpoint = base / "shadow-checkpoint.json"
                    args.checkpoint_path = str(shadow_checkpoint)
                    checkpoint_before = controller.checkpoint_path.read_bytes()
                    state_before = controller.state_path.read_bytes()
                    herdr_before = baseline["state_path"].read_bytes()

                    result, pane_probe = self._run_recovery(
                        fixture, args, legacy_scope=legacy_scope,
                    )

                    self.assertFalse(result["ok"])
                    self.assertIn("canonical namespaced controller checkpoint", result["error"])
                    self.assertEqual(pane_probe.call_count, 0)
                    self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)
                    self.assertEqual(controller.state_path.read_bytes(), state_before)
                    self.assertEqual(baseline["state_path"].read_bytes(), herdr_before)
                    self.assertFalse(shadow_checkpoint.exists())

    def test_superseded_legacy_retirement_preserves_unknown_budget_and_fences_late_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, contract, ledger_path, baseline = self._legacy_fixture(base)
                attempts = baseline["attempts"]
                self.assertFalse(ledger_path.exists())
                self.assertTrue(all(field not in contract for field in (
                    "execution_limits", "deadline_epoch", "expires_at", "issued_at", "attempt", "max_attempts",
                )))
                controller.release(fixture.lease)
                released = json.loads(controller.state_path.read_text(encoding="utf-8"))
                released.pop("dormant_lease", None)
                cli._private_atomic_json(controller.state_path, released)
                result, pane_probe = self._run_recovery(
                    fixture, self._args(fixture), legacy_scope=True,
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["operation"], "retire-superseded-legacy-preidentity")
                self.assertEqual(pane_probe.call_count, 2)
                checkpoint = controller._load_checkpoint()
                self.assertTrue(checkpoint["terminal"])
                self.assertTrue(checkpoint["last_check"].startswith(
                    "cancelled superseded_legacy_scope preidentity launch "
                ))
                record = json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]
                disposition = record["recovery_disposition"]
                self.assertEqual(record["status"], "cancelled_preidentity")
                self.assertEqual(record["return_channel"]["state"], "revoked")
                self.assertEqual(record["attempts"], attempts)
                self.assertEqual(disposition["reason"], "superseded_legacy_scope")
                self.assertEqual(disposition["budget_availability"], "unknown")
                self.assertNotIn("deadline_epoch", disposition)
                self.assertNotIn("execution_limits", disposition)
                self.assertFalse(ledger_path.exists())
                retry, retry_probe = self._run_recovery(
                    fixture, self._args(fixture), legacy_scope=True,
                )
                self.assertTrue(retry["ok"], retry)
                self.assertEqual(retry_probe.call_count, 0)
                self.assertTrue(cli._authenticated_cancelled_preidentity(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                ))
                other_limits = execution_limits_backend.parse_limits({
                    "deadline_seconds": 120, "max_retries": 0,
                })
                assert other_limits is not None
                cli._reserve_execution_attempt(
                    fixture.root, fixture.workflow_root, "other-budgeted-task",
                    limits=other_limits, policy_max_attempts=1,
                    authority_secret=fixture.authority_secret,
                    claim_id="claim-other", lease_id="lease-other", launch_id="launch-other",
                    controller_id=fixture.controller, continuity_id=contract["continuity_id"],
                    lease_epoch=contract["lease_epoch"],
                )
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertNotIn(
                    cli._execution_ledger_key(fixture.root, fixture.workflow_root, fixture.task_id),
                    ledger["entries"],
                )
                self.assertTrue(cli._authenticated_cancelled_preidentity(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                ))

                channel = record["return_channel"]
                result_path = Path(channel["result_path"])
                result_path.write_text(json.dumps({"outcome": "completed", "acceptance_results": []}), encoding="utf-8")
                marker = {
                    "schema": "agentflow.result-submission@1",
                    "contract_sha256": cli._file_sha256(Path(channel["contract_path"])),
                    "result_sha256": cli._file_sha256(result_path), "submitted_at": cli._now(),
                }
                cli._private_atomic_json(Path(channel["submission_file"]), marker)
                late = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
                self.assertEqual(late.status, "pending")
                self.assertIsNone(json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]["result"])
                state = json.loads(controller.state_path.read_text(encoding="utf-8"))
                current_lease = cli.controller_backend.Lease.from_dict(state["lease"])
                ready = {
                    "id": "distinct-ready-task", "status": "open", "assignee": "",
                    "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
                }
                def get_issue(_cwd, issue_id):
                    return {
                        fixture.workflow_root: fixture.root_issue,
                        fixture.task_id: fixture.task_issue,
                        "distinct-ready-task": ready,
                    }[issue_id]
                def run_beads(_cwd, *argv):
                    rows = [] if "--assignee" in argv else [{"id": "distinct-ready-task"}]
                    return subprocess.CompletedProcess(["bd", *argv], 0, json.dumps(rows), "")
                original_metadata = json.loads(json.dumps(fixture.task_issue["metadata"]))
                original_assignee = fixture.task_issue["assignee"]
                for bad_status, bad_owner, bad_claim in (
                    ("open", original_assignee, False),
                    ("closed", original_assignee, False),
                    ("blocked", "foreign-controller", False),
                    ("blocked", original_assignee, True),
                ):
                    fixture.task_issue["status"] = bad_status
                    fixture.task_issue["assignee"] = bad_owner
                    if bad_claim:
                        fixture.task_issue["metadata"]["agentflow"]["claim_id"] = "changed-claim"
                    with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                         mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue, ready]), \
                         mock.patch.object(cli.beads_backend, "run", side_effect=run_beads):
                        with self.assertRaises(cli.controller_backend.ControllerError):
                            cli._verify_cancelled_preidentity_continuation(
                                fixture.root, fixture.workflow_root, fixture.task_id,
                                "distinct-ready-task", controller, current_lease,
                                authority_secret=fixture.authority_secret,
                            )
                    fixture.task_issue["status"] = "blocked"
                    fixture.task_issue["assignee"] = original_assignee
                    fixture.task_issue["metadata"] = json.loads(json.dumps(original_metadata))
                with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue, ready]), \
                     mock.patch.object(cli.beads_backend, "run", side_effect=run_beads):
                    selected = cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id,
                        "distinct-ready-task", controller, current_lease,
                        authority_secret=fixture.authority_secret,
                    )
                self.assertEqual(selected, "distinct-ready-task")
                controller.acknowledge_cancelled_preidentity_halt(
                    fixture.workflow_root, fixture.task_id, "distinct-ready-task", lease=current_lease,
                )
                checkpoint = controller._load_checkpoint()
                self.assertEqual(checkpoint["pending_continuation_task"], "distinct-ready-task")
                self.assertFalse(checkpoint["terminal"])

    def test_legacy_retirement_retries_after_credential_write_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, _contract, ledger_path, baseline = self._legacy_fixture(base)
                controller.release(fixture.lease)
                released = json.loads(controller.state_path.read_text(encoding="utf-8"))
                released.pop("dormant_lease", None)
                cli._private_atomic_json(controller.state_path, released)
                args = self._args(fixture)
                original = cli._controller_credentials
                calls = 0

                def fail_once(*writer_args, **writer_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("credential write crash")
                    return original(*writer_args, **writer_kwargs)

                with mock.patch.object(cli, "_controller_credentials", side_effect=fail_once):
                    first, _ = self._run_recovery(fixture, args, legacy_scope=True)
                    self.assertFalse(first["ok"])
                    self.assertEqual(first["error"], "credential write crash")
                    self.assertEqual(json.loads(controller.state_path.read_text())["epoch"], fixture.lease.epoch)
                    self.assertEqual(controller._load_checkpoint()["state"], "draining")
                    self.assertEqual(
                        json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]["status"],
                        "identity_pending",
                    )
                    second, _ = self._run_recovery(fixture, args, legacy_scope=True)
                self.assertTrue(second["ok"], second)
                self.assertEqual(json.loads(controller.state_path.read_text())["epoch"], fixture.lease.epoch + 1)
                self.assertEqual(
                    json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]["status"],
                    "cancelled_preidentity",
                )
                self.assertFalse(ledger_path.exists())

    def test_superseded_legacy_task_cannot_be_relaunched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, _controller, _contract, _ledger, baseline = self._legacy_fixture(base)
                result, _probe = self._run_recovery(
                    fixture, self._args(fixture), legacy_scope=True,
                )
                self.assertTrue(result["ok"], result)
                state = json.loads(cli._controller_state_dir(
                    fixture.root, fixture.workflow_root,
                ).joinpath("state.json").read_text())
                fixture.lease = cli.controller_backend.Lease.from_dict(state["lease"])
                fixture._persist_claim()
                fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
                launch_args = fixture.launch_args()
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(cli, "_ensure_herdr_server"), \
                     mock.patch.object(cli, "_require_herdr_provider_integration"), \
                     mock.patch.object(cli, "_require_claude_model_switch_version"), \
                     mock.patch.object(cli, "_require_claude_model_switch_hooks"):
                    launch_result = _run_controller_json(cli.herdr_launch, launch_args)
                self.assertFalse(launch_result["ok"], launch_result)
                self.assertIn("collision", launch_result["error"])
                record = json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]
                self.assertEqual(record["status"], "cancelled_preidentity")

    def test_superseded_legacy_rejects_budget_evidence_mismatches_before_pane_probe(self) -> None:
        for case in ("signed-deadline", "malformed-ledger", "malformed-target-ledger", "wrong-claim", "bad-signature"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    fixture, controller, contract, ledger_path, baseline = self._legacy_fixture(base)
                    if case == "signed-deadline":
                        contract["deadline_epoch"] = time.time() - 10
                        contract["authority_hmac"] = cli._authority_mac(
                            fixture.authority_secret, contract, domain="return-contract-v1",
                        )
                        cli._private_atomic_json(Path(contract["contract_path"]), contract)
                        with cli._herdr_transaction(baseline["state_path"]) as state:
                            channel = state["sessions"][fixture.task_id]["return_channel"]
                            channel["contract_binding"] = contract
                            channel["contract_sha256"] = cli._canonical_json_digest(contract)
                    elif case == "malformed-ledger":
                        ledger_path.parent.mkdir(parents=True, exist_ok=True)
                        ledger_path.write_text("not json", encoding="utf-8")
                    elif case == "malformed-target-ledger":
                        ledger_path.parent.mkdir(parents=True, exist_ok=True)
                        ledger_path.write_text(json.dumps({
                            "schema": "agentflow.execution-limit-ledger@1",
                            "entries": {cli._execution_ledger_key(
                                fixture.root, fixture.workflow_root, fixture.task_id,
                            ): {"status": "active"}},
                        }), encoding="utf-8")
                    elif case == "wrong-claim":
                        fixture.task_issue["metadata"]["agentflow"]["claim_id"] = "foreign-claim"
                    else:
                        contract["authority_hmac"] = "0" * 64
                        cli._private_atomic_json(Path(contract["contract_path"]), contract)
                        with cli._herdr_transaction(baseline["state_path"]) as state:
                            channel = state["sessions"][fixture.task_id]["return_channel"]
                            channel["contract_binding"] = contract
                            channel["contract_sha256"] = cli._canonical_json_digest(contract)
                    result, pane_probe = self._run_recovery(
                        fixture, self._args(fixture), legacy_scope=True,
                    )
                    self.assertFalse(result["ok"], (case, result))
                    self.assertEqual(pane_probe.call_count, 0)
                    self.assertEqual(
                        json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]["status"],
                        "identity_pending",
                    )
                    self.assertEqual(controller._load_checkpoint()["state"], "draining")

    def test_superseded_legacy_rejects_active_or_ambiguous_lifecycle_evidence(self) -> None:
        cases = (
            "unknown-pane", "live-pane", "committed-binding", "committed-result",
            "sibling-active", "wrong-continuity", "wrong-epoch", "open-task",
            "closed-task", "changed-owner",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    fixture, controller, _contract, _ledger, baseline = self._legacy_fixture(base)
                    if case == "committed-binding":
                        with cli._herdr_transaction(baseline["state_path"]) as state:
                            state["sessions"][fixture.task_id]["binding"] = {"session_id": "committed"}
                    elif case == "committed-result":
                        with cli._herdr_transaction(baseline["state_path"]) as state:
                            state["sessions"][fixture.task_id]["result"] = {"outcome": "completed"}
                    elif case == "wrong-continuity":
                        credential_path = cli._resume_key_path(self._args(fixture))
                        credentials = json.loads(credential_path.read_text(encoding="utf-8"))
                        credentials["continuity_id"] = "foreign-continuity"
                        cli._private_atomic_json(credential_path, credentials)
                    elif case == "wrong-epoch":
                        state = json.loads(controller.state_path.read_text(encoding="utf-8"))
                        state["epoch"] += 1
                        cli._private_atomic_json(controller.state_path, state)
                    elif case == "open-task":
                        fixture.task_issue["status"] = "open"
                    elif case == "closed-task":
                        fixture.task_issue["status"] = "closed"
                    elif case == "changed-owner":
                        fixture.task_issue["assignee"] = "foreign-controller"
                    if case == "sibling-active":
                        result, pane_probe = self._run_recovery(
                            fixture, self._args(fixture), legacy_scope=True,
                            descendants_override=[
                                fixture.task_issue,
                                {"id": "sibling", "status": "in_progress", "parent": fixture.workflow_root},
                            ],
                        )
                    else:
                        response = None
                        returncode = 1
                        if case == "unknown-pane":
                            response = json.dumps({"error": {"code": "daemon_offline"}, "id": "cli:pane:get"})
                        elif case == "live-pane":
                            response = json.dumps({"id": "cli:pane:get", "result": {"pane_id": "pane-pending"}})
                            returncode = 0
                        result, pane_probe = self._run_recovery(
                            fixture, self._args(fixture), legacy_scope=True,
                            pane_response=response, pane_returncode=returncode,
                        )
                    self.assertFalse(result["ok"], (case, result))
                    self.assertEqual(pane_probe.call_count, 1 if case in {"unknown-pane", "live-pane"} else 0)
                    self.assertEqual(
                        json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]["status"],
                        "identity_pending",
                    )
                    self.assertEqual(controller._load_checkpoint()["state"], "draining")

    def test_dormant_release_recovery_preserves_incarnation_and_rotates_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, contract, _ledger, _baseline = _expired_preidentity_fixture(base)
                controller.release(fixture.lease)
                args = self._args(fixture)
                result, pane_probe = self._run_recovery(fixture, args)
                self.assertTrue(result["ok"], result)
                self.assertEqual(pane_probe.call_count, 2)
                state = json.loads(controller.state_path.read_text(encoding="utf-8"))
                lease = cli.controller_backend.Lease.from_dict(state["lease"])
                self.assertEqual(lease.continuity_id, contract["continuity_id"])
                self.assertEqual(lease.epoch, contract["lease_epoch"] + 1)

    def test_legacy_recovery_retries_after_credential_write_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, contract, _ledger, baseline = _expired_preidentity_fixture(base)
                controller.release(fixture.lease)
                released = json.loads(controller.state_path.read_text(encoding="utf-8"))
                released.pop("dormant_lease", None)
                cli._private_atomic_json(controller.state_path, released)
                args = self._args(fixture)
                checkpoint_epoch = controller._load_checkpoint()["epoch"]
                with mock.patch.object(cli, "_controller_credentials", side_effect=OSError("credential crash")):
                    first, _ = self._run_recovery(fixture, args)
                self.assertEqual(first["error"], "credential crash")
                self.assertEqual(json.loads(controller.state_path.read_text())["epoch"], checkpoint_epoch)
                self.assertEqual(controller._load_checkpoint()["epoch"], checkpoint_epoch)

                second, _ = self._run_recovery(fixture, args)
                self.assertTrue(second["ok"], second)
                state = json.loads(controller.state_path.read_text())
                self.assertEqual(state["epoch"], checkpoint_epoch + 1)
                self.assertNotIn("recovery_reattach", state)
                record = json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]
                self.assertEqual(record["status"], "cancelled_preidentity")
                self.assertEqual(record["attempts"], baseline["attempts"])
                entry = json.loads(_ledger.read_text())["entries"][
                    cli._execution_ledger_key(fixture.root, fixture.workflow_root, fixture.task_id)
                ]
                self.assertEqual(entry["deadline_epoch"], contract["deadline_epoch"])
                self.assertEqual(entry["attempt"], 1)

    def test_dormant_recovery_finishes_signed_pending_credential_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, contract, _ledger, _baseline = _expired_preidentity_fixture(base)
                controller.release(fixture.lease)
                args = self._args(fixture)
                checkpoint_epoch = controller._load_checkpoint()["epoch"]
                writer = cli._controller_credentials

                def write_then_crash(*writer_args, **writer_kwargs):
                    writer(*writer_args, **writer_kwargs)
                    raise OSError("crash after protected credential commit")

                with mock.patch.object(cli, "_controller_credentials", side_effect=write_then_crash):
                    first, _ = self._run_recovery(fixture, args)
                self.assertEqual(first["error"], "crash after protected credential commit")
                staged = json.loads(controller.state_path.read_text())
                self.assertEqual(staged["epoch"], checkpoint_epoch)
                self.assertIsNone(staged.get("lease"))
                self.assertIn("recovery_reattach", staged)
                self.assertEqual(controller._load_checkpoint()["epoch"], checkpoint_epoch)

                second, _ = self._run_recovery(fixture, args)
                self.assertTrue(second["ok"], second)
                current = json.loads(controller.state_path.read_text())
                self.assertEqual(current["epoch"], checkpoint_epoch + 1)
                self.assertNotIn("recovery_reattach", current)
                self.assertEqual(
                    cli.controller_backend.Lease.from_dict(current["lease"]).continuity_id,
                    contract["continuity_id"],
                )

    def test_recovery_accepts_exact_retained_draining_identity_pending_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                fixture, controller, _contract, _ledger, _baseline = _expired_preidentity_fixture(base)
                document = controller._load_checkpoint()
                document.update({
                    "state": "draining", "status": "draining", "terminal": False,
                    "terminal_reason": (
                        "USER_ACTION_REQUIRED: prior no-ready halt; failed sibling; "
                        f"USER_ACTION_REQUIRED: task {fixture.task_id} provider identity never resolved within"
                    )[:500],
                })
                cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, document)
                result, pane_probe = self._run_recovery(fixture, self._args(fixture))
                self.assertTrue(result["ok"], result)
                self.assertEqual(pane_probe.call_count, 2)
                checkpoint = controller._load_checkpoint()
                self.assertTrue(checkpoint["terminal"])
                self.assertEqual(checkpoint["state"], "blocked")
                self.assertTrue(checkpoint["terminal_reason"].startswith("USER_ACTION_REQUIRED: prior no-ready halt"))
                self.assertEqual(checkpoint["active_tasks"], [])

    def test_retained_draining_recovery_rejects_mismatched_or_multiple_active_rows(self) -> None:
        for shape in ("wrong-task", "sibling"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    fixture, controller, _contract, _ledger, baseline = _expired_preidentity_fixture(base)
                    document = controller._load_checkpoint()
                    rows = [dict(row) for row in document["active_tasks"]]
                    if shape == "wrong-task":
                        rows[0]["task"] = "foreign-task"
                    else:
                        rows.append({**rows[0], "task": "sibling-task"})
                    document.update({
                        "state": "draining", "status": "draining", "terminal": False,
                        "terminal_reason": "accumulated and truncated halt history",
                        "active_tasks": rows,
                    })
                    cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, document)
                    result, pane_probe = self._run_recovery(fixture, self._args(fixture))
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(pane_probe.call_count, 0)
                    record = json.loads(baseline["state_path"].read_text())["sessions"][fixture.task_id]
                    self.assertEqual(record["status"], "identity_pending")
                    self.assertEqual(controller._load_checkpoint()["state"], "draining")


class ControllerPendingContinuationTests(unittest.TestCase):
    def _continuation_fixture(self, base: Path, *, ready_task: str = "ready-task"):
        workspace = base / "workspace"
        workspace.mkdir()
        args = _controller_args(
            workspace, workflow_root="wf", once=True,
            continue_after_cancelled_preidentity="old-task",
            continue_task=ready_task,
        )
        controller, _root = cli._controller_instance(args)
        lease = controller.acquire()
        cli._controller_credentials(args, lease)
        controller.halt(
            "blocked",
            "USER_ACTION_REQUIRED: task old-task provider identity never resolved within 1800s",
            lease=lease,
        )
        halted = controller._load_checkpoint()
        halted["last_check"] = "cancelled expired preidentity launch old-launch for old-task"
        cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, halted)
        return args, controller, lease

    def _run_continuation_once(self, args):
        def one_step(step_args, current, root, current_lease, *, operation):
            result = current.resume([], lease=current_lease)
            return ({
                "operation": operation, "ok": True, "root": str(root),
                "result": result.to_dict(),
            }, True)

        with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf", "status": "open"}), \
             mock.patch.object(
                 cli, "_verify_cancelled_preidentity_continuation",
                 return_value=args.continue_task,
             ), \
             mock.patch.object(cli, "_bind_current_controller_sessions"), \
             mock.patch.object(cli, "_controller_step", side_effect=one_step):
            return _run_controller_json(cli.controller_resume, args)

    def _assert_continuation_committed(self, args, controller, ready_task):
        state = json.loads(controller.state_path.read_text())
        checkpoint = controller._load_checkpoint()
        credentials = cli._read_controller_credentials(cli._resume_key_path(args))
        lease = cli.controller_backend.Lease.from_dict(state["lease"])
        self.assertEqual(state["epoch"], checkpoint["epoch"])
        self.assertEqual(checkpoint["pending_continuation_task"], ready_task)
        self.assertTrue(lease.verify_resume_proof(credentials["resume_secret"]))
        self.assertNotIn("continuation_reattach", state)

    def test_invalid_ready_choice_is_rejected_before_epoch_rotation_and_can_be_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args = _controller_args(
                    workspace, workflow_root="wf",
                    continue_after_cancelled_preidentity="old",
                    continue_task="bad-ready-task",
                )
                controller, _root = cli._controller_instance(args)
                lease = controller.acquire()
                cli._controller_credentials(args, lease)
                controller.halt(
                    "blocked",
                    "USER_ACTION_REQUIRED: task old provider identity never resolved within 1800s",
                    lease=lease,
                )
                halted = controller._load_checkpoint()
                halted["last_check"] = "cancelled expired preidentity launch old-launch for old"
                cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, halted)
                initial_epoch = json.loads(controller.state_path.read_text())["epoch"]
                verified: list[str] = []

                def verify(_root, _workflow, _cancelled, ready, *_rest, **_kwargs):
                    verified.append(ready)
                    if ready == "bad-ready-task":
                        raise ValueError("ready task is invalid")
                    return ready

                def one_step(step_args, current, root, current_lease, *, operation):
                    result = current.resume([], lease=current_lease)
                    return ({
                        "operation": operation, "ok": True, "root": str(root),
                        "result": result.to_dict(),
                    }, True)

                with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf", "status": "open"}), \
                     mock.patch.object(cli, "_verify_cancelled_preidentity_continuation", side_effect=verify), \
                     mock.patch.object(cli, "_bind_current_controller_sessions"), \
                     mock.patch.object(cli, "_controller_step", side_effect=one_step):
                    first = _run_controller_json(cli.controller_resume, args)
                    self.assertEqual(first["error"], "ready task is invalid")
                    after_invalid = json.loads(controller.state_path.read_text())
                    self.assertEqual(after_invalid["epoch"], initial_epoch)
                    self.assertEqual(controller._load_checkpoint()["epoch"], initial_epoch)
                    self.assertEqual(controller._load_checkpoint()["pending_continuation_task"], "")

                    args.continue_task = "ready-good-task"
                    second = _run_controller_json(cli.controller_resume, args)
                    self.assertTrue(second["ok"], second)
                    self.assertEqual(verified, ["bad-ready-task", "ready-good-task"])
                    current_state = json.loads(controller.state_path.read_text())
                    self.assertEqual(current_state["epoch"], initial_epoch + 1)
                    self.assertEqual(controller._load_checkpoint()["pending_continuation_task"], "ready-good-task")

    def test_explicit_continuation_rejects_checkpoint_override_before_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, _lease = self._continuation_fixture(base)
                shadow_checkpoint = base / "shadow-checkpoint.json"
                args.checkpoint_path = str(shadow_checkpoint)
                state_before = controller.state_path.read_bytes()
                checkpoint_before = controller.checkpoint_path.read_bytes()

                result = self._run_continuation_once(args)

                self.assertFalse(result["ok"])
                self.assertIn("canonical namespaced controller checkpoint", result["error"])
                self.assertEqual(controller.state_path.read_bytes(), state_before)
                self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)
                self.assertFalse(shadow_checkpoint.exists())

    def test_explicit_continuation_retries_when_credential_write_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                initial_epoch = lease.epoch
                with mock.patch.object(cli, "_controller_credentials", side_effect=OSError("credential write failed")):
                    first = self._run_continuation_once(args)
                self.assertEqual(first["error"], "credential write failed")
                staged = json.loads(controller.state_path.read_text())
                self.assertEqual(staged["epoch"], initial_epoch)
                self.assertIn("continuation_reattach", staged)
                self.assertEqual(controller._load_checkpoint()["epoch"], initial_epoch)

                second = self._run_continuation_once(args)
                self.assertTrue(second["ok"], second)
                self._assert_continuation_committed(args, controller, "ready-task")

    def test_staged_continuation_rejects_changed_target_and_stale_proof_without_losing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                with mock.patch.object(cli, "_controller_credentials", side_effect=OSError("credential write failed")):
                    first = self._run_continuation_once(args)
                self.assertEqual(first["error"], "credential write failed")
                staged_before = controller.state_path.read_bytes()
                checkpoint_before = controller.checkpoint_path.read_bytes()

                args.continue_task = "different-ready-task"
                changed_target = self._run_continuation_once(args)
                self.assertFalse(changed_target["ok"])
                self.assertIn("exact target", changed_target["error"])
                self.assertEqual(controller.state_path.read_bytes(), staged_before)
                self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)

                args.continue_task = "ready-task"
                args.resume_token = "stale-proof"
                stale_proof = self._run_continuation_once(args)
                self.assertFalse(stale_proof["ok"])
                self.assertIn("canonical protected credential", stale_proof["error"])
                self.assertEqual(controller.state_path.read_bytes(), staged_before)
                self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)

                args.resume_token = ""
                retry = self._run_continuation_once(args)
                self.assertTrue(retry["ok"], retry)
                self._assert_continuation_committed(args, controller, "ready-task")

    def test_plain_resume_is_fenced_at_each_staged_continuation_crash_point(self) -> None:
        for phase in ("credential-before", "credential-after", "state-commit", "checkpoint-commit"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    args, controller, lease = self._continuation_fixture(base)
                    original_credentials = cli._controller_credentials
                    if phase == "credential-before":
                        with mock.patch.object(
                            cli, "_controller_credentials", side_effect=OSError("credential crash"),
                        ):
                            staged = self._run_continuation_once(args)
                    elif phase == "credential-after":
                        def write_then_fail(*call_args, **call_kwargs):
                            original_credentials(*call_args, **call_kwargs)
                            raise OSError("credential crash after write")

                        with mock.patch.object(cli, "_controller_credentials", side_effect=write_then_fail):
                            staged = self._run_continuation_once(args)
                    elif phase == "state-commit":
                        writer = cli.controller_backend.RootController._write_state
                        writes = 0

                        def write_state_then_fail(instance, state):
                            nonlocal writes
                            writes += 1
                            writer(instance, state)
                            if writes == 2:
                                raise OSError("state crash after active lease write")

                        with mock.patch.object(
                            cli.controller_backend.RootController, "_write_state",
                            new=write_state_then_fail,
                        ):
                            staged = self._run_continuation_once(args)
                    else:
                        writer = cli.checkpoint_backend.write_checkpoint

                        def write_checkpoint_then_fail(path, document):
                            saved = writer(path, document)
                            if Path(path) == controller.checkpoint_path and document.get("pending_continuation_task"):
                                raise OSError("checkpoint crash after target write")
                            return saved

                        with mock.patch.object(
                            cli.checkpoint_backend, "write_checkpoint",
                            side_effect=write_checkpoint_then_fail,
                        ):
                            staged = self._run_continuation_once(args)
                    self.assertFalse(staged["ok"], staged)
                    self.assertIn("continuation_reattach", json.loads(controller.state_path.read_text()))

                    state_before = controller.state_path.read_bytes()
                    checkpoint_before = controller.checkpoint_path.read_bytes()
                    credential_path = cli._resume_key_path(args)
                    credential_before = credential_path.read_bytes()
                    args.continue_after_cancelled_preidentity = ""
                    args.continue_task = ""
                    args._continuation_task = ""

                    plain = self._run_continuation_once(args)

                    self.assertFalse(plain["ok"], plain)
                    self.assertIn("continuation is pending", plain["error"])
                    self.assertEqual(controller.state_path.read_bytes(), state_before)
                    self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)
                    self.assertEqual(credential_path.read_bytes(), credential_before)

                    args.continue_after_cancelled_preidentity = "old-task"
                    args.continue_task = "ready-task"
                    retry = self._run_continuation_once(args)
                    self.assertTrue(retry["ok"], retry)
                    self._assert_continuation_committed(args, controller, "ready-task")

    def test_staged_continuation_blocks_direct_lease_and_checkpoint_mutators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                with mock.patch.object(cli, "_controller_credentials", side_effect=OSError("credential crash")):
                    staged = self._run_continuation_once(args)
                self.assertFalse(staged["ok"])
                state_before = controller.state_path.read_bytes()
                checkpoint_before = controller.checkpoint_path.read_bytes()
                credential_before = cli._resume_key_path(args).read_bytes()
                task = {"task": "unrelated-task", "root": str(controller.root)}
                mutations = (
                    ("acquire", lambda: controller.acquire(resume_proof=lease.resume_secret)),
                    ("authorize", lambda: controller.authorize(lease.resume_secret)),
                    ("heartbeat", lambda: controller.heartbeat(lease)),
                    ("release", lambda: controller.release(lease)),
                    ("reserve", lambda: controller.reserve_active_task(task, lease=lease)),
                    ("halt", lambda: controller.halt("blocked", "test", lease=lease)),
                    ("progress", lambda: controller.record_session_event(
                        event="phase_completed", task_class="coding", lease=lease,
                    )),
                    ("rotate", lambda: controller.rotate_session_budget(lease=lease)),
                    ("checkpoint", lambda: controller._save_checkpoint(controller._load_checkpoint())),
                    ("waiver", lambda: controller.approve_waiver(
                        workflow_root="wf", task="task", acceptance_id="row",
                        approval_ref="ref", approved_by="operator", approved_at="now",
                        authority_secret="test-authority", lease=lease,
                    )),
                )
                for name, mutate in mutations:
                    with self.subTest(mutation=name), self.assertRaisesRegex(
                        cli.controller_backend.LeaseConflict, "continuation is pending",
                    ):
                        mutate()
                    self.assertEqual(controller.state_path.read_bytes(), state_before)
                    self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)
                    self.assertEqual(cli._resume_key_path(args).read_bytes(), credential_before)

                stopped = _run_controller_json(cli.controller_stop, args)
                self.assertFalse(stopped["ok"], stopped)
                self.assertIn("continuation is pending", stopped["error"])
                self.assertEqual(controller.state_path.read_bytes(), state_before)
                self.assertEqual(controller.checkpoint_path.read_bytes(), checkpoint_before)
                self.assertEqual(cli._resume_key_path(args).read_bytes(), credential_before)

                retry = self._run_continuation_once(args)
                self.assertTrue(retry["ok"], retry)
                self._assert_continuation_committed(args, controller, "ready-task")


    def _args(self, workspace: Path, checkpoint_path: Path) -> argparse.Namespace:
        return argparse.Namespace(
            root=str(workspace), workflow_root="wf", controller="controller",
            state_path="", checkpoint_path=str(checkpoint_path), stale_after=300.0,
            takeover=False, resume_token="", resume_key_file="", once=True,
            poll_interval=0.01, deadline=0.0, identity_deadline=5.0,
            continue_after_cancelled_preidentity="", continue_task="",
            acknowledge_no_ready_halt=False, json=True, task="task", launch_id="launch",
            pane_id="pane", event="phase_completed", task_class="coding", phase="",
            approach="", evidence="", rotate_after_tasks=None, rotate_after_phases=None,
            same_approach_limit=None, acceptance_id="row", approval_ref="ref",
            approved_by="operator", approved_at="", reason="",
        )

    def test_every_mutating_controller_command_rejects_checkpoint_override_before_writes(self) -> None:
        handlers = (
            cli.controller_start, cli.controller_resume, cli.controller_supervise,
            cli.controller_recover_preidentity, cli.controller_retire_superseded_legacy_preidentity,
            cli.controller_progress, cli.controller_rotate, cli.controller_stop,
            cli.controller_approve_waiver,
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            checkpoint = base / "alternate-checkpoint.json"
            state_home = base / "state-home"
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}):
                for handler in handlers:
                    with self.subTest(handler=handler.__name__):
                        result = _run_controller_json(handler, self._args(workspace, checkpoint))
                        self.assertFalse(result["ok"], result)
                        self.assertIn("canonical namespaced controller checkpoint", result["error"])
                        self.assertFalse(checkpoint.exists())
                        self.assertFalse(state_home.exists())

    def test_status_may_inspect_an_alternate_checkpoint_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            checkpoint = base / "alternate-checkpoint.json"
            cli.checkpoint_backend.write_checkpoint(checkpoint, cli.checkpoint_backend.build_checkpoint({
                "task": str(workspace), "phase": "controller", "next_action": "inspect",
                "root": str(workspace), "controller": "controller",
                "state": "blocked", "status": "blocked", "terminal": True,
            }))
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", side_effect=cli.beads_backend.BeadsError("offline")):
                result = _run_controller_json(cli.controller_status, self._args(workspace, checkpoint))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["checkpoint"]["state"], "blocked")

    def test_explicit_continuation_retries_after_credential_write_then_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                original = cli._controller_credentials

                def write_then_crash(*writer_args, **writer_kwargs):
                    original(*writer_args, **writer_kwargs)
                    raise OSError("crash after credential write")

                with mock.patch.object(cli, "_controller_credentials", side_effect=write_then_crash):
                    first = self._run_continuation_once(args)
                self.assertEqual(first["error"], "crash after credential write")
                staged = json.loads(controller.state_path.read_text())
                self.assertEqual(staged["epoch"], lease.epoch)
                self.assertEqual(controller._load_checkpoint()["epoch"], lease.epoch)
                self.assertNotEqual(
                    cli._read_controller_credentials(cli._resume_key_path(args))["resume_secret"],
                    lease.resume_secret,
                )

                second = self._run_continuation_once(args)
                self.assertTrue(second["ok"], second)
                self._assert_continuation_committed(args, controller, "ready-task")

    def test_explicit_continuation_retries_after_lease_state_write_then_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                writer = cli.controller_backend.RootController._write_state
                writes = 0

                def write_state_then_crash(instance, state):
                    nonlocal writes
                    writes += 1
                    writer(instance, state)
                    if writes == 2:
                        raise OSError("crash after active lease write")

                with mock.patch.object(
                    cli.controller_backend.RootController, "_write_state",
                    new=write_state_then_crash,
                ):
                    first = self._run_continuation_once(args)
                self.assertEqual(first["error"], "crash after active lease write")
                staged = json.loads(controller.state_path.read_text())
                self.assertEqual(staged["epoch"], lease.epoch + 1)
                self.assertIn("continuation_reattach", staged)
                self.assertEqual(controller._load_checkpoint()["epoch"], lease.epoch)

                second = self._run_continuation_once(args)
                self.assertTrue(second["ok"], second)
                self._assert_continuation_committed(args, controller, "ready-task")

    def test_explicit_continuation_retries_after_target_checkpoint_write_then_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                args, controller, lease = self._continuation_fixture(base)
                writer = cli.checkpoint_backend.write_checkpoint

                def write_checkpoint_then_crash(path, document):
                    saved = writer(path, document)
                    if Path(path) == controller.checkpoint_path and document.get("pending_continuation_task"):
                        raise OSError("crash after target checkpoint write")
                    return saved

                with mock.patch.object(
                    cli.checkpoint_backend, "write_checkpoint",
                    side_effect=write_checkpoint_then_crash,
                ):
                    first = self._run_continuation_once(args)
                self.assertEqual(first["error"], "crash after target checkpoint write")
                staged = json.loads(controller.state_path.read_text())
                self.assertIn("continuation_reattach", staged)
                self.assertEqual(
                    controller._load_checkpoint()["pending_continuation_task"], "ready-task",
                )

                second = self._run_continuation_once(args)
                self.assertTrue(second["ok"], second)
                self._assert_continuation_committed(args, controller, "ready-task")

    def _exercise_retry(self, *, parallel: bool, claim_landed: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            controller = cli.controller_backend.RootController(
                str(workspace), "controller",
                state_path=base / "protected/controller.json",
                checkpoint_path=base / "protected/checkpoint.json",
            )
            lease = controller.acquire()
            controller.halt(
                "blocked",
                "USER_ACTION_REQUIRED: task old provider identity never resolved within 1800s",
                lease=lease,
            )
            halted = controller._load_checkpoint()
            halted["last_check"] = "cancelled expired preidentity launch old-launch for old"
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, halted)
            controller.acknowledge_cancelled_preidentity_halt(
                "wf", "old", "target", lease=lease,
            )
            issue = {
                "id": "target", "status": "open", "assignee": "",
                "metadata": {"agentflow": {}}, "labels": [],
            }
            claim = cli.beads_backend.ExactClaim(
                cli.beads_backend.ClaimIdentity("wf", "target", "controller", "claim-target"),
                issue,
            )
            args = argparse.Namespace(
                workflow_root="wf", _authority_secret="test-authority",
                _continuation_task="target",
            )
            policy = cli.execution_backend.ExecutionPolicy(max_parallel_workers=2)
            step = cli._controller_step_parallel if parallel else cli._controller_step_serial
            kwargs = {"operation": "resume"}
            if parallel:
                kwargs["policy"] = policy
            exact_calls: list[str] = []
            orphaned_rows: list[dict[str, Any]] = []

            def exact_claim(_cwd, *, task, **_kwargs):
                exact_calls.append(task)
                if len(exact_calls) == 1:
                    if claim_landed:
                        orphaned_rows.append({
                            **issue, "status": "in_progress", "assignee": "controller",
                        })
                    raise cli.beads_backend.BeadsError("injected exact claim failure")
                return claim

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    cli.beads_backend, "get_issue", return_value={"id": "wf", "status": "open"},
                ))
                stack.enter_context(mock.patch.object(
                    cli.beads_backend, "root_descendants", side_effect=lambda *_args: list(orphaned_rows),
                ))
                stack.enter_context(mock.patch.object(cli.beads_backend, "claim_issue_exact", side_effect=exact_claim))
                ready = stack.enter_context(mock.patch.object(cli.beads_backend, "claim_ready"))
                stack.enter_context(mock.patch.object(cli.beads_backend, "update_agentflow_metadata"))
                stack.enter_context(mock.patch.object(cli, "_persist_claim_identity"))
                stack.enter_context(mock.patch.object(cli, "_authenticated_cancelled_preidentity", return_value=False))
                stack.enter_context(mock.patch.object(cli, "_dispatch_via_herdr", return_value=lambda _selected: {
                    "state": "running", "session_id": "session-target",
                }))
                stack.enter_context(mock.patch.object(
                    cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None,
                ))
                with self.assertRaisesRegex(cli.beads_backend.BeadsError, "injected exact claim failure"):
                    step(args, controller, workspace, lease, **kwargs)
                self.assertEqual(controller._load_checkpoint()["pending_continuation_task"], "target")
                # A new CLI process has no in-memory target. The durable
                # checkpoint forces the same exact claim path on retry.
                restarted_args = argparse.Namespace(
                    workflow_root="wf", _authority_secret="test-authority", _continuation_task="",
                )
                result, _stop = step(restarted_args, controller, workspace, lease, **kwargs)
                if not parallel:
                    self.assertEqual(result["result"]["task"], "target")
                self.assertEqual(exact_calls, ["target"] if claim_landed else ["target", "target"])
                ready.assert_not_called()
                checkpoint = controller._load_checkpoint()
                self.assertEqual(checkpoint["pending_continuation_task"], "")
                if parallel:
                    self.assertEqual([row["task"] for row in checkpoint["active_tasks"]], ["target"])
                else:
                    self.assertEqual(checkpoint["task"], "target")

    def test_serial_resume_retries_only_durable_exact_continuation_after_claim_failure(self) -> None:
        self._exercise_retry(parallel=False)

    def test_parallel_resume_retries_only_durable_exact_continuation_after_claim_failure(self) -> None:
        self._exercise_retry(parallel=True)

    def test_serial_resume_adopts_only_durable_target_after_claim_committed_before_crash(self) -> None:
        self._exercise_retry(parallel=False, claim_landed=True)

    def test_parallel_resume_adopts_only_durable_target_after_claim_committed_before_crash(self) -> None:
        self._exercise_retry(parallel=True, claim_landed=True)


class ControllerResumeCredentialTests(unittest.TestCase):
    """AFREL-023: the resume secret gets a protected automatic handoff and
    never appears in status output; AFREL-028's rotation is exercised via
    the real CLI path instead of by reaching into controller internals."""

    def test_controller_state_path_override_is_rejected_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            custom = root / "custom-controller-state.json"
            args = _controller_args(root, state_path=str(custom))
            payload = _run_controller_json(cli.controller_start, args)
            self.assertFalse(payload["ok"])
            self.assertIn("controller --state-path is unsupported", payload["error"])
            self.assertFalse(custom.exists())

    def test_supervisor_requires_exact_workflow_root_before_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, workflow_root="")
            with mock.patch.object(cli.beads_backend, "get_issue") as get_issue:
                payload = _run_controller_json(cli.controller_supervise, args)
            self.assertFalse(payload["ok"])
            self.assertIn("--workflow-root is required", payload["error"])
            get_issue.assert_not_called()
            self.assertFalse(cli._controller_state_dir(root, "").exists())

    def test_supervise_is_registered_as_a_controller_command(self) -> None:
        args = cli.build_parser().parse_args([
            "controller", "supervise", "--root", "/tmp/workspace",
            "--workflow-root", "approved-root",
        ])
        self.assertIs(args.func, cli.controller_supervise)

    def test_resume_key_override_inside_worker_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(
                root, resume_key_file=str(root / "resume.key")
            )
            payload = _run_controller_json(cli.controller_start, args)
            self.assertFalse(payload["ok"])
            self.assertIn("outside the worker workspace", payload["error"])

    def test_documented_default_start_crash_resume_stop_flow_works_with_no_explicit_flags(self) -> None:
        """AFREL-034: the exact README-documented default sequence -- start,
        crash (a fresh process/Namespace), resume, then stop -- using only
        --root/--workflow-root/--controller and NO --resume-token or
        --resume-key-file at all. A safe-by-default protected key must
        make this work without the operator ever handling a credential."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root_issue = {"id": "wf-root", "status": "open", "metadata": {}}
            beads_patches = (
                mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue),
                mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]),
                mock.patch.object(cli.beads_backend, "claim_ready", return_value=None),
            )

            def bare_args(**overrides: object) -> argparse.Namespace:
                # No resume_token, no resume_key_file override -- exactly
                # the documented `agentflow controller <cmd> --root ...
                # --workflow-root ... --controller ...` invocation.
                defaults = dict(
                    root=str(root), controller="agentflow-controller", state_path="",
                    checkpoint_path="", stale_after=300.0, takeover=False,
                    workflow_root="wf-root", resume_token="", resume_key_file="",
                    json=True, once=True, poll_interval=0.01, deadline=5.0,
                    _supervisor_lock_path=str(
                        root / ".agentflow/test-controller-locks"
                        / f"{cli._controller_namespace('wf-root')}.lock"
                    ),
                )
                defaults.update(overrides)
                return argparse.Namespace(**defaults)

            with beads_patches[0], beads_patches[1], beads_patches[2]:
                start_payload = _run_controller_json(cli.controller_start, bare_args())
            self.assertTrue(start_payload["ok"], start_payload)

            # "Crash": a brand-new Namespace, as a fresh process would have.
            with beads_patches[0], beads_patches[1], beads_patches[2]:
                resume_payload = _run_controller_json(cli.controller_resume, bare_args())
            self.assertTrue(resume_payload["ok"], resume_payload)
            self.assertNotEqual(resume_payload["lease"]["token"], start_payload["lease"]["token"])

            stop_payload = _run_controller_json(cli.controller_stop, bare_args())
            self.assertTrue(stop_payload["ok"], stop_payload)
            self.assertTrue(stop_payload["released"])

    def test_controller_status_never_leaks_resume_secret_or_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                _run_controller_json(cli.controller_start, args)
            status_payload = _run_controller_json(cli.controller_status, args)
            self.assertTrue(status_payload["ok"], status_payload)
            lease = status_payload["lease"]
            self.assertIsNotNone(lease)
            self.assertNotIn("resume_secret", lease)
            self.assertNotIn("resume_secret_hash", lease)
            self.assertEqual(set(lease), {"root", "controller", "epoch", "token", "acquired_at", "heartbeat_at", "owner_id", "continuity_id"})
            # The incarnation continuity id is a non-secret identity (like
            # epoch/owner_id); it is exposed for status but is never a secret.
            self.assertTrue(lease["continuity_id"])
            # The raw file also never carries the plaintext, only the hash.
            state_path = cli._controller_state_dir(root, args.workflow_root) / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("resume_secret", raw["lease"])
            self.assertIn("resume_secret_hash", raw["lease"])

    def test_resume_key_file_enables_automatic_reattach_across_invocations(self) -> None:
        """AFREL-023: --resume-key-file removes the need to scrape
        state.json (which no longer even carries the plaintext) or to pass
        --resume-token by hand between repeated invocations."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "workspace"
            root.mkdir()
            key_file = base / "resume.key"
            args = _controller_args(root, resume_key_file=str(key_file))
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                first = _run_controller_json(cli.controller_start, args)
            self.assertTrue(first["ok"], first)
            self.assertTrue(key_file.is_file())
            self.assertEqual(oct(key_file.stat().st_mode & 0o777), "0o600")
            first_secret = json.loads(key_file.read_text())["resume_secret"]
            self.assertTrue(first_secret)

            # A second invocation with no --resume-token but the SAME
            # --resume-key-file must reattach, not collide.
            second_args = _controller_args(root, resume_key_file=str(key_file))
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                second = _run_controller_json(cli.controller_resume, second_args)
            self.assertTrue(second["ok"], second)
            # AFREL-032: reattach rotates the owner-bound fencing identity
            # too (epoch bumps, token changes) -- not just the resume
            # secret -- so a stale pre-reattach token stops authorizing.
            self.assertEqual(second["lease"]["epoch"], first["lease"]["epoch"] + 1)
            self.assertNotEqual(second["lease"]["token"], first["lease"]["token"])

            # AFREL-028: reattach rotates the credential, so the key file
            # now holds a different secret than the first invocation wrote.
            second_secret = json.loads(key_file.read_text())["resume_secret"]
            self.assertNotEqual(second_secret, first_secret)

    def test_stale_pre_reattach_token_cannot_fence_heartbeat_or_release_after_reattach(self) -> None:
        """AFREL-032/AFREL-037: 'Owner B reattaches; owner A string token
        remains authorized' is the exact reproduction. The public token is
        the ONLY form that crosses a process boundary (herdr_launch
        --lease, _controller_fence, controller_stop --resume-token is a
        different value but heartbeat/release/fence all accept a bare
        token string) -- it must stop authorizing anything the instant a
        legitimate reattach has happened."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "workspace"
            root.mkdir()
            key_file = base / "resume.key"
            args = _controller_args(root, resume_key_file=str(key_file))
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                first = _run_controller_json(cli.controller_start, args)
            stale_token = first["lease"]["token"]

            # A real reattach (owner B) through the same protected key file.
            second_args = _controller_args(root, resume_key_file=str(key_file))
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                second = _run_controller_json(cli.controller_resume, second_args)
            self.assertNotEqual(second["lease"]["token"], stale_token)

            # Owner A's now-stale public token must be rejected by the same
            # controller-lock fence herdr_launch uses.
            with self.assertRaises(cli.controller_backend.FencedLease):
                with cli._controller_fence(root, "wf-root", stale_token):
                    pass
            # And the CURRENT token still works.
            with cli._controller_fence(root, "wf-root", second["lease"]["token"]):
                pass

    def test_resume_key_file_authorizes_controller_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "workspace"
            root.mkdir()
            key_file = base / "resume.key"
            args = _controller_args(root, resume_key_file=str(key_file))
            with mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "wf-root", "status": "open", "metadata": {}}), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                _run_controller_json(cli.controller_start, args)
            # A fresh Namespace (a different owner_id) needs the key file to
            # stop a lease it does not itself hold in memory.
            stop_args = _controller_args(root, resume_key_file=str(key_file))
            stop_payload = _run_controller_json(cli.controller_stop, stop_args)
            self.assertTrue(stop_payload["ok"], stop_payload)
            self.assertTrue(stop_payload["released"])

    def test_approve_waiver_persists_rotated_key_and_remains_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            root_issue = {"id": "wf-root", "status": "open", "metadata": {}}
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                started = _run_controller_json(cli.controller_start, args)
            self.assertTrue(started["ok"], started)
            key_path = cli._resume_key_path(args)
            first_secret = cli._read_resume_key(key_path)

            waiver_args = _controller_args(
                root,
                task="task-1",
                acceptance_id="R1",
                approval_ref="approval-1",
                approved_by="release-manager",
                approved_at="2026-07-27T00:00:00Z",
                reason="approved exception",
            )
            approved = _run_controller_json(
                cli.controller_approve_waiver, waiver_args
            )
            self.assertTrue(approved["ok"], approved)
            second_secret = cli._read_resume_key(key_path)
            self.assertTrue(second_secret)
            self.assertNotEqual(second_secret, first_secret)
            self.assertRegex(
                approved["approval"]["authority_hmac"], r"^[0-9a-f]{64}$"
            )

            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                resumed = _run_controller_json(
                    cli.controller_resume, _controller_args(root)
                )
            self.assertTrue(resumed["ok"], resumed)

    def test_workspace_local_v1_resume_key_migrates_to_external_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            controller, _ = cli._controller_instance(args)
            lease = controller.acquire()
            legacy = cli._legacy_resume_key_path(args)
            self.assertIsNotNone(legacy)
            cli._private_atomic_json(legacy, {
                "schema": "agentflow.controller_resume_key",
                "version": 1,
                "resume_secret": lease.resume_secret,
            })
            canonical = cli._resume_key_path(args)
            self.assertNotEqual(canonical, legacy)
            root_issue = {"id": "wf-root", "status": "open", "metadata": {}}
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                resumed = _run_controller_json(
                    cli.controller_resume, _controller_args(root)
                )
            self.assertTrue(resumed["ok"], resumed)
            self.assertFalse(legacy.exists())
            credentials = cli._read_controller_credentials(canonical)
            self.assertTrue(credentials["resume_secret"])
            self.assertTrue(credentials["authority_secret"])


def _spawn_provider_report(fixture, argv: list, *, report_delay: float,
                           report_done: threading.Event, threads: list,
                           report_gate=None) -> None:
    """Simulate the provider a real launch spawned: after launch, write the
    result and finalize the untrusted ``herdr submit`` inbox.

    Run in-process (a thread), not as a detached OS subprocess, precisely
    because the authenticated result path re-verifies the claim against Beads
    -- which is patched in-process and a real subprocess could not see. The
    return channel, capability, continuity fence, acceptance validation, and
    result reader are all exercised for real; nothing about the authentication
    is mocked away.
    """
    env: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token == "--env":
            key, _, value = argv[index + 1].partition("=")
            env[key] = value
    assert "AGENTFLOW_RETURN_CAPABILITY_FILE" not in env
    assert "AGENTFLOW_HERDR_STATE_PATH" not in env
    assert "AGENTFLOW_CONTROLLER_AUTHORITY" not in env

    def report() -> None:
        session_file = fixture.root / ".agentflow/herdr/sessions.json"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                record = json.loads(session_file.read_text(encoding="utf-8"))["sessions"].get("task-1", {})
                channel = record.get("return_channel") or {}
                if record.get("status") == "launched" and channel.get("state") == "issued":
                    break
            except (OSError, json.JSONDecodeError, KeyError):
                pass
            time.sleep(0.01)
        # Stay "running" until the test has observed its required controller
        # state, rather than relying on runner scheduling to fit multiple polls
        # into an arbitrary wall-clock delay.
        if report_gate is None:
            time.sleep(report_delay)
        else:
            gate_deadline = time.monotonic() + 5.0
            while time.monotonic() < gate_deadline and not report_gate():
                time.sleep(0.005)
            assert report_gate(), "controller did not reach the provider report gate"
        contract = json.loads(Path(env["AGENTFLOW_RESULT_CONTRACT"]).read_text(encoding="utf-8"))
        result = {"outcome": "completed", "acceptance_results": [
            {"acceptance_id": aid, "status": "passed",
             "evidence": "the smoke test passed", "source": "provider"}
            for aid in contract["acceptance_ids"]]}
        Path(env["AGENTFLOW_RESULT_FILE"]).write_text(json.dumps(result), encoding="utf-8")
        report_args = argparse.Namespace(
            root=str(fixture.root), contract=env["AGENTFLOW_RESULT_CONTRACT"],
            file=env["AGENTFLOW_RESULT_FILE"], json=True)
        cli.herdr_submit(report_args)
        report_done.set()

    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    threads.append(thread)


class ControllerRunTests(unittest.TestCase):
    """AFREL-010: the controller must traverse ready descendants end to end
    -- claim -> preflight(route) -> real Herdr launch -> matching result ->
    Beads transition -> next node -- instead of stalling forever after one
    claim."""

    def _issue(self, issue_id: str, **overrides: object) -> dict:
        issue = {"id": issue_id, "status": "open", "assignee": "", "labels": [], "metadata": {}}
        issue.update(overrides)
        return issue

    def test_acknowledge_no_ready_halt_is_same_root_authenticated_and_shared_by_schedulers(self) -> None:
        """The explicit acknowledgement reopens only a proven safe no-ready boundary."""
        for workers in (1, 2):
            with self.subTest(max_parallel_workers=workers), tempfile.TemporaryDirectory() as temp:
                base = Path(temp).resolve()
                fixture = ValidLaunch(base / "workspace", seed_lease=False)
                key_file = base / "resume.key"
                ready = dict(fixture.task_issue, id="task-10", title="Newly ready child", status="open")
                ready["metadata"] = {"agentflow": {}}
                prior = self._issue("task-9", status="closed", parent=fixture.workflow_root)
                if workers > 1:
                    fixture.root_issue["metadata"]["agentflow"]["execution"] = {
                        "schema": "agentflow.execution-policy@1", "controller_only": True,
                        "max_parallel_workers": 2, "max_delegation_depth": 1,
                        "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                        "max_expensive_execution_children": 0,
                    }
                args = _controller_args(
                    fixture.root, workflow_root=fixture.workflow_root,
                    resume_key_file=str(key_file),
                )
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    controller, _ = cli._controller_instance(args)
                    lease = controller.acquire()
                    cli._controller_credentials(args, lease)
                    checkpoint = controller._load_checkpoint()
                    checkpoint["completed_evidence"] = "prior authenticated result preserved"
                    checkpoint["changed_files"] = ["src/previous.py"]
                    controller._save_checkpoint(checkpoint, lease=lease)
                    controller.halt(
                        "blocked",
                        "USER_ACTION_REQUIRED: nonterminal descendant(s) remain with no ready work: old (.12)",
                        lease=lease,
                    )
                    cli._private_atomic_json(
                        fixture.root / ".agentflow/herdr/sessions.json",
                        {"schema": "agentflow.herdr", "version": 1, "sessions": {
                            "task-9": {
                                "status": "completed",
                                "binding": {
                                    "root": str(fixture.root), "task_id": "task-9",
                                    "provider": "claude", "session_id": "session-old",
                                    "launch_id": "launch-old", "claim_id": "claim-old", "lease_id": "lease-old",
                                },
                                "return_channel": {
                                    "state": "consumed", "consumed_at": "then",
                                    "result_sha256": "d" * 64,
                                },
                                "result": {
                                    "acceptance_results": [], "actor": fixture.controller,
                                    "claim_token_sha256": "e" * 64, "evidence": "done",
                                    "launch_id": "launch-old", "lease_id": "lease-old",
                                    "outcome": "completed", "provider": "claude",
                                    "session_id": "session-old", "task_id": "task-9",
                                    "workflow_root": fixture.workflow_root,
                                    "workspace_root": str(fixture.root),
                                },
                            },
                        }},
                    )

                    args.acknowledge_no_ready_halt = True
                    args.workflow_root = "wf-other"
                    with mock.patch.object(
                        cli.beads_backend, "get_issue", return_value=fixture.root_issue,
                    ):
                        wrong_root = _run_controller_json(cli.controller_resume, args)
                    self.assertFalse(wrong_root["ok"])
                    self.assertEqual(controller._load_checkpoint()["state"], "blocked")
                    args.workflow_root = fixture.workflow_root
                    args.resume_token = "not-the-protected-reattach-proof"
                    with mock.patch.object(
                        cli.beads_backend, "get_issue",
                        side_effect=lambda _cwd, _id: fixture.root_issue,
                    ):
                        unauthorized = _run_controller_json(cli.controller_resume, args)
                    self.assertFalse(unauthorized["ok"])
                    self.assertEqual(controller._load_checkpoint()["state"], "blocked")
                    args.resume_token = ""
                    args.acknowledge_no_ready_halt = False

                    get_issue = lambda _cwd, issue_id: (
                        fixture.root_issue if issue_id == fixture.workflow_root else ready
                    )
                    with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                         mock.patch.object(cli, "_bind_current_controller_sessions"), \
                         mock.patch.object(cli.beads_backend, "claim_ready") as claim:
                        stale = _run_controller_json(cli.controller_resume, args)
                    self.assertEqual(stale["result"]["state"], "blocked")
                    self.assertTrue(stale["result"]["terminal"])
                    claim.assert_not_called()

                    args.acknowledge_no_ready_halt = True
                    def read_ready(_cwd, *argv):
                        rows = [] if "--assignee" in argv else [{"id": "task-10"}]
                        return subprocess.CompletedProcess(["bd"], 0, json.dumps(rows), "")

                    ready["status"] = "open"
                    with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                         mock.patch.object(cli.beads_backend, "root_descendants", return_value=[prior, ready]), \
                         mock.patch.object(cli.beads_backend, "run", side_effect=read_ready), \
                         mock.patch.object(cli.beads_backend, "claim_ready", side_effect=[ready, None]) as claim, \
                         mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                         mock.patch.object(cli, "_persist_claim_identity"), \
                         mock.patch.object(cli, "_bind_current_controller_sessions"), \
                         mock.patch.object(cli, "_dispatch_via_herdr", return_value=lambda _task: {
                             "state": "running", "session_id": "session-new",
                         }):
                        resumed = _run_controller_json(cli.controller_resume, args)
                    self.assertTrue(resumed["ok"], resumed)
                    self.assertFalse(resumed["result"]["terminal"])
                    self.assertEqual(resumed["result"]["state"], "running")
                    self.assertIn("task-10", resumed["result"]["checkpoint"]["last_check"])
                    self.assertIn(
                        "nonterminal descendant(s) remain",
                        resumed["result"]["checkpoint"]["terminal_reason"],
                    )
                    self.assertEqual(
                        resumed["result"]["checkpoint"]["completed_evidence"],
                        "prior authenticated result preserved",
                    )
                    self.assertEqual(resumed["result"]["checkpoint"]["changed_files"], ["src/previous.py"])
                    self.assertEqual(claim.call_count, workers)

    def test_acknowledgement_rejects_unknown_or_completed_terminal_reason(self) -> None:
        for state in ("blocked", "completed", "failed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller,
                )
                lease = controller.acquire()
                controller.halt(state, "USER_ACTION_REQUIRED: a worker may still be live", lease=lease)
                with self.assertRaises(cli.controller_backend.ControllerError):
                    controller.acknowledge_no_ready_halt(fixture.workflow_root, "task-1", lease=lease)

    def test_acknowledgement_fails_closed_on_unresolved_or_unknown_herdr_lifecycle(self) -> None:
        for status in ("launching", "identity_pending", "failed", "unknown", "completed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller,
                )
                lease = controller.acquire()
                controller.halt(
                    "blocked",
                    "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: task-1",
                    lease=lease,
                )
                cli._private_atomic_json(
                    fixture.root / ".agentflow/herdr/sessions.json",
                    {"schema": "agentflow.herdr", "version": 1,
                     "sessions": {"task-1": {"status": status}}},
                )
                with mock.patch.object(cli.beads_backend, "get_issue", return_value=fixture.root_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]):
                    with self.assertRaises(cli.controller_backend.ControllerError):
                        cli._verify_no_ready_ack_candidate(
                            fixture.root, fixture.workflow_root, controller, lease,
                        )

    def test_acknowledgement_requires_unblocked_ledger_and_a_current_ready_descendant(self) -> None:
        for mode in ("blocked-ledger", "no-ready", "foreign-ready"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller,
                )
                lease = controller.acquire()
                controller.halt(
                    "blocked",
                    "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: task-1",
                    lease=lease,
                )
                if mode == "no-ready":
                    ready_rows = []
                elif mode == "foreign-ready":
                    ready_rows = [{"id": "outside-root"}]
                else:
                    ready_rows = []

                def run_ready(_cwd, *argv):
                    rows = [] if "--assignee" in argv else ready_rows
                    return subprocess.CompletedProcess(["bd"], 0, json.dumps(rows), "")

                with mock.patch.object(cli.beads_backend, "get_issue", return_value=fixture.root_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                     mock.patch.object(cli.beads_backend, "run", side_effect=run_ready), \
                     mock.patch.object(
                         controller, "session_ledger",
                         return_value={"blocked": mode == "blocked-ledger"},
                     ):
                    with self.assertRaises(cli.controller_backend.ControllerError):
                        cli._verify_no_ready_ack_candidate(
                            fixture.root, fixture.workflow_root, controller, lease,
                        )

    def _consumed_session_record(self, root: Path, task_id: str, *, session_id: str = "sess-1",
                                 launch_id: str = "launch-1", acceptance_id: str = "R1",
                                 acceptance_ids: object = None, acceptance_results: object = None,
                                 outcome: str = "completed") -> dict:
        """A durable Herdr session record whose authenticated return channel has
        already been consumed (state == "consumed") -- i.e. the provider has
        submitted through the Herdr return contract. Used to drive the controller's
        traversal (disposition/advance/claim-next) logic; the authentication of
        the channel itself is proven end to end by GenuineLifecycleTests.

        ``acceptance_ids``/``acceptance_results`` may be overridden to drive the
        controller's own acceptance-disposition validation (e.g. empty, secret,
        or non-passing evidence) rather than the default passing row."""
        if acceptance_ids is None:
            acceptance_ids = [acceptance_id]
        if acceptance_results is None:
            acceptance_results = [{
                "acceptance_id": acceptance_id, "status": "passed",
                "evidence": "the smoke test passed", "source": "provider",
            }]
        return {
            "schema": "agentflow.herdr", "version": 1,
            "sessions": {task_id: {
                "status": "completed",
                "binding": {
                    "root": str(root), "task_id": task_id, "claim_id": "claim-1",
                    "lease_id": "lease-1", "launch_id": launch_id, "pane_id": "pane-1",
                    "provider": "claude", "session_id": session_id,
                    "created_at": "now", "launched_at": "now",
                },
                "return_channel": {
                    "state": "consumed", "acceptance_ids": list(acceptance_ids), "approved_waivers": [],
                },
                "result": {
                    "task_id": task_id, "launch_id": launch_id, "provider": "claude",
                    "session_id": session_id, "outcome": outcome,
                    "acceptance_results": acceptance_results,
                },
            }},
        }

    def test_real_fake_provider_self_reports_across_a_crash_resume(self) -> None:
        """AFREL-031: a provider the launch spawned self-reports its structured
        completion through the provider-only `herdr submit` inbox. A first
        controller invocation dispatches; the provider submits without
        mutating Herdr authority; a second invocation (simulating a
        crash/restart) ingests, disposes, and reaches GOAL_COMPLETE."""
        with tempfile.TemporaryDirectory() as temp:
            base_dir = Path(temp).resolve()
            fixture = ValidLaunch(base_dir / "workspace", seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["acceptance"] = {
                "version": 1, "task_id": "wf-root",
                "rows": [{"id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                          "planned_evidence": "e", "status": "passed", "actual_evidence": "ev"}],
            }
            key_file = base_dir / "resume.key"
            base = dict(workflow_root=fixture.workflow_root, resume_key_file=str(key_file))

            threads: list = []
            report_done = threading.Event()
            first_state_observed = threading.Event()

            def on_spawn(argv):
                _spawn_provider_report(fixture, argv, report_delay=0.0,
                                       report_done=report_done, threads=threads,
                                       report_gate=first_state_observed.is_set)

            claim_calls = {"n": 0}

            def fake_claim_ready(cwd, *, parent, labels, actor):
                claim_calls["n"] += 1
                return fixture.task_issue if claim_calls["n"] == 1 else None

            def fake_root_descendants(cwd, root_id):
                return [fixture.task_issue]

            def fake_close_issue(cwd, task_id, reason):
                fixture.task_issue["status"] = "closed"

            common = dict(
                get_issue=mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue),
                descendants=mock.patch.object(cli.beads_backend, "root_descendants", side_effect=fake_root_descendants),
                ancestry=mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None),
                update=mock.patch.object(cli.beads_backend, "update_agentflow_metadata"),
                provider=mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command),
            )

            # Invocation 1 (once): claim + dispatch the real launch. The
            # provider finalizes its untrusted inbox afterward.
            # Capture payloads via _json_or_status: the concurrent report
            # thread emits its own payload, so a shared stdout buffer would
            # interleave two JSON objects.
            payloads1: list = []
            args1 = _controller_args(fixture.root, once=True, poll_interval=0.01, deadline=5.0, **base)
            with common["get_issue"], common["descendants"], common["ancestry"], common["update"], common["provider"], \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(on_spawn=on_spawn)), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda pl, **k: payloads1.append(pl)):
                self.assertEqual(cli.controller_resume(args1), 0)
                first = [pl for pl in payloads1 if pl.get("operation") == "resume"][-1]
                try:
                    self.assertEqual(first["result"]["state"], "running")
                finally:
                    # Keep the provider from submitting until invocation 1's
                    # returned state has been checked; then wait for its real
                    # inbox submission before simulating the crash/restart.
                    first_state_observed.set()
                    for thread in threads:
                        thread.join(timeout=5)
            self.assertTrue(report_done.is_set(), "provider never finalized its result inbox")
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["return_channel"]["state"], "issued")
            self.assertTrue(Path(record["return_channel"]["submission_file"]).is_file())

            # Invocation 2 (crash/restart): the real, unmocked result reader
            # detects the consumed result, closes the bead, and completes.
            args2 = _controller_args(fixture.root, once=False, poll_interval=0.01, deadline=5.0, **base)
            with common["get_issue"], common["descendants"], common["ancestry"], common["update"],                  mock.patch.object(cli.beads_backend, "claim_ready", return_value=None),                  mock.patch.object(cli.beads_backend, "close_issue", side_effect=fake_close_issue) as close_issue:
                second = _run_controller_json(cli.controller_resume, args2)
            close_issue.assert_called_once()
            self.assertEqual(close_issue.call_args.args[1], "task-1")
            self.assertEqual(second["stop_reason"], "GOAL_COMPLETE")
            self.assertTrue(second["result"]["terminal"])

    def test_two_sequential_workflow_roots_in_same_repo_both_complete(self) -> None:
        """AFREL-036: a completed root A's terminal checkpoint must never
        block dispatch/completion for an unrelated root B run later from
        the exact same repository (--root)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            matrix = {
                "version": 1, "task_id": "root",
                "rows": [{
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                }],
            }

            def run_one_root_to_completion(workflow_root: str) -> dict:
                args = _controller_args(root, workflow_root=workflow_root)
                bound_matrix = dict(matrix)
                bound_matrix["task_id"] = workflow_root
                issue = self._issue(
                    workflow_root,
                    metadata={"agentflow": {"acceptance": bound_matrix}},
                )
                with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                     mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                    return _run_controller_json(cli.controller_resume, args)

            first = run_one_root_to_completion("root-A")
            self.assertEqual(first["stop_reason"], "GOAL_COMPLETE")
            self.assertEqual(first["result"]["state"], "completed")

            # Root B, same --root (same repository/workspace), started
            # fresh afterward -- must not inherit root A's terminal state.
            second = run_one_root_to_completion("root-B")
            self.assertEqual(second["stop_reason"], "GOAL_COMPLETE")
            self.assertEqual(second["result"]["state"], "completed")

            # And their state files are genuinely distinct, not shared.
            path_a = cli._controller_state_dir(root, "root-A") / "state.json"
            path_b = cli._controller_state_dir(root, "root-B") / "state.json"
            self.assertNotEqual(path_a, path_b)
            self.assertTrue(path_a.is_file())
            self.assertTrue(path_b.is_file())

    def test_controller_loop_runs_to_goal_complete_without_manual_resume_or_result_calls(self) -> None:
        """AFREL-020: one call into the autonomous loop -- not a human or a
        separate result-consumer plus repeated `resume` invocations -- carries
        claim -> real launch -> poll a live session -> the provider's
        authenticated result -> disposition -> GOAL_COMPLETE end to end. The
        result reader and return channel are real; only the Beads boundary and
        the Herdr binary are stood in for."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["acceptance"] = {
                "version": 1, "task_id": "wf-root",
                "rows": [{"id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                          "planned_evidence": "e", "status": "passed", "actual_evidence": "ev"}],
            }
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root,
                                    once=False, poll_interval=0.01, deadline=8.0)
            threads: list = []
            report_done = threading.Event()

            real_task_result = cli._herdr_task_result
            polls = {"n": 0}

            def spy_task_result(root, task_id):
                polls["n"] += 1
                return real_task_result(root, task_id)

            def on_spawn(argv):
                _spawn_provider_report(fixture, argv, report_delay=0.05,
                                       report_done=report_done, threads=threads,
                                       report_gate=lambda: polls["n"] >= 2)

            claim_calls = {"n": 0}

            def fake_claim_ready(cwd, *, parent, labels, actor):
                claim_calls["n"] += 1
                return fixture.task_issue if claim_calls["n"] == 1 else None

            def fake_root_descendants(cwd, root_id):
                return [fixture.task_issue]

            def fake_close_issue(cwd, task_id, reason):
                fixture.task_issue["status"] = "closed"

            # Capture payloads via _json_or_status (not stdout parsing): the
            # provider-report thread also emits a result payload, so a shared
            # stdout buffer would interleave two JSON objects.
            payloads: list = []
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", side_effect=fake_root_descendants), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=fake_close_issue) as close_issue, \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli, "_herdr_task_result", side_effect=spy_task_result), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(on_spawn=on_spawn)), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda pl, **k: payloads.append(pl)):
                self.assertEqual(cli.controller_resume(args), 0)
                for thread in threads:
                    thread.join(timeout=5)

            resume_payloads = [pl for pl in payloads if pl.get("operation") == "resume"]
            self.assertEqual(len(resume_payloads), 1, resume_payloads)
            payload = resume_payloads[-1]
            self.assertTrue(report_done.is_set(), "provider never self-reported")
            self.assertEqual(payload["stop_reason"], "GOAL_COMPLETE")
            self.assertTrue(payload["result"]["terminal"])
            self.assertEqual(payload["result"]["state"], "completed")
            close_issue.assert_called_once()
            self.assertEqual(close_issue.call_args.args[1], "task-1")
            # Proof the loop polled the live session more than once before the
            # result landed -- not a single blind read.
            self.assertGreaterEqual(polls["n"], 2)

    def test_controller_loop_once_flag_takes_exactly_one_step(self) -> None:
        """--once must expose a single deterministic transition even when
        the underlying condition would otherwise keep the loop going."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, once=True, poll_interval=0.001, deadline=5.0)
            root_issue = self._issue("wf-root")
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready:
                payload = _run_controller_json(cli.controller_resume, args)
            claim_ready.assert_called_once()
            self.assertFalse(payload["result"]["terminal"])

    def test_deadline_zero_is_not_replaced_with_the_one_hour_default(self) -> None:
        class FakeClock:
            def __call__(self) -> float:
                return 100.0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, once=False, deadline=0.0, _monotonic=FakeClock())
            payloads: list[dict] = []
            issue = self._issue("wf-root")
            step_payload = {"operation": "resume", "ok": True, "result": {"state": "advancing"}}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                 mock.patch.object(cli, "_controller_step", return_value=(step_payload, False)), \
                 mock.patch.object(cli.time, "sleep", side_effect=AssertionError("zero deadline was ignored")), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                exit_code = cli.controller_resume(args)

            self.assertEqual(exit_code, 3)
            self.assertEqual(payloads[-1]["status"], "INCOMPLETE")
            self.assertEqual(payloads[-1]["result"]["state"], "incomplete")

    def test_supervisor_deadline_zero_is_durable_incomplete_with_nonzero_exit(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.value = 41.0

            def __call__(self) -> float:
                return self.value

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            clock = FakeClock()
            args = _controller_args(
                root, once=False, deadline=0.0, _monotonic=clock,
                _sleep=lambda _seconds: self.fail("zero deadline must not sleep"),
            )
            payloads: list[dict] = []
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                 mock.patch.object(cli, "_controller_step") as step, \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                exit_code = cli.controller_supervise(args)

            self.assertEqual(exit_code, 3)
            step.assert_not_called()
            self.assertEqual(payloads[-1]["status"], "INCOMPLETE")
            self.assertEqual(payloads[-1]["stop_reason"], "DEADLINE_EXCEEDED")
            self.assertEqual(payloads[-1]["result"]["state"], "incomplete")
            checkpoint_path = cli._controller_state_dir(root, args.workflow_root) / "checkpoint.json"
            checkpoint = cli.checkpoint_backend.load_checkpoint(checkpoint_path)
            self.assertEqual(checkpoint["status"], "incomplete")
            self.assertEqual(checkpoint["terminal_reason"], "DEADLINE_EXCEEDED")

    def test_supervisor_zero_deadline_preserves_terminal_checkpoints(self) -> None:
        for state in ("completed", "blocked"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                base = Path(temp).resolve()
                root = base / "workspace"
                root.mkdir()
                args = _controller_args(root, once=False, deadline=0.0)
                payloads: list[dict] = []
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                    controller, _ = cli._controller_instance(args)
                    lease = controller.acquire()
                    cli._controller_credentials(args, lease)
                    original = controller.halt(state, "original terminal evidence", lease=lease)

                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                     mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                     mock.patch.object(cli, "_controller_step") as step, \
                     mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                    exit_code = cli.controller_supervise(args)

                self.assertEqual(exit_code, 0)
                step.assert_not_called()
                self.assertTrue(payloads[-1]["ok"])
                self.assertTrue(payloads[-1]["result"]["terminal"])
                self.assertEqual(payloads[-1]["result"]["state"], state)
                self.assertEqual(payloads[-1]["result"]["checkpoint"], original.checkpoint)
                final_checkpoint = cli.checkpoint_backend.load_checkpoint(controller.checkpoint_path)
                self.assertEqual(final_checkpoint["status"], state)
                self.assertTrue(final_checkpoint["terminal"])

    def test_duplicate_supervisor_refuses_before_lease_reattach_or_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, once=False, deadline=10.0)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state-home")}):
                controller, _ = cli._controller_instance(args)
                original_lease = controller.acquire()
                cli._controller_credentials(args, original_lease)
            payloads: list[dict] = []
            with controller.supervisor_lock(), \
                 mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                 mock.patch.object(cli, "_controller_step") as step, \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                exit_code = cli.controller_supervise(args)
                resume_exit_code = cli.controller_resume(args)

            self.assertEqual(exit_code, 2)
            self.assertEqual(resume_exit_code, 2)
            self.assertIn("supervisor", payloads[-1]["error"])
            step.assert_not_called()
            self.assertEqual(controller._current_lease().token, original_lease.token)

    def test_supervisor_lock_is_independent_of_state_home_and_resume_key_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "workspace"
            root.mkdir()
            shared_key = base / "resume.key"
            args_a = _controller_args(root, resume_key_file=str(shared_key))
            args_b = _controller_args(root, resume_key_file=str(shared_key))
            args_b._supervisor_lock_path = args_a._supervisor_lock_path

            with mock.patch.dict(os.environ, {
                "AGENTFLOW_STATE_HOME": str(base / "state-a"),
                "HOME": str(base / "home-a"),
            }):
                production_path_a = cli._controller_supervisor_lock_path(root, "wf-root")
                controller_a, _ = cli._controller_instance(args_a)
                lease = controller_a.acquire()
                cli._controller_credentials(args_a, lease)
            with mock.patch.dict(os.environ, {
                "AGENTFLOW_STATE_HOME": str(base / "state-b"),
                "HOME": str(base / "home-b"),
            }):
                production_path_b = cli._controller_supervisor_lock_path(root, "wf-root")
                controller_b, _ = cli._controller_instance(args_b)

            self.assertEqual(production_path_a, production_path_b)
            self.assertEqual(controller_a.supervisor_lock_path, controller_b.supervisor_lock_path)
            payloads: list[dict] = []
            with controller_a.supervisor_lock(), \
                 mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-b")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                 mock.patch.object(cli, "_controller_step") as step, \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                self.assertEqual(cli.controller_resume(args_b), 2)

            self.assertIn("supervisor", payloads[-1]["error"])
            self.assertEqual(controller_a._current_lease().token, lease.token)
            step.assert_not_called()

    def test_supervisor_fails_closed_for_missing_wrong_or_spent_credential(self) -> None:
        for failure_mode in ("missing", "wrong", "spent"):
            with self.subTest(failure_mode=failure_mode), tempfile.TemporaryDirectory() as temp:
                base = Path(temp).resolve()
                root = base / "workspace"
                root.mkdir()
                key_file = base / "resume.key"
                args = _controller_args(root, resume_key_file=str(key_file), once=True)
                root_issue = self._issue(args.workflow_root)
                payloads: list[dict] = []
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                     mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                     mock.patch.object(cli.beads_backend, "claim_ready", return_value=None), \
                     mock.patch.object(cli, "_json_or_status"):
                    self.assertEqual(cli.controller_start(args), 0)
                    old_credentials = cli._read_controller_credentials(key_file)
                    if failure_mode == "spent":
                        # A legitimate reattach spends the previous proof;
                        # restoring it simulates a stale copied credential.
                        self.assertEqual(cli.controller_resume(args), 0)
                        cli._private_atomic_json(key_file, old_credentials)
                    elif failure_mode == "wrong":
                        wrong_credentials = dict(old_credentials)
                        wrong_credentials["resume_secret"] = "not-the-current-proof"
                        cli._private_atomic_json(key_file, wrong_credentials)
                    else:
                        key_file.unlink()

                    payloads.clear()
                    with mock.patch.object(cli.beads_backend, "claim_ready") as claim_ready, \
                         mock.patch.object(cli, "_provider_command") as provider_command, \
                         mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                        self.assertEqual(cli.controller_supervise(args), 2)

                self.assertFalse(payloads[-1]["ok"])
                self.assertTrue(
                    "credential" in payloads[-1]["error"]
                    or "proof" in payloads[-1]["error"]
                )
                claim_ready.assert_not_called()
                provider_command.assert_not_called()

    def test_supervisor_idle_polling_does_not_call_provider(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                return self.value

            def sleep(self, seconds: float) -> None:
                self.value += seconds

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "workspace"
            root.mkdir()
            clock = FakeClock()
            args = _controller_args(
                root, once=False, deadline=2.5, poll_interval=1.0,
                _monotonic=clock, _sleep=clock.sleep,
            )
            root_issue = self._issue(args.workflow_root)
            payloads: list[dict] = []
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready, \
                 mock.patch.object(cli, "_provider_command") as provider_command, \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                exit_code = cli.controller_supervise(args)

            self.assertEqual(exit_code, 3)
            self.assertGreater(claim_ready.call_count, 1)
            provider_command.assert_not_called()
            self.assertEqual(payloads[-1]["stop_reason"], "DEADLINE_EXCEEDED")

    def test_supervisor_stops_at_terminal_and_operator_safe_boundaries(self) -> None:
        for reason in ("GOAL_COMPLETE", "TASK_BLOCKED", "USER_ACTION_REQUIRED", "ROTATION_REQUIRED"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                args = _controller_args(root, once=False, deadline=10.0)
                payload = {"operation": "supervise", "ok": True, "stop_reason": reason}
                payloads: list[dict] = []
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state-home")}), \
                     mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                     mock.patch.object(cli, "_controller_step", return_value=(payload, True)) as step, \
                     mock.patch.object(cli, "_json_or_status", side_effect=lambda value, **_kwargs: payloads.append(value)):
                    self.assertEqual(cli.controller_supervise(args), 0)

                step.assert_called_once()
                self.assertEqual(payloads[-1]["stop_reason"], reason)

    def test_controller_loop_heartbeats_the_lease_between_iterations(self) -> None:
        """AFREL-020: the loop must be lease-heartbeating, not just
        re-polling with a stale lease that could go stale and get taken over."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, once=False, poll_interval=0.001, deadline=0.05)
            root_issue = self._issue("wf-root")
            calls = {"n": 0}

            def fake_claim_ready(cwd, *, parent, labels, actor):
                calls["n"] += 1
                return None  # never any ready work -- loop keeps idling until deadline

            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.controller_backend.RootController, "heartbeat") as heartbeat:
                heartbeat.side_effect = lambda lease=None: lease
                _run_controller_json(cli.controller_resume, args)
            self.assertGreater(calls["n"], 1)
            self.assertGreater(heartbeat.call_count, 0)

    def test_controller_run_dispatches_ready_task_via_real_herdr_launch(self) -> None:
        """The controller claims a ready descendant and dispatches it through
        the REAL herdr_launch (real from-bead handoff, real root preflight, real
        return channel, real argv); only the Beads boundary and the Herdr
        binary are stood in for. The task reaches a live running session."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            capture: dict = {}
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=fixture.task_issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["result"]["task"], "task-1")
            self.assertEqual(payload["result"]["state"], "running")
            self.assertEqual(payload["result"]["session_id"], "sess-1")
            # The dispatch really invoked Herdr with the supported argv.
            self.assertEqual(capture["argv"][:4], ["/usr/bin/herdr", "agent", "start", "task-1"])
            self.assertNotIn("--session", capture["argv"])
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")
            self.assertEqual(record["return_channel"]["state"], "issued")

    def test_codex_app_server_guard_returns_durable_task_block_without_starting_worker(self) -> None:
        """A permission guard is a typed launch failure, not a controller crash."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(
                Path(temp).resolve(), provider="codex", model="gpt-6-luna", seed_lease=False,
            )
            fixture.task_issue["metadata"]["agentflow"]["launch"]["transport"] = "app-server"
            fixture.task_issue["metadata"]["agentflow"]["tool_profile"] = "shell-readonly"
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            spawned: list[object] = []
            real_popen = subprocess.Popen

            def record_popen(command, *argv, **kwargs):
                spawned.append(command)
                return real_popen(command, *argv, **kwargs)

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=fixture.task_issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.codex_app_server_backend, "_sdk_modules", side_effect=AssertionError("SDK must not initialize")) as sdk, \
                 mock.patch.object(subprocess, "Popen", side_effect=record_popen):
                payload = _run_controller_json(cli.controller_resume, args)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["stop_reason"], "TASK_BLOCKED")
            self.assertEqual(payload["result"]["state"], "blocked")
            controller, _ = cli._controller_instance(args)
            self.assertEqual(controller._load_checkpoint()["state"], "blocked")
            sdk.assert_not_called()
            self.assertFalse(any(
                (
                    Path(str(command[0])).name.lower() in {"codex", "codex.exe"}
                    or "app-server" in {str(argument).lower() for argument in command[1:]}
                )
                for command in spawned if isinstance(command, (list, tuple)) and command
            ), spawned)
            sessions_path = fixture.root / ".agentflow/herdr/sessions.json"
            if sessions_path.exists():
                sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
                self.assertNotIn("task-1", sessions.get("sessions", {}))

    def test_supervisor_restart_keeps_lease_and_does_not_relaunch_live_herdr_task(self) -> None:
        """A separate-terminal restart uses the protected proof to keep the
        exact lease and reconciles the durable Herdr session instead of
        launching a second provider process."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            key_file = base / "resume.key"
            common_args = dict(
                workflow_root=fixture.workflow_root,
                resume_key_file=str(key_file),
                once=True,
                poll_interval=0.01,
                deadline=5.0,
            )
            claims = {"n": 0}
            spawned: list[list[str]] = []

            def fake_claim_ready(_cwd, *, parent, labels, actor):
                claims["n"] += 1
                if claims["n"] == 1:
                    fixture.task_issue["status"] = "in_progress"
                    fixture.task_issue["assignee"] = actor
                    return fixture.task_issue
                return None

            payloads: list[dict] = []

            first_args = _controller_args(fixture.root, **common_args)
            second_args = _controller_args(fixture.root, **common_args)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command) as provider_command, \
                 mock.patch.object(
                     cli.subprocess, "run",
                     side_effect=fixture.herdr_run(on_spawn=lambda argv: spawned.append(argv)),
                 ), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                self.assertEqual(cli.controller_supervise(first_args), 0)
                first = payloads[-1]
                self.assertEqual(first["result"]["state"], "running")
                provider_calls_after_first = provider_command.call_count
                self.assertEqual(cli.controller_supervise(second_args), 0)
                second = payloads[-1]

            self.assertEqual(first["lease"]["epoch"], second["lease"]["epoch"])
            self.assertEqual(first["lease"]["token"], second["lease"]["token"])
            self.assertEqual(first["lease"]["continuity_id"], second["lease"]["continuity_id"])
            self.assertEqual(second["result"]["state"], "running")
            self.assertEqual(provider_command.call_count, provider_calls_after_first)
            self.assertEqual(len(spawned), 1)
            self.assertEqual(claims["n"], 1)
            record = json.loads(
                (fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8")
            )["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")

    def test_controller_run_dispatches_gitless_directory_with_typed_workspace_contract(self) -> None:
        """A real controller preflight and Herdr launch work without .git or a fake base."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False, gitless=True)
            self.assertFalse(cli._is_git_repository(fixture.root))
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            capture: dict = {}
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=fixture.task_issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                payload = _run_controller_json(cli.controller_resume, args)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["result"]["state"], "running")
            self.assertEqual(capture["argv"][:4], ["/usr/bin/herdr", "agent", "start", "task-1"])
            manifest_path = fixture.root / ".agentflow/tmp/handoffs/task-1-claude.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["workspace_kind"], "directory")
            self.assertEqual(
                manifest["workspace_contract"],
                {
                    "schema": "agentflow.workspace@1",
                    "kind": "directory",
                    "root": str(fixture.root),
                    "base": None,
                },
            )
            self.assertEqual(manifest["base"], "")
            self.assertEqual(manifest["branch"], "")

    def test_git_workspace_contract_rejects_wrong_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            manifest = dict(fixture.handoff.manifest)
            branch, current_base = cli._git_identity(fixture.root)
            current_revision = current_base.partition("@")[2]
            manifest["base"] = f"{branch}@{current_revision[:1]}"
            workspace = dict(manifest["workspace_contract"])
            workspace["base"] = manifest["base"]
            manifest["workspace_contract"] = workspace
            errors = cli._workspace_contract_errors(manifest, observed_root=fixture.root)
            self.assertTrue(any("approved revision" in error for error in errors), errors)

    def test_git_base_requires_matching_12_to_40_hex_revision_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            branch, _ = cli._git_identity(fixture.root)
            resolved = subprocess.run(
                ["git", "-C", str(fixture.root), "rev-parse", "--verify", "HEAD^{commit}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            for length in (12, 20, 40):
                with self.subTest(length=length):
                    self.assertTrue(
                        cli._git_base_matches(fixture.root, f"{branch}@{resolved[:length]}")
                    )

            wrong_revision = ("0" if resolved[0] != "0" else "1") + resolved[1:]
            self.assertFalse(cli._git_base_matches(fixture.root, f"{branch}@{wrong_revision}"))
            self.assertFalse(cli._git_base_matches(fixture.root, f"{branch}@{resolved[:11]}"))
            self.assertFalse(cli._git_base_matches(fixture.root, f"{branch}@{resolved[:11]}g"))
            self.assertFalse(cli._git_base_matches(fixture.root, f"{branch}@{resolved}0"))
            self.assertFalse(cli._git_base_matches(fixture.root, f"{branch}@{resolved}g"))

    def test_directory_workspace_contract_rejects_changed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            root = Path(temp).resolve()
            manifest = {
                "workspace_kind": "directory",
                "workspace_contract": {
                    "schema": "agentflow.workspace@1",
                    "kind": "directory",
                    "root": str(Path(other).resolve()),
                    "base": None,
                },
                "base": "",
                "branch": "",
                "lane": "external",
            }
            errors = cli._workspace_contract_errors(manifest, observed_root=root)
            self.assertIn("workspace root does not match the exact target workspace", errors)

    def test_controller_run_blocks_spawn_when_root_preflight_fails(self) -> None:
        """AFREL-019: dispatch must call the mandatory root preflight and an
        invalid result must prevent the spawn -- reproduced by making
        preflight fail and asserting herdr_launch is never invoked."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            task_issue = self._issue(
                "task-1",
                metadata={"agentflow": {"launch": {
                    "provider": "claude", "model": "claude-sonnet-5",
                    "effort": "medium", "role": "coding",
                }}},
            )
            root_issue = self._issue("wf-root")

            def fake_get_issue(cwd, issue_id):
                return root_issue if issue_id == "wf-root" else task_issue

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fake_get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value={"id": "task-1", "labels": []}), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.preflight_backend, "check_launch", return_value=mock.Mock(launch_blocked=True)), \
                 mock.patch.object(cli, "herdr_launch") as launch:
                payload = _run_controller_json(cli.controller_resume, args)
            launch.assert_not_called()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["state"], "blocked")
            self.assertEqual(payload["stop_reason"], "TASK_BLOCKED")

    def test_materialize_launch_handoff_raises_on_a_real_invalid_contract(self) -> None:
        """AFREL-030: invalid-contract no-spawn regression -- when the real
        handoff_from_bead pipeline cannot materialize a bead (here: Beads
        itself cannot produce the issue), _materialize_launch_handoff must
        raise rather than silently returning something to launch with."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with mock.patch.object(
                cli.beads_backend, "get_issue",
                side_effect=cli.beads_backend.BeadsError("no such bead"),
            ):
                with self.assertRaises(ValueError):
                    cli._materialize_launch_handoff(root, "task-1", "claude", role="coding")
            self.assertFalse((root / ".agentflow/tmp/handoffs").exists())

    def test_controller_run_persists_exact_handoff_before_spawn(self) -> None:
        """AFREL-030: every spawn references a real, persisted from-bead handoff
        artifact -- title/goal/done-when/authority-boundary plus its structured
        manifest -- and the provider receives a bounded prompt pointing at it,
        not a route-only stub."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            capture: dict = {}
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=fixture.task_issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertEqual(payload["result"]["state"], "running")
            # A real from-bead handoff artifact was persisted and validated.
            handoff_path = fixture.root / ".agentflow/tmp/handoffs/task-1-claude.md"
            self.assertTrue(handoff_path.is_file())
            handoff_text = handoff_path.read_text(encoding="utf-8")
            self.assertIn("## Done when", handoff_text)
            self.assertIn("## Authority boundary", handoff_text)
            self.assertIn('Set `outcome` to exactly `completed`, `failed`,', handoff_text)
            self.assertIn('"status":"passed","evidence":"Exact check and observed result"', handoff_text)
            manifest = json.loads(handoff_path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["bead_id"], "task-1")
            self.assertEqual(manifest["provider"], "claude")
            self.assertEqual(manifest["machine_return_contract"]["schema"], "agentflow.return@1")
            # The provider argv delivers a bounded prompt referencing exactly
            # this persisted handoff artifact.
            argv = capture["argv"]
            provider_tail = argv[argv.index("--") + 1:]
            self.assertTrue(any(str(handoff_path) in token for token in provider_tail),
                            provider_tail)

    def test_controller_run_blocks_when_task_has_no_launch_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            task_issue = self._issue("task-1")  # no metadata.agentflow.launch
            root_issue = self._issue("wf-root")

            def fake_get_issue(cwd, issue_id):
                return root_issue if issue_id == "wf-root" else task_issue

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fake_get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value={"id": "task-1", "labels": []}), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "herdr_launch") as launch:
                payload = _run_controller_json(cli.controller_resume, args)
            launch.assert_not_called()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["state"], "blocked")
            self.assertEqual(payload["stop_reason"], "TASK_BLOCKED")

    def test_controller_run_uses_root_not_process_cwd_for_beads(self) -> None:
        """AFREL-024: controller --root must be authoritative for the Beads
        workspace; the process's actual working directory (what
        _task_cwd(args) falls back to when no --cwd is set, which the
        controller parser never defines) must never be silently substituted."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            del args.cwd  # the real controller parser has no --cwd at all
            matrix = {
                "version": 1, "task_id": "wf-root",
                "rows": [{
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                }],
            }
            root_issue = self._issue("wf-root", metadata={"agentflow": {"acceptance": matrix}})
            seen_cwds: list = []

            def fake_get_issue(cwd, issue_id):
                seen_cwds.append(cwd)
                return root_issue

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fake_get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None), \
                 mock.patch("pathlib.Path.cwd", return_value=Path("/tmp/a-totally-different-process-cwd")):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertTrue(payload["ok"], payload)
            self.assertTrue(seen_cwds)
            self.assertTrue(all(cwd == root for cwd in seen_cwds))

    def test_controller_run_completes_when_no_ready_work_and_matrix_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            matrix = {
                "version": 1, "task_id": "wf-root",
                "rows": [{
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                }],
            }
            root_issue = self._issue("wf-root", metadata={"agentflow": {"acceptance": matrix}})

            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["stop_reason"], "GOAL_COMPLETE")
            self.assertTrue(payload["result"]["terminal"])
            self.assertEqual(payload["result"]["state"], "completed")

    def test_controller_run_rejects_completion_with_nonterminal_descendant(self) -> None:
        """AFREL-022: a passed matrix plus an empty ready queue is not
        enough -- a blocked/deferred/cyclic descendant still open means
        USER_ACTION_REQUIRED, not GOAL_COMPLETE."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            matrix = {
                "version": 1, "task_id": "wf-root",
                "rows": [{
                    "id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                    "planned_evidence": "e", "status": "passed", "actual_evidence": "ev",
                }],
            }
            root_issue = self._issue("wf-root", metadata={"agentflow": {"acceptance": matrix}})
            stuck_descendant = self._issue("task-stuck", status="blocked")

            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[stuck_descendant]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertNotEqual(payload["result"]["state"], "completed")

    def _seed_in_flight_checkpoint(
        self, args: argparse.Namespace, *, task_id: str, session_id: str, state: str = "running",
    ):
        """Seed an in-flight checkpoint for ``task_id`` and return its lease.

        Real controller state (via an actual RootController), not a
        hand-crafted checkpoint dict, so Step 1's disposition logic runs
        against the genuine schema fencing validates against. The plaintext
        secret is only ever visible in-memory to whoever just acquired the
        lease (AFREL-023) -- capture it here and set args.resume_token so
        the SAME test's later controller_resume(args) call reattaches
        instead of colliding with this seeding acquire.
        """
        controller, controller_root = cli._controller_instance(args)
        lease = controller.acquire()
        args.resume_token = lease.resume_secret
        controller._save_checkpoint(
            {
                "task": task_id, "phase": "dispatch", "next_action": "await session",
                "root": str(controller_root), "controller": args.controller,
                "actor": args.controller, "claim_id": f"wf-root/{task_id}/{args.controller}",
                "session_id": session_id, "state": state, "status": state, "terminal": False,
            },
            lease=lease,
        )
        return lease

    def test_controller_halts_on_a_finalized_rejected_result(self) -> None:
        """A bad finalized inbox is a task blocker, not an endless poll."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            self._seed_in_flight_checkpoint(
                args, task_id="task-1", session_id="sess-1"
            )
            root_issue = self._issue("wf-root")
            with mock.patch.object(
                cli.beads_backend, "get_issue", return_value=root_issue
            ), mock.patch.object(
                cli,
                "_ingest_submitted_result",
                return_value=cli._SubmissionIngestion(
                    "rejected", "acceptance row R1 is missing evidence"
                ),
            ):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertEqual(payload["stop_reason"], "TASK_BLOCKED")
            self.assertEqual(payload["result"]["state"], "blocked")
            self.assertIn(
                "acceptance row R1 is missing evidence",
                payload["result"]["checkpoint"]["terminal_reason"],
            )

    def test_controller_run_identity_pending_reaches_durable_user_action_required(self) -> None:
        """AFREL-035: a provider that never reports agent_session must not
        poll forever or exit non-terminal on a generic loop deadline with
        the pane/lease stranded -- an explicit identity deadline reaches a
        durable USER_ACTION_REQUIRED halt, and the collision check still
        blocks any relaunch attempt afterward."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root, identity_deadline=0.01)
            self._seed_in_flight_checkpoint(
                args, task_id="task-1", session_id="", state="identity_pending",
            )
            long_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
            cli._private_atomic_json(
                root / ".agentflow/herdr/sessions.json",
                {
                    "schema": "agentflow.herdr", "version": 1,
                    "sessions": {"task-1": {
                        "status": "identity_pending", "pane_id": "pane-real",
                        "identity_pending_since": long_ago,
                        "root": str(root), "task_id": "task-1", "claim_id": "claim-1",
                        "lease_id": "lease-1", "provider": "codex", "launch_id": "launch-1",
                        "result": None, "binding": None, "attempt": 1, "attempts": [],
                    }},
                },
            )
            root_issue = self._issue("wf-root")
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli, "_provider_command", return_value=None):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertTrue(payload["result"]["terminal"])
            self.assertEqual(payload["result"]["state"], "blocked")

            # The reservation is still identity_pending (nothing cleared,
            # nothing relaunched); a retry attempt still collides.
            state = json.loads((root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sessions"]["task-1"]["status"], "identity_pending")

    def test_codex_trust_prompt_pauses_serial_controller_without_terminalizing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            self._seed_in_flight_checkpoint(
                args, task_id="task-1", session_id="", state="identity_pending",
            )
            cli._private_atomic_json(root / ".agentflow/herdr/sessions.json", {
                "schema": "agentflow.herdr", "version": 1,
                "sessions": {"task-1": {
                    "status": "identity_pending", "pane_id": "pane-real",
                    "provider": "codex", "identity_pending_since": cli._now(),
                }},
            })
            attention = {"code": "codex_project_trust_required", "pane_id": "pane-real", "path": str(root)}
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=self._issue("wf-root")), \
                 mock.patch.object(cli, "_resolve_pending_identity"), \
                 mock.patch.object(cli, "_codex_trust_attention", return_value=attention):
                payload = _run_controller_json(cli.controller_resume, args)
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertEqual(payload["action_required"], attention)
            self.assertFalse(payload["result"]["terminal"])

    def _run_reject_disposition(self, root, *, acceptance_ids=None, acceptance_results=None, outcome="completed"):
        """Drive the controller's step-1 disposition against a consumed
        authenticated result carrying the given (bad) acceptance data, and
        return the payload. The result genuinely reaches
        _validate_acceptance_results -- the block is the disposition rejecting
        the evidence, not a coarse missing-channel gate."""
        args = _controller_args(root)
        self._seed_in_flight_checkpoint(args, task_id="task-1", session_id="sess-1")
        cli._private_atomic_json(
            root / ".agentflow/herdr/sessions.json",
            self._consumed_session_record(
                root, "task-1", session_id="sess-1",
                acceptance_ids=acceptance_ids, acceptance_results=acceptance_results, outcome=outcome),
        )
        task1_issue = self._issue(
            "task-1", status="in_progress", assignee="agentflow-controller",
            acceptance_criteria="task-1 is done when the smoke test passes",
        )
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=task1_issue), \
             mock.patch.object(cli.beads_backend, "close_issue") as close_issue, \
             mock.patch.object(cli.beads_backend, "update_agentflow_metadata") as update_metadata:
            payload = _run_controller_json(cli.controller_resume, args)
        close_issue.assert_not_called()
        update_metadata.assert_not_called()
        self.assertEqual(payload["stop_reason"], "TASK_BLOCKED")
        self.assertEqual(payload["result"]["state"], "blocked")
        return payload

    def test_controller_run_rejects_empty_evidence_completion(self) -> None:
        """AFREL-026: a passed acceptance row with empty evidence must never
        close the bead -- "any matching result closes it" is the defect."""
        with tempfile.TemporaryDirectory() as temp:
            self._run_reject_disposition(
                Path(temp).resolve(),
                acceptance_results=[{"acceptance_id": "R1", "status": "passed",
                                     "evidence": "", "source": "provider"}])

    def test_controller_run_rejects_completion_without_acceptance_criteria(self) -> None:
        """A return channel with no acceptance IDs at all is not an
        authenticated disposition and must never close the bead."""
        with tempfile.TemporaryDirectory() as temp:
            self._run_reject_disposition(
                Path(temp).resolve(), acceptance_ids=[], acceptance_results=[])

    def test_controller_run_rejects_secret_shaped_evidence(self) -> None:
        """AFREL-026/AFREL-018: acceptance evidence is secret-scanned the same
        as Herdr's own result-ingestion path."""
        with tempfile.TemporaryDirectory() as temp:
            self._run_reject_disposition(
                Path(temp).resolve(),
                acceptance_results=[{"acceptance_id": "R1", "status": "passed",
                                     "evidence": "gh" + "p_" + ("a" * 36),
                                     "source": "provider"}])

    def test_controller_run_rejects_failed_status_evidence(self) -> None:
        """AFREL-033: an acceptance row whose own status is not "passed"
        (here "failed") must never close the bead -- presence of evidence is
        not a passing disposition."""
        with tempfile.TemporaryDirectory() as temp:
            self._run_reject_disposition(
                Path(temp).resolve(),
                acceptance_results=[{"acceptance_id": "R1", "status": "failed",
                                     "evidence": "unit tests failed", "source": "provider"}])

    def test_controller_run_rejects_evidence_without_explicit_positive_outcome(self) -> None:
        """AFREL-033: an acceptance row with no status at all is incomplete --
        it must not be silently treated as passing just because a row exists."""
        with tempfile.TemporaryDirectory() as temp:
            self._run_reject_disposition(
                Path(temp).resolve(),
                acceptance_results=[{"acceptance_id": "R1", "evidence": "ran the smoke test",
                                     "source": "provider"}])

    def test_controller_run_disposition_is_idempotent_across_crash_resume(self) -> None:
        """AFREL-026: a crash between recording disposition and advance()
        must not re-close an already-closed bead or duplicate the record
        on the next resume() call."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            args = _controller_args(root)
            self._seed_in_flight_checkpoint(args, task_id="task-1", session_id="sess-1")
            # The provider already self-reported through the authenticated
            # channel (state == "consumed"); a prior process then closed the
            # bead but crashed before advance().
            cli._private_atomic_json(
                root / ".agentflow/herdr/sessions.json",
                self._consumed_session_record(root, "task-1", session_id="sess-1"),
            )
            # Beads already shows the bead closed -- as if a prior process
            # crashed after close_issue succeeded but before advance().
            already_closed_issue = self._issue(
                "task-1", status="closed", assignee="agentflow-controller",
                acceptance_criteria="task-1 is done when the smoke test passes",
            )
            root_issue = self._issue("wf-root")

            def fake_get_issue(cwd, issue_id):
                return root_issue if issue_id == "wf-root" else already_closed_issue

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fake_get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "close_issue") as close_issue, \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata") as update_metadata, \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None):
                payload = _run_controller_json(cli.controller_resume, args)
            # Idempotent: the already-closed bead is neither re-closed nor
            # re-recorded, and traversal advances past it (never stuck
            # re-processing the disposed task).
            close_issue.assert_not_called()
            update_metadata.assert_not_called()
            self.assertTrue(payload["ok"], payload)
            self.assertNotEqual(payload["result"]["task"], "task-1")

    def test_controller_run_reconciles_orphaned_in_progress_claim(self) -> None:
        """AFREL-021: a task Beads already shows as in_progress + assigned to
        this controller (a crash right after the Beads claim), but our own
        checkpoint never recorded, must be adopted -- not left orphaned, and
        not silently re-claimed via claim_ready -- and dispatched for real."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["status"] = "in_progress"
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready, \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                payload = _run_controller_json(cli.controller_resume, args)
            claim_ready.assert_not_called()
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["result"]["task"], "task-1")
            self.assertEqual(payload["result"]["state"], "running")

    def test_claimed_checkpoint_reattaches_committed_result_through_authority_checks(self) -> None:
        """A committed Herdr identity can safely repair a claim-only
        checkpoint; result consumption and disposition still use the existing
        authenticated path, and no replacement provider is launched."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["acceptance"] = {
                "version": 1, "task_id": fixture.workflow_root,
                "rows": [{
                    "id": "R1", "outcome": "root complete", "owner": "eng",
                    "lane": "static", "planned_evidence": "task closes",
                    "status": "passed", "actual_evidence": "task result authenticated",
                }],
            }
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            self._seed_in_flight_checkpoint(
                args, task_id="task-1", session_id="", state="claimed_no_session",
            )
            cli._private_atomic_json(
                fixture.root / ".agentflow/herdr/sessions.json",
                self._consumed_session_record(fixture.root, "task-1", session_id="sess-1"),
            )
            fixture.task_issue["status"] = "in_progress"
            closed: list[str] = []

            def get_issue(_cwd, issue_id):
                return fixture.root_issue if issue_id == fixture.workflow_root else fixture.task_issue

            def close_issue(_cwd, task_id, _reason):
                fixture.task_issue["status"] = "closed"
                closed.append(task_id)

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=close_issue), \
                 mock.patch.object(cli, "_dispatch_via_herdr") as dispatch_builder:
                payload = _run_controller_json(cli.controller_resume, args)

            self.assertEqual(payload["stop_reason"], "GOAL_COMPLETE")
            self.assertEqual(payload["result"]["state"], "completed")
            self.assertEqual(closed, ["task-1"])
            dispatch_builder.assert_not_called()

    def test_controller_run_advances_after_matching_result_then_claims_next(self) -> None:
        """After a matching authenticated completed result for the in-flight
        task, the controller closes it exactly once, advances, and claims +
        dispatches the next ready descendant for real -- one step end to end."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root,
                                    once=True, poll_interval=0.01, deadline=5.0)
            # task-1 is in flight with an already-consumed authenticated result.
            self._seed_in_flight_checkpoint(args, task_id="task-1", session_id="sess-1")
            cli._private_atomic_json(
                fixture.root / ".agentflow/herdr/sessions.json",
                self._consumed_session_record(fixture.root, "task-1", session_id="sess-1"),
            )
            task1_closed = dict(fixture.task_issue, id="task-1", status="in_progress")
            # A second, fully launchable ready descendant.
            task2 = json.loads(json.dumps(fixture.task_issue))
            task2["id"] = "task-2"
            task2["status"] = "open"
            task2["metadata"]["agentflow"]["task"] = "task-2"
            task2["metadata"]["agentflow"]["claim_token"] = "opaque-claim-token-task2-9999999999999999999999"
            task2["metadata"]["agentflow"]["acceptance"]["task_id"] = "task-2"
            issues = {"wf-root": fixture.root_issue, "task-1": task1_closed, "task-2": task2}

            def fake_get_issue(cwd, issue_id):
                return issues[issue_id]

            def fake_close_issue(cwd, task_id, reason):
                task1_closed["status"] = "closed"

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fake_get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[task1_closed, task2]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=fake_close_issue) as close_issue, \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=task2), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                payload = _run_controller_json(cli.controller_resume, args)
            close_issue.assert_called_once()
            self.assertEqual(close_issue.call_args.args[1], "task-1")
            self.assertEqual(payload["result"]["task"], "task-2")
            self.assertEqual(payload["result"]["state"], "running")
            self.assertEqual(payload["result"]["session_id"], "sess-1")


class GenuineLifecycleTests(unittest.TestCase):
    """End-to-end proof of the corrected persistent controller lifecycle:
    a valid launch reaches the real return channel, a provider finalizes an
    untrusted inbox, and the controller authenticates, disposes, and reaches
    GOAL_COMPLETE -- plus adversarial incarnation/takeover and waiver cases."""

    def _passed_root_matrix(self, task_id: str) -> dict:
        return {
            "version": 1, "task_id": task_id,
            "rows": [{"id": "R1", "outcome": "smoke passes", "owner": "eng",
                      "lane": "static", "planned_evidence": "unit test",
                      "status": "passed", "actual_evidence": "smoke passed"}],
        }

    def test_single_invocation_dispatch_to_goal_complete_via_authenticated_result(self) -> None:
        """One controller invocation carries dispatch -> provider spawn ->
        provider inbox submission -> controller authentication/disposition ->
        GOAL_COMPLETE, with no manual consumer or second controller invocation."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["acceptance"] = self._passed_root_matrix("wf-root")
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root,
                                    once=False, poll_interval=0.01, deadline=8.0)
            threads: list[threading.Thread] = []
            report_done = threading.Event()

            def on_spawn(argv: list) -> None:
                env = {}
                for i, token in enumerate(argv):
                    if token == "--env":
                        key, _, value = argv[i + 1].partition("=")
                        env[key] = value

                def report() -> None:
                    session_file = fixture.root / ".agentflow/herdr/sessions.json"
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        try:
                            record = json.loads(session_file.read_text(encoding="utf-8"))["sessions"].get("task-1", {})
                            channel = record.get("return_channel") or {}
                            if record.get("status") == "launched" and channel.get("state") == "issued":
                                break
                        except (OSError, json.JSONDecodeError, KeyError):
                            pass
                        time.sleep(0.01)
                    # Wait for the proof condition itself instead of assuming
                    # an overloaded runner schedules two polls within 50 ms.
                    gate_deadline = time.monotonic() + 5.0
                    while time.monotonic() < gate_deadline and polls["n"] < 2:
                        time.sleep(0.005)
                    self.assertGreaterEqual(polls["n"], 2)
                    contract = json.loads(Path(env["AGENTFLOW_RESULT_CONTRACT"]).read_text(encoding="utf-8"))
                    result = {"outcome": "completed", "acceptance_results": [
                        {"acceptance_id": aid, "status": "passed",
                         "evidence": "the smoke test passed", "source": "provider"}
                        for aid in contract["acceptance_ids"]]}
                    Path(env["AGENTFLOW_RESULT_FILE"]).write_text(json.dumps(result), encoding="utf-8")
                    report_args = argparse.Namespace(
                        root=str(fixture.root), contract=env["AGENTFLOW_RESULT_CONTRACT"],
                        file=env["AGENTFLOW_RESULT_FILE"], json=True)
                    cli.herdr_submit(report_args)
                    report_done.set()

                thread = threading.Thread(target=report, daemon=True)
                thread.start()
                threads.append(thread)

            claim_calls = {"n": 0}

            def fake_claim_ready(cwd, *, parent, labels, actor):
                claim_calls["n"] += 1
                return fixture.task_issue if claim_calls["n"] == 1 else None

            def fake_root_descendants(cwd, root_id):
                return [fixture.task_issue]

            def fake_close_issue(cwd, task_id, reason):
                fixture.task_issue["status"] = "closed"

            payloads: list[dict] = []

            def record_payload(payload, as_json=False, title=""):
                payloads.append(payload)

            # Wrap (not mock) the REAL Herdr result reader to prove the loop
            # actually polls a live session more than once before completion.
            real_task_result = cli._herdr_task_result
            polls = {"n": 0}

            def spy_task_result(root, task_id):
                polls["n"] += 1
                return real_task_result(root, task_id)

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", side_effect=fake_root_descendants), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=fake_close_issue) as close_issue, \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli, "_herdr_task_result", side_effect=spy_task_result), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(on_spawn=on_spawn)), \
                 mock.patch.object(cli, "_json_or_status", side_effect=record_payload):
                self.assertEqual(cli.controller_resume(args), 0)
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(report_done.is_set(), "provider never finalized its result inbox")
            self.assertGreaterEqual(polls["n"], 2, "autonomous loop did not poll the live session more than once")
            result_payloads = [p for p in payloads if p.get("operation") == "result"]
            self.assertTrue(result_payloads and result_payloads[-1]["ok"], result_payloads)
            resume_payloads = [p for p in payloads if p.get("operation") == "resume"]
            self.assertEqual(len(resume_payloads), 1, resume_payloads)
            final = resume_payloads[-1]
            self.assertEqual(final["stop_reason"], "GOAL_COMPLETE", final)
            self.assertTrue(final["result"]["terminal"])
            self.assertEqual(final["result"]["state"], "completed")
            close_issue.assert_called_once()
            self.assertEqual(close_issue.call_args.args[1], "task-1")
            # The result was consumed exactly once through the authenticated
            # channel -- the durable session record proves it, not a mock.
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["return_channel"]["state"], "consumed")
            self.assertEqual(record["result"]["acceptance_results"][0]["acceptance_id"], "R1")

    def test_different_owner_takeover_rejects_the_previous_incarnations_result(self) -> None:
        """Fix #3: a different owner taking over the same reusable controller
        name rotates the incarnation continuity id, so a result issued by the
        previous incarnation is rejected -- it can never be consumed after the
        takeover."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            channel = record["return_channel"]

            # A different owner takes over the same reusable controller name.
            state_path = cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json"
            intruder = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path, stale_after=0.01)
            time.sleep(0.05)
            taken = intruder.acquire(takeover=True)
            self.assertNotEqual(taken.continuity_id, fixture.lease.continuity_id)

            # The previously issued result is now bound to a superseded
            # incarnation and must fail closed.
            Path(channel["result_path"]).write_text(json.dumps({
                "outcome": "completed",
                "acceptance_results": [{"acceptance_id": "R1", "status": "passed",
                                        "evidence": "stale", "source": "provider"}],
            }), encoding="utf-8")
            report_args = argparse.Namespace(
                root=str(fixture.root), contract=channel["contract_path"],
                file=channel["result_path"], json=True,
                _controller_ingest=True,
                _capability_file=channel["capability_file"],
                _authority_secret=fixture.authority_secret)
            payloads: list[dict] = []
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda p, **k: payloads.append(p)):
                self.assertEqual(cli.herdr_result(report_args), 2)
            self.assertIn("superseded controller incarnation", payloads[-1]["error"])
            # The channel was never consumed.
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["return_channel"]["state"], "issued")

    def test_authenticated_result_consumed_once_then_replay_rejected(self) -> None:
        """A legitimate result is consumed exactly once; a replay against the
        now-consumed channel fails closed."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            channel = record["return_channel"]
            result_body = {"outcome": "completed",
                           "acceptance_results": [{"acceptance_id": "R1", "status": "passed",
                                                   "evidence": "smoke passed", "source": "provider"}]}
            # Snapshot the protected channel files: a successful consume deletes
            # them, so to exercise the "already consumed" channel-state guard on
            # replay (rather than a mere missing-file rejection) we restore the
            # exact bytes and retry.
            contract_bytes = Path(channel["contract_path"]).read_bytes()
            cap_bytes = Path(channel["capability_file"]).read_bytes()
            Path(channel["result_path"]).write_text(json.dumps(result_body), encoding="utf-8")
            report_args = argparse.Namespace(
                root=str(fixture.root), contract=channel["contract_path"],
                file=channel["result_path"], json=True,
                _controller_ingest=True,
                _capability_file=channel["capability_file"],
                _authority_secret=fixture.authority_secret)
            with fixture.beads_patches():
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_result(report_args), 0)
                record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
                self.assertEqual(record["return_channel"]["state"], "consumed")
                # Replay: restore the deleted protected files and retry -- the
                # channel is already consumed, so it must fail closed.
                Path(channel["contract_path"]).write_bytes(contract_bytes)
                Path(channel["capability_file"]).write_bytes(cap_bytes)
                Path(channel["result_path"]).write_text(json.dumps(result_body), encoding="utf-8")
                payloads: list[dict] = []
                with mock.patch.object(cli, "_json_or_status", side_effect=lambda p, **k: payloads.append(p)):
                    self.assertEqual(cli.herdr_result(report_args), 2)
            self.assertIn("consumed", payloads[-1]["error"])

    def test_provider_cannot_replace_acceptance_id_in_return_contract(self) -> None:
        """The provider-visible contract is integrity-bound to launch state."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            channel = record["return_channel"]
            contract_path = Path(channel["contract_path"])
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["acceptance_ids"] = ["FORGED"]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            result_path = Path(channel["result_path"])
            result_path.write_text(json.dumps({
                "outcome": "completed",
                "acceptance_results": [{
                    "acceptance_id": "FORGED", "status": "passed",
                    "evidence": "forged evidence", "source": "provider",
                }],
            }), encoding="utf-8")
            payloads: list[dict] = []
            report_args = argparse.Namespace(
                root=str(fixture.root), contract=str(contract_path),
                file=str(result_path), json=True,
                _controller_ingest=True,
                _capability_file=channel["capability_file"],
                _authority_secret=fixture.authority_secret,
            )
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_: payloads.append(payload)):
                self.assertEqual(cli.herdr_result(report_args), 2)
            self.assertIn("integrity", payloads[-1]["error"])
            current = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(current["return_channel"]["state"], "issued")

    def test_imported_result_consumer_cannot_load_authority_for_a_worker(self) -> None:
        """A worker cannot turn the internal consumer into a confused deputy."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            state_path = fixture.root / ".agentflow/herdr/sessions.json"
            channel = json.loads(state_path.read_text(encoding="utf-8"))[
                "sessions"
            ]["task-1"]["return_channel"]
            Path(channel["result_path"]).write_text(json.dumps({
                "outcome": "completed",
                "acceptance_results": [{
                    "acceptance_id": "R1", "status": "passed",
                    "evidence": "worker attempted direct ingestion",
                    "source": "provider",
                }],
            }), encoding="utf-8")
            payloads: list[dict] = []
            args = argparse.Namespace(
                root=str(fixture.root),
                contract=channel["contract_path"],
                file=channel["result_path"],
                json=True,
                _controller_ingest=True,
                _capability_file=channel["capability_file"],
                # Deliberately no _authority_secret: the consumer must not
                # load controller credentials on behalf of this caller.
            )
            with fixture.beads_patches(), \
                 mock.patch.object(
                     cli, "_json_or_status",
                     side_effect=lambda payload, **_: payloads.append(payload),
                 ):
                self.assertEqual(cli.herdr_result(args), 2)
            self.assertIn("authority is unavailable", payloads[-1]["error"])
            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                current["sessions"]["task-1"]["return_channel"]["state"], "issued"
            )

    def test_finalized_invalid_inbox_is_rejected_not_left_pending(self) -> None:
        """A submitted invalid result returns a typed rejection to the controller."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            state_path = fixture.root / ".agentflow/herdr/sessions.json"
            channel = json.loads(state_path.read_text(encoding="utf-8"))[
                "sessions"
            ]["task-1"]["return_channel"]
            Path(channel["result_path"]).write_text(json.dumps({
                "outcome": "completed",
                "acceptance_results": [{
                    "acceptance_id": "FORGED", "status": "passed",
                    "evidence": "not bound to the task", "source": "provider",
                }],
            }), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.herdr_submit(argparse.Namespace(
                    contract=channel["contract_path"],
                    file=channel["result_path"],
                    json=True,
                )), 0)
            with fixture.beads_patches():
                ingestion = cli._ingest_submitted_result(
                    fixture.root,
                    "task-1",
                    authority_secret=fixture.authority_secret,
                )
            self.assertEqual(ingestion.status, "rejected")
            self.assertIn("unknown or duplicate acceptance ID", ingestion.error)
            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                current["sessions"]["task-1"]["return_channel"]["state"], "issued"
            )

    def test_provider_cannot_rewrite_contract_and_workspace_state_together(self) -> None:
        """Workspace copies are untrusted; the external controller MAC wins."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve())
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            state_path = fixture.root / ".agentflow/herdr/sessions.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            channel = state["sessions"]["task-1"]["return_channel"]
            contract_path = Path(channel["contract_path"])
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["acceptance_ids"] = ["FORGED"]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            # Rewriting the workspace's digest and "durable" copy is not
            # sufficient: neither contains the external controller key.
            channel["contract_binding"] = contract
            channel["contract_sha256"] = cli._canonical_json_digest(contract)
            channel["acceptance_ids"] = ["FORGED"]
            cli._private_atomic_json(state_path, state)
            result_path = Path(channel["result_path"])
            result_path.write_text(json.dumps({
                "outcome": "completed",
                "acceptance_results": [{
                    "acceptance_id": "FORGED", "status": "passed",
                    "evidence": "forged evidence", "source": "provider",
                }],
            }), encoding="utf-8")
            payloads: list[dict] = []
            report_args = argparse.Namespace(
                root=str(fixture.root), contract=str(contract_path),
                file=str(result_path), json=True,
                _controller_ingest=True,
                _capability_file=channel["capability_file"],
                _authority_secret=fixture.authority_secret,
            )
            with fixture.beads_patches(), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_: payloads.append(payload)):
                self.assertEqual(cli.herdr_result(report_args), 2)
            self.assertIn("signature", payloads[-1]["error"])
            current = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                current["sessions"]["task-1"]["return_channel"]["state"], "issued"
            )

    # -- Waiver authorization (Fix #4) --------------------------------------

    def _waived_matrix(self, approval_ref: str = "approval-1") -> dict:
        return {
            "version": 1, "task_id": "wf-root",
            "rows": [
                {"id": "R1", "outcome": "x", "owner": "o", "lane": "static",
                 "planned_evidence": "e", "status": "passed", "actual_evidence": "ev"},
                {"id": "R2", "outcome": "x", "owner": "o", "lane": "static",
                 "planned_evidence": "e", "status": "waived", "note": "approved exception",
                 "approval_ref": approval_ref},
            ],
        }

    def _root_issue(self, matrix: dict) -> dict:
        return {"id": "wf-root", "status": "open",
                "metadata": {"agentflow": {"actor": "writer", "acceptance": matrix}}}

    def _typed_approval(self, **overrides) -> dict:
        approval = {
            "schema": "agentflow.waiver-approval@1", "decision": "approved",
            "approval_ref": "approval-1", "workflow_root": "wf-root", "task": "wf-root",
            "acceptance_id": "R2", "approved_by": "release-manager",
            "approved_at": "2026-07-01T00:00:00Z",
        }
        approval.update(overrides)
        return {"id": "approval-1", "status": "closed", "title": "Waiver approval",
                "metadata": {"agentflow": {"waiver_approval": approval}}}

    def test_waiver_authorized_only_by_typed_durable_bound_approval(self) -> None:
        issue = self._root_issue(self._waived_matrix())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            controller = cli.controller_backend.RootController(
                str(root), "release-controller",
                state_path=cli._controller_state_dir(root, "wf-root") / "state.json",
            )
            lease = controller.acquire()
            authority_secret = _controller_authority(root, "wf-root", lease)
            controller.approve_waiver(
                workflow_root="wf-root", task="wf-root", acceptance_id="R2",
                approval_ref="approval-1", approved_by="release-manager",
                approved_at="2026-07-01T00:00:00Z",
                authority_secret=authority_secret, lease=lease,
            )
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=self._typed_approval()):
                self.assertTrue(cli._root_acceptance_passed(issue, beads_cwd=root))

    def test_waiver_rejects_title_only_closed_issue(self) -> None:
        """The removed heuristic: a closed bead whose title merely contains
        'waiver'/'approval'/'decision', with no typed approval metadata, must
        NOT authorize a waiver."""
        issue = self._root_issue(self._waived_matrix())
        title_only = {"id": "approval-1", "status": "closed", "title": "Waiver approval decision"}
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=title_only):
            self.assertFalse(cli._root_acceptance_passed(issue, beads_cwd=Path("/tmp")))

    def test_waiver_rejects_unrelated_or_mismatched_binding(self) -> None:
        issue = self._root_issue(self._waived_matrix())
        for bad in (
            self._typed_approval(acceptance_id="R9"),      # wrong row
            self._typed_approval(workflow_root="other"),   # wrong root
            self._typed_approval(task="other"),            # wrong task
            self._typed_approval(approval_ref="approval-2"),  # ref mismatch
            self._typed_approval(decision="pending"),      # not approved
        ):
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=bad):
                self.assertFalse(cli._root_acceptance_passed(issue, beads_cwd=Path("/tmp")), bad)

    def test_waiver_rejects_open_or_incomplete_decision(self) -> None:
        issue = self._root_issue(self._waived_matrix())
        open_decision = self._typed_approval()
        open_decision["status"] = "open"
        missing_approver = self._typed_approval(approved_by="")
        missing_timestamp = self._typed_approval(approved_at="")
        for bad in (open_decision, missing_approver, missing_timestamp):
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=bad):
                self.assertFalse(cli._root_acceptance_passed(issue, beads_cwd=Path("/tmp")), bad)

    def test_waiver_rejects_self_approval_by_the_worker_actor(self) -> None:
        """A worker cannot approve its own waiver: the durable approver must
        not be the worker actor."""
        issue = self._root_issue(self._waived_matrix())
        self_approved = self._typed_approval(approved_by="writer")  # == metadata.actor
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=self_approved):
            self.assertFalse(cli._root_acceptance_passed(issue, beads_cwd=Path("/tmp")))

    def test_result_waiver_rejects_worker_created_closed_issue(self) -> None:
        """In the provider result path too, a waived acceptance row is rejected
        unless a typed durable external approval authorizes it."""
        contract = {"actor": "writer", "workflow_root": "wf-root",
                    "approved_waivers": ["approval-1"]}
        acceptance = [{"acceptance_id": "R1", "status": "waived", "evidence": "n/a",
                       "reason": "flaky", "approved_by": "release-manager",
                       "approved_at": "2026-07-01T00:00:00Z", "approval_ref": "approval-1"}]
        # A worker-created closed issue with no typed approval metadata.
        worker_bead = {
            "id": "approval-1", "status": "closed", "title": "waiver please",
            "created_by": "writer", "assignee": "writer",
            "metadata": {"agentflow": {"waiver_approval": {
                "schema": "agentflow.waiver-approval@1", "decision": "approved",
                "approval_ref": "approval-1", "workflow_root": "wf-root",
                "task": "task-1", "acceptance_id": "R1", "approved_by": "release-manager",
                "approved_at": "2026-07-01T00:00:00Z",
            }}},
        }
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=worker_bead):
            with self.assertRaises(ValueError):
                cli._validate_acceptance_results(acceptance, ("R1",), contract,
                                                 beads_cwd=Path("/tmp"), task_id="task-1")
        # A typed Beads record alone is still not authority. The exact
        # controller-owned approval operation is required as well.
        approval = {"id": "approval-1", "status": "closed",
                    "metadata": {"agentflow": {"waiver_approval": {
                        "schema": "agentflow.waiver-approval@1", "decision": "approved",
                        "approval_ref": "approval-1", "workflow_root": "wf-root", "task": "task-1",
                        "acceptance_id": "R1", "approved_by": "release-manager",
                        "approved_at": "2026-07-01T00:00:00Z"}}}}
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=approval):
            with self.assertRaises(ValueError):
                cli._validate_acceptance_results(acceptance, ("R1",), contract,
                                                 beads_cwd=Path("/tmp"), task_id="task-1")

    def test_worker_cannot_forge_controller_waiver_in_workspace_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_path = cli._controller_state_dir(root, "wf-root") / "state.json"
            controller = cli.controller_backend.RootController(
                str(root), "agentflow-controller", state_path=state_path,
            )
            lease = controller.acquire()
            _controller_authority(root, "wf-root", lease)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["waiver_approvals"] = [{
                "schema": "agentflow.controller-waiver@1",
                "workflow_root": "wf-root",
                "task": "task-1",
                "acceptance_id": "R1",
                "approval_ref": "approval-1",
                "approved_by": "release-manager",
                "approved_at": "2026-07-27T00:00:00Z",
                "controller_id": lease.controller,
                "continuity_id": lease.continuity_id,
                "epoch": lease.epoch,
                "authority_hmac": "0" * 64,
            }]
            cli._private_atomic_json(state_path, state)
            contract = {
                "actor": "writer",
                "controller_id": lease.controller,
                "workflow_root": "wf-root",
                "approved_waivers": ["approval-1"],
            }
            result = [{
                "acceptance_id": "R1", "status": "waived",
                "evidence": "forged", "reason": "skip",
                "approved_by": "release-manager",
                "approved_at": "2026-07-27T00:00:00Z",
                "approval_ref": "approval-1",
            }]
            approval_bead = {
                "id": "approval-1", "status": "closed", "created_by": "writer",
                "metadata": {"agentflow": {"waiver_approval": {
                    "schema": "agentflow.waiver-approval@1",
                    "decision": "approved",
                    "approval_ref": "approval-1",
                    "workflow_root": "wf-root",
                    "task": "task-1",
                    "acceptance_id": "R1",
                    "approved_by": "release-manager",
                    "approved_at": "2026-07-27T00:00:00Z",
                }}},
            }
            with mock.patch.object(
                cli.beads_backend, "get_issue", return_value=approval_bead
            ):
                with self.assertRaises(ValueError):
                    cli._validate_acceptance_results(
                        result, ("R1",), contract,
                        beads_cwd=root, task_id="task-1",
                    )


class ManagedInstallCliTests(unittest.TestCase):
    def test_codex_hook_merge_retains_user_data_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / ".codex/hooks.json"
            destination.parent.mkdir(parents=True)
            packaged = json.loads(
                cli.packaged_resources.text("templates", "user", "codex-hooks.json")
            )
            session_handler = packaged["hooks"]["SessionStart"][0]["hooks"][0]
            herdr_handler = {"type": "command", "command": "herdr event handler"}
            custom_agentflow_handler = {
                **session_handler,
                "timeout": 19,
                "command": session_handler["command"] + " --customized",
            }
            existing = {
                "description": "user-owned description",
                "metadata": {"retained": True},
                "hooks": {
                    "SessionStart": [{
                        "matcher": "user-start-matcher",
                        "label": "keep this wrapper",
                        "hooks": [session_handler, herdr_handler, custom_agentflow_handler],
                    }],
                    "HerdrEvent": [{
                        "matcher": "herdr-only",
                        "hooks": [herdr_handler],
                    }],
                },
            }
            destination.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            manifest = {"schema": cli.MANAGED_INSTALL_SCHEMA, "resources": {}}
            parts = ("templates", "user", "codex-hooks.json")

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "xdg")}):
                result = cli._install_merged_codex_hooks(
                    destination, parts, manifest, dry_run=False, refresh=True
                )
                self.assertEqual(result, "updated")
                merged = json.loads(destination.read_text(encoding="utf-8"))
                session = merged["hooks"]["SessionStart"][0]
                self.assertEqual(session["matcher"], "user-start-matcher")
                self.assertEqual(session["label"], "keep this wrapper")
                self.assertIn(herdr_handler, session["hooks"])
                self.assertIn(custom_agentflow_handler, session["hooks"])
                self.assertEqual(merged["description"], "user-owned description")
                self.assertEqual(merged["metadata"], {"retained": True})
                self.assertEqual(merged["hooks"]["HerdrEvent"][0]["matcher"], "herdr-only")
                self.assertEqual(
                    sum(item == session_handler for item in session["hooks"]), 1
                )
                for event, handlers in cli._managed_hook_inventory(packaged).items():
                    installed = [
                        item
                        for group in merged["hooks"][event]
                        for item in group.get("hooks", [])
                    ]
                    for handler in handlers:
                        self.assertEqual(installed.count(handler), 1, event)

                first_bytes = destination.read_bytes()
                backup_root = root / "xdg/agentflow/backups"
                backups_before = sorted(backup_root.rglob("hooks.json"))
                self.assertEqual(len(backups_before), 1)
                self.assertEqual(
                    cli._install_merged_codex_hooks(
                        destination, parts, manifest, dry_run=False, refresh=True
                    ),
                    "unchanged",
                )
                self.assertEqual(destination.read_bytes(), first_bytes)
                self.assertEqual(sorted(backup_root.rglob("hooks.json")), backups_before)

    def test_hook_merge_replaces_only_proven_handler_and_preserves_matcher(self) -> None:
        packaged = json.loads(
            cli.packaged_resources.text("templates", "user", "codex-hooks.json")
        )
        prior = cli._managed_hook_inventory(packaged)
        old = packaged["hooks"]["SessionStart"][0]["hooks"][0]
        updated_package = json.loads(json.dumps(packaged))
        new = updated_package["hooks"]["SessionStart"][0]["hooks"][0]
        new["timeout"] = old["timeout"] + 7
        custom = {"command": "keep unrelated command", "type": "command"}
        existing = {
            "topLevel": "keep",
            "hooks": {
                "SessionStart": [{
                    "matcher": "user-customized-matcher",
                    "metadata": {"keep": 1},
                    "hooks": [old, custom],
                }]
            },
        }
        merged, _ = cli._merge_agentflow_hook_config(
            existing, updated_package, previously_owned=prior
        )
        group = merged["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "user-customized-matcher")
        self.assertEqual(group["metadata"], {"keep": 1})
        self.assertIn(custom, group["hooks"])
        self.assertIn(new, group["hooks"])
        self.assertNotIn(old, group["hooks"])
        self.assertEqual(merged["topLevel"], "keep")

    def test_shared_merge_api_handles_claude_settings_shape(self) -> None:
        packaged = json.loads(
            cli.packaged_resources.text("templates", "project", "claude-settings.json")
        )
        existing = {
            "model": "user-model",
            "hooks": {
                "SessionStart": [{
                    "matcher": "user-session-filter",
                    "customMetadata": "preserve",
                    "hooks": [{"type": "command", "command": "custom session hook"}],
                }],
                "UserEvent": [{"hooks": [{"type": "command", "command": "user hook"}]}],
            },
        }
        merged, handlers = cli._merge_agentflow_hook_config(existing, packaged)
        session_rules = merged["hooks"]["SessionStart"]
        self.assertEqual(session_rules[0]["matcher"], "user-session-filter")
        self.assertEqual(session_rules[0]["customMetadata"], "preserve")
        self.assertEqual(session_rules[0]["hooks"][0]["command"], "custom session hook")
        self.assertIn("PostModelSwitch", merged["hooks"])
        self.assertIn("PostModelSwitch", handlers)
        self.assertEqual(merged["hooks"]["UserEvent"][0]["hooks"][0]["command"], "user hook")
        self.assertEqual(merged["model"], "user-model")

    def test_legacy_exact_package_match_establishes_ownership_but_edits_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = cli.packaged_resources.names("skills")[0]
            source = cli.packaged_resources.item("skills", skill)
            exact_destination = root / "exact" / skill
            exact_destination.parent.mkdir(parents=True)
            shutil.copytree(source, exact_destination)
            manifest = {"schema": cli.MANAGED_INSTALL_SCHEMA, "resources": {}}
            parts = ("skills", skill)
            self.assertEqual(
                cli._install_managed_resource(
                    parts, exact_destination, manifest, is_tree=True,
                    dry_run=False, refresh=True,
                ),
                "unchanged",
            )
            self.assertIn(cli._managed_destination_key(exact_destination), manifest["resources"])

            modified_destination = root / "modified" / skill
            modified_destination.parent.mkdir(parents=True)
            shutil.copytree(source, modified_destination)
            skill_md = modified_destination / "SKILL.md"
            skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
            before = skill_md.read_bytes()
            untracked_manifest = {"schema": cli.MANAGED_INSTALL_SCHEMA, "resources": {}}
            self.assertEqual(
                cli._install_managed_resource(
                    parts, modified_destination, untracked_manifest, is_tree=True,
                    dry_run=False, refresh=True,
                ),
                "preserved",
            )
            self.assertEqual(skill_md.read_bytes(), before)
            self.assertEqual(untracked_manifest["resources"], {})

    def test_recorded_assets_refresh_once_and_dry_run_never_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package" / "profile.md"
            source.parent.mkdir()
            source.write_text("new packaged profile\n", encoding="utf-8")
            destination = root / "home" / ".codex/agents/profile.md"
            destination.parent.mkdir(parents=True)
            old_bytes = b"previous bundled profile\n"
            destination.write_bytes(old_bytes)
            parts = ("synthetic", "profile.md")
            manifest = {
                "schema": cli.MANAGED_INSTALL_SCHEMA,
                "resources": {
                    cli._managed_destination_key(destination): {
                        "resource": list(parts), "kind": "file",
                        "sha256": hashlib.sha256(old_bytes).hexdigest(),
                    }
                },
            }
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}), \
                    mock.patch.object(cli.packaged_resources, "item", return_value=source):
                self.assertEqual(
                    cli._install_managed_resource(
                        parts, destination, manifest, is_tree=False,
                        dry_run=True, refresh=True,
                    ),
                    "would-refresh",
                )
                self.assertEqual(destination.read_bytes(), old_bytes)
                self.assertEqual(
                    manifest["resources"][cli._managed_destination_key(destination)]["sha256"],
                    hashlib.sha256(old_bytes).hexdigest(),
                )
                self.assertFalse((root / "state/agentflow/backups").exists())

                self.assertEqual(
                    cli._install_managed_resource(
                        parts, destination, manifest, is_tree=False,
                        dry_run=False, refresh=True,
                    ),
                    "updated",
                )
                self.assertEqual(destination.read_text(encoding="utf-8"), "new packaged profile\n")
                backups = list((root / "state/agentflow/backups").rglob("profile.md"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), old_bytes)
                self.assertEqual(
                    cli._install_managed_resource(
                        parts, destination, manifest, is_tree=False,
                        dry_run=False, refresh=True,
                    ),
                    "unchanged",
                )
                self.assertEqual(len(list((root / "state/agentflow/backups").rglob("profile.md"))), 1)

    def test_recorded_skill_tree_can_refresh_but_backup_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package/skill"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text("new bundled skill\n", encoding="utf-8")
            destination = root / "home/.agents/skills/example-skill"
            destination.mkdir(parents=True)
            old_skill = destination / "SKILL.md"
            old_skill.write_text("previous bundled skill\n", encoding="utf-8")
            old_skill.chmod(0o755)
            executable_digest = cli.installation_backend._tree_digest(destination)
            old_skill.chmod(0o600)
            self.assertEqual(
                cli.installation_backend._tree_digest(destination), executable_digest
            )
            parts = ("synthetic", "example-skill")
            manifest = {
                "schema": cli.MANAGED_INSTALL_SCHEMA,
                "resources": {
                    cli._managed_destination_key(destination): {
                        "resource": list(parts), "kind": "tree",
                        "sha256": cli.installation_backend._tree_digest(destination),
                    }
                },
            }
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}), \
                    mock.patch.object(cli.packaged_resources, "item", return_value=package):
                self.assertEqual(
                    cli._install_managed_resource(
                        parts, destination, manifest, is_tree=True,
                        dry_run=False, refresh=True,
                    ),
                    "refreshed",
                )
                self.assertEqual((destination / "SKILL.md").read_text(encoding="utf-8"), "new bundled skill\n")
                backups = list((root / "state/agentflow/backups").rglob("example-skill"))
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / "SKILL.md").read_text(encoding="utf-8"), "previous bundled skill\n")

    def test_owned_tree_refresh_refuses_file_directory_and_dangling_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parts = ("synthetic", "example-skill")
            old_contents = {
                "SKILL.md": b"old bundled skill\n",
                "nested/data.txt": b"old nested data\n",
            }
            new_contents = {
                "SKILL.md": b"new bundled skill\n",
                "nested/data.txt": b"new nested data\n",
            }

            for link_kind in ("file", "directory", "dangling"):
                with self.subTest(link_kind=link_kind):
                    case_root = root / link_kind
                    package_v1 = case_root / "package-v1/skill"
                    package_v2 = case_root / "package-v2/skill"
                    for package, contents in (
                        (package_v1, old_contents), (package_v2, new_contents)
                    ):
                        (package / "nested").mkdir(parents=True)
                        (package / "SKILL.md").write_bytes(contents["SKILL.md"])
                        (package / "nested/data.txt").write_bytes(contents["nested/data.txt"])

                    destination = case_root / "home/skill"
                    manifest = {"schema": cli.MANAGED_INSTALL_SCHEMA, "resources": {}}
                    with mock.patch.object(
                        cli.packaged_resources, "item", return_value=package_v1
                    ):
                        self.assertEqual(
                            cli._install_managed_resource(
                                parts, destination, manifest, is_tree=True,
                                dry_run=False, refresh=True,
                            ),
                            "installed",
                        )

                    link_target_root = case_root / "external"
                    (link_target_root / "nested").mkdir(parents=True)
                    (link_target_root / "SKILL.md").write_bytes(old_contents["SKILL.md"])
                    (link_target_root / "nested/data.txt").write_bytes(
                        old_contents["nested/data.txt"]
                    )
                    if link_kind == "file":
                        linked_path = destination / "SKILL.md"
                        linked_path.unlink()
                        linked_path.symlink_to(link_target_root / "SKILL.md")
                    elif link_kind == "directory":
                        linked_path = destination / "nested"
                        shutil.rmtree(linked_path)
                        linked_path.symlink_to(link_target_root / "nested", target_is_directory=True)
                    else:
                        linked_path = destination / "SKILL.md"
                        linked_path.unlink()
                        linked_path.symlink_to(case_root / "missing-target")
                    original_link_target = os.readlink(linked_path)

                    owned_record = dict(
                        manifest["resources"][cli._managed_destination_key(destination)]
                    )
                    with mock.patch.dict(
                        os.environ, {"XDG_STATE_HOME": str(case_root / "state")}
                    ), mock.patch.object(
                        cli.packaged_resources, "item", return_value=package_v2
                    ):
                        for dry_run in (True, False):
                            with self.subTest(dry_run=dry_run):
                                self.assertEqual(
                                    cli._install_managed_resource(
                                        parts, destination, manifest, is_tree=True,
                                        dry_run=dry_run, refresh=True,
                                    ),
                                    "refused",
                                )
                                self.assertTrue(linked_path.is_symlink())
                                self.assertEqual(
                                    os.readlink(linked_path),
                                    original_link_target,
                                )
                                self.assertEqual(
                                    manifest["resources"][cli._managed_destination_key(destination)],
                                    owned_record,
                                )
                        self.assertFalse((case_root / "state/agentflow/backups").exists())

    def test_tree_digest_supports_zip_importlib_traversables_without_symlink_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package_name = f"agentflow_zip_resources_{os.getpid()}_{time.time_ns()}"
            archive = root / "resources.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(f"{package_name}/__init__.py", "")
                bundle.writestr(f"{package_name}/tree/nested/data.txt", "packaged data\n")

            sys.path.insert(0, str(archive))
            try:
                package = importlib.import_module(package_name)
                traversable = importlib_resources.files(package).joinpath("tree")
                self.assertFalse(hasattr(traversable.joinpath("nested"), "lstat"))
                self.assertEqual(
                    cli.installation_backend._tree_contents(traversable),
                    {"nested/data.txt": b"packaged data\n"},
                )
            finally:
                sys.path.remove(str(archive))
                sys.modules.pop(package_name, None)

    def test_invalid_codex_json_is_refused_and_dry_run_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / ".codex/hooks.json"
            destination.parent.mkdir(parents=True)
            invalid = b'{"hooks": [}\n'
            destination.write_bytes(invalid)
            manifest = {"schema": cli.MANAGED_INSTALL_SCHEMA, "resources": {}}
            parts = ("templates", "user", "codex-hooks.json")
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                self.assertEqual(
                    cli._install_merged_codex_hooks(
                        destination, parts, manifest, dry_run=False, refresh=True
                    ),
                    "refused",
                )
                self.assertEqual(destination.read_bytes(), invalid)
                self.assertEqual(manifest["resources"], {})
                self.assertFalse((root / "state/agentflow/backups").exists())

                destination.write_text(json.dumps({"hooks": {"UserEvent": []}, "keep": 1}), encoding="utf-8")
                before = destination.read_bytes()
                self.assertEqual(
                    cli._install_merged_codex_hooks(
                        destination, parts, manifest, dry_run=True, refresh=True
                    ),
                    "would-refresh",
                )
                self.assertEqual(destination.read_bytes(), before)
                self.assertEqual(manifest["resources"], {})
                self.assertFalse((root / "state/agentflow/backups").exists())

    def test_install_writes_private_manifest_and_refreshes_mixed_codex_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            hook = home / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text(json.dumps({
                "customTopLevel": "retained",
                "hooks": {"UserEvent": [{"hooks": [{"command": "user handler"}]}]},
            }), encoding="utf-8")
            args = argparse.Namespace(
                path=str(workspace), force=False, dry_run=True, refresh_bundled=True,
            )
            with mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "xdg")}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                original_hook = hook.read_bytes()
                self.assertEqual(cli.install(args), 0)
                self.assertEqual(hook.read_bytes(), original_hook)
                self.assertFalse((home / ".agents/skills").exists())
                self.assertFalse((root / "xdg/agentflow/install/managed-assets.json").exists())

                args.dry_run = False
                self.assertEqual(cli.install(args), 0)
                manifest_path = root / "xdg/agentflow/install/managed-assets.json"
                self.assertTrue(manifest_path.is_file())
                self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
                first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertGreater(len(first_manifest["resources"]), 1)
                merged = json.loads(hook.read_text(encoding="utf-8"))
                self.assertEqual(merged["customTopLevel"], "retained")
                self.assertEqual(merged["hooks"]["UserEvent"][0]["hooks"][0]["command"], "user handler")
                self.assertIn("SessionStart", merged["hooks"])
                self.assertEqual(cli.install(args), 0)
                self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), first_manifest)


class ControllerDocumentationTests(unittest.TestCase):
    def test_documented_no_ready_recovery_example_parses_without_execution(self) -> None:
        document = (Path(__file__).resolve().parents[1] / "docs/CONTROLLER.md").read_text(
            encoding="utf-8"
        )
        shell_blocks = re.findall(r"```(?:sh|shell)\s*\n(.*?)\n```", document, re.DOTALL)
        command = next(
            line.strip()
            for block in shell_blocks
            for line in block.splitlines()
            if line.strip().startswith("agentflow controller resume ")
        )
        args = cli.build_parser().parse_args(shlex.split(command)[1:])

        self.assertIs(args.func, cli.controller_resume)
        self.assertEqual(args.root, "/path/to/workspace")
        self.assertEqual(args.workflow_root, "ROOT")
        self.assertEqual(args.controller, "agentflow-controller")
        self.assertTrue(args.acknowledge_no_ready_halt)
        self.assertFalse(args.takeover)
        self.assertEqual(args.resume_token, "")
        self.assertEqual(args.resume_key_file, "")


class IsolationCliTests(unittest.TestCase):
    def test_isolation_probe_fails_closed_when_unsupported(self) -> None:
        with mock.patch.object(cli.isolation_backend, "platform_supported", return_value=False), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            result = cli.isolation_probe(argparse.Namespace(read=[], write=[], allow_network=False))
        self.assertEqual(result, 2)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["supported"])
        self.assertFalse(report["ok"])

    def test_isolation_launch_refuses_unsupported_platform(self) -> None:
        with mock.patch.object(cli.isolation_backend, "platform_supported", return_value=False):
            result = cli.isolation_launch(
                argparse.Namespace(
                    read=[], write=[], allow_network=False, timeout=5.0, cwd="", argv=["/bin/echo", "hi"]
                )
            )
        self.assertEqual(result, 2)

    def test_isolation_launch_requires_a_command(self) -> None:
        result = cli.isolation_launch(
            argparse.Namespace(read=[], write=[], allow_network=False, timeout=5.0, cwd="", argv=[])
        )
        self.assertEqual(result, 2)


class AssetsCliTests(unittest.TestCase):
    def test_lock_verify_install_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets_root = root / "assets"
            asset_dir = assets_root / "example-skill"
            asset_dir.mkdir(parents=True)
            (asset_dir / "SKILL.md").write_text("---\nname: example-skill\n---\nbody\n", encoding="utf-8")
            lock_file = root / "lock.json"

            lock_result = cli.assets_lock(
                argparse.Namespace(
                    lock_file=str(lock_file), name="example-skill", kind="skill", path=str(asset_dir),
                    source="https://example.test/example-skill", revision="1", entrypoint="SKILL.md",
                    reviewer="alice", capability=["read"],
                )
            )
            self.assertEqual(lock_result, 0)

            verify_result = cli.assets_verify(
                argparse.Namespace(
                    lock_file=str(lock_file), assets_root=str(assets_root), quarantine_root="",
                    no_quarantine=False,
                )
            )
            self.assertEqual(verify_result, 0)

            install_root = root / "install"
            install_result = cli.assets_install(
                argparse.Namespace(
                    name="example-skill", lock_file=str(lock_file), assets_root=str(assets_root),
                    install_root=str(install_root), dry_run=False,
                )
            )
            self.assertEqual(install_result, 0)
            self.assertTrue((install_root / "example-skill").is_symlink())

    def test_verify_rejects_tampered_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets_root = root / "assets"
            asset_dir = assets_root / "example-skill"
            asset_dir.mkdir(parents=True)
            (asset_dir / "SKILL.md").write_text("original\n", encoding="utf-8")
            lock_file = root / "lock.json"
            cli.assets_lock(
                argparse.Namespace(
                    lock_file=str(lock_file), name="example-skill", kind="skill", path=str(asset_dir),
                    source="s", revision="1", entrypoint="SKILL.md", reviewer="alice", capability=[],
                )
            )
            (asset_dir / "SKILL.md").write_text("tampered\n", encoding="utf-8")
            result = cli.assets_verify(
                argparse.Namespace(
                    lock_file=str(lock_file), assets_root=str(assets_root), quarantine_root="",
                    no_quarantine=False,
                )
            )
            self.assertEqual(result, 2)
            self.assertFalse(asset_dir.exists())  # quarantined, not left in place

    def test_install_rejects_unlocked_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets_root = root / "assets"
            (assets_root / "unlocked-skill").mkdir(parents=True)
            result = cli.assets_install(
                argparse.Namespace(
                    name="unlocked-skill", lock_file=str(root / "lock.json"),
                    assets_root=str(assets_root), install_root=str(root / "install"), dry_run=False,
                )
            )
            self.assertEqual(result, 2)


class HandoffPreflightAssetAndIsolationGateTests(unittest.TestCase):
    def _handoff_args(self, root: Path, output: Path, context: Path, **overrides) -> argparse.Namespace:
        base = dict(
            to="claude",
            title="Bounded change",
            goal="Observable outcome",
            task_id="",
            task_class="implementation",
            lane="external",
            tool_profile="shell-write",
            output_boundary=str(root),
            require_tool=[],
            require_skill=[],
            allow_delegation=False,
            return_type="result",
            max_ai_credits=None,
            acceptance_matrix="",
            base="",
            dependency=[],
            done_when=["Do the thing"],
            context=[str(context)],
            constraint=[],
            check=[],
            budget=["20 minutes; one retry"],
            issue="",
            branch="",
            out=str(output),
            cwd=str(root),
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_preflight_blocks_on_unlocked_required_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context.md"
            context.write_text("context\n", encoding="utf-8")
            output = root / "handoff.md"
            self.assertEqual(
                cli.handoff_create(
                    self._handoff_args(root, output, context, require_asset=["missing-skill"])
                ),
                0,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                )
            self.assertEqual(result, 2)

    def test_preflight_passes_when_required_asset_is_locked_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context.md"
            context.write_text("context\n", encoding="utf-8")
            assets_root = root / ".agentflow/assets"
            asset_dir = assets_root / "trusted-skill"
            asset_dir.mkdir(parents=True)
            (asset_dir / "SKILL.md").write_text("body\n", encoding="utf-8")
            cli.assets_lock(
                argparse.Namespace(
                    lock_file=str(assets_root / "lock.json"), name="trusted-skill", kind="skill",
                    path=str(asset_dir), source="s", revision="1", entrypoint="SKILL.md",
                    reviewer="alice", capability=[],
                )
            )
            output = root / "handoff.md"
            self.assertEqual(
                cli.handoff_create(
                    self._handoff_args(root, output, context, require_asset=["trusted-skill"])
                ),
                0,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                )
            self.assertEqual(result, 0)

    def test_preflight_blocks_when_hardened_isolation_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context.md"
            context.write_text("context\n", encoding="utf-8")
            output = root / "handoff.md"
            self.assertEqual(
                cli.handoff_create(
                    self._handoff_args(root, output, context, isolation_profile="hardened")
                ),
                0,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), mock.patch.object(
                cli.isolation_backend, "probe", return_value={"supported": False, "ok": False, "controls": {}}
            ):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                )
            self.assertEqual(result, 2)

    def test_preflight_passes_when_hardened_isolation_probe_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context.md"
            context.write_text("context\n", encoding="utf-8")
            output = root / "handoff.md"
            self.assertEqual(
                cli.handoff_create(
                    self._handoff_args(root, output, context, isolation_profile="hardened")
                ),
                0,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), mock.patch.object(
                cli.isolation_backend,
                "probe",
                return_value={"supported": True, "ok": True, "controls": {"network_denied": "pass"}},
            ):
                result = cli.handoff_preflight(
                    argparse.Namespace(file=str(output), cwd=str(root), require_matrix=False)
                )
            self.assertEqual(result, 0)


class ControllerParallelDispatchTests(unittest.TestCase):
    """The root scheduler admits a bounded launch wave before polling results."""

    def test_dispatch_uses_normalized_history_for_per_task_retry_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            task = dict(fixture.task_issue)
            state_path = fixture.root / ".agentflow/herdr/sessions.json"
            cli._private_atomic_json(state_path, {
                "schema": "agentflow.herdr", "version": 1,
                "sessions": {
                    str(task["id"]): {
                        "root": str(fixture.root),
                        "workflow_root": fixture.workflow_root,
                        "task_id": str(task["id"]),
                        "launch_id": "launch-two",
                        "status": "completed", "model": fixture.model,
                        "role": fixture.role,
                        # Top-level attempt is absent, but exact history proves
                        # the task has already spent both allowed launches.
                        "attempts": [
                            {"attempt": 1, "launch_id": "launch-one", "status": "failed"},
                            {"attempt": 2, "launch_id": "launch-two", "status": "failed"},
                        ],
                    },
                },
            })
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller,
                state_path=cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json",
            )
            lease = controller.acquire()
            policy = cli.execution_backend.ExecutionPolicy(
                max_parallel_workers=2, max_attempts_per_task=2,
                launch_budget_multiplier=10, max_expensive_execution_children=0,
            )
            args = argparse.Namespace(workflow_root=fixture.workflow_root)
            handoff = cli.provider_argv_backend.ConfinedHandoff(
                path=fixture.root / ".agentflow/tmp/handoffs/synthetic.md",
                manifest={
                    "context": [], "required_tools": [],
                    "output_boundary": str(fixture.root),
                    "machine_return_contract": {"acceptance_ids": ["R1"]},
                },
                content_sha256="a" * 64,
                manifest_sha256="b" * 64,
                preflight_sha256="c" * 64,
            )

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[task] * 10), \
                 mock.patch.object(cli.beads_backend, "add_comment") as add_comment, \
                 mock.patch.object(cli, "_controller_execution_policy", return_value=policy), \
                 mock.patch.object(cli, "_materialize_launch_handoff", return_value=handoff.path), \
                 mock.patch.object(cli.provider_argv_backend, "validate_confined_handoff", return_value=handoff), \
                 mock.patch.object(cli, "_run_actual_root_preflight", return_value=({}, "d" * 64)), \
                 mock.patch.object(cli, "herdr_launch") as launch:
                dispatch = cli._dispatch_via_herdr(
                    args, fixture.root, fixture.root, fixture.workflow_root, lease,
                )
                result = dispatch(task)

            self.assertEqual(result["state"], "blocked", result)
            add_comment.assert_called_once()
            self.assertIn("task attempt 3 exceeds limit 2", add_comment.call_args.args[2])
            launch.assert_not_called()

    def test_accounting_key_lookup_rejects_workspace_local_state_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            local_state_home = workspace / ".agentflow/controller-state"
            local_state_home.mkdir(parents=True)
            linked_state_home = base / "state-home-link"
            linked_state_home.symlink_to(local_state_home, target_is_directory=True)

            for state_home in (local_state_home, linked_state_home):
                with self.subTest(state_home=state_home.name), mock.patch.dict(
                    os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}
                ):
                    with self.assertRaisesRegex(
                        cli.controller_backend.ControllerError,
                        "outside the worker workspace",
                    ):
                        cli._accounting_authority_secret(
                            workspace, "wf-root", continuity_id="old-incarnation",
                            authority_key_id="a" * 24,
                        )

    def test_dispatch_accounts_signed_sessions_by_workflow_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            root_a = "root-a"
            root_b = "root-b"
            fixture.workflow_root = root_b
            fixture.root_issue["id"] = root_b
            task = json.loads(json.dumps(fixture.task_issue))
            task.update(id="task-b-new", parent=root_b)
            task["metadata"]["agentflow"].update(root=root_b, task="task-b-new")
            previous = json.loads(json.dumps(task))
            previous.update(id="task-b-prior", parent=root_b)
            previous["metadata"]["agentflow"].update(root=root_b, task="task-b-prior")

            def make_lease(workflow_root: str):
                state_path = cli._controller_state_dir(fixture.root, workflow_root) / "state.json"
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller, state_path=state_path,
                )
                lease = controller.acquire()
                _, credentials = cli._controller_credentials(
                    argparse.Namespace(
                        root=str(fixture.root), workflow_root=workflow_root,
                        resume_key_file="",
                    ),
                    lease,
                )
                return controller, lease, credentials["authority_secret"]

            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                controller_a, lease_a, secret_a = make_lease(root_a)
                _controller_b, lease_b, secret_b = make_lease(root_b)
                handoff = cli.provider_argv_backend.ConfinedHandoff(
                    path=fixture.root / ".agentflow/tmp/handoffs/synthetic.md",
                    manifest={
                        "context": [], "required_tools": [],
                        "output_boundary": str(fixture.root),
                        "machine_return_contract": {"acceptance_ids": ["R1"]},
                    },
                    content_sha256="a" * 64,
                    manifest_sha256="b" * 64,
                    preflight_sha256="c" * 64,
                )

                def signed_record(workflow_root, lease, authority_secret, task_id, launches):
                    current_launch = launches[-1]
                    minted = cli._mint_return_channel(
                        fixture.root, workflow_root,
                        task_id=task_id, actor=fixture.controller,
                        claim_token="opaque-claim-token-0123456789abcdef0123456789ab",
                        lease_id=f"lease-{workflow_root}", launch_id=current_launch,
                        provider="claude", model="claude-sonnet-5", effort="medium",
                        handoff=handoff, acceptance_ids=("R1",),
                        state_path=cli._controller_state_dir(fixture.root, workflow_root) / "state.json",
                        controller_id=lease.controller, lease_epoch=lease.epoch,
                        continuity_id=lease.continuity_id, authority_secret=authority_secret,
                    )
                    events = [
                        {"attempt": index, "launch_id": launch, "status": "launched"}
                        for index, launch in enumerate(launches, start=1)
                    ]
                    return {
                        "root": str(fixture.root), "workflow_root": workflow_root,
                        "task_id": task_id, "launch_id": current_launch,
                        "attempt": len(launches), "attempts": events,
                        "status": "completed", "model": "claude-sonnet-5", "role": "coding",
                        "return_channel": {
                            "state": "consumed",
                            "contract_path": str(minted["contract_path"]),
                            "capability_file": str(minted["capability_path"]),
                            "contract_sha256": minted["contract_sha256"],
                            "contract_binding": minted["contract"],
                            "acceptance_ids": ["R1"], "approved_waivers": [],
                        },
                    }

                old_root_records = {
                    "task-a-one": signed_record(root_a, lease_a, secret_a, "task-a-one", ["a-1"]),
                    "task-a-two": signed_record(root_a, lease_a, secret_a, "task-a-two", ["a-2"]),
                }
                # Authenticated reattach preserves this incarnation. An
                # explicit takeover then starts a fresh one; old snapshots
                # remain verifiable for accounting without live authority.
                controller_a.release(lease_a)
                reattached_lease_a = controller_a.acquire(resume_proof=lease_a.resume_secret)
                _, reattached_credentials_a = cli._controller_credentials(
                    argparse.Namespace(
                        root=str(fixture.root), workflow_root=root_a,
                        resume_key_file="",
                    ),
                    reattached_lease_a,
                )
                self.assertEqual(reattached_lease_a.continuity_id, lease_a.continuity_id)
                self.assertEqual(secret_a, reattached_credentials_a["authority_secret"])
                controller_a.release(reattached_lease_a)
                renewed_lease_a = controller_a.acquire(takeover=True)
                _, renewed_credentials_a = cli._controller_credentials(
                    argparse.Namespace(
                        root=str(fixture.root), workflow_root=root_a,
                        resume_key_file="",
                    ),
                    renewed_lease_a,
                )
                self.assertNotEqual(secret_a, renewed_credentials_a["authority_secret"])
                archive_path = cli._accounting_key_archive_path(
                    cli._resume_key_path(argparse.Namespace(
                        root=str(fixture.root), workflow_root=root_a,
                        resume_key_file="",
                    )),
                    lease_a.continuity_id,
                )
                self.assertEqual(archive_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    cli._accounting_authority_secret(
                        fixture.root, root_a,
                        continuity_id=lease_a.continuity_id,
                        authority_key_id=cli._credential_key_id(secret_a),
                    ),
                    secret_a,
                )
                with self.assertRaises(cli.controller_backend.ControllerError):
                    cli._authority_secret(
                        fixture.root, root_a, continuity_id=lease_a.continuity_id,
                    )

                state_path = fixture.root / ".agentflow/herdr/sessions.json"
                cli._private_atomic_json(state_path, {
                    "schema": "agentflow.herdr", "version": 1,
                    "sessions": old_root_records,
                })

                args = argparse.Namespace(
                    workflow_root=root_b, _authority_secret=secret_b,
                )
                policy = cli.execution_backend.ExecutionPolicy(
                    max_parallel_workers=2, max_attempts_per_task=2,
                    launch_budget_multiplier=1, max_expensive_execution_children=0,
                )
                bead_issues = {root_b: fixture.root_issue, str(task["id"]): task}

                def get_issue(_cwd, issue_id):
                    return bead_issues.get(issue_id, task)

                with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[task, previous]), \
                     mock.patch.object(cli.beads_backend, "add_comment"), \
                     mock.patch.object(cli, "_controller_execution_policy", return_value=policy), \
                     mock.patch.object(cli, "_materialize_launch_handoff", return_value=handoff.path), \
                     mock.patch.object(cli.provider_argv_backend, "validate_confined_handoff", return_value=handoff), \
                     mock.patch.object(cli, "_run_actual_root_preflight", return_value=({}, "d" * 64)), \
                     mock.patch.object(cli, "herdr_launch", return_value=0) as launch:
                    dispatch = cli._dispatch_via_herdr(
                        args, fixture.root, fixture.root, root_b, lease_b,
                    )
                    admitted = dispatch(task)
                    self.assertEqual(admitted["state"], "running", admitted)
                    launch.assert_called_once()

                    conflicting_ids = dict(old_root_records)
                    conflicting_record = signed_record(
                        root_b, lease_b, secret_b, "task-b-conflicting-ids", ["launch-two"],
                    )
                    conflicting_record["attempts"] = [
                        {"attempt": 1, "launch_id": "launch-one", "status": "identity_pending"},
                        {"attempt": 1, "launch_id": "launch-two", "status": "launched",
                         "resolved_from": "identity_pending"},
                    ]
                    conflicting_ids["task-b-conflicting-ids"] = conflicting_record
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": conflicting_ids,
                    })
                    conflicting_result = dispatch(task)

                    # The archived key can authenticate the old snapshot for
                    # accounting only. Result ingestion still requires the
                    # current controller incarnation and rejects old output.
                    stale_result_record = json.loads(json.dumps(old_root_records["task-a-one"]))
                    stale_result_record["return_channel"]["state"] = "issued"
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": {"task-a-one": stale_result_record},
                    })
                    stale_result_path = Path(
                        stale_result_record["return_channel"]["contract_binding"]["result_path"]
                    )
                    stale_result_path.write_text(json.dumps({
                        "outcome": "completed",
                        "acceptance_results": [{
                            "acceptance_id": "R1", "status": "passed",
                            "evidence": "synthetic stale result", "source": "provider",
                        }],
                    }), encoding="utf-8")
                    stale_channel = stale_result_record["return_channel"]
                    stale_args = argparse.Namespace(
                        root=str(fixture.root), contract=stale_channel["contract_path"],
                        file=str(stale_result_path), json=True,
                        _controller_ingest=True,
                        _capability_file=stale_channel["capability_file"],
                        _authority_secret=secret_a,
                    )
                    stale_payloads: list[dict] = []
                    with fixture.beads_patches(), mock.patch.object(
                        cli, "_json_or_status",
                        side_effect=lambda payload, **_kwargs: stale_payloads.append(payload),
                    ):
                        self.assertEqual(cli.herdr_result(stale_args), 2)
                    self.assertIn("superseded controller incarnation", stale_payloads[-1]["error"])

                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": old_root_records,
                    })

                    own_root_at_cap = dict(old_root_records)
                    own_root_at_cap["task-b-prior"] = signed_record(
                        root_b, lease_b, secret_b, "task-b-prior", ["b-1", "b-2"],
                    )
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": own_root_at_cap,
                    })
                    blocked = dispatch(task)
                    self.assertEqual(blocked["state"], "blocked")

                    corrupt_history = dict(old_root_records)
                    corrupt_record = signed_record(
                        root_b, lease_b, secret_b, "task-b-corrupt", ["b-corrupt"],
                    )
                    corrupt_record["attempts"] = "corrupt"
                    corrupt_history["task-b-corrupt"] = corrupt_record
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": corrupt_history,
                    })
                    corrupt_result = dispatch(task)

                    tampered = json.loads(json.dumps(old_root_records))
                    tampered["task-a-one"]["return_channel"]["contract_binding"]["workflow_root"] = root_b
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": tampered,
                    })
                    tampered_result = dispatch(task)

                    unknown_ancestry = dict(old_root_records)
                    unknown_ancestry["legacy-unknown"] = {
                        "task_id": "legacy-unknown", "root": str(fixture.root),
                        "attempt": 1, "attempts": [], "status": "completed",
                    }
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": unknown_ancestry,
                    })
                    unknown_result = dispatch(task)

                    legacy_task = {
                        "id": "legacy-task", "parent": "phase",
                        "status": "closed", "metadata": {},
                    }
                    bead_issues.update({
                        "legacy-task": legacy_task,
                        "phase": {"id": "phase", "parent": root_b},
                    })
                    # The unsigned field names a valid intermediate ancestor.
                    # Exact ancestry still proves this spend belongs beneath
                    # root B, so the retained root cap must include it.
                    cli._private_atomic_json(state_path, {
                        "schema": "agentflow.herdr", "version": 1,
                        "sessions": {
                            "legacy-task": {
                                "root": str(fixture.root),
                                "workflow_root": "phase",
                                "task_id": "legacy-task", "attempt": 2,
                                "attempts": [], "status": "completed",
                                "model": "claude-sonnet-5", "role": "coding",
                            },
                        },
                    })
                    legacy_at_cap = dispatch(task)

            self.assertTrue(corrupt_result.get("accounting_indeterminate"), corrupt_result)
            self.assertTrue(conflicting_result.get("accounting_indeterminate"), conflicting_result)
            self.assertTrue(tampered_result.get("accounting_indeterminate"), tampered_result)
            self.assertTrue(unknown_result.get("accounting_indeterminate"), unknown_result)
            self.assertEqual(legacy_at_cap["state"], "blocked", legacy_at_cap)
            launch.assert_called_once()

    def test_codex_trust_prompt_pauses_parallel_controller_without_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            task_id = str(fixture.task_issue["id"])
            task = dict(fixture.task_issue, status="in_progress")
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            controller.reserve_active_task({
                "task": task_id, "root": str(fixture.root), "actor": fixture.controller,
                "claim_id": "claim-task-1",
            }, lease=lease)
            controller.bind_active_task(task_id, session_id="", state="identity_pending", lease=lease)
            args = argparse.Namespace(workflow_root=fixture.workflow_root, _authority_secret="test-secret")
            attention = {
                "code": "codex_project_trust_required", "task_id": task_id,
                "pane_id": "w1:p1", "path": str(fixture.root),
            }
            record = {
                "status": "identity_pending", "pane_id": "w1:p1",
                "identity_pending_since": cli._now(),
            }
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=lambda _cwd, issue_id:
                                   fixture.root_issue if issue_id == fixture.workflow_root else task), \
                 mock.patch.object(cli, "_herdr_session_record", return_value=record), \
                 mock.patch.object(cli, "_resolve_pending_identity"), \
                 mock.patch.object(cli, "_codex_trust_attention", return_value=attention), \
                 mock.patch.object(cli, "_ingest_submitted_result") as ingest, \
                 mock.patch.object(cli.beads_backend, "claim_ready") as claim:
                payload, stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )
            self.assertTrue(stop)
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertEqual(payload["action_required"], attention)
            self.assertEqual(controller.active_tasks()[0]["state"], "identity_pending")
            self.assertFalse(payload["result"]["terminal"])
            ingest.assert_not_called()
            claim.assert_not_called()

    def test_authenticated_resume_finalizes_empty_drain_without_claiming_ready_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            key_file = base / "resume.key"
            fixture.root_issue["metadata"]["agentflow"]["execution"] = {
                "schema": "agentflow.execution-policy@1", "controller_only": True,
                "max_parallel_workers": 1, "max_delegation_depth": 1,
                "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                "max_expensive_execution_children": 0,
            }
            args = _controller_args(
                fixture.root, workflow_root=fixture.workflow_root,
                resume_key_file=str(key_file),
            )
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}):
                controller, _ = cli._controller_instance(args)
                lease = controller.acquire()
                cli._controller_credentials(args, lease)
                controller.reserve_active_task({
                    "task": "failed-task", "root": str(fixture.root),
                    "actor": fixture.controller, "claim_id": "claim-failed",
                }, lease=lease)
                controller.bind_active_task(
                    "failed-task", session_id="session-failed", lease=lease,
                )
                controller.begin_draining(
                    "USER_ACTION_REQUIRED: failed-task exited non-zero",
                    failed_task="failed-task", lease=lease,
                )
                cutpoint = controller.mark_incomplete(lease=lease)
                self.assertEqual(cutpoint.checkpoint["state"], "draining")
                self.assertEqual(cutpoint.checkpoint["status"], "incomplete")
                self.assertEqual(cutpoint.checkpoint["active_tasks"], [])

                claims: list[str] = []

                def claim_ready(_cwd, *, parent, labels, actor):
                    claims.append(actor)
                    fixture.task_issue["status"] = "in_progress"
                    return fixture.task_issue

                payload = None
                with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                     mock.patch.object(cli.beads_backend, "claim_ready", side_effect=claim_ready), \
                     mock.patch.object(cli, "_bind_current_controller_sessions"), \
                     mock.patch.object(cli, "_dispatch_via_herdr") as dispatch_builder, \
                     mock.patch.object(cli, "_provider_command") as provider_command:
                    payload = _run_controller_json(cli.controller_resume, args)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["result"]["state"], "blocked")
            self.assertTrue(payload["result"]["terminal"])
            self.assertEqual(payload["result"]["checkpoint"]["active_tasks"], [])
            self.assertIn("failed-task exited non-zero", payload["result"]["checkpoint"]["terminal_reason"])
            self.assertEqual(claims, [])
            dispatch_builder.assert_not_called()
            provider_command.assert_not_called()

    def test_invalid_acceptance_result_is_tracked_until_herdr_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            task = dict(fixture.task_issue)
            task["status"] = "in_progress"
            task_id = str(task["id"])
            acceptance_ids = cli._bound_acceptance_ids(task, task_id)
            herdr_status = {task_id: "running"}
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            controller.reserve_active_task({
                "task": task_id, "root": str(fixture.root), "actor": fixture.controller,
                "claim_id": "claim-task-1",
            }, lease=lease)
            controller.bind_active_task(task_id, session_id="session-task-1", lease=lease)
            args = argparse.Namespace(workflow_root=fixture.workflow_root, _authority_secret="test-secret")
            claim_ready = mock.Mock()

            def session_record(_root, _task_id):
                return {
                    "status": herdr_status[task_id],
                    "return_channel": {
                        "state": "consumed", "acceptance_ids": list(acceptance_ids),
                        "approved_waivers": [],
                    },
                }

            invalid_result = {
                "outcome": "completed",
                "acceptance_results": [{
                    "acceptance_id": acceptance_ids[0], "status": "failed",
                    "evidence": "validation did not pass", "source": "provider",
                }],
            }
            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=lambda _cwd, issue_id:
                                   fixture.root_issue if issue_id == fixture.workflow_root else task), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[task]), \
                 mock.patch.object(cli, "_herdr_session_record", side_effect=session_record), \
                 mock.patch.object(cli, "_ingest_submitted_result",
                                   return_value=cli._SubmissionIngestion("consumed")), \
                 mock.patch.object(cli, "_herdr_task_result", return_value=invalid_result), \
                 mock.patch.object(cli.beads_backend, "add_comment"), \
                 mock.patch.object(cli.beads_backend, "claim_ready", claim_ready):
                first_payload, first_stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )
                self.assertFalse(first_stop)
                self.assertEqual(first_payload["result"]["state"], "draining")
                self.assertEqual([row["task"] for row in controller.active_tasks()], [task_id])
                self.assertEqual(controller.active_tasks()[0]["state"], "running")

                herdr_status[task_id] = "completed"
                final_payload, final_stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )

            self.assertTrue(final_stop)
            self.assertEqual(final_payload["stop_reason"], "TASK_BLOCKED")
            self.assertEqual(final_payload["result"]["state"], "blocked")
            self.assertTrue(final_payload["result"]["terminal"])
            self.assertIn("acceptance disposition rejected", final_payload["result"]["checkpoint"]["terminal_reason"])
            self.assertEqual(final_payload["result"]["checkpoint"]["active_tasks"], [])
            claim_ready.assert_not_called()

    def test_failed_parallel_task_drains_sibling_before_terminalizing_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            first = dict(fixture.task_issue)
            second = json.loads(json.dumps(fixture.task_issue))
            second.update(id="task-2", title="Second bounded task")
            second["metadata"]["agentflow"].update(
                task="task-2", claim_id="claim-2",
                claim_token="opaque-claim-token-task-two-0123456789abcdef",
            )
            second["metadata"]["agentflow"]["acceptance"]["task_id"] = "task-2"
            tasks = {"task-1": first, "task-2": second}
            for task in tasks.values():
                task["status"] = "in_progress"
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            for task_id in tasks:
                controller.reserve_active_task({
                    "task": task_id, "root": str(fixture.root), "actor": fixture.controller,
                    "claim_id": f"claim-{task_id}",
                }, lease=lease)
                controller.bind_active_task(
                    task_id, session_id=f"session-{task_id}", lease=lease,
                )
            args = argparse.Namespace(workflow_root=fixture.workflow_root, _authority_secret="test-secret")
            first_ids = cli._bound_acceptance_ids(first, "task-1")
            second_ids = cli._bound_acceptance_ids(second, "task-2")
            failure_reason = {"task-1": "provider exited non-zero"}
            result_state = {"task-2": None}
            comments: list[tuple[str, str]] = []
            closed: list[str] = []
            claim_ready = mock.Mock()

            def session_record(_root, task_id):
                ids = first_ids if task_id == "task-1" else second_ids
                status = "failed" if task_id == "task-1" else "completed" if result_state[task_id] else "launched"
                return {"status": status, "return_channel": {
                    "state": "consumed", "acceptance_ids": list(ids), "approved_waivers": [],
                }}

            def task_result(_root, task_id):
                if task_id == "task-1":
                    return {"outcome": "failed", "error": failure_reason[task_id]}
                return result_state[task_id]

            def close_issue(_cwd, task_id, _reason):
                tasks[task_id]["status"] = "closed"
                closed.append(task_id)

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=lambda _cwd, issue_id:
                                   fixture.root_issue if issue_id == fixture.workflow_root else tasks[issue_id]), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=list(tasks.values())), \
                 mock.patch.object(cli, "_herdr_session_record", side_effect=session_record), \
                 mock.patch.object(cli, "_ingest_submitted_result", return_value=cli._SubmissionIngestion("consumed")), \
                 mock.patch.object(cli, "_herdr_task_result", side_effect=task_result), \
                 mock.patch.object(cli.beads_backend, "add_comment", side_effect=lambda _cwd, task_id, text:
                                   comments.append((task_id, text))), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=close_issue), \
                 mock.patch.object(cli.beads_backend, "claim_ready", claim_ready):
                first_payload, first_stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )
                self.assertFalse(first_stop)
                self.assertEqual(first_payload["result"]["state"], "draining")
                self.assertEqual([item["task"] for item in controller.active_tasks()], ["task-2"])
                self.assertEqual(comments[0][0], "task-1")
                first_reason = first_payload["result"]["checkpoint"]["terminal_reason"]

                # The deadline status must coexist with the draining state
                # until live sibling results are reconciled, retaining the
                # original failure evidence used to block future claims.
                deadline_result = controller.mark_incomplete(lease=lease)
                self.assertEqual(deadline_result.state, "incomplete")
                deadline_checkpoint = controller._load_checkpoint()
                self.assertEqual(deadline_checkpoint["status"], "incomplete")
                self.assertEqual(deadline_checkpoint["state"], "draining")
                self.assertEqual(deadline_checkpoint["terminal_reason"], first_reason)

                # Simulate a crashed controller and a legitimate reattach;
                # the durable drain barrier must survive without reopening
                # task admission.
                resume_secret = lease.resume_secret
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller, state_path=state_path,
                    checkpoint_path=state_path.with_name("checkpoint.json"),
                )
                lease = controller.acquire(resume_proof=resume_secret)
                resumed_checkpoint = controller._load_checkpoint()
                self.assertEqual(resumed_checkpoint["status"], "incomplete")
                self.assertEqual(resumed_checkpoint["state"], "draining")

                result_state["task-2"] = {
                    "outcome": "completed",
                    "acceptance_results": [{
                        "acceptance_id": acceptance_id, "status": "passed",
                        "evidence": "verified sibling result", "source": "provider",
                    } for acceptance_id in second_ids],
                }
                final_payload, final_stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )

            self.assertTrue(final_stop)
            self.assertEqual(final_payload["stop_reason"], "TASK_BLOCKED")
            self.assertEqual(final_payload["result"]["state"], "blocked")
            self.assertTrue(final_payload["result"]["terminal"])
            self.assertIn("task-1", final_payload["result"]["checkpoint"]["terminal_reason"])
            self.assertIn("failed", final_payload["result"]["checkpoint"]["terminal_reason"])
            self.assertEqual(final_payload["result"]["checkpoint"]["terminal_reason"], first_reason)
            self.assertEqual(final_payload["result"]["checkpoint"]["active_tasks"], [])
            self.assertEqual(closed, ["task-2"])
            claim_ready.assert_not_called()

    def test_orphaned_launching_record_does_not_become_stuck_identity_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            orphan = dict(fixture.task_issue)
            orphan.update(status="in_progress", assignee=fixture.controller)
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            args = argparse.Namespace(workflow_root=fixture.workflow_root, _authority_secret="test-secret")
            payloads: list[dict] = []

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=lambda _cwd, issue_id:
                                   fixture.root_issue if issue_id == fixture.workflow_root else orphan), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[orphan]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready, \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_persist_claim_identity"), \
                 mock.patch.object(cli, "_herdr_session_record", return_value={
                     "status": "launching", "binding": None, "pane_id": "",
                 }), \
                 mock.patch.object(cli, "_resolve_pending_identity") as resolve_identity:
                payload, stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )
                payloads.append(payload)

            self.assertTrue(stop)
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertEqual(payload["result"]["state"], "draining")
            self.assertIn("launching", payload["result"]["checkpoint"]["terminal_reason"])
            self.assertEqual([item["state"] for item in controller.active_tasks()], ["claimed_no_session"])
            claim_ready.assert_not_called()
            resolve_identity.assert_not_called()

    def test_legacy_terminal_root_with_live_sibling_reopens_only_to_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            sibling = json.loads(json.dumps(fixture.task_issue))
            sibling.update(id="task-2", status="in_progress")
            sibling["metadata"]["agentflow"]["acceptance"]["task_id"] = "task-2"
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            controller.reserve_active_task({
                "task": "task-2", "root": str(fixture.root), "actor": fixture.controller,
                "claim_id": "claim-task-2",
            }, lease=lease)
            controller.bind_active_task("task-2", session_id="session-task-2", lease=lease)
            previous = controller._load_checkpoint()
            previous.update({
                "state": "blocked", "status": "blocked", "terminal": True,
                "terminal_reason": "task-1 failed before sibling task-2 completed",
            })
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, previous)
            args = argparse.Namespace(workflow_root=fixture.workflow_root, _authority_secret="test-secret")

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=lambda _cwd, issue_id:
                                   fixture.root_issue if issue_id == fixture.workflow_root else sibling), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[sibling]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready, \
                 mock.patch.object(cli, "_herdr_session_record", return_value={
                     "status": "launched", "return_channel": {"state": "issued"},
                 }), \
                 mock.patch.object(cli, "_ingest_submitted_result", return_value=cli._SubmissionIngestion("pending")), \
                 mock.patch.object(cli, "_herdr_task_result", return_value=None):
                payload, stop = cli._controller_step_parallel(
                    args, controller, fixture.root, lease, operation="resume",
                    policy=cli.execution_backend.ExecutionPolicy(max_parallel_workers=2),
                )

            self.assertFalse(stop)
            self.assertEqual(payload["result"]["state"], "draining")
            self.assertEqual(payload["stop_reason"], "DRAINING_AFTER_TASK_FAILURE")
            self.assertEqual(payload["result"]["checkpoint"]["terminal_reason"],
                             "task-1 failed before sibling task-2 completed")
            self.assertEqual([item["task"] for item in controller.active_tasks()], ["task-2"])
            claim_ready.assert_not_called()

    def test_checkpoint_capacity_is_enforced_before_beads_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["execution"] = {
                "schema": cli.execution_backend.SCHEMA, "controller_only": True,
                "max_parallel_workers": 9, "max_delegation_depth": 1,
                "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                "max_expensive_execution_children": 0,
            }
            state_path = fixture.root / ".agentflow/controller/state.json"
            controller = cli.controller_backend.RootController(
                str(fixture.root), fixture.controller, state_path=state_path,
                checkpoint_path=state_path.with_name("checkpoint.json"),
            )
            lease = controller.acquire()
            args = argparse.Namespace(workflow_root=fixture.workflow_root)
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=fixture.root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=None) as claim_ready:
                with self.assertRaisesRegex(cli.execution_backend.ExecutionPolicyError,
                                            "maximum supported parallel worker count"):
                    cli._controller_step(args, controller, fixture.root, lease, operation="resume")
            claim_ready.assert_not_called()

    def test_two_ready_tasks_launch_before_either_result_is_polled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            first = dict(fixture.task_issue)
            second = json.loads(json.dumps(fixture.task_issue))
            second.update(id="task-2", title="Second bounded task")
            second["metadata"]["agentflow"].update(
                task="task-2", claim_id="claim-2",
                claim_token="opaque-claim-token-task-two-0123456789abcdef",
            )
            second["metadata"]["agentflow"]["acceptance"]["task_id"] = "task-2"
            first["status"] = second["status"] = "open"
            tasks = {"task-1": first, "task-2": second}
            fixture.root_issue["metadata"]["agentflow"]["execution"] = {
                "schema": "agentflow.execution-policy@1", "controller_only": True,
                "max_parallel_workers": 2, "max_delegation_depth": 1,
                "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                "max_expensive_execution_children": 0,
            }
            ready = iter((first, second, None))
            events: list[str] = []
            payloads: list[dict] = []
            args = _controller_args(
                fixture.root, workflow_root=fixture.workflow_root, once=True
            )

            def fake_dispatch(_args, _root, _cwd, _workflow_root, _lease):
                def dispatch(selected):
                    task_id = str(selected.get("task") or selected.get("id"))
                    events.append(f"launch:{task_id}")
                    return {"state": "running", "session_id": f"session-{task_id}"}
                return dispatch

            def get_issue(_cwd, issue_id):
                return fixture.root_issue if issue_id == fixture.workflow_root else tasks[issue_id]

            def fake_claim_ready(*_args, **_kwargs):
                issue = next(ready)
                if issue is not None:
                    issue["status"] = "in_progress"
                return issue

            def task_result(*_args, **_kwargs):
                events.append("poll-result")
                return None

            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(fixture.root / "agentflow-state")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[first, second]), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=fake_claim_ready), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_persist_claim_identity"), \
                 mock.patch.object(cli, "_dispatch_via_herdr", side_effect=fake_dispatch), \
                 mock.patch.object(cli, "_herdr_task_result", side_effect=task_result), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                self.assertEqual(cli.controller_resume(args), 0, payloads)

            self.assertEqual(events, ["launch:task-1", "launch:task-2"])
            checkpoint = cli.checkpoint_backend.load_checkpoint(
                cli._controller_state_dir(fixture.root, fixture.workflow_root) / "checkpoint.json"
            )
            self.assertEqual(
                [item["task"] for item in checkpoint["active_tasks"]],
                ["task-1", "task-2"],
            )

    def test_crash_after_reservation_halts_without_duplicate_provider_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.root_issue["metadata"]["agentflow"]["execution"] = {
                "schema": "agentflow.execution-policy@1", "controller_only": True,
                "max_parallel_workers": 2, "max_delegation_depth": 1,
                "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                "max_expensive_execution_children": 0,
            }
            task = dict(fixture.task_issue)
            task["status"] = "in_progress"
            task["assignee"] = fixture.controller
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root, once=True)
            payloads: list[dict] = []
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(fixture.root / "state-home")}):
                state_path = cli._controller_state_dir(fixture.root, fixture.workflow_root) / "state.json"
                controller = cli.controller_backend.RootController(
                    str(fixture.root), fixture.controller, state_path=state_path,
                    checkpoint_path=state_path.with_name("checkpoint.json"),
                )
                lease = controller.acquire()
                cli._controller_credentials(args, lease)
                controller.reserve_active_task({
                    "task": "task-1", "root": str(fixture.root),
                    "actor": fixture.controller, "claim_id": "claim-1",
                }, lease=lease)
                self.assertEqual(controller.active_tasks()[0]["state"], "claimed_no_session")
                self.assertEqual(
                    cli._controller_execution_policy(
                        fixture.root, fixture.root, fixture.workflow_root,
                        root_issue=fixture.root_issue,
                    ).max_parallel_workers,
                    2,
                )
                with mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                     mock.patch.object(cli.beads_backend, "root_descendants", return_value=[task]), \
                     mock.patch.object(cli.beads_backend, "claim_ready") as claim_ready, \
                     mock.patch.object(cli, "_dispatch_via_herdr") as dispatch_builder, \
                     mock.patch.object(cli, "_json_or_status", side_effect=lambda value, **_kwargs: payloads.append(value)):
                    self.assertEqual(cli.controller_resume(args), 0, payloads)
            claim_ready.assert_not_called()
            dispatch_builder.assert_not_called()
            final = payloads[-1]
            self.assertEqual(final["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertEqual(final["result"]["checkpoint"]["active_tasks"][0]["state"], "claimed_no_session")

    def test_two_authenticated_results_are_dispositioned_out_of_launch_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            first = dict(fixture.task_issue)
            second = json.loads(json.dumps(fixture.task_issue))
            second.update(id="task-2", title="Second bounded task")
            second["metadata"]["agentflow"].update(
                task="task-2", claim_id="claim-2",
                claim_token="opaque-claim-token-task-two-0123456789abcdef",
            )
            second["metadata"]["agentflow"]["acceptance"]["task_id"] = "task-2"
            tasks = {"task-1": first, "task-2": second}
            fixture.root_issue["metadata"]["agentflow"].update({
                "execution": {
                    "schema": "agentflow.execution-policy@1", "controller_only": True,
                    "max_parallel_workers": 2, "max_delegation_depth": 1,
                    "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
                    "max_expensive_execution_children": 0,
                },
                "acceptance": {
                    "version": 1, "task_id": fixture.workflow_root,
                    "rows": [{"id": "ROOT", "outcome": "complete", "owner": "eng",
                              "lane": "static", "planned_evidence": "both tasks close",
                              "status": "passed", "actual_evidence": "both tasks closed"}],
                },
            })
            ready = iter((first, second, None))
            spawned: list[str] = []
            closed: list[str] = []
            task2_closed = threading.Event()
            provider_done = threading.Event()
            threads: list[threading.Thread] = []

            def get_issue(_cwd, issue_id):
                return fixture.root_issue if issue_id == fixture.workflow_root else tasks[issue_id]

            def root_descendants(_cwd, _root_id):
                return [first, second]

            def claim_ready(*_args, **_kwargs):
                issue = next(ready, None)
                if issue is not None:
                    issue["status"] = "in_progress"
                    issue["assignee"] = fixture.controller
                return issue

            def close_issue(_cwd, task_id, _reason):
                tasks[task_id]["status"] = "closed"
                closed.append(task_id)
                if task_id == "task-2":
                    task2_closed.set()

            def submit_result(task_id: str) -> None:
                state_path = fixture.root / ".agentflow/herdr/sessions.json"
                deadline = time.monotonic() + 6
                record = None
                while time.monotonic() < deadline:
                    try:
                        record = json.loads(state_path.read_text(encoding="utf-8"))["sessions"].get(task_id)
                        if isinstance(record, dict) and record.get("status") == "launched":
                            channel = record.get("return_channel") or {}
                            if channel.get("state") == "issued":
                                break
                    except (OSError, json.JSONDecodeError, KeyError):
                        pass
                    time.sleep(0.01)
                assert isinstance(record, dict), f"no Herdr launch record for {task_id}"
                channel = record["return_channel"]
                contract_path = Path(channel["contract_path"])
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                result_path = Path(channel["result_path"])
                result_path.write_text(json.dumps({
                    "outcome": "completed",
                    "acceptance_results": [{
                        "acceptance_id": acceptance_id, "status": "passed",
                        "evidence": f"verified result for {task_id}", "source": "provider",
                    } for acceptance_id in contract["acceptance_ids"]],
                }), encoding="utf-8")
                self.assertEqual(cli.herdr_submit(argparse.Namespace(
                    root=str(fixture.root), contract=str(contract_path),
                    file=str(result_path), json=True,
                )), 0)

            def result_order_worker() -> None:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and len(spawned) < 2:
                    time.sleep(0.005)
                assert len(spawned) == 2, "controller did not launch the two-task wave"
                submit_result("task-2")
                assert task2_closed.wait(timeout=6), "second task was not dispositioned first"
                submit_result("task-1")
                provider_done.set()

            real_subprocess_run = subprocess.run

            def fake_herdr_run(argv, **kwargs):
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["status", "server"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="status: running\ncompatible: yes\n", stderr="")
                if argv and Path(str(argv[0])).name == "herdr" and list(argv[1:]) == ["integration", "status"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="codex: current (v1)\n", stderr="")
                if argv and Path(str(argv[0])).name == "claude" and list(argv[1:]) == ["--version"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="2.1.281 (Claude Code)\n", stderr="")
                if argv and Path(str(argv[0])).name == "herdr" and "start" in argv:
                    task_id = str(argv[3])
                    session_id = f"session-{task_id}"
                    spawned.append(task_id)
                    cli.events_backend.record_event_safely(
                        cli.events_backend.EventSpool(cli._state_dir() / "events.jsonl"),
                        cli.events_backend.normalize_event(
                            "claude", {"event": "session.start", "session_id": session_id,
                                       "model": fixture.model},
                            event_id=f"parallel-model-{task_id}",
                        ),
                    )
                    if len(spawned) == 2:
                        thread = threading.Thread(target=result_order_worker, daemon=True)
                        thread.start()
                        threads.append(thread)
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=_agent_started_stdout("claude", session_id=session_id), stderr=""
                    )
                return real_subprocess_run(argv, **kwargs)

            args = _controller_args(
                fixture.root, workflow_root=fixture.workflow_root, once=False,
                poll_interval=0.01, deadline=12.0,
            )
            payloads: list[dict] = []
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state-home")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", side_effect=root_descendants), \
                 mock.patch.object(cli.beads_backend, "claim_ready", side_effect=claim_ready), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli.beads_backend, "close_issue", side_effect=close_issue), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fake_herdr_run), \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda value, **_kwargs: payloads.append(value)):
                self.assertEqual(cli.controller_resume(args), 0, payloads)
                for thread in threads:
                    thread.join(timeout=6)

            self.assertTrue(provider_done.is_set(), payloads)
            self.assertEqual(spawned, ["task-1", "task-2"])
            self.assertEqual(closed, ["task-2", "task-1"])
            final = [item for item in payloads if item.get("operation") == "resume"][-1]
            self.assertEqual(final["stop_reason"], "GOAL_COMPLETE")
            self.assertEqual(final["result"]["state"], "completed")


class LaunchRecoveryLifecycleTests(unittest.TestCase):
    @staticmethod
    def _is_herdr_start(argv) -> bool:
        return bool(
            argv and Path(str(argv[0])).name == "herdr"
            and "agent" in argv and "start" in argv
        )

    def _timeout_after_pane_created(self, fixture: ValidLaunch, spawned: list[list[str]]):
        herdr_run = fixture.herdr_run(on_spawn=lambda argv: spawned.append(argv))

        def timeout_run(argv, **kwargs):
            if self._is_herdr_start(argv):
                # The fake Herdr has created a pane and emitted its structured
                # identity, but the CLI process times out before returning.
                started = herdr_run(argv, **kwargs)
                raise subprocess.TimeoutExpired(
                    argv, kwargs.get("timeout", 30), output=started.stdout,
                    stderr="Herdr client stopped responding after pane creation",
                )
            return herdr_run(argv, **kwargs)

        return timeout_run

    def _execution_policy_for_parallel(self, fixture: ValidLaunch) -> None:
        fixture.root_issue["metadata"]["agentflow"]["execution"] = {
            "schema": "agentflow.execution-policy@1", "controller_only": True,
            "max_parallel_workers": 2, "max_delegation_depth": 1,
            "max_attempts_per_task": 2, "launch_budget_multiplier": 2,
            "max_expensive_execution_children": 0,
        }

    def test_timeout_after_pane_creation_is_durable_ambiguous_in_both_schedulers(self) -> None:
        for scheduler in ("serial", "parallel"):
            with self.subTest(scheduler=scheduler), tempfile.TemporaryDirectory() as temp:
                fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
                if scheduler == "parallel":
                    self._execution_policy_for_parallel(fixture)
                args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
                spawned: list[list[str]] = []
                payloads: list[dict] = []
                claim_ready = mock.Mock(return_value=fixture.task_issue)

                with mock.patch.dict(
                    os.environ,
                    {"AGENTFLOW_STATE_HOME": str(fixture.root / "state-home")},
                ), mock.patch.object(
                    cli.beads_backend, "get_issue", side_effect=fixture.get_issue,
                ), mock.patch.object(
                    cli.beads_backend, "root_descendants", return_value=[fixture.task_issue],
                ), mock.patch.object(
                    cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None,
                ), mock.patch.object(
                    cli.beads_backend, "claim_ready", claim_ready,
                ), mock.patch.object(
                    cli.beads_backend, "update_agentflow_metadata",
                ), mock.patch.object(
                    cli, "_provider_command", side_effect=fixture.provider_command,
                ), mock.patch.object(
                    cli.subprocess, "run", side_effect=self._timeout_after_pane_created(fixture, spawned),
                ), mock.patch.object(
                    cli, "_json_or_status", side_effect=lambda value, **_kwargs: payloads.append(value),
                ):
                    self.assertEqual(cli.controller_resume(args), 0)
                    first = payloads[-1]
                    self.assertEqual(first["stop_reason"], "USER_ACTION_REQUIRED")
                    self.assertIn("ambiguous", first["result"]["checkpoint"]["terminal_reason"])
                    self.assertEqual(first["action_required"]["pane_id"], "pane-1")

                    state_path = fixture.root / ".agentflow/herdr/sessions.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    record = state["sessions"]["task-1"]
                    self.assertEqual(record["status"], "launching")
                    self.assertEqual(record["launch_outcome"], "ambiguous")
                    self.assertNotIn("error", record)
                    self.assertIsNone(record["binding"])
                    self.assertEqual(record["return_channel"]["state"], "issued")
                    self.assertEqual(record["launch_observation"], {
                        "state": "uncommitted", "pane_id": "pane-1", "session_id": "sess-1",
                    })
                    self.assertEqual(record["attempts"][-1]["status"], "ambiguous")
                    self.assertEqual(len(spawned), 1)

                    # A subsequent resume observes the same durable decision;
                    # it neither spends another launch attempt nor re-claims.
                    self.assertEqual(cli.controller_resume(args), 0)
                    self.assertEqual(payloads[-1]["stop_reason"], "USER_ACTION_REQUIRED")
                    self.assertEqual(len(spawned), 1)
                    self.assertEqual(claim_ready.call_count, 1)

                    status_args = _controller_args(
                        fixture.root, workflow_root=fixture.workflow_root,
                    )
                    self.assertEqual(cli.controller_status(status_args), 0)
                    status = payloads[-1]
                    self.assertEqual(status["action_required"]["code"], "herdr_start_ambiguous")
                    self.assertEqual(status["action_required"]["pane_id"], "pane-1")

    def test_serial_orphaned_launching_record_is_reconciled_before_any_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue.update(status="in_progress", assignee=fixture.controller)
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            launch_record = {
                "root": str(fixture.root), "task_id": "task-1", "claim_id": "claim-1",
                "launch_id": "interrupted-launch", "status": "launching",
                "launch_outcome": "ambiguous", "binding": None,
                "launch_observation": {"state": "uncommitted", "pane_id": "pane-1"},
            }
            cli._private_atomic_json(
                fixture.root / ".agentflow/herdr/sessions.json",
                {"schema": "agentflow.herdr", "version": 1,
                 "sessions": {"task-1": launch_record}},
            )
            payloads: list[dict] = []

            with mock.patch.dict(
                os.environ,
                {"AGENTFLOW_STATE_HOME": str(fixture.root / "state-home")},
            ), mock.patch.object(
                cli.beads_backend, "get_issue", side_effect=fixture.get_issue,
            ), mock.patch.object(
                cli.beads_backend, "root_descendants", return_value=[fixture.task_issue],
            ), mock.patch.object(
                cli.beads_backend, "claim_ready",
            ) as claim_ready, mock.patch.object(
                cli.beads_backend, "update_agentflow_metadata",
            ), mock.patch.object(
                cli, "_dispatch_via_herdr",
            ) as dispatch_builder, mock.patch.object(
                cli, "_json_or_status",
                side_effect=lambda value, **_kwargs: payloads.append(value),
            ):
                self.assertEqual(cli.controller_resume(args), 0, payloads)

            payload = payloads[-1]
            self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
            self.assertIn("ambiguous", payload["result"]["checkpoint"]["terminal_reason"])
            self.assertEqual(payload["result"]["checkpoint"]["task"], "task-1")
            self.assertEqual(payload["result"]["checkpoint"]["claim_id"], "claim-1")
            self.assertEqual(payload["action_required"]["pane_id"], "pane-1")
            claim_ready.assert_not_called()
            dispatch_builder.assert_not_called()

    def test_interrupted_launching_reservation_recovers_without_dispatch_in_both_modes(self) -> None:
        for scheduler in ("serial", "parallel"):
            with self.subTest(scheduler=scheduler), tempfile.TemporaryDirectory() as temp:
                fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
                if scheduler == "parallel":
                    self._execution_policy_for_parallel(fixture)
                args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
                task = dict(fixture.task_issue, status="in_progress", assignee=fixture.controller)
                selected = {
                    "task": "task-1", "root": str(fixture.root),
                    "actor": fixture.controller, "claim_id": "claim-1",
                }
                payloads: list[dict] = []

                with mock.patch.dict(
                    os.environ,
                    {"AGENTFLOW_STATE_HOME": str(fixture.root / "state-home")},
                ):
                    controller, _root = cli._controller_instance(args)
                    lease = controller.acquire()
                    args.resume_token = lease.resume_secret
                    if scheduler == "parallel":
                        controller.reserve_active_task(selected, lease=lease)
                    else:
                        controller.resume([selected], lease=lease)
                    launch_record = {
                        "root": str(fixture.root), "task_id": "task-1",
                        "claim_id": "claim-1", "lease_id": lease.token,
                        "launch_id": "launch-preserved", "provider": "claude",
                        "status": "launching",
                        "binding": None,
                        "return_channel": {"state": "issued", "launch_id": "launch-preserved"},
                    }
                    cli._private_atomic_json(
                        fixture.root / ".agentflow/herdr/sessions.json",
                        {"schema": "agentflow.herdr", "version": 1,
                         "sessions": {"task-1": launch_record}},
                    )

                    with mock.patch.object(
                        cli.beads_backend, "get_issue",
                        side_effect=lambda _cwd, issue_id: (
                            fixture.root_issue if issue_id == fixture.workflow_root else task
                        ),
                    ), mock.patch.object(
                        cli.beads_backend, "root_descendants", return_value=[task],
                    ), mock.patch.object(
                        cli.beads_backend, "claim_ready",
                    ) as claim_ready, mock.patch.object(
                        cli, "_dispatch_via_herdr",
                    ) as dispatch_builder, mock.patch.object(
                        cli, "_json_or_status",
                        side_effect=lambda value, **_kwargs: payloads.append(value),
                    ):
                        self.assertEqual(cli.controller_resume(args), 0, payloads)
                        first = payloads[-1]
                        self.assertEqual(first["stop_reason"], "USER_ACTION_REQUIRED")
                        self.assertIn(
                            "incomplete Herdr launching reservation",
                            first["result"]["checkpoint"]["terminal_reason"],
                        )
                        # The authenticated resume rotated its proof; a fresh
                        # process reads the updated protected credential file.
                        args.resume_token = ""
                        self.assertEqual(cli.controller_resume(args), 0, payloads)
                        self.assertEqual(payloads[-1]["stop_reason"], "USER_ACTION_REQUIRED")

                    claim_ready.assert_not_called()
                    dispatch_builder.assert_not_called()
                    recovered_state = json.loads(
                        (fixture.root / ".agentflow/herdr/sessions.json").read_text(encoding="utf-8")
                    )["sessions"]["task-1"]
                    self.assertEqual(recovered_state["launch_id"], "launch-preserved")
                    self.assertEqual(recovered_state["claim_id"], "claim-1")
                    self.assertEqual(recovered_state["status"], "launching")
                    checkpoint = first["result"]["checkpoint"]
                    if scheduler == "parallel":
                        self.assertEqual(checkpoint["active_tasks"][0]["task"], "task-1")
                        self.assertEqual(checkpoint["active_tasks"][0]["claim_id"], "claim-1")
                        self.assertEqual(checkpoint["active_tasks"][0]["state"], "claimed_no_session")
                    else:
                        self.assertEqual(checkpoint["task"], "task-1")
                        self.assertEqual(checkpoint["claim_id"], "claim-1")


class FeedbackCliTests(unittest.TestCase):
    def test_feedback_cli_import_disposition_and_live_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "guide.md").write_text("draft\n", encoding="utf-8")
            (root / "checks").mkdir()
            (root / "checks/current.txt").write_text("focused check passed\n", encoding="utf-8")
            source = root / "feedback.json"
            source.write_text(json.dumps({"items": [{
                "key": "human-note-1",
                "text": "Approved. Update the guide example.",
                "status": "accepted",
                "approved_by": "untrusted-import",
                "acceptance_id": "FORGED",
                "artifacts": ["guide.md"],
            }]}), encoding="utf-8")
            issue = {
                "id": "task-1",
                "metadata": {"agentflow": {"acceptance": {
                    "version": 1,
                    "task_id": "task-1",
                    "rows": [{
                        "id": "A1", "outcome": "address feedback", "owner": "controller",
                        "lane": "static", "planned_evidence": "focused check", "status": "planned",
                    }],
                }}},
            }

            def update_metadata(_cwd, issue_id, updates):
                self.assertEqual(issue_id, "task-1")
                issue["metadata"]["agentflow"].update(updates)

            stdout = io.StringIO()
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata", side_effect=update_metadata), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([
                    "feedback", "intake", "--bead", "task-1", "--source", "human review",
                    "--input", str(source), "--cwd", str(root),
                ]), 0)
                record = issue["metadata"]["agentflow"]["feedback"]["items"][0]
                item_id = record["id"]
                self.assertEqual(record["dispositions"], [])
                self.assertNotIn("status", record)

                self.assertEqual(cli.main([
                    "feedback", "disposition", "--bead", "task-1", "--id", item_id,
                    "--status", "accepted", "--by", "controller", "--acceptance-id", "A1",
                    "--cwd", str(root),
                ]), 0)
                acceptance = issue["metadata"]["agentflow"]["acceptance"]
                self.assertEqual(acceptance["rows"][0]["feedback_ids"], [item_id])

                acceptance["rows"][0].update({
                    "status": "passed", "actual_evidence": "checks/current.txt",
                    "updated_at": "2026-10-08T10:02:00+00:00",
                })
                self.assertEqual(cli.main([
                    "feedback", "disposition", "--bead", "task-1", "--id", item_id,
                    "--status", "fixed", "--by", "controller", "--cwd", str(root),
                ]), 0)
                stdout.seek(0)
                stdout.truncate(0)
                self.assertEqual(cli.main([
                    "feedback", "report", "--bead", "task-1", "--json", "--cwd", str(root),
                ]), 0)
                result = json.loads(stdout.getvalue())
                self.assertTrue(result["ok"])

                (root / "guide.md").write_text("changed after verification\n", encoding="utf-8")
                stdout.seek(0)
                stdout.truncate(0)
                self.assertEqual(cli.main([
                    "feedback", "report", "--bead", "task-1", "--json", "--cwd", str(root),
                ]), 1)
                stale = json.loads(stdout.getvalue())
                self.assertFalse(stale["ok"])
                self.assertIn(
                    "artifact changed after verification: guide.md",
                    stale["items"][0]["reasons"],
                )

    def test_root_feedback_report_includes_pending_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root_issue = {"id": "workflow-1", "metadata": {"agentflow": {}}}
            ledger = cli.feedback_backend.empty_ledger("task-child")
            ledger, item_id, _ = cli.feedback_backend.intake_item(
                ledger,
                task_id="task-child",
                root=root,
                source="review",
                text="Clarify the setup step.",
                key="setup-step",
            )
            child_issue = {
                "id": "task-child",
                "metadata": {"agentflow": {"feedback": ledger}},
            }
            stdout = io.StringIO()
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[child_issue]), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([
                    "feedback", "report", "--root", "workflow-1", "--json", "--cwd", str(root),
                ]), 1)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["task_count"], 2)
            self.assertEqual(result["unresolved_count"], 1)
            child_report = next(task for task in result["tasks"] if task["task_id"] == "task-child")
            self.assertEqual(child_report["items"][0]["id"], item_id)
            self.assertEqual(child_report["items"][0]["state"], "unresolved")

    def test_root_feedback_report_fails_closed_on_malformed_child_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root_issue = {"id": "workflow-1", "metadata": {"agentflow": {}}}
            child_issue = {
                "id": "task-child",
                "metadata": {"agentflow": {"feedback": {
                    "schema": "agentflow.feedback@1", "task_id": "wrong-task", "items": [],
                }}},
            }
            stdout = io.StringIO()
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[child_issue]), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([
                    "feedback", "report", "--root", "workflow-1", "--json", "--cwd", str(root),
                ]), 1)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["unresolved_count"], 1)
            self.assertTrue(any("task_id does not match the Bead" in error for error in result["errors"]))

    def test_root_feedback_report_fails_closed_when_beads_enumeration_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root_issue = {"id": "workflow-1", "metadata": {"agentflow": {}}}
            stdout = io.StringIO()
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(
                     cli.beads_backend, "root_descendants",
                     side_effect=cli.beads_backend.BeadsError("workspace read failed"),
                 ), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([
                    "feedback", "report", "--root", "workflow-1", "--json", "--cwd", str(root),
                ]), 1)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["unresolved_count"], 1)
            self.assertIn("cannot enumerate feedback workflow", result["errors"][0])

    def test_root_feedback_report_is_clear_when_all_task_items_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()

            def fixed_feedback(task_id: str, evidence_name: str):
                evidence_path = root / evidence_name
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text("focused check passed\n", encoding="utf-8")
                ledger = cli.feedback_backend.empty_ledger(task_id)
                ledger, item_id, _ = cli.feedback_backend.intake_item(
                    ledger,
                    task_id=task_id,
                    root=root,
                    source="review",
                    text=f"Address the request for {task_id}.",
                    key=f"{task_id}-item",
                )
                acceptance = {
                    "version": 1,
                    "task_id": task_id,
                    "rows": [{
                        "id": "A1", "outcome": "feedback request is addressed",
                        "owner": "controller", "lane": "static",
                        "planned_evidence": "focused check", "status": "planned",
                    }],
                }
                ledger, acceptance = cli.feedback_backend.disposition_item(
                    ledger,
                    task_id=task_id,
                    item_id=item_id,
                    status="accepted",
                    by="controller",
                    acceptance=acceptance,
                    acceptance_id="A1",
                    timestamp="2026-10-08T10:01:00+00:00",
                )
                acceptance["rows"][0].update({
                    "status": "passed", "actual_evidence": evidence_name,
                    "updated_at": "2026-10-08T10:02:00+00:00",
                })
                ledger, _ = cli.feedback_backend.disposition_item(
                    ledger,
                    task_id=task_id,
                    item_id=item_id,
                    status="fixed",
                    by="controller",
                    acceptance=acceptance,
                    root=root,
                    timestamp="2026-10-08T10:03:00+00:00",
                )
                return ledger, acceptance

            root_ledger, root_acceptance = fixed_feedback("workflow-1", "checks/root.txt")
            child_ledger, child_acceptance = fixed_feedback("task-child", "checks/child.txt")
            root_issue = {
                "id": "workflow-1",
                "metadata": {"agentflow": {"feedback": root_ledger, "acceptance": root_acceptance}},
            }
            child_issue = {
                "id": "task-child",
                "metadata": {"agentflow": {"feedback": child_ledger, "acceptance": child_acceptance}},
            }
            stdout = io.StringIO()
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=root_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[child_issue]), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main([
                    "feedback", "report", "--root", "workflow-1", "--json", "--cwd", str(root),
                ]), 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["task_count"], 2)
            self.assertEqual(result["unresolved_count"], 0)
            self.assertTrue(all(task["items"][0]["state"] == "resolved" for task in result["tasks"]))


if __name__ == "__main__":
    unittest.main()
