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

    def test_parallel_worker_limit_cannot_exceed_checkpoint_capacity(self) -> None:
        with self.assertRaisesRegex(execution.ExecutionPolicyError, "maximum supported parallel worker count"):
            execution.ExecutionPolicy(max_parallel_workers=9)


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

    def test_launch_attempt_accounting_collapses_lifecycle_events(self) -> None:
        pending = {
            "attempt": 1,
            "launch_id": "launch-one",
            "status": "identity_pending",
            "model": "gpt-6-sol",
            "role": "coding",
            "attempts": [
                {"attempt": 1, "launch_id": "launch-one", "status": "identity_pending"},
                {"attempt": 1, "launch_id": "launch-one", "status": "launched",
                 "resolved_from": "identity_pending"},
            ],
        }
        retry = {
            "attempt": 2,
            "attempts": [
                {"attempt": 1, "launch_id": "launch-one", "status": "failed"},
                {"attempt": 2, "launch_id": "launch-two", "status": "launched"},
            ],
        }
        reservation = {"attempt": 1, "attempts": [], "status": "launching"}
        failed_start = {
            "attempt": 1,
            "attempts": [{"attempt": 1, "launch_id": "launch-failed", "status": "failed"}],
            "status": "failed",
        }
        ambiguous = {
            "attempt": 1,
            "attempts": [{"attempt": 1, "launch_id": "launch-ambiguous", "status": "ambiguous"}],
            "status": "launching",
        }
        mixed_version_pending = {
            "attempt": 1,
            "attempts": [
                {"attempt": 1, "status": "identity_pending"},
                {"attempt": 1, "launch_id": "launch-one", "status": "launched",
                 "resolved_from": "identity_pending"},
            ],
        }
        legacy_pending_resolution = {
            "attempt": 1,
            "attempts": [
                {"attempt": 1, "status": "identity_pending"},
                {"attempt": 1, "status": "launched",
                 "resolved_from": "identity_pending"},
            ],
        }

        self.assertEqual(execution.summarize_attempts([pending]), (1, 1, 1))
        self.assertEqual(execution.summarize_attempts([retry])[0], 2)
        self.assertEqual(execution.summarize_attempts([reservation])[0], 1)
        self.assertEqual(execution.summarize_attempts([failed_start])[0], 1)
        self.assertEqual(execution.summarize_attempts([ambiguous])[0], 1)
        self.assertEqual(execution.summarize_attempts([mixed_version_pending])[0], 1)
        self.assertEqual(execution.summarize_attempts([legacy_pending_resolution])[0], 1)
        self.assertEqual(execution.summarize_attempts([{"attempts": [{}, {}, {}]}])[0], 3)

        report = context_budget.add_execution_attempts(
            context_budget.audit([], days=30), {"task-1": pending},
            policy=execution.ExecutionPolicy(max_attempts_per_task=1),
        )
        self.assertEqual(report["execution"]["recorded_attempts"], 1)
        self.assertEqual(report["execution"]["active_workers"], 1)
        self.assertEqual(report["execution"]["expensive_execution_children"], 1)
        self.assertEqual(report["execution"]["tasks_over_attempt_budget"], 0)

    def test_attempt_accounting_rejects_conflicting_explicit_launch_ids(self) -> None:
        conflicts = {
            "same-number-different-ids": {
                "attempts": [
                    {"attempt": 1, "launch_id": "launch-one", "status": "identity_pending"},
                    {"attempt": 1, "launch_id": "launch-two", "status": "launched",
                     "resolved_from": "identity_pending"},
                ],
            },
            "numberless-different-ids": {
                "attempts": [
                    {"launch_id": "launch-one", "status": "identity_pending"},
                    {"launch_id": "launch-two", "status": "launched",
                     "resolved_from": "identity_pending"},
                ],
            },
            "reservation-conflicts-with-history": {
                "attempt": 1, "launch_id": "launch-two",
                "attempts": [
                    {"attempt": 1, "launch_id": "launch-one", "status": "identity_pending"},
                    {"attempt": 1, "launch_id": "launch-two", "status": "launched",
                     "resolved_from": "identity_pending"},
                ],
            },
            "record-conflicts-with-binding": {
                "attempt": 1, "launch_id": "launch-one",
                "binding": {"launch_id": "launch-two"}, "attempts": [],
            },
            "one-id-two-attempt-numbers": {
                "attempts": [
                    {"attempt": 1, "launch_id": "launch-one", "status": "failed"},
                    {"attempt": 2, "launch_id": "launch-one", "status": "failed"},
                ],
            },
        }
        for name, record in conflicts.items():
            with self.subTest(name=name), self.assertRaises(execution.AccountingIndeterminate):
                execution.attempt_count(record)

        report = context_budget.add_execution_attempts(
            context_budget.audit([], days=30),
            {"task-1": conflicts["same-number-different-ids"]},
            policy=execution.ExecutionPolicy(max_attempts_per_task=2),
        )
        self.assertIsNone(report["execution"]["recorded_attempts"])
        self.assertTrue(report["execution"]["accounting_indeterminate"])
        self.assertIn(
            "execution-accounting-indeterminate",
            {finding["id"] for finding in report["findings"]},
        )

    def test_numbered_history_preserves_attempt_highwater(self) -> None:
        history_only = {
            "attempts": [{"attempt": 3, "launch_id": "launch-three", "status": "failed"}],
        }
        self.assertEqual(execution.attempt_count(history_only), 3)
        self.assertEqual(execution.summarize_attempts([history_only])[0], 3)

    def test_corrupt_attempt_history_is_an_actionable_audit_finding(self) -> None:
        report = context_budget.add_execution_attempts(
            context_budget.audit([], days=30),
            {"task-1": {"attempt": 1, "attempts": "corrupt", "status": "running"}},
            policy=execution.ExecutionPolicy(),
        )

        self.assertTrue(report["execution"]["accounting_indeterminate"])
        self.assertIsNone(report["execution"]["recorded_attempts"])
        finding = next(item for item in report["findings"]
                       if item["id"] == "execution-accounting-indeterminate")
        self.assertEqual(finding["severity"], "high")
        self.assertTrue(finding["recommendation"])

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
                                              "output_tokens": 100, "reasoning_output_tokens": 30},
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


