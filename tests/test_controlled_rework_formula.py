from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "templates/beads/formulas/agentflow-controlled-rework.formula.toml"
PACKAGED_FORMULA = (
    ROOT
    / "src/agentflow/resources/templates/beads/formulas/agentflow-controlled-rework.formula.toml"
)


def _steps() -> dict[str, dict[str, object]]:
    data = tomllib.loads(FORMULA.read_text(encoding="utf-8"))
    return {str(step["id"]): step for step in data["steps"]}


def _ready_ids(steps: dict[str, dict[str, object]], closed: set[str]) -> set[str]:
    return {
        step_id
        for step_id, step in steps.items()
        if step_id not in closed and set(step.get("needs", [])) <= closed
    }


def _queue_ids(
    steps: dict[str, dict[str, object]], closed: set[str], stage: str
) -> set[str]:
    return {
        step_id
        for step_id in _ready_ids(steps, closed)
        if f"af:stage:{stage}" in steps[step_id].get("labels", [])
    }


class ControlledReworkFormulaTests(unittest.TestCase):
    def test_wide_dispatch_waits_for_pilot_and_independent_controller_validation(
        self,
    ) -> None:
        steps = _steps()
        pilot_id = "pilot-slice"
        validation_id = "pilot-validation"
        dispatch_id = "writer-swarm"

        self.assertIn(pilot_id, steps)
        self.assertIn(validation_id, steps)
        self.assertIn("specification", steps[pilot_id].get("needs", []))
        self.assertIn(pilot_id, steps[validation_id].get("needs", []))
        self.assertTrue(
            {"specification", pilot_id, validation_id}
            <= set(steps[dispatch_id].get("needs", []))
        )

        closed = {"contract", "specification"}
        self.assertIn(pilot_id, _queue_ids(steps, closed, "code"))
        self.assertNotIn(dispatch_id, _queue_ids(steps, closed, "dispatch"))

        closed.add(pilot_id)
        validation_step = steps[validation_id]
        self.assertIn(validation_id, _queue_ids(steps, closed, "integration"))
        self.assertIn("af:role:controller", validation_step.get("labels", []))
        self.assertNotIn(validation_id, _queue_ids(steps, closed, "code"))
        self.assertNotIn(dispatch_id, _queue_ids(steps, closed, "dispatch"))

        closed.add(validation_id)
        self.assertIn(dispatch_id, _queue_ids(steps, closed, "dispatch"))

    def test_later_slices_keep_one_writer_and_incremental_acceptance(self) -> None:
        description = " ".join(
            str(_steps()["writer-swarm"].get("description", "")).lower().split()
        )
        self.assertIn("one responsible sub-primary writer", description)
        self.assertIn("incremental acceptance criteria", description)

    def test_pilot_validation_uses_project_owned_criteria_and_declared_checks(self) -> None:
        description = str(_steps()["pilot-validation"].get("description", "")).lower()
        self.assertIn("project-owned acceptance criteria", description)
        self.assertIn("authoritative checks", description)
        self.assertIn("separate verdict", description)

    def test_nonlearner_review_uses_project_criteria_without_pedagogy_requirement(
        self,
    ) -> None:
        step = _steps()["project-domain-review"]
        description = " ".join(str(step.get("description", "")).lower().split())
        self.assertNotIn("af:cap:pedagogy", step.get("labels", []))
        self.assertIn(
            "project-specific review criteria in the approved specification",
            description,
        )
        self.assertIn("required project skills named there", description)
        self.assertIn("for non-learner work", description)
        self.assertIn("without adding pedagogy assumptions", description)

    def test_learner_pedagogy_review_remains_available_when_the_spec_requires_it(
        self,
    ) -> None:
        description = " ".join(
            str(_steps()["project-domain-review"].get("description", "")).lower().split()
        )
        self.assertIn("for learner-facing work", description)
        self.assertIn("learner-journey and pedagogy criteria", description)
        self.assertIn("when the specification requires them", description)
        self.assertIn("only when the project specification calls for them", description)

    def test_review_ci_and_final_integration_remain_downstream_without_cycles(self) -> None:
        steps = _steps()
        self.assertEqual(steps["project-domain-review"].get("needs"), ["writer-swarm"])
        self.assertEqual(steps["lifecycle-review"].get("needs"), ["writer-swarm"])
        self.assertEqual(steps["ci-validation"].get("needs"), ["writer-swarm"])
        self.assertEqual(
            set(steps["controller-integration"].get("needs", [])),
            {"project-domain-review", "lifecycle-review", "ci-validation"},
        )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            self.assertNotIn(step_id, visiting, f"dependency cycle reaches {step_id}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in steps[step_id].get("needs", []):
                visit(str(dependency))
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in steps:
            visit(step_id)

    def test_packaged_formula_mirrors_the_editable_template(self) -> None:
        self.assertEqual(FORMULA.read_bytes(), PACKAGED_FORMULA.read_bytes())

    def test_disposable_beads_cli_enforces_the_pilot_gate(self) -> None:
        bd = shutil.which("bd")
        if bd is None:
            self.skipTest("Beads CLI is unavailable")

        with tempfile.TemporaryDirectory(prefix="agentflow-pilot-cli-") as temporary:
            temporary_root = Path(temporary)
            workspace = temporary_root / "workspace"
            private_home = temporary_root / "home"
            workspace.mkdir()
            private_home.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(private_home),
                    "XDG_CONFIG_HOME": str(private_home / "config"),
                    "BEADS_DIR": str(workspace / ".beads"),
                    "BD_NON_INTERACTIVE": "1",
                    "DOLT_DISABLE_EVENT_FLUSH": "1",
                    "PYTHONPATH": str(ROOT / "src"),
                }
            )

            version = subprocess.run(
                [bd, "version"],
                cwd=workspace,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            version_text = (version.stdout or version.stderr).strip()
            match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_text)
            if (
                version.returncode
                or match is None
                or tuple(map(int, match.groups())) < (1, 1, 0)
            ):
                self.skipTest(
                    "Beads 1.1.0 or later is required for the CLI integration fixture"
                )

            def run(*command: str) -> str:
                result = subprocess.run(
                    list(command),
                    cwd=workspace,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0,
                    msg=f"{command[0]} failed: {(result.stderr or result.stdout).strip()}",
                )
                return result.stdout

            run(sys.executable, "-m", "agentflow", "beads", "init", str(workspace))
            run(
                bd,
                "mol",
                "pour",
                "agentflow-controlled-rework",
                "--var",
                "work_name=CLI Gate Probe",
            )
            issues = json.loads(run(bd, "list", "--all", "--json"))
            root_rows = [issue for issue in issues if issue.get("issue_type") == "molecule"]
            self.assertEqual(len(root_rows), 1)
            root_id = str(root_rows[0]["id"])

            def issue_id(title_start: str) -> str:
                matches = [
                    issue
                    for issue in issues
                    if str(issue.get("title", "")).startswith(title_start)
                ]
                self.assertEqual(len(matches), 1, title_start)
                return str(matches[0]["id"])

            contract_id = issue_id("Controller: approve CLI Gate Probe contract")
            specification_id = issue_id(
                "Controller: specify CLI Gate Probe interfaces"
            )
            pilot_id = issue_id("Writer: implement one pilot slice for CLI Gate Probe")
            validation_id = issue_id("Controller: independently validate the pilot slice")
            dispatch_id = issue_id(
                "Controller: coordinate CLI Gate Probe sub-primary writers"
            )

            def close(issue: str, actor: str) -> None:
                run(
                    bd,
                    "close",
                    issue,
                    "--reason",
                    "disposable pilot-gate acceptance",
                    "--actor",
                    actor,
                )

            def pull(stage: str, actor: str) -> str:
                return run(
                    sys.executable,
                    "-m",
                    "agentflow",
                    "worker",
                    "pull",
                    "--root",
                    root_id,
                    "--stage",
                    stage,
                    "--actor",
                    actor,
                    "--once",
                )

            close(contract_id, "controller-test")
            close(specification_id, "controller-test")
            self.assertIn("No ready dispatch work", pull("dispatch", "writer-test"))
            pilot_claim = pull("code", "pilot-writer")
            self.assertIn(
                f"CLAIMED Writer: implement one pilot slice for CLI Gate Probe ({pilot_id})",
                pilot_claim,
            )

            close(pilot_id, "pilot-writer")
            self.assertIn("No ready code work", pull("code", "writer-test"))
            validation_claim = pull("integration", "controller-test")
            self.assertIn(
                f"CLAIMED Controller: independently validate the pilot slice for CLI Gate Probe ({validation_id})",
                validation_claim,
            )
            self.assertIn("No ready dispatch work", pull("dispatch", "writer-test"))

            close(validation_id, "controller-test")
            dispatch_claim = pull("dispatch", "controller-test")
            self.assertIn(
                f"CLAIMED Controller: coordinate CLI Gate Probe sub-primary writers ({dispatch_id})",
                dispatch_claim,
            )


if __name__ == "__main__":
    unittest.main()
