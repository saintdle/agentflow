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

from agentflow import cli, usage


class UsageEvaluationTests(unittest.TestCase):
    def test_default_usage_yield_reads_one_jsonl_record_for_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([
                    "usage", "record", "codex", "--task-class", "implementation",
                    "--model", "gpt-5.6-luna", "--effort", "high",
                    "--evaluation-id", "live-cli-smoke", "--variant", "treatment",
                    "--case-id", "boolean-quantity", "--accepted-result", "accepted",
                ]), 0)

            evaluation_stdout = io.StringIO()
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(evaluation_stdout):
                self.assertEqual(cli.main(["usage", "yield", "--evaluation", "--json"]), 0)
            evaluation = json.loads(evaluation_stdout.getvalue())["evaluation"]
            self.assertEqual(evaluation["records"], 1)
            self.assertEqual(evaluation["cohorts"][0]["variants"]["treatment"]["attempts"], 1)

            legacy_stdout = io.StringIO()
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(legacy_stdout):
                self.assertEqual(cli.main(["usage", "yield", "--json"]), 0)
            legacy = json.loads(legacy_stdout.getvalue())
            self.assertEqual(legacy["task_classes"][0]["task_class"], "implementation")
            self.assertEqual(legacy["task_classes"][0]["attempts"], 1)
            self.assertEqual(legacy["task_classes"][0]["success_yield"], 0.0)

            explicit_object = state / "one-record.json"
            explicit_object.write_text((state / "usage.jsonl").read_text().strip())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["usage", "yield", "--file", str(explicit_object), "--evaluation", "--json"]), 2)

    def test_legacy_record_keeps_zero_retry_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["usage", "record", "codex"]), 0)
            saved = json.loads((state / "usage.jsonl").read_text().splitlines()[0])
            self.assertEqual(saved["retries"], 0)

    def test_record_cli_round_trips_explicit_evaluation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([
                    "usage", "record", "claude", "--model", "claude-sonnet-5", "--effort", "medium",
                    "--task-class", "focused-review", "--evaluation-id", "pilot-1", "--variant", "treatment",
                    "--case-id", "case-a", "--rework-rounds", "2", "--unrequested-changes", "0",
                    "--human-interventions", "1", "--elapsed-seconds", "31", "--source", "/usage",
                    "--accepted-result", "accepted",
                ]), 0)
            saved = json.loads((state / "usage.jsonl").read_text().splitlines()[0])
            self.assertEqual(saved["evaluation_id"], "pilot-1")
            self.assertEqual(saved["variant"], "treatment")
            self.assertEqual(saved["case_id"], "case-a")
            self.assertEqual(saved["rework_rounds"], 2)
            self.assertIsNone(saved["retries"])
            self.assertEqual(saved["elapsed_seconds"], 31)
            self.assertEqual(saved["source"], "/usage")
            self.assertEqual(saved["accepted_result"], "accepted")

            input_path = state / "input.json"
            input_path.write_text(json.dumps([saved]))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["usage", "yield", "--file", str(input_path), "--evaluation", "--json"]), 0)
            result = json.loads(stdout.getvalue())
            treatment = result["evaluation"]["cohorts"][0]["variants"]["treatment"]
            self.assertEqual(treatment["metrics"]["retries"], {"n": 0, "missing": 1, "invalid": 0, "mean": None})
            self.assertEqual(treatment["usage_measurement_authority"], "provider-authoritative")
            self.assertEqual(treatment["local_observation_authority"], "local-observation")

    def test_usage_float_fields_reject_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for option in ("--remaining", "--remaining-before", "--remaining-after", "--credits-used", "--elapsed-seconds"):
                for value in ("nan", "inf", "-inf"):
                    with self.subTest(option=option, value=value), mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(["usage", "record", "codex", f"{option}={value}"]), 2)
            with mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["usage", "record", "codex", "--elapsed-seconds=-0.5"]), 2)
            for option in ("--retries", "--rework-rounds", "--unrequested-changes", "--human-interventions"):
                with self.subTest(option=option), mock.patch.object(cli, "_state_dir", return_value=state), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["usage", "record", "codex", f"{option}=-1"]), 2)
            self.assertFalse((state / "usage.jsonl").exists())

    def test_text_evaluation_handles_malformed_accepted_result_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "records.json"
            base = {"evaluation_id": "pilot", "task_class": "implementation", "case_id": "case-1", "provider": "codex", "model": "model-x", "effort": "medium", "source": "manual"}
            input_path.write_text(json.dumps([
                {**base, "variant": "baseline", "accepted_result": ["accepted"]},
                {**base, "variant": "treatment", "accepted_result": "accepted"},
            ]))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["usage", "yield", "--file", str(input_path), "--evaluation"]), 0)
            self.assertIn("'invalid': 1", stdout.getvalue())
            self.assertIn("'unknown': 0", stdout.getvalue())

    def test_groups_do_not_cross_provider_model_effort_and_reports_gaps(self) -> None:
        base = {
            "evaluation_id": "pilot", "task_class": "implementation", "case_id": "shared",
            "model": "model-x", "effort": "high", "source": "/usage", "outcome": "completed",
            "rework_rounds": 2,
        }
        records = [
            {**base, "provider": "codex", "variant": "baseline", "case_id": "shared", "accepted_result": "accepted"},
            {**base, "provider": "codex", "variant": "treatment", "case_id": "shared", "rework_rounds": 1, "accepted_result": "rejected"},
            {**base, "provider": "claude", "variant": "baseline", "case_id": "shared"},
            {**base, "provider": "claude", "variant": "treatment", "case_id": "shared", "rework_rounds": 1},
            {**base, "provider": "codex", "model": "model-y", "variant": "baseline", "case_id": "shared"},
            {**base, "provider": "codex", "model": "model-y", "variant": "treatment", "case_id": "shared"},
            {**base, "provider": "codex", "effort": "low", "variant": "baseline", "case_id": "shared"},
            {**base, "provider": "codex", "effort": "low", "variant": "treatment", "case_id": "shared"},
            {**base, "provider": "codex", "variant": "baseline", "case_id": "baseline-only"},
            {**base, "provider": "codex", "variant": "treatment", "case_id": "duplicate"},
            {**base, "provider": "codex", "variant": "treatment", "case_id": "duplicate"},
            {**base, "variant": "baseline", "provider": "", "case_id": "missing-provider"},
            {**base, "provider": "codex", "variant": "treatment", "model": "", "case_id": "missing-model"},
            {**base, "provider": "codex", "variant": "baseline", "effort": "", "case_id": "missing-effort"},
        ]
        result = usage.evaluation_report(records)
        self.assertEqual(len(result["cohorts"]), 4)
        self.assertEqual({row["provider"] for row in result["cohorts"]}, {"codex", "claude"})
        codex_default = next(row for row in result["cohorts"] if row["provider"] == "codex" and row["model"] == "model-x" and row["effort"] == "high")
        paired = codex_default["comparisons"][0]
        self.assertEqual(paired["matched_cases"], 1)
        self.assertEqual(paired["unmatched_cases"], 3)
        self.assertEqual(paired["ambiguous_duplicate_cases"], 1)
        self.assertEqual(paired["ambiguous_duplicate_records"], 2)
        delta = paired["metric_deltas"]["rework_rounds"]
        self.assertEqual(delta["mean_treatment_minus_baseline"], -1)
        self.assertEqual(delta["n"], 1)
        acceptance = paired["accepted_results"]
        self.assertEqual(acceptance["counts"]["baseline"], {"accepted": 1, "rejected": 0, "unknown": 0, "invalid": 0})
        self.assertEqual(acceptance["counts"]["treatment"], {"accepted": 0, "rejected": 1, "unknown": 0, "invalid": 0})
        self.assertEqual(acceptance["accepted_count_delta"], -1)
        self.assertEqual(acceptance["acceptance_rate_delta"], -1)
        self.assertEqual(acceptance["missing_pairs"], 0)
        claude_default = next(row for row in result["cohorts"] if row["provider"] == "claude")
        unknown_acceptance = claude_default["comparisons"][0]["accepted_results"]
        self.assertEqual(unknown_acceptance["counts"]["baseline"]["unknown"], 1)
        self.assertEqual(unknown_acceptance["missing_pairs"], 1)
        self.assertEqual(claude_default["variants"]["baseline"]["local_observation_authority"], "local-observation")
        self.assertEqual(result["excluded"], {"missing:effort": 1, "missing:model": 1, "missing:provider": 1})
        self.assertEqual(codex_default["variants"]["baseline"]["metrics"]["human_interventions"]["missing"], 2)

        invalid = usage.evaluation_report([
            {**base, "provider": "codex", "variant": "baseline", "case_id": "invalid", "elapsed_seconds": float("nan")},
            {**base, "provider": "codex", "variant": "treatment", "case_id": "invalid", "elapsed_seconds": 100},
        ])
        invalid_delta = invalid["cohorts"][0]["comparisons"][0]["metric_deltas"]["elapsed_seconds"]
        self.assertEqual(invalid_delta["invalid"], 1)
        self.assertEqual(invalid_delta["missing"], 0)
        json.dumps(invalid, allow_nan=False)

    def test_provider_quota_authority_does_not_cover_local_yield(self) -> None:
        bucket = usage.report([
            {"provider": "codex", "task_class": "implementation", "outcome": "completed", "source": "/usage"},
       ])["task_classes"][0]
        self.assertEqual(bucket["usage_measurement_authority"], "provider-authoritative")
        self.assertEqual(bucket["measurement"], "local-estimate")


if __name__ == "__main__":
    unittest.main()
