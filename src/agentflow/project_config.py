"""Schema-versioned project configuration and local skill registration."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "agentflow.project@1"
LOCAL_SCHEMA = "agentflow.project-local@1"
MANAGED_LINKS_SCHEMA = "agentflow.managed-skill-links@1"
VERSION = 1
PROVIDERS = ("codex", "claude", "copilot")
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EFFORT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
DEFAULT_PROSE_EDITOR = {
    "provider": "codex",
    "model": "gpt-6-luna",
    "effort": "medium",
}
DEFAULT_EXECUTION = {
    "controller_only": True,
    "max_parallel_workers": 3,
    "max_delegation_depth": 1,
    "max_attempts_per_task": 2,
    "launch_budget_multiplier": 2,
    "max_expensive_execution_children": 0,
}
DEFAULT_GUIDANCE = {
    "strategic_compaction": False,
    "verification": False,
    "context_pressure_percent": 75,
    "max_children_per_parent": 12,
}
DEFAULT_MEMORY = {
    "enabled": False,
    "on_prompt": False,
    "capture_failures": True,
    "max_items": 5,
    "max_chars": 2000,
    "max_age_days": 30,
    "scopes": ["project"],
    "scope_id": "",
    "max_events": 10_000,
    "max_event_bytes": 5 * 1024 * 1024,
    "retention_days": 30,
    "session_retention_days": 30,
    "maintenance_interval_seconds": 300,
    "session_ledger_limit": 256,
    "startup_query": "agentflow",
}


class ConfigError(ValueError):
    pass


def default_data() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "model_policy": ".agentflow/models-v2.json",
        "prose": {"editor": dict(DEFAULT_PROSE_EDITOR)},
        "execution": dict(DEFAULT_EXECUTION),
        "guidance": dict(DEFAULT_GUIDANCE),
        "memory": dict(DEFAULT_MEMORY),
        "skills": [],
    }


def default_local_data() -> dict[str, Any]:
    return {"schema": LOCAL_SCHEMA, "version": VERSION, "skills": []}


def config_path(root: Path) -> Path:
    return Path(root).resolve() / ".agentflow/config.json"


def local_config_path(root: Path) -> Path:
    return Path(root).resolve() / ".agentflow/config.local.json"


def managed_links_path(root: Path) -> Path:
    return Path(root).resolve() / ".agentflow/managed-skill-links.json"


def _resolve_skill(root: Path, value: str) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else Path(root) / raw
    if candidate.is_symlink():
        raise ConfigError(f"skill path must not be a symlink: {value}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"skill path cannot be resolved: {value}") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise ConfigError(f"skill path must be a real directory: {value}")
    entrypoint = resolved / "SKILL.md"
    if not entrypoint.is_file() or entrypoint.is_symlink():
        raise ConfigError(f"skill path requires a regular SKILL.md: {value}")
    return resolved


def validate(data: Any, root: Path, *, local: bool = False) -> list[str]:
    if not isinstance(data, dict):
        return ["configuration root must be an object"]
    errors: list[str] = []
    # Keep the public schema limited to values consumed by runtime code.
    # Workflow guidance lives in the generated provider instructions; accepting
    # security-looking but unenforced switches here would create false trust.
    allowed = {"schema", "version", "model_policy", "prose", "execution", "guidance", "memory", "skills"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append(f"unknown field(s): {', '.join(unknown)}")
    expected_schema = LOCAL_SCHEMA if local else SCHEMA
    if data.get("schema") != expected_schema or data.get("version") != VERSION:
        errors.append(f"schema/version must be {expected_schema}/{VERSION}")
    if not local and (not isinstance(data.get("model_policy"), str) or not data.get("model_policy", "").strip()):
        errors.append("model_policy must be a non-empty path")
    if local and "model_policy" in data and (
        not isinstance(data.get("model_policy"), str) or not data.get("model_policy", "").strip()
    ):
        errors.append("model_policy must be a non-empty path when present")
    if "prose" in data:
        prose = data.get("prose")
        if not isinstance(prose, dict) or set(prose) != {"editor"}:
            errors.append("prose must contain exactly an editor field")
        else:
            editor = prose.get("editor")
            if editor is not None:
                allowed_editor = {"provider", "model", "effort", "max_ai_credits"}
                if (
                    not isinstance(editor, dict)
                    or set(editor) - allowed_editor
                    or not {"provider", "model", "effort"} <= set(editor)
                ):
                    errors.append(
                        "prose.editor must be null or contain provider, model, effort, "
                        "and optional max_ai_credits"
                    )
                else:
                    if editor.get("provider") not in PROVIDERS:
                        errors.append(f"prose.editor.provider must be one of {', '.join(PROVIDERS)}")
                    model = editor.get("model")
                    if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
                        errors.append("prose.editor.model has an invalid shape")
                    effort = editor.get("effort")
                    if not isinstance(effort, str) or not EFFORT_PATTERN.fullmatch(effort):
                        errors.append("prose.editor.effort has an invalid shape")
                    credits = editor.get("max_ai_credits")
                    if credits is not None and (
                        not isinstance(credits, int) or isinstance(credits, bool) or credits < 1
                    ):
                        errors.append("prose.editor.max_ai_credits must be a positive integer")
                    if editor.get("provider") == "copilot" and (
                        not isinstance(credits, int) or isinstance(credits, bool) or credits < 30
                    ):
                        errors.append("Copilot prose.editor requires max_ai_credits of at least 30")
    if "execution" in data:
        execution = data.get("execution")
        if not isinstance(execution, dict) or set(execution) != set(DEFAULT_EXECUTION):
            errors.append("execution must contain exactly the supported controller and launch-budget fields")
        else:
            if not isinstance(execution.get("controller_only"), bool):
                errors.append("execution.controller_only must be a boolean")
            for field, minimum in (
                ("max_parallel_workers", 1),
                ("max_delegation_depth", 0),
                ("max_attempts_per_task", 1),
                ("launch_budget_multiplier", 1),
                ("max_expensive_execution_children", 0),
            ):
                value = execution.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                    errors.append(f"execution.{field} must be an integer of at least {minimum}")
    if "guidance" in data:
        guidance = data.get("guidance")
        if not isinstance(guidance, dict) or set(guidance) != set(DEFAULT_GUIDANCE):
            errors.append("guidance must contain exactly the supported optional guidance fields")
        else:
            for field in ("strategic_compaction", "verification"):
                if not isinstance(guidance.get(field), bool):
                    errors.append(f"guidance.{field} must be a boolean")
            pressure = guidance.get("context_pressure_percent")
            if not isinstance(pressure, int) or isinstance(pressure, bool) or not 1 <= pressure <= 100:
                errors.append("guidance.context_pressure_percent must be between 1 and 100")
            children = guidance.get("max_children_per_parent")
            if not isinstance(children, int) or isinstance(children, bool) or children < 1:
                errors.append("guidance.max_children_per_parent must be positive")
    if "memory" in data:
        memory = data.get("memory")
        if not isinstance(memory, dict) or set(memory) != set(DEFAULT_MEMORY):
            errors.append("memory must contain exactly the supported opt-in retention, recall, and capture fields")
        elif isinstance(memory, dict):
            for field in ("enabled", "on_prompt", "capture_failures"):
                if not isinstance(memory.get(field), bool):
                    errors.append(f"memory.{field} must be a boolean")
            for field, minimum, maximum in (
                ("max_items", 1, 100), ("max_chars", 1, 100_000),
                ("max_events", 1, 1_000_000), ("max_event_bytes", 256, 100_000_000),
                ("retention_days", 0, 3650), ("session_retention_days", 0, 3650),
                ("maintenance_interval_seconds", 0, 86_400), ("session_ledger_limit", 1, 10_000),
            ):
                value = memory.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                    errors.append(f"memory.{field} must be an integer between {minimum} and {maximum}")
            age = memory.get("max_age_days")
            if not isinstance(age, (int, float)) or isinstance(age, bool) or age < 0 or age > 3650:
                errors.append("memory.max_age_days must be a non-negative number at most 3650")
            scopes = memory.get("scopes")
            if not isinstance(scopes, list) or not scopes or any(item not in {"user", "project", "root", "task"} for item in scopes):
                errors.append("memory.scopes must be a non-empty list of user, project, root, or task")
            scope_id = memory.get("scope_id")
            if not isinstance(scope_id, str) or len(scope_id) > 240 or any(ord(c) < 32 for c in scope_id):
                errors.append("memory.scope_id must be bounded text")
            startup_query = memory.get("startup_query")
            if not isinstance(startup_query, str) or len(startup_query) > 400 or any(ord(c) < 32 for c in startup_query):
                errors.append("memory.startup_query must be bounded text")
    entries = data.get("skills")
    if not isinstance(entries, list):
        errors.append("skills must be a list")
        return errors
    seen: set[str] = set()
    for index, skill in enumerate(entries, 1):
        prefix = f"skill {index}"
        if not isinstance(skill, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(skill) - {"name", "path", "providers"}:
            errors.append(f"{prefix} has unknown fields")
        name = skill.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            errors.append(f"{prefix} has an invalid name")
        elif name in seen:
            errors.append(f"duplicate skill name: {name}")
        else:
            seen.add(name)
        source = skill.get("path")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{prefix} requires path")
        else:
            try:
                _resolve_skill(root, source)
            except ConfigError as exc:
                errors.append(str(exc))
        providers = skill.get("providers", list(PROVIDERS))
        if not isinstance(providers, list) or not providers or any(p not in PROVIDERS for p in providers):
            errors.append(f"{prefix} providers must be a non-empty subset of {', '.join(PROVIDERS)}")
    return errors


def _read_layer(path: Path, root: Path, *, local: bool) -> dict[str, Any]:
    if path.is_symlink():
        raise ConfigError(f"configuration path must not be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {path}; run `agentflow init`") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"configuration is unreadable: {exc}") from exc
    errors = validate(data, Path(root), local=local)
    if errors:
        raise ConfigError(f"{path}: {'; '.join(errors)}")
    return data


def load_layers(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(root).resolve()
    shared = _read_layer(config_path(root), root, local=False)
    local_path = local_config_path(root)
    local = (
        _read_layer(local_path, root, local=True)
        if local_path.exists() or local_path.is_symlink()
        else default_local_data()
    )
    return shared, local


def merge(shared: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Merge validated layers; local scalars and same-name skills win."""

    result = dict(shared)
    if "model_policy" in local:
        result["model_policy"] = local["model_policy"]
    if "prose" in local:
        result["prose"] = local["prose"]
    if "execution" in local:
        result["execution"] = local["execution"]
    if "guidance" in local:
        result["guidance"] = local["guidance"]
    if "memory" in local:
        result["memory"] = local["memory"]
    merged_skills: dict[str, dict[str, Any]] = {}
    for entry in shared.get("skills", []):
        merged_skills[entry["name"]] = dict(entry)
    for entry in local.get("skills", []):
        merged_skills[entry["name"]] = dict(entry)
    result["skills"] = list(merged_skills.values())
    return result


