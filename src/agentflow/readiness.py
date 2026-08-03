from __future__ import annotations

from pathlib import Path
from typing import Any


# The assessor is strictly read-only: it inspects the tree and reports, and must
# never create, modify, or delete a file. Owners accept or reject any proposal.


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


def _finding(fid: str, severity: str, area: str, finding: str, recommendation: str) -> dict[str, str]:
    return {
        "id": fid,
        "severity": severity,
        "area": area,
        "finding": finding,
        "recommendation": recommendation,
    }


def assess(root: Path) -> dict[str, Any]:
    """Return a read-only agent-readiness report for ``root``. Performs zero writes."""

    root = root.resolve()
    findings: list[dict[str, str]] = []

    instructions = _first_present(
        root, ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
    )
    if instructions is None:
        findings.append(
            _finding(
                "instructions",
                "high",
                "instructions",
                "No agent instruction entrypoint (AGENTS.md, CLAUDE.md, or "
                ".github/copilot-instructions.md) was found.",
                "Add an AGENTS.md describing the workflow, checks, and constraints.",
            )
        )

    if not (root / "scripts/validate.py").exists() and not (root / "Makefile").exists():
        findings.append(
            _finding(
                "checks",
                "high",
                "deterministic-checks",
                "No deterministic validator (scripts/validate.py or Makefile) was found.",
                "Add a single command that validates the repository deterministically.",
            )
        )

    if not _has_tests(root):
        findings.append(
            _finding(
                "tests",
                "high",
                "deterministic-checks",
                "No test files (tests/test_*.py) were discovered.",
                "Add a runnable test suite so agents can prove changes.",
            )
        )

    skills_present = (root / ".agents/skills").is_dir() or (root / ".claude/skills").is_dir()
    if not skills_present:
        findings.append(
            _finding(
                "skills",
                "medium",
                "domain-skills",
                "No domain skills directory (.agents/skills or .claude/skills) was found.",
                "Capture repeatable domain procedures as skills for reuse and routing.",
            )
        )

    if not (root / "docs/WORKFLOW.md").exists() and not (root / "docs").is_dir():
        findings.append(
            _finding(
                "wait-predicates",
                "low",
                "wait-predicates",
                "No docs/ directory documenting readiness or wait predicates was found.",
                "Document environment-specific success/failure predicates for long-running work.",
            )
        )

    if instructions is not None:
        text = (root / instructions).read_text(encoding="utf-8", errors="ignore")
        if "check" not in text.lower():
            findings.append(
                _finding(
                    "evidence",
                    "medium",
                    "evidence-gaps",
                    f"{instructions} does not mention checks or evidence expectations.",
                    "State the exact checks and evidence an agent must produce.",
                )
            )

    return {
        "root": str(root),
        "instructions_entrypoint": instructions,
        "has_deterministic_checks": (root / "scripts/validate.py").exists()
        or (root / "Makefile").exists(),
        "has_tests": _has_tests(root),
        "has_domain_skills": skills_present,
        "findings": findings,
        "ready": not findings,
    }


def render(report: dict[str, Any]) -> str:
    lines = [f"Agent-readiness assessment for {report['root']}", ""]
    entry = report.get("instructions_entrypoint") or "(none)"
    lines.append(f"Instruction entrypoint: {entry}")
    lines.append(f"Deterministic checks:   {'yes' if report['has_deterministic_checks'] else 'no'}")
    lines.append(f"Test suite:             {'yes' if report['has_tests'] else 'no'}")
    lines.append(f"Domain skills:          {'yes' if report['has_domain_skills'] else 'no'}")
    lines.append("")
    findings = report.get("findings", [])
    if not findings:
        lines.append("No readiness gaps detected. This report is advisory; owners decide.")
        return "\n".join(lines)
    lines.append(f"Recommendations ({len(findings)}):")
    for item in findings:
        lines.append(f"  [{item['severity']}] {item['area']}: {item['finding']}")
        lines.append(f"      -> {item['recommendation']}")
    lines.append("")
    lines.append("This report is advisory and writes nothing. Owners accept or reject each change.")
    return "\n".join(lines)
