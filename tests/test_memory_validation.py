from __future__ import annotations

import datetime as dt
import argparse
import io
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow import cli, project_config
from agentflow.events import normalize_event
from agentflow.memory_runtime import MemoryRuntime, ReceiptSpool, _now, state_home
from agentflow.project_config import DEFAULT_MEMORY
from agentflow.search import KnowledgeDocument, KnowledgeIndex, SearchError


def _receipt_writer(path: str, start: int) -> None:
    spool = ReceiptSpool(Path(path), max_events=200, max_bytes=100_000, retention_days=30)
    for index in range(start, start + 10):
        spool.append({"timestamp": "2099-01-01T00:00:00Z", "event_id": f"e-{index}", "privacy": "metadata-only"})


class MemoryValidationTests(unittest.TestCase):
    def test_fitting_receipt_replaces_stale_overflow_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "injections.jsonl"
            spool = ReceiptSpool(path, max_events=10, max_bytes=512, retention_days=30)
            spool.append({
                "schema": "agentflow.memory-receipt@2",
                "timestamp": "2099-01-01T00:00:00Z",
                "padding": "x" * 600,
                "privacy": "metadata-only",
            })
            old_marker = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(old_marker["schema"], "agentflow.memory-receipt-storage@1")

            fitting_legacy = {
                "schema": "agentflow.memory-receipt@1",
                "timestamp": "2099-01-01T00:00:01Z",
                "session_id": "s" * 60,
                "event_id": "e" * 60,
                "items": 1,
                "characters": 20_000,
                "source_digests": ["a" * 64],
                "privacy": "metadata-only",
            }
            encoded_legacy = (
                json.dumps(fitting_legacy, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            self.assertEqual(len(encoded_legacy), 366)
            spool.append(fitting_legacy)

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertLessEqual(len(path.read_bytes()), 512)
            self.assertEqual([row["schema"] for row in rows], ["agentflow.memory-receipt@1"])
            self.assertEqual(spool.count(), 1)

    def test_hook_receipt_overflow_preserves_legacy_row_and_records_diagnostic(self) -> None:
        for cap in (256, 512):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as state:
                root = Path(state) / "repo"
                root.mkdir()
                config = project_config.default_data()
                config["memory"].update(
                    enabled=True, startup_query="blue comet", max_items=1,
                    max_event_bytes=cap,
                )
                config_path = project_config.config_path(root)
                config_path.parent.mkdir(parents=True)
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": state}, clear=False):
                    runtime = MemoryRuntime(root, project_config.memory_settings(config))
                    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                    with KnowledgeIndex(runtime.database) as index:
                        index.candidate(KnowledgeDocument(
                            "approved", "Blue comet", "blue comet memory", "docs/source", freshness=stamp,
                        ))
                        index.approve(
                            "approved", approval_by="human:test", approval_ref="review-1",
                            approved_at=stamp,
                        )
                    if cap == 512:
                        legacy = {
                            "schema": "agentflow.memory-receipt@1",
                            "timestamp": (
                                (_now() - dt.timedelta(seconds=1))
                                .isoformat(timespec="seconds").replace("+00:00", "Z")
                            ),
                            "session_id": "s" * 52,
                            "event_id": "e" * 52,
                            "items": 1,
                            "characters": 2000,
                            "source_digests": ["a" * 64],
                            "privacy": "metadata-only",
                        }
                        legacy_bytes = (
                            json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n"
                        ).encode("utf-8")
                        self.assertEqual(len(legacy_bytes), 349)
                        runtime.receipts_path.write_bytes(legacy_bytes)

                    payload = {
                        "hook_event_name": "SessionStart", "session_id": f"small-cap-{cap}",
                        "cwd": str(root),
                    }
                    with mock.patch.object(cli.beads_backend, "prime", return_value=""), \
                         mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                         mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)

                    response = json.loads(stdout.getvalue())
                    self.assertTrue(response["hookSpecificOutput"]["additionalContext"])
                    encoded_spool = runtime.receipts_path.read_bytes()
                    self.assertLessEqual(len(encoded_spool), cap)
                    rows = [json.loads(line) for line in encoded_spool.splitlines()]
                    diagnostic = next(
                        row for row in rows
                        if row.get("schema") == "agentflow.memory-receipt-storage@1"
                    )
                    self.assertEqual(sum(
                        row.get("schema") == "agentflow.memory-receipt-storage@1" for row in rows
                    ), 1)
                    self.assertFalse(any(row.get("schema") == "agentflow.memory-receipt@2" for row in rows))
                    self.assertEqual(diagnostic["status"], "unavailable")
                    self.assertEqual(diagnostic["reason"], "receipt_exceeds_cap")
                    if cap == 512:
                        self.assertEqual(len(encoded_spool), cap)
                        self.assertTrue(any(row.get("schema") == "agentflow.memory-receipt@1" for row in rows))
                        self.assertEqual(runtime.receipts.count(), 1)
                    else:
                        self.assertEqual(runtime.receipts.count(), 0)

    def test_hook_receipt_marks_caught_requested_recall_failure_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            root = Path(state) / "repo"
            root.mkdir()
            config = project_config.default_data()
            config["memory"].update(enabled=True, startup_query="blue comet", max_items=2)
            config_path = project_config.config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": state}, clear=False):
                runtime = MemoryRuntime(root, project_config.memory_settings(config))
                stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                with KnowledgeIndex(runtime.database) as index:
                    for number in range(2):
                        document_id = f"doc-{number}"
                        index.candidate(KnowledgeDocument(
                            document_id, f"Blue comet {number}", f"blue comet memory {number}",
                            f"docs/{number}.md", freshness=stamp,
                        ))
                        index.approve(
                            document_id, approval_by="human:test", approval_ref=f"review-{number}",
                            approved_at=stamp,
                        )
                original_claim = KnowledgeIndex.claim_injection
                claim_count = 0

                def fail_second_claim(index, *args, **kwargs):
                    nonlocal claim_count
                    claim_count += 1
                    if claim_count == 2:
                        raise SearchError("synthetic second-claim failure")
                    return original_claim(index, *args, **kwargs)

                payload = {
                    "hook_event_name": "SessionStart", "session_id": "hook-failure",
                    "cwd": str(root),
                }
                with mock.patch.object(KnowledgeIndex, "claim_injection", fail_second_claim), \
                     mock.patch.object(cli.beads_backend, "prime", return_value=""), \
                     mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                     mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)

                rows = [json.loads(line) for line in runtime.receipts_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(rows[-1]["recall_status"], "unavailable")
                self.assertEqual(rows[-1]["stage"], "emitted")

    def test_partial_deferred_claim_failure_releases_plan_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            root = Path(state) / "repo"
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": state}, clear=False):
                settings = dict(DEFAULT_MEMORY, enabled=True, startup_query="blue comet", max_items=2)
                runtime = MemoryRuntime(root, settings)
                stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                with KnowledgeIndex(runtime.database) as index:
                    for number in range(2):
                        document_id = f"doc-{number}"
                        index.candidate(KnowledgeDocument(
                            document_id, f"Blue comet {number}", f"blue comet memory {number}",
                            f"docs/{number}.md", freshness=stamp,
                        ))
                        index.approve(
                            document_id, approval_by="human:test", approval_ref=f"review-{number}",
                            approved_at=stamp,
                        )

                original_claim = KnowledgeIndex.claim_injection
                claim_count = 0

                def fail_second_claim(index, *args, **kwargs):
                    nonlocal claim_count
                    claim_count += 1
                    if claim_count == 2:
                        raise SearchError("synthetic second-claim failure")
                    return original_claim(index, *args, **kwargs)

                with mock.patch.object(KnowledgeIndex, "claim_injection", fail_second_claim):
                    event, failed_plan, failed_status = runtime.process(
                        "codex", {"event": "SessionStart", "session_id": "retry-session"},
                        "SessionStart", record_usage=False,
                    )

                self.assertIsNotNone(event)
                self.assertIsNone(failed_plan)
                with KnowledgeIndex(runtime.database) as index:
                    held = index.connection.execute(
                        "SELECT COUNT(*) FROM session_injections WHERE session_id = ?",
                        (event.session_id,),
                    ).fetchone()[0]
                self.assertEqual(held, 0)
                self.assertEqual(failed_status.get("recall_status"), "unavailable")

                retry_event, retry_plan, retry_status = runtime.process(
                    "codex", {"event": "SessionStart", "session_id": "retry-session"},
                    "SessionStart", record_usage=False,
                )
                self.assertIsNotNone(retry_event)
                self.assertIsNotNone(retry_plan)
                self.assertEqual(retry_plan.item_count, 2)
                self.assertEqual(retry_status.get("recall_status"), "selected")
                runtime.finish_delivery(retry_event, retry_plan, retry_plan.items)
                with KnowledgeIndex(runtime.database) as index:
                    claims = index.connection.execute(
                        "SELECT COUNT(*) FROM session_injections WHERE session_id = ?",
                        (retry_event.session_id,),
                    ).fetchone()[0]
                self.assertEqual(claims, 2)

    def test_native_prompt_shapes_are_transient_and_recall_for_each_provider(self) -> None:
        fixtures = (
            ("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "claude", "prompt": "blue comet"}),
            ("codex", {"type": "UserPromptSubmit", "data": {"sessionId": "codex", "prompt": "blue comet"}}),
            ("copilot", {"event": "UserPromptSubmit", "sessionId": "copilot", "userPrompt": "blue comet"}),
        )
        for provider, payload in fixtures:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as state:
                root = Path(state) / "repo"
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": state}, clear=False):
                    settings = dict(DEFAULT_MEMORY, enabled=True, on_prompt=True, scope_id="scope")
                    runtime = MemoryRuntime(root, settings)
                    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                    with KnowledgeIndex(runtime.database) as index:
                        index.candidate(KnowledgeDocument("doc", "blue comet", "governed blue comet", "docs/source", freshness=stamp, scope="project", scope_id="scope"))
                        index.approve("doc", approval_by="human:test", approval_ref="review-1", approved_at=stamp)
                    name = payload.get("hook_event_name") or payload.get("type") or payload.get("event")
                    _, plan, _ = runtime.process(provider, payload, str(name))
                    self.assertIsNotNone(plan)
                    self.assertEqual(plan.item_count, 1)
                    serialized = "".join(path.read_bytes().decode("utf-8", errors="ignore") for path in runtime.directory.glob("*.jsonl"))
                    self.assertNotIn("blue comet", serialized)

    def test_tool_links_and_adversarial_metadata_are_stable(self) -> None:
        failure = normalize_event("codex", {"event": "PostToolUseFailure", "data": {"sessionId": "nested", "toolId": "/private/tool-id", "toolName": "/private/tool-name", "error": "permission denied /private/path"}, "tool_id": "/private/top-level-id", "tool_name": "/private/top-level-name", "reason": "/private/path", "source": "/private/source", "eventId": "/tmp/caller-id"})
        self.assertEqual(failure.metadata["failure_class"], "permission")
        self.assertRegex(failure.metadata["tool_id"], r"^tool_[0-9a-f]{64}$")
        self.assertRegex(failure.metadata["tool_name"], r"^tool_[0-9a-f]{64}$")
        self.assertNotIn("toolid", failure.metadata)
        self.assertNotIn("toolname", failure.metadata)
        self.assertNotIn("/private", json.dumps(failure.to_dict()))
        self.assertNotIn("/tmp/caller-id", failure.event_id)
        self.assertEqual(failure.to_dict(), type(failure).from_dict(failure.to_dict()).to_dict())

    def test_receipts_are_process_safe_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "injections.jsonl")
            processes = [multiprocessing.Process(target=_receipt_writer, args=(path, offset)) for offset in (0, 10, 20, 30)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            receipt = Path(path)
            self.assertLessEqual(len(receipt.read_bytes()), 100_000)
            rows = [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 40)
            self.assertEqual(ReceiptSpool(receipt, max_events=5, max_bytes=100_000, retention_days=30).prune(), 35)
            self.assertEqual(len(receipt.read_text(encoding="utf-8").splitlines()), 5)

    def test_state_home_override_is_shared(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as xdg:
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": directory, "XDG_STATE_HOME": xdg}, clear=False):
                self.assertEqual(state_home(), Path(directory))


if __name__ == "__main__":
    unittest.main()
