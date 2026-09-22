from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from agentflow.events import (
    DuplicateEventError,
    EventPrivacyError,
    EventSpool,
    FailureClass,
    classify_failure,
    normalize_event,
)


class EventSpineTests(unittest.TestCase):
    def test_provider_variants_share_one_private_schema(self) -> None:
        fixtures = (
            ("claude", {"hook_event_name": "SessionStart", "session_id": "s-1", "model": "claude-sonnet", "prompt": "secret"}),
            ("codex", {"type": "session.start", "data": {"sessionId": "s-2", "selectedModel": "gpt-5", "reasoningEffort": "high"}}),
            ("copilot", {"event": "sessionStart", "sessionId": "s-3", "model": "copilot"}),
        )
        events = [normalize_event(provider, payload) for provider, payload in fixtures]
        self.assertEqual({event.event for event in events}, {"session.start"})
        self.assertEqual({event.privacy for event in events}, {"metadata-only"})
        self.assertNotIn("prompt", events[0].metadata)
        self.assertEqual(events[1].metadata["model"], "gpt-5")

    def test_prompt_event_rejected_and_credential_dropped(self) -> None:
        with self.assertRaises(EventPrivacyError):
            normalize_event("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "do not store"})
        with self.assertRaises(EventPrivacyError):
            normalize_event("codex", {"type": "session.start", "session_id": "s", "model": "api_key=secret"})

    def test_spool_orders_per_session_and_rejects_duplicate_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = EventSpool(Path(directory) / "events.jsonl")
            first = normalize_event("claude", {"event": "SessionStart", "session_id": "s"}, event_id="same")
            second = normalize_event("claude", {"event": "SessionStop", "session_id": "s"}, event_id="other")
            self.assertEqual(spool.append(first).sequence, 1)
            self.assertEqual(spool.append(second).sequence, 2)
            with self.assertRaises(DuplicateEventError):
                spool.append(first)
            self.assertEqual([event.sequence for event in spool.read()], [1, 2])

    def test_concurrent_appends_remain_valid_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            events = [normalize_event("copilot", {"event": "SessionStart", "session_id": "same"}, event_id=f"id-{index}") for index in range(30)]

            def write(event):
                return EventSpool(path).append(event)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, events))
            rows = EventSpool(path).read()
            self.assertEqual(len(rows), 30)
            self.assertEqual({row.event_id for row in rows}, {f"id-{index}" for index in range(30)})
            self.assertEqual(sorted(row.sequence for row in rows), list(range(1, 31)))
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertIsInstance(json.loads(line), dict)

    def test_retention_and_legacy_import_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            spool = EventSpool(directory / "spool.jsonl", max_events=2, max_bytes=2_000)
            for index in range(4):
                spool.append(normalize_event("codex", {"event": "SessionStart", "session_id": "s"}, event_id=f"e-{index}"))
            self.assertEqual([row.event_id for row in spool.read()], ["e-2", "e-3"])
            legacy = directory / "events.jsonl"
            legacy.write_text(json.dumps({"event": "SessionStart", "session_id": "legacy", "model": "m", "prompt": "private"}) + "\n", encoding="utf-8")
            self.assertEqual(spool.import_legacy(legacy, provider="claude"), 1)
            self.assertEqual(spool.read()[-1].session_id, "legacy")

    def test_spool_reads_existing_hook_rows_without_exposing_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                json.dumps({"timestamp": "2026-09-22T00:00:00Z", "provider": "codex", "event": "SessionStart", "session": "hashed", "model": "m", "prompt": "private"}) + "\n"
                + "not-json\n",
                encoding="utf-8",
            )
            rows = EventSpool(path).read()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].session_id, "hashed")
            self.assertNotIn("private", json.dumps(rows[0].to_dict()))

    def test_failure_taxonomy_has_no_payload_text(self) -> None:
        try:
            normalize_event("unknown", {})
        except Exception as error:  # noqa: BLE001 - taxonomy is the behavior under test.
            self.assertEqual(classify_failure(error), FailureClass.UNSUPPORTED_PROVIDER.value)
            self.assertNotIn("unknown", classify_failure(error))


if __name__ == "__main__":
    unittest.main()
