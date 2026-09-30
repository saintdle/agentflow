"""Protected provider-session to Agentflow-workspace bindings.

Bindings let provider hooks resolve the workflow workspace selected by the
leased controller instead of trusting the editor process working directory.
Raw provider session identifiers are never persisted.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping


SCHEMA = "agentflow.workspace-bindings@1"
ENV_SESSION_IDS: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
    "claude": ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
    "copilot": ("COPILOT_SESSION_ID", "GITHUB_COPILOT_SESSION_ID"),
}


def session_scope(provider: str, raw_session_id: str) -> str:
    value = f"agentflow.workspace-binding\0{provider}\0{raw_session_id}"
    return "bind_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_session(provider: str, environ: Mapping[str, str] | None = None) -> str:
    values = current_sessions(provider, environ)
    return values[0] if values else ""


def current_sessions(provider: str, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    values: list[str] = []
    for name in ENV_SESSION_IDS.get(provider, ()):
        value = str(source.get(name) or "").strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA, "bindings": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("unsupported workspace binding registry")
    if not isinstance(value.get("bindings"), dict):
        raise ValueError("workspace binding registry has no bindings object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bindings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def bind(
    path: Path,
    *,
    provider: str,
    raw_session_id: str,
    workspace_root: Path,
    workflow_root: str,
    controller_id: str,
    continuity_id: str,
    controller_state: Path,
    bound_at: str,
) -> str:
    scope = session_scope(provider, raw_session_id)
    record = {
        "provider": provider,
        "session_scope": scope,
        "workspace_root": str(workspace_root.resolve()),
        "workflow_root": workflow_root,
        "controller_id": controller_id,
        "continuity_id": continuity_id,
        "controller_state": str(controller_state.resolve()),
        "bound_at": bound_at,
    }
    with _locked(path):
        value = _read(path)
        value["bindings"][scope] = record
        _write(path, value)
    return scope


def lookup(path: Path, *, provider: str, raw_session_id: str) -> dict[str, Any] | None:
    if not raw_session_id:
        return None
    with _locked(path):
        record = _read(path).get("bindings", {}).get(session_scope(provider, raw_session_id))
    return dict(record) if isinstance(record, Mapping) else None


def remove(path: Path, *, provider: str, raw_session_id: str) -> None:
    if not raw_session_id:
        return
    with _locked(path):
        value = _read(path)
        value.get("bindings", {}).pop(session_scope(provider, raw_session_id), None)
        _write(path, value)


def remove_controller(path: Path, *, workspace_root: Path, workflow_root: str) -> None:
    root = str(workspace_root.resolve())
    with _locked(path):
        value = _read(path)
        bindings = value.get("bindings", {})
        value["bindings"] = {
            key: record for key, record in bindings.items()
            if not (
                isinstance(record, Mapping)
                and str(record.get("workspace_root") or "") == root
                and str(record.get("workflow_root") or "") == workflow_root
            )
        }
        _write(path, value)


__all__ = ["bind", "current_session", "current_sessions", "lookup", "remove", "remove_controller", "session_scope"]
