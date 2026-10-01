from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.preflight import (
    BLOCKER, WARNING,
    LaunchSpec, PreflightError, PreflightFinding, PreflightReport,
    RootSnapshot, assess_filesystem, check_launch, take_snapshot,
)


def _minimal_spec(**kwargs) -> LaunchSpec:
    defaults = dict(
        base="abc1234",
        context=(),
        boundary="/repo",
        matrix=("H1",),
        tools=(),
        model="claude-sonnet-4.6",
        session_id="sess-001",
        provider="copilot",
        role="coding",
        effort="medium",
        policy_version="models-v2",
        lease_id="lease-1",
        claim_id="claim-1",
        handoff="handoff-1",
        herdr_session="herdr-1",
        herdr_protocol="agentflow.herdr@1",
        workspace_kind="git",
        workspace_root="/repo",
    )
    defaults.update(kwargs)
    return LaunchSpec(**defaults)


def _fs_spec(**kwargs) -> LaunchSpec:
    """A spec with only the fields assess_filesystem() cares about."""
    defaults = dict(
        base="abc1234",
        context=(),
        boundary="/repo",
        matrix=("H1",),
        tools=(),
        model="claude-sonnet-4.6",
        session_id="sess-001",
        workspace_kind="git",
        workspace_root="/repo",
    )
    defaults.update(kwargs)
    return LaunchSpec(**defaults)


def _minimal_snapshot(root: str, **kwargs) -> RootSnapshot:
    defaults = dict(
        taken_at="2026-07-25T20:00:00+00:00",
        instructions_entrypoint="AGENTS.md",
        has_checks=True,
        has_tests=True,
        has_skills=False,
        tool_availability=(),
    )
    defaults.update(kwargs)
    return RootSnapshot(root=root, **defaults)


class SnapshotStabilityTests(unittest.TestCase):
    """A stable root snapshot must be immutable and repeatable."""

    def test_snapshot_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = take_snapshot(Path(tmp), now="2026-07-25T20:00:00+00:00")
            with self.assertRaises((AttributeError, TypeError)):
                snap.root = "/other"  # type: ignore[misc]

    def test_same_inputs_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s1 = take_snapshot(Path(tmp), now="2026-07-25T20:00:00+00:00")
            s2 = take_snapshot(Path(tmp), now="2026-07-25T20:00:00+00:00")
            self.assertEqual(s1, s2)

    def test_snapshot_probes_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = take_snapshot(Path(tmp), tools=["python3", "does-not-exist-xyz"])
            avail = dict(snap.tool_availability)
            self.assertTrue(avail["python3"])
            self.assertFalse(avail["does-not-exist-xyz"])

    def test_snapshot_detects_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = take_snapshot(root)
            self.assertIsNone(snap.instructions_entrypoint)
            (root / "AGENTS.md").write_text("instructions")
            snap2 = take_snapshot(root)
            self.assertEqual(snap2.instructions_entrypoint, "AGENTS.md")


