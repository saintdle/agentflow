"""Regressions for unattended first-run provider startup."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli


class HerdrStartupTests(unittest.TestCase):
    def test_stopped_server_is_started_before_provider_spawn(self) -> None:
        replies = iter((
            subprocess.CompletedProcess(["herdr", "status", "server"], 0, "status: not running\n", ""),
            subprocess.CompletedProcess(["herdr", "status", "server"], 0, "status: running\ncompatible: yes\n", ""),
        ))
        with mock.patch.object(cli.subprocess, "run", side_effect=lambda *a, **k: next(replies)) as run, \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            result = cli._ensure_herdr_server("/usr/bin/herdr")
        self.assertTrue(result)
        popen.assert_called_once()
        self.assertEqual(run.call_count, 2)

    def test_running_server_does_not_start_another(self) -> None:
        running = subprocess.CompletedProcess(
            ["herdr", "status", "server"], 0, "status: running\ncompatible: yes\n", "",
        )
        with mock.patch.object(cli.subprocess, "run", return_value=running), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            self.assertFalse(cli._ensure_herdr_server("/usr/bin/herdr"))
        popen.assert_not_called()

    def test_missing_codex_integration_fails_before_launch(self) -> None:
        missing = subprocess.CompletedProcess(
            ["herdr", "integration", "status"], 0,
            "codex: not installed (/example/herdr-agent-state.sh)\n", "",
        )
        with mock.patch.object(cli.subprocess, "run", return_value=missing):
            with self.assertRaisesRegex(ValueError, "Herdr Codex integration is not current"):
                cli._require_herdr_provider_integration("/usr/bin/herdr", "codex")

    def test_current_codex_integration_is_accepted(self) -> None:
        current = subprocess.CompletedProcess(
            ["herdr", "integration", "status"], 0, "codex: current (v7)\n", "",
        )
        with mock.patch.object(cli.subprocess, "run", return_value=current):
            cli._require_herdr_provider_integration("/usr/bin/herdr", "codex")


class CodexTrustPromptTests(unittest.TestCase):
    def test_wrapped_actual_prompt_becomes_bounded_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / ".agentflow/herdr/sessions.json"
            cli._private_atomic_json(path, {"schema": "agentflow.herdr", "version": 1, "sessions": {
                "task-1": {"provider": "codex", "status": "identity_pending",
                           "pane_id": "pane-smoke", "execution_root": str(root)},
            }})
            pane = subprocess.CompletedProcess(
                ["herdr", "pane", "read"], 0,
                "> You are in /\nDo you trust\nthe contents\nof this\ndirectory?\n", "",
            )
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/herdr"), \
                 mock.patch.object(cli.subprocess, "run", return_value=pane) as run:
                attention = cli._codex_trust_attention(root, "task-1")
            self.assertEqual(attention["code"], "codex_project_trust_required")
            self.assertEqual(attention["path"], str(root))
            self.assertEqual(attention["pane_id"], "pane-smoke")
            self.assertEqual(run.call_args.args[0][1:4], ["pane", "read", "pane-smoke"])
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["sessions"]["task-1"]["startup_attention"], attention)
            self.assertNotIn("You are in", json.dumps(state))

    def test_status_finds_attention_in_parallel_active_task(self) -> None:
        attention = {"code": "codex_project_trust_required", "pane_id": "pane-smoke"}
        checkpoint = {"task": "/workspace", "active_tasks": [
            {"task": "task-1", "state": "identity_pending"},
        ]}
        sessions = {"task-1": {"status": "identity_pending", "startup_attention": attention}}
        self.assertEqual(cli._pending_startup_attention(checkpoint, sessions), attention)

    def test_unrelated_pane_text_does_not_create_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / ".agentflow/herdr/sessions.json"
            cli._private_atomic_json(path, {"schema": "agentflow.herdr", "version": 1, "sessions": {
                "task-1": {"provider": "codex", "status": "identity_pending", "pane_id": "pane-smoke"},
            }})
            pane = subprocess.CompletedProcess(["herdr", "pane", "read"], 0, "Codex is working", "")
            with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/herdr"), \
                 mock.patch.object(cli.subprocess, "run", return_value=pane):
                self.assertIsNone(cli._codex_trust_attention(root, "task-1"))
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("startup_attention", state["sessions"]["task-1"])


if __name__ == "__main__":
    unittest.main()
