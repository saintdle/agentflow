from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow import cli  # noqa: E402
from agentflow import execution  # noqa: E402
from agentflow import launch_budget  # noqa: E402


NOW = "2026-01-01T00:00:00Z"
WORKFLOW_ROOT = "wf-root"


def _frozen(tasks: int = 3, multiplier: int = 2) -> launch_budget.LaunchBaseline:
    return launch_budget.freeze(planned_tasks=tasks, multiplier=multiplier, now=NOW)


class BaselineRecordTests(unittest.TestCase):
    def test_freeze_records_original_graph_and_cap(self) -> None:
        baseline = _frozen(tasks=6, multiplier=2)
        self.assertEqual(baseline.original_tasks, 6)
        self.assertEqual(baseline.original_launch_budget, 12)
        self.assertEqual(baseline.current_cap, 12)
        self.assertEqual(launch_budget.parse(baseline.to_dict()), baseline)

    def test_absent_baseline_is_none(self) -> None:
        self.assertIsNone(launch_budget.parse(None))

    def test_parse_rejects_tampered_or_rewritten_history(self) -> None:
        revised = launch_budget.revise(
            _frozen(), new_cap=8, decision_ref="decision-1", reason="correction", consumed=6, now=NOW,
        )
        valid = revised.to_dict()
        cases: dict[str, Any] = {}
        cases["wrong schema"] = {**valid, "schema": "agentflow.launch-baseline@0"}
        cases["extra field"] = {**valid, "graph_tasks": 9}
        cases["reset cap"] = {**valid, "original_launch_budget": 20}
        lowered = copy.deepcopy(valid)
        lowered["revisions"][0]["new_cap"] = 5
        cases["cap lowered"] = lowered
        rewritten = copy.deepcopy(valid)
        rewritten["revisions"][0]["previous_cap"] = 7
        cases["previous cap rewritten"] = rewritten
        renumbered = copy.deepcopy(valid)
        renumbered["revisions"][0]["revision"] = 2
        cases["revision gap"] = renumbered
        second = launch_budget.revise(
            revised, new_cap=10, decision_ref="decision-2", reason="second correction", consumed=8, now=NOW,
        ).to_dict()
        second["revisions"][1]["consumed_at_revision"] = 1
        cases["consumption decreased"] = second
        early_decision = _frozen().to_dict()
        early_decision["approach_events"] = [{
            "approach": "a", "outcome": "decision", "task": "", "reference": "r", "recorded_at": NOW,
        }]
        cases["decision before stop"] = early_decision
        cases["boolean count"] = {**valid, "original_tasks": True}
        cases["multiline reason"] = copy.deepcopy(valid)
        cases["multiline reason"]["revisions"][0]["reason"] = "line\nbreak"
        cases["not a mapping"] = ["launch_baseline"]
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(launch_budget.LaunchBaselineError):
                    launch_budget.parse(value)

    def test_revision_must_raise_cap_and_cannot_lose_consumption(self) -> None:
        baseline = _frozen()
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.revise(baseline, new_cap=6, decision_ref="d", reason="r", consumed=6, now=NOW)
        revised = launch_budget.revise(baseline, new_cap=9, decision_ref="d", reason="r", consumed=6, now=NOW)
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.revise(revised, new_cap=12, decision_ref="d2", reason="r", consumed=5, now=NOW)
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.revise(revised, new_cap=12, decision_ref="", reason="r", consumed=7, now=NOW)

    def test_revisions_never_change_the_original_baseline(self) -> None:
        baseline = _frozen()
        for cap in (8, 10, 13):
            baseline = launch_budget.revise(
                baseline, new_cap=cap, decision_ref=f"d{cap}", reason="more work", consumed=cap - 2, now=NOW,
            )
        restored = launch_budget.parse(json.loads(json.dumps(baseline.to_dict())))
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.original_launch_budget, 6)
        self.assertEqual(restored.current_cap, 13)
        self.assertEqual([item.previous_cap for item in restored.revisions], [6, 8, 10])