class PreflightAggregateTests(unittest.TestCase):
    """All blockers surface in a single pass; no partial mutation."""

    def _report(self, spec: LaunchSpec, root: str = "/repo") -> PreflightReport:
        snap = _minimal_snapshot(root)
        return check_launch(spec, snap)

    def test_valid_spec_no_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp, tool_availability=(("python3", True),))
            spec = _minimal_spec(
                boundary=tmp,
                tools=("python3",),
            )
            report = check_launch(spec, snap)
            self.assertFalse(report.launch_blocked)
            self.assertEqual(
                [f for f in report.findings if f.severity == BLOCKER], []
            )

    def test_launch_requires_exact_authenticated_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(
                boundary=tmp,
                provider="",
                role="",
                effort="",
                lease_id="",
                claim_id="",
                handoff="",
                herdr_session="",
                herdr_protocol="",
            )
            report = check_launch(spec, snap)
            ids = {finding.id for finding in report.findings}
            self.assertTrue(report.launch_blocked)
            self.assertTrue({"provider-missing", "role-missing", "effort-missing"} <= ids)
            self.assertIn("lease-missing", ids)
            self.assertIn("claim-missing", ids)
            self.assertIn("handoff-missing", ids)
            self.assertIn("herdr-binding-missing", ids)
            self.assertIn("herdr-protocol-missing", ids)

    def test_policy_is_mandatory_without_strict(self) -> None:
        """AFREL-017: check_launch has no optional/strict escape hatch — a
        caller that never sets ``strict`` still gets the full exact-route,
        lease, claim, handoff, and Herdr binding contract enforced."""
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(
                boundary=tmp,
                provider="",
                role="",
                effort="",
                lease_id="",
                claim_id="",
                handoff="",
                herdr_session="",
                herdr_protocol="",
                strict=False,
            )
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            ids = {finding.id for finding in report.findings}
            self.assertTrue({"provider-missing", "role-missing", "effort-missing"} <= ids)
            self.assertIn("lease-missing", ids)
            self.assertIn("claim-missing", ids)
            self.assertIn("handoff-missing", ids)

    def test_unapproved_route_blocks_without_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp, provider="codex", role="coding", effort="high", strict=False)
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertIn("policy-route-invalid", [f.id for f in report.findings])

    def test_assess_filesystem_never_enforces_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _fs_spec(boundary=tmp)
            report = assess_filesystem(spec, snap)
            self.assertFalse(report.launch_blocked)
            ids = {finding.id for finding in report.findings}
            self.assertFalse({"provider-missing", "role-missing", "effort-missing", "lease-missing", "claim-missing"} & ids)

    def test_empty_base_is_blocker(self) -> None:
        with self.assertRaises(PreflightError):
            _minimal_spec(base="")

    def test_directory_workspace_passes_with_exact_root_and_no_git_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            snap = _minimal_snapshot(str(root))
            spec = _minimal_spec(
                base="", workspace_kind="directory", workspace_root=str(root),
                boundary=str(root),
            )
            report = check_launch(spec, snap)
            self.assertFalse(report.launch_blocked, report.findings)
            self.assertFalse(any(f.id == "base-empty" for f in report.findings))

    def test_directory_workspace_rejects_fake_git_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = _minimal_spec(
                base="workspace", workspace_kind="directory",
                workspace_root=str(root), boundary=str(root),
            )
            report = check_launch(spec, _minimal_snapshot(str(root)))
            self.assertTrue(report.launch_blocked)
            self.assertIn("directory-base-present", {f.id for f in report.findings})

    def test_directory_workspace_requires_canonical_absolute_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            snap = _minimal_snapshot(str(root))
            spec = _minimal_spec(
                base="", workspace_kind="directory",
                workspace_root="relative/path", boundary=str(root),
            )
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertIn("workspace-root-invalid", {f.id for f in report.findings})

    def test_boundary_outside_root_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary="/completely/different/path")
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            ids = [f.id for f in report.findings]
            self.assertIn("boundary-outside-root", ids)

    def test_missing_context_file_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp, context=("missing-file.md",))
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertTrue(any("context-missing" in f.id for f in report.findings))

    def test_present_context_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("ok")
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp, context=("AGENTS.md",))
            report = check_launch(spec, snap)
            self.assertFalse(any("context-missing" in f.id for f in report.findings))

    def test_empty_matrix_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp, matrix=())
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertIn("matrix-empty", [f.id for f in report.findings])

    def test_tool_not_probed_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp, tool_availability=())
            spec = _minimal_spec(boundary=tmp, tools=("python3",))
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertIn("tool-not-probed-python3", [f.id for f in report.findings])

    def test_tool_missing_from_path_is_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp, tool_availability=(("myapp", False),))
            spec = _minimal_spec(boundary=tmp, tools=("myapp",))
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            self.assertIn("tool-missing-myapp", [f.id for f in report.findings])

    def test_forbidden_model_is_blocker(self) -> None:
        for bad_model in ("auto", "claude-haiku-latest", "terra-v1", "generic-model"):
            with tempfile.TemporaryDirectory() as tmp:
                snap = _minimal_snapshot(tmp)
                spec = _minimal_spec(boundary=tmp, model=bad_model)
                report = check_launch(spec, snap)
                self.assertTrue(report.launch_blocked, f"expected blocker for model {bad_model!r}")
                self.assertIn("model-forbidden", [f.id for f in report.findings])

    def test_empty_session_id_is_blocker(self) -> None:
        with self.assertRaises(PreflightError):
            _minimal_spec(session_id="")

    def test_multiple_blockers_all_present(self) -> None:
        """All blockers surface in a single pass — no short-circuit."""
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp, tool_availability=(("myapp", False),))
            spec = _minimal_spec(
                boundary="/nowhere",
                matrix=(),
                tools=("myapp",),
                model="auto",
                context=("no-such-file.md",),
            )
            report = check_launch(spec, snap)
            self.assertTrue(report.launch_blocked)
            ids = [f.id for f in report.findings]
            self.assertIn("boundary-outside-root", ids)
            self.assertIn("matrix-empty", ids)
            self.assertIn("tool-missing-myapp", ids)
            self.assertIn("model-forbidden", ids)
            self.assertTrue(any("context-missing" in i for i in ids))

    def test_check_launch_is_pure(self) -> None:
        """Calling check_launch twice yields equal results — no mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp)
            r1 = check_launch(spec, snap)
            r2 = check_launch(spec, snap)
            self.assertEqual(r1.findings, r2.findings)
            self.assertEqual(r1.launch_blocked, r2.launch_blocked)

    def test_root_health_warnings_do_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp, has_checks=False, has_tests=False)
            spec = _minimal_spec(boundary=tmp)
            report = check_launch(spec, snap)
            self.assertFalse(report.launch_blocked)
            self.assertTrue(any(f.severity == WARNING for f in report.findings))

    def test_report_to_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = _minimal_snapshot(tmp)
            spec = _minimal_spec(boundary=tmp)
            report = check_launch(spec, snap)
            d = report.to_dict()
            self.assertIn("launch_blocked", d)
            self.assertIn("findings", d)
            self.assertIsInstance(d["findings"], list)


if __name__ == "__main__":
    unittest.main()
