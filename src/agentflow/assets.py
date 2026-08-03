from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any


class AssetError(RuntimeError):
    """A safe, user-facing imported-asset trust error."""


LOCK_VERSION = 1
ASSET_KINDS = ("skill", "plugin", "mcp")
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LOCK_REQUIRED_FIELDS = ("source", "revision", "sha256", "entrypoint", "reviewer", "approved_at")
QUARANTINE_STATUSES = ("tampered", "capability_expanded")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_root(path: Path) -> Path:
    """Canonicalize an asset root and confirm it is a real directory.

    Symlinks *in the root path itself* are resolved (macOS `/tmp` is a symlink
    to `/private/tmp`, so callers legitimately reach assets through one); it is
    members *inside* the root that must not be symlinks (see below). Raises
    ``AssetError`` if the root cannot be resolved or is not a directory.
    """
    root = Path(path).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AssetError(f"asset root cannot be resolved: {path}") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise AssetError(f"asset path is not a directory: {path}")
    return resolved


def iter_asset_files(root: Path) -> list[Path]:
    """Every regular file strictly inside ``root``, sorted, symlinks refused.

    Walks the tree using file descriptors with O_NOFOLLOW to eliminate check/use
    races. Any symlink (to a file, directory, or dangling), any special file
    (fifo/socket/device), and by construction any member that would escape the
    canonical root, is rejected with ``AssetError`` — nothing under the tree is
    hashed until the whole tree is proven to be ordinary files inside the boundary.
    """
    files: list[Path] = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        stack = [(root, root_fd)]
        visited_fds = {root_fd}
        while stack:
            current_path, current_fd = stack.pop()
            with os.scandir(current_fd) as entries:
                for entry in entries:
                    entry_path = current_path / entry.name
                    if entry.is_symlink():
                        raise AssetError(f"refusing symlink inside asset root: {entry_path}")
                    if entry.is_dir(follow_symlinks=False):
                        # Open directory with O_NOFOLLOW | O_DIRECTORY
                        try:
                            dir_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                        except (OSError, FileNotFoundError) as exc:
                            raise AssetError(f"cannot open directory inside asset root: {entry_path}") from exc
                        stack.append((entry_path, dir_fd))
                        visited_fds.add(dir_fd)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(entry_path)
                    else:
                        raise AssetError(
                            f"refusing non-regular file inside asset root: {entry_path}"
                        )
    finally:
        for fd in visited_fds:
            try:
                os.close(fd)
            except OSError:
                pass
    return sorted(files)


def validate_entrypoint(root: Path, entrypoint: str) -> Path:
    """Resolve and validate an entrypoint declared relative to ``root``.

    The entrypoint must be a relative path (no absolute members, no ``..``
    traversal), every path component must be a non-symlink, and the target must
    be an existing regular file that stays strictly inside the canonical root.
    Uses descriptor-based operations with O_NOFOLLOW to eliminate check/use races.
    Any violation raises ``AssetError`` before the asset is trusted.
    """
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise AssetError("entrypoint is required")
    candidate = Path(entrypoint)
    if candidate.is_absolute():
        raise AssetError(f"entrypoint must be relative to the asset root: {entrypoint}")
    if any(part == ".." for part in candidate.parts):
        raise AssetError(f"entrypoint escapes the asset root: {entrypoint}")

    # Walk the path component-by-component using descriptors with O_NOFOLLOW
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current_fd = root_fd
        temp_fds = []
        parts = list(candidate.parts)

        # Navigate directories
        for part in parts[:-1]:
            try:
                dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                temp_fds.append(dir_fd)
                current_fd = dir_fd
            except OSError as exc:
                raise AssetError(f"entrypoint path component is invalid or a symlink: {root / '/'.join(parts[:parts.index(part)+1])}") from exc

        # Open and verify the final file with O_NOFOLLOW
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
            try:
                stat_info = os.fstat(file_fd)
                if not stat.S_ISREG(stat_info.st_mode):
                    raise AssetError(f"entrypoint is not a regular file: {root / entrypoint}")
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise AssetError(f"entrypoint does not exist or is a symlink: {root / entrypoint}") from exc
        finally:
            for fd in temp_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        os.close(root_fd)

    target = root / candidate
    try:
        target.resolve(strict=True).relative_to(root)
    except (ValueError, OSError) as exc:
        raise AssetError(f"entrypoint escapes the asset root: {entrypoint}") from exc
    return target


