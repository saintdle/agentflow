from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentflow import beads, cli
from agentflow.context_delivery import (
    BEADS_GUARD,
    MEMORY_PREFIX,
    classify_synthetic_observation,
    plan_context,
    serialize_context,
)
from agentflow.memory import RecallItem
from agentflow.memory_runtime import MemoryRuntime
from agentflow.project_config import DEFAULT_MEMORY, default_data, memory_settings
from agentflow.search import KnowledgeDocument, KnowledgeIndex


ROOT = Path(__file__).resolve().parents[1]


def _item(document_id: str, *, title: str = "Title", summary: str = "Summary", source: str = "docs/note.md") -> RecallItem:
    return RecallItem(
        document_id=document_id,
        title=title,
        summary=summary,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        scope="project",
        scope_id="scope",
        authority=90,
        provenance={},
    )


def _template_events(path: Path) -> set[str]:
    return set(json.loads(path.read_text(encoding="utf-8"))["hooks"])


class ContextDeliveryTests(unittest.TestCase):
    def _seed_recall(self, root: Path, state: Path) -> dict[str, object]:
        config = default_data()
        config["memory"].update(enabled=True, startup_query="synthetic recall")
        config_path = root / ".agentflow/config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
            runtime = MemoryRuntime(root, memory_settings(config))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            with KnowledgeIndex(runtime.database) as index:
                index.add(KnowledgeDocument(
                    "synthetic-retry-doc", "Synthetic recall title", "synthetic recall summary",
                    "docs/synthetic-recall.md", authority=90, last_verified_at=now,
                ))
                index.approve(
                    "synthetic-retry-doc", approval_by="human:test", approval_ref="review-1", approved_at=now,
                )
        return config

    def _run_hook(
        self, root: Path, state: Path, payload: dict[str, object], *, output: object | None = None,
        guidance: str | None = None,
    ) -> str:
        stdout = output if output is not None else io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(payload)),
        ), mock.patch("sys.stdout", stdout), mock.patch.object(cli.beads_backend, "prime", return_value=""):
            if guidance is None:
                cli.hook(SimpleNamespace(provider="codex", event=""))
            else:
                original = plan_context
                with mock.patch.object(
                    cli.context_delivery_backend, "plan_context",
                    side_effect=lambda provider, event, **kwargs: original(
                        provider, event, guidance=guidance, **kwargs,
                    ),
                ):
                    cli.hook(SimpleNamespace(provider="codex", event=""))
        return stdout.getvalue() if isinstance(stdout, io.StringIO) else ""

    @staticmethod
    def _receipt_rows(state: Path) -> list[dict[str, object]]:
        path = next((state / "memory").rglob("injections.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_all_bundled_template_events_are_explicitly_classified(self) -> None:
        templates = {
            "codex": ("templates/user/codex-hooks.json", "src/agentflow/resources/templates/user/codex-hooks.json"),
            "claude": ("templates/project/claude-settings.json", "src/agentflow/resources/templates/project/claude-settings.json"),
            "copilot": ("templates/project/copilot-hooks.json", "src/agentflow/resources/templates/project/copilot-hooks.json"),
        }
        expected = {
            "codex": {"SessionStart", "Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"},
            "claude": {"SessionStart", "PostModelSwitch", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact", "Stop"},
            "copilot": {"sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse", "postToolUseFailure", "preCompact", "agentStop"},
        }
        for provider, paths in templates.items():
            present = []
            for relative in paths:
                path = ROOT / relative
                if path.exists():
                    present.append(_template_events(path))
            self.assertTrue(present)
            self.assertTrue(all(events == present[0] for events in present))
            self.assertTrue(present[0].issubset(expected[provider]))
            for event in present[0]:
                with self.subTest(provider=provider, event=event):
                    plan = plan_context(provider, event)
                    if provider == "copilot":
                        self.assertEqual(plan["status"], "ready")
                        self.assertEqual(serialize_context(plan).keys(), {"additionalContext"})
                        self.assertEqual(plan["cap_value"], None)
                    elif provider == "codex" and event in {"Stop", "PostToolUseFailure", "PreCompact"}:
                        self.assertEqual(plan["status"], "unsupported_event")
                        self.assertIsNone(serialize_context(plan))
                    elif provider == "claude" and event == "PreCompact":
                        self.assertEqual(plan["status"], "unsupported_event")
                        self.assertIsNone(serialize_context(plan))
                    else:
                        self.assertEqual(plan["status"], "ready")

    def test_codex_uses_documented_context_field_without_control_changes(self) -> None:
        plan = plan_context("codex", "SessionStart", guidance="plain guidance")
        self.assertEqual(serialize_context(plan), {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "plain guidance",
            },
        })
        for event in ("Stop", "PostToolUseFailure", "PreCompact", "unknown"):
            self.assertEqual(plan_context("codex", event)["status"], "unsupported_event")
            self.assertIsNone(serialize_context(plan_context("codex", event)))
        post_switch = plan_context("claude", "PostModelSwitch")
        self.assertEqual(serialize_context(post_switch)["hookSpecificOutput"]["hookEventName"], "PostModelSwitch")

    def test_unicode_and_exact_caps_measure_final_context(self) -> None:
        codex_exact = plan_context("codex", "SessionStart", guidance="é" * 1_000)
        self.assertEqual(codex_exact["status"], "ready")
        self.assertEqual(codex_exact["context_bytes"], 2_000)
        self.assertEqual(codex_exact["context_characters"], 1_000)
        self.assertEqual(plan_context("codex", "SessionStart", guidance="é" * 1_001)["status"], "failed")
        claude_exact = plan_context("claude", "SessionStart", guidance="é" * 9_000)
        self.assertEqual(claude_exact["status"], "ready")
        self.assertEqual(claude_exact["context_characters"], 9_000)
        self.assertEqual(claude_exact["context_bytes"], 18_000)
        self.assertEqual(plan_context("claude", "SessionStart", guidance="é" * 9_001)["status"], "failed")

    def test_optional_prime_is_atomic_and_recall_keeps_whole_records(self) -> None:
        prime = "prime-content" * 40
        plan = plan_context("codex", "SessionStart", guidance="g" * 1_500, prime=prime)
        self.assertEqual(plan["status"], "ready")
        self.assertNotIn("prime-content", plan["text"])
        self.assertNotIn(BEADS_GUARD, plan["text"])
        self.assertEqual(plan["omitted"][0]["reason"], "over_budget")

        recall = plan_context("copilot", "sessionStart", memory_items=(
            _item("first", source="docs/first.md"), _item("second", source="docs/second.md"),
        ))
        self.assertEqual(recall["status"], "ready")
        self.assertEqual([part["kind"] for part in recall["included"]], ["guidance", "memory_guard", "memory_record", "memory_record"])
        self.assertIn(MEMORY_PREFIX, recall["text"])
        self.assertIn('"source":"docs/first.md"', recall["text"])
        self.assertIn('"source":"docs/second.md"', recall["text"])
        self.assertIn(_item("first", source="docs/first.md").source_digest, recall["text"])
        self.assertEqual(sum(part["characters"] for part in recall["included"]), recall["context_characters"])
        self.assertEqual(sum(part["bytes"] for part in recall["included"]), recall["context_bytes"])
        self.assertEqual(recall["context_sha256"], hashlib.sha256(recall["text"].encode()).hexdigest())
        self.assertIn("memory_guard", [part["kind"] for part in recall["included"]])
        memory_component = next(part for part in recall["included"] if part["kind"] == "memory_record")
        self.assertEqual(memory_component["source_digest"], _item("first", source="docs/first.md").source_digest)
        self.assertNotEqual(memory_component["digest"], memory_component["source_digest"])

    def test_no_recall_and_mandatory_overflow_are_explicit(self) -> None:
        no_recall = plan_context("codex", "SessionStart")
        self.assertNotIn(MEMORY_PREFIX, no_recall["text"])
        omitted_memory = plan_context(
            "codex", "SessionStart", guidance="g" * 1_700,
            memory_items=(_item("omitted", summary="full summary", source="docs/full-source.md"),),
        )
        memory_omission = next(part for part in omitted_memory["omitted"] if part["kind"] == "memory_record")
        self.assertEqual(memory_omission["reason"], "over_budget")
        self.assertEqual(memory_omission["source_digest"], hashlib.sha256(b"docs/full-source.md").hexdigest())
        self.assertNotEqual(memory_omission["digest"], memory_omission["source_digest"])
        oversized = plan_context(
            "codex", "SessionStart", guidance="g" * 2_001, prime="prime",
            memory_items=(_item("record"),),
        )
        self.assertEqual(oversized["status"], "failed")
        self.assertEqual(oversized["reason"], "mandatory_context_exceeds_cap")
        self.assertIsNone(oversized["text"])
        self.assertEqual([part["reason"] for part in oversized["omitted"]], ["mandatory_overflow"] * 3)

    def test_bundled_prime_helper_preserves_default_and_can_return_complete_block(self) -> None:
        with mock.patch.object(beads, "workspace", return_value={"_agentflow_gitless": False}), mock.patch.object(
            beads, "run", return_value=SimpleNamespace(returncode=0, stdout="prime" * 4_000)
        ):
            self.assertEqual(len(beads.prime(Path("/tmp/project"))), 12_000)
            self.assertEqual(len(beads.prime(Path("/tmp/project"), truncate=False)), 20_000)

    def test_synthetic_observation_classifications_never_claim_full_delivery(self) -> None:
        expected = "SYNTHETIC-CONTEXT-7d91"
        evidence = (
            ({"model_input": expected}, "model_input_payload_observed", "exact"),
            ({"model_input": "prefix " + expected + " suffix"}, "model_input_payload_observed", "contained"),
            ({"model_input": expected[:10]}, "truncated", "prefix"),
            ({"preview": expected[:8]}, "preview_only", "prefix"),
            ({"file_read": True}, "file_read_only", "none"),
            ({"provider_recorded": True}, "provider_recorded_only", "none"),
            ({"answer": "I saw it"}, "answer_only", "none"),
            ({}, "unknown_unobserved", "none"),
            ({"model_input": "unrelated observed context"}, "missing", "none"),
        )
        for kwargs, classification, match_scope in evidence:
            with self.subTest(classification=classification):
                result = classify_synthetic_observation(expected, **kwargs)
                self.assertEqual(result["classification"], classification)
                self.assertEqual(result["match_scope"], match_scope)
                self.assertFalse(result["full_delivery_proven"])
                serialized = json.dumps(result)
                self.assertNotIn(expected, serialized)
                self.assertNotIn("I saw it", serialized)

    def test_disabled_hook_writes_prepared_then_emitted_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            config_path = root / ".agentflow/config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps(default_data()), encoding="utf-8")
            payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "private-session"}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}), mock.patch(
                "sys.stdin", io.StringIO(json.dumps(payload)),
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, mock.patch.object(
                cli.beads_backend, "prime", return_value="",
            ):
                self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
            response = json.loads(stdout.getvalue())
            self.assertIn("hookSpecificOutput", response)
            receipt_path = next((state / "memory").rglob("injections.jsonl"))
            rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["prepared", "emitted"])
            self.assertTrue(all(row["schema"] == "agentflow.memory-receipt@2" for row in rows))
            self.assertTrue(all(row["recall_status"] == "disabled" for row in rows))
            self.assertNotIn("private-session", receipt_path.read_text(encoding="utf-8"))
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(rows[-1]["context_sha256"], hashlib.sha256(context.encode()).hexdigest())
            self.assertEqual(sum(part["characters"] for part in rows[-1]["included"]), len(context))
            self.assertEqual(sum(part["bytes"] for part in rows[-1]["included"]), len(context.encode()))

    def test_selected_recall_receipt_keeps_digest_provenance_not_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            config = default_data()
            config["memory"].update(enabled=True, startup_query="synthetic recall")
            config_path = root / ".agentflow/config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime_root = root
            payload = {
                "hook_event_name": "SessionStart", "cwd": str(root),
                "session_id": "private-session", "prompt": "private prompt text",
            }
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                from agentflow.memory_runtime import MemoryRuntime
                runtime = MemoryRuntime(runtime_root, memory_settings(config))
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with KnowledgeIndex(runtime.database) as index:
                    index.add(KnowledgeDocument(
                        "synthetic-doc", "Synthetic title", "synthetic recall summary", "private/source.md",
                        authority=90, last_verified_at=now,
                    ))
                    index.approve("synthetic-doc", approval_by="human:test", approval_ref="review-1", approved_at=now)
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch(
                    "sys.stdout", new_callable=io.StringIO,
                ) as stdout, mock.patch.object(cli.beads_backend, "prime", return_value=""):
                    self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
                response = json.loads(stdout.getvalue())
                context = response["hookSpecificOutput"]["additionalContext"]
                self.assertIn('"source":"private/source.md"', context)
                receipt_path = next((state / "memory").rglob("injections.jsonl"))
                receipts_text = receipt_path.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in receipts_text.splitlines()]
                self.assertEqual([row["stage"] for row in rows], ["prepared", "emitted"])
                self.assertTrue(all(row["recall_status"] == "selected" for row in rows))
                self.assertEqual(rows[-1]["selected_item_count"], 1)
                self.assertEqual(rows[-1]["retained_item_count"], 1)
                self.assertEqual(rows[-1]["omitted_item_count"], 0)
                self.assertEqual(rows[-1]["context_sha256"], hashlib.sha256(context.encode()).hexdigest())
                self.assertEqual(sum(part["characters"] for part in rows[-1]["included"]), len(context))
                self.assertEqual(sum(part["bytes"] for part in rows[-1]["included"]), len(context.encode()))
                memory_guard = next(part for part in rows[-1]["included"] if part["kind"] == "memory_guard")
                memory_record = next(part for part in rows[-1]["included"] if part["kind"] == "memory_record")
                self.assertEqual(memory_guard["characters"], len("\n\n" + MEMORY_PREFIX + "\n"))
                self.assertEqual(memory_record["source_digest"], hashlib.sha256(b"private/source.md").hexdigest())
                self.assertNotEqual(memory_record["source_digest"], memory_record["digest"])
                self.assertNotIn("private prompt text", receipts_text)
                self.assertNotIn("private/source.md", receipts_text)
                self.assertNotIn("Synthetic title", receipts_text)

    def test_empty_recall_and_unsupported_event_receipts_are_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            config = default_data()
            config["memory"].update(enabled=True, startup_query="no matching entry")
            config_path = root / ".agentflow/config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}), mock.patch.object(
                cli.beads_backend, "prime", return_value="",
            ):
                with mock.patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(root)}))), mock.patch(
                    "sys.stdout", new_callable=io.StringIO,
                ) as stdout:
                    self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
                self.assertTrue(json.loads(stdout.getvalue())["hookSpecificOutput"])

                unsupported = {
                    "hook_event_name": "PreCompact", "cwd": str(root),
                    "memory_query": "no matching entry",
                }
                with mock.patch("sys.stdin", io.StringIO(json.dumps(unsupported))), mock.patch(
                    "sys.stdout", new_callable=io.StringIO,
                ) as stdout:
                    self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
                self.assertEqual(stdout.getvalue(), "")

            receipt_path = next((state / "memory").rglob("injections.jsonl"))
            rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["prepared", "emitted", "omitted"])
            self.assertEqual(rows[0]["recall_status"], "empty")
            self.assertEqual(rows[-1]["event"], "PreCompact")
            self.assertEqual(rows[-1]["reason"], "event_has_no_documented_context_field")

    def test_inactive_supported_and_unsupported_hooks_record_receipts_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            config_path = root / ".agentflow/config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps(default_data()), encoding="utf-8")
            self._run_hook(root, state, {"hook_event_name": "UserPromptSubmit", "cwd": str(root)})
            self._run_hook(root, state, {"hook_event_name": "PreCompact", "cwd": str(root)})
            rows = self._receipt_rows(state)
            self.assertEqual([row["stage"] for row in rows], ["omitted", "omitted"])
            self.assertEqual([row["event"] for row in rows], ["UserPromptSubmit", "PreCompact"])
            self.assertEqual(rows[0]["reason"], "recall_disabled")
            self.assertEqual(rows[0]["recall_status"], "disabled")
            self.assertEqual(rows[1]["reason"], "event_has_no_documented_context_field")
            self.assertTrue(all(row["retained_item_count"] == 0 for row in rows))

    def test_hook_omission_releases_claim_retry_emits_and_then_suppresses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            self._seed_recall(root, state)
            payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "same-session"}

            self._run_hook(root, state, payload, guidance="g" * 1_900)
            first = self._receipt_rows(state)[-1]
            self.assertEqual(first["stage"], "emitted")
            self.assertEqual(first["retained_item_count"], 0)
            self.assertEqual(first["omitted_item_count"], 1)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                runtime = MemoryRuntime(root)
                with KnowledgeIndex(runtime.database) as index:
                    self.assertEqual(index.get("synthetic-retry-doc").injection_count, 0)

            retry_output = self._run_hook(root, state, payload)
            self.assertIn("synthetic recall summary", retry_output)
            rows = self._receipt_rows(state)
            self.assertEqual(rows[-1]["stage"], "emitted")
            self.assertEqual(rows[-1]["retained_item_count"], 1)
            self._run_hook(root, state, payload)
            final_rows = self._receipt_rows(state)
            self.assertEqual(final_rows[-1]["stage"], "emitted")
            self.assertEqual(final_rows[-1]["recall_status"], "empty")
            self.assertEqual(final_rows[-1]["retained_item_count"], 0)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                with KnowledgeIndex(runtime.database) as index:
                    self.assertEqual(index.get("synthetic-retry-doc").injection_count, 1)

    def test_hook_failed_write_releases_claim_for_retry(self) -> None:
        class BrokenOutput:
            def write(self, _value: str) -> int:
                raise BrokenPipeError("closed")

            def flush(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            self._seed_recall(root, state)
            payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "retry-session"}
            self._run_hook(root, state, payload, output=BrokenOutput())
            rows = self._receipt_rows(state)
            self.assertEqual([row["stage"] for row in rows], ["prepared", "failed"])
            self.assertEqual(rows[-1]["retained_item_count"], 0)
            self.assertEqual(rows[-1]["omitted_item_count"], 1)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                runtime = MemoryRuntime(root)
                with KnowledgeIndex(runtime.database) as index:
                    self.assertEqual(index.get("synthetic-retry-doc").injection_count, 0)
            retry_output = self._run_hook(root, state, payload)
            self.assertIn("synthetic recall summary", retry_output)
            self.assertEqual(self._receipt_rows(state)[-1]["stage"], "emitted")

    def test_runtime_claims_are_atomic_across_concurrent_hook_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            config = self._seed_recall(root, state)
            payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "race-session"}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                runtime = MemoryRuntime(root, memory_settings(config))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(runtime.process, "codex", payload, "SessionStart", record_usage=False) for _ in range(2)]
                    results = [future.result(timeout=10) for future in futures]
            self.assertEqual(sorted(len(plan.items) if plan is not None else 0 for _, plan, _ in results), [0, 1])

    def test_stale_cleanup_cannot_release_a_newer_session_reservation(self) -> None:
        with KnowledgeIndex() as index:
            digest = "a" * 64
            self.assertTrue(index.claim_injection("same-session", digest, "doc", reservation_id="old-plan"))
            index.reset_session("same-session")
            self.assertTrue(index.claim_injection("same-session", digest, "doc", reservation_id="new-plan"))
            self.assertEqual(index.release_injection("same-session", digest, "old-plan"), 0)
            self.assertFalse(index.claim_injection("same-session", digest, "doc", reservation_id="third-plan"))

    def test_legacy_session_claim_survives_concurrent_reservation_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "memory.sqlite3"
            with KnowledgeIndex(database):
                pass
            digest = "b" * 64
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE session_injections")
            connection.execute("""CREATE TABLE session_injections (
                session_id TEXT NOT NULL, source_digest TEXT NOT NULL, document_id TEXT NOT NULL,
                injected_at TEXT NOT NULL, PRIMARY KEY (session_id, source_digest)
            )""")
            connection.execute(
                "INSERT INTO session_injections VALUES (?, ?, ?, ?)",
                ("legacy-session", digest, "legacy-doc", "2026-10-06T00:00:00+00:00"),
            )
            connection.commit()
            connection.close()

            def open_and_claim() -> bool:
                with KnowledgeIndex(database) as index:
                    return index.claim_injection(
                        "legacy-session", digest, "legacy-doc", reservation_id="new-plan",
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: open_and_claim(), range(2)))
            self.assertEqual(results, [False, False])
            with KnowledgeIndex(database) as index:
                row = index.connection.execute(
                    "SELECT reservation_id FROM session_injections WHERE session_id = ? AND source_digest = ?",
                    ("legacy-session", digest),
                ).fetchone()
                self.assertEqual(row["reservation_id"], "")

    def test_component_inventory_covers_forty_and_one_hundred_records(self) -> None:
        for count in (40, 100):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "project"
                root.mkdir()
                state = Path(temp) / "state"
                components = [
                    {"id": f"memory_{i}", "kind": "memory_record", "digest": f"{i:064x}", "characters": 10, "bytes": 10}
                    for i in range(count)
                ]
                with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                    runtime = MemoryRuntime(root)
                    runtime.record_delivery({
                        "provider": "copilot", "event": "sessionStart", "stage": "emitted",
                        "included": components, "omitted": [], "retained_item_count": count,
                    })
                row = self._receipt_rows(state)[0]
                self.assertEqual(len(row["included"]), count)
                self.assertEqual(row["component_inventory"]["complete"], True)

    def test_component_inventory_reports_explicit_limit_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            components = [
                {"id": f"memory_{i}", "kind": "memory_record", "digest": f"{i:064x}", "characters": 1, "bytes": 1}
                for i in range(140)
            ]
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}):
                MemoryRuntime(root).record_delivery({
                    "provider": "copilot", "event": "sessionStart", "stage": "emitted",
                    "included": components, "omitted": [],
                })
            row = self._receipt_rows(state)[0]
            self.assertEqual(len(row["included"]), 128)
            self.assertEqual(row["component_inventory"]["included_input_count"], 140)
            self.assertEqual(row["component_inventory"]["included_recorded_count"], 128)
            self.assertFalse(row["component_inventory"]["complete"])
            self.assertEqual(row["component_inventory"]["diagnostic"], "included_limit_exceeded")

    def test_failed_write_does_not_create_emitted_receipt(self) -> None:
        class BrokenOutput:
            def write(self, _value: str) -> int:
                raise BrokenPipeError("closed")

            def flush(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            payload = {"hook_event_name": "SessionStart", "cwd": str(root)}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}), mock.patch(
                "sys.stdin", io.StringIO(json.dumps(payload)),
            ), mock.patch("sys.stdout", BrokenOutput()), mock.patch.object(
                cli.beads_backend, "prime", return_value="",
            ):
                self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
            receipt_path = next((state / "memory").rglob("injections.jsonl"))
            rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["prepared", "failed"])
            self.assertEqual(rows[-1]["reason"], "output_write_failed")

    def test_serialization_failure_is_recorded_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            state = Path(temp) / "state"
            payload = {"hook_event_name": "SessionStart", "cwd": str(root)}
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(state), "XDG_STATE_HOME": ""}), mock.patch(
                "sys.stdin", io.StringIO(json.dumps(payload)),
            ), mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch.object(
                cli.beads_backend, "prime", return_value="",
            ), mock.patch.object(cli.context_delivery_backend, "serialize_context", return_value={"bad": object()}):
                self.assertEqual(cli.hook(SimpleNamespace(provider="codex", event="")), 0)
            receipt_path = next((state / "memory").rglob("injections.jsonl"))
            rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["prepared", "failed"])
            self.assertEqual(rows[-1]["reason"], "serialization_failed")


if __name__ == "__main__":
    unittest.main()
