from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]


class DocumentationDiagramTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (REPOSITORY / relative).read_text(encoding="utf-8")

    def test_component_map_is_present_in_readme(self) -> None:
        readme = self._read("README.md")

        self.assertIn("## How the components fit together", readme)
        self.assertIn('Beads["Beads<br/>goals, graph, claims, decisions, evidence"]', readme)
        self.assertIn('Git["Git and GitHub<br/>review, CI, PR, merge"]', readme)

    def test_workflow_diagram_preserves_halt_and_remediation_paths(self) -> None:
        workflow = self._read("docs/WORKFLOW.md")

        self.assertIn("stateDiagram-v2", workflow)
        self.assertIn("Preflight --> UserActionRequired: blocked", workflow)
        self.assertIn("Review --> Remediate: accepted findings", workflow)
        self.assertIn("Integrate --> GoalComplete: root acceptance passes", workflow)

    def test_security_diagram_marks_trusted_and_worker_boundaries(self) -> None:
        security = self._read("docs/SECURITY.md")

        self.assertIn('subgraph Trusted["Trusted coordination boundary"]', security)
        self.assertIn('subgraph Scoped["Task-scoped worker boundary"]', security)
        self.assertIn('Inbox -->|"validate and consume"| Controller', security)

    def test_controller_managed_external_sessions_use_herdr_not_tmux(self) -> None:
        readme = " ".join(self._read("README.md").split())
        workflow = " ".join(self._read("docs/WORKFLOW.md").split())
        security = " ".join(self._read("docs/SECURITY.md").split())

        self.assertIn('Herdr<br/>controller-managed external session', readme)
        self.assertIn("`tmux` for manually managed terminal sessions", readme)
        self.assertIn("Use Herdr for controller-managed", workflow)
        self.assertIn(
            "tmux` is available for manually managed terminal handoffs only", workflow
        )
        self.assertIn('Launcher["Herdr<br/>controller-managed external launch"]', security)
        self.assertIn("`tmux` is only for manually managed terminal sessions", security)
        self.assertNotIn("Herdr or tmux", readme)
        self.assertNotIn("Herdr or `tmux`", workflow)
        self.assertNotIn("Herdr, tmux, or native agent runtime", security)

    def test_mermaid_fences_are_balanced(self) -> None:
        for relative in ("README.md", "docs/WORKFLOW.md", "docs/SECURITY.md"):
            document = self._read(relative)
            blocks = re.findall(r"```mermaid\n(.*?)\n```", document, flags=re.DOTALL)
            self.assertEqual(len(blocks), 1, relative)
            self.assertTrue(blocks[0].strip(), relative)


if __name__ == "__main__":
    unittest.main()
