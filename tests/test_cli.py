from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli
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
                 claim_token: str = "opaque-claim-token-0123456789abcdef0123456789ab") -> None:
        self.root = root
        self.provider = provider
        self.model = model
        self.effort = effort
        self.role = role
        self.controller = controller
        self.workflow_root = "wf-root"
        self.task_id = "task-1"
        self.actor = controller
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
        subprocess.run(["git", "init", "-b", "main", r], capture_output=True, check=True)
        subprocess.run(["git", "-C", r, "config", "user.email", "t@example.test"], capture_output=True, check=True)
        subprocess.run(["git", "-C", r, "config", "user.name", "Test"], capture_output=True, check=True)
        (self.root / "README.md").write_text("root\n", encoding="utf-8")
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/validate.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
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
        with self.beads_patches():
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
                  capture: dict | None = None, on_spawn=None):
        """A subprocess.run side_effect that intercepts ONLY the herdr spawn.

        Real git subprocesses (used by the handoff pipeline and preflight) pass
        through untouched; the `herdr agent start ...` invocation returns a
        canned agent_started body so no real Herdr binary is required. This is
        the exact argv production passes to Herdr, so `capture` records it.
        """
        real_run = subprocess.run
        body = stdout if stdout is not None else _agent_started_stdout(self.provider)

        def _run(argv, **kwargs):
            if argv and Path(str(argv[0])).name == "herdr" and "agent" in argv and "start" in argv:
                if capture is not None:
                    capture["argv"] = list(argv)
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
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
            record = json.loads(state_file.read_text(encoding="utf-8"))["sessions"]["task-1"]
            self.assertEqual(record["status"], "launched")
            self.assertTrue(record["binding"]["launch_id"])
            self.assertEqual(record["binding"]["launch_id"], record["launch_id"])
            self.assertEqual(record["binding"]["session_id"], "sess-1")
            self.assertEqual(oct(state_file.stat().st_mode & 0o777), "0o600")
            self.assertEqual(record["attempt"], 2)

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
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), mock.patch(
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
            os.environ, {"XDG_STATE_HOME": state}
        ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, mock.patch.object(cli.beads_backend, "prime", return_value=""):
            self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)
            response = json.loads(stdout.getvalue())
            self.assertIn("project-owned domain skills", response["systemMessage"])
            self.assertNotIn("training", response["systemMessage"].lower())
            self.assertNotIn("private", response["systemMessage"].lower())

    def test_session_start_hook_injects_beads_only_when_active(self) -> None:
        payload = {"hook_event_name": "SessionStart", "cwd": "/tmp/project"}
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state}
        ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, mock.patch.object(
            cli.beads_backend, "prime", return_value="BEADS PRIME CONTEXT"
        ) as prime:
            self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)
            response = json.loads(stdout.getvalue())
            self.assertIn("BEADS PRIME CONTEXT", response["systemMessage"])
            self.assertIn("untrusted task data", response["systemMessage"])
            self.assertIn("never present a bare bead ID", response["systemMessage"])
            prime.assert_called_once()

    def test_copilot_session_hook_uses_additional_context_json(self) -> None:
        payload = {"hookEventName": "sessionStart", "cwd": "/tmp/project"}
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": state}
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
                base="main@abc123",
                dependency=[],
                done_when=["Review returns a verdict"],
                context=[str(context)],
                constraint=["Read-only"],
                check=["test -s source.md"],
                budget=["20 minutes; one retry; stop on blocker"],
                issue="#1",
                branch="agent/review",
                out=str(output),
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
                base="main@abc123",
                dependency=[],
                done_when=["Evidence returned"],
                context=[str(context)],
                constraint=[],
                check=[],
                budget=["10 minutes; no retry"],
                issue="",
                branch="",
                out=str(output),
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
                base="main@abc123",
                dependency=[],
                done_when=["Evidence returned"],
                context=[str(context)],
                constraint=[],
                check=[],
                budget=["10 minutes; no retry; stop on blocker"],
                issue="",
                branch="",
                out=str(output),
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
                base="main@abc123",
                branch="agent/work-1",
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
            os.environ, {"XDG_STATE_HOME": temp}
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
                           report_done: threading.Event, threads: list) -> None:
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
        # Stay "running" for a few controller polls before the result lands.
        time.sleep(report_delay)
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

            def on_spawn(argv):
                _spawn_provider_report(fixture, argv, report_delay=0.0,
                                       report_done=report_done, threads=threads)

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
                self.assertEqual(first["result"]["state"], "running")
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

            def on_spawn(argv):
                _spawn_provider_report(fixture, argv, report_delay=0.05,
                                       report_done=report_done, threads=threads)

            claim_calls = {"n": 0}

            def fake_claim_ready(cwd, *, parent, labels, actor):
                claim_calls["n"] += 1
                return fixture.task_issue if claim_calls["n"] == 1 else None

            def fake_root_descendants(cwd, root_id):
                return [fixture.task_issue]

            def fake_close_issue(cwd, task_id, reason):
                fixture.task_issue["status"] = "closed"

            real_task_result = cli._herdr_task_result
            polls = {"n": 0}

            def spy_task_result(root, task_id):
                polls["n"] += 1
                return real_task_result(root, task_id)

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
                 mock.patch.object(cli.beads_backend, "claim_ready") as claim_ready, \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                payload = _run_controller_json(cli.controller_resume, args)
            claim_ready.assert_not_called()
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["result"]["task"], "task-1")
            self.assertEqual(payload["result"]["state"], "running")

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
                    # Stay "running" for a few controller polls before the
                    # result lands, so the autonomous loop is proven to poll
                    # more than once through a live session (AFREL-020).
                    time.sleep(0.05)
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
            base="main@abc123",
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


if __name__ == "__main__":
    unittest.main()
