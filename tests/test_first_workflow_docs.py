from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from agentflow import cli


REPOSITORY = Path(__file__).resolve().parents[1]


class FirstWorkflowDocumentationTests(unittest.TestCase):
    def test_documented_task_materializes_and_preflights_after_fresh_init(self) -> None:
        tutorial = (REPOSITORY / "docs/FIRST_WORKFLOW.md").read_text(encoding="utf-8")
        match = re.search(r"--metadata '([^']+)'", tutorial)
        self.assertIsNotNone(match, "tutorial must contain task metadata")
        metadata = json.loads(match.group(1))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            issue = {
                "id": "first-workflow-1",
                "title": "Write the first-run note",
                "description": "Create docs/first-run-note.md with one verified setup example.",
                "acceptance_criteria": (
                    "docs/first-run-note.md exists, is non-empty, and contains no private data."
                ),
                "status": "open",
                "metadata": metadata,
            }
            handoff = root / ".agentflow/tmp/handoffs/first-workflow-1-codex.md"
            from_bead = argparse.Namespace(
                bead=issue["id"], to="codex", cwd=str(root), task_class="implementation",
                role="", lane="", tool_profile="", output_boundary="",
                require_tool=[], require_skill=[], allow_delegation=False, return_type="result",
                max_ai_credits=None, base="", branch="",
                context=[], constraint=[], check=[], budget=[], out=str(handoff),
            )
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), mock.patch.object(
                cli.beads_backend, "update_agentflow_metadata"
            ), mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.handoff_from_bead(from_bead), 0)

            preflight = argparse.Namespace(file=str(handoff), cwd=str(root), require_matrix=False)
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), mock.patch.object(
                cli.beads_backend, "update_agentflow_metadata"
            ), mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                self.assertEqual(cli.handoff_preflight(preflight), 0)


if __name__ == "__main__":
    unittest.main()