class ApproachStopRuleTests(unittest.TestCase):
    def test_two_failures_across_replacement_tasks_stop_the_approach(self) -> None:
        baseline = launch_budget.record_approach(
            _frozen(), approach="patch-parser", outcome="failure", task="task-1", now=NOW,
        )
        self.assertEqual(launch_budget.admission_findings(baseline, approach="patch-parser"), [])
        baseline = launch_budget.record_approach(
            baseline, approach="patch-parser", outcome="failure", task="task-1-replacement", now=NOW,
        )
        findings = launch_budget.admission_findings(baseline, approach="patch-parser")
        self.assertEqual([item.id for item in findings], ["approach-decision-required"])
        self.assertEqual(launch_budget.admission_findings(baseline, approach="other"), [])
        self.assertEqual(launch_budget.admission_findings(baseline), [])

    def test_owner_decision_unblocks_and_the_count_restarts(self) -> None:
        baseline = _frozen()
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.record_approach(
                baseline, approach="a", outcome="decision", reference="decision-1", now=NOW,
            )
        for task in ("t1", "t2"):
            baseline = launch_budget.record_approach(baseline, approach="a", outcome="failure", task=task, now=NOW)
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.record_approach(baseline, approach="a", outcome="decision", reference="", now=NOW)
        baseline = launch_budget.record_approach(
            baseline, approach="a", outcome="decision", reference="decision-1", now=NOW,
        )
        self.assertEqual(launch_budget.admission_findings(baseline, approach="a"), [])
        baseline = launch_budget.record_approach(baseline, approach="a", outcome="failure", task="t3", now=NOW)
        state = baseline.approach_state("a")
        self.assertEqual((state.failures, state.decisions, state.failures_since_decision), (3, 1, 1))
        self.assertFalse(state.blocked)
        self.assertEqual(launch_budget.parse(baseline.to_dict()), baseline)

    def test_failure_requires_the_failed_task(self) -> None:
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.record_approach(_frozen(), approach="a", outcome="failure", task="", now=NOW)
        with self.assertRaises(launch_budget.LaunchBaselineError):
            launch_budget.record_approach(_frozen(), approach="a", outcome="retry", task="t", now=NOW)


class EvaluateLaunchCapTests(unittest.TestCase):
    policy = execution.ExecutionPolicy(
        max_parallel_workers=3, max_attempts_per_task=5, launch_budget_multiplier=2,
        max_expensive_execution_children=0,
    )

    def _admission(self, *, total: int, planned: int, cap: int | None) -> execution.AdmissionReport:
        return execution.evaluate_launch(
            policy=self.policy, model="gpt-5.3-codex", role="coding", task_attempt=1,
            total_attempts=total, planned_tasks=planned, active_workers=0, launch_cap=cap,
        )

    def test_threshold_below_at_and_above_the_frozen_cap(self) -> None:
        below = self._admission(total=5, planned=40, cap=6)
        self.assertTrue(below.allowed)
        self.assertEqual(below.root_launch_budget, 6)
        for total in (6, 7):
            with self.subTest(total=total):
                report = self._admission(total=total, planned=40, cap=6)
                self.assertFalse(report.allowed)
                self.assertEqual([item.id for item in report.findings], ["launch-reforecast-required"])

    def test_graph_growth_does_not_raise_a_frozen_cap(self) -> None:
        self.assertTrue(self._admission(total=6, planned=40, cap=None).allowed)
        self.assertFalse(self._admission(total=6, planned=40, cap=6).allowed)

    def test_legacy_budget_finding_is_unchanged_without_a_cap(self) -> None:
        report = self._admission(total=6, planned=3, cap=None)
        self.assertEqual([item.id for item in report.findings], ["root-launch-budget-exhausted"])


