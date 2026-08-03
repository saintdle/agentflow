#!/usr/bin/env python3
"""Inspect built distributions for completeness, provenance, and privacy leaks."""

from __future__ import annotations

import argparse
import email.parser
import fnmatch
import io
import re
import tarfile
from pathlib import Path, PurePosixPath
import zipfile


REQUIRED_WHEEL_FILES = {
    "agentflow/__init__.py",
    "agentflow/__main__.py",
    "agentflow/cli.py",
    "agentflow/resources/agents/claude/agentflow-controller.md",
    "agentflow/resources/agents/claude/agentflow-explorer.md",
    "agentflow/resources/agents/claude/agentflow-pr-gatekeeper.md",
    "agentflow/resources/agents/claude/agentflow-reviewer.md",
    "agentflow/resources/agents/codex/agentflow-controller.toml",
    "agentflow/resources/agents/codex/agentflow-explorer.toml",
    "agentflow/resources/agents/codex/agentflow-pr-gatekeeper.toml",
    "agentflow/resources/agents/codex/agentflow-reviewer.toml",
    "agentflow/resources/agents/copilot/agentflow-controller.agent.md",
    "agentflow/resources/agents/copilot/agentflow-explorer.agent.md",
    "agentflow/resources/agents/copilot/agentflow-pr-gatekeeper.agent.md",
    "agentflow/resources/agents/copilot/agentflow-reviewer.agent.md",
    "agentflow/resources/policies/models-v1.json",
    "agentflow/resources/skills/code-review/SKILL.md",
    "agentflow/resources/skills/diagnosing-bugs/SKILL.md",
    "agentflow/resources/skills/gatekeep-prs/SKILL.md",
    "agentflow/resources/skills/orchestrate-agents/SKILL.md",
    "agentflow/resources/skills/shape-goal/SKILL.md",
    "agentflow/resources/skills/to-tickets/SKILL.md",
    "agentflow/resources/skills/wayfinder/SKILL.md",
    "agentflow/resources/templates/project/claude-settings.json",
    "agentflow/resources/templates/project/copilot-hooks.json",
    "agentflow/resources/templates/project/agentflow.json",
    "agentflow/resources/templates/beads/PRIME.md",
    "agentflow/resources/templates/user/codex-hooks.json",
    "*.dist-info/licenses/LICENSE",
    "*.dist-info/licenses/NOTICE",
    "*.dist-info/licenses/THIRD_PARTY_NOTICES.md",
}
FORBIDDEN_PATH_PARTS = {
    ".agentflow",
    ".beads",
    ".claude",
    ".codex",
    ".copilot",
    ".git",
    ".venv",
    "__pycache__",
    "sessions",
    "transcripts",
}
FORBIDDEN_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx"}
HOME_PATH = re.compile(rb"(?:/Users/|/home/)[A-Za-z0-9._-]+/")
CREDENTIAL_PATTERNS = {
    "private key material": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{36,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b"),
    "provider API key": re.compile(rb"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,255}\b"),
}
TEXT_SUFFIXES = {
    ".cfg", ".ini", ".json", ".md", ".py", ".rst", ".toml", ".txt", ".yaml", ".yml"
}


def _safe_member(name: str, *, wheel: bool = False) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive member: {name}")
    if FORBIDDEN_PATH_PARTS.intersection(path.parts):
        raise ValueError(f"development/runtime state included in distribution: {name}")
    if wheel and "tests" in path.parts:
        raise ValueError(f"tests must not be installed in the runtime wheel: {name}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError(f"sensitive file type included in distribution: {name}")
    return path


def _inspect_text(name: str, payload: bytes) -> None:
    if HOME_PATH.search(payload):
        raise ValueError(f"personal absolute home path found in {name}")
    for reason, pattern in CREDENTIAL_PATTERNS.items():
        for match in pattern.finditer(payload):
            # Tests use an unmistakable alphabet sequence to prove redaction.
            # Keep that explicit fixture while rejecting credential-shaped entropy.
            if b"abcdefghijklmnopqrstuvwxyz" in match.group().lower():
                continue
            raise ValueError(f"{reason} found in {name}")


def _wheel_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        members: dict[str, bytes] = {}
        for item in archive.infolist():
            name = _safe_member(item.filename, wheel=True).as_posix()
            if item.is_dir():
                continue
            payload = archive.read(item)
            members[name] = payload
            if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES:
                _inspect_text(name, payload)
        return members


def _sdist_members(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for item in archive.getmembers():
            name = _safe_member(item.name).as_posix()
            if item.issym() or item.islnk():
                raise ValueError(f"links are not allowed in source distributions: {name}")
            if not item.isfile() or PurePosixPath(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            extracted = archive.extractfile(item)
            if extracted is not None:
                _inspect_text(name, extracted.read())


def inspect(dist: Path, expected_version: str) -> None:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one .tar.gz source distribution")

    members = _wheel_members(wheels[0])
    missing = sorted(
        pattern
        for pattern in REQUIRED_WHEEL_FILES
        if not any(fnmatch.fnmatchcase(name, pattern) for name in members)
    )
    if missing:
        raise ValueError(f"wheel is missing required runtime files: {', '.join(missing)}")

    metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ValueError("wheel must contain exactly one METADATA file")
    metadata = email.parser.BytesParser().parse(io.BytesIO(members[metadata_names[0]]))
    if metadata.get("Name") != "saintdle-agentflow":
        raise ValueError(f"unexpected project name: {metadata.get('Name')!r}")
    if metadata.get("Version") != expected_version:
        raise ValueError(
            f"distribution version {metadata.get('Version')!r} does not match {expected_version!r}"
        )

    _sdist_members(sdists[0])
    print(f"Inspected {wheels[0].name} and {sdists[0].name}: version, resources, and privacy checks passed.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("--expected-version", default="0.0.1")
    args = parser.parse_args()
    try:
        inspect(args.dist, args.expected_version)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
