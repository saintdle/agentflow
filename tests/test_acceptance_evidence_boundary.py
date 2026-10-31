"""Acceptance rows pass only on evidence from their own lane and scope."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli


ROWS = [
    "L1::check passes locally::writer::local-runtime::unit run",
    "H1::hosted setup succeeds::controller::hosted-runtime::hosted run URL",
    "R1::runs on the target architecture::controller::architecture-specific::arm64 run",
    "W1::request reaches the backend end to end::controller::local-runtime::end-to-end trace",
]


class AcceptanceEvidenceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.matrix = self.root / "acceptance.json"

    def _run(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            args = cli.build_parser().parse_args(argv)
            code = args.func(args)
        return code, stderr.getvalue()

    def _create(self, *extra: str) -> None:
        argv = ["acceptance", "create", "--task", "LAB-1", "--out", str(self.matrix)]
        for row in ROWS:
            argv += ["--row", row]
        code, err = self._run(argv + list(extra))
        self.assertEqual(code, 0, err)

    def _set(self, row_id: str, status: str, *extra: str) -> tuple[int, str]:
        return self._run(
            ["acceptance", "set", str(self.matrix), "--id", row_id, "--status", status, *extra]
        )

    def _row(self, row_id: str) -> dict:
        data = json.loads(self.matrix.read_text(encoding="utf-8"))
        return next(row for row in data["rows"] if row["id"] == row_id)

    def _valid(self) -> bool:
        code, _ = self._run(["acceptance", "validate", str(self.matrix)])
        return code == 0

    def test_new_matrix_records_the_evidence_boundary(self) -> None:
        self._create()
        data = json.loads(self.matrix.read_text(encoding="utf-8"))
        self.assertEqual(data["evidence_boundary"], cli.EVIDENCE_BOUNDARY)
        self.assertNotIn("scope", self._row("W1"))
        self.assertTrue(self._valid())

    def test_hosted_row_passes_on_same_lane_evidence(self) -> None:
        self._create()
        code, err = self._set(
            "H1", "passed", "--evidence", "hosted run 123 passed", "--evidence-lane", "hosted-runtime"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self._row("H1")["status"], "passed")
        self.assertEqual(self._row("H1")["evidence_lane"], "hosted-runtime")
        self.assertTrue(self._valid())

    def test_live_rows_reject_evidence_from_another_lane(self) -> None:
        self._create()
        for row_id, lane in (("H1", "local-runtime"), ("R1", "static"), ("R1", "hosted-runtime")):
            with self.subTest(row=row_id, lane=lane):
                code, err = self._set(
                    row_id, "passed", "--evidence", "settings API returned ok", "--evidence-lane", lane
                )
                self.assertEqual(code, 2)
                self.assertIn("cannot pass it", err)
                self.assertEqual(self._row(row_id)["status"], "planned")

    def test_live_rows_require_a_recorded_evidence_lane(self) -> None:
        self._create()
        for row_id, lane in (("H1", "hosted-runtime"), ("R1", "architecture-specific")):
            with self.subTest(row=row_id):
                code, err = self._set(row_id, "passed", "--evidence", "component receipt")
                self.assertEqual(code, 2)
                self.assertIn(f"--evidence-lane {lane}", err)
                self.assertIn("leave it planned", err)
                self.assertEqual(self._row(row_id)["status"], "planned")
        self.assertTrue(self._valid())

    def test_other_lanes_keep_their_existing_pass_path(self) -> None:
        self._create()
        code, err = self._set("L1", "passed", "--evidence", "unit run passed")
        self.assertEqual(code, 0, err)
        self.assertNotIn("evidence_lane", self._row("L1"))
        code, err = self._set("L1", "passed", "--evidence", "rendered page", "--evidence-lane", "rendered")
        self.assertEqual(code, 2)
        self.assertIn("authoritative lane local-runtime", err)
        self.assertTrue(self._valid())

    def test_whole_chain_row_rejects_component_evidence(self) -> None:
        self._create("--whole-chain", "W1")
        self.assertEqual(self._row("W1")["scope"], "whole-chain")
        for extra in ((), ("--evidence-scope", "component")):
            with self.subTest(extra=extra):
                code, err = self._set("W1", "passed", "--evidence", "proxy receipt only", *extra)
                self.assertEqual(code, 2)
                self.assertIn("--evidence-scope whole-chain", err)
                self.assertEqual(self._row("W1")["status"], "planned")
        code, err = self._set(
            "W1", "passed", "--evidence", "end-to-end trace 9 passed", "--evidence-scope", "whole-chain"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self._row("W1")["evidence_scope"], "whole-chain")
        self.assertTrue(self._valid())

    def test_whole_chain_flag_rejects_an_unknown_row(self) -> None:
        argv = ["acceptance", "create", "--task", "LAB-1", "--out", str(self.matrix)]
        code, err = self._run(argv + ["--row", ROWS[0], "--whole-chain", "NOPE"])
        self.assertEqual(code, 2)
        self.assertIn("unknown row: NOPE", err)
        self.assertFalse(self.matrix.exists())

    def test_failed_and_waived_paths_are_unchanged(self) -> None:
        self._create("--whole-chain", "W1")
        code, err = self._set("H1", "failed", "--evidence", "hosted environment unavailable")
        self.assertEqual(code, 0, err)
        code, err = self._set("W1", "failed", "--evidence", "backend hop timed out")
        self.assertEqual(code, 0, err)
        code, err = self._set("R1", "waived", "--note", "approved exception")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._row("H1")["status"], "failed")
        self.assertEqual(self._row("W1")["status"], "failed")
        self.assertEqual(self._row("R1")["status"], "waived")

    def test_status_change_clears_stale_evidence_lane_and_scope(self) -> None:
        self._create("--whole-chain", "W1")
        self._set("H1", "passed", "--evidence", "hosted run", "--evidence-lane", "hosted-runtime")
        self._set("W1", "passed", "--evidence", "trace", "--evidence-scope", "whole-chain")
        self._set("H1", "failed", "--evidence", "regressed")
        self._set("W1", "failed", "--evidence", "regressed")
        self.assertNotIn("evidence_lane", self._row("H1"))
        self.assertNotIn("evidence_scope", self._row("W1"))
        self.assertTrue(self._valid())

    def test_lane_and_scope_are_recorded_only_on_passed_rows(self) -> None:
        self._create("--whole-chain", "W1")
        code, err = self._set(
            "H1", "failed", "--evidence", "hosted run failed", "--evidence-lane", "hosted-runtime"
        )
        self.assertEqual(code, 0, err)
        code, err = self._set(
            "W1", "waived", "--note", "approved exception", "--evidence-scope", "whole-chain"
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("evidence_lane", self._row("H1"))
        self.assertNotIn("evidence_scope", self._row("W1"))

    def test_live_whole_chain_row_needs_both_lane_and_scope(self) -> None:
        self._create("--whole-chain", "H1")
        code, err = self._set(
            "H1", "passed", "--evidence", "hosted trace", "--evidence-lane", "hosted-runtime"
        )
        self.assertEqual(code, 2)
        self.assertIn("--evidence-scope whole-chain", err)
        code, err = self._set(
            "H1", "passed", "--evidence", "hosted trace", "--evidence-scope", "whole-chain"
        )
        self.assertEqual(code, 2)
        self.assertIn("--evidence-lane hosted-runtime", err)
        self.assertEqual(self._row("H1")["status"], "planned")
        code, err = self._set(
            "H1", "passed", "--evidence", "hosted end-to-end trace",
            "--evidence-lane", "hosted-runtime", "--evidence-scope", "whole-chain",
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(self._valid())

    def test_bead_matrix_rejects_cross_lane_pass_without_writing(self) -> None:
        data = {
            "version": 1,
            "task_id": "work-2",
            "evidence_boundary": cli.EVIDENCE_BOUNDARY,
            "rows": [
                {
                    "id": "H1",
                    "outcome": "hosted setup succeeds",
                    "owner": "controller",
                    "lane": "hosted-runtime",
                    "planned_evidence": "hosted run",
                    "status": "planned",
                }
            ],
        }
        issue = {"metadata": {"agentflow": {"acceptance": data}}}
        with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), mock.patch.object(
            cli.beads_backend, "update_agentflow_metadata"
        ) as update:
            code, err = self._run(
                [
                    "acceptance", "set", "bead:work-2", "--cwd", ".", "--id", "H1",
                    "--status", "passed", "--evidence", "settings API ok", "--evidence-lane", "static",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("cannot pass it", err)
        update.assert_not_called()


def _matrix(row: dict, *, boundary: bool) -> dict:
    data = {"version": 1, "task_id": "ROOT", "rows": [row]}
    if boundary:
        data["evidence_boundary"] = cli.EVIDENCE_BOUNDARY
    return data


def _row(**fields) -> dict:
    row = {
        "id": "A1",
        "outcome": "outcome",
        "owner": "controller",
        "lane": "hosted-runtime",
        "planned_evidence": "hosted run",
        "status": "passed",
        "actual_evidence": "evidence",
    }
    row.update(fields)
    return row


class AcceptanceEvidenceValidatorTests(unittest.TestCase):
    def test_legacy_matrix_without_boundary_still_validates(self) -> None:
        self.assertEqual(cli._validate_acceptance_data(_matrix(_row(), boundary=False)), [])

    def test_boundary_matrix_rejects_hand_edited_live_pass(self) -> None:
        errors = cli._validate_acceptance_data(_matrix(_row(), boundary=True))
        self.assertEqual(errors, ["row A1 requires evidence recorded from the hosted-runtime lane to pass"])

    def test_mismatched_evidence_lane_is_rejected_in_any_matrix(self) -> None:
        for boundary in (False, True):
            with self.subTest(boundary=boundary):
                errors = cli._validate_acceptance_data(
                    _matrix(_row(evidence_lane="local-runtime"), boundary=boundary)
                )
                self.assertEqual(
                    errors,
                    ["row A1 cannot pass on local-runtime evidence; its authoritative lane is hosted-runtime"],
                )

    def test_whole_chain_pass_requires_whole_chain_evidence(self) -> None:
        row = _row(lane="local-runtime", scope="whole-chain", evidence_scope="component")
        errors = cli._validate_acceptance_data(_matrix(row, boundary=True))
        self.assertEqual(errors, ["row A1 is whole-chain and cannot pass on component or unscoped evidence"])
        row["evidence_scope"] = "whole-chain"
        self.assertEqual(cli._validate_acceptance_data(_matrix(row, boundary=True)), [])

    def test_waived_and_planned_live_rows_validate_without_evidence_lane(self) -> None:
        waived = _row(status="waived", actual_evidence="", note="approved", approval_ref="issue#1")
        planned = _row(status="planned", actual_evidence="")
        for row in (waived, planned):
            with self.subTest(status=row["status"]):
                self.assertEqual(cli._validate_acceptance_data(_matrix(row, boundary=True)), [])

    def test_unknown_boundary_values_are_rejected(self) -> None:
        data = _matrix(
            _row(scope="partial", evidence_lane="cloud", evidence_scope="some", status="planned"),
            boundary=True,
        )
        data["evidence_boundary"] = "other@9"
        self.assertEqual(
            cli._validate_acceptance_data(data),
            [
                "evidence_boundary must be lane-and-scope@1",
                "row A1 has unsupported scope: partial",
                "row A1 has unsupported evidence_lane: cloud",
                "row A1 has unsupported evidence_scope: some",
            ],
        )

    def test_root_completion_rejects_cross_lane_pass(self) -> None:
        issue = {
            "id": "ROOT",
            "metadata": {"agentflow": {"acceptance": _matrix(_row(evidence_lane="static"), boundary=True)}},
        }
        self.assertFalse(cli._root_acceptance_passed(issue))
        issue["metadata"]["agentflow"]["acceptance"]["rows"][0]["evidence_lane"] = "hosted-runtime"
        self.assertTrue(cli._root_acceptance_passed(issue))

    def test_native_acceptance_binding_rejects_unrecorded_live_pass(self) -> None:
        matrix = _matrix(_row(), boundary=True)
        matrix["task_id"] = "TASK"
        issue = {"id": "TASK", "metadata": {"agentflow": {"acceptance": matrix}}}
        self.assertEqual(cli._bound_acceptance_ids(issue, "TASK"), ())
        matrix["rows"][0]["evidence_lane"] = "hosted-runtime"
        self.assertEqual(cli._bound_acceptance_ids(issue, "TASK"), ("A1",))


class AcceptanceEvidenceSkillGuidanceTests(unittest.TestCase):
    def test_planning_skills_state_the_executable_readiness_rule(self) -> None:
        skills = Path(cli.__file__).resolve().parent / "resources" / "skills"
        for name in ("shape-goal", "to-tickets", "diagnosing-bugs"):
            with self.subTest(skill=name):
                text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("not executable readiness", text)
                self.assertIn("--evidence-lane", text)


if __name__ == "__main__":
    unittest.main()