def load(root: Path) -> dict[str, Any]:
    shared, local = load_layers(root)
    return merge(shared, local)


def prose_editor(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the configured exact editor route, defaulting legacy projects safely."""

    prose = data.get("prose")
    if prose is None:
        return dict(DEFAULT_PROSE_EDITOR)
    editor = prose.get("editor")
    return dict(editor) if isinstance(editor, dict) else None


def execution_settings(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("execution")
    return dict(value) if isinstance(value, dict) else dict(DEFAULT_EXECUTION)


def guidance_settings(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("guidance")
    return dict(value) if isinstance(value, dict) else dict(DEFAULT_GUIDANCE)


def memory_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Return strict memory settings while keeping schema-v1 legacy configs safe."""
    value = data.get("memory")
    if not isinstance(value, dict):
        return dict(DEFAULT_MEMORY)
    result = dict(DEFAULT_MEMORY)
    result.update(value)
    result["scopes"] = list(value.get("scopes", DEFAULT_MEMORY["scopes"]))
    return result


def skill_origins(root: Path) -> dict[str, str]:
    shared, local = load_layers(root)
    origins = {entry["name"]: "shared" for entry in shared["skills"]}
    origins.update({entry["name"]: "local" for entry in local["skills"]})
    return origins


def write_layer(root: Path, data: dict[str, Any], *, local: bool) -> Path:
    root = Path(root).resolve()
    errors = validate(data, root, local=local)
    if errors:
        raise ConfigError("; ".join(errors))
    path = local_config_path(root) if local else config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def prepare_memory_update(
    root: Path, *, enabled: bool, local: bool = False
) -> tuple[Path, dict[str, Any], bytes | None, bool]:
    """Validate both config layers and prepare a memory-only update.

    Legacy schema-v1 files may omit ``memory``. Updating such a file materializes
    the complete current defaults before changing only ``enabled``. Unknown or
    invalid data is never discarded as part of this operation.
    """

    root = Path(root).resolve()
    shared_path = config_path(root)
    local_path = local_config_path(root)
    if shared_path.parent.is_symlink() or local_path.parent.is_symlink():
        raise ConfigError(".agentflow must not be a symlink")

    def read(path: Path, *, is_local: bool, allow_missing: bool = False) -> tuple[dict[str, Any], bytes | None]:
        if path.is_symlink():
            raise ConfigError(f"configuration path must not be a symlink: {path}")
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if allow_missing:
                data = default_local_data() if is_local else default_data()
                errors = validate(data, root, local=is_local)
                if errors:
                    raise ConfigError("; ".join(errors))
                return data, None
            raise ConfigError(f"configuration not found: {path}; run `agentflow init`")
        except OSError as exc:
            raise ConfigError(f"configuration is unreadable: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"configuration is unreadable: {exc}") from exc
        errors = validate(data, root, local=is_local)
        if errors:
            raise ConfigError(f"{path}: {'; '.join(errors)}")
        return data, raw

    shared, shared_raw = read(shared_path, is_local=False)
    local_data, local_raw = read(local_path, is_local=True, allow_missing=True)
    target_path = local_path if local else shared_path
    target = copy.deepcopy(local_data if local else shared)
    original = local_raw if local else shared_raw
    if not local and "memory" in local_data:
        raise ConfigError(
            "shared memory is shadowed by .agentflow/config.local.json; use --local to change the effective setting"
        )
    previous = copy.deepcopy(target)
    memory = target.get("memory")
    if memory is None:
        memory = dict(DEFAULT_MEMORY)
        target["memory"] = memory
    elif isinstance(memory, dict):
        memory = dict(memory)
        target["memory"] = memory
    else:  # validate() normally rejects this; keep the mutation boundary explicit.
        raise ConfigError("memory must be an object")
    memory["enabled"] = bool(enabled)
    errors = validate(target, root, local=local)
    if errors:
        raise ConfigError(f"refusing invalid memory update: {'; '.join(errors)}")
    return target_path, target, original, target != previous


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _check_owned_regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"refusing non-regular or symlink destination: {path}")
    uid = _current_uid()
    if uid is not None and info.st_uid != uid:
        raise ConfigError(f"destination is not owned by the current user: {path}")
    return info


def _ensure_directory(path: Path, *, private: bool = False) -> None:
    """Create a directory chain without following symlink components."""

    path = Path(path)
    component = path
    while True:
        if component.is_symlink():
            raise ConfigError(f"refusing symlink directory component: {component}")
        if component.parent == component:
            break
        component = component.parent
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ConfigError(f"refusing symlink directory: {current}")
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ConfigError(f"cannot create directory: {path}")
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise ConfigError(f"directory is not a real directory: {current}")
    uid = _current_uid()
    if uid is not None and current.stat().st_uid != uid:
        raise ConfigError(f"directory is not owned by the current user: {current}")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        except OSError as exc:
            raise ConfigError(f"cannot create directory {directory}: {exc}") from exc
    if private:
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise ConfigError(f"cannot make backup directory private: {exc}") from exc
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"directory changed while preparing write: {path}")
    if uid is not None and info.st_uid != uid:
        raise ConfigError(f"directory is not owned by the current user: {path}")


