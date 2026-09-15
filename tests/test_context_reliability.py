from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import context_budget
from agentflow import cli
from agentflow import execution
from agentflow import guidance
from agentflow import history
from agentflow import model_policy
from agentflow import project_config
from agentflow import reconciliation


class ExecutionAdmissionTests(unittest.TestCase):
    def test_controller_only_and_expensive_execution_fail_closed(self) -> None:
        policy = execution.ExecutionPolicy()
        controller = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-sol", role="controller",
            task_attempt=1, total_attempts=0, planned_tasks=2,
        )
        self.assertFalse(controller.allowed)
        self.assertIn("controller-child-forbidden", {item.id for item in controller.findings})
        coding = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-sol", role="coding",
            task_attempt=1, total_attempts=0, planned_tasks=2,
        )
        self.assertFalse(coding.allowed)
        self.assertIn("expensive-execution-forbidden", {item.id for item in coding.findings})

    def test_luna_is_default_execution_and_terra_is_selective(self) -> None:
        policy = execution.ExecutionPolicy()
        luna = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-luna", role="coding",
            task_attempt=1, total_attempts=0, planned_tasks=1,
        )
        self.assertTrue(luna.allowed)
        terra = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-terra", role="coding",
            task_attempt=1, total_attempts=0, planned_tasks=1,
        )
        self.assertFalse(terra.allowed)
        selected = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-terra", role="coding",
            task_attempt=1, total_attempts=0, planned_tasks=1,
            selective_model=True,
        )
        self.assertTrue(selected.allowed)

    def test_graph_derived_launch_and_retry_budgets(self) -> None:
        policy = execution.ExecutionPolicy(max_attempts_per_task=2, launch_budget_multiplier=2)
        report = execution.evaluate_launch(
            policy=policy, model="gpt-5.6-luna", role="coding",
            task_attempt=3, total_attempts=6, planned_tasks=3,
        )
        self.assertFalse(report.allowed)
        self.assertEqual(report.root_launch_budget, 6)
        ids = {item.id for item in report.findings}
        self.assertIn("task-attempt-budget-exhausted", ids)
        self.assertIn("root-launch-budget-exhausted", ids)

    def test_root_policy_override_must_be_complete_and_typed(self) -> None:
        with self.assertRaises(execution.ExecutionPolicyError):
            execution.policy_from_root_metadata({"max_parallel_workers": 1})
        override = {"schema": execution.SCHEMA, **execution.ExecutionPolicy(
            max_parallel_workers=1, max_attempts_per_task=1,
        ).to_dict()}
        # to_dict already contains schema; dict expansion above remains exact.
        loaded = execution.policy_from_root_metadata(override)
        self.assertEqual(loaded.max_parallel_workers, 1)
        self.assertEqual(loaded.max_attempts_per_task, 1)


