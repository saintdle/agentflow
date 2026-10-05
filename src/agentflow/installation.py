"""Ownership-aware installation of bundled Agentflow assets and hooks."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from agentflow import resources as packaged_resources


MANAGED_INSTALL_SCHEMA = "agentflow.managed-install-assets@1"


def managed_install_manifest_path() -> Path:
    """Use stable user state, not provider-session-scoped AGENTFLOW_STATE_HOME."""

    xdg = os.environ.get("XDG_STATE_HOME", "")
    state_root = Path(xdg).expanduser() / "agentflow" if xdg else Path.home() / ".local/state/agentflow"
    return state_root / "install" / "managed-assets.json"


def managed_destination_key(destination: Path) -> str:
    return str(destination.expanduser().absolute())


def load_managed_install_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or managed_install_manifest_path()
    if manifest_path.parent.is_symlink():
        raise ValueError("managed install manifest directory is a symlink; refusing to trust it")
    if manifest_path.is_symlink():
        raise ValueError("managed install manifest is a symlink; refusing to trust it")
    if not manifest_path.exists():
        return {"schema": MANAGED_INSTALL_SCHEMA, "resources": {}}
    if not manifest_path.is_file():
        raise ValueError("managed install manifest is not a regular file")
    uid = getattr(os, "getuid", lambda: None)()
    if uid is not None and manifest_path.stat().st_uid != uid:
        raise ValueError("managed install manifest is not owned by the current user")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("managed install manifest is unreadable or invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != MANAGED_INSTALL_SCHEMA
        or not isinstance(value.get("resources"), dict)
    ):
        raise ValueError("managed install manifest has an unsupported schema")
    for record in value["resources"].values():
        if not isinstance(record, dict):
            raise ValueError("managed install manifest contains an invalid resource record")
        kind = record.get("kind")
        if (
            not isinstance(record.get("resource"), list)
            or any(not isinstance(part, str) for part in record["resource"])
            or not isinstance(kind, str)
            or kind not in {"file", "tree", "codex-hooks", "claude-hooks"}
        ):
            raise ValueError("managed install manifest contains an invalid resource identity")
        digest = record.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("managed install manifest contains an invalid digest")
        handlers = record.get("managed_handlers")
        if handlers is not None and (
            not isinstance(handlers, dict)
            or any(
                not isinstance(event, str)
                or not isinstance(items, list)
                or any(not isinstance(item, dict) for item in items)
                for event, items in handlers.items()
            )
        ):
            raise ValueError("managed install manifest contains invalid hook ownership evidence")
    return value


def save_managed_install_manifest(manifest: Mapping[str, Any], path: Path | None = None) -> None:
    manifest_path = path or managed_install_manifest_path()
    if manifest_path.parent.is_symlink() or manifest_path.is_symlink():
        raise ValueError("managed install manifest path contains a symlink")
    manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not manifest_path.parent.is_dir():
        raise ValueError("managed install manifest directory is not a directory")
    uid = getattr(os, "getuid", lambda: None)()
    if uid is not None and manifest_path.parent.stat().st_uid != uid:
        raise ValueError("managed install manifest directory is not owned by the current user")
    if manifest_path.exists():
        if not manifest_path.is_file():
            raise ValueError("managed install manifest is not a regular file")
        if uid is not None and manifest_path.stat().st_uid != uid:
            raise ValueError("managed install manifest is not owned by the current user")
    manifest_path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", dir=manifest_path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
        manifest_path.chmod(0o600)
        try:
            directory = os.open(manifest_path.parent, os.O_RDONLY)
        except OSError:
            directory = -1
        if directory >= 0:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _tree_contents(root: Any) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}

    def walk(current: Any, prefix: str = "") -> None:
        for child in sorted(current.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir():
                walk(child, relative)
            elif child.is_file():
                contents[relative] = child.read_bytes()
            else:
                raise OSError(f"unsupported resource entry: {relative}")

    walk(root)
    return contents


def _tree_digest(root: Any) -> str:
    inventory = _tree_contents(root)
    canonical_inventory = [
        [relative, hashlib.sha256(payload).hexdigest()]
        for relative, payload in sorted(inventory.items())
    ]
    canonical = json.dumps(canonical_inventory, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resource_digest(parts: tuple[str, ...], *, is_tree: bool) -> str:
    resource = packaged_resources.item(*parts)
    return _tree_digest(resource) if is_tree else hashlib.sha256(resource.read_bytes()).hexdigest()


def _destination_digest(destination: Path, *, is_tree: bool) -> str:
    return _tree_digest(destination) if is_tree else hashlib.sha256(destination.read_bytes()).hexdigest()


def _record_asset(
    manifest: dict[str, Any], destination: Path, parts: tuple[str, ...], *,
    kind: str, digest: str | None = None,
    managed_handlers: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    record: dict[str, Any] = {"resource": list(parts), "kind": kind}
    if digest is not None:
        record["sha256"] = digest
    if managed_handlers is not None:
        record["managed_handlers"] = {
            event: [dict(item) for item in items]
            for event, items in sorted(managed_handlers.items())
        }
    manifest["resources"][managed_destination_key(destination)] = record


def record_managed_hook_config(
    destination: Path,
    *,
    provider: str,
    handlers: Mapping[str, list[dict[str, Any]]],
    path: Path | None = None,
) -> None:
    """Persist exact managed handler leaves for a Codex or Claude hook target."""

    resources = {
        "codex": (("templates", "user", "codex-hooks.json"), "codex-hooks"),
        "claude": (("templates", "project", "claude-settings.json"), "claude-hooks"),
    }
    if provider not in resources:
        raise ValueError("managed hook provider must be codex or claude")
    parts, kind = resources[provider]
    manifest = load_managed_install_manifest(path)
    _record_asset(
        manifest,
        destination,
        parts,
        kind=kind,
        managed_handlers=handlers,
    )
    save_managed_install_manifest(manifest, path)


def _refresh_backup_path(destination: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity = hashlib.sha256(str(destination.absolute()).encode("utf-8")).hexdigest()[:12]
    xdg = os.environ.get("XDG_STATE_HOME", "")
    state_root = Path(xdg).expanduser() / "agentflow" if xdg else Path.home() / ".local/state/agentflow"
    backup_root = state_root / "backups" / stamp / identity
    backup_root.mkdir(parents=True, exist_ok=True)
    try:
        backup_root.chmod(0o700)
    except OSError:
        pass
    candidate = backup_root / destination.name
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = backup_root / f"{destination.name}.{suffix}"
        suffix += 1
    return candidate


def _copy_file(parts: tuple[str, ...], destination: Path, *, refresh: bool = False) -> str:
    existed = destination.exists() or destination.is_symlink()
    if existed:
        if destination.is_file() and not destination.is_symlink():
            try:
                if destination.read_bytes() == packaged_resources.item(*parts).read_bytes():
                    return "unchanged"
            except OSError:
                return "unreadable"
        if not refresh:
            return "preserved"
        if not destination.is_file() or destination.is_symlink():
            return "refused"
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if existed:
        backup = _refresh_backup_path(destination)
        destination.rename(backup)
    try:
        destination.write_bytes(packaged_resources.item(*parts).read_bytes())
    except Exception:
        if backup is not None:
            if destination.exists():
                destination.rename(_refresh_backup_path(destination))
            backup.rename(destination)
        raise
    return "updated" if existed else "created"


def _copy_tree(parts: tuple[str, ...], destination: Path, *, refresh: bool = False) -> str:
    existed = destination.exists() or destination.is_symlink()
    if existed:
        if destination.is_symlink() or not destination.is_dir():
            return "refused"
        try:
            if _tree_contents(destination) == _tree_contents(packaged_resources.item(*parts)):
                return "unchanged"
        except OSError:
            return "unreadable"
        if not refresh:
            return "stale"

    def copy_directory(source: Any, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=False)
        for child in source.iterdir():
            child_target = target / child.name
            if child.is_dir():
                copy_directory(child, child_target)
            elif child.is_file():
                child_target.write_bytes(child.read_bytes())
            else:
                raise OSError(f"unsupported packaged resource: {child.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if existed:
        backup = _refresh_backup_path(destination)
        destination.rename(backup)
    try:
        copy_directory(packaged_resources.item(*parts), destination)
    except Exception:
        if backup is not None:
            if destination.exists():
                destination.rename(_refresh_backup_path(destination))
            backup.rename(destination)
        raise
    return "refreshed" if existed else "installed"


def install_resource(
    parts: tuple[str, ...], destination: Path, manifest: dict[str, Any], *,
    is_tree: bool, dry_run: bool, refresh: bool,
) -> str:
    """Install or refresh a file/tree only when prior content proves ownership."""

    kind = "tree" if is_tree else "file"
    try:
        packaged_digest = _resource_digest(parts, is_tree=is_tree)
    except OSError:
        return "unreadable"
    existed = destination.exists() or destination.is_symlink()
    if not existed:
        if dry_run:
            return "would-install"
        result = _copy_tree(parts, destination) if is_tree else _copy_file(parts, destination)
        if result in {"installed", "created"}:
            _record_asset(manifest, destination, parts, kind=kind, digest=packaged_digest)
        return "installed" if result in {"installed", "created"} else result

    if destination.is_symlink():
        return "refused"
    if is_tree and not destination.is_dir():
        return "refused"
    if not is_tree and not destination.is_file():
        return "refused"
    try:
        current_digest = _destination_digest(destination, is_tree=is_tree)
    except OSError:
        return "unreadable"
    if current_digest == packaged_digest:
        # Exact packaged bytes are the only migration evidence accepted for
        # installs made before this manifest existed (including v0.0.7).
        _record_asset(manifest, destination, parts, kind=kind, digest=packaged_digest)
        return "unchanged"

    prior = manifest["resources"].get(managed_destination_key(destination))
    previously_owned = (
        isinstance(prior, dict)
        and prior.get("resource") == list(parts)
        and prior.get("kind") == kind
        and prior.get("sha256") == current_digest
    )
    if not previously_owned:
        return "preserved"
    if not refresh:
        return "stale"
    if dry_run:
        return "would-refresh"
    result = _copy_tree(parts, destination, refresh=True) if is_tree else _copy_file(parts, destination, refresh=True)
    if result in {"refreshed", "updated", "unchanged"}:
        _record_asset(manifest, destination, parts, kind=kind, digest=packaged_digest)
    return result


def _managed_hook_inventory(document: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    hooks = document.get("hooks")
    if not isinstance(hooks, Mapping):
        raise ValueError("packaged hook resource has no hooks object")
    result: dict[str, list[dict[str, Any]]] = {}
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise ValueError("packaged hook resource has an invalid event entry")
        handlers: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("packaged hook resource has an invalid handler group")
            handlers.extend(dict(handler) for handler in group["hooks"] if isinstance(handler, dict))
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError("packaged hook resource has an invalid handler")
        if handlers:
            result[event] = handlers
    return result


def _hook_identity(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def merge_agentflow_hook_config(
    existing: Mapping[str, Any], packaged: Mapping[str, Any], *,
    previously_owned: Mapping[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Merge exact Agentflow handler leaves; retain rule and top-level metadata.

    Codex and Claude settings share the event -> rule -> hooks shape.  Only an
    exact handler leaf shipped now or recorded as previously owned is replaced
    or removed; custom handler bodies are never inferred to be Agentflow-owned.
    """

    current_handlers = _managed_hook_inventory(packaged)
    owned_handlers = previously_owned or {}
    if not isinstance(owned_handlers, Mapping) or any(
        not isinstance(event, str)
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        for event, items in owned_handlers.items()
    ):
        raise ValueError("previous Agentflow hook ownership evidence is invalid")
    merged = json.loads(json.dumps(dict(existing)))
    hooks = merged.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing hook configuration has no hooks object")
    merged["hooks"] = hooks
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise ValueError("existing hook configuration has an invalid event entry")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("existing hook configuration has an invalid handler group")
            if "hooks" in group and not isinstance(group["hooks"], list):
                raise ValueError("existing hook configuration has an invalid handler list")
            if any(not isinstance(item, dict) for item in group.get("hooks", [])):
                raise ValueError("existing hook configuration has an invalid handler")

    for event, groups in hooks.items():
        prior_items = owned_handlers.get(event, [])
        current_items = current_handlers.get(event, [])
        prior_ids = {_hook_identity(item) for item in prior_items}
        current_by_id = {_hook_identity(item): item for item in current_items}
        replacement_by_id = dict(current_by_id)
        for index, prior_item in enumerate(prior_items):
            prior_id = _hook_identity(prior_item)
            if prior_id in replacement_by_id:
                continue
            command_keys = ("command", "bash", "powershell", "url")
            prior_commands = tuple(prior_item.get(key) for key in command_keys if key in prior_item)
            matches = [
                item for item in current_items
                if prior_commands
                and tuple(item.get(key) for key in command_keys if key in item) == prior_commands
            ]
            if len(matches) == 1:
                replacement_by_id[prior_id] = matches[0]
            elif len(prior_items) == len(current_items) and index < len(current_items):
                # Both inventories are exact Agentflow-owned event entries; a
                # stable position is an unambiguous migration when the event's
                # handler cardinality has not changed.
                replacement_by_id[prior_id] = current_items[index]
        recognized_ids = prior_ids | set(current_by_id)
        for group in groups:
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            seen_agentflow: set[str] = set()
            retained: list[dict[str, Any]] = []
            for handler in handlers:
                identity = _hook_identity(handler)
                if identity not in recognized_ids:
                    retained.append(handler)
                    continue
                replacement = replacement_by_id.get(identity)
                if replacement is None:
                    continue
                replacement_id = _hook_identity(replacement)
                if replacement_id not in seen_agentflow:
                    retained.append(dict(replacement))
                    seen_agentflow.add(replacement_id)
            group["hooks"] = retained

    for event, current_items in current_handlers.items():
        groups = hooks.setdefault(event, [])
        present: set[str] = set()
        for group in groups:
            handlers = group.get("hooks", [])
            if isinstance(handlers, list):
                present.update(_hook_identity(handler) for handler in handlers)
        missing = [item for item in current_items if _hook_identity(item) not in present]
        if not missing:
            continue
        missing_ids = {_hook_identity(item) for item in missing}
        for packaged_group in packaged["hooks"][event]:
            package_items = [
                item for item in packaged_group["hooks"]
                if isinstance(item, dict) and _hook_identity(item) in missing_ids
            ]
            if not package_items:
                continue
            group = {
                key: json.loads(json.dumps(value))
                for key, value in packaged_group.items()
                if key != "hooks"
            }
            group["hooks"] = [dict(item) for item in package_items]
            groups.append(group)
    return merged, current_handlers


