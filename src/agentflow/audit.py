"""Metadata-only audit attribution with explicit, non-inferred identity."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from agentflow.privacy import PrivacyError, require_safe_mapping, require_safe_text


UNATTRIBUTED = "unattributed"


def _value(value: Any, field: str) -> str:
    if value is None or value == "":
        return UNATTRIBUTED
    return require_safe_text(value, field, limit=240)


def _timestamp(value: str | None) -> str:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    safe_value = require_safe_text(value, "timestamp", limit=80)
    if safe_value != value:
        raise PrivacyError("timestamp must not have surrounding whitespace")
    parse_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt.datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise PrivacyError("timestamp must be ISO-8601") from exc
    return value


@dataclasses.dataclass(frozen=True)
class AuditEvent:
    event: str
    actor: str = UNATTRIBUTED
    provider: str = UNATTRIBUTED
    model: str = UNATTRIBUTED
    role: str = UNATTRIBUTED
    session: str = UNATTRIBUTED
    evidence: tuple[Mapping[str, Any], ...] = ()
    timestamp: str | None = None
    attribution: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event", require_safe_text(self.event, "event", limit=96))
        for field in ("actor", "provider", "model", "role", "session"):
            object.__setattr__(self, field, _value(getattr(self, field), field))
        safe_evidence: list[Mapping[str, Any]] = []
        for item in self.evidence:
            safe_evidence.append(require_safe_mapping(dict(item), "evidence"))
        object.__setattr__(self, "evidence", tuple(safe_evidence))
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        missing = any(getattr(self, field) == UNATTRIBUTED for field in ("actor", "provider", "model", "role", "session"))
        object.__setattr__(self, "attribution", UNATTRIBUTED if missing else (self.attribution or "attributed"))

    @classmethod
    def create(cls, event: str, *, actor: Any = None, provider: Any = None, model: Any = None, role: Any = None, session: Any = None, evidence: Iterable[Mapping[str, Any]] = (), timestamp: str | None = None) -> "AuditEvent":
        return cls(event, actor or UNATTRIBUTED, provider or UNATTRIBUTED, model or UNATTRIBUTED, role or UNATTRIBUTED, session or UNATTRIBUTED, tuple(evidence), timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "actor": self.actor,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "session": self.session,
            "evidence": [dict(item) for item in self.evidence],
            "attribution": self.attribution,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuditEvent":
        if not isinstance(value, Mapping):
            raise PrivacyError("audit event must be an object")
        return cls.create(
            value.get("event", ""), actor=value.get("actor"), provider=value.get("provider"),
            model=value.get("model"), role=value.get("role"), session=value.get("session"),
            evidence=value.get("evidence") or (), timestamp=value.get("timestamp"),
        )


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None

    def append(self, event: AuditEvent) -> AuditEvent:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
            self.path.chmod(0o600)
        return event

    def read(self) -> list[AuditEvent]:
        if self.path is None or not self.path.exists():
            return []
        events: list[AuditEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(AuditEvent.from_dict(value))
            except (json.JSONDecodeError, PrivacyError):
                continue
        return events


def record_event(event: str, **kwargs: Any) -> dict[str, Any]:
    return AuditEvent.create(event, **kwargs).to_dict()