class ContextAuditTests(unittest.TestCase):
    def test_audit_uses_only_sanitized_metadata(self) -> None:
        now = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
        sessions = [
            {
                "bead_id": "history-safe-parent", "source_id": "parent",
                "started_at": "2026-09-14T00:00:00+00:00", "ended_at": "2026-09-14T01:00:00+00:00",
                "models": ["gpt-5.6-sol"], "efforts": ["xhigh"], "role": "controller",
                "total_tokens": 100, "context_window_tokens": 1000, "peak_context_tokens": 800,
                "prompt": "never persist this text",
            },
            {
                "bead_id": "history-safe-child", "source_id": "child", "parent_ref": "parent",
                "started_at": "2026-09-14T00:10:00+00:00", "ended_at": "2026-09-14T00:20:00+00:00",
                "models": ["gpt-5.6-sol"], "efforts": ["high"], "role": "worker",
                "delegation_depth": 2, "total_tokens": 200,
            },
        ]
        report = context_budget.audit(
            sessions, days=30, now=now,
            thresholds=context_budget.ContextThresholds(max_children_per_parent=1),
        )
        encoded = json.dumps(report)
        self.assertNotIn("never persist", encoded)
        ids = {item["id"] for item in report["findings"]}
        self.assertIn("context-pressure", ids)
        self.assertIn("delegation-depth-exceeded", ids)
        self.assertIn("expensive-execution-route", ids)
        self.assertEqual(report["efforts"], {"high": 1, "xhigh": 1})
        self.assertFalse(report["privacy"]["prompt_content_read"])
        self.assertTrue(context_budget.compaction_guidance(report)["recommended"])

    def test_herdr_attempt_metadata_is_bounded_and_audited(self) -> None:
        base = context_budget.audit([], days=30)
        report = context_budget.add_execution_attempts(
            base,
            {"task-1": {
                "attempts": [{}, {}, {}], "status": "running",
                "model": "gpt-5.6-luna", "role": "coding",
            }},
            policy=execution.ExecutionPolicy(max_attempts_per_task=2),
        )
        self.assertEqual(report["execution"]["recorded_attempts"], 3)
        self.assertEqual(report["execution"]["active_workers"], 1)
        self.assertIn(
            "task-attempt-budget-exceeded",
            {item["id"] for item in report["findings"]},
        )

    def test_codex_scanner_extracts_only_bounded_metadata(self) -> None:
        session_id = "019fd336-dd42-7e22-894b-d969f2d90404"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / f"rollout-{session_id}.jsonl"
            rows = [
                {"type": "session_meta", "timestamp": "2026-09-14T00:00:00Z", "payload": {
                    "id": session_id, "cwd": "/repo", "timestamp": "2026-09-14T00:00:00Z",
                    "thread_source": "subagent", "base_instructions": "private prompt",
                    "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": "019fd336-dd42-7e22-894b-d969f2d90405",
                        "depth": 1, "agent_role": "worker", "agent_path": "/private/name",
                    }}},
                }},
                {"type": "turn_context", "timestamp": "2026-09-14T00:01:00Z", "payload": {
                    "model": "gpt-5.6-luna", "effort": "high", "summary": "private summary",
                }},
                {"type": "event_msg", "timestamp": "2026-09-14T00:02:00Z", "payload": {
                    "type": "token_count", "info": {
                        "model_context_window": 1000,
                        "last_token_usage": {"total_tokens": 700},
                        "total_token_usage": {"total_tokens": 900, "input_tokens": 800,
                                              "output_tokens": 70, "reasoning_output_tokens": 30},
                    },
                }},
                {"type": "event_msg", "timestamp": "2026-09-14T00:03:00Z", "payload": {"type": "task_complete"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            record = history._codex_record(session_id, [path], home=Path(temp))
            value = record.manifest_value()
            self.assertEqual(value["models"], ["gpt-5.6-luna"])
            self.assertEqual(value["role"], "worker")
            self.assertEqual(value["delegation_depth"], 1)
            self.assertEqual(value["total_tokens"], 900)
            self.assertEqual(value["peak_context_tokens"], 700)
            encoded = json.dumps(value)
            self.assertNotIn("private prompt", encoded)
            self.assertNotIn("private summary", encoded)
            self.assertNotIn("/private/name", encoded)

    def test_claude_scanner_extracts_model_effort_and_usage_without_prose(self) -> None:
        session_id = "019fd336-dd42-7e22-894b-d969f2d90406"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / f"{session_id}.jsonl"
            rows = [
                {"type": "assistant", "sessionId": session_id, "timestamp": "2026-09-14T00:00:00Z",
                 "isSidechain": True,
                 "effort": "high", "message": {
                     "id": "msg-1", "model": "claude-sonnet-5", "content": "private prose",
                     "usage": {"input_tokens": 5, "cache_read_input_tokens": 20,
                               "cache_creation_input_tokens": 10, "output_tokens": 7},
                 }},
                # Duplicate message ids are stream snapshots, not additional usage.
                {"type": "assistant", "sessionId": session_id, "timestamp": "2026-09-14T00:01:00Z",
                 "effort": "high", "message": {"id": "msg-1", "model": "claude-sonnet-5",
                                                   "content": "more private prose",
                                                   "usage": {"output_tokens": 99}}},
                {"type": "result", "sessionId": session_id, "timestamp": "2026-09-14T00:02:00Z"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            value = history._claude_record(path, home=Path(temp)).manifest_value()
            self.assertEqual(value["models"], ["claude-sonnet-5"])
            self.assertEqual(value["efforts"], ["high"])
            self.assertEqual(value["role"], "worker")
            self.assertEqual(value["thread_source"], "subagent")
            self.assertEqual(value["input_tokens"], 35)
            self.assertEqual(value["output_tokens"], 7)
            self.assertEqual(value["total_tokens"], 42)
            self.assertNotIn("private prose", json.dumps(value))

    def test_copilot_cli_scanner_extracts_model_changes_without_messages(self) -> None:
        session_id = "019fd336-dd42-7e22-894b-d969f2d90407"
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / session_id
            session.mkdir()
            events = session / "events.jsonl"
            rows = [
                {"type": "session.start", "timestamp": "2026-09-14T00:00:00Z", "data": {
                    "sessionId": session_id, "selectedModel": "claude-sonnet-4.6",
                    "reasoningEffort": "medium", "context": "private prompt",
                }},
                {"type": "session.model_change", "timestamp": "2026-09-14T00:01:00Z", "data": {
                    "newModel": "claude-opus-4.8", "reasoningEffort": "high",
                }},
                {"type": "session.shutdown", "timestamp": "2026-09-14T00:02:00Z"},
            ]
            events.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            value = history._copilot_cli_record(session, home=Path(temp)).manifest_value()
            self.assertEqual(value["models"], ["claude-sonnet-4.6", "claude-opus-4.8"])
            self.assertEqual(value["efforts"], ["medium", "high"])
            self.assertNotIn("private prompt", json.dumps(value))


class PolicyGuidanceAndReconciliationTests(unittest.TestCase):
    def test_new_policy_is_v2_and_legacy_v1_remains_immutable(self) -> None:
        current = model_policy.load_policy()
        legacy = model_policy.load_policy(
            Path(__file__).resolve().parents[1] / "policies/models-v1.json"
        )
        self.assertEqual(current.id, "models-v2")
        self.assertEqual(legacy.id, "models-v1")
        self.assertFalse(legacy.validate_route(
            provider="codex", role="coding", model="gpt-5.6-terra", effort="medium",
            selective=True,
        ).ok)

    def test_terra_policy_requires_explicit_selection(self) -> None:
        policy = model_policy.load_policy()
        rejected = policy.validate_route(
            provider="codex", role="coding", model="gpt-5.6-terra", effort="medium"
        )
        approved = policy.validate_route(
            provider="codex", role="coding", model="gpt-5.6-terra", effort="medium",
            selective=True,
        )
        self.assertFalse(rejected.ok)
        self.assertTrue(approved.ok)

    def test_project_defaults_are_backward_compatible(self) -> None:
        legacy = {
            "schema": project_config.SCHEMA, "version": project_config.VERSION,
            "model_policy": ".agentflow/models-v1.json", "skills": [],
        }
        self.assertEqual(project_config.validate(legacy, Path(".")), [])
        self.assertTrue(project_config.execution_settings(legacy)["controller_only"])
        self.assertFalse(project_config.guidance_settings(legacy)["verification"])

    def test_verification_guidance_never_claims_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            (root / "tests").mkdir()
            plan = guidance.verification_plan(root)
            self.assertEqual(plan["status"], "planned")
            self.assertNotIn("result", plan)
            self.assertNotIn("exit_code", plan)

    def test_lifecycle_reconciliation_surfaces_divergence(self) -> None:
        report = reconciliation.reconcile(
            [{"id": "task-1", "status": "closed"}, {"id": "task-2", "status": "in_progress"}],
            {"task-1": {"status": "identity_pending"}},
        )
        self.assertFalse(report["ok"])
        ids = {item["id"] for item in report["findings"]}
        self.assertEqual(ids, {"terminal-bead-active-session", "claim-without-session"})

    def test_cli_exposes_context_reconciliation_and_verification_commands(self) -> None:
        parser = cli.build_parser()
        audit_args = parser.parse_args(["context", "audit", "--days", "45"])
        self.assertIs(audit_args.func, cli.context_audit)
        self.assertEqual(audit_args.days, 45)
        compact_args = parser.parse_args(["context", "compact", "--root", "/tmp/work"])
        self.assertIs(compact_args.func, cli.context_compact)
        self.assertEqual(compact_args.root, "/tmp/work")
        reconcile_args = parser.parse_args([
            "herdr", "reconcile", "--workflow-root", "af-root",
        ])
        self.assertIs(reconcile_args.func, cli.herdr_reconcile)
        verify_args = parser.parse_args(["verify", "plan"])
        self.assertIs(verify_args.func, cli.verification_guide)


if __name__ == "__main__":
    unittest.main()
