import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agentflow import cli, project_config
from agentflow.memory_runtime import MemoryRuntime
from agentflow.search import KnowledgeDocument, KnowledgeIndex


class MemoryIntegrationTests(unittest.TestCase):
    def test_legacy_config_gets_disabled_memory_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = project_config.default_data()
            data.pop("memory")
            self.assertEqual(project_config.validate(data, root), [])
            self.assertFalse(project_config.memory_settings(data)["enabled"])

    def test_recall_is_governed_and_suppressed_until_compaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                settings = dict(project_config.DEFAULT_MEMORY)
                settings.update(enabled=True, on_prompt=True, scope_id="scope")
                runtime = MemoryRuntime(root, settings)
                timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
                with KnowledgeIndex(runtime.database) as index:
                    index.candidate(KnowledgeDocument(
                        "doc", "Agentflow", "governed memory", "docs/MEMORY.md",
                        freshness=timestamp, scope="project", scope_id="scope",
                    ))
                    index.approve("doc", approval_by="human:test", approval_ref="review-1", approved_at=timestamp)
                _, first, _ = runtime.process("codex", {"event": "SessionStart", "session_id": "s", "memory_query": "memory"}, "SessionStart")
                _, repeated, _ = runtime.process("codex", {"event": "UserPromptSubmit", "session_id": "s", "memory_query": "memory"}, "UserPromptSubmit")
                self.assertEqual(first.item_count, 1)
                self.assertEqual(repeated.item_count, 0)
                runtime.process("codex", {"event": "PreCompact", "session_id": "s"}, "PreCompact")
                _, after, _ = runtime.process("codex", {"event": "UserPromptSubmit", "session_id": "s", "memory_query": "memory"}, "UserPromptSubmit")
                self.assertEqual(after.item_count, 1)

    def test_hook_normalizes_metadata_and_fails_open(self):
        payload = {
            "hook_event_name": "PostToolUseFailure", "session_id": "raw-session",
            "tool_name": "shell", "tool_output": "do not store", "failure_id": "failure-1",
        }
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(payload)),
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.hook(argparse.Namespace(provider="claude", event="")), 0)
            stored = (Path(state) / "agentflow/events.jsonl").read_text()
            self.assertNotIn("do not store", stored)
            self.assertNotIn("raw-session", stored)
            self.assertIn("tool.failure", stored)


if __name__ == "__main__":
    unittest.main()
