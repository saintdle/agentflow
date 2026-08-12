"""Schema-versioned project configuration and local skill registration."""

from __future__ import annotations

import json
import os
import re
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
    "model": "gpt-5.6-luna",
    "effort": "medium",
}


class ConfigError(ValueError):
    pass


def default_data() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "model_policy": ".agentflow/models-v1.json",
        "prose": {"editor": dict(DEFAULT_PROSE_EDITOR)},
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
    allowed = {"schema", "version", "model_policy", "prose", "skills"}
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