def _private_backup(path: Path, original: bytes, state_home: Path) -> Path:
    state_home = Path(state_home).expanduser()
    _ensure_directory(state_home)
    backup_root = state_home / "config-backups"
    _ensure_directory(backup_root, private=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_id = hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()[:12]
    transaction = backup_root / f"{stamp}-{target_id}"
    try:
        transaction.mkdir(mode=0o700)
        transaction.chmod(0o700)
        backup = transaction / path.name
        descriptor = os.open(
            backup,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        directory = os.open(transaction, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return backup
    except OSError as exc:
        raise ConfigError(f"cannot create private backup for {path}: {exc}") from exc


def write_private_file(
    path: Path, payload: bytes, *, expected_original: bytes | None,
    state_home: Path,
) -> Path | None:
    """Atomically replace an owned regular file, retaining a private backup."""

    path = Path(path).expanduser()
    if path.parent.is_symlink():
        raise ConfigError(f"refusing symlink parent directory: {path.parent}")
    _ensure_directory(path.parent)
    exists = path.exists() or path.is_symlink()
    original: bytes | None = None
    if exists:
        _check_owned_regular(path)
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    if original != expected_original:
        raise ConfigError(f"{path} changed since it was inspected; refusing to overwrite")
    backup = _private_backup(path, original, state_home) if original is not None else None

    # Recheck after backup creation so a concurrent edit is never replaced.
    now_exists = path.exists() or path.is_symlink()
    if now_exists != exists:
        raise ConfigError(f"{path} changed since it was inspected; refusing to overwrite")
    if now_exists:
        _check_owned_regular(path)
        try:
            if path.read_bytes() != expected_original:
                raise ConfigError(f"{path} changed since it was inspected; refusing to overwrite")
        except OSError as exc:
            raise ConfigError(f"cannot recheck {path}: {exc}") from exc

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = -1
        if directory >= 0:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"cannot atomically replace {path}: {exc}") from exc
    return backup


def load_managed_links(root: Path) -> list[dict[str, str]]:
    path = managed_links_path(root)
    if path.is_symlink():
        raise ConfigError("managed skill-link registry must not be a symlink")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"managed skill-link registry is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != MANAGED_LINKS_SCHEMA or data.get("version") != VERSION:
        raise ConfigError("managed skill-link registry has an unsupported schema")
    entries = data.get("links")
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"name", "provider", "source", "destination"}
        or not all(isinstance(entry[field], str) and entry[field] for field in entry)
        or not NAME_PATTERN.fullmatch(entry["name"])
        or entry["provider"] not in PROVIDERS
        or not Path(entry["source"]).is_absolute()
        or not Path(entry["destination"]).is_absolute()
        for entry in entries
    ):
        raise ConfigError("managed skill-link registry has invalid links")
    return [dict(entry) for entry in entries]