class ReforecastTests(unittest.TestCase):
    @staticmethod
    def _item(item_id: str, status: str = "open", blocks: tuple[str, ...] = (), labels: tuple[str, ...] = ()) -> dict[str, Any]:
        return {
            "id": item_id, "status": status, "labels": list(labels),
            "dependencies": [
                {"issue_id": item_id, "depends_on_id": target, "type": "blocks"} for target in blocks
            ] + [{"issue_id": item_id, "depends_on_id": WORKFLOW_ROOT, "type": "parent-child"}],
        }

    def test_separates_launches_concurrency_open_work_serial_path_and_waits(self) -> None:
        baseline = launch_budget.revise(
            _frozen(tasks=3, multiplier=2), new_cap=8, decision_ref="d", reason="r", consumed=6, now=NOW,
        )
        descendants = [
            self._item("a", status="closed"),
            self._item("b", blocks=("a",)),
            self._item("c", blocks=("b",)),
            self._item("d", status="blocked", blocks=("c",)),
            self._item("e", labels=("af:stage:ci",)),
        ]
        report = launch_budget.reforecast(
            baseline, root=WORKFLOW_ROOT, consumed=7, active=1, descendants=descendants,
        )
        self.assertEqual(report["cumulative_launches"], 7)
        self.assertEqual(report["active_concurrency"], 1)
        self.assertEqual(report["remaining_open_tasks"], 4)
        self.assertEqual(report["remaining_serial_path"], 3)
        self.assertEqual(report["external_wait_tasks"], ["d", "e"])
        self.assertEqual(report["original_threshold"], "above")
        self.assertEqual(report["current_threshold"], "below")
        self.assertTrue(report["next_launch_admitted_by_cap"])
        self.assertEqual(report["remaining_launches"], 1)
        self.assertIsNone(report["delivery_eta"])
        self.assertEqual(len(report["revisions"]), 1)

    def test_cycle_reports_serial_path_unavailable(self) -> None:
        report = launch_budget.reforecast(
            _frozen(), root=WORKFLOW_ROOT, consumed=6, active=0,
            descendants=[self._item("a", blocks=("b",)), self._item("b", blocks=("a",))],
        )
        self.assertIsNone(report["remaining_serial_path"])
        self.assertEqual(report["current_threshold"], "at")
        self.assertFalse(report["next_launch_admitted_by_cap"])

    def test_absent_baseline_recommends_freezing(self) -> None:
        report = launch_budget.reforecast(None, root=WORKFLOW_ROOT, consumed=2, active=0, descendants=[])
        self.assertEqual(report["baseline"], "absent")
        self.assertIsNone(report["next_launch_admitted_by_cap"])
        self.assertIn("agentflow budget freeze", report["recommendation"])


