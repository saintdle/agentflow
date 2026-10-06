from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli, execution_limits
from tests.test_cli import ValidLaunch


class StructuredLimitTests(unittest.TestCase):
    def test_complete_fields_and_policy_limits(self) -> None:
        limits = execution_limits.parse_limits({"deadline_seconds": 90, "max_retries": 20})
        self.assertEqual(limits.to_dict(), {"deadline_seconds": 90, "max_retries": 20})
        self.assertEqual(execution_limits.effective_attempt_limit(2, limits), 2)
        retry_once = execution_limits.parse_limits({"deadline_seconds": 90, "max_retries": 0})
        self.assertEqual(execution_limits.effective_attempt_limit(2, retry_once), 1)
        self.assertEqual(execution_limits.capability_status(None)["deadline"], "advisory")
        self.assertEqual(execution_limits.capability_status(None)["retries"], "unavailable")
        enforced = execution_limits.capability_status(limits, enforced=True)
        self.assertEqual(enforced["provider_spend"], "unavailable")
        self.assertEqual(enforced["detached_descendants"], "outside_process_group_cap")

    def test_legacy_snapshot_cannot_remove_protected_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_home = root.parent / f"{root.name}-external-state"
            limits = execution_limits.ExecutionLimits(deadline_seconds=30, max_retries=1)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}):
                entry = cli._reserve_execution_attempt(
                    root, "workflow", "task", limits=limits,
                    policy_max_attempts=2, authority_secret="controller-secret",
                )
                structured = {
                    "execution_limits": limits.to_dict(),
                    "deadline_epoch": entry["deadline_epoch"],
                    "attempt": entry["attempt"],
                    "max_attempts": entry["max_attempts"],
                }
                self.assertEqual(
                    cli._verify_execution_snapshot_against_ledger(
                        root, "workflow", "task", structured,
                        authority_secret="controller-secret",
                    ),
                    entry,
                )
                with self.assertRaisesRegex(ValueError, "cannot remove limits"):
                    cli._verify_execution_snapshot_against_ledger(
                        root, "workflow", "task", {"execution_limits": None},
                        authority_secret="controller-secret",
                    )
                path = cli._execution_limit_ledger_path(root, "workflow")
                ledger = json.loads(path.read_text(encoding="utf-8"))
                key = cli._execution_ledger_key(root, "workflow", "task")
                ledger["entries"][key]["deadline_epoch"] += 1
                cli._private_atomic_json(path, ledger)
                with self.assertRaisesRegex(ValueError, "invalid"):
                    cli._read_any_execution_ledger_entry(
                        root, "workflow", "task", authority_secret="controller-secret",
                    )

    def test_expired_protected_deadline_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            state_home = root.parent / f"{root.name}-external-state"
            limits = execution_limits.ExecutionLimits(deadline_seconds=60, max_retries=3)
            clock = {"now": 1_000.0}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}), \
                 mock.patch.object(cli.time, "time", side_effect=lambda: clock["now"]):
                first = cli._reserve_execution_attempt(
                    root, "workflow", "task", limits=limits,
                    policy_max_attempts=4, authority_secret="controller-secret",
                )
                clock["now"] += 20
                second = cli._reserve_execution_attempt(
                    root, "workflow", "task", limits=limits,
                    policy_max_attempts=4, authority_secret="controller-secret",
                )
                self.assertEqual(second["deadline_epoch"], first["deadline_epoch"])
                self.assertEqual(second["attempt"], 2)
                clock["now"] = first["deadline_epoch"] + 1
                with self.assertRaisesRegex(ValueError, "deadline expired"):
                    cli._reserve_execution_attempt(
                        root, "workflow", "task", limits=limits,
                        policy_max_attempts=4, authority_secret="controller-secret",
                    )

    def test_partial_or_invalid_fields_rejected(self) -> None:
        for value in (
            {"deadline_seconds": 30}, {"max_retries": 1},
            {"deadline_seconds": 0, "max_retries": 0},
            {"deadline_seconds": True, "max_retries": 0},
            {"deadline_seconds": 30, "max_retries": -1},
        ):
            with self.subTest(value=value), self.assertRaises(execution_limits.ExecutionLimitError):
                execution_limits.parse_limits(value)

    @unittest.skipUnless(hasattr(pty, "fork"), "PTY fork is unavailable")
    def test_delayed_foreground_handoff_resumes_interactive_provider(self) -> None:
        module_path = str(Path(execution_limits.__file__).resolve())
        provider_code = (
            "import sys,time; print('START',flush=True); "
            "print('READ_OK:'+sys.stdin.readline().strip(),flush=True); time.sleep(30)"
        )
        supervisor_code = (
            "import os,sys,time; sys.path.insert(0,sys.argv[1]); "
            "from agentflow import execution_limits as e; original=e.os.tcsetpgrp; "
            "e.os.tcsetpgrp=lambda fd,g:(time.sleep(.3),original(fd,g))[-1]; "
            "raise SystemExit(e.supervise(time.time()+1.5,[sys.executable,'-u','-c',sys.argv[2]]))"
        )
        pid, master_fd = pty.fork()
        if pid == 0:
            os.execl(sys.executable, sys.executable, "-c", supervisor_code,
                     str(Path(module_path).parents[1]), provider_code)
        output = bytearray()
        sent = False
        status = None
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    try:
                        output.extend(os.read(master_fd, 4096))
                    except OSError:
                        break
                if b"START\r\n" in output and not sent:
                    os.write(master_fd, b"fixture-line\n")
                    sent = True
                if b"READ_OK:fixture-line" in output:
                    break
            self.assertTrue(sent, output.decode("utf-8", "replace"))
            self.assertIn(b"READ_OK:fixture-line", output, output.decode("utf-8", "replace"))
            while time.monotonic() < deadline:
                waited, raw_status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    status = raw_status
                    break
                time.sleep(0.02)
            self.assertIsNotNone(status, output.decode("utf-8", "replace"))
            self.assertEqual(os.waitstatus_to_exitcode(status), 124)
        finally:
            if status is None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            os.close(master_fd)

    def test_launch_deadline_and_attempt_ledger_survive_session_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
                "deadline_seconds": 60, "max_retries": 0,
            }
            fixture.lease = fixture._seed_lease()
            fixture.authority_secret = cli._controller_credentials(
                argparse.Namespace(root=str(fixture.root), workflow_root=fixture.workflow_root, resume_key_file=""),
                fixture.lease,
            )[1]["authority_secret"]
            fixture._persist_claim()
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            capture: dict[str, object] = {}
            external_state = Path(temp).parent / f"{Path(temp).name}-external-state"
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(external_state)}), \
                 fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                row = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text())["sessions"][fixture.task_id]
                deadline_epoch = row["deadline_epoch"]
                self.assertEqual(row["attempt"], 1)
                self.assertEqual(row["max_attempts"], 1)
                self.assertEqual(row["execution_limit_capabilities"]["provider_spend"], "unavailable")
                argv = capture["argv"]
                provider_tail = argv[argv.index("--") + 1:]
                self.assertTrue(Path(provider_tail[0]).name.startswith("python"))
                self.assertIn("execution_limits.py", provider_tail[1])
                self.assertEqual(provider_tail[provider_tail.index("--") + 1], "/usr/bin/claude")
                self.assertIn(fixture.model, provider_tail)
                cli._require_attested_model(fixture.provider, "sess-1", fixture.model)

                row["status"] = "failed"
                cli._private_atomic_json(
                    fixture.root / ".agentflow/herdr/sessions.json",
                    {"schema": "agentflow.herdr", "version": 1, "sessions": {fixture.task_id: row}},
                )
                # Roll back the workspace session snapshot. The protected
                # ledger still refuses a launch beyond max_retries=0.
                cli._private_atomic_json(
                    fixture.root / ".agentflow/herdr/sessions.json",
                    {"schema": "agentflow.herdr", "version": 1, "sessions": {}},
                )
                capture.clear()
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                self.assertEqual(capture, {})
                self.assertGreater(deadline_epoch, time.time())

    def test_duplicate_active_launch_does_not_debit_protected_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
                "deadline_seconds": 60, "max_retries": 1,
            }
            fixture.lease = fixture._seed_lease()
            fixture.authority_secret = cli._controller_credentials(
                argparse.Namespace(root=str(fixture.root), workflow_root=fixture.workflow_root, resume_key_file=""),
                fixture.lease,
            )[1]["authority_secret"]
            fixture._persist_claim()
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            capture: dict[str, object] = {}
            external_state = Path(temp).parent / f"{Path(temp).name}-external-state"
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(external_state)}), \
                 fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run(capture=capture)):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                before = cli._read_any_execution_ledger_entry(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                )
                self.assertEqual(before["attempt"], 1)
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                after = cli._read_any_execution_ledger_entry(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                )
                self.assertEqual(after["attempt"], 1)

    def test_retry_after_session_rollback_keeps_ledger_attempt_and_ingests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
                "deadline_seconds": 60, "max_retries": 1,
            }
            fixture.lease = fixture._seed_lease()
            fixture.authority_secret = cli._controller_credentials(
                argparse.Namespace(root=str(fixture.root), workflow_root=fixture.workflow_root, resume_key_file=""),
                fixture.lease,
            )[1]["authority_secret"]
            fixture._persist_claim()
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            capture: dict[str, object] = {}
            external_state = Path(temp).parent / f"{Path(temp).name}-external-state"
            starts = {"count": 0}
            fixture_herdr_run = fixture.herdr_run(capture=capture)

            def herdr_run(argv, **kwargs):
                if argv and Path(str(argv[0])).name == "herdr" and "start" in argv:
                    starts["count"] += 1
                    result = fixture_herdr_run(argv, **kwargs)
                    if starts["count"] == 1:
                        return subprocess.CompletedProcess(
                            argv, 1, stdout=result.stdout, stderr=result.stderr,
                        )
                    return result
                return fixture_herdr_run(argv, **kwargs)

            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(external_state)}), \
                 fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=herdr_run):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 2)
                state_path = fixture.root / ".agentflow/herdr/sessions.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                row = state["sessions"][fixture.task_id]
                self.assertEqual(row["status"], "failed")
                # Simulate rollback of the workspace-owned retry history. The
                # protected ledger and signed retry contract remain intact.
                for key in ("attempt", "launch_id", "binding"):
                    row.pop(key, None)
                row["attempts"] = []
                cli._private_atomic_json(state_path, state)

                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                row = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                ledger = cli._read_any_execution_ledger_entry(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                )
                self.assertEqual(ledger["attempt"], 2)
                self.assertEqual(row["attempt"], 2)
                self.assertEqual(row["return_channel"]["contract_binding"]["attempt"], 2)

                channel = row["return_channel"]
                Path(channel["result_path"]).write_text(json.dumps({
                    "outcome": "completed",
                    "acceptance_results": [{
                        "acceptance_id": "R1", "status": "passed",
                        "evidence": "retry passed", "source": "provider",
                    }],
                }), encoding="utf-8")
                result_args = argparse.Namespace(
                    root=str(fixture.root), contract=channel["contract_path"],
                    file=channel["result_path"], json=True,
                    _controller_ingest=True, _capability_file=channel["capability_file"],
                    _authority_secret=fixture.authority_secret,
                )
                with mock.patch.object(cli, "_json_or_status"):
                    self.assertEqual(cli.herdr_result(result_args), 0)
                completed = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                self.assertEqual(completed["return_channel"]["state"], "consumed")

    def test_deadline_is_rechecked_inside_final_result_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
                "deadline_seconds": 60, "max_retries": 1,
            }
            fixture.lease = fixture._seed_lease()
            fixture.authority_secret = cli._controller_credentials(
                argparse.Namespace(root=str(fixture.root), workflow_root=fixture.workflow_root, resume_key_file=""),
                fixture.lease,
            )[1]["authority_secret"]
            fixture._persist_claim()
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            external_state = Path(temp).parent / f"{Path(temp).name}-external-state"
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(external_state)}), \
                 fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                state_path = fixture.root / ".agentflow/herdr/sessions.json"
                row = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                channel = row["return_channel"]
                deadline = row["deadline_epoch"]
                Path(channel["result_path"]).write_text(json.dumps({
                    "outcome": "completed",
                    "acceptance_results": [{
                        "acceptance_id": "R1", "status": "passed",
                        "evidence": "on-time before final fence", "source": "provider",
                    }],
                }), encoding="utf-8")
                args = argparse.Namespace(
                    root=str(fixture.root), contract=channel["contract_path"],
                    file=channel["result_path"], json=True, _controller_ingest=True,
                    _capability_file=channel["capability_file"],
                    _authority_secret=fixture.authority_secret,
                )
                real_transaction = cli._herdr_transaction

                @contextlib.contextmanager
                def expire_inside_transaction(path):
                    with real_transaction(path) as current:
                        with mock.patch.object(cli.time, "time", return_value=deadline + 1):
                            yield current

                payloads: list[dict] = []
                with fixture.beads_patches(), \
                     mock.patch.object(cli, "_herdr_transaction", side_effect=expire_inside_transaction), \
                     mock.patch.object(cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload)):
                    self.assertEqual(cli.herdr_result(args), 2)
                self.assertIn("deadline expired", payloads[-1]["error"])
                after = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                self.assertEqual(after["return_channel"]["state"], "issued")


if __name__ == "__main__":
    unittest.main()
