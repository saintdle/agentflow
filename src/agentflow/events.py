"""Private, provider-neutral lifecycle events.

This module is intentionally small and conservative.  It records lifecycle
metadata (never a transcript) and uses a lock around every read/modify/write
operation so several hook processes can safely share one local JSONL spool.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # fcntl is available on the supported local Unix environments.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback (thread safety remains).
    fcntl = None  # type: ignore[assignment]


SCHEMA = "agentflow.lifecycle-event@1"
PRIVACY = "metadata-only"
DEFAULT_MAX_EVENTS = 10_000
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


class EventError(ValueError):
    """Base class for invalid, unsafe, or duplicate event operations."""


class EventPrivacyError(EventError):
    """Raised when an event cannot cross the metadata-only boundary."""


class EventValidationError(EventError):
    """Raised for malformed or unsupported event input."""


class DuplicateEventError(EventError):
    """Raised when an event ID is already present in the spool."""


class SpoolError(OSError):
    """Raised for bounded-spool I/O failures."""


class FailureClass(str, enum.Enum):
    MALFORMED_INPUT = "malformed_input"
    PRIVACY_REJECTED = "privacy_rejected"
    DUPLICATE_EVENT = "duplicate_event"
    STORAGE_FAILURE = "storage_failure"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    UNSUPPORTED_EVENT = "unsupported_event"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    MISSING_CAPABILITY = "missing_capability"
    INVALID_COMMAND = "invalid_command"
    TEST_FAILURE = "test_failure"
    SCHEMA_CONFIG_DRIFT = "schema_config_drift"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


_PROVIDERS = {
    "claude": "claude",
    "claude_code": "claude",
    "anthropic": "claude",
    "codex": "codex",
    "openai": "codex",
    "copilot": "copilot",
    "github_copilot": "copilot",
    "github-copilot": "copilot",
}

_EVENTS = {
    "sessionstart": "session.start",
    "session_start": "session.start",
    "session.start": "session.start",
    "start": "session.start",
    "sessionstop": "session.stop",
    "session_stop": "session.stop",
    "session.stop": "session.stop",
    "sessionend": "session.stop",
    "session_end": "session.stop",
    "session.end": "session.stop",
    "session.shutdown": "session.stop",
    "shutdown": "session.stop",
    "stop": "session.stop",
    "sessionmodelchange": "session.model_change",
    "session_model_change": "session.model_change",
    "session.model_change": "session.model_change",
    "modelchange": "session.model_change",
    "model_change": "session.model_change",
    "sessionpause": "session.pause",
    "session_pause": "session.pause",
    "session.pause": "session.pause",
    "sessionresume": "session.resume",
    "session_resume": "session.resume",
    "session.resume": "session.resume",
    "agentstart": "agent.start",
    "agent_start": "agent.start",
    "agent.start": "agent.start",
    "agentstop": "agent.stop",
    "agent_stop": "agent.stop",
    "agent.stop": "agent.stop",
    "turnstart": "turn.start",
    "turn_start": "turn.start",
    "turn.start": "turn.start",
    "turnstop": "turn.stop",
    "turn_stop": "turn.stop",
    "turn.stop": "turn.stop",
    "error": "session.error",
    "sessionerror": "session.error",
    "session_error": "session.error",
    "session.error": "session.error",
    "userpromptsubmit": "prompt.submit",
    "user_prompt_submit": "prompt.submit",
    "userpromptsubmitted": "prompt.submit",
    "prompt.submit": "prompt.submit",
    "prompt_submit": "prompt.submit",
    "presubmit": "prompt.submit",
    "pretooluse": "tool.start",
    "pre_tool_use": "tool.start",
    "toolstart": "tool.start",
    "tool_start": "tool.start",
    "tool.start": "tool.start",
    "posttooluse": "tool.success",
    "post_tool_use": "tool.success",
    "toolsuccess": "tool.success",
    "tool_success": "tool.success",
    "tool.success": "tool.success",
    "posttoolusefailure": "tool.failure",
    "post_tool_use_failure": "tool.failure",
    "toolfailure": "tool.failure",
    "tool_failure": "tool.failure",
    "tool.failure": "tool.failure",
    "toolcall": "tool.start",
    "tool_call": "tool.start",
    "toolexecutionstart": "tool.start",
    "tool_execution_start": "tool.start",
    "functioncall": "tool.start",
    "function_call": "tool.start",
    "toolexecutionend": "tool.success",
    "tool_execution_end": "tool.success",
    "functionresult": "tool.success",
    "function_result": "tool.success",
    "toolexecutionerror": "tool.failure",
    "tool_execution_error": "tool.failure",
    "functionerror": "tool.failure",
    "function_error": "tool.failure",
    "precompact": "context.compact",
    "pre_compact": "context.compact",
    "postcompact": "context.compact",
    "post_compact": "context.compact",
    "contextcompact": "context.compact",
    "context_compact": "context.compact",
    "context.compact": "context.compact",
    "contextcompaction": "context.compact",
    "context_compaction": "context.compact",
    "compaction": "context.compact",
}

_PROMPT_EVENTS = {
    "prompt", "message", "messages", "assistantmessage", "tool", "tooluse", "tool_use",
}
_UNSAFE_KEY_PARTS = (
    "prompt", "message", "reasoning", "thinking", "transcript", "response",
    "command", "shell", "argv", "credential", "secret", "token", "password",
    "tool_input", "tool_output", "toolinput", "tooloutput", "raw",
)
_SAFE_KEYS = {
    "model", "selectedmodel", "newmodel", "modelid", "model_id", "effort",
    "reasoningeffort", "reasoning_effort", "cwd", "source", "version", "role",
    "status", "phase", "mode", "permissionmode", "permission_mode", "durationms",
    "duration_ms", "exitcode", "exit_code", "reason", "kind", "attempt", "retry",
    "success", "error_class", "failure_class", "workspace", "workspace_scope", "branch",
    "tool", "tool_name", "toolname", "tool_id", "toolid", "tool_type", "tooltype",
    "compaction_id", "compactionid", "tokens_before", "tokensbefore", "tokens_after", "tokensafter",
    "failure_ref", "resolution_ref", "failure_id", "resolution_id", "resolves_failure_ref",
}

_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret[_-]?access[_-]?key)\s*[:=]"
    r"|(?:bearer\s+|(?:sk|ghp|github_pat|glpat|xox[baprs]|npm|pypi)[-_])[A-Za-z0-9._~+/=-]{10,}"
    r"|-----BEGIN [^-]*PRIVATE KEY-----"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_SESSION_HASH_RE = re.compile(r"^sess_[0-9a-f]{64}$")
_WORKSPACE_HASH_RE = re.compile(r"^ws_[0-9a-f]{64}$")
_REFERENCE_HASHES = {
    "failure_ref": re.compile(r"^fail_[0-9a-f]{64}$"),
    "resolution_ref": re.compile(r"^res_[0-9a-f]{64}$"),
}


def _external_id(prefix: str, value: Any, field: str = "identifier") -> str:
    """Canonicalize caller-controlled identifiers without retaining their value."""
    raw = _text(value, field, limit=240)
    if re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", raw):
        return raw
    return prefix + hashlib.sha256(("agentflow." + field + "\0" + raw).encode("utf-8")).hexdigest()


def _event_id(value: Any) -> str:
    """Keep legacy opaque IDs, but hash path/content-like caller IDs."""
    raw = _text(value, "event_id", limit=192)
    if _ID_RE.fullmatch(raw) and not any(part in raw for part in ("/", "\\", "~", "..")):
        return raw
    return _external_id("evt_", raw, "event_id")


def _text(value: Any, field: str, *, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 and c not in "\t" for c in value) or "\n" in value:
        raise EventValidationError(f"{field} is not bounded metadata")
    if _SECRET_RE.search(value):
        raise EventPrivacyError(f"{field} contains credential material")
    return value


def _timestamp(value: Any = None) -> str:
    if value is None or value == "":
        return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    value = _text(value, "timestamp", limit=80)
    try:
        _dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise EventValidationError("timestamp must be ISO-8601") from exc
    return value


def _workspace_scope(value: Any) -> str:
    """Return a stable scope identifier without retaining a filesystem path."""
    raw = _text(value, "workspace_scope", limit=2_000)
    if _WORKSPACE_HASH_RE.fullmatch(raw):
        return raw
    # Rows from the first implementation used an unprefixed digest; recognize
    # it as canonical and add the prefix without hashing it again.
    if re.fullmatch(r"[0-9a-f]{64}", raw):
        return "ws_" + raw
    return "ws_" + hashlib.sha256(("agentflow.workspace.scope\0" + raw).encode("utf-8")).hexdigest()


def _session_scope(value: Any) -> str:
    """Return a stable session identifier that never persists provider IDs."""
    raw = _text(value, "session_id", limit=192)
    if _SESSION_HASH_RE.fullmatch(raw):
        return raw
    return "sess_" + hashlib.sha256(("agentflow.session\0" + raw).encode("utf-8")).hexdigest()


def session_scope(value: Any) -> str:
    """Return the privacy-safe session identity used by lifecycle events."""

    return _session_scope(value)


def _reference(value: Any, field: str, prefix: str) -> str:
    raw = _text(value, field, limit=192)
    pattern = _REFERENCE_HASHES[field]
    if pattern.fullmatch(raw):
        return raw
    return prefix + hashlib.sha256(("agentflow." + field + "\0" + raw).encode("utf-8")).hexdigest()


def _canonical_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise EventValidationError("provider must be a string")
    provider = _PROVIDERS.get(value.strip().lower())
    if provider is None:
        raise EventValidationError("unsupported provider")
    return provider


def _canonical_event(value: Any) -> str:
    if not isinstance(value, str):
        raise EventValidationError("event type must be a string")
    key = re.sub(r"[^a-z0-9_.-]", "", value.strip().lower())
    if key in _PROMPT_EVENTS:
        raise EventPrivacyError("prompt, message, and tool events are not stored")
    event = _EVENTS.get(key)
    if event is None:
        raise EventValidationError("unsupported event type")
    return event


def _lookup(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    for nested_key in ("data", "payload", "hookSpecificOutput"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            for key in keys:
                if key in nested:
                    return nested[key]
    return None


def _metadata(payload: Mapping[str, Any], nested: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    values: list[Mapping[str, Any]] = [payload]
    for nested_key in ("payload", "hookSpecificOutput"):
        nested_value = payload.get(nested_key)
        if isinstance(nested_value, Mapping):
            values.append(nested_value)
    if nested is not None and nested is not payload:
        values.append(nested)
    for source in values:
        for raw_key, raw_value in source.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip().lower()
            if any(part in key for part in _UNSAFE_KEY_PARTS):
                # Explicitly dropped, rather than hashed or truncated.
                continue
            if key not in _SAFE_KEYS or raw_value is None:
                continue
            if key in {"cwd", "workspace", "workspace_scope"}:
                if isinstance(raw_value, str) and raw_value.strip():
                    result["workspace_scope"] = _workspace_scope(raw_value)
                continue
            if key in {"reason", "source"}:
                if isinstance(raw_value, str) and raw_value.strip():
                    result[key] = _external_id(key + "_", raw_value, key)
                continue
            if key in {"tool", "tool_id", "toolid", "tool_name", "toolname"}:
                if isinstance(raw_value, str) and raw_value.strip():
                    result["tool_name" if key in {"tool", "tool_name", "toolname"} else "tool_id"] = _external_id(
                        "tool_", raw_value, "tool_name" if key in {"tool", "tool_name", "toolname"} else "tool_id"
                    )
                # Never let a raw provider alias reach generic assignment.
                continue
            if key in {"failure_class", "error_class"}:
                result["failure_class"] = classify_operational_failure(raw_value)
                continue
            if key in {"failure_ref", "failure_id", "resolves_failure_ref"}:
                if isinstance(raw_value, str) and raw_value.strip():
                    target = "resolves_failure_ref" if key == "resolves_failure_ref" else "failure_ref"
                    result[target] = _reference(raw_value, "failure_ref", "fail_")
                continue
            if key in {"resolution_ref", "resolution_id"}:
                if isinstance(raw_value, str) and raw_value.strip():
                    result["resolution_ref"] = _reference(raw_value, "resolution_ref", "res_")
                continue
            if isinstance(raw_value, bool):
                result[key] = raw_value
            elif isinstance(raw_value, int) and not isinstance(raw_value, bool):
                if -2**31 <= raw_value <= 2**31 - 1:
                    result[key] = raw_value
            elif isinstance(raw_value, str):
                try:
                    result[key] = _text(raw_value, f"metadata.{key}", limit=512)
                except EventPrivacyError:
                    raise
                except EventValidationError:
                    # A malformed optional provider field is not an excuse to
                    # persist its contents; safely omit it.
                    continue
    # Keep the public shape stable for the provider-neutral consumers.
    if "reasoningeffort" in result and "reasoning_effort" not in result:
        result["reasoning_effort"] = result.pop("reasoningeffort")
    if "selectedmodel" in result and "model" not in result:
        result["model"] = result.pop("selectedmodel")
    if "newmodel" in result and "model" not in result:
        result["model"] = result.pop("newmodel")
    aliases = {
        "toolname": "tool_name", "toolid": "tool_id", "tooltype": "tool_type",
        "compactionid": "compaction_id", "tokensbefore": "tokens_before", "tokensafter": "tokens_after",
    }
    for source_key, target_key in aliases.items():
        if source_key in result and target_key not in result:
            result[target_key] = result.pop(source_key)
    return dict(sorted(result.items()))


@dataclasses.dataclass(frozen=True)
class EventEnvelope:
    """Normalized metadata-only lifecycle event."""

    event_id: str
    session_id: str
    sequence: int
    provider: str
    event: str
    timestamp: str
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    privacy: str = PRIVACY
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _event_id(self.event_id))
        object.__setattr__(self, "session_id", _session_scope(self.session_id))
        if not _ID_RE.fullmatch(self.event_id) or not _ID_RE.fullmatch(self.session_id):
            raise EventValidationError("event_id and session_id contain unsafe characters")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise EventValidationError("sequence must be a non-negative integer")
        object.__setattr__(self, "provider", _canonical_provider(self.provider))
        object.__setattr__(self, "event", _canonical_event(self.event))
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        if self.privacy != PRIVACY:
            raise EventPrivacyError("only metadata-only events are supported")
        if not isinstance(self.metadata, Mapping):
            raise EventPrivacyError("metadata must be an object")
        object.__setattr__(self, "metadata", _metadata(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "provider": self.provider,
            "event": self.event,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
            "privacy": self.privacy,
        }

    @property
    def event_type(self) -> str:
        return self.event

    @property
    def id(self) -> str:
        return self.event_id

    @property
    def session(self) -> str:
        return self.session_id

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(value, Mapping):
            raise EventValidationError("event row must be an object")
        if value.get("schema") not in (None, SCHEMA):
            raise EventValidationError("unsupported event schema")
        return cls(
            event_id=value.get("event_id", value.get("eventId", "")),
            session_id=value.get("session_id", value.get("sessionId", "")),
            sequence=value.get("sequence", 0), provider=value.get("provider", ""),
            event=value.get("event", value.get("type", "")), timestamp=value.get("timestamp"),
            metadata=value.get("metadata", {}), privacy=value.get("privacy", PRIVACY),
        )


LifecycleEvent = EventEnvelope


def normalize_event(
    provider: str | Mapping[str, Any], payload: Mapping[str, Any] | None = None, *,
    session_id: str | None = None, event_id: str | None = None, event: str | None = None,
    timestamp: str | None = None, sequence: int = 0, metadata: Mapping[str, Any] | None = None,
) -> EventEnvelope:
    """Normalize Claude, Codex, or Copilot hook payloads.

    Unknown fields are ignored.  Explicit content fields are dropped, while
    prompt, tool, and compaction events retain only safe lifecycle metadata.
    """
    if isinstance(provider, Mapping):
        if payload is not None:
            raise EventValidationError("payload supplied twice")
        payload = provider
        provider = payload.get("provider", "")
    if not isinstance(payload, Mapping):
        raise EventValidationError("payload must be an object")
    provider_name = _canonical_provider(provider)
    nested = payload.get("data") if isinstance(payload.get("data"), Mapping) else None
    raw_event = event if event is not None else _lookup(payload, "event", "hook_event_name", "hookEventName", "type", "name")
    canonical_event = _canonical_event(raw_event)
    raw_session = session_id or _lookup(payload, "session_id", "sessionId", "sessionID", "session")
    if not raw_session:
        # A few native hooks omit session identity.  Keep those events isolated
        # by provider/workspace/event identity rather than sharing one literal
        # bucket; the seed is immediately hashed by _session_scope.
        fallback_seed = _lookup(payload, "cwd", "workspace") or "missing"
        fallback_event_id = _lookup(payload, "event_id", "eventId", "id") or "missing"
        raw_session = "missing:" + provider_name + ":" + hashlib.sha256(
            (str(fallback_seed) + "\0" + str(fallback_event_id)).encode("utf-8")
        ).hexdigest()[:24]
    safe_session = _session_scope(raw_session)
    raw_timestamp = timestamp if timestamp is not None else _lookup(payload, "timestamp", "time", "created_at", "createdAt")
    safe_metadata = _metadata(payload, nested)
    if metadata is not None:
        safe_metadata.update(_metadata(dict(metadata)))
    # Native providers commonly report only an error/status and a tool call
    # identifier.  Derive bounded, deterministic links transiently; the error
    # text itself never crosses the metadata boundary.
    if canonical_event in {"tool.failure", "tool.success"}:
        failure_class = classify_operational_failure(payload)
        if canonical_event == "tool.failure":
            safe_metadata.setdefault("failure_class", failure_class)
        identity = _lookup(payload, "tool_id", "toolId", "tool_call_id", "toolCallId", "call_id", "callId", "tool_name", "toolName")
        if identity is not None:
            identity_text = str(identity).strip()[:240]
            if identity_text:
                failure_ref = _external_id("fail_", f"{provider_name}|{safe_session}|{identity_text}", "tool_failure")
                if canonical_event == "tool.failure":
                    safe_metadata.setdefault("failure_ref", failure_ref)
                else:
                    safe_metadata.setdefault("resolves_failure_ref", failure_ref)
                    safe_metadata.setdefault("resolution_ref", _external_id("res_", failure_ref, "resolution_ref"))
    safe_timestamp = _timestamp(raw_timestamp)
    if event_id is None:
        candidate_id = _lookup(payload, "event_id", "eventId", "id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            event_id = _event_id(candidate_id)
    if event_id is None:
        identity = {"provider": provider_name, "session_id": safe_session, "event": canonical_event, "metadata": safe_metadata}
        event_id = "evt_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    return EventEnvelope(event_id, safe_session, sequence, provider_name, canonical_event, safe_timestamp, safe_metadata)


normalize = normalize_event


_OPERATIONAL_TOKENS: tuple[tuple[FailureClass, tuple[str, ...]], ...] = (
    (FailureClass.TIMEOUT, ("timeout", "timed out", "deadline exceeded")),
    (FailureClass.PERMISSION, ("permission", "forbidden", "access denied", "not authorized", "eacces")),
    (FailureClass.MISSING_CAPABILITY, ("missing capability", "capability_missing", "capability unavailable", "not implemented", "unsupported capability")),
    (FailureClass.INVALID_COMMAND, ("invalid command", "invalid_command", "command not found", "invalid argv", "executable not found", "exit code 127")),
    (FailureClass.TEST_FAILURE, ("test failure", "test_failure", "test_failed", "tests failed", "assertionerror", "assertion failed")),
    (FailureClass.SCHEMA_CONFIG_DRIFT, ("schema drift", "config drift", "configuration drift", "schema/config drift", "schema_config_drift", "schema mismatch", "version mismatch")),
    (FailureClass.RATE_LIMIT, ("rate limit", "rate_limit", "too many requests", "http 429", "status 429", "throttled")),
)


def classify_operational_failure(value: Any) -> str:
    """Classify an operational outcome without retaining its diagnostic text."""
    if isinstance(value, TimeoutError):
        return FailureClass.TIMEOUT.value
    if isinstance(value, PermissionError):
        return FailureClass.PERMISSION.value
    if isinstance(value, NotImplementedError):
        return FailureClass.MISSING_CAPABILITY.value
    if isinstance(value, AssertionError):
        return FailureClass.TEST_FAILURE.value
    parts: list[str] = []
    if isinstance(value, Mapping):
        for key in ("failure_class", "failure", "error_class", "error", "error_message", "message", "detail", "reason", "category", "class", "code", "status", "kind"):
            item = value.get(key)
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, int):
                parts.append(str(item))
        for nested_key in ("data", "payload", "hookSpecificOutput"):
            nested = value.get(nested_key)
            if isinstance(nested, Mapping):
                nested_class = classify_operational_failure(nested)
                if nested_class != FailureClass.UNKNOWN.value:
                    return nested_class
    elif isinstance(value, BaseException):
        parts.extend((type(value).__name__, str(value)))
    elif isinstance(value, str):
        parts.append(value)
    text = " ".join(parts).lower()
    for failure, tokens in _OPERATIONAL_TOKENS:
        if any(token in text for token in tokens):
            return failure.value
    return FailureClass.UNKNOWN.value


def classify_failure(error: Any) -> str:
    """Return a stable class without exposing exception/payload text."""
    operational = classify_operational_failure(error)
    if operational != FailureClass.UNKNOWN.value:
        return operational
    if isinstance(error, DuplicateEventError):
        return FailureClass.DUPLICATE_EVENT.value
    if isinstance(error, EventPrivacyError):
        return FailureClass.PRIVACY_REJECTED.value
    if isinstance(error, SpoolError) or isinstance(error, OSError):
        return FailureClass.STORAGE_FAILURE.value
    if isinstance(error, EventValidationError):
        text = str(error).lower()
        if "provider" in text:
            return FailureClass.UNSUPPORTED_PROVIDER.value
        if "event type" in text or "unsupported event" in text:
            return FailureClass.UNSUPPORTED_EVENT.value
        return FailureClass.MALFORMED_INPUT.value
    return FailureClass.UNKNOWN.value


failure_class = classify_failure
operational_failure_class = classify_operational_failure


def failure_metadata(value: Any) -> dict[str, str]:
    """Return linkable failure metadata with no diagnostic payload."""
    return {"failure_class": classify_operational_failure(value), "privacy": PRIVACY}


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class EventSpool:
    """A bounded, append-only JSONL spool with per-session sequence numbers."""

    def __init__(self, path: Path | str, *, max_events: int = DEFAULT_MAX_EVENTS, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        if not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be positive")
        if not isinstance(max_bytes, int) or max_bytes < 256:
            raise ValueError("max_bytes must be at least 256")
        self.max_events, self.max_bytes = max_events, max_bytes
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> list[EventEnvelope]:
        if not self.path.exists():
            return []
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Read-only or unusual filesystems are handled by the fail-open
            # recording boundary; never copy or expose their contents here.
            pass
        rows: list[EventEnvelope] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                        if isinstance(value, Mapping):
                            if value.get("event_id") or value.get("eventId"):
                                rows.append(EventEnvelope.from_dict(value))
                            else:
                                # Read old agentflow hook rows without
                                # importing their content or mutating the
                                # source file.
                                legacy = dict(value)
                                if not legacy.get("session_id") and not legacy.get("sessionId") and not legacy.get("session"):
                                    legacy["session_id"] = "unknown"
                                rows.append(normalize_event(legacy.get("provider"), legacy))
                    except (json.JSONDecodeError, UnicodeError, EventError, TypeError):
                        # Safe reading: malformed and legacy-unrecognized rows do
                        # not poison the usable portion of a local spool.
                        continue
        except OSError as exc:
            raise SpoolError(str(exc)) from exc
        return rows

    def read(self) -> list[EventEnvelope]:
        with _FileLock(self.lock_path):
            return self._read_unlocked()

    def append(self, event: EventEnvelope | Mapping[str, Any]) -> EventEnvelope:
        if not isinstance(event, EventEnvelope):
            event = EventEnvelope.from_dict(event)
        try:
            with _FileLock(self.lock_path):
                rows = self._read_unlocked()
                if any(row.event_id == event.event_id for row in rows):
                    raise DuplicateEventError(f"event already exists: {event.event_id}")
                next_sequence = max((row.sequence for row in rows if row.session_id == event.session_id), default=0) + 1
                event = dataclasses.replace(event, sequence=next_sequence)
                encoded = (json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                rows.append(event)
                self._prune_unlocked(rows)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                return event
        except DuplicateEventError:
            raise
        except SpoolError:
            raise
        except OSError as exc:
            raise SpoolError("event spool write failed") from exc

    add = append
    record = append

    def record_safely(self, event: EventEnvelope | Mapping[str, Any]) -> bool:
        """Best-effort hook recording; lifecycle execution must never depend on it."""
        try:
            self.append(event)
            return True
        except Exception:  # noqa: BLE001 - this is the explicit fail-open boundary.
            return False

    def _prune_unlocked(self, rows: list[EventEnvelope]) -> None:
        original_count = len(rows)
        while len(rows) > self.max_events:
            rows.pop(0)
        def payload(items: Iterable[EventEnvelope]) -> bytes:
            return b"".join((json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode() for item in items)
        encoded = payload(rows)
        while rows and len(encoded) > self.max_bytes:
            rows.pop(0)
            encoded = payload(rows)
        if self.path.exists() and original_count == len(rows) and self.path.stat().st_size <= self.max_bytes:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def prune(self) -> int:
        with _FileLock(self.lock_path):
            before = len(self._read_unlocked())
            rows = self._read_unlocked()
            self._prune_unlocked(rows)
            return before - len(rows)

    @staticmethod
    def read_legacy(path: Path | str, *, provider: str | None = None) -> list[EventEnvelope]:
        """Safely import the historic hook ``events.jsonl`` shape."""
        result: list[EventEnvelope] = []
        source = Path(path)
        if not source.exists():
            return result
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise SpoolError("legacy event log could not be read") from exc
        for line in lines:
            try:
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    continue
                provider_name = provider or value.get("provider")
                # The historical hook omitted the session when a provider did
                # not supply one.  Preserve that log safely under an explicit
                # synthetic bucket rather than losing the whole file.
                if not value.get("session_id") and not value.get("sessionId") and not value.get("session"):
                    value = dict(value)
                    value["session_id"] = "unknown"
                result.append(normalize_event(provider_name, value))
            except (json.JSONDecodeError, EventError, TypeError):
                continue
        return result

    def import_legacy(self, path: Path | str, *, provider: str | None = None) -> int:
        count = 0
        for event in self.read_legacy(path, provider=provider):
            try:
                self.append(event)
            except DuplicateEventError:
                continue
            count += 1
        return count


BoundedEventSpool = EventSpool


def attested_model(spool: EventSpool, provider: str, raw_session_id: str) -> str:
    """Return the latest locally observed lifecycle model for one exact session.

    This is cooperative hook evidence, not provider-authenticated or
    cryptographically protected proof: a same-UID process can forge or alter
    the local spool. An empty value means no usable observation exists and
    fidelity-sensitive callers must fail closed.
    """

    target = _session_scope(raw_session_id)
    canonical_provider = _canonical_provider(provider)
    model = ""
    for row in spool.read():
        if (
            row.provider == canonical_provider
            and row.session_id == target
            and row.event in {"session.start", "session.model_change"}
        ):
            candidate = str(row.metadata.get("model") or "").strip()
            if candidate:
                model = candidate
    return model


def record_event_safely(spool: EventSpool | Path | str, event: EventEnvelope | Mapping[str, Any]) -> bool:
    """Record metadata without making a provider hook fail closed on I/O."""
    try:
        target = spool if isinstance(spool, EventSpool) else EventSpool(spool)
        return target.record_safely(event)
    except Exception:  # noqa: BLE001 - recording must not break provider hooks.
        return False


__all__ = [
    "SCHEMA", "PRIVACY", "EventError", "EventPrivacyError", "EventValidationError",
    "DuplicateEventError", "SpoolError", "FailureClass", "EventEnvelope", "LifecycleEvent",
    "normalize_event", "normalize", "classify_failure", "failure_class", "classify_operational_failure",
    "operational_failure_class", "failure_metadata", "EventSpool", "BoundedEventSpool", "record_event_safely",
    "session_scope", "attested_model",
]
