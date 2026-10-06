"""Pure typed root preflight: stable snapshot + aggregate launch-blocker findings.

Design:
- take_snapshot() reads the root once and returns an immutable RootSnapshot.
- check_launch() validates a LaunchSpec against a snapshot; no snapshot mutation.
- All findings are typed PreflightFinding records; blockers set launch_blocked=True.
- Invalid base/context/boundary/matrix/tool/model/session combinations each
  produce a distinct BLOCKER finding so all problems surface in one pass.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Sequence

BLOCKER = "blocker"
WARNING = "warning"
INFO = "info"

_VALID_SEVERITIES = (BLOCKER, WARNING, INFO)

# Model tokens that indicate a non-specific or disallowed model.
_FORBIDDEN_MODEL_TOKENS = ("auto", "haiku", "generic")


class PreflightError(ValueError):
    """Raised when a launch specification is structurally invalid at build time."""


@dataclasses.dataclass(frozen=True)
class PreflightFinding:
    id: str
    severity: str
    area: str
    message: str
    recommendation: str

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"invalid severity: {self.severity!r}")


@dataclasses.dataclass(frozen=True)
class RootSnapshot:
    """Immutable point-in-time view of a root directory. Performs zero writes."""

    root: str
    taken_at: str
    instructions_entrypoint: str | None
    has_checks: bool
    has_tests: bool
    has_skills: bool
    tool_availability: tuple[tuple[str, bool], ...]


@dataclasses.dataclass(frozen=True)
class LaunchSpec:
    base: str
    context: tuple[str, ...]
    boundary: str
    matrix: tuple[str, ...]
    tools: tuple[str, ...]
    model: str
    session_id: str
    provider: str = ""
    role: str = ""
    effort: str = ""
    policy_version: str = ""
    lease_id: str = ""
    claim_id: str = ""
    handoff: str = ""
    handoff_content_sha256: str = ""
    handoff_manifest_sha256: str = ""
    handoff_preflight_sha256: str = ""
    root_preflight_sha256: str = ""
    duplicate_sessions: tuple[str, ...] = ()
    herdr_session: str = ""
    herdr_protocol: str = ""
    provider_transport: str = "herdr"
    external: bool = False
    authenticated_confinement: bool = False
    selective_model: bool = False
    strict: bool = False
    workspace_kind: str = "git"
    workspace_root: str = ""

    def __post_init__(self) -> None:
        if self.workspace_kind not in {"git", "directory"}:
            raise PreflightError("workspace_kind must be 'git' or 'directory'")
        if self.workspace_kind == "git" and not self.base.strip():
            raise PreflightError("git workspaces require a non-empty base")
        if not self.boundary:
            raise PreflightError("boundary must not be empty")
        if not self.model:
            raise PreflightError("model must not be empty")
        if self.provider_transport not in {"herdr", "app-server"}:
            raise PreflightError("provider_transport must be 'herdr' or 'app-server'")
        if self.provider_transport == "app-server" and self.provider != "codex":
            raise PreflightError("app-server transport is supported only for Codex")
        if not self.session_id and self.provider_transport != "app-server":
            raise PreflightError("session_id must not be empty")


@dataclasses.dataclass(frozen=True)
class PreflightReport:
    snapshot: RootSnapshot
    spec: LaunchSpec
    findings: tuple[PreflightFinding, ...]
    launch_blocked: bool
    taken_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.snapshot.root,
            "taken_at": self.taken_at,
            "launch_blocked": self.launch_blocked,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }


def report_digest(report: PreflightReport) -> str:
    """Digest stable launch evidence, excluding volatile timestamps."""
    value = {
        "root": report.snapshot.root,
        "spec": dataclasses.asdict(report.spec),
        "findings": [dataclasses.asdict(finding) for finding in report.findings],
        "launch_blocked": report.launch_blocked,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _first_present(root: Path, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if (root / candidate).exists():
            return candidate
    return None


def _has_tests(root: Path) -> bool:
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        return any(tests_dir.glob("test_*.py")) or any(tests_dir.rglob("*_test.py"))
    return bool(list(root.glob("test_*.py")))


def take_snapshot(
    root: Path,
    *,
    tools: Sequence[str] = (),
    now: str | None = None,
) -> RootSnapshot:
    """Return an immutable point-in-time snapshot of *root*. Performs zero writes."""
    root = root.resolve()
    instructions = _first_present(
        root, ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
    )
    has_checks = (root / "scripts/validate.py").exists() or (root / "Makefile").exists()
    has_tests = _has_tests(root)
    has_skills = (root / ".agents/skills").is_dir() or (root / ".claude/skills").is_dir()
    tool_avail: list[tuple[str, bool]] = [
        (tool, shutil.which(tool) is not None) for tool in tools
    ]
    return RootSnapshot(
        root=str(root),
        taken_at=now or _utcnow(),
        instructions_entrypoint=instructions,
        has_checks=has_checks,
        has_tests=has_tests,
        has_skills=has_skills,
        tool_availability=tuple(tool_avail),
    )


def _filesystem_findings(spec: LaunchSpec, snapshot: RootSnapshot) -> list[PreflightFinding]:
    """Pure filesystem/root assessment: no provider, model, or policy checks.

    Shared by :func:`assess_filesystem` (a caller-facing, policy-free API) and
    :func:`check_launch` (which adds the mandatory launch-authority findings
    below). Kept private so the two public entry points cannot drift apart.
    """
    findings: list[PreflightFinding] = []
    root = Path(snapshot.root)

    # Typed workspace identity. Git workspaces bind to an exact ref/revision;
    # directory workspaces bind to their canonical absolute path and carry no
    # invented branch/base sentinel.
    if spec.workspace_kind not in {"git", "directory"}:
        findings.append(PreflightFinding(
            "workspace-kind-invalid", BLOCKER, "workspace",
            f"unsupported workspace kind {spec.workspace_kind!r}",
            "Use the typed 'git' or 'directory' workspace contract.",
        ))
    if not spec.workspace_root.strip():
        findings.append(PreflightFinding(
            "workspace-root-missing", BLOCKER, "workspace",
            "workspace contract has no exact root path",
            "Bind the launch to the canonical absolute Git or directory root.",
        ))
    else:
        workspace_root = Path(spec.workspace_root).expanduser()
        if not workspace_root.is_absolute() or str(workspace_root.resolve()) != spec.workspace_root:
            findings.append(PreflightFinding(
                "workspace-root-invalid", BLOCKER, "workspace",
                f"workspace root {spec.workspace_root!r} is not a canonical absolute path",
                "Use the resolved absolute root stored in the durable workspace contract.",
            ))
    if spec.workspace_kind == "git" and not spec.base.strip():
        findings.append(PreflightFinding(
            "base-empty", BLOCKER, "base",
            "Git workspace base ref is empty",
            "Provide the approved branch@revision base.",
        ))
    elif spec.workspace_kind == "directory" and spec.base:
        findings.append(PreflightFinding(
            "directory-base-present", BLOCKER, "base",
            "directory workspace unexpectedly carries a Git base",
            "Remove the Git base and bind the exact directory root instead.",
        ))

    # boundary
    try:
        boundary = Path(spec.boundary).resolve()
        root_resolved = root.resolve()
        try:
            boundary.relative_to(root_resolved)
            within_root = True
        except ValueError:
            within_root = False
        if not within_root:
            findings.append(PreflightFinding(
                "boundary-outside-root", BLOCKER, "boundary",
                f"output boundary {spec.boundary!r} is not within root {str(root_resolved)!r}",
                "Set boundary to a path under the repository root.",
            ))
    except Exception:
        findings.append(PreflightFinding(
            "boundary-invalid", BLOCKER, "boundary",
            f"output boundary {spec.boundary!r} could not be resolved",
            "Provide a valid filesystem path.",
        ))

    # context files
    for ctx in spec.context:
        if not (root / ctx).exists():
            findings.append(PreflightFinding(
                f"context-missing-{ctx.replace('/', '-').replace('.', '_')}",
                BLOCKER, "context",
                f"required context file {ctx!r} not found in root",
                f"Ensure {ctx} exists before launching.",
            ))

    # matrix
    if not spec.matrix:
        findings.append(PreflightFinding(
            "matrix-empty", BLOCKER, "matrix",
            "acceptance matrix is empty; no done conditions are defined",
            "Provide at least one acceptance-matrix item ID.",
        ))

    # tools
    available = {name: avail for name, avail in snapshot.tool_availability}
    for tool in spec.tools:
        if tool not in available:
            findings.append(PreflightFinding(
                f"tool-not-probed-{tool}",
                BLOCKER, "tools",
                f"required tool {tool!r} was not probed in the snapshot",
                f"Include {tool!r} in the tools list when calling take_snapshot.",
            ))
        elif not available[tool]:
            findings.append(PreflightFinding(
                f"tool-missing-{tool}",
                BLOCKER, "tools",
                f"required tool {tool!r} is not available on PATH",
                f"Install {tool} before launching.",
            ))

    # model
    if not spec.model.strip():
        findings.append(PreflightFinding(
            "model-empty", BLOCKER, "model",
            "model is empty",
            "Specify an exact model identifier.",
        ))
    elif (
        any(tok in spec.model.lower() for tok in _FORBIDDEN_MODEL_TOKENS)
        or ("terra" in spec.model.lower() and spec.model != "gpt-5.6-terra")
    ):
        findings.append(PreflightFinding(
            "model-forbidden", BLOCKER, "model",
            f"model {spec.model!r} matches a forbidden pattern (auto/haiku/generic)",
            "Specify an exact, non-generic model identifier.",
        ))

    # session
    if not spec.session_id.strip() and spec.provider_transport != "app-server":
        findings.append(PreflightFinding(
            "session-empty", BLOCKER, "session",
            "session_id is empty",
            "Provide a unique session_id.",
        ))

    # root health (warnings only — do not block launch)
    if not snapshot.has_checks:
        findings.append(PreflightFinding(
            "root-no-checks", WARNING, "root",
            "root has no deterministic checks (scripts/validate.py or Makefile)",
            "Add a validator so agents can prove changes.",
        ))
    if not snapshot.has_tests:
        findings.append(PreflightFinding(
            "root-no-tests", WARNING, "root",
            "root has no test suite (tests/test_*.py)",
            "Add a test suite.",
        ))

    return findings


def assess_filesystem(spec: LaunchSpec, snapshot: RootSnapshot) -> PreflightReport:
    """Filesystem-only assessment: base/boundary/context/matrix/tools/model shape.

    Deliberately never enforces provider/model policy, lease, claim, handoff,
    or Herdr binding — callers that only need to know whether the root itself
    is in a launchable shape (before a provider/role/model is even chosen)
    use this instead of :func:`check_launch`, which is the mandatory launch
    authority and always enforces the full policy contract.
    """
    findings = tuple(_filesystem_findings(spec, snapshot))
    return PreflightReport(
        snapshot=snapshot,
        spec=spec,
        findings=findings,
        launch_blocked=any(f.severity == BLOCKER for f in findings),
        taken_at=snapshot.taken_at,
    )


def check_launch(spec: LaunchSpec, snapshot: RootSnapshot, *, policy: Any = None) -> PreflightReport:
    """Validate *spec* against the stable *snapshot*: the mandatory launch authority.

    Returns a PreflightReport whose findings are aggregated in one pass.
    Neither *spec* nor *snapshot* is mutated. Calling twice with the same
    arguments always returns an equal report. Every finding below is
    unconditional: there is no ``strict``/optional-field escape hatch that
    lets a caller skip the exact versioned provider/role/effort route or the
    lease/claim/handoff/herdr binding checks. Callers that only want the
    filesystem subset use :func:`assess_filesystem`.
    """
    findings: list[PreflightFinding] = _filesystem_findings(spec, snapshot)

    # The exact configured policy route is mandatory for every launch decision; there
    # is no lenient mode that lets a caller omit provider/role/effort.
    for field, value in (
        ("provider", spec.provider),
        ("role", spec.role),
        ("effort", spec.effort),
    ):
        if not str(value).strip():
            findings.append(PreflightFinding(
                f"{field}-missing", BLOCKER, "policy",
                f"{field} is required for an authenticated launch",
                f"Provide the exact {field} from the configured model policy.",
            ))
    try:
        from agentflow.model_policy import load_policy

        policy = policy or load_policy()
        route = policy.validate_route(
            provider=spec.provider,
            role=spec.role,
            model=spec.model,
            effort=spec.effort or None,
            selective=spec.selective_model,
        )
        if not route.ok:
            findings.append(PreflightFinding(
                "policy-route-invalid", BLOCKER, "policy", route.reason,
                "Use an exact provider/model/role/effort route from the configured policy.",
            ))
    except Exception as exc:
        findings.append(PreflightFinding(
            "policy-unavailable", BLOCKER, "policy", str(exc),
            "Load and validate the configured versioned policy before launch.",
        ))
    expected_policy_id = str(getattr(policy, "id", "") or "")
    if spec.policy_version != expected_policy_id:
        findings.append(PreflightFinding(
            "policy-version-missing" if not spec.policy_version else "policy-version-invalid",
            BLOCKER, "policy",
            f"exact policy id {expected_policy_id or 'unavailable'} is required "
            f"(got {spec.policy_version!r})",
            f"Provide --policy-version {expected_policy_id or '<configured-policy-id>'}.",
        ))
    if not spec.lease_id:
        findings.append(PreflightFinding(
            "lease-missing", BLOCKER, "lease",
            "authenticated root preflight requires the current controller lease",
            "Acquire and provide the current root lease token.",
        ))
    if not spec.claim_id:
        findings.append(PreflightFinding(
            "claim-missing", BLOCKER, "claims",
            "authenticated root preflight requires an exact Beads claim",
            "Claim the named descendant before launching it.",
        ))
    if not spec.handoff:
        findings.append(PreflightFinding(
            "handoff-missing", BLOCKER, "handoff",
            "authenticated provider launch has no durable handoff identity",
            "Supply the validated handoff or task contract.",
        ))
    for field, value in (
        ("handoff_content_sha256", spec.handoff_content_sha256),
        ("handoff_manifest_sha256", spec.handoff_manifest_sha256),
        ("handoff_preflight_sha256", spec.handoff_preflight_sha256),
    ):
        if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
            findings.append(PreflightFinding(
                f"{field}-invalid", BLOCKER, "handoff",
                f"{field} must be a lowercase SHA-256 digest",
                "Re-materialize and preflight the exact handoff artifact.",
            ))
    if spec.duplicate_sessions:
        findings.append(PreflightFinding(
            "duplicate-sessions", BLOCKER, "sessions",
            "duplicate active sessions: " + ", ".join(sorted(spec.duplicate_sessions)),
            "Keep one provider session per exact task and claim.",
        ))
    if spec.provider_transport == "herdr":
        if spec.herdr_session and not spec.session_id:
            findings.append(PreflightFinding(
                "herdr-session-mismatch", BLOCKER, "herdr",
                "Herdr session binding has no provider session identity",
                "Persist and bind the named Herdr session to the provider session.",
            ))
        if spec.herdr_protocol and spec.herdr_protocol not in {"agentflow.herdr@1", "1"}:
            findings.append(PreflightFinding(
                "herdr-protocol-invalid", BLOCKER, "herdr",
                f"unsupported Herdr protocol {spec.herdr_protocol!r}",
                "Use the versioned agentflow.herdr@1 protocol.",
            ))
        if not spec.herdr_protocol:
            findings.append(PreflightFinding(
                "herdr-protocol-missing", BLOCKER, "herdr",
                "authenticated launch requires the versioned Herdr protocol",
                "Provide herdr-protocol agentflow.herdr@1.",
            ))
        if not spec.herdr_session:
            findings.append(PreflightFinding(
                "herdr-binding-missing", BLOCKER, "herdr",
                "authenticated launch has no named Herdr session binding",
                "Bind the provider launch to a named Herdr session before launching.",
            ))
    elif spec.herdr_session or spec.herdr_protocol:
        findings.append(PreflightFinding(
            "herdr-identity-for-other-transport", BLOCKER, "transport",
            "app-server preflight must not claim Herdr pane or protocol evidence",
            "Use the typed app-server transport fields and persist its SDK thread identity after launch.",
        ))
    if spec.external and not spec.authenticated_confinement:
        findings.append(PreflightFinding(
            "external-confinement-unavailable", BLOCKER, "confinement",
            "hardened external launch cannot be authenticated and confined",
            "Do not launch externally until authenticated networked confinement exists.",
        ))

    launch_blocked = any(f.severity == BLOCKER for f in findings)
    return PreflightReport(
        snapshot=snapshot,
        spec=spec,
        findings=tuple(findings),
        launch_blocked=launch_blocked,
        taken_at=snapshot.taken_at,
    )
