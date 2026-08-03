from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


class WorktreeError(RuntimeError):
    """A safe, user-facing worktree-lifecycle error."""


REGISTRY_DIR = ".agentflow/worktrees"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _require_repo(repo: Path) -> None:
    out = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if out != "true":
        raise WorktreeError(f"{repo} is not a git work tree")


def parse_base(base: str) -> tuple[str, str | None]:
    """Split ``ref@sha`` (or a bare ref/sha) into a ref and an expected SHA."""

    base = (base or "").strip()
    if not base:
        raise WorktreeError("a base ref is required")
    if "@" in base:
        ref, _, sha = base.partition("@")
        ref = ref.strip()
        sha = sha.strip().lower()
        if not ref:
            raise WorktreeError(f"base {base!r} is missing a ref before '@'")
        if not _SHA_RE.match(sha):
            raise WorktreeError(f"base {base!r} has an invalid SHA component")
        return ref, sha
    if _SHA_RE.match(base.lower()):
        return base, base.lower()
    return base, None


def _resolve_sha(repo: Path, ref: str) -> str:
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if not out:
        raise WorktreeError(f"base ref {ref!r} does not resolve to a commit")
    return out


def _validate_base(repo: Path, base: str) -> tuple[str, str]:
    ref, expected = parse_base(base)
    resolved = _resolve_sha(repo, ref)
    if expected and not resolved.startswith(expected):
        raise WorktreeError(
            f"base mismatch: {ref} resolves to {resolved[:12]} but {expected} was requested"
        )
    return ref, resolved


def _registry_path(repo: Path, path: Path) -> Path:
    resolved = str(path.resolve())
    slug = "".join(c if c.isalnum() else "-" for c in resolved)
    slug = "-".join(part for part in slug.split("-") if part) or "worktree"
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return repo / REGISTRY_DIR / f"{slug}-{digest}.json"


def _dirty(worktree: Path) -> bool:
    return bool(_git(worktree, "status", "--porcelain"))


def _commits_ahead(repo: Path, base_sha: str, head: str) -> int:
    out = _git(repo, "rev-list", "--count", f"{base_sha}..{head}", check=False)
    return int(out) if out.isdigit() else 0


def provision(
    repo: Path,
    *,
    bead: str,
    actor: str,
    base: str,
    branch: str,
    path: Path,
    session_hash: str = "",
) -> dict[str, Any]:
    """Create an exact-base worktree and record disjoint ownership metadata."""

    repo = repo.resolve()
    path = path.resolve()
    _require_repo(repo)
    ref, base_sha = _validate_base(repo, base)
    if path.exists() and any(path.iterdir()):
        raise WorktreeError(f"worktree path {path} already exists and is not empty")
    _git(repo, "worktree", "add", "-b", branch, str(path), base_sha)
    record = {
        "bead": bead,
        "actor": actor,
        "base_ref": ref,
        "base_sha": base_sha,
        "branch": branch,
        "path": str(path),
        "session_hash": session_hash,
        "dirty": False,
        "cleanup_eligible": True,
    }
    registry = _registry_path(repo, path)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def status(repo: Path, path: Path) -> dict[str, Any]:
    """Report dirty state, exact base, and cleanup eligibility for a worktree."""

    repo = repo.resolve()
    path = path.resolve()
    registry = _registry_path(repo, path)
    if not registry.exists():
        raise WorktreeError(f"no registry record for worktree {path}")
    record = json.loads(registry.read_text(encoding="utf-8"))
    if not path.exists():
        record.update({"present": False, "cleanup_eligible": False})
        return record
    dirty = _dirty(path)
    head = _git(path, "rev-parse", "HEAD")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    ahead = _commits_ahead(repo, record["base_sha"], head)
    record.update(
        {
            "present": True,
            "head": head,
            "branch": branch,
            "dirty": dirty,
            "commits_ahead_of_base": ahead,
            "cleanup_eligible": (not dirty) and ahead == 0,
        }
    )
    return record


def retire(
    repo: Path,
    path: Path,
    *,
    merged_into: str | None = None,
) -> dict[str, Any]:
    """Remove a worktree only when clean and merged. Never delete unmerged work."""

    repo = repo.resolve()
    path = path.resolve()
    state = status(repo, path)
    if not state.get("present", True):
        raise WorktreeError(f"worktree {path} is already gone from disk")
    if state.get("dirty"):
        raise WorktreeError("refusing to retire: worktree has uncommitted changes")
    ahead = int(state.get("commits_ahead_of_base", 0))
    if ahead > 0:
        if not merged_into:
            raise WorktreeError(
                f"refusing to retire: {ahead} commit(s) not in the base and no --merged-into given"
            )
        head = state["head"]
        unmerged = _git(repo, "rev-list", "--count", f"{head}", "--not", merged_into, check=False)
        if not unmerged.isdigit():
            raise WorktreeError(f"cannot verify merge into {merged_into!r}")
        if int(unmerged) > 0:
            raise WorktreeError(
                f"refusing to retire: {unmerged} commit(s) not merged into {merged_into}"
            )
    _git(repo, "worktree", "remove", str(path))
    registry = _registry_path(repo, path)
    registry.unlink(missing_ok=True)
    state.update({"present": False, "retired": True})
    return state
