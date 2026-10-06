from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import beads, cli
from tests import _state_home  # noqa: F401  # external controller authority


ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.environ.get("AGENTFLOW_INTEGRATION") == "1"
NATIVE_PAYLOAD_FIXTURES = ROOT / "tests/fixtures/provider-native-payloads"


def _native_payload(name: str, *, session_id: str, cwd: str) -> dict:
    payload = json.loads((NATIVE_PAYLOAD_FIXTURES / name).read_text(encoding="utf-8"))
    session_key = "sessionId" if "sessionId" in payload else "session_id"
    payload[session_key] = session_id
    payload["cwd"] = cwd
    return payload


def _issue_from_create(root: Path, *arguments: str) -> dict:
    result = beads.run(root, "create", *arguments, "--json")
    value = beads._json_output(result, "bd create")
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict) or not value.get("id"):
        raise AssertionError(f"unexpected bead create result: {value!r}")
    return value


class ProcessBoundaryLifecycleTests(unittest.TestCase):
    """OS-process proof with real Beads and a deterministic Herdr protocol fake."""

    @unittest.skipUnless(
        RUN_INTEGRATION and shutil.which("bd"),
        "set AGENTFLOW_INTEGRATION=1 and install bd to run real-tool integration",
    )
    def test_real_beads_herdr_provider_crash_resume_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            test_root = Path(temporary).resolve()
            root = test_root / "workspace"
            subprocess.run(["git", "init", "-b", "main", str(root)], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "process@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Process Test"], check=True)
            (root / "README.md").write_text("process boundary\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "init"], capture_output=True, check=True)
            base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            beads.initialize(root, mode="embedded", stealth=True, prefix="af", gitless=True)
            matrix = {
                "version": 1,
                "task_id": "workflow-root",
                "rows": [{
                    "id": "R1", "outcome": "provider process exits cleanly", "owner": "controller",
                    "lane": "local-runtime", "planned_evidence": "provider subprocess report",
                    "status": "passed", "actual_evidence": "process-boundary test",
                }],
            }
            root_issue = _issue_from_create(
                root, "Workflow root", "--description", "process boundary root",
                "--acceptance", "R1 passes", "--labels", "workflow",
                "--no-inherit-labels",
            )
            workflow_root = str(root_issue["id"])
            matrix["task_id"] = workflow_root
            beads.update_agentflow_metadata(root, workflow_root, {"acceptance": matrix})
            task = _issue_from_create(
                root, "Deterministic provider task", "--description", "run the provider subprocess",
                "--acceptance", "R1 passes", "--parent", workflow_root,
                "--labels", "implementation", "--no-inherit-labels",
            )
            task_id = str(task["id"])
            task_matrix = dict(matrix)
            task_matrix["task_id"] = task_id
            beads.update_agentflow_metadata(root, task_id, {
                "launch": {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium", "role": "coding"},
                "base": f"main@{base[:12]}",
                "acceptance": task_matrix,
                "context": ["README.md"],
                "tool_profile": "shell-write",
                "budget": ["10 minutes; one retry; stop on blocker"],
                "checks": ["process-boundary"],
                # Controller dispatch materializes this as an external lane;
                # provide the same explicit context/profile/budget a manual
                # external from-bead handoff requires.
                "context": ["README.md"],
                "tool_profile": "shell-write",
                "budget": ["time: 2 minutes", "retry: one bounded retry", "stop: report a blocker"],
            })
            second_task = _issue_from_create(
                root, "Deterministic provider task two", "--description", "run the provider subprocess twice",
                "--acceptance", "R1 passes", "--parent", workflow_root,
                "--labels", "implementation", "--no-inherit-labels",
            )
            second_task_id = str(second_task["id"])
            second_matrix = dict(matrix)
            second_matrix["task_id"] = second_task_id
            beads.update_agentflow_metadata(root, second_task_id, {
                "launch": {"provider": "codex", "model": "gpt-5.6-luna", "effort": "medium", "role": "coding"},
                "base": f"main@{base[:12]}",
                "acceptance": second_matrix,
                "context": ["README.md"],
                "tool_profile": "shell-write",
                "budget": ["10 minutes; one retry; stop on blocker"],
                "checks": ["process-boundary"],
                "context": ["README.md"],
                "tool_profile": "shell-write",
                "budget": ["time: 2 minutes", "retry: one bounded retry", "stop: report a blocker"],
            })

            bin_dir = root / "bin"
            bin_dir.mkdir()
            provider = bin_dir / "codex"
            provider.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "root = Path(os.environ['AGENTFLOW_HANDOFF_PATH']).parents[3]\n"
                "agent = os.environ['AGENTFLOW_HERDR_AGENT_NAME']\n"
                "pane = ''\n"
                "deadline = time.time() + 10\n"
                "while time.time() < deadline and not pane:\n"
                "    probe = subprocess.run(['herdr', 'agent', 'get', agent], capture_output=True, text=True)\n"
                "    try:\n"
                "        data = json.loads(probe.stdout)\n"
                "        pane = str(data.get('result', {}).get('agent', {}).get('pane_id') or '')\n"
                "    except (json.JSONDecodeError, AttributeError):\n"
                "        pass\n"
                "    if not pane: time.sleep(0.05)\n"
                "if not pane: raise SystemExit('Herdr pane was not discoverable')\n"
                "hook = {'hook_event_name': 'SessionStart', 'session_id': 'provider-process-session', 'cwd': os.getcwd(), 'model': 'gpt-5.6-luna'}\n"
                "subprocess.run([sys.executable, '-m', 'agentflow.cli', 'hook', '--provider', 'codex', '--event', 'SessionStart'], input=json.dumps(hook), text=True, check=True, capture_output=True)\n"
                "subprocess.run(['herdr', 'pane', 'report-agent-session', pane, '--source', 'process-boundary', '--agent', 'codex', '--agent-session-id', 'provider-process-session'], check=True)\n"
                "contract_path = Path(os.environ['AGENTFLOW_RESULT_CONTRACT'])\n"
                "result_path = Path(os.environ['AGENTFLOW_RESULT_FILE'])\n"
                "contract = json.loads(contract_path.read_text())\n"
                "task_id = os.environ['AGENTFLOW_TASK_ID']\n"
                "snapshot = root / '.process-boundary-snapshots' / task_id; snapshot.mkdir(parents=True, exist_ok=True)\n"
                "result_body = json.dumps({'outcome': 'completed', 'acceptance_results': [{'acceptance_id': item, 'status': 'passed', 'evidence': 'real provider process', 'source': 'provider-process'} for item in contract['acceptance_ids']]})\n"
                "(snapshot / 'contract').write_bytes(contract_path.read_bytes())\n"
                "(snapshot / 'result').write_text(result_body)\n"
                "result_path.write_text(result_body)\n"
                "report = subprocess.run([sys.executable, '-m', 'agentflow.cli', 'herdr', 'submit', '--contract', str(contract_path), '--file', str(result_path), '--json'], check=False)\n"
                "raise SystemExit(report.returncode)\n",
                encoding="utf-8",
            )
            provider.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}"
            environment["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{environment.get('PYTHONPATH', '')}"

            # Herdr-compatible deterministic fallback: installed Herdr needs
            # a persistent interactive server and provider-specific session
            # reporting, which a CI provider executable cannot supply. This
            # executable still crosses the real OS subprocess boundary and
            # returns the same structured agent-start contract.
            fake_herdr = bin_dir / "herdr"
            fake_herdr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, subprocess, sys\n"
                "args = sys.argv[1:]\n"
                "agent = args[2] if len(args) > 2 and args[:2] == ['agent', 'start'] else 'process-boundary-agent'\n"
                "if args[:2] == ['integration', 'status']:\n"
                "    print('codex: current (test)')\n"
                "    sys.exit(0)\n"
                "if args[:2] == ['status', 'server']:\n"
                "    print('status: running\\ncompatible: yes')\n"
                "    sys.exit(0)\n"
                "if args[:2] == ['agent', 'start']:\n"
                "    child_env = dict(os.environ)\n"
                "    i = 3\n"
                "    while i < len(args) and args[i] != '--':\n"
                "        if args[i] == '--env':\n"
                "            key, _, value = args[i + 1].partition('='); child_env[key] = value; i += 2\n"
                "        else: i += 1\n"
                "    subprocess.Popen(args[i + 1:], env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
                "    agent_data = {'pane_id': 'deterministic-pane', 'agent': 'codex', 'agent_session': {'source': 'process-boundary', 'agent': 'codex', 'kind': 'id', 'value': 'provider-process-session'}}\n"
                "    print(json.dumps({'id': 'cli:agent:start', 'result': {'type': 'agent_started', 'agent': agent_data}}))\n"
                "    sys.exit(0)\n"
                "if args[:2] == ['agent', 'get']:\n"
                "    agent_data = {'pane_id': 'deterministic-pane', 'agent': 'codex', 'agent_session': {'source': 'process-boundary', 'agent': 'codex', 'kind': 'id', 'value': 'provider-process-session'}}\n"
                "    print(json.dumps({'id': 'cli:agent:get', 'result': {'type': 'agent_info', 'agent': agent_data}}))\n"
                "    sys.exit(0)\n"
                "print(json.dumps({'ok': True}))\n",
                encoding="utf-8",
            )
            fake_herdr.chmod(0o700)
            runner = root / "agentflow-test-runner.py"
            runner.write_text(
                "import sys\n"
                "from agentflow import cli\n"
                f"fake = {str(fake_herdr)!r}\n"
                f"provider = {str(provider)!r}\n"
                "original = cli._provider_command\n"
                "cli._provider_command = lambda name: fake if name == 'herdr' else provider if name == 'codex' else original(name)\n"
                "raise SystemExit(cli.main())\n",
                encoding="utf-8",
            )

            handoff_probe = subprocess.run([
                sys.executable, "-m", "agentflow.cli", "handoff", "from-bead", task_id,
                "--to", "codex", "--cwd", str(root),
                "--out", str(root / ".agentflow/tmp/handoffs/probe.md"),
            ], cwd=root, env=environment, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(handoff_probe.returncode, 0, handoff_probe.stdout + handoff_probe.stderr)

            controller_args = [
                sys.executable, str(runner), "controller", "resume",
                "--root", str(root), "--workflow-root", workflow_root,
                "--poll-interval", "0.05", "--deadline", "120", "--json",
            ]
            first = subprocess.run(controller_args + ["--once"], cwd=root, env=environment,
                                   capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = json.loads(first.stdout)
            herdr_state_exists = (root / ".agentflow/herdr/sessions.json").is_file()
            self.assertEqual(
                first_payload["result"]["state"], "running",
                f"controller state={first_payload['result']['state']}; Herdr state exists={herdr_state_exists}",
            )

            # The first controller process is deliberately treated as crashed
            # after dispatch. A new process consumes the provider's result,
            # disposes the real Bead, and reaches GOAL_COMPLETE.
            resumed = subprocess.run(controller_args, cwd=root, env=environment,
                                     capture_output=True, text=True, timeout=130, check=False)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_payload = json.loads(resumed.stdout)
            self.assertEqual(resumed_payload["stop_reason"], "GOAL_COMPLETE", resumed.stdout)

            state = json.loads((root / ".agentflow/herdr/sessions.json").read_text())
            record = state["sessions"][task_id]
            self.assertEqual(record["return_channel"]["state"], "consumed")
            self.assertEqual(record["result"]["acceptance_results"][0]["acceptance_id"], "R1")
            second_record = state["sessions"][second_task_id]
            self.assertEqual(second_record["return_channel"]["state"], "consumed")
            self.assertEqual(second_record["result"]["acceptance_results"][0]["acceptance_id"], "R1")

            # A separate OS process taking over the reusable controller name
            # gets a new incarnation identity; it cannot inherit the prior
            # controller's return authority.
            time.sleep(0.05)
            takeover = subprocess.run([
                sys.executable, str(runner), "controller", "resume", "--root", str(root),
                "--workflow-root", workflow_root, "--takeover", "--stale-after", "0.01",
                "--resume-key-file", str(test_root / "different-owner.key"), "--once", "--json",
            ], cwd=root, env=environment, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(takeover.returncode, 0, takeover.stdout + takeover.stderr)
            takeover_payload = json.loads(takeover.stdout)
            self.assertNotEqual(takeover_payload["lease"]["continuity_id"], resumed_payload["lease"]["continuity_id"])

            # Restore the exact provider-visible files from the subprocess
            # snapshot. A provider process still cannot invoke the
            # state-mutating consumer directly; only the controller broker
            # owns that path.
            snapshot = root / ".process-boundary-snapshots" / task_id
            contract_path = Path(record["return_channel"]["contract_path"])
            result_path = Path(record["return_channel"]["result_path"])
            contract_path.write_bytes((snapshot / "contract").read_bytes())
            result_path.write_bytes((snapshot / "result").read_bytes())
            replay = subprocess.run([
                sys.executable, str(runner), "herdr", "result",
                "--contract", str(contract_path), "--file", str(result_path),
                "--state-path", str(root / ".agentflow/herdr/sessions.json"), "--json",
            ], cwd=root, env=environment,
                capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(replay.returncode, 2, replay.stdout + replay.stderr)
            self.assertIn("controller-owned", replay.stdout)


class SterileSessionHookLifecycleTests(unittest.TestCase):
    """Exercise the packaged project hook and native event across a process boundary."""

    def test_sterile_native_hooks_preserve_provider_payloads_and_model_changes(self) -> None:
        cases = (
            ("claude", Path(".claude/settings.json"), {"SessionStart", "PostModelSwitch"}),
            ("copilot", Path(".github/hooks/agentflow.json"), {"sessionStart"}),
        )
        for provider, hook_path, event_names in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                source = directory / "source"
                source.mkdir()
                context = source / "allowed.md"
                context.write_text("bounded evidence\n", encoding="utf-8")
                handoff = source / ".agentflow/handoffs/session-hook.md"
                handoff_args = argparse.Namespace(
                    to=provider, title="Session hook lifecycle", goal="Attest the provider route",
                    task_id="task-session-hook", task_class="focused-review", role="reviewer",
                    artifact_kind="internal", writer_model="", lane="external",
                    tool_profile="shell-readonly", output_boundary=str(source / "output"),
                    require_tool=[], require_skill=[], allow_delegation=False, return_type="result",
                    max_ai_credits=30 if provider == "copilot" else None,
                    acceptance_matrix="", isolation_profile="none",
                    require_asset=[], base="", dependency=[],
                    done_when=["Return evidence"], context=[str(context)],
                    constraint=["Do not inspect other files"], check=[],
                    budget=["10 minutes; one retry; stop on blocker"], issue="", branch="",
                    out=str(handoff), cwd=str(source), untrusted_task_data=False,
                )
                (source / "output").mkdir()
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.handoff_create(handoff_args), 0)
                stage = directory / "sterile"
                with mock.patch.object(cli, "_provider_command", return_value="/fake/provider"), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    packaged_handoff = cli._package_handoff_sterile(handoff, stage)
                self.assertEqual(cli._validate_sterile_package(stage), packaged_handoff)

                config = json.loads((stage / hook_path).read_text(encoding="utf-8"))
                self.assertEqual(set(config["hooks"]), event_names)

                state_home = directory / "state"
                environment = dict(os.environ)
                environment["AGENTFLOW_STATE_HOME"] = str(state_home)
                environment["PYTHONPATH"] = (
                    str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
                )
                native_session = f"{provider}-native-session"

                def invoke_hook(event_name: str, event_payload: dict) -> None:
                    entry = config["hooks"][event_name][0]
                    command = entry["hooks"][0]["command"] if provider == "claude" else entry["bash"]
                    command_args = shlex.split(command)
                    self.assertEqual(command_args[0], "~/.local/bin/agentflow")
                    self.assertEqual(command_args[1:4], ["hook", "--provider", provider])
                    event_from_config = command_args[command_args.index("--event") + 1]
                    self.assertEqual(event_from_config, event_name)
                    # The bundled command is rooted at the normal local install
                    # path. Replace only that executable so provider payloads
                    # still cross a real child-process hook boundary.
                    hook_argv = [sys.executable, "-m", "agentflow.cli", *command_args[1:]]
                    hook_result = subprocess.run(
                        hook_argv, cwd=stage, env=environment,
                        input=json.dumps(event_payload), capture_output=True,
                        text=True, timeout=10, check=False,
                    )
                    self.assertEqual(
                        hook_result.returncode, 0, hook_result.stdout + hook_result.stderr
                    )

                if provider == "claude":
                    invoke_hook("SessionStart", _native_payload(
                        "claude-session-start.json", session_id=native_session, cwd=str(stage),
                    ))
                    with mock.patch.dict(
                        os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}, clear=False
                    ):
                        cli._require_attested_model("claude", native_session, "claude-sonnet-5")
                        invoke_hook("PostModelSwitch", _native_payload(
                            "claude-post-model-switch.json",
                            session_id=native_session, cwd=str(stage),
                        ))
                        with self.assertRaisesRegex(ValueError, "provider model mismatch"):
                            cli._require_attested_model(
                                "claude", native_session, "claude-sonnet-5"
                            )
                        cli._require_attested_model(
                            "claude", native_session, "claude-sonnet-4.6"
                        )
                        with self.assertRaisesRegex(
                            ValueError, "no usable local lifecycle model evidence"
                        ):
                            cli._require_attested_model(
                                "claude", "missing-session", "claude-sonnet-5"
                            )
                else:
                    # This is the documented Copilot `sessionStart` shape:
                    # camelCase sessionId and a millisecond timestamp, with
                    # no model field. It must still normalize as lifecycle
                    # metadata without fabricating model attestation.
                    invoke_hook("sessionStart", _native_payload(
                        "copilot-session-start.json", session_id=native_session, cwd=str(stage),
                    ))
                    rows = cli.events_backend.EventSpool(
                        state_home / "events.jsonl"
                    ).read()
                    matching = [
                        row for row in rows
                        if row.provider == "copilot"
                        and row.session_id == cli.events_backend.session_scope(native_session)
                    ]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(matching[0].event, "session.start")
                    self.assertEqual(matching[0].timestamp, "2024-08-29T18:00:00.000Z")
                    self.assertRegex(
                        matching[0].metadata.get("source", ""), r"^source_[0-9a-f]{64}$"
                    )
                    self.assertNotIn("model", matching[0].metadata)
                    with mock.patch.dict(
                        os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}, clear=False
                    ), self.assertRaisesRegex(
                        ValueError, "Copilot.*resolved-model evidence.*unsupported"
                    ):
                        cli._require_attested_model(
                            "copilot", native_session, "claude-sonnet-5"
                        )


if __name__ == "__main__":
    unittest.main()
