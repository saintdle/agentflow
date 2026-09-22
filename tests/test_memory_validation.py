from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow.events import normalize_event
from agentflow.memory_runtime import MemoryRuntime, ReceiptSpool, state_home
from agentflow.project_config import DEFAULT_MEMORY
from agentflow.search import KnowledgeDocument, KnowledgeIndex


def _receipt_writer(path: str, start: int) -> None:
    spool = ReceiptSpool(Path(path), max_events=200, max_bytes=100_000, retention_days=30)
    for index in range(start, start + 10):
        spool.append({"timestamp": "2099-01-01T00:00:00Z", "event_id": f"e-{index}", "privacy": "metadata-only"})


class MemoryValidationTests(unittest.TestCase):
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
