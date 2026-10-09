from __future__ import annotations

import unittest
from pathlib import Path
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
    def test_wide_dispatch_waits_for_pilot_and_independent_controller_validation(self) -> None:
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

    def test_review_ci_and_final_integration_remain_downstream_without_cycles(self) -> None:
        steps = _steps()
        self.assertEqual(steps["pedagogy-review"].get("needs"), ["writer-swarm"])
        self.assertEqual(steps["lifecycle-review"].get("needs"), ["writer-swarm"])
        self.assertEqual(steps["ci-validation"].get("needs"), ["writer-swarm"])
        self.assertEqual(
            set(steps["controller-integration"].get("needs", [])),
            {"pedagogy-review", "lifecycle-review", "ci-validation"},
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


if __name__ == "__main__":
    unittest.main()
