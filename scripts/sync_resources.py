#!/usr/bin/env python3
"""Check or export developer-facing mirrors of packaged runtime resources.

``src/agentflow/resources`` is the canonical source. The repository-level
copies are convenient exports for editing and local tooling; they must never
become an alternate runtime source of truth. Running this script without
``--sync`` is read-only and fails when any export differs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_MIRRORS = (
    ("src/agentflow/resources/skills", ".agents/skills"),
    ("src/agentflow/resources/agents/codex", ".codex/agents"),
    ("src/agentflow/resources/agents/claude", ".claude/agents"),
    ("src/agentflow/resources/agents/copilot", ".github/agents"),
    ("src/agentflow/resources/templates", "templates"),
    ("src/agentflow/resources/policies", "policies"),
)


class ResourceSyncError(ValueError):
    """Raised when a resource tree cannot be safely read or exported."""


def _safe_tree_path(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ResourceSyncError(f"resource path must stay within repository root: {relative}")
    path = root
    for part in relative_path.parts:
        path = path / part
        if path.is_symlink():
            raise ResourceSyncError(f"resource tree path must not traverse symlinks: {path}")
    return path


def _tree_contents(directory: Path, *, required: bool) -> dict[Path, bytes]:
    if directory.is_symlink():
        raise ResourceSyncError(f"resource tree must not be a symlink: {directory}")
    if not directory.exists():
        if required:
            raise ResourceSyncError(f"canonical resource tree is missing: {directory}")
        return {}
    if not directory.is_dir():
        raise ResourceSyncError(f"resource tree is not a directory: {directory}")

    contents: dict[Path, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ResourceSyncError(f"resource tree must not contain symlinks: {path}")
        if path.is_file():
            contents[path.relative_to(directory)] = path.read_bytes()
    return contents


def check_resources(
    root: Path = ROOT,
    *,
    mirrors: Iterable[tuple[str, str]] = RESOURCE_MIRRORS,
) -> list[str]:
    """Return deterministic drift diagnostics without modifying any files."""

    errors: list[str] = []
    for source_relative, export_relative in mirrors:
        source_relative_path = Path(source_relative)
        export_relative_path = Path(export_relative)
        try:
            source = _safe_tree_path(root, source_relative)
            export = _safe_tree_path(root, export_relative)
            canonical_files = _tree_contents(source, required=True)
            exported_files = _tree_contents(export, required=False)
        except (OSError, ResourceSyncError) as exc:
            errors.append(str(exc))
            continue

        for relative in sorted(set(canonical_files) | set(exported_files)):
            canonical = canonical_files.get(relative)
            exported = exported_files.get(relative)
            export_path = export_relative_path / relative
            canonical_path = source_relative_path / relative
            if canonical is None:
                errors.append(f"{export_path}: extra export; no packaged resource at {canonical_path}")
            elif exported is None:
                errors.append(f"{export_path}: missing export of packaged resource {canonical_path}")
            elif exported != canonical:
                errors.append(f"{export_path}: differs from packaged resource {canonical_path}")
    return errors


def sync_resources(
    root: Path = ROOT,
    *,
    mirrors: Iterable[tuple[str, str]] = RESOURCE_MIRRORS,
) -> tuple[str, ...]:
    """Export canonical resource trees, removing stale files in those exports.

    All trees are read and checked for unsafe symlinks before the first write.
    Re-running after a successful export makes no changes.
    """

    plans: list[tuple[Path, Path, dict[Path, bytes], dict[Path, bytes]]] = []
    for source_relative, export_relative in mirrors:
        source = _safe_tree_path(root, source_relative)
        export = _safe_tree_path(root, export_relative)
        canonical_files = _tree_contents(source, required=True)
        exported_files = _tree_contents(export, required=False)
        plans.append((Path(source_relative), Path(export_relative), canonical_files, exported_files))

    changes: list[str] = []
    for source_relative, export_relative, canonical_files, exported_files in plans:
        export = root / export_relative
        for relative, payload in sorted(canonical_files.items()):
            if exported_files.get(relative) == payload:
                continue
            destination = export / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            changes.append((export_relative / relative).as_posix())

        for relative in sorted(set(exported_files) - set(canonical_files)):
            (export / relative).unlink()
            changes.append((export_relative / relative).as_posix())

        if export.exists():
            for directory in sorted(
                (path for path in export.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    return tuple(changes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="check exports without changing files (the default)",
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help="explicitly regenerate repository-level exports from packaged resources",
    )
    args = parser.parse_args()

    try:
        if args.sync:
            changed = sync_resources()
            if changed:
                print(f"Synchronized {len(changed)} exported resource file(s).")
                for path in changed:
                    print(f"- {path}")
            else:
                print("Resource exports already match packaged resources.")
            return 0

        errors = check_resources()
    except (OSError, ResourceSyncError) as exc:
        errors = [str(exc)]

    if errors:
        print("Resource export validation failed:")
        for error in errors:
            print(f"- {error}")
        print("Run python3 scripts/sync_resources.py --sync to regenerate exports.")
        return 1
    print("Resource exports match packaged runtime resources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
