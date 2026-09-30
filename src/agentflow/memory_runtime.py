"""Opt-in, metadata-only provider memory wiring.

The runtime is deliberately model-free.  Hooks normalize one bounded event,
optionally perform governed find-then-fetch recall, and leave a durable health
record for Stop-time maintenance.  Every failure is fail-open at the hook
boundary: provider sessions must continue even when private local state is
unavailable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from agentflow.events import EventEnvelope, EventSpool, normalize_event, record_event_safely
from agentflow.memory import RecallPlan, find_first
from agentflow.project_config import DEFAULT_MEMORY
from agentflow.search import KnowledgeIndex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def workspace_scope(root: Path) -> str:
    return "ws_" + hashlib.sha256(("agentflow.workspace.scope\0" + str(root.resolve())).encode()).hexdigest()


def state_home() -> Path:
    """Return the one local state root used by CLI hooks and runtime state."""
    configured = os.environ.get("AGENTFLOW_STATE_HOME", "")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if xdg:
        return Path(xdg).expanduser() / "agentflow"
    return Path.home() / ".local/state/agentflow"


def state_root(root: Path) -> Path:
    return state_home() / "memory" / workspace_scope(root)[3:]


def _private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _private(temporary)
    os.replace(temporary, path)
    _private(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path, self.handle = path, None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.handle = self.path.open("a+", encoding="utf-8")
        _private(self.path)
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    value = dict(DEFAULT_MEMORY)
    if isinstance(settings, Mapping):
        value.update(settings)
    return value


def _transient_prompt(payload: Mapping[str, Any], *, depth: int = 0) -> str:
    if depth > 3:
        return ""
    keys = {"prompt", "prompt_text", "user_prompt", "userprompt", "userPrompt", "text", "input", "message", "content"}
    for key, value in payload.items():
        if isinstance(key, str) and key in keys and isinstance(value, str) and value.strip():
            return value.strip()[:400]
    for value in payload.values():
        if isinstance(value, Mapping):
            result = _transient_prompt(value, depth=depth + 1)
            if result:
                return result
    return ""


def _query(payload: Mapping[str, Any], event: str, settings: Mapping[str, Any]) -> str:
    # memory_query is transient input only.  It is never passed to the event
    # normalizer or persisted in the event/receipt stores.
    value = payload.get("memory_query")
    if isinstance(value, str) and value.strip():
        return value.strip()[:400]
    if event == "prompt.submit":
        return _transient_prompt(payload)
    if event == "session.start":
        configured = settings.get("startup_query")
        return configured.strip()[:400] if isinstance(configured, str) and configured.strip() else ""
    return ""


class ReceiptSpool:
    """Process-locked, atomic, bounded injection receipts."""

    def __init__(self, path: Path, *, max_events: int, max_bytes: int, retention_days: float) -> None:
        self.path = Path(path)
        self.max_events = max(1, int(max_events))
        self.max_bytes = max(256, int(max_bytes))
        self.retention_days = max(0.0, float(retention_days))
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
        except OSError:
            return []
        return rows

    def _prune(self, rows: list[dict[str, Any]]) -> int:
        before = len(rows)
        cutoff = _now() - dt.timedelta(days=self.retention_days)
        kept: list[dict[str, Any]] = []
        for row in rows:
            stamp = row.get("timestamp")
            try:
                parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                parsed = _now()
            if parsed >= cutoff:
                kept.append(row)
        rows[:] = kept[-self.max_events:]
        while rows:
            encoded = b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in rows)
            if len(encoded) <= self.max_bytes:
                break
            rows.pop(0)
        return before - len(rows)

    def _write_unlocked(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in rows))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def append(self, value: Mapping[str, Any]) -> None:
        with _Lock(self.lock_path):
            rows = self._read_unlocked()
            rows.append(dict(value))
            self._prune(rows)
            self._write_unlocked(rows)

    def prune(self) -> int:
        with _Lock(self.lock_path):
            rows = self._read_unlocked()
            removed = self._prune(rows)
            self._write_unlocked(rows)
            return removed

    def count(self) -> int:
        with _Lock(self.lock_path):
            return len(self._read_unlocked())


class MemoryRuntime:
    def __init__(self, root: Path, settings: Mapping[str, Any] | None = None) -> None:
        self.root = Path(root).resolve()
        self.settings = _settings(settings)
        self.directory = state_root(self.root)
        if bool(self.settings.get("enabled", False)):
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                self.directory.chmod(0o700)
            except OSError:
                pass
        self.spool = EventSpool(
            self.directory / "events.jsonl",
            max_events=int(self.settings["max_events"]),
            max_bytes=int(self.settings["max_event_bytes"]),
        )
        self.database = self.directory / "knowledge.sqlite3"
        self.health_path = self.directory / "health.json"
        self.receipts_path = self.directory / "injections.jsonl"
        self.receipts = ReceiptSpool(
            self.receipts_path,
            max_events=int(self.settings["max_events"]),
            max_bytes=int(self.settings["max_event_bytes"]),
            retention_days=float(self.settings["retention_days"]),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    def _record_receipt(self, event: EventEnvelope, plan: RecallPlan) -> None:
        if not plan.items:
            return
        self.receipts.append({
                "schema": "agentflow.memory-receipt@1",
                "timestamp": _iso(),
                "session_id": event.session_id,
                "event_id": event.event_id,
                "items": len(plan.items),
                "characters": len(plan.text),
                "source_digests": [item.source_digest for item in plan.items],
                "privacy": "metadata-only",
            })

    def recall(self, event: EventEnvelope, payload: Mapping[str, Any]) -> RecallPlan | None:
        if not self.enabled:
            return None
        if event.event == "prompt.submit" and not bool(self.settings.get("on_prompt")):
            return None
        query = _query(payload, event.event, self.settings)
        if not query:
            return None
        try:
            with KnowledgeIndex(self.database) as index:
                plan = find_first(
                    index, query, scopes=self.settings["scopes"],
                    scope_id=str(self.settings.get("scope_id") or ""),
                    max_items=int(self.settings["max_items"]),
                    max_chars=int(self.settings["max_chars"]),
                    max_age_days=float(self.settings["max_age_days"]),
                    session_id=event.session_id,
                    session_ledger_limit=int(self.settings["session_ledger_limit"]),
                )
            self._record_receipt(event, plan)
            return plan
        except Exception:  # noqa: BLE001 - hook fail-open
            return None

    def maintain(self, *, force: bool = False) -> dict[str, Any]:
        """Run bounded, idempotent local maintenance under one process lock."""
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / "maintenance.lock"
        with _Lock(lock_path):
            health = _read_json(self.health_path)
            now = _now()
            next_allowed = str(health.get("next_allowed_at") or "")
            if not force and next_allowed:
                try:
                    if dt.datetime.fromisoformat(next_allowed.replace("Z", "+00:00")) > now:
                        return health
                except ValueError:
                    pass
            error: Exception | None = None
            for _attempt in range(2):
                try:
                    pruned_events = self.spool.prune()
                    pruned_receipts = self.receipts.prune()
                    pruned_documents = 0
                    pruned_sessions = 0
                    with KnowledgeIndex(self.database) as index:
                        pruned_documents, pruned_sessions = index.maintenance(
                            max_age_days=float(self.settings["retention_days"]),
                            session_retention_days=float(self.settings["session_retention_days"]),
                        )
                    health = {
                        "schema": "agentflow.memory-health@1", "status": "ok",
                        "last_success_at": _iso(now), "last_error": "",
                        "pruned_events": pruned_events, "pruned_documents": pruned_documents,
                        "pruned_sessions": pruned_sessions,
                        "pruned_receipts": pruned_receipts,
                        "receipt_count": self.receipts.count(),
                        "next_allowed_at": _iso(now + dt.timedelta(seconds=int(self.settings["maintenance_interval_seconds"]))),
                        "attempts": int(health.get("attempts", 0)) + 1,
                        "privacy": "metadata-only",
                    }
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001 - health must not break Stop
                    error = exc
            if error is not None:
                health = {
                    "schema": "agentflow.memory-health@1", "status": "degraded",
                    "last_success_at": str(health.get("last_success_at") or ""),
                    "last_error": type(error).__name__, "attempts": int(health.get("attempts", 0)) + 1,
                    "next_allowed_at": _iso(now + dt.timedelta(seconds=min(300, max(1, int(self.settings["maintenance_interval_seconds"]))))),
                    "privacy": "metadata-only",
                }
            _write_json(self.health_path, health)
            return health

    def process(self, provider: str, payload: Mapping[str, Any], event_name: str = "") -> tuple[EventEnvelope | None, RecallPlan | None, dict[str, Any]]:
        if not self.enabled:
            return None, None, {}
        try:
            event = normalize_event(provider, payload, event=event_name or None)
            if event.event == "tool.failure" and not bool(self.settings.get("capture_failures", True)):
                return event, None, {}
            # A transient local lock/I/O failure is retried once, then the
            # provider hook remains fail-open and the event is intentionally
            # dropped rather than copied into an unsafe fallback.
            if not record_event_safely(self.spool, event):
                record_event_safely(self.spool, event)
            if event.event == "context.compact":
                try:
                    with KnowledgeIndex(self.database) as index:
                        index.reset_session(event.session_id)
                except Exception:  # noqa: BLE001 - compaction reset is fail-open
                    pass
            plan = self.recall(event, payload)
            health = self.maintain() if event.event == "session.stop" else {}
            return event, plan, health
        except Exception:  # noqa: BLE001 - provider hooks are fail-open
            return None, None, {}


def health(root: Path, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    runtime = MemoryRuntime(root, settings)
    if not runtime.enabled:
        return {"status": "disabled", "privacy": "metadata-only"}
    result = _read_json(runtime.health_path)
    return result or {"status": "not-run", "privacy": "metadata-only"}


__all__ = ["MemoryRuntime", "ReceiptSpool", "health", "state_home", "state_root", "workspace_scope"]
