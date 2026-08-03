"""Schema-versioned project configuration and local skill registration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "agentflow.project@1"
VERSION = 1
PROVIDERS = ("codex", "claude", "copilot")
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ConfigError(ValueError):
    pass


def default_data() -> dict[str, Any]:
    return {"schema": SCHEMA, "version": VERSION, "model_policy": ".agentflow/models-v1.json", "skills": []}


def config_path(root: Path) -> Path:
    return Path(root).resolve() / ".agentflow/config.json"


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


def validate(data: Any, root: Path) -> list[str]:
    if not isinstance(data, dict):
        return ["configuration root must be an object"]
    errors: list[str] = []
    allowed = {
        "schema", "version", "model_policy", "skills", "workflow_mode", "state_backend",
        "beads_sync_policy", "beads_parallel_mode", "require_approved_goal",
        "require_durable_work_item_before_implementation", "max_parallel_workers",
        "max_delegation_depth", "default_execution_lane", "external_session_tool",
        "direct_human_intervention_on_blocked", "require_external_preflight",
        "require_acceptance_matrix_for_substantial_work", "queue_scope", "queue_labels",
        "validation_lanes", "default_reporting", "merge_order",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append(f"unknown field(s): {', '.join(unknown)}")
    if data.get("schema") != SCHEMA or data.get("version") != VERSION:
        errors.append(f"schema/version must be {SCHEMA}/{VERSION}")
    if not isinstance(data.get("model_policy"), str) or not data.get("model_policy", "").strip():
        errors.append("model_policy must be a non-empty path")
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


def load(root: Path) -> dict[str, Any]:
    path = config_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {path}; run `agentflow init`") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"configuration is unreadable: {exc}") from exc
    errors = validate(data, Path(root))
    if errors:
        raise ConfigError("; ".join(errors))
    return data


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
