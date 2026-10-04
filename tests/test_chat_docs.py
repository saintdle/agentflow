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

    def test_pull_request_ci_runs_bounded_real_pinned_beads_smoke(self) -> None:
        ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        integration = (REPOSITORY / ".github/workflows/integration.yml").read_text(encoding="utf-8")
        smoke = (REPOSITORY / "scripts/ci/integration_beads.sh").read_text(encoding="utf-8")

        self.assertRegex(ci, r"(?m)^  pull_request:\s*$")
        self.assertIn("beads-smoke:", ci)
        self.assertIn("if: github.event_name == 'pull_request'", ci)
        self.assertIn("timeout-minutes: 20", ci)
        self.assertIn('AGENTFLOW_INTEGRATION: "1"', ci)
        self.assertIn('CGO_ENABLED: "1"', ci)
        self.assertIn('GOFLAGS: "-tags=gms_pure_go"', ci)
        self.assertIn('go-version: "1.26.2"', ci)
        self.assertIn("go install github.com/steveyegge/beads/cmd/bd@v1.1.0", ci)
        self.assertIn("scripts/ci/integration_beads.sh", ci)
        self.assertIn('schedule:', integration)
        self.assertIn('workflow_dispatch:', integration)
        self.assertNotRegex(integration, r"(?m)^  pull_request:")
        self.assertIn('CGO_ENABLED: "1"', integration)
        self.assertIn('GOFLAGS: "-tags=gms_pure_go"', integration)
        self.assertIn('go install github.com/steveyegge/beads/cmd/bd@v1.1.0', integration)
        self.assertIn('Expected Beads 1.1.0', smoke)

    def test_setup_docs_explain_public_tagged_install_herdr_and_copilot(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        installation = (REPOSITORY / "docs/INSTALLATION.md").read_text(encoding="utf-8")
        setup = (REPOSITORY / "AGENT_SETUP.md").read_text(encoding="utf-8")
        chat = (REPOSITORY / "docs/CHAT_WORKFLOWS.md").read_text(encoding="utf-8")

        self.assertNotIn("Until the repository is public", readme)
        self.assertNotIn("while it remains private", installation)
        for required in (
            "uv tool install --reinstall",
            "pipx install --force",
            "@v0.0.6",
            "Herdr is required",
            "Agent mode",
            "terminal access",
        ):
            self.assertIn(required, installation + setup + chat + readme)


if __name__ == "__main__":
    unittest.main()
