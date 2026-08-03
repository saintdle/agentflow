"""Fail-closed privacy scanning for every blob reachable from Git refs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterable


HOME_PATH = re.compile(rb"(?:/Users/|/home/)[A-Za-z0-9._-]+/")
CREDENTIAL_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{36,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
    "slack-token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b"),
    "provider-api-key": re.compile(rb"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,255}\b"),
}
SENSITIVE_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx"}
RUNTIME_PARTS = {"sessions", "transcripts"}
AGENTFLOW_RUNTIME_PARTS = {
    "controller", "herdr", "claims", "runtime", "handoffs", "tmp", "logs", "worktrees", "history"
}


class ScanError(RuntimeError):
    """Git history could not be completely inspected."""


@dataclass(frozen=True)
class Finding:
    code: str
    object_id: str
    path: str


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScanError("Git history inspection failed") from exc
    if result.returncode != 0:
        raise ScanError("Git history inspection failed")
    return result.stdout


def _objects(root: Path) -> dict[str, set[str]]:
    output = _git(root, "rev-list", "--objects", "--all")
    objects: dict[str, set[str]] = {}
    for line in output.splitlines():
        object_id, separator, path = line.partition(" ")
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", object_id):
            objects.setdefault(object_id, set())
            if separator and path:
                objects[object_id].add(path)
    if not objects:
        raise ScanError("no objects were reachable from Git refs")
    checks = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_text="".join(f"{object_id}\n" for object_id in objects),
    )
    blobs: dict[str, set[str]] = {}
    for line in checks.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob":
            blobs[fields[0]] = objects.get(fields[0], set())
    return blobs


def _path_code(path: str) -> str:
    parts = PurePosixPath(path).parts
    lowered = tuple(part.lower() for part in parts)
    name = lowered[-1] if lowered else ""
    if name == ".env" or name.startswith(".env.") or PurePosixPath(name).suffix in SENSITIVE_SUFFIXES:
        return "sensitive-path"
    if RUNTIME_PARTS.intersection(lowered):
        return "session-or-transcript-path"
    for index, part in enumerate(lowered[:-1]):
        if part == ".agentflow" and lowered[index + 1] in AGENTFLOW_RUNTIME_PARTS:
            return "agentflow-runtime-path"
        if part in {".codex", ".claude", ".copilot"} and lowered[index + 1] in RUNTIME_PARTS:
            return "provider-session-path"
    if ".beads" in lowered and any(
        part in {"embeddeddolt", "dolt", "data"} or part.endswith((".db", ".sqlite", ".sqlite3"))
        for part in lowered
    ):
        return "beads-database-path"
    return ""


def _deny_markers(values: Iterable[str]) -> tuple[bytes, ...]:
    markers: set[bytes] = set()
    for value in values:
        stripped = value.strip()
        if not stripped or stripped.startswith("#"):
            continue
        encoded = stripped.casefold().encode("utf-8")
        if len(encoded) < 4:
            raise ScanError("private denylist markers must contain at least four bytes")
        markers.add(encoded)
    return tuple(sorted(markers))


def denylist_from_environment(path: Path | None = None) -> tuple[bytes, ...]:
    values = os.environ.get("AGENTFLOW_PUBLICATION_DENYLIST", "").splitlines()
    if path is not None:
        try:
            values.extend(path.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            raise ScanError("private denylist file could not be read") from exc
    return _deny_markers(values)


def scan_repository(
    root: Path,
    *,
    deny_markers: Iterable[bytes] = (),
    max_blob_bytes: int = 10 * 1024 * 1024,
) -> tuple[list[Finding], int]:
    repository = root.expanduser().resolve()
    blobs = _objects(repository)
    findings: set[Finding] = set()
    markers = tuple(marker.lower() for marker in deny_markers)
    for object_id, paths in sorted(blobs.items()):
        display_paths = sorted(paths) or ["<unmapped-blob>"]
        for path in display_paths:
            code = _path_code(path)
            if code:
                findings.add(Finding(code, object_id, path))
        size_text = _git(repository, "cat-file", "-s", object_id).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ScanError("Git returned an invalid blob size") from exc
        if size > max_blob_bytes:
            for path in display_paths:
                findings.add(Finding("oversized-uninspected-blob", object_id, path))
            continue
        try:
            payload = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "blob", object_id],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScanError("Git blob inspection failed") from exc
        if payload.returncode != 0:
            raise ScanError("Git blob inspection failed")
        codes: set[str] = set()
        if HOME_PATH.search(payload.stdout):
            codes.add("personal-home-path")
        for code, pattern in CREDENTIAL_PATTERNS.items():
            if pattern.search(payload.stdout):
                codes.add(code)
        lowered = payload.stdout.lower()
        if any(marker in lowered for marker in markers):
            codes.add("private-denylist-marker")
        for code in codes:
            for path in display_paths:
                findings.add(Finding(code, object_id, path))
    return sorted(findings, key=lambda item: (item.code, item.path, item.object_id)), len(blobs)
