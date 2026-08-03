"""Transactional migration from a recognized legacy Agentflow checkout.

The migration deliberately operates only on user-level integration links that
provably point at exact, known locations in the legacy checkout.  Project
repositories, Beads databases, session state, evidence, and provider-owned
configuration are outside this module's write boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from typing import Any, Iterator

from agentflow import __version__, resources


SCHEMA = "agentflow.legacy-migration@1"
MIGRATION_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")


class MigrationError(RuntimeError):
    """A migration safety check or transaction failed."""


def default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local/state") / "agentflow/migrations"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _migration_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o700)
    path = state_root / ".lock"
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("another Agentflow migration is active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"link\0" + os.readlink(path).encode("utf-8"))
        return digest.hexdigest()
    if path.is_file():
        digest.update(b"file\0" + path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        return "missing"
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        if child.is_symlink():
            digest.update(b"link\0" + relative + b"\0" + os.readlink(child).encode("utf-8"))
        elif child.is_file():
            digest.update(b"file\0" + relative + b"\0" + child.read_bytes())
        elif child.is_dir():
            digest.update(b"dir\0" + relative + b"\0")
    return digest.hexdigest()


def _copy_resource(parts: tuple[str, ...], destination: Path, *, tree: bool) -> None:
    source = resources.item(*parts)

    def copy_tree(item: Any, target: Path) -> None:
        target.mkdir(mode=0o755)
        for child in sorted(item.iterdir(), key=lambda entry: entry.name):
            child_target = target / child.name
            if child.is_dir():
                copy_tree(child, child_target)
            elif child.is_file():
                child_target.write_bytes(child.read_bytes())
                child_target.chmod(0o644)
            else:
                raise MigrationError(f"unsupported packaged resource entry: {child.name}")

    if tree:
        copy_tree(source, destination)
    else:
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o644)


def _candidate_operations(
    home: Path, legacy_root: Path, new_command: Path | None
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: list[tuple[str, Path, Path, tuple[str, ...] | None, bool]] = []
    candidates.append(
        ("cli", home / ".local/bin/agentflow", legacy_root / "bin/agentflow", None, False)
    )
    for name in resources.names("skills"):
        legacy_source = legacy_root / ".agents/skills" / name
        for provider_home in (home / ".agents/skills", home / ".claude/skills", home / ".copilot/skills"):
            candidates.append(
                ("resource-tree", provider_home / name, legacy_source, ("skills", name), True)
            )
    profile_sources = {
        "codex": legacy_root / ".codex/agents",
        "claude": legacy_root / ".claude/agents",
        "copilot": legacy_root / ".github/agents",
    }
    profile_destinations = {
        "codex": home / ".codex/agents",
        "claude": home / ".claude/agents",
        "copilot": home / ".copilot/agents",
    }
    for provider in sorted(profile_sources):
        for name in resources.names("agents", provider):
            candidates.append(
                (
                    "resource-file",
                    profile_destinations[provider] / name,
                    profile_sources[provider] / name,
                    ("agents", provider, name),
                    False,
                )
            )
    candidates.append(
        (
            "resource-file",
            home / ".codex/hooks.json",
            legacy_root / "templates/user/codex-hooks.json",
            ("templates", "user", "codex-hooks.json"),
            False,
        )
    )

    operations: list[dict[str, Any]] = []
    preserved: list[str] = []
    for kind, destination, legacy_source, resource_parts, tree in candidates:
        exists = destination.exists() or destination.is_symlink()
        if not exists:
            continue
        if not destination.is_symlink() or destination.resolve(strict=False) != legacy_source.resolve(strict=False):
            preserved.append(str(destination.absolute()))
            continue
        record: dict[str, Any] = {
            "kind": kind,
            "destination": str(destination.absolute()),
            "legacy_target": str(legacy_source.absolute()),
            "tree": tree,
            "resource": list(resource_parts or ()),
            "status": "planned",
        }
        if kind == "cli":
            record["new_target"] = str(new_command.absolute()) if new_command else ""
        operations.append(record)
    return operations, preserved


def _validate_legacy_root(path: Path) -> Path:
    root = path.expanduser().resolve(strict=True)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise MigrationError("legacy root must be a dedicated checkout, not a broad directory")
    markers = (root / "bin/agentflow", root / ".agents/skills")
    if not markers[0].is_file() or not markers[1].is_dir():
        raise MigrationError("legacy root does not have the recognized Agentflow checkout layout")
    return root


def _validate_new_command(command: Path | None, legacy_root: Path) -> Path:
    if command is None:
        raise MigrationError("--apply requires the packaged Agentflow executable to be running outside the legacy checkout")
    resolved = command.expanduser().resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise MigrationError("the packaged Agentflow executable is not an executable file")
    if _under(resolved, legacy_root):
        raise MigrationError("the replacement executable still belongs to the legacy checkout")
    try:
        version = subprocess.run(
            [str(resolved), "--version"], capture_output=True, text=True, timeout=8, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError("the replacement executable identity could not be verified") from exc
    if version.returncode != 0 or version.stdout.strip() != f"agentflow {__version__}":
        raise MigrationError(
            f"the replacement executable is not the running Agentflow {__version__} distribution"
        )
    return resolved


def active_legacy_processes(legacy_root: Path) -> list[int]:
    """Return direct legacy Agentflow processes; never expose command lines."""

    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=8, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError("cannot prove that legacy Agentflow processes are stopped") from exc
    if result.returncode != 0:
        raise MigrationError("cannot prove that legacy Agentflow processes are stopped")
    matches: list[int] = []
    executable_markers = (
        str(legacy_root / "bin/agentflow"),
        str(legacy_root / ".venv/bin/agentflow"),
        str(legacy_root / ".venv/bin/python"),
    )
    for line in result.stdout.splitlines():
        pieces = line.strip().split(maxsplit=1)
        if len(pieces) != 2 or not pieces[0].isdigit():
            continue
        pid, command = int(pieces[0]), pieces[1]
        if pid == os.getpid():
            continue
        if any(marker in command for marker in executable_markers):
            matches.append(pid)
    return sorted(matches)


def plan(
    legacy_root: Path,
    *,
    home: Path | None = None,
    new_command: Path | None = None,
) -> dict[str, Any]:
    legacy = _validate_legacy_root(legacy_root)
    target_home = (home or Path.home()).expanduser().resolve()
    operations, preserved = _candidate_operations(target_home, legacy, new_command)
    for directory in (target_home / ".agents/skills", target_home / ".claude/skills", target_home / ".copilot/skills"):
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_symlink() and not _under(child.resolve(strict=False), legacy):
                preserved.append(str(child.absolute()))
    return {
        "schema": SCHEMA,
        "mode": "dry-run",
        "legacy_root": str(legacy),
        "home": str(target_home),
        "operations": operations,
        "preserved_unmanaged_entries": sorted(set(preserved)),
        "protected_scopes": ["project repositories", "Beads databases", "sessions and evidence", "Git state"],
    }


def _stage_operation(record: dict[str, Any], staging: Path, index: int, new_command: Path) -> Path:
    target = staging / f"{index:03d}"
    if record["kind"] == "cli":
        target.symlink_to(new_command)
    else:
        _copy_resource(tuple(record["resource"]), target, tree=bool(record["tree"]))
    return target


def _destination_is_owned(record: dict[str, Any]) -> bool:
    destination = Path(record["destination"])
    return destination.is_symlink() and destination.resolve(strict=False) == Path(record["legacy_target"]).resolve(strict=False)


def apply(
    legacy_root: Path,
    *,
    new_command: Path,
    home: Path | None = None,
    state_root: Path | None = None,
    process_check: bool = True,
) -> dict[str, Any]:
    legacy = _validate_legacy_root(legacy_root)
    command = _validate_new_command(new_command, legacy)
    state = (state_root or default_state_root()).expanduser().resolve()
    with _migration_lock(state):
        if process_check:
            active = active_legacy_processes(legacy)
            if active:
                raise MigrationError(
                    "legacy Agentflow work is still active (PID(s): " + ", ".join(str(pid) for pid in active) + ")"
                )
        document = plan(legacy, home=home, new_command=command)
        operations = document["operations"]
        if not operations:
            raise MigrationError("no exact legacy-owned integration links were found")
        if not any(record["kind"] == "cli" for record in operations):
            raise MigrationError("the active agentflow command link is not owned by the legacy checkout")
        migration_id = _new_id()
        migration_dir = state / migration_id
        staging = migration_dir / "staging"
        backup = migration_dir / "backup"
        migration_dir.mkdir(parents=True, mode=0o700)
        staging.mkdir(mode=0o700)
        backup.mkdir(mode=0o700)
        manifest = {
            **document,
            "id": migration_id,
            "mode": "apply",
            "status": "prepared",
            "created_at": _now(),
            "new_command": str(command),
        }
        manifest_path = migration_dir / "manifest.json"
        _atomic_json(manifest_path, manifest)
        staged: list[Path] = []
        try:
            for index, record in enumerate(operations):
                staged.append(_stage_operation(record, staging, index, command))
            for index, (record, staged_path) in enumerate(zip(operations, staged)):
                if not _destination_is_owned(record):
                    raise MigrationError("an integration changed after planning; no unverified overwrite was attempted")
                destination = Path(record["destination"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup_path = backup / f"{index:03d}"
                os.replace(destination, backup_path)
                record["backup"] = str(backup_path)
                record["status"] = "backed-up"
                _atomic_json(manifest_path, manifest)
                try:
                    os.replace(staged_path, destination)
                except Exception:
                    os.replace(backup_path, destination)
                    record["status"] = "restored"
                    _atomic_json(manifest_path, manifest)
                    raise
                record["status"] = "installed"
                record["installed_digest"] = _tree_digest(destination)
                record["status"] = "applied"
                _atomic_json(manifest_path, manifest)
            manifest["status"] = "applied"
            manifest["completed_at"] = _now()
            _atomic_json(manifest_path, manifest)
            shutil.rmtree(staging, ignore_errors=True)
            return manifest
        except Exception as exc:
            rollback_errors: list[str] = []
            for record in reversed(operations):
                if record.get("status") not in {"backed-up", "installed", "applied"}:
                    continue
                destination = Path(record["destination"])
                backup_path = Path(record["backup"])
                try:
                    if record.get("status") in {"installed", "applied"} and (
                        destination.exists() or destination.is_symlink()
                    ):
                        if destination.is_dir() and not destination.is_symlink():
                            shutil.rmtree(destination)
                        else:
                            destination.unlink()
                    if backup_path.exists() or backup_path.is_symlink():
                        os.replace(backup_path, destination)
                    record["status"] = "restored"
                except OSError:
                    rollback_errors.append(destination.name)
            manifest["status"] = "rollback-failed" if rollback_errors else "rolled-back-after-failure"
            manifest["failure"] = type(exc).__name__
            manifest["rollback_error_count"] = len(rollback_errors)
            _atomic_json(manifest_path, manifest)
            if rollback_errors:
                raise MigrationError("migration failed and automatic rollback was incomplete; inspect the private manifest") from exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError("migration failed and was rolled back") from exc


def rollback(migration_id: str, *, state_root: Path | None = None) -> dict[str, Any]:
    if not MIGRATION_ID.fullmatch(migration_id):
        raise MigrationError("invalid migration ID")
    state = (state_root or default_state_root()).expanduser().resolve()
    with _migration_lock(state):
        manifest_path = state / migration_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError("migration manifest is missing or invalid") from exc
        if manifest.get("schema") != SCHEMA or manifest.get("id") != migration_id:
            raise MigrationError("migration manifest identity is invalid")
        if manifest.get("status") != "applied":
            raise MigrationError(f"migration is not rollbackable from status {manifest.get('status')!r}")
        operations = manifest.get("operations")
        if not isinstance(operations, list):
            raise MigrationError("migration manifest operations are invalid")
        for record in operations:
            destination = Path(str(record.get("destination") or ""))
            if record.get("status") != "applied" or _tree_digest(destination) != record.get("installed_digest"):
                raise MigrationError("an installed integration changed after migration; rollback refused before mutation")
            backup_path = Path(str(record.get("backup") or ""))
            if not backup_path.is_symlink():
                raise MigrationError("a migration backup is missing; rollback refused before mutation")
        staged_records: list[dict[str, Any]] = []
        try:
            for record in reversed(operations):
                destination = Path(record["destination"])
                backup_path = Path(record["backup"])
                tomb = manifest_path.parent / f"rollback-{secrets.token_hex(6)}"
                os.replace(destination, tomb)
                try:
                    os.replace(backup_path, destination)
                except Exception:
                    os.replace(tomb, destination)
                    raise
                record["rollback_tomb"] = str(tomb)
                record["status"] = "rollback-staged"
                staged_records.append(record)
                _atomic_json(manifest_path, manifest)
        except Exception as exc:
            recovery_errors = 0
            for record in reversed(staged_records):
                destination = Path(record["destination"])
                backup_path = Path(record["backup"])
                tomb = Path(record["rollback_tomb"])
                try:
                    os.replace(destination, backup_path)
                    os.replace(tomb, destination)
                    record["status"] = "applied"
                    record.pop("rollback_tomb", None)
                except OSError:
                    recovery_errors += 1
            manifest["status"] = "rollback-failed" if recovery_errors else "applied"
            manifest["rollback_error_count"] = recovery_errors
            _atomic_json(manifest_path, manifest)
            if recovery_errors:
                raise MigrationError("rollback failed and recovery was incomplete; inspect the private manifest") from exc
            raise MigrationError("rollback failed; the applied migration was restored") from exc
        manifest["status"] = "rolled-back"
        manifest["rolled_back_at"] = _now()
        tombs: list[Path] = []
        for record in operations:
            record["status"] = "rolled-back"
            raw_tomb = str(record.pop("rollback_tomb", ""))
            if raw_tomb:
                tombs.append(Path(raw_tomb))
        _atomic_json(manifest_path, manifest)
        for tomb in tombs:
            if tomb.is_dir() and not tomb.is_symlink():
                shutil.rmtree(tomb, ignore_errors=True)
            elif tomb.exists() or tomb.is_symlink():
                try:
                    tomb.unlink()
                except OSError:
                    pass
        return manifest