class CodexUsageProjectionTests(unittest.TestCase):
    session_id = "019fd336-dd42-7e22-894b-d969f2d90408"

    def _record(self, rows: list[dict[str, object]], *, version: str = "0.153.4") -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / f"rollout-{self.session_id}.jsonl"
            all_rows = [{
                "type": "session_meta", "timestamp": "2026-09-14T00:00:00Z", "payload": {
                    "id": self.session_id, "cwd": "/repo",
                    "timestamp": "2026-09-14T00:00:00Z", "client_version": version,
                },
            }, *rows, {"type": "event_msg", "payload": {"type": "task_complete"}}]
            path.write_text("".join(json.dumps(row) + "\n" for row in all_rows), encoding="utf-8")
            return history._codex_record(self.session_id, [path], home=Path(temp)).manifest_value()

    @staticmethod
    def _native(response_id: str, input_tokens: int, cached: int, output_tokens: int, reasoning: int) -> dict[str, object]:
        return {
            "type": "response_item", "payload": {
                "type": "token_usage_record", "response_id": response_id,
                "token_usage": {
                    "input_tokens": input_tokens, "cached_input_tokens": cached,
                    "output_tokens": output_tokens, "reasoning_output_tokens": reasoning,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
        }

    @staticmethod
    def _token_count(
        input_tokens: int, cached: int, output_tokens: int, reasoning: int,
        total_input: int, total_cached: int, total_output: int, total_reasoning: int,
    ) -> dict[str, object]:
        return {
            "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "model_context_window": 258400,
                    "last_token_usage": {
                        "input_tokens": input_tokens, "cached_input_tokens": cached,
                        "output_tokens": output_tokens, "reasoning_output_tokens": reasoning,
                        "total_tokens": input_tokens + output_tokens,
                    },
                    "total_token_usage": {
                        "input_tokens": total_input, "cached_input_tokens": total_cached,
                        "output_tokens": total_output,
                        "reasoning_output_tokens": total_reasoning,
                        "total_tokens": total_input + total_output,
                    },
                },
            },
        }

    def test_eight_request_views_reconcile_once_and_audit_uses_request_input(self) -> None:
        requests = [
            (18580, 9984, 173, 41), (21309, 18176, 400, 238),
            (24529, 9984, 392, 173), (26895, 20224, 145, 42),
            (28147, 26368, 160, 12), (28331, 27392, 257, 78),
            (29809, 27392, 228, 122), (31126, 29440, 265, 192),
        ]
        rows: list[dict[str, object]] = []
        total_input = total_cached = total_output = total_reasoning = 0
        for index, (inputs, cached, outputs, reasoning) in enumerate(requests, start=1):
            rows.append(self._native(f"synthetic-response-{index}", inputs, cached, outputs, reasoning))
            total_input += inputs
            total_cached += cached
            total_output += outputs
            total_reasoning += reasoning
            rows.append(self._token_count(
                inputs, cached, outputs, reasoning,
                total_input, total_cached, total_output, total_reasoning,
            ))
        rows.extend((rows[-2], rows[-1]))

        value = self._record(rows)
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "complete")
        self.assertEqual(usage["cross_check"], "matched")
        self.assertEqual(usage["source"], "codex-native-token-usage-record")
        self.assertEqual(usage["cross_check_source"], "codex-event-msg-token-count")
        self.assertEqual(usage["client_version"], "0.153.4")
        self.assertEqual(usage["input_tokens"], 208726)
        self.assertEqual(usage["cached_input_tokens"], 168960)
        self.assertEqual(usage["output_tokens"], 2020)
        self.assertEqual(usage["total_tokens"], 210746)
        self.assertEqual(usage["request_count"], 8)
        self.assertEqual(usage["max_request_input_tokens"], 31126)
        self.assertEqual(value["peak_context_tokens"], 31391)
        self.assertEqual(value["peak_context_semantics"], "input_plus_output_proxy")
        self.assertIn("duplicate_response_id_ignored", usage["diagnostics"])
        self.assertIn("duplicate_token_count_snapshot_ignored", usage["diagnostics"])

        report = context_budget.audit(
            [value], now=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
            thresholds=context_budget.ContextThresholds(context_pressure_percent=10),
        )
        pressure = next(item for item in report["findings"] if item["id"] == "context-pressure")
        self.assertEqual(pressure["basis"], "request_input")
        self.assertIn("12.0%", pressure["message"])
        self.assertEqual(report["context_pressure_basis"]["request_input"], 1)

    def test_cumulative_only_preserves_reported_cache_and_unknown_request_fields(self) -> None:
        value = self._record([{
            "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "total_token_usage": {
                        "input_tokens": 500, "cached_input_tokens": 320,
                        "output_tokens": 50, "total_tokens": 550,
                    },
                },
            },
        }])
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "partial")
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["cached_input_tokens"], 320)
        self.assertIsNone(usage["request_count"])
        self.assertIsNone(usage["max_request_input_tokens"])

    def test_missing_usage_is_unknown_and_legacy_manifest_peak_is_labeled_proxy(self) -> None:
        value = self._record([])
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "unavailable")
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["request_count"])
        long_version = self._record([], version="9" * 100)["usage_metadata"]
        self.assertIsNone(long_version["client_version"])

        old_manifest = {
            "bead_id": "history-safe-legacy", "source_id": "legacy",
            "started_at": "2026-09-14T00:00:00+00:00", "total_tokens": 210746,
            "context_window_tokens": 40000, "peak_context_tokens": 31391,
        }
        report = context_budget.audit(
            [old_manifest], now=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
        )
        pressure = next(item for item in report["findings"] if item["id"] == "context-pressure")
        self.assertEqual(pressure["basis"], "legacy_total_proxy")
        self.assertIn("78.5%", pressure["message"])

    def test_conflicting_request_and_event_views_are_ambiguous(self) -> None:
        value = self._record([
            self._native("synthetic-conflict", 101, 50, 9, 2),
            self._token_count(100, 50, 10, 2, 100, 50, 10, 2),
        ])
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "ambiguous")
        self.assertEqual(usage["cross_check"], "conflict")
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["max_request_input_tokens"])
        self.assertIn("usage_view_conflict", usage["diagnostics"])
        self.assertIn("request_view_conflict", usage["diagnostics"])

    def test_intermediate_cumulative_prefix_conflict_is_not_hidden_by_matching_final_total(self) -> None:
        value = self._record([
            self._native("synthetic-prefix-1", 100, 50, 10, 2),
            self._token_count(100, 50, 10, 2, 90, 50, 20, 2),
            self._native("synthetic-prefix-2", 200, 100, 20, 4),
            self._token_count(200, 100, 20, 4, 300, 150, 30, 6),
        ])
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "ambiguous")
        self.assertIn("usage_view_conflict", usage["diagnostics"])

    def test_conflicting_duplicate_response_id_is_ambiguous(self) -> None:
        value = self._record([
            self._native("synthetic-duplicate", 100, 50, 10, 2),
            self._native("synthetic-duplicate", 101, 50, 9, 2),
        ])
        usage = value["usage_metadata"]
        self.assertEqual(usage["availability"], "ambiguous")
        self.assertIsNone(usage["request_count"])
        self.assertIn("conflicting_duplicate_response_id", usage["diagnostics"])

    def test_cumulative_reset_and_malformed_request_are_ambiguous(self) -> None:
        first = self._token_count(500, 300, 50, 10, 500, 300, 50, 10)
        reset = self._token_count(400, 250, 40, 8, 400, 250, 40, 8)
        reset_value = self._record([first, reset])
        self.assertEqual(reset_value["usage_metadata"]["availability"], "ambiguous")
        self.assertIn("cumulative_counter_reset", reset_value["usage_metadata"]["diagnostics"])

        malformed = {
            "type": "response_item", "payload": {
                "type": "token_usage_record", "response_id": "synthetic-malformed",
                "token_usage": {"input_tokens": True, "output_tokens": 7},
            },
        }
        malformed_value = self._record([malformed])
        self.assertEqual(malformed_value["usage_metadata"]["availability"], "ambiguous")
        self.assertIn("malformed_response_usage", malformed_value["usage_metadata"]["diagnostics"])

        malformed_event = self._record([{
            "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": "invalid"},
            },
        }])
        self.assertEqual(malformed_event["usage_metadata"]["availability"], "ambiguous")
        self.assertIn("malformed_token_count_usage", malformed_event["usage_metadata"]["diagnostics"])


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