def write_managed_links(root: Path, entries: list[dict[str, str]]) -> None:
    path = managed_links_path(root)
    if path.is_symlink():
        raise ConfigError("managed skill-link registry must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": MANAGED_LINKS_SCHEMA, "version": VERSION, "links": entries}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def record_managed_link(root: Path, *, name: str, provider: str, source: Path, destination: Path) -> None:
    entries = load_managed_links(root)
    record = {
        "name": name,
        "provider": provider,
        "source": str(source.resolve()),
        "destination": str(destination.absolute()),
    }
    key = (name, provider, str(destination.absolute()))
    entries = [
        entry for entry in entries
        if (entry["name"], entry["provider"], entry["destination"]) != key
    ]
    entries.append(record)
    entries.sort(key=lambda entry: (entry["name"], entry["provider"], entry["destination"]))
    write_managed_links(root, entries)


def skills(data: dict[str, Any], root: Path) -> list[tuple[str, Path, tuple[str, ...]]]:
    return [
        (entry["name"], _resolve_skill(root, entry["path"]), tuple(entry.get("providers", PROVIDERS)))
        for entry in data["skills"]
    ]


def provider_destination(provider: str, name: str) -> Path:
    homes = {
        "codex": (
            Path(os.environ["CODEX_HOME"]) / "skills"
            if os.environ.get("CODEX_HOME")
            else Path.home() / ".agents/skills"
        ),
        "claude": Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")) / "skills",
        "copilot": Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")) / "skills",
    }
    return homes[provider] / name
