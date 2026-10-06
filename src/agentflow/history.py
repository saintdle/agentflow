from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable

from agentflow import beads as beads_backend

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX systems
    msvcrt = None  # type: ignore[assignment]


PROVIDERS = ("copilot", "codex", "claude")
ARCHIVE_ENV = "AGENTFLOW_HISTORY_ARCHIVE"
DEFAULT_ARCHIVE = Path("~/Library/Application Support/Agentflow/history").expanduser()
MANIFEST_NAME = "manifest.json"
ARTIFACT_CONFIG_NAME = "artifacts.json"
TRANSACTION_NAME = ".history-pending-update.json"
LOCK_NAME = ".history.lock"
PLIST_LABEL = "com.agentflow.history-sync"
MAX_SUMMARY_BYTES = 16_384
SUMMARY_FIELDS = {
    "goal": 500,
    "outcome": 1_000,
    "decisions": 8,
    "evidence": 8,
    "blockers": 8,
    "unresolved": 8,
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b\s*[:=]"),
    re.compile(
        r"(?i)\b(?:aws_)?secret_access_key\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{16,}"
    ),
    re.compile(
        r"\b(?:sk|ghp|github_pat|glpat|xox[baprs]|npm|pypi)[-_][A-Za-z0-9_-]{12,}\b",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:A3T|AKIA|ASIA|AGPA|AIDA|ANPA|ANVA|AROA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
UNTRUSTED_SUMMARY_PATTERNS = (
    re.compile(r"(?im)^\s*(?:user|assistant|system|developer|tool)\s*:"),
    re.compile(r"(?i)\bignore (?:all |the )?(?:previous|prior|above) instructions\b"),
    re.compile(r"(?i)\b(?:system|developer) (?:prompt|message|instructions?)\b"),
    re.compile(r"(?i)<\/?(?:system|developer|assistant|tool)(?:\s|>)"),
    re.compile(r"(?i)\b(?:begin|end) (?:transcript|raw log|system prompt)\b"),
    re.compile(r"```"),
    re.compile(
        r"(?m)^\s*(?:traceback \(most recent call last\)|\[[A-Z]+\]\s+\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    ),
)
FORBIDDEN_SUMMARY_KEYS = {
    "messages",
    "prompt",
    "prompts",
    "response",
    "responses",
    "reasoning",
    "thinking",
    "tools",
    "tool_inputs",
    "tool_results",
    "transcript",
    "logs",
}
UUID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class HistoryError(RuntimeError):
    """A safe, user-facing history archive error."""


_ARCHIVE_LOCKS: dict[str, threading.RLock] = {}
_ARCHIVE_LOCKS_GUARD = threading.Lock()
_ARCHIVE_LOCK_STATE = threading.local()


def _thread_lock_for_archive(key: str) -> threading.RLock:
    with _ARCHIVE_LOCKS_GUARD:
        lock = _ARCHIVE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ARCHIVE_LOCKS[key] = lock
        return lock


def _lock_file_descriptor(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HistoryError(f"Cannot open history archive lock: {exc}") from exc
    try:
        value = os.fstat(descriptor)
        try:
            path_value = path.lstat()
        except OSError as exc:
            raise HistoryError("History archive lock changed while opening") from exc
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(path_value.st_mode)
            or (value.st_dev, value.st_ino) != (path_value.st_dev, path_value.st_ino)
            or value.st_uid != os.getuid()
        ):
            raise HistoryError("History archive lock is not a private regular file")
        os.fchmod(descriptor, 0o600)
        if fcntl is None and msvcrt is not None and value.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_archive_file_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - unsupported Python platform
        raise HistoryError("Process locks are unavailable on this platform")
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:  # LK_LOCK has a bounded retry count on Windows; retry indefinitely.
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(0.05)


def _release_archive_file_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows fallback
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _archive_lock(archive: Path) -> Iterable[None]:
    """Serialize archive transactions across threads and independent processes."""
    target = Path(archive).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(target)
    thread_lock = _thread_lock_for_archive(key)
    thread_lock.acquire()
    held = getattr(_ARCHIVE_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _ARCHIVE_LOCK_STATE.held = held
    active = held.get(key)
    try:
        if active is None:
            descriptor = _lock_file_descriptor(target / LOCK_NAME)
            try:
                _acquire_archive_file_lock(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            held[key] = [descriptor, 1]
        else:
            active[1] += 1
        try:
            yield
        finally:
            active = held[key]
            active[1] -= 1
            if active[1] == 0:
                del held[key]
                try:
                    _release_archive_file_lock(active[0])
                finally:
                    os.close(active[0])
    finally:
        thread_lock.release()


@dataclasses.dataclass(frozen=True)
class SourceRoots:
    copilot_cli: Path
    vscode_storage: Path
    codex_sessions: tuple[Path, ...]
    claude_projects: Path

    @classmethod
    def defaults(cls, home: Path | None = None) -> "SourceRoots":
        root = home or Path.home()
        return cls(
            copilot_cli=root / ".copilot/session-state",
            vscode_storage=root / "Library/Application Support/Code/User/workspaceStorage",
            codex_sessions=(root / ".codex/sessions", root / ".codex/archived_sessions"),
            claude_projects=root / ".claude/projects",
        )


@dataclasses.dataclass(frozen=True)
class SessionRecord:
    provider: str
    source_id: str
    source_kind: str
    locators: tuple[str, ...]
    fingerprint_locators: tuple[str, ...]
    fingerprint: str
    size: int
    mtime_ns: int
    event_count: int
    disposition: str
    workspace_refs: tuple[str, ...] = ()
    started_at: str = ""
    ended_at: str = ""
    parent_ref: str = ""
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    role: str = ""
    thread_source: str = ""
    delegation_depth: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    context_window_tokens: int = 0
    peak_context_tokens: int = 0
    diagnostics: tuple[str, ...] = ()
    usage_metadata: dict[str, Any] | None = None

    @property
    def bead_id(self) -> str:
        digest = hashlib.sha256(f"{self.provider}:{self.source_id}".encode()).hexdigest()
        return f"history-s-{digest[:12]}"

    def manifest_value(self) -> dict[str, Any]:
        value = {
            "bead_id": self.bead_id,
            "provider": self.provider,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "locators": list(self.locators),
            "fingerprint_locators": list(self.fingerprint_locators),
            "fingerprint": self.fingerprint,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "event_count": self.event_count,
            "disposition": self.disposition,
            "workspace_refs": list(self.workspace_refs),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "parent_ref": self.parent_ref,
            "models": list(self.models),
            "efforts": list(self.efforts),
            "role": self.role,
            "thread_source": self.thread_source,
            "delegation_depth": self.delegation_depth,
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "context_window_tokens": self.context_window_tokens,
            "peak_context_tokens": self.peak_context_tokens,
            "diagnostics": list(self.diagnostics),
        }
        if self.provider == "codex":
            value["peak_context_semantics"] = "input_plus_output_proxy"
        if self.usage_metadata is not None:
            value["usage_metadata"] = self.usage_metadata
        return value


def archive_path(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser()
    configured = os.environ.get(ARCHIVE_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_ARCHIVE


def stable_session_id(provider: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{provider}:{source_id}".encode()).hexdigest()
    return f"history-s-{digest[:12]}"


def stable_artifact_id(name: str) -> str:
    """Return the durable, registration-name based artifact identity."""
    return "history-a-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _safe_artifact_text(value: str, field: str, *, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HistoryError(f"Artifact {field} must be a non-empty string of at most {limit} characters")
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HistoryError(f"Artifact {field} contains control characters")
    if any(pattern.search(value) for pattern in SECRET_PATTERNS + UNTRUSTED_SUMMARY_PATTERNS):
        raise HistoryError(f"Artifact {field} contains unsafe text")
    return value


def _safe_artifact_name(value: str) -> str:
    value = _safe_artifact_text(value, "name", limit=96)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", value):
        raise HistoryError("Artifact name must use lowercase letters, digits, dots, underscores, or hyphens")
    return value


def _safe_pattern(value: str, field: str) -> str:
    value = _safe_artifact_text(value, field, limit=240)
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise HistoryError(f"Artifact {field} must be relative and may not escape its source")
    return value


ARTIFACT_FIELDS = frozenset(
    {"name", "bead_id", "path", "title", "kind", "description", "include", "watch", "source_type"}
)


def _validate_artifact_registration(key: Any, item: Any) -> dict[str, Any]:
    """Validate one persisted registration before it can reach a reader or exporter."""
    if not isinstance(key, str) or _safe_artifact_name(key) != key:
        raise HistoryError("Artifact configuration contains an invalid registration name")
    if not isinstance(item, dict) or set(item) != ARTIFACT_FIELDS:
        raise HistoryError(f"Artifact configuration for {key} has an invalid schema")
    name = item["name"]
    if not isinstance(name, str) or _safe_artifact_name(name) != name or name != key:
        raise HistoryError(f"Artifact configuration for {key} has an invalid name")
    bead_id = item["bead_id"]
    if not isinstance(bead_id, str) or bead_id != stable_artifact_id(name):
        raise HistoryError(f"Artifact configuration for {key} has an invalid identity")

    path_value = item["path"]
    if not isinstance(path_value, str) or not path_value or len(path_value) > 4_096:
        raise HistoryError(f"Artifact configuration for {key} has an invalid path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path_value):
        raise HistoryError(f"Artifact configuration for {key} has an invalid path")
    source = Path(path_value)
    if not source.is_absolute():
        raise HistoryError(f"Artifact configuration for {key} has a non-absolute path")
    if any(part == ".." for part in source.parts):
        raise HistoryError(f"Artifact configuration for {key} has a traversal path")
    try:
        persisted_stat = _artifact_lstat(source)
    except FileNotFoundError:
        persisted_stat = None
    except OSError as exc:
        raise HistoryError(f"Artifact configuration for {key} has an unreadable path") from exc
    if persisted_stat is not None and stat.S_ISLNK(persisted_stat.st_mode):
        raise HistoryError(f"Artifact configuration for {key} has a symlink path")
    text_fields = ("title", "kind", "description")
    cleaned: dict[str, Any] = {"name": name, "bead_id": bead_id, "path": str(source)}
    for field in text_fields:
        value = item[field]
        limit = 80 if field == "kind" else 1_000 if field == "description" else 240
        if not isinstance(value, str) or _safe_artifact_text(value, field, limit=limit) != value:
            raise HistoryError(f"Artifact configuration for {key} has unsafe {field}")
        if field == "kind" and not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", value):
            raise HistoryError(f"Artifact configuration for {key} has an invalid kind")
        cleaned[field] = value

    source_type = item["source_type"]
    if not isinstance(source_type, str) or source_type not in {"file", "directory"}:
        raise HistoryError(f"Artifact configuration for {key} has an invalid source type")
    cleaned["source_type"] = source_type
    for field in ("include", "watch"):
        values = item[field]
        if not isinstance(values, list):
            raise HistoryError(f"Artifact configuration for {key} has invalid {field} patterns")
        checked: list[str] = []
        for value in values:
            if not isinstance(value, str) or _safe_pattern(value, field) != value:
                raise HistoryError(f"Artifact configuration for {key} has invalid {field} patterns")
            checked.append(value)
        cleaned[field] = checked

    try:
        source_stat = _artifact_lstat(source)
    except FileNotFoundError:
        source_stat = None
    except OSError as exc:
        raise HistoryError(f"Artifact configuration for {key} has an unreadable path") from exc
    if source_stat is not None:
        if stat.S_ISLNK(source_stat.st_mode):
            raise HistoryError(f"Artifact configuration for {key} has a symlink path")
        if stat.S_ISREG(source_stat.st_mode):
            actual_type = "file"
        elif stat.S_ISDIR(source_stat.st_mode):
            actual_type = "directory"
        else:
            raise HistoryError(f"Artifact configuration for {key} has a non-regular source")
        if actual_type != source_type:
            raise HistoryError(f"Artifact configuration for {key} has a mismatched source type")

    if source_type == "directory" and not cleaned["include"]:
        raise HistoryError(f"Artifact configuration for {key} has no include patterns")
    if source_type == "file" and (cleaned["include"] or cleaned["watch"]):
        raise HistoryError(f"Artifact configuration for {key} has patterns for a file source")
    return cleaned


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.lower()
    return candidate if UUID_RE.fullmatch(candidate) else ""


def _safe_filename_identity(value: str) -> str:
    canonical = _canonical_uuid(value)
    if canonical:
        return canonical
    digest = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()
    return f"invalid-{digest[:16]}"


def _normalize_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        return ""
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _normalized_parent(value: Any) -> str:
    return _canonical_uuid(value)


def _workspace_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _locator(path: Path, home: Path | None = None) -> str:
    path = path.resolve()
    root = (home or Path.home()).resolve()
    try:
        return "~/" + path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _lexical_locator(path: Path, home: Path | None = None) -> str:
    """Create a locator without resolving a path that may be racing."""
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(home or Path.home()))
    try:
        return "~/" + path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _path_from_locator(locator: str, home: Path | None = None) -> Path:
    if locator == "~":
        return home or Path.home()
    if locator.startswith("~/"):
        return (home or Path.home()) / locator[2:]
    return Path(locator)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ends_with_newline(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return True
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:
        return False


def _fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        data_hash = _hash_file(path)
        digest.update(path.name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(data_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_snapshot(paths: Iterable[Path]) -> dict[str, tuple[int, int, int, int]]:
    snapshot: dict[str, tuple[int, int, int, int]] = {}
    for path in paths:
        value = path.stat()
        snapshot[str(path)] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    return snapshot


def _finish_source_read(
    paths: Iterable[Path],
    before: dict[str, tuple[int, int, int, int]],
    *,
    fingerprint_paths: Iterable[Path] | None = None,
) -> tuple[str, list[os.stat_result], bool]:
    source_paths = list(paths)
    fingerprint = _fingerprint(fingerprint_paths or source_paths)
    stats = [path.stat() for path in source_paths]
    after = {
        str(path): (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        for path, value in zip(source_paths, stats)
    }
    return fingerprint, stats, before != after


def _is_volatile(path: Path, now_ns: int | None = None) -> bool:
    current = now_ns if now_ns is not None else int(dt.datetime.now().timestamp() * 1_000_000_000)
    try:
        return current - path.stat().st_mtime_ns < 5 * 60 * 1_000_000_000
    except OSError:
        return False


def _workspace_yaml_ref(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        for line in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            if line.startswith("cwd:"):
                value = line.partition(":")[2].strip().strip("'\"")
                return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    except OSError:
        pass
    return ""


def _copilot_cli_record(directory: Path, *, home: Path | None = None) -> SessionRecord:
    events = directory / "events.jsonl"
    workspace = directory / "workspace.yaml"
    source_paths = [events] + ([workspace] if workspace.is_file() else [])
    before = _source_snapshot(source_paths)
    count = malformed = 0
    timestamps: list[str] = []
    filename_id = _canonical_uuid(directory.name)
    source_id = _safe_filename_identity(directory.name)
    event_session_id = ""
    terminal = False
    models: list[str] = []
    efforts: list[str] = []
    diagnostics: list[str] = []
    if not filename_id:
        diagnostics.append("invalid_directory_session_id")
    try:
        with events.open("r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    malformed += 1
                    continue
                if not isinstance(value, dict):
                    malformed += 1
                    continue
                count += 1
                event_type = value.get("type")
                timestamp = _normalize_timestamp(value.get("timestamp"))
                if timestamp:
                    timestamps.append(timestamp)
                elif value.get("timestamp") is not None:
                    diagnostics.append("invalid_timestamp")
                if event_type == "session.start":
                    data = value.get("data")
                    if isinstance(data, dict):
                        if isinstance(data.get("sessionId"), str):
                            event_session_id = _canonical_uuid(data["sessionId"])
                        raw_model = data.get("selectedModel")
                        if isinstance(raw_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_model):
                            if raw_model not in models:
                                models.append(raw_model)
                        raw_effort = data.get("reasoningEffort")
                        if isinstance(raw_effort, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", raw_effort):
                            if raw_effort not in efforts:
                                efforts.append(raw_effort)
                if event_type == "session.model_change":
                    data = value.get("data")
                    if isinstance(data, dict):
                        raw_model = data.get("newModel")
                        if isinstance(raw_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_model):
                            if raw_model not in models:
                                models.append(raw_model)
                        raw_effort = data.get("reasoningEffort")
                        if isinstance(raw_effort, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", raw_effort):
                            if raw_effort not in efforts:
                                efforts.append(raw_effort)
                if event_type in {"session.end", "session.shutdown", "session.stop"}:
                    terminal = True
    except OSError:
        malformed += 1
    identity_invalid = not filename_id or event_session_id != filename_id
    if event_session_id != filename_id:
        diagnostics.append("directory_session_id_mismatch")
    if not _ends_with_newline(events):
        malformed += 1
        diagnostics.append("missing_final_newline")
    if malformed:
        diagnostics.append(f"malformed_rows:{malformed}")
    if terminal:
        diagnostics.append("terminal_signal")
    fingerprint, file_stats, mutated = _finish_source_read(source_paths, before)
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    disposition = (
        "volatile"
        if mutated
        else "skipped_stub"
        if count == 0 and not malformed
        else "quarantined"
        if malformed or identity_invalid
        else "volatile"
        if _is_volatile(events) and not terminal
        else "pending_summary"
    )
    source_locators = tuple(_locator(path, home) for path in source_paths)
    return SessionRecord(
        provider="copilot",
        source_id=source_id,
        source_kind="copilot-cli",
        locators=source_locators,
        fingerprint_locators=source_locators,
        fingerprint=fingerprint,
        size=sum(item.st_size for item in file_stats),
        mtime_ns=max(item.st_mtime_ns for item in file_stats),
        event_count=count,
        disposition=disposition,
        workspace_refs=tuple(filter(None, (_workspace_yaml_ref(workspace),))),
        started_at=min(timestamps) if timestamps else "",
        ended_at=max(timestamps) if timestamps else "",
        models=tuple(models),
        efforts=tuple(efforts),
        diagnostics=tuple(diagnostics),
    )


def _vscode_replay_record(
    source_id: str, path: Path, workspace: str, *, home: Path | None = None
) -> SessionRecord:
    before = _source_snapshot((path,))
    count = malformed = 0
    filename_id = _canonical_uuid(source_id)
    safe_source_id = _safe_filename_identity(source_id)
    snapshot_id = ""
    diagnostics: list[str] = []
    saw_snapshot = False
    if not filename_id:
        diagnostics.append("invalid_filename_session_id")
    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    malformed += 1
                    continue
                if not isinstance(value, dict):
                    malformed += 1
                    continue
                count += 1
                kind = value.get("kind")
                if kind == 0:
                    snapshot = value.get("v")
                    if saw_snapshot or not isinstance(snapshot, dict):
                        malformed += 1
                        continue
                    saw_snapshot = True
                    if isinstance(snapshot.get("sessionId"), str):
                        snapshot_id = _canonical_uuid(snapshot["sessionId"])
                elif kind == 1:
                    if not saw_snapshot or not isinstance(value.get("k"), list) or "v" not in value:
                        malformed += 1
                elif kind == 2:
                    if (
                        not saw_snapshot
                        or not isinstance(value.get("k"), list)
                        or ("i" in value and not isinstance(value.get("i"), int))
                        or ("v" in value and not isinstance(value.get("v"), list))
                    ):
                        malformed += 1
                else:
                    malformed += 1
    except OSError:
        malformed += 1
    if not saw_snapshot:
        diagnostics.append("missing_replay_snapshot")
    if not _ends_with_newline(path):
        malformed += 1
        diagnostics.append("missing_final_newline")
    identity_invalid = not filename_id or snapshot_id != filename_id
    if snapshot_id != filename_id:
        diagnostics.append("filename_session_id_mismatch")
    if malformed:
        diagnostics.append(f"invalid_replay_rows:{malformed}")
    fingerprint, stats, mutated = _finish_source_read((path,), before)
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    return SessionRecord(
        provider="copilot",
        source_id=safe_source_id,
        source_kind="copilot-vscode",
        locators=(_locator(path, home),),
        fingerprint_locators=(_locator(path, home),),
        fingerprint=fingerprint,
        size=stats[0].st_size,
        mtime_ns=stats[0].st_mtime_ns,
        event_count=count,
        disposition=(
            "volatile"
            if mutated
            else "quarantined"
            if malformed or not saw_snapshot or identity_invalid
            else "pending_summary"
        ),
        workspace_refs=(_workspace_ref(workspace),),
        diagnostics=tuple(diagnostics),
    )


def scan_copilot(roots: SourceRoots, *, home: Path | None = None) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    if roots.copilot_cli.is_dir():
        for directory in sorted(path for path in roots.copilot_cli.iterdir() if path.is_dir()):
            events = directory / "events.jsonl"
            if events.is_file():
                records.append(_copilot_cli_record(directory, home=home))
            elif (directory / "workspace.yaml").is_file():
                workspace = directory / "workspace.yaml"
                value = _record_for_stub("copilot", directory.name, "copilot-cli", workspace, home)
                records.append(value)

    variants: dict[str, list[SessionRecord]] = {}
    if roots.vscode_storage.is_dir():
        for path in sorted(roots.vscode_storage.glob("*/chatSessions/*")):
            if path.suffix not in {".json", ".jsonl"} or not path.is_file():
                continue
            source_id = path.stem
            workspace = path.parents[1].name
            if path.suffix == ".jsonl":
                record = _vscode_replay_record(source_id, path, workspace, home=home)
            else:
                record = _record_for_legacy_json(
                    "copilot", source_id, "copilot-vscode-legacy", path, workspace, home
                )
            variants.setdefault(record.source_id, []).append(record)
    for source_id, candidates in variants.items():
        by_hash: dict[str, list[SessionRecord]] = {}
        for candidate in candidates:
            by_hash.setdefault(candidate.fingerprint, []).append(candidate)
        preferred = sorted(
            candidates,
            key=lambda item: (item.source_kind.endswith("legacy"), item.locators),
        )[0]
        locators = tuple(sorted({locator for item in candidates for locator in item.locators}))
        workspace_refs = tuple(
            sorted({ref for item in candidates for ref in item.workspace_refs if ref})
        )
        diagnostics = list(preferred.diagnostics)
        disposition = preferred.disposition
        if len(candidates) > 1 and len(by_hash) == 1:
            diagnostics.append(f"exact_mirrors:{len(candidates)}")
            fingerprint_locators = (locators[0],)
            fingerprint = preferred.fingerprint
        else:
            fingerprint_locators = locators
            fingerprint = _fingerprint(
                tuple(_path_from_locator(locator, home) for locator in locators)
            )
        if len(by_hash) > 1:
            diagnostics.append(f"conflicting_variants:{len(by_hash)}")
            disposition = "quarantined"
        records.append(
            dataclasses.replace(
                preferred,
                locators=locators,
                fingerprint_locators=fingerprint_locators,
                fingerprint=fingerprint,
                workspace_refs=workspace_refs,
                disposition=disposition,
                diagnostics=tuple(diagnostics),
            )
        )
    return records


def _record_for_stub(
    provider: str, source_id: str, source_kind: str, path: Path, home: Path | None
) -> SessionRecord:
    before = _source_snapshot((path,))
    fingerprint, stats, mutated = _finish_source_read((path,), before)
    identity_valid = bool(_canonical_uuid(source_id))
    diagnostics = ["missing_canonical_events"]
    if not identity_valid:
        diagnostics.append("invalid_filename_session_id")
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    value = stats[0]
    return SessionRecord(
        provider=provider,
        source_id=_safe_filename_identity(source_id),
        source_kind=source_kind,
        locators=(_locator(path, home),),
        fingerprint_locators=(_locator(path, home),),
        fingerprint=fingerprint,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        event_count=0,
        disposition="volatile" if mutated else "skipped_stub" if identity_valid else "quarantined",
        diagnostics=tuple(diagnostics),
    )


def _record_for_legacy_json(
    provider: str,
    source_id: str,
    source_kind: str,
    path: Path,
    workspace: str,
    home: Path | None,
) -> SessionRecord:
    before = _source_snapshot((path,))
    malformed = False
    event_count = 0
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="surrogateescape"))
        if isinstance(value, dict):
            requests = value.get("requests")
            event_count = len(requests) if isinstance(requests, list) else 1
        elif isinstance(value, list):
            event_count = len(value)
        else:
            malformed = True
    except (OSError, UnicodeError, json.JSONDecodeError):
        malformed = True
    filename_id = _canonical_uuid(source_id)
    fingerprint, stats, mutated = _finish_source_read((path,), before)
    diagnostics = []
    if malformed:
        diagnostics.append("malformed_json")
    if not filename_id:
        diagnostics.append("invalid_filename_session_id")
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    file_stat = stats[0]
    return SessionRecord(
        provider=provider,
        source_id=_safe_filename_identity(source_id),
        source_kind=source_kind,
        locators=(_locator(path, home),),
        fingerprint_locators=(_locator(path, home),),
        fingerprint=fingerprint,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        event_count=event_count,
        disposition=(
            "volatile"
            if mutated
            else "quarantined"
            if malformed or not filename_id
            else "pending_summary"
        ),
        workspace_refs=(_workspace_ref(workspace),),
        diagnostics=tuple(diagnostics),
    )


_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _usage_counter_values(raw: Any) -> tuple[dict[str, int | None], set[str]]:
    """Read only bounded token counters from one provider usage object."""
    values: dict[str, int | None] = {name: None for name in _USAGE_FIELDS}
    invalid: set[str] = set()
    if not isinstance(raw, dict):
        return values, {"usage_object_invalid"}

    def read(name: str, *aliases: str) -> int | None:
        for key in (name, *aliases):
            if key in raw:
                value = raw[key]
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
                invalid.add(f"{key}_invalid")
                return None
        return None

    values["input_tokens"] = read("input_tokens")
    values["output_tokens"] = read("output_tokens")
    values["reasoning_output_tokens"] = read("reasoning_output_tokens")
    if values["reasoning_output_tokens"] is None:
        details = raw.get("output_tokens_details")
        if isinstance(details, dict) and "reasoning_tokens" in details:
            candidate = details["reasoning_tokens"]
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                values["reasoning_output_tokens"] = candidate
            else:
                invalid.add("reasoning_tokens_invalid")

    cached = read("cached_input_tokens")
    if cached is None:
        details = raw.get("input_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            candidate = details["cached_tokens"]
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                cached = candidate
            else:
                invalid.add("cached_tokens_invalid")
        elif "cache_read_input_tokens" in raw and "cache_creation_input_tokens" in raw:
            cache_values: list[int] = []
            cache_invalid = False
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                candidate = raw[key]
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                    cache_values.append(candidate)
                else:
                    invalid.add(f"{key}_invalid")
                    cache_invalid = True
            if not cache_invalid:
                cached = sum(cache_values)
        elif "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
            cached = None
    values["cached_input_tokens"] = cached

    supplied_total = read("total_tokens")
    if supplied_total is not None:
        values["total_tokens"] = supplied_total
    elif values["input_tokens"] is not None and values["output_tokens"] is not None:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]

    if (
        values["input_tokens"] is not None
        and values["output_tokens"] is not None
        and values["total_tokens"] is not None
        and values["total_tokens"] != values["input_tokens"] + values["output_tokens"]
    ):
        invalid.add("total_does_not_match_input_and_output")
    if (
        values["cached_input_tokens"] is not None
        and values["input_tokens"] is not None
        and values["cached_input_tokens"] > values["input_tokens"]
    ):
        invalid.add("cached_input_exceeds_input")
    if (
        values["reasoning_output_tokens"] is not None
        and values["output_tokens"] is not None
        and values["reasoning_output_tokens"] > values["output_tokens"]
    ):
        invalid.add("reasoning_exceeds_output")
    return values, invalid


def _codex_usage_metadata(
    response_records: list[tuple[str, dict[str, int | None]]],
    event_snapshots: list[tuple[dict[str, int | None], dict[str, int | None]]],
    *,
    client_version: str | None,
    diagnostics: Iterable[str] = (),
) -> dict[str, Any]:
    """Reconcile response-level usage with token_count snapshots without adding views."""
    issues = set(diagnostics)
    by_response: dict[str, dict[str, int | None]] = {}
    ordered_records: list[dict[str, int | None]] = []
    duplicate_seen = False
    for response_id, counters in response_records:
        previous = by_response.get(response_id)
        if previous is not None:
            if previous == counters:
                duplicate_seen = True
                continue
            issues.add("conflicting_duplicate_response_id")
            continue
        by_response[response_id] = counters
        ordered_records.append(counters)
    if duplicate_seen:
        issues.add("duplicate_response_id_ignored")

    record_sum: dict[str, int | None] = {}
    if ordered_records:
        for name in _USAGE_FIELDS:
            entries = [item[name] for item in ordered_records]
            record_sum[name] = sum(entries) if all(item is not None for item in entries) else None

    ambiguous = any(
        item in issues
        for item in (
            "conflicting_duplicate_response_id",
            "malformed_response_usage",
            "malformed_token_count_usage",
            "cumulative_counter_reset",
            "conflicting_cumulative_snapshot",
            "usage_view_conflict",
            "request_view_conflict",
        )
    )
    cross_check = "not_available"
    cross_check_source: str | None = "codex-event-msg-token-count" if event_snapshots else None
    if ordered_records and event_snapshots:
        checked = True
        event_totals = event_snapshots[-1][0]
        for name in _USAGE_FIELDS:
            left, right = record_sum.get(name), event_totals.get(name)
            if left is not None and right is not None:
                if left != right:
                    issues.add("usage_view_conflict")
                    ambiguous = True
            elif name in {"input_tokens", "output_tokens", "total_tokens"}:
                checked = False
        if len(event_snapshots) != len(ordered_records):
            checked = False
            issues.add("cumulative_prefix_count_unverified")
        else:
            prefix: dict[str, int] = {name: 0 for name in _USAGE_FIELDS}
            prefix_complete = {name: True for name in _USAGE_FIELDS}
            for native, (event_cumulative, _) in zip(ordered_records, event_snapshots):
                for name in _USAGE_FIELDS:
                    if native[name] is None:
                        prefix_complete[name] = False
                    elif prefix_complete[name]:
                        prefix[name] += native[name]
                    expected = prefix[name] if prefix_complete[name] else None
                    observed = event_cumulative[name]
                    if expected is not None and observed is not None:
                        if expected != observed:
                            issues.add("usage_view_conflict")
                            ambiguous = True
                    elif name in {"input_tokens", "output_tokens", "total_tokens"}:
                        checked = False
        event_requests = [last for _, last in event_snapshots if any(value is not None for value in last.values())]
        if len(event_requests) != len(ordered_records):
            checked = False
            issues.add("request_view_count_unverified")
        else:
            for native, event in zip(ordered_records, event_requests):
                for name in _USAGE_FIELDS:
                    if native[name] is not None and event[name] is not None and native[name] != event[name]:
                        issues.add("request_view_conflict")
                        ambiguous = True
        cross_check = "matched" if checked and not ambiguous else "partial"
        if ambiguous:
            cross_check = "conflict"

    if ordered_records:
        source = "codex-native-token-usage-record"
        counters = record_sum
        request_count: int | None = len(ordered_records)
        max_request_input = (
            max(item["input_tokens"] for item in ordered_records if item["input_tokens"] is not None)
            if all(item["input_tokens"] is not None for item in ordered_records)
            else None
        )
        if any(item["input_tokens"] is None or item["output_tokens"] is None for item in ordered_records):
            issues.add("incomplete_response_usage")
            ambiguous = True
    elif event_snapshots:
        source = "codex-event-msg-token-count"
        counters = event_snapshots[-1][0]
        request_count = None
        max_request_input = None
    else:
        source = "unavailable"
        counters = {name: None for name in _USAGE_FIELDS}
        request_count = None
        max_request_input = None

    if ambiguous:
        availability = "ambiguous"
        counters = {name: None for name in _USAGE_FIELDS}
        request_count = None
        max_request_input = None
    elif ordered_records:
        availability = "complete"
    elif event_snapshots and any(value is not None for value in counters.values()):
        availability = "partial"
    else:
        availability = "unavailable"

    return {
        "schema_version": 1,
        "source": source,
        "client_version": client_version,
        "availability": availability,
        "cross_check": cross_check,
        "cross_check_source": cross_check_source,
        "input_tokens": counters.get("input_tokens"),
        "cached_input_tokens": counters.get("cached_input_tokens"),
        "output_tokens": counters.get("output_tokens"),
        "reasoning_output_tokens": counters.get("reasoning_output_tokens"),
        "total_tokens": counters.get("total_tokens"),
        "request_count": request_count,
        "max_request_input_tokens": max_request_input,
        "diagnostics": sorted(issues),
    }


def _codex_record(source_id: str, paths: list[Path], *, home: Path | None = None) -> SessionRecord:
    preferred = max(paths, key=lambda item: (item.stat().st_size, item.stat().st_mtime_ns))
    before = _source_snapshot(paths)
    count = malformed = 0
    filename_id = _canonical_uuid(source_id)
    safe_source_id = _safe_filename_identity(source_id)
    meta_id = ""
    workspace_ref = ""
    parent_ref = ""
    models: list[str] = []
    efforts: list[str] = []
    role = ""
    thread_source = ""
    delegation_depth = 0
    token_usage = {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }
    context_window_tokens = 0
    peak_context_tokens = 0
    timestamps: list[str] = []
    terminal = False
    saw_meta = False
    diagnostics: list[str] = []
    usage_response_records: list[tuple[str, dict[str, int | None]]] = []
    usage_event_snapshots: list[tuple[dict[str, int | None], dict[str, int | None]]] = []
    usage_diagnostics: set[str] = set()
    client_version: str | None = None
    seen_event_snapshots: set[tuple[tuple[int | None, ...], tuple[int | None, ...]]] = set()
    cumulative_last_by_value: dict[tuple[int | None, ...], tuple[int | None, ...]] = {}
    previous_cumulative: dict[str, int | None] | None = None
    if not filename_id:
        diagnostics.append("invalid_filename_session_id")
    try:
        with preferred.open("r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    malformed += 1
                    continue
                if not isinstance(value, dict):
                    malformed += 1
                    continue
                count += 1
                payload = value.get("payload")
                if value.get("type") == "session_meta" and isinstance(payload, dict):
                    if saw_meta:
                        diagnostics.append("duplicate_session_meta")
                    else:
                        saw_meta = True
                        if isinstance(payload.get("id"), str):
                            meta_id = _canonical_uuid(payload["id"])
                        if isinstance(payload.get("cwd"), str):
                            workspace_ref = _workspace_ref(payload["cwd"])
                        raw_client_version = payload.get("client_version", payload.get("cli_version"))
                        if isinstance(raw_client_version, str) and len(raw_client_version) <= 64 and re.fullmatch(
                            r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", raw_client_version
                        ):
                            client_version = raw_client_version
                        if payload.get("parent_thread_id") is not None:
                            parent_ref = _normalized_parent(payload.get("parent_thread_id"))
                            if not parent_ref:
                                diagnostics.append("invalid_parent_ref_dropped")
                        raw_thread_source = payload.get("thread_source")
                        if isinstance(raw_thread_source, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", raw_thread_source):
                            thread_source = raw_thread_source
                        source = payload.get("source")
                        subagent = source.get("subagent") if isinstance(source, dict) else None
                        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                        if isinstance(spawn, dict):
                            nested_parent = _normalized_parent(spawn.get("parent_thread_id"))
                            if nested_parent:
                                parent_ref = nested_parent
                            raw_role = spawn.get("agent_role")
                            if isinstance(raw_role, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", raw_role):
                                role = raw_role
                            raw_depth = spawn.get("depth")
                            if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) and raw_depth >= 0:
                                delegation_depth = raw_depth
                        timestamp = _normalize_timestamp(payload.get("timestamp"))
                        if timestamp:
                            timestamps.append(timestamp)
                        elif payload.get("timestamp") is not None:
                            diagnostics.append("invalid_timestamp")
                timestamp = _normalize_timestamp(value.get("timestamp"))
                if timestamp:
                    timestamps.append(timestamp)
                elif value.get("timestamp") is not None:
                    diagnostics.append("invalid_timestamp")
                if value.get("type") == "turn_context" and isinstance(payload, dict):
                    raw_model = payload.get("model")
                    if isinstance(raw_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_model):
                        if raw_model not in models:
                            models.append(raw_model)
                    raw_effort = payload.get("effort")
                    if isinstance(raw_effort, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", raw_effort):
                        if raw_effort not in efforts:
                            efforts.append(raw_effort)
                native_usage = isinstance(payload, dict) and payload.get("type") == "token_usage_record"
                native_usage = native_usage or value.get("type") == "token_usage_record"
                if native_usage:
                    usage_record = payload if isinstance(payload, dict) else value
                    response_id = usage_record.get("response_id")
                    if not isinstance(response_id, str) or not response_id.strip() or len(response_id) > 256:
                        usage_diagnostics.add("malformed_response_usage")
                    else:
                        raw_usage = usage_record.get("token_usage", usage_record.get("usage"))
                        if raw_usage is None and isinstance(usage_record.get("info"), dict):
                            raw_usage = usage_record["info"].get("token_usage")
                        if raw_usage is None:
                            raw_usage = usage_record
                        counters, invalid_usage = _usage_counter_values(raw_usage)
                        if invalid_usage or counters["input_tokens"] is None or counters["output_tokens"] is None:
                            usage_diagnostics.add("malformed_response_usage")
                        else:
                            usage_response_records.append((response_id.strip(), counters))
                if value.get("type") == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") in {"task_complete", "turn_aborted", "session_configured"}:
                        terminal = payload.get("type") in {"task_complete", "turn_aborted"} or terminal
                    if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                        info = payload["info"]
                        total = info.get("total_token_usage")
                        last = info.get("last_token_usage")
                        if ("total_token_usage" in info and not isinstance(total, dict)) or (
                            "last_token_usage" in info and not isinstance(last, dict)
                        ):
                            usage_diagnostics.add("malformed_token_count_usage")
                        raw_window = info.get("model_context_window")
                        if isinstance(raw_window, int) and not isinstance(raw_window, bool) and raw_window > 0:
                            context_window_tokens = max(context_window_tokens, raw_window)
                        if isinstance(last, dict):
                            last_values, last_invalid = _usage_counter_values(last)
                            if last_invalid:
                                usage_diagnostics.add("malformed_token_count_usage")
                            raw_peak = last_values.get("total_tokens")
                            if raw_peak is not None:
                                peak_context_tokens = max(peak_context_tokens, raw_peak)
                        else:
                            last_values = {name: None for name in _USAGE_FIELDS}
                        if isinstance(total, dict):
                            total_values, total_invalid = _usage_counter_values(total)
                            if total_invalid:
                                usage_diagnostics.add("malformed_token_count_usage")
                            if any(value is not None for value in total_values.values()):
                                cumulative_key = tuple(total_values[name] for name in _USAGE_FIELDS)
                                last_key = tuple(last_values[name] for name in _USAGE_FIELDS)
                                previous_last = cumulative_last_by_value.get(cumulative_key)
                                if previous_last is not None:
                                    if previous_last != last_key:
                                        usage_diagnostics.add("conflicting_cumulative_snapshot")
                                    else:
                                        usage_diagnostics.add("duplicate_token_count_snapshot_ignored")
                                else:
                                    cumulative_last_by_value[cumulative_key] = last_key
                                    snapshot_key = (cumulative_key, last_key)
                                    if snapshot_key not in seen_event_snapshots:
                                        if previous_cumulative is not None:
                                            for name in _USAGE_FIELDS:
                                                before_value = previous_cumulative.get(name)
                                                after_value = total_values.get(name)
                                                if before_value is not None and after_value is not None and after_value < before_value:
                                                    usage_diagnostics.add("cumulative_counter_reset")
                                        previous_cumulative = total_values
                                        usage_event_snapshots.append((total_values, last_values))
                                        seen_event_snapshots.add(snapshot_key)
                                    else:
                                        usage_diagnostics.add("duplicate_token_count_snapshot_ignored")
                            raw_total = total_values.get("total_tokens")
                            if raw_total is not None and raw_total >= token_usage["total_tokens"]:
                                def token_value(name: str) -> int:
                                    candidate = total_values.get(name)
                                    return candidate if candidate is not None else 0

                                token_usage = {
                                    "total_tokens": raw_total,
                                    "input_tokens": token_value("input_tokens"),
                                    "output_tokens": token_value("output_tokens"),
                                    "reasoning_tokens": token_value("reasoning_output_tokens"),
                                }
                    elif payload.get("type") == "token_count":
                        usage_diagnostics.add("malformed_token_count_usage")
    except OSError:
        malformed += 1
    if not saw_meta:
        diagnostics.append("missing_primary_session_meta")
    if not _ends_with_newline(preferred):
        malformed += 1
        diagnostics.append("missing_final_newline")
    identity_invalid = not filename_id or meta_id != filename_id
    if meta_id != filename_id:
        diagnostics.append("filename_session_id_mismatch")
    if terminal:
        diagnostics.append("terminal_signal")
    if malformed:
        diagnostics.append(f"malformed_rows:{malformed}")
    hashes = {_hash_file(path) for path in paths}
    if len(paths) > 1:
        diagnostics.append(
            f"{'exact_mirrors' if len(hashes) == 1 else 'active_archive_collision'}:{len(paths)}"
        )
    fingerprint, stats, mutated = _finish_source_read(
        paths, before, fingerprint_paths=(preferred,)
    )
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    disposition = (
        "volatile"
        if mutated
        else "quarantined"
        if malformed or not saw_meta or identity_invalid
        else "volatile"
        if _is_volatile(preferred) and not terminal
        else "pending_summary"
    )
    usage_metadata = _codex_usage_metadata(
        usage_response_records,
        usage_event_snapshots,
        client_version=client_version,
        diagnostics=usage_diagnostics,
    )
    if usage_metadata["availability"] == "complete":
        if not usage_event_snapshots:
            for name in ("total_tokens", "input_tokens", "output_tokens"):
                value = usage_metadata[name]
                if value is not None:
                    token_usage[name] = value
            token_usage["reasoning_tokens"] = usage_metadata["reasoning_output_tokens"] or 0
        if usage_metadata["total_tokens"] is not None:
            peak_context_tokens = max(
                peak_context_tokens,
                max((item[1]["total_tokens"] or 0 for item in usage_response_records), default=0),
            )
    return SessionRecord(
        provider="codex",
        source_id=safe_source_id,
        source_kind="codex-rollout",
        locators=tuple(_locator(path, home) for path in sorted(paths)),
        fingerprint_locators=(_locator(preferred, home),),
        fingerprint=fingerprint,
        size=sum(item.st_size for item in stats),
        mtime_ns=max(item.st_mtime_ns for item in stats),
        event_count=count,
        disposition=disposition,
        workspace_refs=tuple(filter(None, (workspace_ref,))),
        started_at=min(timestamps) if timestamps else "",
        ended_at=max(timestamps) if timestamps else "",
        parent_ref=parent_ref,
        models=tuple(models),
        efforts=tuple(efforts),
        role=role,
        thread_source=thread_source,
        delegation_depth=delegation_depth,
        total_tokens=token_usage["total_tokens"],
        input_tokens=token_usage["input_tokens"],
        output_tokens=token_usage["output_tokens"],
        reasoning_tokens=token_usage["reasoning_tokens"],
        context_window_tokens=context_window_tokens,
        peak_context_tokens=peak_context_tokens,
        diagnostics=tuple(diagnostics),
        usage_metadata=usage_metadata,
    )


def scan_codex(roots: SourceRoots, *, home: Path | None = None) -> list[SessionRecord]:
    candidates: dict[str, list[Path]] = {}
    for root in roots.codex_sessions:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            suffix = path.stem[-36:]
            source_id = _canonical_uuid(suffix) or path.stem
            candidates.setdefault(source_id, []).append(path)
    records: list[SessionRecord] = []
    for source_id, paths in candidates.items():
        records.append(_codex_record(source_id, paths, home=home))
    return records


def _claude_record(path: Path, *, home: Path | None = None) -> SessionRecord:
    before = _source_snapshot((path,))
    filename_id = _canonical_uuid(path.stem)
    source_id = _safe_filename_identity(path.stem)
    count = malformed = 0
    canonical_ids: set[str] = set()
    timestamps: list[str] = []
    terminal = False
    models: list[str] = []
    efforts: list[str] = []
    role = ""
    thread_source = ""
    input_tokens = 0
    output_tokens = 0
    usage_message_ids: set[str] = set()
    diagnostics: list[str] = []
    workspace_ref = _workspace_ref(path.parent.name)
    if not filename_id:
        diagnostics.append("invalid_filename_session_id")
    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    malformed += 1
                    continue
                if not isinstance(value, dict):
                    malformed += 1
                    continue
                count += 1
                session_id = value.get("sessionId")
                if isinstance(session_id, str):
                    canonical = _canonical_uuid(session_id)
                    if canonical:
                        canonical_ids.add(canonical)
                    else:
                        diagnostics.append("invalid_embedded_session_id")
                timestamp = _normalize_timestamp(value.get("timestamp"))
                if timestamp:
                    timestamps.append(timestamp)
                elif value.get("timestamp") is not None:
                    diagnostics.append("invalid_timestamp")
                if isinstance(value.get("cwd"), str):
                    workspace_ref = _workspace_ref(value["cwd"])
                if value.get("isSidechain") is True:
                    role = "worker"
                    thread_source = "subagent"
                message = value.get("message")
                if value.get("type") == "assistant" and isinstance(message, dict):
                    raw_model = message.get("model")
                    if isinstance(raw_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_model):
                        if raw_model not in models:
                            models.append(raw_model)
                    raw_effort = value.get("effort") or value.get("perTurnEffort")
                    if isinstance(raw_effort, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", raw_effort):
                        if raw_effort not in efforts:
                            efforts.append(raw_effort)
                    message_id = message.get("id")
                    usage_key = message_id if isinstance(message_id, str) and message_id else f"row:{count}"
                    usage = message.get("usage")
                    if usage_key not in usage_message_ids and isinstance(usage, dict):
                        usage_message_ids.add(usage_key)

                        def usage_value(name: str) -> int:
                            candidate = usage.get(name)
                            return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0 else 0

                        input_tokens += (
                            usage_value("input_tokens")
                            + usage_value("cache_read_input_tokens")
                            + usage_value("cache_creation_input_tokens")
                        )
                        output_tokens += usage_value("output_tokens")
                if value.get("type") in {"result", "sessionEnd", "last-prompt"}:
                    terminal = True
    except OSError:
        malformed += 1
    if len(canonical_ids) > 1:
        diagnostics.append("conflicting_session_ids")
    if not _ends_with_newline(path):
        malformed += 1
        diagnostics.append("missing_final_newline")
    identity_invalid = (
        not filename_id
        or len(canonical_ids) != 1
        or filename_id not in canonical_ids
    )
    if canonical_ids != ({filename_id} if filename_id else set()):
        diagnostics.append("filename_session_id_mismatch")
    if terminal:
        diagnostics.append("terminal_signal")
    if malformed:
        diagnostics.append(f"malformed_rows:{malformed}")
    fingerprint, stats, mutated = _finish_source_read((path,), before)
    if mutated:
        diagnostics.extend(("source_mutated_during_read", "retry_required"))
    file_stat = stats[0]
    disposition = (
        "skipped_stub"
        if count == 0 and not malformed
        else "volatile"
        if mutated
        else "quarantined"
        if malformed or identity_invalid
        else "volatile"
        if _is_volatile(path) and not terminal
        else "pending_summary"
    )
    return SessionRecord(
        provider="claude",
        source_id=source_id,
        source_kind="claude-project",
        locators=(_locator(path, home),),
        fingerprint_locators=(_locator(path, home),),
        fingerprint=fingerprint,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        event_count=count,
        disposition=disposition,
        workspace_refs=(workspace_ref,),
        started_at=min(timestamps) if timestamps else "",
        ended_at=max(timestamps) if timestamps else "",
        models=tuple(models),
        efforts=tuple(efforts),
        role=role,
        thread_source=thread_source,
        total_tokens=input_tokens + output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        diagnostics=tuple(diagnostics),
    )


def scan_claude(roots: SourceRoots, *, home: Path | None = None) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    if not roots.claude_projects.is_dir():
        return records
    for path in sorted(roots.claude_projects.rglob("*.jsonl")):
        records.append(_claude_record(path, home=home))
    return records


def discover(
    providers: Iterable[str] = PROVIDERS,
    *,
    roots: SourceRoots | None = None,
    home: Path | None = None,
) -> list[SessionRecord]:
    selected = tuple(dict.fromkeys(providers))
    unknown = set(selected) - set(PROVIDERS)
    if unknown:
        raise HistoryError(f"Unknown history provider: {', '.join(sorted(unknown))}")
    actual_roots = roots or SourceRoots.defaults(home)
    scanners = {"copilot": scan_copilot, "codex": scan_codex, "claude": scan_claude}
    records: list[SessionRecord] = []
    for provider in selected:
        records.extend(scanners[provider](actual_roots, home=home))
    unique: dict[str, SessionRecord] = {}
    for record in sorted(records, key=lambda item: (item.provider, item.source_id, item.source_kind)):
        previous = unique.get(record.bead_id)
        if previous is None:
            unique[record.bead_id] = record
            continue
        if previous.provider != record.provider or previous.source_id != record.source_id:
            raise HistoryError(f"Stable history ID collision: {record.bead_id}")
        if previous.fingerprint != record.fingerprint:
            raise HistoryError(
                f"Duplicate canonical history identity has conflicting sources: {record.bead_id}"
            )
        unique[record.bead_id] = dataclasses.replace(
            previous,
            locators=tuple(sorted(set(previous.locators) | set(record.locators))),
            workspace_refs=tuple(
                sorted(set(previous.workspace_refs) | set(record.workspace_refs))
            ),
            diagnostics=tuple(
                sorted(set(previous.diagnostics) | set(record.diagnostics) | {"duplicate_collapsed"})
            ),
        )
    return sorted(unique.values(), key=lambda item: (item.provider, item.source_id))


def _empty_manifest() -> dict[str, Any]:
    return {"schema_version": 1, "sessions": {}, "last_sync_at": ""}


def load_manifest(archive: Path) -> dict[str, Any]:
    if not archive.is_dir():
        return _empty_manifest()
    with _archive_lock(archive):
        return _load_manifest_unlocked(archive)


def _load_manifest_unlocked(archive: Path) -> dict[str, Any]:
    path = archive / MANIFEST_NAME
    if not path.is_file():
        return _empty_manifest()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryError(f"History manifest is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HistoryError("History manifest has an unsupported schema")
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise HistoryError("History manifest sessions are invalid")
    return value


def _empty_artifact_config() -> dict[str, Any]:
    return {"schema_version": 1, "artifacts": {}}


def load_artifact_config(archive: Path) -> dict[str, Any]:
    if not archive.is_dir():
        return _empty_artifact_config()
    with _archive_lock(archive):
        return _load_artifact_config_unlocked(archive)


def _load_artifact_config_unlocked(archive: Path) -> dict[str, Any]:
    path = archive / ARTIFACT_CONFIG_NAME
    if not path.is_file():
        return _empty_artifact_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryError(f"Artifact configuration is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("artifacts"), dict):
        raise HistoryError("Artifact configuration has an unsupported schema")
    artifacts: dict[str, dict[str, Any]] = {}
    for key, item in value["artifacts"].items():
        checked = _validate_artifact_registration(key, item)
        artifacts[checked["name"]] = checked
    return {"schema_version": 1, "artifacts": artifacts}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EACCES}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        os.close(descriptor)


def register_artifact(
    name: str, path: str | Path, *, title: str, kind: str, description: str,
    include: Iterable[str] = (), watch: Iterable[str] = (), replace: bool = False,
    archive: Path | None = None, home: Path | None = None,
) -> dict[str, Any]:
    """Store an explicit local artifact registration; never read its content here."""
    target = archive or archive_path()
    name = _safe_artifact_name(name)
    title = _safe_artifact_text(title, "title")
    kind = _safe_artifact_text(kind, "kind", limit=80)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", kind):
        raise HistoryError("Artifact kind must use lowercase letters, digits, dots, underscores, or hyphens")
    description = _safe_artifact_text(description, "description", limit=1000)
    source = Path(os.path.abspath(Path(path).expanduser()))
    try:
        source_stat = _artifact_lstat(source)
    except OSError as exc:
        raise HistoryError(f"Artifact source is unavailable: {exc}") from exc
    if stat.S_ISLNK(source_stat.st_mode):
        raise HistoryError("Artifact source root may not be a symlink")
    if not stat.S_ISREG(source_stat.st_mode) and not stat.S_ISDIR(source_stat.st_mode):
        raise HistoryError("Artifact source must be a regular file or directory")
    includes = tuple(_safe_pattern(item, "include") for item in include)
    watches = tuple(_safe_pattern(item, "watch") for item in watch)
    if stat.S_ISDIR(source_stat.st_mode) and not includes:
        raise HistoryError("Directory artifact sources require at least one --include pattern")
    if stat.S_ISREG(source_stat.st_mode) and (includes or watches):
        raise HistoryError("File artifact sources do not accept include or watch patterns")
    item = {"name": name, "bead_id": stable_artifact_id(name), "path": str(source),
            "title": title, "kind": kind, "description": description,
            "include": list(includes), "watch": list(watches), "source_type": "directory" if stat.S_ISDIR(source_stat.st_mode) else "file"}
    with _archive_lock(target):
        ensure_archive(target)
        _recover_pending_archive_update(target)
        config = load_artifact_config(target)
        existing = config["artifacts"].get(name)
        if existing == item:
            return {"artifact": name, "id": item["bead_id"], "status": "unchanged"}
        if existing is not None and not replace:
            raise HistoryError("Artifact registration conflicts; use --replace to update it")
        config["artifacts"][name] = item
        _atomic_json(target / ARTIFACT_CONFIG_NAME, config)
        return {"artifact": name, "id": item["bead_id"], "status": "replaced" if existing else "registered"}


def list_artifacts(archive: Path | None = None) -> list[dict[str, Any]]:
    target = archive or archive_path()
    values = load_artifact_config(target)["artifacts"]
    return [{"name": name, "id": value.get("bead_id"), "title": value.get("title"), "kind": value.get("kind"), "source_type": value.get("source_type")} for name, value in sorted(values.items()) if isinstance(value, dict)]


def unregister_artifact(name: str, *, archive: Path | None = None) -> dict[str, Any]:
    """Remove an active registration; sync will reconcile its indexed row."""
    target = archive or archive_path()
    name = _safe_artifact_name(name)
    with _archive_lock(target):
        ensure_archive(target)
        _recover_pending_archive_update(target)
        config = load_artifact_config(target)
        if name not in config["artifacts"]:
            return {"artifact": name, "id": stable_artifact_id(name), "status": "unchanged"}
        del config["artifacts"][name]
        _atomic_json(target / ARTIFACT_CONFIG_NAME, config)
        return {"artifact": name, "id": stable_artifact_id(name), "status": "unregistered"}


def _artifact_lstat(path: Path) -> os.stat_result:
    """lstat a path after opening every parent directory without following links."""
    parent_fd = _artifact_directory_fd(path.parent)
    try:
        return os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)


def _artifact_directory_fd(path: Path) -> int:
    """Open a directory path with no-follow semantics for every component."""
    flags = _artifact_open_flags(directory=True)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OSError("unsafe artifact directory path")
    # macOS exposes temporary directories through the stable /var alias.
    if path.parts[1:2] == ("var",) and Path("/private/var").is_dir():
        path = Path("/private/var", *path.parts[2:])
    current_fd = os.open(os.sep, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _artifact_files(source: Path, patterns: Iterable[str]) -> tuple[list[Path], bool]:
    try:
        source_stat = _artifact_lstat(source)
    except OSError:
        return [], True
    if stat.S_ISREG(source_stat.st_mode):
        return ([source] if not stat.S_ISLNK(source_stat.st_mode) else []), stat.S_ISLNK(source_stat.st_mode)
    if not stat.S_ISDIR(source_stat.st_mode):
        return [], True
    found: set[Path] = set()
    unsafe = False
    for pattern in patterns:
        for path in source.glob(pattern):
            if path.is_symlink():
                unsafe = True
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            current = source
            if any((current := current / part).is_symlink() for part in relative.parts):
                unsafe = True
                continue
            found.add(path)
    return sorted(found, key=lambda item: item.relative_to(source).as_posix()), unsafe


def _artifact_open_flags(*, directory: bool = False) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("no-follow artifact opens are unavailable")
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _hash_artifact_member(root_fd: int, relative: Path) -> str:
    """Hash one member beneath a held directory FD without following symlinks."""
    flags = _artifact_open_flags()
    directory_flags = _artifact_open_flags(directory=True)
    current_fd = os.dup(root_fd)
    try:
        parts = relative.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise OSError("unsafe artifact member path")
        for part in parts[:-1]:
            before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise OSError("artifact member parent is not a directory")
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                os.close(next_fd)
                raise OSError("artifact member parent changed during open")
            os.close(current_fd)
            current_fd = next_fd
        name = parts[-1]
        before = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("artifact member is not a regular file")
        member_fd = os.open(name, flags, dir_fd=current_fd)
        try:
            opened = os.fstat(member_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise OSError("artifact member changed during open")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(member_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(member_fd)
            if (after.st_dev, after.st_ino, after.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
                raise OSError("artifact member changed during read")
            return digest.hexdigest()
        finally:
            os.close(member_fd)
    finally:
        os.close(current_fd)


def _artifact_root_fd(
    source: Path, *, expected_identity: tuple[int, int] | None = None
) -> tuple[int, bool]:
    source_stat = _artifact_lstat(source)
    source_identity = (source_stat.st_dev, source_stat.st_ino)
    if expected_identity is not None and source_identity != expected_identity:
        raise OSError("registered artifact source changed before open")
    if stat.S_ISDIR(source_stat.st_mode):
        root_fd = _artifact_directory_fd(source)
        file_source = False
    elif stat.S_ISREG(source_stat.st_mode):
        root_fd = _artifact_directory_fd(source.parent)
        file_source = True
    else:
        raise OSError("registered artifact source is not a regular file or directory")
    try:
        opened = os.fstat(root_fd)
        if (
            not file_source
            and expected_identity is not None
            and (opened.st_dev, opened.st_ino) != expected_identity
        ):
            raise OSError("registered artifact root changed during open")
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("registered artifact root is not a directory")
        if file_source and expected_identity is not None:
            current = os.stat(source.name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != expected_identity
            ):
                raise OSError("registered artifact file changed during open")
        return root_fd, file_source
    except BaseException:
        os.close(root_fd)
        raise


def _artifact_root_matches(
    source: Path,
    root_fd: int,
    file_source: bool,
    expected_source_identity: tuple[int, int] | None = None,
) -> bool:
    try:
        root = os.fstat(root_fd)
        current = _artifact_lstat(source.parent if file_source else source)
        if file_source and expected_source_identity is not None:
            source_stat = os.stat(source.name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return False
    root_matches = (
        stat.S_ISDIR(current.st_mode)
        and (root.st_dev, root.st_ino) == (current.st_dev, current.st_ino)
    )
    if not root_matches:
        return False
    if file_source and expected_source_identity is not None:
        return (
            stat.S_ISREG(source_stat.st_mode)
            and (source_stat.st_dev, source_stat.st_ino) == expected_source_identity
        )
    if not file_source and expected_source_identity is not None:
        return (root.st_dev, root.st_ino) == expected_source_identity
    return True


def _artifact_snapshot(paths: Iterable[Path]) -> dict[str, tuple[int, int, int, int]]:
    snapshot: dict[str, tuple[int, int, int, int]] = {}
    for path in paths:
        value = _artifact_lstat(path)
        snapshot[str(path)] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    return snapshot


def _artifact_quarantine_value(item: Any) -> dict[str, Any]:
    name = "unknown"
    if isinstance(item, dict) and isinstance(item.get("name"), str):
        try:
            if _safe_artifact_name(item["name"]) == item["name"]:
                name = item["name"]
        except HistoryError:
            pass
    return {
        "bead_id": stable_artifact_id(name), "artifact_name": name,
        "title": "Artifact", "kind": "unknown",
        "description": "Artifact registration rejected.", "source_type": "unknown",
        "disposition": "quarantined", "fingerprint": "", "locators": [],
        "member_count": 0, "diagnostics": ["invalid_registration", "retry_required"],
    }


def _artifact_value(item: dict[str, Any], *, home: Path | None = None) -> dict[str, Any]:
    """Read one artifact defensively; source races must not abort archive sync."""
    try:
        checked = _validate_artifact_registration(item.get("name") if isinstance(item, dict) else None, item)
    except HistoryError:
        return _artifact_quarantine_value(item)
    try:
        return _artifact_value_inner(checked, home=home)
    except OSError:
        return {
            "bead_id": checked["bead_id"], "artifact_name": checked["name"],
            "title": checked["title"], "kind": checked["kind"],
            "description": checked["description"], "source_type": checked["source_type"],
            "disposition": "volatile", "fingerprint": "", "locators": [],
            "member_count": 0,
            "diagnostics": ["source_unreadable_or_mutated", "retry_required"],
        }


def _artifact_tombstone(previous: dict[str, Any]) -> dict[str, Any]:
    """Represent an unregistered artifact without retaining source provenance."""
    name = str(previous.get("artifact_name") or "unknown")
    try:
        name = _safe_artifact_name(name)
    except HistoryError:
        name = "unknown"
    kind = str(previous.get("kind") or "unknown")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", kind):
        kind = "unknown"
    source_type = previous.get("source_type")
    if source_type not in {"file", "directory"}:
        source_type = "unknown"
    return {
        "bead_id": stable_artifact_id(name),
        "artifact_name": name,
        "title": str(previous.get("title") or "Artifact"),
        "kind": kind,
        "description": "Artifact registration was removed; no source content is indexed.",
        "source_type": source_type,
        "disposition": "removed",
        "fingerprint": "",
        "locators": [],
        "member_count": 0,
        "diagnostics": ["unregistered"],
    }


def _artifact_value_inner(item: dict[str, Any], *, home: Path | None = None) -> dict[str, Any]:
    configured_source = Path(str(item["path"]))
    name = str(item["name"])
    base = {"bead_id": stable_artifact_id(name), "artifact_name": name, "title": item["title"], "kind": item["kind"], "description": item["description"], "source_type": item["source_type"], "disposition": "indexed"}
    try:
        configured_stat = _artifact_lstat(configured_source)
    except FileNotFoundError:
        return base | {"disposition": "missing", "fingerprint": "", "locators": [], "member_count": 0}
    if stat.S_ISLNK(configured_stat.st_mode) or not stat.S_ISREG(configured_stat.st_mode) and not stat.S_ISDIR(configured_stat.st_mode):
        return base | {"disposition": "missing", "fingerprint": "", "locators": [], "member_count": 0}
    configured_identity = (configured_stat.st_dev, configured_stat.st_ino)
    try:
        root_fd, file_source = _artifact_root_fd(
            configured_source, expected_identity=configured_identity
        )
    except OSError:
        raise
    source = configured_source
    root = source.parent if file_source else source
    stable = False
    locators: list[str] = []
    try:
        include_files, unsafe_include = _artifact_files(source, item.get("include", []))
        watch_files, unsafe_watch = _artifact_files(source, item.get("watch", []))
        if not _artifact_root_matches(
            source, root_fd, file_source, configured_identity
        ):
            return base | {"disposition": "volatile", "fingerprint": "", "locators": [], "member_count": 0, "diagnostics": ["source_mutated_during_read", "retry_required"]}
        if unsafe_include or unsafe_watch:
            return base | {"disposition": "quarantined", "fingerprint": "", "locators": [], "member_count": 0, "diagnostics": ["symlink_match_rejected"]}
        if not include_files:
            return base | {"disposition": "missing", "fingerprint": "", "locators": [], "member_count": 0, "diagnostics": ["no_safe_include_matches"]}
        before = _artifact_snapshot([root, *include_files, *watch_files])
        if not _artifact_root_matches(
            source, root_fd, file_source, configured_identity
        ):
            return base | {"disposition": "volatile", "fingerprint": "", "locators": [], "member_count": 0, "diagnostics": ["source_mutated_during_read", "retry_required"]}
        digest = hashlib.sha256()
        for label, files in ((b"include", include_files), (b"watch", watch_files)):
            digest.update(label)
            for file in files:
                relative = Path(file.name) if file_source else file.relative_to(source)
                digest.update(relative.as_posix().encode("utf-8", "surrogateescape")); digest.update(b"\0")
                digest.update(_hash_artifact_member(root_fd, relative).encode()); digest.update(b"\0")
        after_include, after_unsafe_include = _artifact_files(source, item.get("include", []))
        after_watch, after_unsafe_watch = _artifact_files(source, item.get("watch", []))
        after_paths = [root, *after_include, *after_watch]
        try:
            after = _artifact_snapshot(after_paths)
        except OSError:
            after = {}
        stable = (
            _artifact_root_matches(
                source, root_fd, file_source, configured_identity
            )
            and before == after
            and include_files == after_include
            and watch_files == after_watch
            and not after_unsafe_include
            and not after_unsafe_watch
        )
        if stable:
            locators = [_lexical_locator(file, home) for file in include_files]
    finally:
        os.close(root_fd)
    if not stable:
        return base | {"disposition": "volatile", "fingerprint": "", "locators": [], "member_count": 0, "diagnostics": ["source_mutated_during_read", "retry_required"]}
    return base | {"fingerprint": digest.hexdigest(), "locators": locators, "member_count": len(include_files)}


def _archive_beads_dir(archive: Path) -> Path:
    path = archive / ".beads"
    try:
        value = path.lstat()
    except FileNotFoundError:
        return archive.resolve() / ".beads"
    if stat.S_ISLNK(value.st_mode):
        raise HistoryError(f"Refusing archive Beads symlink: {path}")
    if not stat.S_ISDIR(value.st_mode):
        raise HistoryError(f"Archive Beads path is not a directory: {path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(archive.resolve())
    except ValueError as exc:
        raise HistoryError("Archive Beads path escapes the archive") from exc
    return resolved


def _history_beads_environment(archive: Path) -> dict[str, str]:
    return {"BEADS_DIR": str(_archive_beads_dir(archive))}


def _verify_history_beads_workspace(archive: Path) -> bool:
    result = beads_backend.run(
        archive,
        "where",
        "--json",
        timeout=8,
        environment_overrides=_history_beads_environment(archive),
    )
    if result.returncode:
        return False
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HistoryError("History Beads workspace returned invalid identity") from exc
    actual = Path(str(value.get("path") or "")).resolve() if isinstance(value, dict) else None
    expected = _archive_beads_dir(archive)
    if actual != expected:
        raise HistoryError(
            f"History Beads workspace escaped the archive: {actual or 'unknown'}"
        )
    return True


def ensure_archive(archive: Path) -> str:
    with _archive_lock(archive):
        return _ensure_archive_locked(archive)


def _ensure_archive_locked(archive: Path) -> str:
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(archive, 0o700)
    mode = stat.S_IMODE(archive.stat().st_mode)
    if mode != 0o700:
        raise HistoryError(f"History archive permissions are {mode:o}; expected 700")
    try:
        beads_backend.require_supported_version()
        if _verify_history_beads_workspace(archive):
            return "existing"
        beads_dir = archive / ".beads"
        if beads_dir.exists() and any(beads_dir.iterdir()):
            raise HistoryError("Archive-local Beads state exists but cannot be verified")
        beads_dir.mkdir(mode=0o700, exist_ok=True)
        result = beads_backend.run(
            archive,
            "init",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
            "--prefix",
            "history",
            timeout=90,
            environment_overrides=_history_beads_environment(archive),
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise HistoryError(
                f"History Beads initialization failed: "
                f"{detail[-1] if detail else result.returncode}"
            )
        if not _verify_history_beads_workspace(archive):
            raise HistoryError("History Beads initialization cannot be verified")
        return "created"
    except beads_backend.BeadsError as exc:
        raise HistoryError(str(exc)) from exc


def _bead_row(value: dict[str, Any]) -> dict[str, Any]:
    if "artifact_name" in value:
        disposition = str(value.get("disposition") or "unknown")
        description = (
            f"Curated local artifact ({value.get('kind')}). {value.get('description')}\n\n"
            "Metadata-only index; source content is never copied."
        )
        return {
            "id": str(value["bead_id"]), "title": str(value["title"]), "description": description,
            "issue_type": "chore", "status": "closed", "priority": 2,
            "labels": ["history", "history:artifact", f"history:artifact-kind:{value.get('kind')}", f"history:disposition:{disposition}"],
            "source_system": "agentflow-history-artifact", "external_ref": str(value["artifact_name"]),
            "metadata": {"agentflow_history_artifact": value},
            "updated_at": value.get("bead_updated_at") or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
    provider = str(value["provider"])
    source_id = str(value["source_id"])
    disposition = str(value["disposition"])
    summary = value.get("summary")
    description = (
        "Metadata-only local provider session disposition. "
        "No transcript, prompt, response, reasoning, tool data, secret, or raw log is stored."
    )
    if isinstance(summary, dict):
        description += "\n\n" + _render_summary(summary)
    labels = [
        "history",
        f"history:provider:{provider}",
        f"history:disposition:{disposition}",
    ]
    workspace_refs = value.get("workspace_refs")
    if not isinstance(workspace_refs, list):
        legacy = str(value.get("workspace_ref") or "")
        workspace_refs = [legacy] if legacy else []
    for workspace_ref in workspace_refs:
        if workspace_ref:
            labels.append(f"history:workspace:{workspace_ref}")
    title = f"{provider} session {source_id[:12]}"
    if isinstance(summary, dict):
        goal = " ".join(str(summary.get("goal") or "").split())
        if goal:
            title = f"{provider}: {goal[:90]}"
    return {
        "id": stable_session_id(provider, source_id),
        "title": title,
        "description": description,
        "issue_type": "chore",
        "status": "closed",
        "priority": 2,
        "labels": labels,
        "source_system": f"agentflow-history-{provider}",
        "external_ref": source_id,
        "metadata": {"agentflow_history": value},
        "updated_at": value.get("bead_updated_at")
        or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def _import_rows(archive: Path, values: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = [_bead_row(value) for value in values]
    if not rows:
        return {}
    input_text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    result = beads_backend.run(
        archive,
        "import",
        "-",
        "--json",
        input_text=input_text,
        timeout=120,
        environment_overrides=_history_beads_environment(archive),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise HistoryError(f"Beads history import failed: {detail[-1] if detail else result.returncode}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"output": result.stdout.strip()}


def _pending_archive_update(archive: Path) -> dict[str, Any] | None:
    path = archive / TRANSACTION_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise HistoryError("Pending history update is not a private regular file")
    try:
        transaction = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryError(f"Pending history update is unreadable: {exc}") from exc
    if (
        not isinstance(transaction, dict)
        or set(transaction) != {"schema_version", "manifest", "rows"}
        or transaction.get("schema_version") != 1
    ):
        raise HistoryError("Pending history update has an unsupported schema")
    manifest = transaction.get("manifest")
    rows = transaction.get("rows")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("sessions"), dict)
        or ("artifacts" in manifest and not isinstance(manifest["artifacts"], dict))
        or not isinstance(rows, list)
    ):
        raise HistoryError("Pending history update has invalid archive data")
    sessions = manifest["sessions"]
    artifacts = manifest.get("artifacts", {})
    imported_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("bead_id"), str):
            raise HistoryError("Pending history update contains an invalid Beads row")
        bead_id = row["bead_id"]
        if bead_id in imported_ids:
            raise HistoryError("Pending history update contains duplicate Beads rows")
        imported_ids.add(bead_id)
        is_artifact = "artifact_name" in row
        expected = (artifacts if is_artifact else sessions).get(bead_id)
        if not isinstance(expected, dict) or expected != row:
            raise HistoryError("Pending history update does not match its manifest")
        try:
            _bead_row(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoryError("Pending history update contains an invalid Beads row") from exc
    return {"manifest": manifest, "rows": rows}


def _clear_pending_archive_update(archive: Path) -> None:
    path = archive / TRANSACTION_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HistoryError("Refusing to remove a non-regular pending history update")
    path.unlink()
    _fsync_directory(archive)


def _recover_pending_archive_update(archive: Path) -> None:
    """Replay a prepared import before any new archive transaction can proceed.

    Beads history rows use deterministic session/artifact IDs, so replaying an
    import whose outcome was ambiguous is an upsert, not a duplicate creation.
    The journal remains until both the import and manifest replacement finish.
    """
    pending = _pending_archive_update(archive)
    if pending is None:
        return
    _import_rows(archive, pending["rows"])
    manifest = pending["manifest"]
    _preserve_newer_summaries(manifest, _load_manifest_unlocked(archive))
    _atomic_json(archive / MANIFEST_NAME, manifest)
    _clear_pending_archive_update(archive)


def _commit_archive_update(
    archive: Path,
    manifest: dict[str, Any],
    import_values: Iterable[dict[str, Any]],
) -> None:
    rows = list(import_values)
    if rows:
        _atomic_json(
            archive / TRANSACTION_NAME,
            {"schema_version": 1, "manifest": manifest, "rows": rows},
        )
        # If import fails or the process exits here, the prepared transaction is
        # deliberately retained and replayed on the next mutating archive call.
    _import_rows(archive, rows)
    # The archive lock serializes independent writers. This final merge also
    # protects a summary completed reentrantly by a same-thread integration
    # callback while the outer import was in flight.
    _preserve_newer_summaries(manifest, _load_manifest_unlocked(archive))
    _atomic_json(archive / MANIFEST_NAME, manifest)
    if rows:
        _clear_pending_archive_update(archive)


def _preserve_newer_summaries(
    manifest: dict[str, Any], latest: dict[str, Any]
) -> None:
    sessions = manifest.get("sessions")
    latest_sessions = latest.get("sessions")
    if not isinstance(sessions, dict) or not isinstance(latest_sessions, dict):
        return
    for session_id, current in list(sessions.items()):
        newer = latest_sessions.get(session_id)
        if not isinstance(current, dict) or not isinstance(newer, dict):
            continue
        summary = newer.get("summary")
        fingerprint = current.get("fingerprint")
        if (
            not isinstance(summary, dict)
            or not fingerprint
            or newer.get("summary_source_fingerprint") != fingerprint
        ):
            continue
        try:
            current_updated = dt.datetime.fromisoformat(
                str(current.get("bead_updated_at") or "").replace("Z", "+00:00")
            )
            newer_updated = dt.datetime.fromisoformat(
                str(newer.get("bead_updated_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            current_updated = newer_updated = None
        if current_updated is not None and current_updated.tzinfo is None:
            current_updated = current_updated.replace(tzinfo=dt.timezone.utc)
        if newer_updated is not None and newer_updated.tzinfo is None:
            newer_updated = newer_updated.replace(tzinfo=dt.timezone.utc)
        if current_updated is not None and newer_updated is not None and newer_updated <= current_updated:
            continue
        if current_updated is not None and newer_updated is None:
            continue
        preserved = dict(current)
        preserved["summary"] = summary
        preserved["summary_source_fingerprint"] = fingerprint
        preserved["disposition"] = "summarized"
        if isinstance(newer.get("bead_updated_at"), str):
            preserved["bead_updated_at"] = newer["bead_updated_at"]
        sessions[session_id] = preserved


def sync(
    *,
    archive: Path | None = None,
    providers: Iterable[str] = PROVIDERS,
    dry_run: bool = False,
    roots: SourceRoots | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    target = archive or archive_path()
    selected_providers = tuple(dict.fromkeys(providers))
    # Session discovery only reads provider files and has no archive side
    # effects. Keep it outside the transaction lock; all decisions based on the
    # archive's current manifest/configuration happen after acquiring the lock.
    records = discover(selected_providers, roots=roots, home=home)
    if dry_run:
        if target.is_dir():
            with _archive_lock(target):
                return _sync_archive_locked(
                    target, records, selected_providers, home=home, dry_run=True
                )
        return _sync_archive_locked(
            target, records, selected_providers, home=home, dry_run=True
        )
    with _archive_lock(target):
        if _pending_archive_update(target) is not None:
            ensure_archive(target)
            _recover_pending_archive_update(target)
        return _sync_archive_locked(
            target, records, selected_providers, home=home, dry_run=False
        )


def _sync_archive_locked(
    target: Path,
    records: list[SessionRecord],
    selected_providers: tuple[str, ...],
    *,
    home: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    manifest = load_manifest(target)
    old_sessions = manifest["sessions"]
    artifact_config = load_artifact_config(target)
    old_artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    selected = set(selected_providers)
    seen: set[str] = set()
    new_values: list[dict[str, Any]] = []
    import_values: list[dict[str, Any]] = []
    added = changed = unchanged = 0
    dispositions: dict[str, int] = {}
    for record in records:
        key = record.bead_id
        seen.add(key)
        value = record.manifest_value()
        previous = old_sessions.get(key)
        if (
            isinstance(previous, dict)
            and isinstance(previous.get("summary"), dict)
            and previous.get("summary_source_fingerprint") == value["fingerprint"]
        ):
            value["summary"] = previous["summary"]
            value["summary_source_fingerprint"] = previous["summary_source_fingerprint"]
            value["disposition"] = "summarized"
        if previous is None:
            added += 1
            value["bead_updated_at"] = _next_bead_updated_at(None)
            import_values.append(value)
        elif _comparable(previous) == _comparable(value):
            unchanged += 1
            value["bead_updated_at"] = previous.get("bead_updated_at", "")
        else:
            changed += 1
            value["bead_updated_at"] = _next_bead_updated_at(previous)
            import_values.append(value)
        value["missing_scans"] = 0
        new_values.append(value)
        dispositions[value["disposition"]] = dispositions.get(value["disposition"], 0) + 1

    missing = 0
    for key, previous in old_sessions.items():
        if not isinstance(previous, dict) or previous.get("provider") not in selected or key in seen:
            continue
        value = dict(previous)
        if value.get("disposition") == "missing":
            value["missing_scans"] = max(2, int(value.get("missing_scans") or 0))
            new_values.append(value)
            missing += 1
            unchanged += 1
            dispositions["missing"] = dispositions.get("missing", 0) + 1
            continue
        value["missing_scans"] = int(value.get("missing_scans") or 0) + 1
        value["disposition"] = "missing" if value["missing_scans"] >= 2 else "missing_pending"
        value["bead_updated_at"] = _next_bead_updated_at(previous)
        new_values.append(value)
        import_values.append(value)
        missing += 1
        changed += 1
        dispositions[value["disposition"]] = dispositions.get(value["disposition"], 0) + 1

    artifact_values: dict[str, dict[str, Any]] = {}
    artifact_added = artifact_changed = artifact_unchanged = 0
    for item in artifact_config["artifacts"].values():
        if not isinstance(item, dict):
            continue
        value = _artifact_value(item, home=home)
        key = str(value["bead_id"])
        previous = old_artifacts.get(key)
        if previous is None:
            artifact_added += 1; value["bead_updated_at"] = _next_bead_updated_at(None); import_values.append(value)
        elif _comparable(previous) == _comparable(value):
            artifact_unchanged += 1; value["bead_updated_at"] = previous.get("bead_updated_at", "")
        else:
            artifact_changed += 1; value["bead_updated_at"] = _next_bead_updated_at(previous); import_values.append(value)
        artifact_values[key] = value

    for key, previous in old_artifacts.items():
        if key in artifact_values or not isinstance(previous, dict) or "artifact_name" not in previous:
            continue
        value = _artifact_tombstone(previous)
        value["bead_id"] = str(previous.get("bead_id") or value["bead_id"])
        if _comparable(previous) == _comparable(value):
            artifact_unchanged += 1
            value["bead_updated_at"] = previous.get("bead_updated_at", "")
        else:
            artifact_changed += 1
            value["bead_updated_at"] = _next_bead_updated_at(previous)
            import_values.append(value)
        artifact_values[key] = value

    report = {
        "archive": str(target),
        "providers": sorted(selected),
        "discovered": len(records),
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "missing": missing,
        "artifacts": {"registered": len(artifact_config["artifacts"]), "added": artifact_added, "changed": artifact_changed, "unchanged": artifact_unchanged},
        "dispositions": dict(sorted(dispositions.items())),
        "dry_run": dry_run,
        "model_work": 0,
    }
    if dry_run:
        return report
    ensure_archive(target)
    merged = dict(old_sessions)
    for value in new_values:
        merged[str(value["bead_id"])] = value
    manifest["sessions"] = merged
    manifest["artifacts"] = artifact_values
    manifest["last_sync_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _commit_archive_update(target, manifest, import_values)
    return report


def _comparable(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"missing_scans", "summary", "disposition", "bead_updated_at"}
    } | {"disposition": value.get("disposition") if value.get("disposition") != "summarized" else "pending_summary"}


def _next_bead_updated_at(previous: dict[str, Any] | None) -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if previous:
        text = previous.get("bead_updated_at")
        if isinstance(text, str) and text:
            try:
                prior = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
                now = max(now, prior + dt.timedelta(seconds=1))
            except ValueError:
                pass
    return now.isoformat(timespec="seconds")


def status(archive: Path | None = None) -> dict[str, Any]:
    target = archive or archive_path()
    manifest = load_manifest(target)
    configured_artifacts = load_artifact_config(target)["artifacts"]
    sessions = [value for value in manifest["sessions"].values() if isinstance(value, dict)]
    artifacts = [value for value in (manifest.get("artifacts") or {}).values() if isinstance(value, dict)]
    dispositions: dict[str, int] = {}
    providers: dict[str, int] = {}
    for value in sessions:
        disposition = str(value.get("disposition") or "unknown")
        provider = str(value.get("provider") or "unknown")
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
        providers[provider] = providers.get(provider, 0) + 1
    mode = None
    if target.exists():
        mode = f"{stat.S_IMODE(target.stat().st_mode):04o}"
    return {
        "archive": str(target),
        "exists": target.is_dir(),
        "permissions": mode,
        "beads": (target / ".beads").is_dir(),
        "sessions": len(sessions),
        "artifacts": {"registered": len(configured_artifacts), "indexed": len(artifacts)},
        "providers": dict(sorted(providers.items())),
        "dispositions": dict(sorted(dispositions.items())),
        "last_sync_at": manifest.get("last_sync_at") or "",
    }


def pending(
    archive: Path | None = None,
    *,
    limit: int = 20,
    provider: str = "",
    workspace: str = "",
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise HistoryError("Pending limit must be between 1 and 100")
    if provider and provider not in PROVIDERS:
        raise HistoryError(f"Unknown history provider: {provider}")
    target = archive or archive_path()
    manifest = load_manifest(target)
    rows: list[dict[str, Any]] = []
    for value in manifest["sessions"].values():
        if not isinstance(value, dict) or value.get("disposition") != "pending_summary":
            continue
        if provider and value.get("provider") != provider:
            continue
        workspace_refs = value.get("workspace_refs")
        if not isinstance(workspace_refs, list):
            legacy = value.get("workspace_ref")
            workspace_refs = [legacy] if legacy else []
        if workspace and workspace not in workspace_refs:
            continue
        rows.append(
            {
                "session_id": value.get("bead_id"),
                "provider": value.get("provider"),
                "source_id": value.get("source_id"),
                "source_fingerprint": value.get("fingerprint"),
                "event_count": value.get("event_count"),
                "safe_locators": value.get("locators", []),
                "workspace_refs": workspace_refs,
            }
        )
    return sorted(rows, key=lambda item: (str(item["provider"]), str(item["source_id"])))[:limit]


def _validate_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistoryError("Summary input must be one JSON object")
    encoded = json.dumps(value, ensure_ascii=False).encode()
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise HistoryError(f"Summary exceeds {MAX_SUMMARY_BYTES} bytes")
    allowed = {"session_id", "source_fingerprint", *SUMMARY_FIELDS}
    extra = set(value) - allowed
    if extra:
        raise HistoryError(f"Summary contains unsupported fields: {', '.join(sorted(extra))}")
    for required in ("session_id", "source_fingerprint", "goal", "outcome"):
        if not isinstance(value.get(required), str) or not value[required].strip():
            raise HistoryError(f"Summary field {required} must be a non-empty string")
    if not re.fullmatch(r"history-s-[0-9a-f]{12}", value["session_id"]):
        raise HistoryError("Summary session_id is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value["source_fingerprint"]):
        raise HistoryError("Summary source_fingerprint is invalid")
    for key in FORBIDDEN_SUMMARY_KEYS:
        if key in value:
            raise HistoryError(f"Summary field {key} is forbidden")
    for key, limit in SUMMARY_FIELDS.items():
        item = value.get(key, [] if isinstance(limit, int) and key not in {"goal", "outcome"} else "")
        if key in {"goal", "outcome"}:
            if not isinstance(item, str) or len(item) > limit:
                raise HistoryError(f"Summary field {key} must be a string of at most {limit} characters")
            _validate_summary_text(item, key)
        else:
            if not isinstance(item, list) or len(item) > limit:
                raise HistoryError(f"Summary field {key} must be a list with at most {limit} items")
            for entry in item:
                if not isinstance(entry, str) or not entry.strip() or len(entry) > 500:
                    raise HistoryError(f"Summary field {key} contains an invalid item")
                _validate_summary_text(entry, key)
    text = json.dumps(value, ensure_ascii=False)
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise HistoryError("Summary rejected because it resembles a credential or secret")
    return value


def _validate_summary_text(value: str, field: str) -> None:
    if "\n" in value or "\r" in value or any(ord(character) < 32 for character in value):
        raise HistoryError(f"Summary field {field} must contain single-line text")
    for pattern in UNTRUSTED_SUMMARY_PATTERNS:
        if pattern.search(value):
            raise HistoryError(
                f"Summary field {field} resembles transcript, log, or injected instructions"
            )


def _current_fingerprint(value: dict[str, Any], home: Path | None = None) -> str:
    locators = value.get("fingerprint_locators")
    if not isinstance(locators, list) or not locators:
        raise HistoryError("Session provenance has no source locator")
    paths = [_path_from_locator(str(locator), home) for locator in locators]
    existing = [path for path in paths if path.is_file()]
    if not existing:
        raise HistoryError("Session source no longer exists")
    return _fingerprint(existing)


def apply_summary(
    summary: dict[str, Any],
    *,
    archive: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    validated = _validate_summary(summary)
    target = archive or archive_path()
    with _archive_lock(target):
        if _pending_archive_update(target) is not None:
            ensure_archive(target)
            _recover_pending_archive_update(target)
        manifest = load_manifest(target)
        session_id = validated["session_id"]
        value = manifest["sessions"].get(session_id)
        if not isinstance(value, dict):
            raise HistoryError(f"Unknown history session: {session_id}")
        if value.get("disposition") not in {"pending_summary", "summarized"}:
            raise HistoryError(
                f"Session disposition {value.get('disposition') or 'unknown'} cannot accept a summary"
            )
        expected = str(value.get("fingerprint") or "")
        if validated["source_fingerprint"] != expected:
            raise HistoryError("Summary source fingerprint does not match the indexed session")
        current = _current_fingerprint(value, home)
        if current != expected:
            raise HistoryError("Session source changed after indexing; run history sync first")
        ensure_archive(target)
        durable = {
            key: validated.get(key, "" if key in {"goal", "outcome"} else [])
            for key in SUMMARY_FIELDS
        }
        if (
            value.get("disposition") == "summarized"
            and value.get("summary") == durable
            and value.get("summary_source_fingerprint") == expected
        ):
            return {"session_id": session_id, "disposition": "summarized"}
        value = dict(value)
        value["summary"] = durable
        value["summary_source_fingerprint"] = expected
        value["disposition"] = "summarized"
        value["bead_updated_at"] = _next_bead_updated_at(value)
        manifest["sessions"][session_id] = value
        _commit_archive_update(target, manifest, [value])
        return {"session_id": session_id, "disposition": "summarized"}


def _render_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Distilled summary (validated against the indexed source fingerprint):",
        f"Goal: {summary.get('goal', '')}",
        f"Outcome: {summary.get('outcome', '')}",
    ]
    for key in ("decisions", "evidence", "blockers", "unresolved"):
        values = summary.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{key.title()}:")
            lines.extend(f"- {item}" for item in values)
    return "\n".join(lines)


def launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library/LaunchAgents" / f"{PLIST_LABEL}.plist"


def _runtime_source_files() -> tuple[Path, ...]:
    source = Path(__file__).resolve().parent
    return tuple(sorted(source.glob("*.py"), key=lambda item: item.name))


def history_runtime_path(archive: Path) -> Path:
    digest = hashlib.sha256()
    for source in _runtime_source_files():
        digest.update(source.name.encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return archive.parent / "runtime" / digest.hexdigest()[:16]


def _install_history_runtime(archive: Path, runtime: Path) -> None:
    sources = _runtime_source_files()
    package = runtime / "agentflow"
    if runtime.exists():
        if runtime.is_symlink() or not runtime.is_dir():
            raise HistoryError(f"Refusing unsafe history runtime path: {runtime}")
        for source in sources:
            destination = package / source.name
            if not destination.is_file() or destination.read_bytes() != source.read_bytes():
                raise HistoryError(f"Installed history runtime is incomplete: {runtime}")
        return

    runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime.parent, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.", dir=runtime.parent))
    try:
        temporary_package = temporary / "agentflow"
        temporary_package.mkdir(mode=0o700)
        for source in sources:
            destination = temporary_package / source.name
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o600)
        temporary.replace(runtime)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def schedule_plist(
    archive: Path | None = None, *, runtime: Path | None = None
) -> bytes:
    target = archive or archive_path()
    runtime_root = runtime or history_runtime_path(target)
    python = shutil.which("python3") or sys.executable
    arguments = [
        python,
        "-m",
        "agentflow",
        "history",
        "sync",
    ]
    return plistlib.dumps(
        {
            "Label": PLIST_LABEL,
            "ProgramArguments": arguments,
            "EnvironmentVariables": {
                ARCHIVE_ENV: str(target),
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": str(runtime_root),
            },
            "StartCalendarInterval": {"Hour": 3, "Minute": 15},
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": os.devnull,
            "StandardErrorPath": os.devnull,
        },
        sort_keys=True,
    )


def _existing_owned_regular_file(path: Path) -> os.stat_result | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(value.st_mode):
        raise HistoryError(f"Refusing LaunchAgent symlink: {path}")
    if not stat.S_ISREG(value.st_mode):
        raise HistoryError(f"Refusing non-regular LaunchAgent path: {path}")
    if value.st_uid != os.getuid():
        raise HistoryError(f"Refusing LaunchAgent not owned by the current user: {path}")
    return value


def _atomic_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _launchctl_loaded(domain: str) -> bool:
    if not shutil.which("launchctl"):
        return False
    result = subprocess.run(
        ["launchctl", "print", f"{domain}/{PLIST_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=8,
    )
    return result.returncode == 0


def schedule(action: str, *, archive: Path | None = None, home: Path | None = None) -> dict[str, Any]:
    target = archive or archive_path()
    path = launch_agent_path(home)
    domain = f"gui/{os.getuid()}"
    if action == "status":
        installed = _existing_owned_regular_file(path) is not None
        loaded = _launchctl_loaded(domain) if installed else False
        return {"installed": installed, "loaded": loaded, "path": str(path)}
    if action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_stat = _existing_owned_regular_file(path)
        previous = path.read_bytes() if previous_stat else None
        runtime = history_runtime_path(target)
        desired = schedule_plist(target, runtime=runtime)
        if previous is not None and previous != desired:
            raise HistoryError(
                "Refusing to overwrite a non-identical existing LaunchAgent without --force"
            )
        loaded_before = _launchctl_loaded(domain) if previous_stat else False
        ensure_archive(target)
        _install_history_runtime(target, runtime)
        if previous is None or (
            previous_stat is not None and stat.S_IMODE(previous_stat.st_mode) != 0o600
        ):
            _atomic_bytes(path, desired)
        if shutil.which("launchctl"):
            subprocess.run(
                ["launchctl", "bootout", domain, str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            if result.returncode:
                rollback_error = ""
                if previous is None:
                    _existing_owned_regular_file(path)
                    path.unlink(missing_ok=True)
                else:
                    _atomic_bytes(
                        path,
                        previous,
                        mode=stat.S_IMODE(previous_stat.st_mode) if previous_stat else 0o600,
                    )
                    if loaded_before:
                        restored = subprocess.run(
                            ["launchctl", "bootstrap", domain, str(path)],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=8,
                        )
                        if restored.returncode:
                            rollback_error = "; previous LaunchAgent could not be reloaded"
                detail = (result.stderr or result.stdout).strip() or "launchctl bootstrap failed"
                raise HistoryError(detail + rollback_error)
        return {"installed": True, "loaded": bool(shutil.which("launchctl")), "path": str(path)}
    if action == "uninstall":
        existed = _existing_owned_regular_file(path) is not None
        if existed and shutil.which("launchctl"):
            subprocess.run(
                ["launchctl", "bootout", domain, str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        if existed:
            path.unlink()
        return {"installed": False, "removed": existed, "path": str(path)}
    raise HistoryError(f"Unknown schedule action: {action}")
