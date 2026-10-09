#!/usr/bin/env python3
"""Opt-in, no-turn Codex SDK preflight; never a worker launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


SDK_PIN = "0.160.1"
PROFILE = "agentflow-shell-readonly"
MODEL = "gpt-6-luna"
EFFORT = "medium"
MAX_ATTEMPTS = 2
TOTAL_TIMEOUT = 60
RPC_TIMEOUT = 10
PROFILE_WAIT = 30
CLEANUP_TIMEOUT = 5
MAX_EVIDENCE = 16 * 1024
MAX_NOTIFICATION_EVENTS = 8
STATE_SCHEMA = "agentflow.codex_preflight_state.v1"
RESULT_SCHEMA = "agentflow.codex_preflight_result.v3"
FIXTURE = b"Synthetic preflight fixture. No project source or skills.\n"
CHILD_MODE_VALUE = "parent-issued-v1"
CHILD_TOKEN_ENV = "AGENTFLOW_PREFLIGHT_CHILD_TOKEN"
CHILD_MODE_ENV = "AGENTFLOW_PREFLIGHT_CHILD_MODE"
CHILD_WORKSPACE_ENV = "AGENTFLOW_PREFLIGHT_WORKSPACE"
NORMAL_CODEX_HOME_ENV = "AGENTFLOW_PREFLIGHT_NORMAL_CODEX_HOME"
DARWIN_TEXT_ENCODING_ENV = "__CF_USER_TEXT_ENCODING"

_CODES = frozenset({
    "authentication_required", "sdk_unavailable", "sdk_pin_mismatch",
    "initialize_failed", "config_unverified", "legacy_sandbox_present",
    "thread_start_failed", "thread_start_ambiguous", "settings_timeout", "settings_unsupported",
    "settings_notification_budget_exhausted",
    "settings_mismatch", "tool_inventory_unverified", "rpc_timeout", "rpc_failed",
    "cleanup_failed", "child_timeout", "child_result_invalid",
    "attempt_budget_exhausted", "attempt_state_invalid", "attempt_lock_unavailable",
    "trial_layout_invalid", "unsupported_platform",
})
_RPC_PHASES = frozenset({
    "client_start", "initialize", "config_read", "account_read",
    "thread_start", "settings_notification",
})
_RPC_FAILURE_KINDS = frozenset({"exception", "deadline"})
_RPC_FAILURE_CATEGORIES = frozenset({
    "sdk_transport_closed", "sdk_rpc_error", "sdk_error", "response_validation",
    "timeout_exception", "os_error", "value_error", "type_error", "unknown",
})
_NOTIFICATION_METHODS = frozenset({
    "thread_started", "thread_settings_updated", "account_updated", "app_list_updated", "unknown",
})
_NOTIFICATION_PAYLOADS = frozenset({
    "thread_started", "thread_settings_updated", "account_updated", "app_list_updated", "unknown",
})
_NOTIFICATION_MISMATCH_REASONS = frozenset({
    "method_unknown", "payload_type_mismatch", "target_thread_mismatch",
    "startup_payload_malformed", "startup_payload_not_empty", "settings_mismatch",
    "account_state_mismatch", "notification_limit",
})
_NOTIFICATION_METHOD_CATEGORIES = {
    "thread/started": "thread_started",
    "thread/settings/updated": "thread_settings_updated",
    "account/updated": "account_updated",
    "app/list/updated": "app_list_updated",
}


class Halt(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _CODES else "initialize_failed"
        super().__init__(self.code)


class RPCFailure(RuntimeError):
    """Sanitized outcome from one bounded SDK call; never retains its exception."""

    def __init__(self, code: str, phase: str, kind: str, category: str | None = None):
        self.code = code if code in _CODES else "rpc_failed"
        self.phase = phase if phase in _RPC_PHASES else "initialize"
        self.kind = kind if kind in _RPC_FAILURE_KINDS else "exception"
        self.category = (
            category if category in _RPC_FAILURE_CATEGORIES else "unknown"
        ) if self.kind == "exception" else None
        super().__init__(self.code)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _private_dir(path: Path, *, create: bool = True) -> None:
    if path.is_symlink():
        raise Halt("trial_layout_invalid")
    if not path.exists():
        if not create:
            raise Halt("trial_layout_invalid")
        path.mkdir(mode=0o700)
    info = path.stat()
    if not path.is_dir() or (info.st_mode & 0o077) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise Halt("trial_layout_invalid")


def _private_existing_dir(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_absolute() or path.resolve(strict=True) != path:
            raise Halt("trial_layout_invalid")
        info = path.stat()
    except OSError:
        raise Halt("trial_layout_invalid") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise Halt("trial_layout_invalid")


def _private_file(path: Path) -> bytes:
    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise Halt("trial_layout_invalid")
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_EVIDENCE or info.st_nlink != 1
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise Halt("trial_layout_invalid")
        return path.read_bytes()
    except OSError:
        raise Halt("trial_layout_invalid") from None


def _validate_private_tree(root: Path) -> None:
    pending = [(root, 0)]
    count = 0
    while pending:
        directory, depth = pending.pop()
        if depth > 8:
            raise Halt("trial_layout_invalid")
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise Halt("trial_layout_invalid") from None
        for entry in entries:
            count += 1
            if count > 512:
                raise Halt("trial_layout_invalid")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise Halt("trial_layout_invalid") from None
            if (stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise Halt("trial_layout_invalid")
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), depth + 1))
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Halt("trial_layout_invalid")


def _write_private(path: Path, data: bytes) -> None:
    if len(data) > MAX_EVIDENCE:
        raise Halt("attempt_state_invalid")
    fd, temporary = tempfile.mkstemp(prefix=".preflight-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": STATE_SCHEMA, "attempts": []}
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > MAX_EVIDENCE:
        raise Halt("attempt_state_invalid")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise Halt("attempt_state_invalid") from None
    if (not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA
            or not isinstance(state.get("attempts"), list) or len(state["attempts"]) > MAX_ATTEMPTS):
        raise Halt("attempt_state_invalid")
    for index, record in enumerate(state["attempts"], 1):
        if (not isinstance(record, dict) or record.get("attempt") != index
                or record.get("status") not in {"running", "blocked", "halted", "ambiguous"}
                or record.get("code") not in _CODES):
            raise Halt("attempt_state_invalid")
    return state


def _reserve_attempt(authority: Path) -> tuple[int, int]:
    if os.name != "posix":
        raise Halt("unsupported_platform")
    try:
        import fcntl
    except ImportError:
        raise Halt("unsupported_platform") from None
    lock_path = authority / "attempts.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        if "fd" in locals():
            os.close(fd)
        raise Halt("attempt_lock_unavailable") from None
    try:
        ledger_path = authority / "attempts.json"
        state = _load_state(ledger_path)
        if state["attempts"] and state["attempts"][-1]["status"] in {"running", "ambiguous"}:
            raise Halt("attempt_state_invalid")
        if len(state["attempts"]) >= MAX_ATTEMPTS:
            raise Halt("attempt_budget_exhausted")
        number = len(state["attempts"]) + 1
        state["attempts"].append({"attempt": number, "status": "running", "code": "tool_inventory_unverified"})
        _write_private(ledger_path, _json_bytes(state))
        return fd, number
    except Exception:
        os.close(fd)
        raise


def _finish_attempt(authority: Path, attempt: int, result: dict[str, Any]) -> None:
    path = authority / "attempts.json"
    state = _load_state(path)
    record = state["attempts"][attempt - 1]
    record["status"] = result["status"]
    record["code"] = result["code"]
    record["thread_start_attempted"] = result["thread_start_attempted"]
    record["profile_observed"] = result["profile_observed"]
    _write_private(path, _json_bytes(state))


def _prepare_trial(root: Path, attempt: int) -> tuple[Path, Path, dict[str, str], str, str]:
    if os.name != "posix":
        raise Halt("unsupported_platform")
    repo = Path(__file__).resolve().parents[2]
    codex_home = (Path.home() / ".codex").resolve()
    target = root.resolve()
    if (target == codex_home or target in codex_home.parents or codex_home in target.parents
            or target == repo or target in repo.parents or repo in target.parents):
        raise Halt("trial_layout_invalid")
    # Normalize benign platform aliases (for example macOS /tmp) before
    # recording the child's cwd and exact disposable paths in its environment.
    root = target
    if not root.exists():
        if not root.parent.is_dir() or root.parent.is_symlink():
            raise Halt("trial_layout_invalid")
        _private_dir(root)
        (root / ".trial-marker").write_text("codex-preflight-v1\n", encoding="utf-8")
        os.chmod(root / ".trial-marker", 0o600)
    else:
        _private_dir(root, create=False)
        if (root / ".trial-marker").is_symlink() or not (root / ".trial-marker").is_file():
            raise Halt("trial_layout_invalid")
    paths = {name: root / name for name in ("home", "codex_home", "config", "cache", "tmp", "authority")}
    for path in paths.values():
        _private_dir(path)
    workspace = root / f"workspace-{attempt:02d}"
    _private_dir(workspace)
    fixture_path = workspace / "fixture.txt"
    if fixture_path.exists() and fixture_path.read_bytes() != FIXTURE:
        raise Halt("trial_layout_invalid")
    fixture_path.write_bytes(FIXTURE)
    os.chmod(fixture_path, 0o600)

    config_file = paths["config"] / "permission-profile.toml"
    rendered = _render_profile_config(workspace, paths)
    if config_file.exists() and config_file.read_bytes() != rendered:
        raise Halt("trial_layout_invalid")
    if not config_file.exists():
        config_file.write_bytes(rendered)
        os.chmod(config_file, 0o600)
    env = {
        "HOME": str(paths["home"]), "CODEX_HOME": str(paths["codex_home"]),
        "XDG_CONFIG_HOME": str(paths["config"]), "XDG_CACHE_HOME": str(paths["cache"]),
        "TMPDIR": str(paths["tmp"]), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
        "AGENTFLOW_PREFLIGHT_CONFIG": str(config_file),
        CHILD_WORKSPACE_ENV: str(workspace),
        NORMAL_CODEX_HOME_ENV: str(codex_home),
    }
    if sys.platform == "darwin" and DARWIN_TEXT_ENCODING_ENV in os.environ:
        text_encoding = os.environ[DARWIN_TEXT_ENCODING_ENV]
        if not _valid_darwin_text_encoding(text_encoding):
            raise Halt("trial_layout_invalid")
        env[DARWIN_TEXT_ENCODING_ENV] = text_encoding
    config_hash = hashlib.sha256(rendered).hexdigest()
    fixture_hash = hashlib.sha256(FIXTURE).hexdigest()
    return workspace, paths["authority"], env, config_hash, fixture_hash


def _render_profile_config(workspace: Path, paths: dict[str, Path]) -> bytes:
    repo = Path(__file__).resolve().parents[2]
    src = str(repo / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from agentflow.codex_permissions import build_readonly_permission_config
    profile = build_readonly_permission_config(
        workspace_root=str(workspace),
        protected_external_paths=tuple(
            str(paths[name]) for name in ("home", "codex_home", "config", "cache", "tmp", "authority")
        ),
    )
    return profile.config_toml.encode("utf-8")


def _host_codex_home() -> Path:
    # Compare path metadata only; never open or copy normal-home credentials.
    if not hasattr(os, "getuid"):
        raise Halt("unsupported_platform")
    try:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        return (home / ".codex").resolve()
    except Exception:
        raise Halt("unsupported_platform") from None


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _valid_darwin_text_encoding(value: str) -> bool:
    """Accept only the bounded CoreFoundation marker for this macOS user."""
    if sys.platform != "darwin" or not hasattr(os, "getuid") or not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 3:
        return False
    user_id, encoding, language = parts
    if user_id.lower() != f"0x{os.getuid():X}".lower():
        return False
    for selector in (encoding, language):
        if (not selector.isascii() or not selector.isdecimal() or len(selector) > 5
                or (len(selector) > 1 and selector.startswith("0"))
                or int(selector) > 0xFFFF):
            return False
    return True


def _validate_child_context(env: dict[str, str], supplied_token: str) -> None:
    """Validate the disposable parent-created layout before importing the SDK."""
    if os.name != "posix":
        raise Halt("unsupported_platform")
    required = {
        "HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR",
        "PATH", "LANG", "LC_ALL", "AGENTFLOW_PREFLIGHT_CONFIG",
        CHILD_WORKSPACE_ENV, NORMAL_CODEX_HOME_ENV, CHILD_MODE_ENV, CHILD_TOKEN_ENV,
    }
    env_keys = set(env)
    extra_keys = env_keys - required
    if (not required.issubset(env_keys)
            or extra_keys - {DARWIN_TEXT_ENCODING_ENV}
            or (DARWIN_TEXT_ENCODING_ENV in extra_keys
                and not _valid_darwin_text_encoding(env[DARWIN_TEXT_ENCODING_ENV]))
            or env.get(CHILD_MODE_ENV) != CHILD_MODE_VALUE):
        raise Halt("trial_layout_invalid")
    token = env.get(CHILD_TOKEN_ENV, "")
    if (len(token) != 64 or any(char not in "0123456789abcdef" for char in token)
            or not secrets.compare_digest(token, supplied_token)):
        raise Halt("trial_layout_invalid")

    try:
        cwd = Path.cwd()
        workspace = Path(env[CHILD_WORKSPACE_ENV])
        normal_codex_home = Path(env[NORMAL_CODEX_HOME_ENV])
        if (not cwd.is_absolute() or not workspace.is_absolute()
                or str(workspace) != os.path.normpath(env[CHILD_WORKSPACE_ENV])
                or workspace != cwd or not workspace.name.startswith("workspace-")):
            raise Halt("trial_layout_invalid")
        attempt_text = workspace.name.removeprefix("workspace-")
        if attempt_text not in {"01", "02"}:
            raise Halt("trial_layout_invalid")
        attempt = int(attempt_text)
        root = workspace.parent
        repo = Path(__file__).resolve().parents[2]
        if (not normal_codex_home.is_absolute()
                or str(normal_codex_home) != os.path.normpath(env[NORMAL_CODEX_HOME_ENV])
                or normal_codex_home.resolve(strict=False) != normal_codex_home
                or normal_codex_home != _host_codex_home()
                or _paths_overlap(root, repo)
                or _paths_overlap(root, normal_codex_home)):
            raise Halt("trial_layout_invalid")
        if cwd.resolve(strict=True) != cwd or workspace.resolve(strict=True) != workspace:
            raise Halt("trial_layout_invalid")
    except Halt:
        raise
    except Exception:
        raise Halt("trial_layout_invalid") from None

    _private_existing_dir(root)
    paths = {name: root / name for name in ("home", "codex_home", "config", "cache", "tmp", "authority")}
    expected_env_paths = {
        "HOME": paths["home"], "CODEX_HOME": paths["codex_home"],
        "XDG_CONFIG_HOME": paths["config"], "XDG_CACHE_HOME": paths["cache"],
        "TMPDIR": paths["tmp"],
        "AGENTFLOW_PREFLIGHT_CONFIG": paths["config"] / "permission-profile.toml",
    }
    for key, expected in expected_env_paths.items():
        if env[key] != str(expected):
            raise Halt("trial_layout_invalid")
    for directory in (*paths.values(), workspace):
        _private_existing_dir(directory)

    marker = root / ".trial-marker"
    if _private_file(marker) != b"codex-preflight-v1\n":
        raise Halt("trial_layout_invalid")
    authority = paths["authority"]
    ledger = authority / "attempts.json"
    lock = authority / "attempts.lock"
    _private_file(lock)
    _private_file(ledger)
    state = _load_state(ledger)
    if (len(state["attempts"]) != attempt or not state["attempts"]
            or state["attempts"][-1]["status"] != "running"):
        raise Halt("trial_layout_invalid")

    expected_root_entries = {
        ".trial-marker", "home", "codex_home", "config", "cache", "tmp", "authority",
        *(f"workspace-{index:02d}" for index in range(1, attempt + 1)),
    }
    try:
        if {entry.name for entry in os.scandir(root)} != expected_root_entries:
            raise Halt("trial_layout_invalid")
        if {entry.name for entry in os.scandir(authority)} != {"attempts.json", "attempts.lock"}:
            raise Halt("trial_layout_invalid")
        if {entry.name for entry in os.scandir(paths["config"])} != {"permission-profile.toml"}:
            raise Halt("trial_layout_invalid")
    except OSError:
        raise Halt("trial_layout_invalid") from None

    for directory in (*paths.values(), *(root / f"workspace-{index:02d}" for index in range(1, attempt + 1))):
        _validate_private_tree(directory)
    for index in range(1, attempt + 1):
        prior_workspace = root / f"workspace-{index:02d}"
        _private_existing_dir(prior_workspace)
        if {entry.name for entry in os.scandir(prior_workspace)} != {"fixture.txt"}:
            raise Halt("trial_layout_invalid")
        if _private_file(prior_workspace / "fixture.txt") != FIXTURE:
            raise Halt("trial_layout_invalid")

    config_path = paths["config"] / "permission-profile.toml"
    if _private_file(config_path) != _render_profile_config(workspace, paths):
        raise Halt("trial_layout_invalid")


def _toml_overrides(config_text: str) -> tuple[str, ...]:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    tree = tomllib.loads(config_text)
    values: list[str] = []

    def key_text(key: str) -> str:
        return key if key.replace("_", "a").replace("-", "a").isalnum() else json.dumps(key)

    def value_text(value: Any) -> str:
        if isinstance(value, dict):
            entries = (f"{key_text(str(key))}={value_text(item)}" for key, item in value.items())
            return "{" + ",".join(entries) + "}"
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, bool):
            return str(value).lower()
        raise Halt("trial_layout_invalid")

    for key, value in tree.items():
        values.append(f"{key_text(str(key))}={value_text(value)}")
    return tuple(values)


def _bounded_call(
    client: Any, fn: Any, timeout: float, phase: str,
    *, deadline_code: str = "rpc_timeout", exception_code: str = "rpc_failed",
) -> Any:
    result: list[Any] = []
    failure: list[str] = []

    def invoke() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            failure.append(_exception_category(exc))

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise RPCFailure(deadline_code, phase, "deadline")
    if failure:
        raise RPCFailure(exception_code, phase, "exception", failure[0]) from None
    return result[0]


def _exception_category(exc: BaseException) -> str:
    """Reduce a caught exception to a fixed family without reading its contents."""
    exception_type = type(exc)
    builtin_categories = (
        (TimeoutError, "timeout_exception"),
        (OSError, "os_error"), (FileNotFoundError, "os_error"),
        (PermissionError, "os_error"), (ConnectionError, "os_error"),
        (BrokenPipeError, "os_error"), (ValueError, "value_error"),
        (TypeError, "type_error"),
    )
    for known_type, category in builtin_categories:
        if exception_type is known_type:
            return category

    # The pinned SDK imports this module before making bounded calls. Consult
    # only those already-loaded, fixed class identities; never import or inspect
    # the exception's class name, text, arguments, chains, or traceback.
    family_specs = (
        ("openai_codex.errors", (
            ("TransportClosedError", "sdk_transport_closed"),
            ("JsonRpcError", "sdk_rpc_error"),
            ("CodexRpcError", "sdk_rpc_error"),
            ("ParseError", "sdk_rpc_error"),
            ("InvalidRequestError", "sdk_rpc_error"),
            ("MethodNotFoundError", "sdk_rpc_error"),
            ("InvalidParamsError", "sdk_rpc_error"),
            ("InternalRpcError", "sdk_rpc_error"),
            ("ServerBusyError", "sdk_rpc_error"),
            ("RetryLimitExceededError", "sdk_rpc_error"),
            ("CodexError", "sdk_error"),
        )),
        ("pydantic", (("ValidationError", "response_validation"),)),
    )
    for module_name, known_classes in family_specs:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        try:
            module_namespace = module.__dict__
        except BaseException:
            return "unknown"
        if type(module_namespace) is not dict:
            return "unknown"
        for class_name, category in known_classes:
            known_type = dict.get(module_namespace, class_name)
            if type(known_type) is type and exception_type is known_type:
                return category
    return "unknown"


def _enum(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("root", value)
    if hasattr(value, "root"):
        value = value.root
    return str(getattr(value, "value", value))


def _path_text(value: Any) -> str:
    return str(getattr(value, "root", value))


def _config_map(config: Any) -> dict[str, Any]:
    if hasattr(config, "model_dump"):
        return config.model_dump(mode="json", by_alias=False, exclude_none=True)
    if isinstance(config, dict):
        return config
    raise Halt("config_unverified")


def _has_legacy_sandbox(value: Any) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=False, exclude_none=True)
    if isinstance(value, dict):
        return any(k in {"sandbox", "sandbox_mode", "sandbox_workspace_write"} or _has_legacy_sandbox(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_legacy_sandbox(item) for item in value)
    return False


def _load_sdk() -> tuple[Any, ...]:
    import openai_codex
    from openai_codex.client import CodexClient, CodexConfig
    from openai_codex.generated.v2_all import (
        ActivePermissionProfile, AccountUpdatedNotification, AppListUpdatedNotification,
        AskForApproval, AskForApprovalValue, ConfigReadParams, ConfigReadResponse,
        GetAccountParams, Thread, ThreadSettings, ThreadSettingsUpdatedNotification,
        ThreadStartParams, ThreadStartedNotification,
    )
    return (openai_codex, CodexClient, CodexConfig, AskForApproval,
            AskForApprovalValue, ConfigReadParams, ConfigReadResponse,
            GetAccountParams, ThreadSettingsUpdatedNotification, ThreadStartParams,
            ThreadStartedNotification, AccountUpdatedNotification,
            AppListUpdatedNotification, Thread, ThreadSettings, ActivePermissionProfile)


def _child_diagnostic(env: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA, "status": "halted", "code": "sdk_unavailable",
        "authenticated": False, "thread_start_attempted": False,
        "profile_observed": False, "thread_id_present": False,
        "instruction_sources_empty": None, "inventory_status": "unverified",
        "cleanup_ok": True, "failure_phase": None, "failure_kind": None,
        "failure_category": None, "notification_count": 0,
        "startup_notifications_skipped": 0,
        "first_notification_method": None, "first_notification_payload": None,
        "rejected_notification_method": None, "rejected_notification_payload": None,
        "notification_mismatch_reason": None,
    }
    client = None
    started = time.monotonic()
    try:
        (sdk, Client, Config, Approval, ApprovalValue, ReadParams, ReadResponse,
         AccountParams, SettingsNotification, ThreadParams, StartedNotification,
         AccountNotification, AppListNotification, Thread, ThreadSettings,
         ActivePermissionProfile) = _load_sdk()
        if str(getattr(sdk, "__version__", "")) != SDK_PIN:
            raise Halt("sdk_pin_mismatch")
        workspace = str(Path.cwd().resolve())
        config_text = Path(env["AGENTFLOW_PREFLIGHT_CONFIG"]).read_text(encoding="utf-8")
        overrides = (*_toml_overrides(config_text), 'cli_auth_credentials_store="file"',
                     f'model="{MODEL}"', f'model_reasoning_effort="{EFFORT}"',
                     'mcp_servers={}', 'plugins={}', 'apps={}', 'web_search="disabled"',
                     "features.web_search=false", "features.web_search_cached=false",
                     "features.web_search_request=false", "features.multi_agent=false",
                     "features.skill_mcp_dependency_install=false")
        sdk_env = {key: value for key, value in env.items() if not key.startswith("AGENTFLOW_PREFLIGHT_")}
        client = Client(Config(cwd=workspace, env=sdk_env, config_overrides=overrides, experimental_api=True))
        _bounded_call(client, client.start, RPC_TIMEOUT, "client_start")
        _bounded_call(client, client.initialize, RPC_TIMEOUT, "initialize")
        config_read = _bounded_call(
            client,
            lambda: client.request(
                "config/read",
                ReadParams(cwd=workspace, include_layers=True).model_dump(by_alias=True, exclude_none=True),
                response_model=ReadResponse,
            ),
            RPC_TIMEOUT, "config_read",
        )
        config_map = _config_map(config_read.config)
        if _has_legacy_sandbox(config_map) or _has_legacy_sandbox(getattr(config_read, "layers", None)):
            raise Halt("legacy_sandbox_present")
        if (config_map.get("default_permissions") != PROFILE
                or config_map.get("model") != MODEL
                or _enum(config_map.get("model_reasoning_effort")) != EFFORT
                or _enum(config_map.get("approval_policy")) != "never"):
            raise Halt("config_unverified")
        account = _bounded_call(
            client, lambda: client.account_read(AccountParams(refresh_token=False)),
            RPC_TIMEOUT, "account_read",
        )
        account_value = getattr(account, "account", None)
        account_root = getattr(account_value, "root", None)
        if account_value is None or getattr(account_root, "type", None) != "chatgpt":
            raise Halt("authentication_required")
        account_plan_value = getattr(account_root, "plan_type", None)
        account_plan_type = _enum(account_plan_value) if account_plan_value is not None else None
        result["authenticated"] = True
        params = ThreadParams(
            cwd=workspace, model=MODEL,
            config={"default_permissions": PROFILE},
            approval_policy=Approval(root=ApprovalValue.never),
        )
        result["thread_start_attempted"] = True
        response = _bounded_call(
            client, lambda: client.thread_start(params), RPC_TIMEOUT, "thread_start",
            deadline_code="thread_start_ambiguous", exception_code="thread_start_ambiguous",
        )
        thread = getattr(response, "thread", None)
        thread_id = str(getattr(thread, "id", ""))
        result["thread_id_present"] = bool(thread_id)
        if not thread_id:
            raise Halt("thread_start_ambiguous")
        if (str(getattr(response, "model", "")) != MODEL
                or os.path.realpath(_path_text(getattr(response, "cwd", ""))) != workspace):
            raise Halt("settings_mismatch")
        sources = getattr(response, "instruction_sources", None)
        result["instruction_sources_empty"] = sources == []
        deadline = time.monotonic() + PROFILE_WAIT
        observed_notifications = 0

        def reject_notification(
            method_category: str, payload_category: str, reason: str,
            *, code: str = "settings_unsupported",
        ) -> None:
            result["rejected_notification_method"] = method_category
            result["rejected_notification_payload"] = payload_category
            result["notification_mismatch_reason"] = reason
            raise Halt(code)

        while time.monotonic() < deadline:
            notification = _bounded_call(
                client, client.next_notification, deadline - time.monotonic(),
                "settings_notification", deadline_code="settings_timeout",
            )
            observed_notifications += 1
            method_value = getattr(notification, "method", None)
            payload = getattr(notification, "payload", None)
            method_category = (
                _NOTIFICATION_METHOD_CATEGORIES.get(method_value, "unknown")
                if type(method_value) is str else "unknown"
            )
            payload_categories = (
                (StartedNotification, "thread_started"),
                (SettingsNotification, "thread_settings_updated"),
                (AccountNotification, "account_updated"),
                (AppListNotification, "app_list_updated"),
            )
            payload_category = next(
                (category for payload_type, category in payload_categories if type(payload) is payload_type),
                "unknown",
            )
            if result["first_notification_method"] is None:
                result["first_notification_method"] = method_category
                result["first_notification_payload"] = payload_category
            result["notification_count"] = min(observed_notifications, MAX_NOTIFICATION_EVENTS)
            if observed_notifications > MAX_NOTIFICATION_EVENTS:
                reject_notification(
                    method_category, payload_category, "notification_limit",
                    code="settings_notification_budget_exhausted",
                )

            if method_category == "thread_started":
                if payload_category != "thread_started":
                    reject_notification(method_category, payload_category, "payload_type_mismatch")
                started_thread = getattr(payload, "thread", None)
                if (type(started_thread) is not Thread
                        or type(getattr(started_thread, "id", None)) is not str
                        or getattr(started_thread, "id", None) != thread_id):
                    reject_notification(method_category, payload_category, "target_thread_mismatch")
                result["startup_notifications_skipped"] += 1
                continue

            if method_category == "account_updated":
                if payload_category != "account_updated":
                    reject_notification(method_category, payload_category, "payload_type_mismatch")
                auth_mode_value = getattr(payload, "auth_mode", None)
                plan_type_value = getattr(payload, "plan_type", None)
                auth_mode = _enum(auth_mode_value) if auth_mode_value is not None else None
                plan_type = _enum(plan_type_value) if plan_type_value is not None else None
                # These pinned fields are nullable, so absence cannot confirm the earlier
                # account decision. Require its exact ChatGPT mode and an explicit matching plan.
                if (auth_mode != "chatgpt" or plan_type is None or account_plan_type is None
                        or plan_type != account_plan_type):
                    reject_notification(method_category, payload_category, "account_state_mismatch")
                result["startup_notifications_skipped"] += 1
                continue

            if method_category == "app_list_updated":
                if payload_category != "app_list_updated":
                    reject_notification(method_category, payload_category, "payload_type_mismatch")
                apps = getattr(payload, "data", None)
                if type(apps) is not list:
                    reject_notification(method_category, payload_category, "startup_payload_malformed")
                if apps:
                    reject_notification(method_category, payload_category, "startup_payload_not_empty")
                # Only the exact pinned app-list type with an empty list is harmless to skip.
                result["startup_notifications_skipped"] += 1
                continue

            if method_category == "unknown":
                reject_notification(method_category, payload_category, "method_unknown")

            if method_category != "thread_settings_updated":
                reject_notification(method_category, payload_category, "method_unknown")
            if payload_category != "thread_settings_updated":
                reject_notification(method_category, payload_category, "payload_type_mismatch")
            if (type(getattr(payload, "thread_id", None)) is not str
                    or getattr(payload, "thread_id", None) != thread_id):
                reject_notification(
                    method_category, payload_category, "target_thread_mismatch",
                    code="settings_mismatch",
                )
            settings = getattr(payload, "thread_settings", None)
            profile = getattr(settings, "active_permission_profile", None)
            if (type(settings) is not ThreadSettings
                    or type(profile) is not ActivePermissionProfile
                    or getattr(profile, "id", None) != PROFILE
                    or getattr(profile, "extends", None) != ":read-only"
                    or _path_text(getattr(settings, "cwd", "")) != workspace
                    or getattr(settings, "model", None) != MODEL
                    or _enum(getattr(settings, "effort", None)) != EFFORT
                    or _enum(getattr(settings, "approval_policy", None)) != "never"):
                reject_notification(
                    method_category, payload_category, "settings_mismatch",
                    code="settings_mismatch",
                )
            result["profile_observed"] = True
            break
        if not result["profile_observed"]:
            raise Halt("settings_timeout")
        if sources != []:
            raise Halt("tool_inventory_unverified")
        # The pinned API cannot enumerate the full effective tool/instruction surface or prove
        # lifecycle A1 enforcement; active-profile metadata remains diagnostic evidence only.
        raise Halt("tool_inventory_unverified")
    except RPCFailure as exc:
        result["code"] = exc.code
        result["status"] = "ambiguous" if exc.code == "thread_start_ambiguous" else "halted"
        result["failure_phase"] = exc.phase
        result["failure_kind"] = exc.kind
        result["failure_category"] = exc.category
    except Halt as exc:
        result["code"] = exc.code
        result["status"] = (
            "blocked" if exc.code == "authentication_required"
            else "ambiguous" if exc.code == "thread_start_ambiguous"
            else "halted"
        )
    except Exception:
        result["code"] = "initialize_failed"
        result["status"] = "halted"
    finally:
        if client is not None:
            closed = threading.Event()
            close_failed: list[bool] = []

            def close_client() -> None:
                try:
                    client.close()
                except Exception:
                    close_failed.append(True)
                finally:
                    closed.set()

            threading.Thread(target=close_client, daemon=True).start()
            result["cleanup_ok"] = closed.wait(CLEANUP_TIMEOUT) and not close_failed
            if not result["cleanup_ok"]:
                result["code"] = "cleanup_failed"
                result["status"] = "halted"
        result["elapsed_ms"] = min(TOTAL_TIMEOUT * 1000, max(0, int((time.monotonic() - started) * 1000)))
    return result


def _child_main(supplied_token: str) -> int:
    env = dict(os.environ)
    _validate_child_context(env, supplied_token)
    result = _child_diagnostic(env)
    encoded = _json_bytes(result)
    if len(encoded) > MAX_EVIDENCE:
        encoded = _json_bytes(_empty_child_result("child_result_invalid", cleanup_ok=False))
    sys.stdout.buffer.write(encoded)
    return 0


def _empty_child_result(code: str, *, cleanup_ok: bool = True) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA, "status": "halted", "code": code,
        "authenticated": False, "thread_start_attempted": False,
        "profile_observed": False, "thread_id_present": False,
        "instruction_sources_empty": None, "inventory_status": "unverified",
        "cleanup_ok": cleanup_ok, "elapsed_ms": 0,
        "failure_phase": None, "failure_kind": None, "failure_category": None,
        "notification_count": 0, "startup_notifications_skipped": 0,
        "first_notification_method": None, "first_notification_payload": None,
        "rejected_notification_method": None, "rejected_notification_payload": None,
        "notification_mismatch_reason": None,
    }


def _decode_child(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_EVIDENCE:
        raise Halt("child_result_invalid")
    try:
        value = json.loads(data)
    except Exception:
        raise Halt("child_result_invalid") from None
    expected = {"schema", "status", "code", "authenticated", "thread_start_attempted",
                "profile_observed", "thread_id_present", "instruction_sources_empty",
                "inventory_status", "cleanup_ok", "elapsed_ms", "failure_phase", "failure_kind",
                "failure_category", "notification_count", "startup_notifications_skipped",
                "first_notification_method", "first_notification_payload",
                "rejected_notification_method", "rejected_notification_payload",
                "notification_mismatch_reason"}
    if (not isinstance(value, dict) or set(value) != expected or value.get("schema") != RESULT_SCHEMA
            or value.get("status") not in {"blocked", "halted", "ambiguous"}
            or value.get("code") not in _CODES
            or not isinstance(value.get("thread_start_attempted"), bool)
            or not isinstance(value.get("profile_observed"), bool)):
        raise Halt("child_result_invalid")
    if (type(value.get("authenticated")) is not bool
            or type(value.get("thread_id_present")) is not bool
            or type(value.get("cleanup_ok")) is not bool
            or value.get("inventory_status") != "unverified"
            or not isinstance(value.get("elapsed_ms"), int)
            or isinstance(value.get("elapsed_ms"), bool)):
        raise Halt("child_result_invalid")
    if (value.get("instruction_sources_empty") is not None
            and type(value.get("instruction_sources_empty")) is not bool):
        raise Halt("child_result_invalid")
    notification_count = value.get("notification_count")
    skipped_count = value.get("startup_notifications_skipped")
    if (type(notification_count) is not int or not 0 <= notification_count <= MAX_NOTIFICATION_EVENTS
            or type(skipped_count) is not int or not 0 <= skipped_count <= notification_count):
        raise Halt("child_result_invalid")
    first_method, first_payload = (
        value.get("first_notification_method"), value.get("first_notification_payload")
    )
    rejected_method, rejected_payload = (
        value.get("rejected_notification_method"), value.get("rejected_notification_payload")
    )
    mismatch_reason = value.get("notification_mismatch_reason")
    if ((first_method is None) != (first_payload is None)
            or (first_method is None) != (notification_count == 0)
            or (first_method is not None
                and (not isinstance(first_method, str) or first_method not in _NOTIFICATION_METHODS
                     or not isinstance(first_payload, str) or first_payload not in _NOTIFICATION_PAYLOADS))
            or ((rejected_method is None) != (rejected_payload is None))
            or ((rejected_method is None) != (mismatch_reason is None))
            or (rejected_method is not None
                and (not isinstance(rejected_method, str) or rejected_method not in _NOTIFICATION_METHODS
                     or not isinstance(rejected_payload, str) or rejected_payload not in _NOTIFICATION_PAYLOADS
                     or not isinstance(mismatch_reason, str)
                     or mismatch_reason not in _NOTIFICATION_MISMATCH_REASONS))):
        raise Halt("child_result_invalid")
    failure_phase, failure_kind = value.get("failure_phase"), value.get("failure_kind")
    if ((failure_phase is None) != (failure_kind is None)
            or (failure_phase is not None
                and (not isinstance(failure_phase, str) or failure_phase not in _RPC_PHASES))
            or (failure_kind is not None
                and (not isinstance(failure_kind, str) or failure_kind not in _RPC_FAILURE_KINDS))):
        raise Halt("child_result_invalid")
    failure_category = value.get("failure_category")
    if failure_kind == "deadline":
        if failure_category is not None:
            raise Halt("child_result_invalid")
    elif failure_kind == "exception":
        if (not isinstance(failure_category, str)
                or failure_category not in _RPC_FAILURE_CATEGORIES):
            raise Halt("child_result_invalid")
    elif failure_category is not None:
        raise Halt("child_result_invalid")
    invalid_thread_start = (
        (failure_phase == "thread_start" and not value["thread_start_attempted"])
        or (value.get("code") == "thread_start_ambiguous"
            and (value.get("status") != "ambiguous" or not value["thread_start_attempted"]))
        or (value.get("status") == "ambiguous"
            and value.get("code") != "thread_start_ambiguous")
    )
    if invalid_thread_start:
        raise Halt("child_result_invalid")
    return {key: value[key] for key in expected}


def _decode_child_preflight_halt(data: bytes) -> dict[str, Any]:
    """Keep only the fixed pre-SDK layout rejection from the child entrypoint."""
    if len(data) > MAX_EVIDENCE:
        raise Halt("child_result_invalid")
    try:
        value = json.loads(data)
    except Exception:
        raise Halt("child_result_invalid") from None
    if (not isinstance(value, dict) or set(value) != {"status", "code"}
            or value.get("status") != "halted" or value.get("code") != "trial_layout_invalid"):
        raise Halt("child_result_invalid")
    # The child entrypoint emits this record only when its allowlisted
    # environment/path checks fail, before importing or starting the SDK.
    return _empty_child_result("trial_layout_invalid")


def _kill_owned_group(proc: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            try:
                proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return False
            return True
        except OSError:
            return False
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        os.killpg(proc.pid, 0)
        return False
    except ProcessLookupError:
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_trial(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    if os.name != "posix":
        raise Halt("unsupported_platform")
    if (not root.is_absolute() or root != Path(os.path.normpath(root)) or root.is_symlink()
            or not root.parent.is_dir()):
        raise Halt("trial_layout_invalid")
    repo = Path(__file__).resolve().parents[2]
    codex_home = (Path.home() / ".codex").resolve()
    target = root.parent.resolve() / root.name
    if (target == codex_home or target in codex_home.parents or codex_home in target.parents
            or target == repo or target in repo.parents or repo in target.parents):
        raise Halt("trial_layout_invalid")
    # Prepare layout before reserving; no SDK work can occur in this phase.
    if not root.exists():
        _private_dir(root)
    else:
        _private_dir(root, create=False)
    marker = root / ".trial-marker"
    if marker.exists():
        if marker.is_symlink() or marker.read_text(encoding="utf-8") != "codex-preflight-v1\n":
            raise Halt("trial_layout_invalid")
    elif any(root.iterdir()):
        raise Halt("trial_layout_invalid")
    else:
        marker.write_text("codex-preflight-v1\n", encoding="utf-8")
        os.chmod(marker, 0o600)
    authority = root / "authority"
    if not root.exists():
        # _prepare_trial creates the root and all owned directories below.
        _private_dir(root)
        (root / ".trial-marker").write_text("codex-preflight-v1\n", encoding="utf-8")
        os.chmod(root / ".trial-marker", 0o600)
    for name in ("home", "codex_home", "config", "cache", "tmp", "authority"):
        _private_dir(root / name)
    fd, attempt = _reserve_attempt(authority)
    os.close(fd)
    try:
        workspace, authority, env, config_hash, fixture_hash = _prepare_trial(root, attempt)
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "--child"]
        child_token = secrets.token_hex(32)
        env[CHILD_MODE_ENV] = CHILD_MODE_VALUE
        env[CHILD_TOKEN_ENV] = child_token
        proc = subprocess.Popen(
            command, cwd=workspace, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            remaining = max(0.1, TOTAL_TIMEOUT - CLEANUP_TIMEOUT - (time.monotonic() - started))
            stdout, _ = proc.communicate(input=(child_token + "\n").encode("ascii"), timeout=remaining)
            cleanup_ok = _kill_owned_group(proc)
            if proc.returncode == 0:
                result = _decode_child(stdout)
            elif proc.returncode == 2:
                result = _decode_child_preflight_halt(stdout)
            else:
                raise Halt("child_result_invalid")
            if not cleanup_ok:
                result["cleanup_ok"] = False
        except subprocess.TimeoutExpired:
            cleanup_ok = _kill_owned_group(proc)
            result = {"schema": RESULT_SCHEMA, "status": "ambiguous", "code": "child_timeout",
                      "thread_start_attempted": True, "profile_observed": False,
                      "cleanup_ok": cleanup_ok, "failure_phase": None,
                      "failure_kind": None, "failure_category": None,
                      "notification_count": 0, "startup_notifications_skipped": 0,
                      "first_notification_method": None, "first_notification_payload": None,
                      "rejected_notification_method": None, "rejected_notification_payload": None,
                      "notification_mismatch_reason": None}
        except Halt:
            cleanup_ok = _kill_owned_group(proc)
            result = {"schema": RESULT_SCHEMA, "status": "ambiguous", "code": "child_result_invalid",
                      "thread_start_attempted": True, "profile_observed": False,
                      "cleanup_ok": cleanup_ok, "failure_phase": None,
                      "failure_kind": None, "failure_category": None,
                      "notification_count": 0, "startup_notifications_skipped": 0,
                      "first_notification_method": None, "first_notification_payload": None,
                      "rejected_notification_method": None, "rejected_notification_payload": None,
                      "notification_mismatch_reason": None}
        result.update({"attempt": attempt, "sdk_pin": SDK_PIN,
                       "workspace_sha256": fixture_hash, "config_sha256": config_hash})
        status = result["status"] if result["status"] in {"blocked", "halted", "ambiguous"} else "ambiguous"
        if status == "ambiguous":
            if result.get("code") not in {"child_timeout", "thread_start_ambiguous"}:
                result["code"] = "child_result_invalid"
        if not result.get("cleanup_ok", True):
            result["status"], result["code"] = "halted", "cleanup_failed"
        ledger_status = "ambiguous" if not result.get("cleanup_ok", True) else result["status"]
        _finish_attempt(authority, attempt, {**result, "status": ledger_status})
        return result
    except Exception:
        # A reservation without a complete, sanitized child result is ambiguous.
        state = _load_state(authority / "attempts.json")
        if state["attempts"][attempt - 1]["status"] == "running":
            state["attempts"][attempt - 1].update({"status": "ambiguous", "code": "child_result_invalid"})
            _write_private(authority / "attempts.json", _json_bytes(state))
        raise


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["--child"]:
        try:
            env = dict(os.environ)
            expected = env.get(CHILD_TOKEN_ENV, "")
            if (env.get(CHILD_MODE_ENV) != CHILD_MODE_VALUE or len(expected) != 64
                    or any(char not in "0123456789abcdef" for char in expected)
                    or getattr(sys.stdin, "isatty", lambda: False)()):
                raise Halt("trial_layout_invalid")
            line = sys.stdin.buffer.readline(66)
            if len(line) != 65 or not line.endswith(b"\n") or sys.stdin.buffer.read(1):
                raise Halt("trial_layout_invalid")
            supplied = line[:-1].decode("ascii")
            _validate_child_context(env, supplied)
            return _child_main(supplied)
        except Halt as exc:
            code = exc.code
        except Exception:
            code = "trial_layout_invalid"
        sys.stdout.write(_json_bytes({"status": "halted", "code": code}).decode())
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="explicitly run the no-inference SDK preflight")
    parser.add_argument("--trial-dir", type=Path, help="new private trial directory; reused only for its two-attempt ledger")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"status": "not_run", "consent_required": "--run", "starts_thread": False, "starts_turn": False}, sort_keys=True))
        return 0
    if args.trial_dir is None:
        print(json.dumps({"status": "halted", "code": "trial_layout_invalid"}, sort_keys=True))
        return 2
    try:
        report = _run_trial(args.trial_dir)
    except Halt as exc:
        report = {"status": "halted", "code": exc.code}
    except Exception:
        report = {"status": "halted", "code": "child_result_invalid"}
    encoded = _json_bytes(report)
    if len(encoded) > MAX_EVIDENCE:
        report = {"status": "halted", "code": "child_result_invalid"}
        encoded = _json_bytes(report)
    sys.stdout.buffer.write(encoded)
    return 0 if report.get("status") in {"blocked", "not_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
