from __future__ import annotations

from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]


class AgentLedDocumentationTests(unittest.TestCase):
    def test_setup_contract_is_safe_and_discoverable(self) -> None:
        setup = (REPOSITORY / "AGENT_SETUP.md").read_text(encoding="utf-8")
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

        self.assertIn("saintdle-agentflow", setup)
        self.assertIn("agentflow install --dry-run", setup)
        self.assertIn("transactional legacy migration", setup)
        self.assertIn("Preserve unrelated", setup)
        self.assertIn("AGENT_SETUP.md", readme)
        self.assertNotIn("/Users/dean", setup)

    def test_chat_guide_preserves_approval_and_persistent_execution(self) -> None:
        guide = (REPOSITORY / "docs/CHAT_WORKFLOWS.md").read_text(encoding="utf-8")

        for required in (
            "Approved.",
            "without --once",
            "GOAL_COMPLETE",
            "USER_ACTION_REQUIRED",
            "Do not create a new root",
            "Do not claim, launch, resume, stop, edit, commit, push, or merge",
            "routine worker outputs between",
        ):
            self.assertIn(required, guide)
        self.assertNotIn("/Users/dean", guide)
        self.assertNotIn("gpt-5.6", guide.lower())

    def test_manual_tutorial_links_to_chat_first_path(self) -> None:
        tutorial = (REPOSITORY / "docs/FIRST_WORKFLOW.md").read_text(encoding="utf-8")
        installation = (REPOSITORY / "docs/INSTALLATION.md").read_text(encoding="utf-8")

        self.assertIn("CHAT_WORKFLOWS.md", tutorial)
        self.assertIn("AGENT_SETUP.md", installation)


if __name__ == "__main__":
    unittest.main()
