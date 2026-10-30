from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY / ".github/workflows"
RETIRED_RUNNER_LABELS = ("macos-14",)
UNIT_JOB_NAME = "name: Unit (${{ matrix.os }}, Python ${{ matrix.python }})"
# Branch protection on main requires these unit check names. Changing a runner
# label renames the check, so protection must be updated in the same change.
EXPECTED_UNIT_CHECKS = {
    "Unit (ubuntu-24.04, Python 3.10)",
    "Unit (ubuntu-24.04, Python 3.11)",
    "Unit (ubuntu-24.04, Python 3.12)",
    "Unit (ubuntu-24.04, Python 3.13)",
    "Unit (ubuntu-24.04, Python 3.14)",
    "Unit (macos-15, Python 3.10)",
    "Unit (macos-15, Python 3.14)",
}


class CiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    def test_workflows_do_not_use_retired_runner_images(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            for label in RETIRED_RUNNER_LABELS:
                self.assertFalse(label in text, f"{workflow.name} uses retired runner {label}")

    def test_required_unit_check_names_use_supported_pinned_runners(self) -> None:
        self.assertIn(UNIT_JOB_NAME, self.ci)
        pairs = re.findall(r'(?m)^\s+- os: (\S+)\n\s+python: "([0-9.]+)"$', self.ci)
        names = {f"Unit ({os}, Python {python})" for os, python in pairs}
        self.assertEqual(len(pairs), len(names))
        self.assertEqual(names, EXPECTED_UNIT_CHECKS)
        self.assertNotIn("macos-latest", self.ci)

    def test_pull_request_beads_smoke_covers_linux_and_supported_macos(self) -> None:
        self.assertIn("os: [ubuntu-24.04, macos-15]", self.ci)


if __name__ == "__main__":
    unittest.main()
