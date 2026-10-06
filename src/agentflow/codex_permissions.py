"""Strict, standalone Codex named-permission profile generation.

This helper is used by deterministic and opt-in no-turn probes only. It is not
wired into Agentflow worker admission or App Server launch.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

try:  # Python 3.10 uses the declared tomli compatibility dependency.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_PROFILE_NAME = "agentflow-shell-readonly"
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_LEGACY_KEYS = frozenset({"sandbox_mode", "sandbox_workspace_write", "sandbox", "--sandbox"})
_CONTROL_KEYS = frozenset({"default_permissions", "permissions", "approval_policy"})
_WORKSPACE_RULES = {
    ".": "read",
    ".agentflow": "deny",
    ".agentflow/controller-state": "deny",
}


class CodexPermissionConfigError(ValueError):
    """Safe validation error; messages never embed caller config or private paths."""


@dataclasses.dataclass(frozen=True)
class ReadonlyPermissionProfile:
    profile_name: str
    workspace_root: str
    protected_external_paths: tuple[str, ...]
    config_toml: str


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _canonical_directory(value: str | os.PathLike[str], *, label: str) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise CodexPermissionConfigError(f"{label} must be an absolute path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise CodexPermissionConfigError(f"{label} is malformed")
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        raise CodexPermissionConfigError(f"{label} must be normalized and absolute")
    canonical = os.path.realpath(raw)
    if not os.path.isdir(canonical):
        raise CodexPermissionConfigError(f"{label} must name an existing directory")
    return canonical


def _canonical_protected_path(value: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise CodexPermissionConfigError("protected path must be absolute") from exc
    if (
        not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw or "\r" in raw
        or not os.path.isabs(raw) or os.path.normpath(raw) != raw
        or any(char in raw for char in "*?[]")
    ):
        raise CodexPermissionConfigError("protected path must be a normalized absolute path")
    canonical = os.path.realpath(raw)
    if canonical == os.path.sep:
        raise CodexPermissionConfigError("filesystem root cannot be a protected path")
    return canonical


def _canonical_external_paths(
    values: Iterable[str | os.PathLike[str]], *, workspace_root: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CodexPermissionConfigError("protected paths must be a collection of paths")
    try:
        external = tuple(sorted({_canonical_protected_path(path) for path in values}))
    except TypeError as exc:
        raise CodexPermissionConfigError("protected paths must be a collection of paths") from exc
    if not external:
        raise CodexPermissionConfigError("at least one protected external path is required")
    for path in external:
        if path == workspace_root or path.startswith(workspace_root.rstrip(os.path.sep) + os.path.sep):
            raise CodexPermissionConfigError("protected external path overlaps the workspace")
        if workspace_root.startswith(path.rstrip(os.path.sep) + os.path.sep):
            raise CodexPermissionConfigError("protected external path overlaps the workspace")
    return external


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key.casefold()
            yield from _walk_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_keys(nested)


def validate_caller_config(config: str | Mapping[str, Any]) -> None:
    """Reject bases whose sandbox/profile/approval settings could conflict.

    This examines only a caller-supplied in-memory fixture; the helper never
    reads user, system, managed, or authentication configuration.
    """
    if isinstance(config, str):
        if len(config) > 1_000_000:
            raise CodexPermissionConfigError("caller config exceeds the supported size")
        try:
            parsed = tomllib.loads(config)
        except Exception as exc:
            raise CodexPermissionConfigError("caller config is not valid TOML") from exc
    elif isinstance(config, Mapping):
        parsed = config
    else:
        raise CodexPermissionConfigError("caller config must be TOML text or a mapping")
    keys = set(_walk_keys(parsed))
    if keys & _LEGACY_KEYS:
        raise CodexPermissionConfigError("caller config contains a legacy sandbox setting")
    if keys & _CONTROL_KEYS:
        raise CodexPermissionConfigError("caller config conflicts with the generated permission profile")


def validate_readonly_permission_config(
    config_toml: str,
    *,
    profile_name: str = DEFAULT_PROFILE_NAME,
    workspace_root: str | os.PathLike[str],
    protected_external_paths: Iterable[str | os.PathLike[str]],
) -> ReadonlyPermissionProfile:
    """Parse and verify every effective safety rule in a generated TOML string."""
    if not isinstance(profile_name, str) or not _PROFILE_NAME.fullmatch(profile_name):
        raise CodexPermissionConfigError("permission profile name is invalid")
    root = _canonical_directory(workspace_root, label="workspace root")
    external = _canonical_external_paths(protected_external_paths, workspace_root=root)
    if not isinstance(config_toml, str) or len(config_toml) > 1_000_000:
        raise CodexPermissionConfigError("permission profile TOML is malformed")
    try:
        parsed = tomllib.loads(config_toml)
    except Exception as exc:
        raise CodexPermissionConfigError("permission profile TOML is malformed") from exc
    keys = set(_walk_keys(parsed))
    if keys & _LEGACY_KEYS:
        raise CodexPermissionConfigError("legacy sandbox settings cannot compose with permission profiles")
    if set(parsed) != {"approval_policy", "default_permissions", "permissions"}:
        raise CodexPermissionConfigError("permission profile has unexpected top-level settings")
    if parsed.get("approval_policy") != "never":
        raise CodexPermissionConfigError("approval policy must be never")
    if parsed.get("default_permissions") != profile_name:
        raise CodexPermissionConfigError("default permission profile does not match")
    permissions = parsed.get("permissions")
    if not isinstance(permissions, Mapping) or set(permissions) != {profile_name}:
        raise CodexPermissionConfigError("permission profile table does not match")
    profile = permissions[profile_name]
    if not isinstance(profile, Mapping) or set(profile) != {"extends", "filesystem", "network"}:
        raise CodexPermissionConfigError("permission profile fields are incomplete or unexpected")
    if profile.get("extends") != ":read-only":
        raise CodexPermissionConfigError("permission profile must extend read-only")
    if profile.get("network") != {"enabled": False}:
        raise CodexPermissionConfigError("permission profile network access must be disabled")
    filesystem = profile.get("filesystem")
    if not isinstance(filesystem, Mapping):
        raise CodexPermissionConfigError("permission profile filesystem rules are missing")
    workspace_rules = filesystem.get(":workspace_roots")
    if workspace_rules != _WORKSPACE_RULES:
        raise CodexPermissionConfigError("workspace read and controller-state deny rules are required")
    expected_direct = {":root": "deny", ":minimal": "read", **{path: "deny" for path in external}}
    direct = {key: value for key, value in filesystem.items() if key != ":workspace_roots"}
    if direct != expected_direct:
        raise CodexPermissionConfigError("filesystem root, minimal, or external deny rules do not match")
    return ReadonlyPermissionProfile(profile_name, root, external, config_toml)


def build_readonly_permission_config(
    *,
    workspace_root: str | os.PathLike[str],
    protected_external_paths: Iterable[str | os.PathLike[str]],
    profile_name: str = DEFAULT_PROFILE_NAME,
    caller_config: str | Mapping[str, Any] | None = None,
) -> ReadonlyPermissionProfile:
    """Build the pinned read-only profile as documented TOML tables.

    Intended for private, isolated config homes and test probes. It does not
    alter config, select a transport, or authorize a worker launch.
    """
    if caller_config is not None:
        validate_caller_config(caller_config)
    if not isinstance(profile_name, str) or not _PROFILE_NAME.fullmatch(profile_name):
        raise CodexPermissionConfigError("permission profile name is invalid")
    root = _canonical_directory(workspace_root, label="workspace root")
    external = _canonical_external_paths(protected_external_paths, workspace_root=root)
    lines = [
        'approval_policy = "never"',
        f"default_permissions = {_toml_string(profile_name)}",
        "",
        f"[permissions.{profile_name}]",
        'extends = ":read-only"',
        "",
        f"[permissions.{profile_name}.filesystem]",
        '":root" = "deny"',
        '":minimal" = "read"',
    ]
    lines.extend(f"{_toml_string(path)} = \"deny\"" for path in external)
    lines.extend([
        "",
        f'[permissions.{profile_name}.filesystem.":workspace_roots"]',
        '"." = "read"',
        '".agentflow" = "deny"',
        '".agentflow/controller-state" = "deny"',
        "",
        f"[permissions.{profile_name}.network]",
        "enabled = false",
        "",
    ])
    rendered = "\n".join(lines)
    return validate_readonly_permission_config(
        rendered,
        profile_name=profile_name,
        workspace_root=root,
        protected_external_paths=external,
    )
