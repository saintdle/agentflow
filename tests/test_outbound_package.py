"""Outbound package limits, review verdicts, and usage allowance reconciliation."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli, outbound_package, project_config, review_verdict, usage
from tests.test_cli import ValidLaunch


class PackageMeasurementTests(unittest.TestCase):
    def test_measures_every_listed_input_and_counts_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            context = Path(temp) / "context.md"
            context.write_text("same bytes\n", encoding="utf-8")
            inputs = [
                outbound_package.PackageInput("handoff", "handoff.md", content=b"handoff\n"),
                outbound_package.PackageInput("context", "a", path=context),
                outbound_package.PackageInput("context", "b", path=context),
            ]
            package, errors = outbound_package.measure(inputs)
            self.assertEqual(errors, [])
            self.assertEqual(package["file_count"], 3)
            self.assertEqual(package["total_bytes"], 8 + 11 + 11)
            self.assertEqual(package["unique_bytes"], 8 + 11)
            self.assertEqual(package["duplicate_bytes"], 11)
            self.assertEqual(
                [entry["sha256"] for entry in package["entries"]][1:],
                [hashlib.sha256(b"same bytes\n").hexdigest()] * 2,
            )
            again, _ = outbound_package.measure(inputs)
            self.assertEqual(again["digest"], package["digest"])
            context.write_text("same bytes!\n", encoding="utf-8")
            changed, _ = outbound_package.measure(inputs)
            self.assertNotEqual(changed["digest"], package["digest"])

    def test_missing_input_rejects_the_package(self) -> None:
        package, errors = outbound_package.measure([
            outbound_package.PackageInput("context", "gone.md", path=Path("/nonexistent/gone.md")),
        ])
        self.assertIsNone(package)
        self.assertEqual(errors, ["outbound package input is missing: gone.md"])

    def _package(self, total: int) -> dict:
        package, _ = outbound_package.measure([
            outbound_package.PackageInput("handoff", "h", content=b"x" * total),
        ])
        return package

    def test_declared_limit_boundaries(self) -> None:
        limit = 100
        for total, expected in ((limit - 1, "within-limit"), (limit, "within-limit"), (limit + 1, "over-limit")):
            with self.subTest(total=total):
                result = outbound_package.evaluate(
                    self._package(total), key="focused-review/claude", limit=limit,
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["excess_bytes"], max(0, total - limit))

    def test_undeclared_limit_is_uncalibrated_not_within_limit(self) -> None:
        result = outbound_package.evaluate(self._package(1), key="scan/codex", limit=None)
        self.assertEqual(result["status"], "uncalibrated")
        self.assertIsNone(result["excess_bytes"])

    def test_exception_applies_only_to_the_exact_package_digest(self) -> None:
        package = self._package(101)
        approved = outbound_package.parse_exception(package["digest"], "decision-42")
        result = outbound_package.evaluate(package, key="scan/codex", limit=100, exception=approved)
        self.assertEqual(result["status"], "exception-approved")
        self.assertEqual(result["exception"], {"digest": package["digest"], "decision_ref": "decision-42"})
        stale = outbound_package.parse_exception("0" * 64, "decision-42")
        rejected = outbound_package.evaluate(package, key="scan/codex", limit=100, exception=stale)
        self.assertEqual(rejected["status"], "over-limit")
        self.assertIn("different package digest", outbound_package.over_limit_message(rejected))

    def test_exception_requires_digest_and_decision(self) -> None:
        self.assertIsNone(outbound_package.parse_exception(None, None))
        for digest, ref in (("a" * 64, None), (None, "decision"), ("A" * 64, "decision"), ("a" * 63, "decision")):
            with self.subTest(digest=digest, ref=ref), self.assertRaises(outbound_package.PackageError):
                outbound_package.parse_exception(digest, ref)


class PackageLimitConfigTests(unittest.TestCase):
    def test_no_limit_ships_by_default(self) -> None:
        self.assertIsNone(project_config.package_limit(project_config.default_data(), "scan", "codex"))

    def test_local_limits_override_shared_limits_per_key(self) -> None:
        shared = {**project_config.default_data(), "package_limits": {"scan/codex": 10, "scan/claude": 20}}
        local = {**project_config.default_local_data(), "package_limits": {"scan/codex": 30}}
        merged = project_config.merge(shared, local)
        self.assertEqual(project_config.package_limit(merged, "scan", "codex"), 30)
        self.assertEqual(project_config.package_limit(merged, "scan", "claude"), 20)

    def test_rejects_unknown_keys_and_non_positive_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for limits in ({"scan": 10}, {"bogus/codex": 10}, {"scan/other": 10},
                           {"scan/codex": 0}, {"scan/codex": True}, {"scan/codex": 1.5}, []):
                with self.subTest(limits=limits):
                    data = {**project_config.default_data(), "package_limits": limits}
                    self.assertTrue(project_config.validate(data, Path(temp)))


class PreflightPackageLimitTests(unittest.TestCase):
    def _create(self, root: Path) -> Path:
        context = root / "context.md"
        context.write_text("context\n", encoding="utf-8")
        output = root / "handoff.md"
        args = argparse.Namespace(
            to="claude", title="Bounded change", goal="Observable outcome", task_id="",
            task_class="focused-review", lane="external", tool_profile="shell-write",
            output_boundary=str(root), require_tool=[], require_skill=[], allow_delegation=False,
            return_type="result", max_ai_credits=None, acceptance_matrix="", base="",
            dependency=[], done_when=["Do the thing"], context=[str(context)], constraint=[],
            check=[], budget=["20 minutes; one retry"], issue="", branch="", out=str(output),
            cwd=str(root),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.handoff_create(args), 0)
        return output

    def _configure(self, root: Path, limit: int | None) -> None:
        data = project_config.default_data()
        if limit is not None:
            data["package_limits"] = {"focused-review/claude": limit}
        path = project_config.config_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _preflight(self, output: Path, root: Path, **extra) -> tuple[int, str, dict]:
        stdout = io.StringIO()
        with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.handoff_preflight(argparse.Namespace(
                file=str(output), cwd=str(root), require_matrix=False, **extra,
            ))
        manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
        return code, stdout.getvalue(), manifest

    def test_preflight_measures_exact_package_and_reports_uncalibrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = self._create(root)
            code, text, manifest = self._preflight(output, root)
            self.assertEqual(code, 0, text)
            report = manifest["preflight"]["report"]
            package = report["outbound_package"]
            self.assertEqual([entry["kind"] for entry in package["entries"]], ["handoff", "manifest", "context"])
            self.assertEqual(package["entries"][0]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(
                package["entries"][2]["sha256"],
                hashlib.sha256((root / "context.md").read_bytes()).hexdigest(),
            )
            self.assertEqual(report["package_limit"]["status"], "uncalibrated")
            self.assertIn("package-limit: uncalibrated key=focused-review/claude limit=undeclared", text)
            code, _, again = self._preflight(output, root)
            self.assertEqual(code, 0)
            self.assertEqual(again["preflight"]["report"]["outbound_package"]["digest"], package["digest"])

    def test_preflight_enforces_declared_limit_at_b_minus_one_b_and_b_plus_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = self._create(root)
            _, _, manifest = self._preflight(output, root)
            total = manifest["preflight"]["report"]["outbound_package"]["total_bytes"]
            digest = manifest["preflight"]["report"]["outbound_package"]["digest"]
            for limit, expected_code, status in (
                (total + 1, 0, "within-limit"), (total, 0, "within-limit"), (total - 1, 2, None),
            ):
                with self.subTest(limit=limit):
                    self._configure(root, limit)
                    code, text, manifest = self._preflight(output, root)
                    self.assertEqual(code, expected_code, text)
                    if status:
                        self.assertEqual(manifest["preflight"]["report"]["package_limit"]["status"], status)
                    else:
                        self.assertNotIn("preflight", manifest)
            self._configure(root, total - 1)
            code, _, manifest = self._preflight(
                output, root, package_exception=digest, decision_ref="owner-approved-oversize",
            )
            self.assertEqual(code, 0)
            limit_report = manifest["preflight"]["report"]["package_limit"]
            self.assertEqual(limit_report["status"], "exception-approved")
            self.assertEqual(limit_report["exception"]["decision_ref"], "owner-approved-oversize")
            code, _, _ = self._preflight(
                output, root, package_exception="f" * 64, decision_ref="owner-approved-oversize",
            )
            self.assertEqual(code, 2)

    def test_missing_context_rejects_before_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = self._create(root)
            (root / "context.md").unlink()
            code, _, manifest = self._preflight(output, root)
            self.assertEqual(code, 2)
            self.assertNotIn("preflight", manifest)

    def test_unreadable_limit_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = self._create(root)
            path = project_config.config_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**project_config.default_data(), "package_limits": {"scan/codex": -1}}))
            code, _, _ = self._preflight(output, root)
            self.assertEqual(code, 2)


class ReviewVerdictTests(unittest.TestCase):
    SUBJECT = "main@" + "1" * 40
    DIGEST = "2" * 64

    def _verdict(self, **overrides) -> dict:
        value = {"verdict": "approve", "subject": self.SUBJECT, "handoff_sha256": self.DIGEST}
        value.update(overrides)
        return value

    def test_valid_verdicts_are_bound(self) -> None:
        requirement = review_verdict.requirement(self.SUBJECT)
        for verdict in review_verdict.VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    review_verdict.validate(self._verdict(verdict=verdict), requirement, handoff_sha256=self.DIGEST),
                    {"verdict": verdict, "subject": self.SUBJECT, "handoff_sha256": self.DIGEST},
                )

    def test_missing_malformed_stale_and_mismatched_verdicts_are_unavailable(self) -> None:
        requirement = review_verdict.requirement(self.SUBJECT)
        cases = (
            (None, "missing"), ("", "missing"), ({}, "missing"),
            ("APPROVE", "malformed"), ({"verdict": "approve"}, "malformed"),
            (self._verdict(verdict="APPROVE"), "malformed"), (self._verdict(verdict="lgtm"), "malformed"),
            (self._verdict(extra="x"), "malformed"), (self._verdict(handoff_sha256="short"), "malformed"),
            (self._verdict(subject="main@" + "9" * 40), "stale"),
            (self._verdict(handoff_sha256="3" * 64), "mismatched"),
        )
        for value, state in cases:
            with self.subTest(value=value), self.assertRaises(review_verdict.ReviewVerdictError) as raised:
                review_verdict.validate(value, requirement, handoff_sha256=self.DIGEST)
            self.assertEqual(raised.exception.state, state)
            self.assertIn("not approval", str(raised.exception))

    def test_requirement_shape_is_strict(self) -> None:
        self.assertIsNone(review_verdict.parse_requirement(None))
        for value in ({"schema": "x", "subject": "s", "verdicts": list(review_verdict.VERDICTS)},
                      {"schema": review_verdict.SCHEMA, "subject": "", "verdicts": list(review_verdict.VERDICTS)},
                      {"schema": review_verdict.SCHEMA, "subject": "s", "verdicts": ["approve"]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                review_verdict.parse_requirement(value)

    def test_subject_is_the_exact_git_base_or_unversioned(self) -> None:
        self.assertEqual(review_verdict.subject_for({"workspace_kind": "git", "base": self.SUBJECT}), self.SUBJECT)
        self.assertEqual(review_verdict.subject_for({"workspace_kind": "directory", "base": ""}), "unversioned")

    def test_completed_review_return_requires_verdict_but_failed_may_omit_it(self) -> None:
        contract = {"review": review_verdict.requirement(self.SUBJECT), "handoff_sha256": self.DIGEST}
        with self.assertRaises(review_verdict.ReviewVerdictError):
            cli._validate_review_return({"outcome": "completed"}, contract, "completed")
        self.assertIsNone(cli._validate_review_return({"outcome": "blocked"}, contract, "blocked"))
        self.assertIsNone(cli._validate_review_return({"review": "ignored"}, {}, "completed"))

    def test_codex_schema_requires_bound_verdict_only_for_reviews(self) -> None:
        plain = cli._codex_result_schema(("R1",))
        self.assertNotIn("review", plain["properties"])
        requirement = review_verdict.requirement(self.SUBJECT)
        schema = cli._codex_result_schema(("R1",), requirement, handoff_sha256=self.DIGEST)
        self.assertIn("review", schema["required"])
        self.assertEqual(schema["properties"]["review"]["properties"]["handoff_sha256"]["enum"], [self.DIGEST])
        candidate = cli._codex_result_candidate(json.dumps({
            "outcome": "completed", "acceptance_results": [], "review": self._verdict(),
        }))
        self.assertEqual(candidate["review"]["verdict"], "approve")
        self.assertIsNone(cli._codex_result_candidate(json.dumps({
            "outcome": "completed", "acceptance_results": [], "approved": True,
        })))


class ReviewHandoffContractTests(unittest.TestCase):
    def test_external_review_handoff_requires_verdict_and_preflight_rejects_its_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            context = root / "context.md"
            context.write_text("context\n", encoding="utf-8")
            output = root / "review.md"
            args = argparse.Namespace(
                to="claude", title="Review change", goal="Find defects", task_id="",
                task_class="focused-review", lane="external", tool_profile="read-only",
                output_boundary=str(root), require_tool=[], require_skill=[], allow_delegation=False,
                return_type="review", max_ai_credits=None, acceptance_matrix="", base="",
                dependency=[], done_when=["Review is complete"], context=[str(context)], constraint=[],
                check=[], budget=["20 minutes; no retry"], issue="", branch="", out=str(output),
                cwd=str(root),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.handoff_create(args), 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("VERDICT: CHANGES-REQUESTED", text)
            self.assertIn("never as approval", text)
            manifest_path = output.with_suffix(".json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            contract = manifest["machine_return_contract"]
            self.assertEqual(contract["review"], review_verdict.requirement("unversioned"))
            self.assertIn("review", contract["required_result_fields"])

            def preflight() -> int:
                with mock.patch.object(cli, "_provider_command", return_value="/usr/bin/true"), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return cli.handoff_preflight(argparse.Namespace(
                        file=str(output), cwd=str(root), require_matrix=False,
                    ))

            self.assertEqual(preflight(), 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["machine_return_contract"]["review"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(preflight(), 2)
            manifest["machine_return_contract"]["review"] = review_verdict.requirement("main@" + "4" * 40)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(preflight(), 2)


class ReviewResultIngestionTests(unittest.TestCase):
    """A real signed review launch: exit status alone never approves."""

    def test_result_ingestion_rejects_unavailable_verdicts_and_binds_a_valid_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ValidLaunch(Path(temp).resolve(), seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["return_type"] = "review"
            fixture.lease = fixture._seed_lease()
            fixture._persist_claim()
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            external_state = Path(temp).parent / f"{Path(temp).name}-external-state"
            self.addCleanup(shutil.rmtree, external_state, True)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(external_state)}), \
                 fixture.beads_patches(), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                state_path = fixture.root / ".agentflow/herdr/sessions.json"
                channel = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]["return_channel"]
                binding = channel["contract_binding"]
                requirement = binding["review"]
                self.assertTrue(requirement["subject"].startswith("main@"))
                self.assertEqual(binding["required_result_fields"], ["outcome", "acceptance_results", "review"])
                handoff_sha256 = binding["handoff_sha256"]
                acceptance = [{"acceptance_id": "R1", "status": "passed", "evidence": "reviewed", "source": "provider"}]

                def submit(result: dict) -> tuple[int, dict]:
                    Path(channel["result_path"]).write_text(json.dumps(result), encoding="utf-8")
                    payloads: list[dict] = []
                    with mock.patch.object(
                        cli, "_json_or_status", side_effect=lambda payload, **_kwargs: payloads.append(payload),
                    ):
                        code = cli.herdr_result(argparse.Namespace(
                            root=str(fixture.root), contract=channel["contract_path"],
                            file=channel["result_path"], json=True, _controller_ingest=True,
                            _capability_file=channel["capability_file"],
                            _authority_secret=fixture.authority_secret,
                        ))
                    return code, payloads[-1]

                verdict = {"verdict": "approve", "subject": requirement["subject"], "handoff_sha256": handoff_sha256}
                for review, state in (
                    (None, "missing"), ({"verdict": "approve"}, "malformed"),
                    ({**verdict, "subject": "main@" + "0" * 40}, "stale"),
                    ({**verdict, "handoff_sha256": "0" * 64}, "mismatched"),
                ):
                    with self.subTest(state=state):
                        result = {"outcome": "completed", "acceptance_results": acceptance}
                        if review is not None:
                            result["review"] = review
                        code, payload = submit(result)
                        self.assertEqual(code, 2)
                        self.assertIn(f"review verdict unavailable ({state})", payload["error"])
                        row = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                        self.assertEqual(row["return_channel"]["state"], "issued")
                        self.assertIsNone(row["result"])

                code, payload = submit({"outcome": "completed", "acceptance_results": acceptance, "review": verdict})
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["review"], verdict)
                row = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][fixture.task_id]
                self.assertEqual(row["return_channel"]["state"], "consumed")
                self.assertEqual(row["result"]["review"], verdict)
                self.assertEqual(
                    cli._validate_review_return(row["result"], row["return_channel"]["contract_binding"], "completed"),
                    verdict,
                )
                tampered = {key: value for key, value in row["result"].items() if key != "review"}
                with self.assertRaises(review_verdict.ReviewVerdictError):
                    cli._validate_review_return(tampered, row["return_channel"]["contract_binding"], "completed")


class UsageAllowanceTests(unittest.TestCase):
    def _record(self, state: Path, *extra: str) -> int:
        with mock.patch.object(cli, "_state_dir", return_value=state), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(["usage", "record", "copilot", *extra])

    def test_allowance_actual_estimate_and_overshoot_stay_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(self._record(
                state, "--source", "dashboard", "--allowance-credits", "30",
                "--actual-credits", "42.5", "--estimated-credits", "25",
            ), 0)
            saved = json.loads((state / "usage.jsonl").read_text().splitlines()[-1])
            self.assertEqual(saved["allowance_credits"], 30)
            self.assertEqual(saved["actual_credits"], 42.5)
            self.assertEqual(saved["estimated_credits"], 25)
            self.assertEqual(saved["allowance_overshoot_credits"], 12.5)
            self.assertEqual(saved["allowance_status"], "soft-cap-overshoot")
            self.assertEqual(saved["measurement_authority"], "provider-authoritative")

    def test_estimate_never_becomes_actual_usage_or_overshoot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(self._record(
                state, "--source", "local-log", "--allowance-credits", "30", "--estimated-credits", "90",
            ), 0)
            saved = json.loads((state / "usage.jsonl").read_text().splitlines()[-1])
            self.assertIsNone(saved["actual_credits"])
            self.assertIsNone(saved["allowance_overshoot_credits"])
            self.assertEqual(saved["allowance_status"], "actual-unavailable")
            self.assertEqual(self._record(state, "--source", "local-log", "--actual-credits", "5"), 2)
            self.assertEqual(len((state / "usage.jsonl").read_text().splitlines()), 1)

    def test_reconciliation_statuses(self) -> None:
        within = usage.allowance_reconciliation(allowance=30, actual=30, estimate=None, source="/usage")
        self.assertEqual((within["allowance_status"], within["allowance_overshoot_credits"]), ("within-allowance", 0.0))
        unconfigured = usage.allowance_reconciliation(allowance=None, actual=3, estimate=None, source="api")
        self.assertEqual(unconfigured["allowance_status"], "allowance-unconfigured")
        for bad in (-1, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usage.allowance_reconciliation(allowance=bad, actual=None, estimate=None, source="api")


if __name__ == "__main__":
    unittest.main()
