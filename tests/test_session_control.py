from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.controller import RootController
from agentflow import cli
from agentflow.session_control import (
    PACKET_SCHEMA,
    SessionBudget,
    build_handoff_packet,
    new_ledger,
    next_generation,
    record_event,
    render_resume_prompt,
)
from agentflow.usage import reconcile_codeburn


class SessionControlTests(unittest.TestCase):
    def test_progress_is_task_aware_and_does_not_require_edits(self) -> None:
        coding = record_event(new_ledger(), event="evidence", task_class="coding")
        self.assertEqual(coding["last_progress"], {})
        review = record_event(new_ledger(), event="evidence", task_class="review", evidence="R1")
        self.assertEqual(review["last_progress"]["event"], "evidence")
        waiting = record_event(new_ledger(), event="watcher", task_class="external-wait")
        self.assertEqual(waiting["last_progress"]["event"], "watcher")

    def test_rotation_is_advisory_after_tasks_or_phases(self) -> None:
        budget = SessionBudget(rotate_after_completed_tasks=2, rotate_after_phases=2)
        ledger = new_ledger(budget=budget)
        ledger = record_event(
            ledger, event="completed", task_class="coding", task="one", phase="code"
        )
        self.assertFalse(ledger["rotation"]["recommended"])
        ledger = record_event(
            ledger, event="completed", task_class="review", task="two", phase="review"
        )
        self.assertTrue(ledger["rotation"]["recommended"])
        self.assertFalse(ledger["blocked"])

    def test_same_named_approach_blocks_at_limit(self) -> None:
        ledger = new_ledger(budget=SessionBudget(same_approach_failure_limit=2))
        first = record_event(
            ledger, event="failure", task_class="coding", approach="patch parser"
        )
        self.assertFalse(first["blocked"])
        second = record_event(
            first, event="failure", task_class="coding", approach="patch parser"
        )
        self.assertTrue(second["blocked"])
        self.assertIn("failed 2 times", second["block_reason"])

    def test_new_generation_preserves_only_summary_and_policy(self) -> None:
        ledger = record_event(
            new_ledger(), event="completed", task_class="coding", task="one", phase="code"
        )
        rotated = next_generation(ledger)
        self.assertEqual(rotated["generation"], 2)
        self.assertEqual(rotated["completed_tasks"], [])
        self.assertEqual(rotated["previous"]["completed_task_count"], 1)

    def test_handoff_packet_is_minimal_and_contains_no_transcript_or_secret(self) -> None:
        packet = build_handoff_packet(
            workspace_root="/repo", workflow_root="af-root", controller="controller",
            continuity_id="continuity", checkpoint={"task": "af-1", "state": "running"},
            ready_tasks=[{"id": "af-2", "title": "authorization=Bearer demo-secret", "status": "open", "labels": ["af:stage:review"]}],
            ledger=new_ledger(),
        )
        self.assertEqual(packet["schema"], PACKET_SCHEMA)
        self.assertFalse(packet["transcript_included"])
        encoded = json.dumps(packet).lower()
        self.assertNotIn("resume_secret", encoded)
        self.assertNotIn("authority_secret", encoded)
        self.assertNotIn("demo-secret", encoded)
        self.assertNotIn("title", packet["ready_tasks"][0])
        self.assertEqual(packet["ready_tasks"][0]["stage"], "review")
        self.assertIn("af-root", render_resume_prompt(packet))

    def test_controller_persists_progress_and_rotation_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            controller = RootController("root", "controller", state_path=state)
            lease = controller.acquire()
            ledger = controller.record_session_event(
                event="completed", task_class="coding", task="task-1", phase="code",
                lease=lease,
            )
            self.assertEqual(ledger["completed_tasks"], ["task-1"])
            self.assertEqual(controller.session_ledger()["completed_tasks"], ["task-1"])
            rotated = controller.rotate_session_budget(lease=lease)
            self.assertEqual(rotated["generation"], 2)

    def test_cli_repeated_failure_halts_and_rotate_emits_resume_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            state_home = Path(tmp) / "state"
            common = [
                "controller", "progress", "--root", str(root),
                "--workflow-root", "af-root", "--event", "failure",
                "--task-class", "coding", "--approach", "same fix", "--json",
            ]
            with mock.patch.dict("os.environ", {"AGENTFLOW_STATE_HOME": str(state_home)}):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(common + ["--same-approach-limit", "3"]), 0)
                    self.assertEqual(cli.main(common), 0)
                status = io.StringIO()
                with redirect_stdout(status):
                    self.assertEqual(
                        cli.main([
                            "controller", "status", "--root", str(root),
                            "--workflow-root", "af-root", "--json",
                        ]),
                        0,
                    )
                status_value = json.loads(status.getvalue())
                self.assertEqual(status_value["state"], "idle")
                self.assertEqual(status_value["lease"]["epoch"], 1)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(common), 0)
                blocked = io.StringIO()
                with redirect_stdout(blocked):
                    self.assertEqual(
                        cli.main([
                            "controller", "status", "--root", str(root),
                            "--workflow-root", "af-root", "--json",
                        ]), 0,
                    )
                self.assertEqual(json.loads(blocked.getvalue())["state"], "blocked")

                ready = subprocess.CompletedProcess(
                    ["bd", "ready", "--json"], 0,
                    stdout=json.dumps([{"id": "af-task", "title": "Review", "status": "open"}]),
                    stderr="",
                )
                output = io.StringIO()
                with (
                    mock.patch.object(cli.beads_backend, "root_descendants", return_value=[{"id": "af-task"}]),
                    mock.patch.object(cli.beads_backend, "run", return_value=ready),
                    redirect_stdout(output),
                ):
                    self.assertEqual(
                        cli.main([
                            "controller", "rotate", "--root", str(root),
                            "--workflow-root", "af-root", "--json",
                        ]),
                        0,
                    )
                payload = json.loads(output.getvalue())
                self.assertIn("Resume existing Agentflow root af-root", payload["resume_prompt"])
                packet = json.loads(Path(payload["packet"]).read_text(encoding="utf-8"))
                self.assertFalse(packet["transcript_included"])

    def test_terminal_checkpoint_prevents_beads_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            state_home = Path(tmp) / "state"
            with mock.patch.dict("os.environ", {"AGENTFLOW_STATE_HOME": str(state_home)}):
                args = cli.build_parser().parse_args([
                    "controller", "resume", "--root", str(root),
                    "--workflow-root", "af-root", "--once", "--json",
                ])
                controller, _ = cli._controller_instance(args)
                lease = controller.acquire()
                cli._controller_credentials(args, lease)
                controller.halt("blocked", "USER_ACTION_REQUIRED: stop", lease=lease)
                with (
                    mock.patch.object(cli.beads_backend, "get_issue", return_value={"id": "af-root"}),
                    mock.patch.object(cli.beads_backend, "claim_ready") as claim_ready,
                    mock.patch.object(cli.beads_backend, "root_descendants") as descendants,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli.controller_resume(args), 0)
                claim_ready.assert_not_called()
                descendants.assert_not_called()

    def test_herdr_evidence_requires_binding_and_consumed_channel(self) -> None:
        forged = {"result": {"outcome": "completed"}}
        issued = {
            "result": {"outcome": "completed"}, "binding": {"task_id": "t"},
            "return_channel": {"state": "issued"},
        }
        consumed = {
            "result": {"outcome": "completed"}, "binding": {"task_id": "t"},
            "return_channel": {"state": "consumed"},
        }
        self.assertFalse(cli._has_authenticated_herdr_result(forged))
        self.assertFalse(cli._has_authenticated_herdr_result(issued))
        self.assertTrue(cli._has_authenticated_herdr_result(consumed))