def hash_tree(path: Path) -> str:
    """A deterministic content hash of every regular file under an asset root.

    The tree is validated first using descriptor-based traversal with O_NOFOLLOW:
    symlinks, special files, and escaping members are rejected before any byte is
    hashed. Each file is opened with O_NOFOLLOW, validated via fstat to be a
    regular file, and hashed only from the verified descriptor. A symlink/special
    file swapped in after the tree walk but before read is detected and fails closed.
    """
    root = canonical_root(path)
    digest = hashlib.sha256()
    file_list = iter_asset_files(root)

    # Now hash each file using descriptor-based operations
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for file_path in file_list:
            relative = file_path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")

            # Open the file with O_NOFOLLOW relative to root
            rel_parts = relative.split('/')
            current_fd = root_fd
            temp_fds = []
            try:
                # Navigate to parent directories
                for part in rel_parts[:-1]:
                    dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                    temp_fds.append(dir_fd)
                    current_fd = dir_fd

                # Open the file itself with O_NOFOLLOW
                file_fd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
                try:
                    # Verify it's still a regular file via fstat
                    stat_info = os.fstat(file_fd)
                    if not stat.S_ISREG(stat_info.st_mode):
                        raise AssetError(
                            f"refusing non-regular file (swapped during hash): {file_path}"
                        )
                    # Read from the verified descriptor
                    while True:
                        chunk = os.read(file_fd, 65536)
                        if not chunk:
                            break
                        digest.update(chunk)
                finally:
                    os.close(file_fd)
            except (OSError, FileNotFoundError) as exc:
                raise AssetError(
                    f"cannot open file (may have been removed or swapped): {file_path}"
                ) from exc
            finally:
                for fd in temp_fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            digest.update(b"\0")
    finally:
        os.close(root_fd)

    return digest.hexdigest()