def _write_with_private_backup(destination: Path, payload: bytes) -> None:
    original_mode = destination.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(original_mode)
        backup = _refresh_backup_path(destination)
        destination.rename(backup)
        try:
            os.replace(temporary, destination)
        except Exception:
            backup.rename(destination)
            backup = None
            raise
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        if backup is not None and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise


def install_codex_hooks(
    destination: Path, parts: tuple[str, ...], manifest: dict[str, Any], *,
    dry_run: bool, refresh: bool,
) -> str:
    try:
        packaged = json.loads(packaged_resources.item(*parts).read_text(encoding="utf-8"))
        if not isinstance(packaged, dict):
            return "refused"
        desired_handlers = _managed_hook_inventory(packaged)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "unreadable"
    if not (destination.exists() or destination.is_symlink()):
        if dry_run:
            return "would-install"
        result = _copy_file(parts, destination)
        if result in {"created", "unchanged"}:
            _record_asset(
                manifest, destination, parts, kind="codex-hooks",
                digest=_resource_digest(parts, is_tree=False), managed_handlers=desired_handlers,
            )
            return "installed" if result == "created" else "unchanged"
        return result
    if destination.is_symlink() or not destination.is_file():
        return "refused"
    try:
        existing = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "refused"
    if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
        return "refused"
    prior = manifest["resources"].get(managed_destination_key(destination))
    previously_owned = {}
    if (
        isinstance(prior, dict)
        and prior.get("resource") == list(parts)
        and prior.get("kind") == "codex-hooks"
        and isinstance(prior.get("managed_handlers"), dict)
    ):
        previously_owned = prior["managed_handlers"]
    try:
        merged, owned_handlers = merge_agentflow_hook_config(
            existing, packaged, previously_owned=previously_owned
        )
    except (ValueError, TypeError):
        return "refused"
    changed = merged != existing
    if changed and not refresh:
        return "stale" if previously_owned else "preserved"
    if changed and dry_run:
        return "would-refresh"
    serialized: bytes | None = None
    if changed:
        serialized = (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            _write_with_private_backup(destination, serialized)
        except OSError:
            return "unreadable"
    content = serialized if serialized is not None else destination.read_bytes()
    _record_asset(
        manifest, destination, parts, kind="codex-hooks",
        digest=hashlib.sha256(content).hexdigest(), managed_handlers=owned_handlers,
    )
    return "updated" if changed else "unchanged"