class CodeBurnReconciliationTests(unittest.TestCase):
    def test_codeburn_findings_remain_advisory_and_managed_assets_are_protected(self) -> None:
        report = {
            "findings": [
                {"id": "low-worth-sessions", "title": "one", "severity": "high", "estimatedSavingsUSD": 10},
                {"id": "unused-agents", "title": "two", "severity": "medium", "estimatedSavingsUSD": 1},
            ]
        }
        records = [
            {"project": "repo", "outcome": "completed", "checks": ["tests"], "files": 2},
            {"project": "other", "outcome": "failed"},
        ]
        value = reconcile_codeburn(
            report, records, project="repo",
            workflow_evidence={"terminal_descendants": 2, "authenticated_results": 1},
        )
        self.assertTrue(value["advisory_only"])
        self.assertEqual(value["agentflow_evidence"]["completed_records"], 1)
        self.assertEqual(value["agentflow_evidence"]["workflow"]["authenticated_results"], 1)
        by_id = {item["id"]: item for item in value["findings"]}
        self.assertEqual(by_id["low-worth-sessions"]["disposition"], "review-with-agentflow-evidence")
        self.assertEqual(by_id["unused-agents"]["disposition"], "do-not-auto-apply")
        self.assertIn("not verified", by_id["low-worth-sessions"]["savings_measurement"])


if __name__ == "__main__":
    unittest.main()
