"""Optional deterministic workflow guidance.

Guidance describes evidence to collect.  It never runs a check and never marks
one as passed, keeping execution and acceptance authority separate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def verification_plan(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    checks: list[dict[str, str]] = []
    if (root / "pyproject.toml").is_file() and (root / "tests").is_dir():
        checks.append({"stage": "tests", "command": "python3 -m unittest discover -s tests -p 'test_*.py'"})
    if (root / "scripts/validate.py").is_file():
        checks.append({"stage": "validation", "command": "python3 scripts/validate.py"})
    if (root / "Makefile").is_file():
        checks.append({"stage": "project", "command": "make test"})
    if (root / ".git").exists() or (root / ".git").is_file():
        checks.extend(
            [
                {"stage": "diff", "command": "git diff --check"},
                {"stage": "scope", "command": "git status --short"},
            ]
        )
    return {
        "schema": "agentflow.verification-guidance@1",
        "root": str(root),
        "checks": checks,
        "status": "planned",
        "instructions": [
            "Run only checks applicable to the changed boundary.",
            "Record command, exit status, and concise evidence in the owning Bead.",
            "Never convert a planned or skipped check into passed evidence.",
            "Use a deterministic watcher for external CI instead of an LLM wait loop.",
        ],
    }


__all__ = ["verification_plan"]