def validate_lock_data(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["lock root must be an object"]
    if data.get("version") != LOCK_VERSION:
        errors.append("version must be 1")
    assets = data.get("assets")
    if not isinstance(assets, list):
        errors.append("assets must be a list")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(assets, start=1):
        label = entry.get("name") if isinstance(entry, dict) else None
        prefix = f"asset {label or index}"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            errors.append(f"{prefix}: invalid name")
        elif name in seen:
            errors.append(f"{prefix}: duplicate asset name")
        else:
            seen.add(name)
        if entry.get("kind") not in ASSET_KINDS:
            errors.append(f"{prefix}: kind must be one of {', '.join(ASSET_KINDS)}")
        for field in LOCK_REQUIRED_FIELDS:
            if not isinstance(entry.get(field), str) or not entry.get(field):
                errors.append(f"{prefix}: {field} is required")
        sha256 = entry.get("sha256")
        if isinstance(sha256, str) and sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            errors.append(f"{prefix}: sha256 must be a 64-character hex digest")
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            errors.append(f"{prefix}: capabilities must be a list of strings")
    return errors


def load_lock(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {"version": LOCK_VERSION, "assets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AssetError(f"cannot read asset lockfile {path}: {exc}") from exc
    errors = validate_lock_data(data)
    if errors:
        raise AssetError(f"invalid asset lockfile {path}: {'; '.join(errors)}")
    return data


def save_lock(path: Path, data: dict[str, Any]) -> None:
    errors = validate_lock_data(data)
    if errors:
        raise AssetError(f"refusing to save invalid lockfile: {'; '.join(errors)}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def lock_asset(
    lock_path: Path,
    *,
    name: str,
    kind: str,
    asset_path: Path,
    source: str,
    revision: str,
    entrypoint: str,
    reviewer: str,
    capabilities: list[str],
) -> dict[str, Any]:
    """Compute provenance for an imported asset and record an immutable, approved lock entry."""
    if kind not in ASSET_KINDS:
        raise AssetError(f"unsupported asset kind: {kind}")
    if not NAME_PATTERN.fullmatch(name):
        raise AssetError(f"invalid asset name: {name}")
    if not reviewer.strip():
        raise AssetError("a named human reviewer is required to lock an asset")
    if not source.strip() or not revision.strip():
        raise AssetError("source and revision are required to lock an asset")
    root = canonical_root(asset_path)
    validate_entrypoint(root, entrypoint)
    digest = hash_tree(root)

    data = load_lock(lock_path)
    existing = next((e for e in data["assets"] if e["name"] == name), None)
    if existing is not None and existing["revision"] == revision and existing["sha256"] != digest:
        raise AssetError(
            f"asset {name} revision {revision} is already locked with a different hash; "
            "bump the revision before re-approving changed contents"
        )
    entry = {
        "name": name,
        "kind": kind,
        "source": source,
        "revision": revision,
        "sha256": digest,
        "entrypoint": entrypoint,
        "reviewer": reviewer,
        "approved_at": _now(),
        "capabilities": sorted(set(capabilities)),
    }
    assets = [e for e in data["assets"] if e["name"] != name]
    assets.append(entry)
    save_lock(lock_path, {"version": LOCK_VERSION, "assets": assets})
    return entry


@dataclasses.dataclass(frozen=True)
class AssetVerification:
    name: str
    status: str  # ok, tampered, capability_expanded, missing, missing_entrypoint, unapproved
    detail: str = ""


def _declared_capabilities(asset_path: Path) -> list[str] | None:
    """Read capabilities.json using descriptor-based operations with O_NOFOLLOW.

    Never follow a symlinked manifest: a symlink pointing outside the asset
    root could otherwise smuggle in a wider capability set than the tree hash
    would ever cover. iter_asset_files() rejects it too, but this read runs
    ahead of the tree hash by design, so we use O_NOFOLLOW to avoid races.
    """
    manifest_name = "capabilities.json"
    root_fd = os.open(asset_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            file_fd = os.open(manifest_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        except (OSError, FileNotFoundError):
            return None
        try:
            # Verify it's a regular file via fstat
            stat_info = os.fstat(file_fd)
            if not stat.S_ISREG(stat_info.st_mode):
                return None
            # Read from the verified descriptor
            content = b""
            while True:
                chunk = os.read(file_fd, 65536)
                if not chunk:
                    break
                content += chunk
            data = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        finally:
            os.close(file_fd)
    finally:
        os.close(root_fd)

    values = data.get("capabilities") if isinstance(data, dict) else None
    return [str(value) for value in values] if isinstance(values, list) else None


def verify_asset(entry: dict[str, Any], asset_path: Path) -> AssetVerification:
    name = str(entry.get("name"))
    if not entry.get("reviewer") or not entry.get("approved_at"):
        return AssetVerification(name, "unapproved", "missing reviewer or approval timestamp")
    try:
        root = canonical_root(asset_path)
    except AssetError as exc:
        return AssetVerification(name, "missing", str(exc))
    # Enforce the same entrypoint boundary as lock_asset: a relative, regular,
    # non-symlink file inside the root. A symlinked or escaping entrypoint that
    # appeared after approval is a boundary violation, not a benign miss.
    try:
        validate_entrypoint(root, str(entry.get("entrypoint") or ""))
    except AssetError as exc:
        return AssetVerification(name, "missing_entrypoint", str(exc))
    # Capability expansion is checked ahead of the tamper hash: capabilities.json
    # is itself part of the hashed tree, so a widened-but-otherwise-consistent
    # capabilities.json would otherwise always be masked as generic "tampered"
    # and never reach this more specific, actionable diagnosis. Any change that
    # is NOT an approved-capability superset still falls through to the hash
    # check below, so unrelated tampering is still caught.
    declared = _declared_capabilities(root)
    locked_capabilities = set(entry.get("capabilities") or [])
    if declared is not None and not set(declared) <= locked_capabilities:
        widened = sorted(set(declared) - locked_capabilities)
        return AssetVerification(
            name, "capability_expanded", f"undeclared capabilities: {', '.join(widened)}"
        )
    # A symlink/special/escaping member swapped into the tree after approval is
    # rejected here (fail-closed) and surfaced as tampering, since it both
    # violates the boundary and necessarily diverges from the locked hash.
    try:
        digest = hash_tree(root)
    except AssetError as exc:
        return AssetVerification(name, "tampered", str(exc))
    if digest != entry.get("sha256"):
        return AssetVerification(
            name, "tampered", f"hash mismatch: locked={entry.get('sha256')} actual={digest}"
        )
    return AssetVerification(name, "ok")


def quarantine_asset(asset_path: Path, quarantine_root: Path, *, reason: str) -> Path:
    """Move a compromised asset aside without deleting it; never overwrites."""
    asset_path = Path(asset_path)
    quarantine_root = Path(quarantine_root)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    destination = quarantine_root / asset_path.name
    counter = 1
    while destination.exists() or destination.is_symlink():
        destination = quarantine_root / f"{asset_path.name}-{counter}"
        counter += 1
    shutil.move(str(asset_path), str(destination))
    sidecar = destination.parent / f"{destination.name}.quarantine.json"
    sidecar.write_text(
        json.dumps(
            {"reason": reason, "quarantined_at": _now(), "original_path": str(asset_path)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def preflight_assets(
    lock_path: Path,
    assets_root: Path,
    *,
    quarantine_root: Path | None = None,
    auto_quarantine: bool = True,
) -> tuple[bool, list[dict[str, Any]]]:
    """Verify every locked asset and flag any unlocked asset present on disk."""
    data = load_lock(lock_path)
    assets_root = Path(assets_root)
    quarantine_root = Path(quarantine_root) if quarantine_root else assets_root.parent / "quarantine"
    report: list[dict[str, Any]] = []
    ok = True
    locked_names: set[str] = set()
    for entry in data["assets"]:
        name = entry["name"]
        locked_names.add(name)
        asset_path = assets_root / name
        verification = verify_asset(entry, asset_path)
        row: dict[str, Any] = {"name": name, "status": verification.status, "detail": verification.detail}
        if verification.status != "ok":
            ok = False
            if auto_quarantine and verification.status in QUARANTINE_STATUSES and asset_path.is_dir():
                destination = quarantine_asset(asset_path, quarantine_root, reason=verification.status)
                row["quarantined_to"] = str(destination)
        report.append(row)
    if assets_root.is_dir():
        for child in sorted(p for p in assets_root.iterdir() if p.is_dir()):
            if child.name in locked_names:
                continue
            ok = False
            report.append(
                {"name": child.name, "status": "unlocked", "detail": "no lock entry for imported asset"}
            )
    return ok, report


def install_asset(
    name: str,
    lock_path: Path,
    assets_root: Path,
    install_root: Path,
    *,
    dry_run: bool = False,
) -> str:
    """Install a locked, verified asset by symlink. Rejects unlocked/tampered/unapproved state."""
    data = load_lock(lock_path)
    entry = next((e for e in data["assets"] if e["name"] == name), None)
    if entry is None:
        raise AssetError(f"asset is not locked: {name}")
    asset_path = Path(assets_root) / name
    verification = verify_asset(entry, asset_path)
    if verification.status != "ok":
        raise AssetError(f"refusing to install {name}: {verification.status} ({verification.detail})")
    destination = Path(install_root) / name
    if destination.is_symlink() and destination.resolve() == asset_path.resolve():
        return "unchanged"
    if dry_run:
        return "would-install"
    if destination.exists() or destination.is_symlink():
        raise AssetError(f"install destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(asset_path.resolve(), target_is_directory=True)
    return "installed"