class BudgetCommandTests(unittest.TestCase):
    """Public ``agentflow budget`` commands against a fake Beads boundary."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".agentflow").mkdir()
        self.issues: dict[str, dict[str, Any]] = {
            WORKFLOW_ROOT: {"id": WORKFLOW_ROOT, "status": "open", "metadata": {"agentflow": {}}},
        }
        for index in range(1, 4):
            task_id = f"{WORKFLOW_ROOT}.{index}"
            self.issues[task_id] = {
                "id": task_id, "status": "open", "parent": WORKFLOW_ROOT,
                "metadata": {"agentflow": {"launch": {
                    "provider": "codex", "model": "gpt-5.3-codex", "effort": "medium", "role": "coding",
                }}},
                "dependencies": [],
            }
        self.writes: list[dict[str, Any]] = []
        policy = execution.ExecutionPolicy(
            max_parallel_workers=1, max_attempts_per_task=2, launch_budget_multiplier=2,
            max_expensive_execution_children=0,
        )
        for patcher in (
            mock.patch.object(cli.beads_backend, "get_issue", side_effect=self._get_issue),
            mock.patch.object(cli.beads_backend, "root_descendants", side_effect=self._descendants),
            mock.patch.object(cli.beads_backend, "update_agentflow_metadata", side_effect=self._update),
            mock.patch.object(cli, "_controller_execution_policy", return_value=policy),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _get_issue(self, _cwd: Path, issue_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.issues[issue_id])

    def _descendants(self, _cwd: Path, _root: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for key, item in self.issues.items() if key != WORKFLOW_ROOT]

    def _update(self, _cwd: Path, issue_id: str, values: dict[str, Any], *, snapshot: Any = None) -> None:
        if snapshot is not None:
            self.assertEqual(snapshot, self.issues[issue_id], "write must use the current snapshot")
        self.writes.append(copy.deepcopy(values))
        self.issues[issue_id]["metadata"]["agentflow"].update(copy.deepcopy(values))

    def _launches(self, attempts_by_task: dict[str, int]) -> None:
        sessions = {
            task_id: {
                "root": str(self.root), "workflow_root": WORKFLOW_ROOT, "task_id": task_id,
                "launch_id": f"{task_id}-{count}", "status": "failed",
                "attempts": [
                    {"attempt": number, "launch_id": f"{task_id}-{number}", "status": "failed"}
                    for number in range(1, count + 1)
                ],
            }
            for task_id, count in attempts_by_task.items()
        }
        cli._private_atomic_json(
            self.root / ".agentflow/herdr/sessions.json",
            {"schema": "agentflow.herdr", "version": 1, "sessions": sessions},
        )

    def _run(self, *argv: str) -> tuple[int, dict[str, Any]]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["budget", *argv, "--workflow-root", WORKFLOW_ROOT, "--cwd", str(self.root), "--json"])
        return code, json.loads(stdout.getvalue())

    def _stored(self) -> launch_budget.LaunchBaseline:
        baseline = launch_budget.parse(
            self.issues[WORKFLOW_ROOT]["metadata"]["agentflow"][launch_budget.METADATA_KEY]
        )
        assert baseline is not None
        return baseline

    def test_freeze_uses_launchable_tasks_and_refuses_a_second_freeze(self) -> None:
        code, payload = self._run("freeze")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["baseline"]["original_tasks"], 3)
        self.assertEqual(self._stored().original_launch_budget, 6)
        code, payload = self._run("freeze", "--planned-tasks", "10")
        self.assertEqual(code, 2)
        self.assertIn("already has a frozen baseline", payload["error"])
        self.assertEqual(len(self.writes), 1)

    def test_status_reports_cap_reached_after_retries_and_graph_growth(self) -> None:
        self.assertEqual(self._run("freeze")[0], 0)
        self._launches({f"{WORKFLOW_ROOT}.1": 2, f"{WORKFLOW_ROOT}.2": 2, f"{WORKFLOW_ROOT}.3": 2})
        for index in range(4, 9):
            task_id = f"{WORKFLOW_ROOT}.{index}"
            self.issues[task_id] = {**copy.deepcopy(self.issues[f"{WORKFLOW_ROOT}.1"]), "id": task_id}
        code, payload = self._run("status")
        self.assertEqual(code, 0, payload)
        report = payload["reforecast"]
        self.assertEqual(report["cumulative_launches"], 6)
        self.assertEqual(report["current_cap"], 6)
        self.assertEqual(report["original_threshold"], "at")
        self.assertFalse(report["next_launch_admitted_by_cap"])
        self.assertEqual(report["remaining_open_tasks"], 8)

    def test_revise_appends_with_observed_consumption(self) -> None:
        self.assertEqual(self._run("freeze")[0], 0)
        self._launches({f"{WORKFLOW_ROOT}.1": 2, f"{WORKFLOW_ROOT}.2": 2, f"{WORKFLOW_ROOT}.3": 2})
        code, payload = self._run(
            "revise", "--new-cap", "9", "--decision-ref", "owner-decision-7", "--reason", "two correction tasks",
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["revision"]["consumed_at_revision"], 6)
        self.assertEqual(payload["revision"]["previous_cap"], 6)
        stored = self._stored()
        self.assertEqual((stored.original_launch_budget, stored.current_cap), (6, 9))
        code, payload = self._run(
            "revise", "--new-cap", "9", "--decision-ref", "owner-decision-8", "--reason", "again",
        )
        self.assertEqual(code, 2)
        self.assertIn("must exceed the current cap 9", payload["error"])

    def test_approach_records_stop_and_decision(self) -> None:
        self.assertEqual(self._run("freeze")[0], 0)
        for task in (f"{WORKFLOW_ROOT}.1", f"{WORKFLOW_ROOT}.2"):
            code, payload = self._run(
                "approach", "--approach", "patch-parser", "--outcome", "failure", "--task", task,
            )
            self.assertEqual(code, 0, payload)
        self.assertTrue(payload["approach"]["blocked"])
        code, payload = self._run(
            "approach", "--approach", "patch-parser", "--outcome", "decision", "--decision-ref", "owner-ok",
        )
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["approach"]["blocked"])

    def test_commands_other_than_freeze_and_status_require_a_baseline(self) -> None:
        code, payload = self._run("revise", "--new-cap", "9", "--decision-ref", "d", "--reason", "r")
        self.assertEqual(code, 2)
        self.assertIn("run agentflow budget freeze first", payload["error"])
        code, payload = self._run("status")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["reforecast"]["baseline"], "absent")
        self.assertEqual(self.writes, [])

    def test_malformed_stored_baseline_is_rejected(self) -> None:
        self.issues[WORKFLOW_ROOT]["metadata"]["agentflow"][launch_budget.METADATA_KEY] = {"schema": "other"}
        code, payload = self._run("status")
        self.assertEqual(code, 2)
        self.assertIn(launch_budget.SCHEMA, payload["error"])

    def test_corrupt_launch_history_is_rejected(self) -> None:
        self.assertEqual(self._run("freeze")[0], 0)
        state = self.root / ".agentflow/herdr/sessions.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{not json", encoding="utf-8")
        code, payload = self._run("status")
        self.assertEqual(code, 2)
        self.assertIn("root launch accounting is unavailable", payload["error"])

    def test_directory_without_workspace_state_cannot_report_zero_launches(self) -> None:
        self.assertEqual(self._run("freeze")[0], 0)
        (self.root / ".agentflow").rmdir()
        for argv in (("status",), ("revise", "--new-cap", "9", "--decision-ref", "d", "--reason", "r")):
            with self.subTest(argv[0]):
                code, payload = self._run(*argv)
                self.assertEqual(code, 2)
                self.assertIn("no Agentflow workspace state", payload["error"])
        self.assertEqual(len(self.writes), 1)

    def test_write_refuses_a_root_changed_after_it_was_read(self) -> None:
        reads = 0
        original = self._get_issue

        def changing_get_issue(cwd: Path, issue_id: str) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            if issue_id == WORKFLOW_ROOT and reads == 2:
                self.issues[WORKFLOW_ROOT]["metadata"]["agentflow"]["other_key"] = "changed"
            return original(cwd, issue_id)

        with mock.patch.object(cli.beads_backend, "get_issue", side_effect=changing_get_issue):
            code, payload = self._run("freeze")
        self.assertEqual(code, 2)
        self.assertIn("root metadata changed", payload["error"])
        self.assertEqual(self.writes, [])

    def test_write_detects_a_lost_or_clobbered_update(self) -> None:
        with mock.patch.object(cli.beads_backend, "update_agentflow_metadata"):
            code, payload = self._run("freeze")
        self.assertEqual(code, 2)
        self.assertIn("read-back does not match", payload["error"])


if __name__ == "__main__":
    unittest.main()
