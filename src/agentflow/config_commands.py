"""Project configuration command handlers and parser registration."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys
from typing import Any, Callable

from agentflow import installation as installation_backend
from agentflow import memory_runtime as memory_runtime_backend
from agentflow import project_config as project_config_backend
from agentflow import resources as packaged_resources


Handler = Callable[[argparse.Namespace], int]


def config_show(args: argparse.Namespace, *, project_config: Any = project_config_backend) -> int:
    root = Path(args.path).expanduser().resolve()
    try:
        data = project_config.load(root)
    except (project_config.ConfigError, OSError) as exc:
        print(f"Invalid Agentflow configuration: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def memory_status(
    args: argparse.Namespace, *, project_config: Any = project_config_backend,
    memory_runtime: Any = memory_runtime_backend,
) -> int:
    root = Path(args.root).expanduser().resolve()
    try:
        path = project_config.config_path(root)
        config = (
            project_config.load(root)
            if path.exists() or path.is_symlink()
            else project_config.default_data()
        )
        settings = project_config.memory_settings(config)
        value = memory_runtime.health(root, settings)
        value = {"enabled": bool(settings["enabled"]), "root": str(root), **value}
    except (project_config.ConfigError, OSError, ValueError) as exc:
        print(f"memory status: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(value, indent=2, sort_keys=True)
        if args.json else f"memory: {value.get('status', 'unknown')}"
    )
    return 0


def memory_maintain(
    args: argparse.Namespace, *, project_config: Any = project_config_backend,
    memory_runtime: Any = memory_runtime_backend,
) -> int:
    root = Path(args.root).expanduser().resolve()
    try:
        path = project_config.config_path(root)
        config = (
            project_config.load(root)
            if path.exists() or path.is_symlink()
            else project_config.default_data()
        )
        settings = project_config.memory_settings(config)
        runtime = memory_runtime.MemoryRuntime(root, settings)
        value = runtime.maintain(force=True)
    except (project_config.ConfigError, OSError, ValueError) as exc:
        print(f"memory maintain: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(value, indent=2, sort_keys=True)
        if args.json else f"memory maintenance: {value.get('status', 'unknown')}"
    )
    return 0


def _render_diff(path: Path, old: bytes | None, new: bytes) -> str:
    before = old.decode("utf-8").splitlines(keepends=True) if old is not None else []
    after = new.decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=str(path) if old is not None else f"{path} (absent)",
            tofile=str(path),
        )
    )


def memory_toggle(
    args: argparse.Namespace, *, project_config: Any = project_config_backend,
    memory_runtime: Any = memory_runtime_backend,
) -> int:
    root = Path(args.root).expanduser().resolve()
    enabled = args.action == "enable"
    scope = "local" if args.local else "shared"
    try:
        path, data, original, changed = project_config.prepare_memory_update(
            root, enabled=enabled, local=args.local
        )
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        diff = _render_diff(path, original, payload) if changed else ""
        backup: Path | None = None
        if changed and not args.dry_run:
            backup = project_config.write_private_file(
                path,
                payload,
                expected_original=original,
                state_home=memory_runtime.state_home(),
            )
        status = "would-update" if changed and args.dry_run else "updated" if changed else "unchanged"
    except (project_config.ConfigError, OSError, ValueError) as exc:
        print(f"config memory {args.action}: {exc}", file=sys.stderr)
        return 2
    value = {
        "action": args.action,
        "changed": changed,
        "enabled": enabled,
        "scope": scope,
        "status": status,
        "target": str(path),
        "diff": diff,
        "backup": str(backup) if backup else None,
    }
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(f"memory {status} ({scope}): {path}")
        if backup is not None:
            print(f"backup: {backup}")
        if args.dry_run and diff:
            print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


def _read_hook_target(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if path.parent.is_symlink():
        raise ValueError(f"refusing symlink hook directory: {path.parent}")
    if path.is_symlink():
        raise ValueError(f"refusing symlink hook configuration: {path}")
    if not path.exists():
        return {"hooks": {}}, None
    if not path.is_file():
        raise ValueError(f"hook configuration is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"hook configuration is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    return value, raw


def _previous_hook_ownership(
    provider: str, target: Path, installation: Any
) -> dict[str, list[dict[str, Any]]] | None:
    manifest = installation.load_managed_install_manifest()
    record = manifest.get("resources", {}).get(installation.managed_destination_key(target))
    expected = {
        "codex": ("codex-hooks", ["templates", "user", "codex-hooks.json"]),
        "claude": ("claude-hooks", ["templates", "project", "claude-settings.json"]),
    }
    kind, resource = expected[provider]
    if (
        isinstance(record, dict)
        and record.get("kind") == kind
        and record.get("resource") == resource
    ):
        handlers = record.get("managed_handlers")
        if isinstance(handlers, dict):
            return handlers
    return None


def hooks_merge(
    args: argparse.Namespace, *, project_config: Any = project_config_backend,
    installation: Any = installation_backend, memory_runtime: Any = memory_runtime_backend,
) -> int:
    root = Path(args.root).expanduser().resolve()
    if args.provider == "codex":
        target = Path.home() / ".codex/hooks.json"
        resource = ("templates", "user", "codex-hooks.json")
    else:
        target = root / ".claude/settings.json"
        resource = ("templates", "project", "claude-settings.json")
    try:
        existing, original = _read_hook_target(target)
        packaged = json.loads(packaged_resources.item(*resource).read_text(encoding="utf-8"))
        previously_owned = _previous_hook_ownership(args.provider, target, installation)
        merged, _ = installation.merge_agentflow_hook_config(
            existing, packaged, previously_owned=previously_owned
        )
        changed = merged != existing
        payload = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        diff = _render_diff(target, original, payload) if changed else ""
        owned_handlers = installation.merge_agentflow_hook_config(
            {"hooks": {}}, packaged
        )[1]
        manifest = installation.load_managed_install_manifest()
        record = {
            "resource": list(resource),
            "kind": "codex-hooks" if args.provider == "codex" else "claude-hooks",
            "managed_handlers": owned_handlers,
        }
        ownership_needed = (
            manifest.get("resources", {}).get(installation.managed_destination_key(target)) != record
        )
        backup: Path | None = None
        if changed and not args.dry_run:
            backup = project_config.write_private_file(
                target,
                payload,
                expected_original=original,
                state_home=memory_runtime.state_home(),
            )
        if ownership_needed and not args.dry_run:
            installation.record_managed_hook_config(
                target,
                provider=args.provider,
                handlers=owned_handlers,
            )
        status = (
            "would-merge" if changed and args.dry_run
            else "would-record-ownership" if ownership_needed and args.dry_run
            else "merged" if changed
            else "recorded" if ownership_needed
            else "unchanged"
        )
    except (OSError, ValueError, project_config.ConfigError) as exc:
        print(f"config hooks merge: {exc}", file=sys.stderr)
        return 2
    value = {
        "changed": changed,
        "diff": diff,
        "ownership_recorded": ownership_needed and not args.dry_run,
        "ownership_would_be_recorded": ownership_needed and args.dry_run,
        "provider": args.provider,
        "status": status,
        "target": str(target),
        "backup": str(backup) if backup else None,
    }
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(f"hooks {status} ({args.provider}): {target}")
        if backup is not None:
            print(f"backup: {backup}")
        if ownership_needed and not args.dry_run:
            print("managed hook ownership receipt updated")
        elif ownership_needed:
            print("dry-run: would update the private managed-hook ownership receipt")
        if args.dry_run and diff:
            print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


def register_parser(
    subparsers: Any, *, providers: tuple[str, ...],
    config_show_handler: Handler,
    memory_status_handler: Handler,
    memory_maintain_handler: Handler,
    memory_toggle_handler: Handler,
    hooks_merge_handler: Handler,
) -> None:
    """Register config and existing top-level memory commands in one group."""

    memory_parser = subparsers.add_parser(
        "memory", help="Inspect or maintain optional governed local memory"
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command", required=True)
    status_parser = memory_sub.add_parser("status")
    status_parser.add_argument("--root", default=".")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=memory_status_handler)
    maintain_parser = memory_sub.add_parser("maintain")
    maintain_parser.add_argument("--root", default=".")
    maintain_parser.add_argument("--json", action="store_true")
    maintain_parser.set_defaults(func=memory_maintain_handler)

    config_parser = subparsers.add_parser("config", help="Inspect and safely update project configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    show_parser = config_sub.add_parser("show", help="Validate and print the active configuration")
    show_parser.add_argument("path", nargs="?", default=".")
    show_parser.set_defaults(func=config_show_handler)

    memory_config_parser = config_sub.add_parser("memory", help="Enable or disable governed memory")
    memory_config_sub = memory_config_parser.add_subparsers(dest="config_memory_command", required=True)
    for action in ("enable", "disable"):
        action_parser = memory_config_sub.add_parser(action)
        action_parser.add_argument("--root", default=".")
        action_parser.add_argument("--local", action="store_true", help="Update the ignored machine-local layer")
        action_parser.add_argument("--dry-run", action="store_true")
        action_parser.add_argument("--json", action="store_true")
        action_parser.set_defaults(action=action, func=memory_toggle_handler)

    hooks_parser = config_sub.add_parser("hooks", help="Safely merge bundled native provider hooks")
    hooks_sub = hooks_parser.add_subparsers(dest="config_hooks_command", required=True)
    merge_parser = hooks_sub.add_parser("merge", help="Preview or merge Agentflow hooks without replacing custom handlers")
    merge_parser.add_argument(
        "--provider", choices=tuple(provider for provider in providers if provider in {"codex", "claude"}),
        required=True,
    )
    merge_parser.add_argument(
        "--root", default=".",
        help="Project root: Claude merges .claude/settings.json; Codex targets user-level ~/.codex/hooks.json",
    )
    merge_parser.add_argument("--dry-run", action="store_true")
    merge_parser.add_argument("--json", action="store_true")
    merge_parser.set_defaults(func=hooks_merge_handler)
