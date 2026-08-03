"""Deterministic attention state derived from explicit bead/session bindings."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Iterable, Mapping


ATTENTION_STATES = (
    "claimed_no_session", "live", "stale", "blocked", "failed_ci", "approval_required", "ambiguous",
)
HERDR_PILOT = "herdr_pilot"


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _as_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else {}


@dataclasses.dataclass(frozen=True)
class SessionBinding:
    bead_id: str
    session_id: str
    provider: str = ""
    model: str = ""
    role: str = ""
    heartbeat_at: str = ""
    status: str = "live"


@dataclasses.dataclass(frozen=True)
class AttentionRecord:
    bead_id: str
    state: str
    session_ids: tuple[str, ...] = ()
    provider: str = ""
    reason: str = ""
    notify: bool = False

    def __post_init__(self) -> None:
        if self.state not in ATTENTION_STATES:
            raise ValueError(f"unsupported attention state: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _bindings_for(bead_id: str, bindings: Iterable[Any]) -> list[Mapping[str, Any]]:
    return [item for item in (_as_dict(binding) for binding in bindings) if item.get("bead_id") == bead_id and item.get("session_id")]


def derive_state(bead: Any, bindings: Iterable[Any], *, now: dt.datetime | None = None, stale_after_seconds: float = 300) -> AttentionRecord:
    """Derive one state using only explicit bead fields and binding records.

    Pane state, process presence, and provider guesses are intentionally not
    consulted. Multiple explicit bindings are ambiguous and therefore never
    collapsed into a misleading ``live`` state.
    """
    item = _as_dict(bead)
    bead_id = str(item.get("bead_id") or item.get("id") or "")
    if not bead_id:
        raise ValueError("bead_id is required")
    matches = _bindings_for(bead_id, bindings)
    explicit_status = str(item.get("attention_state") or item.get("state") or item.get("status") or "").lower()
    if explicit_status in {"blocked", "failed_ci", "approval_required"}:
        state = explicit_status
        reason = "explicit bead state"
    elif len(matches) > 1:
        state = "ambiguous"
        reason = "multiple explicit session bindings"
    elif not matches:
        claimed = bool(item.get("claimed") or item.get("assignee") or item.get("actor") or explicit_status in {"claimed", "claimed_no_session"})
        state = "claimed_no_session" if claimed else "ambiguous"
        reason = "claimed bead has no explicit session binding" if claimed else "no explicit bead-session binding"
    else:
        binding = matches[0]
        binding_status = str(binding.get("status") or "live").lower()
        if binding_status in {"blocked", "failed_ci", "approval_required"}:
            state, reason = binding_status, "explicit session binding state"
        else:
            heartbeat = _parse_time(binding.get("heartbeat_at") or binding.get("last_seen_at"))
            current = now or dt.datetime.now(dt.timezone.utc)
            if heartbeat is None:
                state, reason = "ambiguous", "binding has no parseable heartbeat"
            elif (current - heartbeat).total_seconds() > stale_after_seconds:
                state, reason = "stale", "explicit heartbeat exceeded freshness window"
            else:
                state, reason = "live", "explicit session binding and fresh heartbeat"
    provider = str(matches[0].get("provider") or "") if len(matches) == 1 else ""
    return AttentionRecord(bead_id, state, tuple(str(item["session_id"]) for item in matches), provider, reason)


class AttentionRegistry:
    """In-memory registry; persistence remains the caller's responsibility."""

    def __init__(self, *, stale_after_seconds: float = 300, notifications_enabled: bool = False, herdr_pilot: bool = False) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.notifications_enabled = notifications_enabled
        self.herdr_pilot = herdr_pilot

    @property
    def notification_gate_open(self) -> bool:
        return bool(self.notifications_enabled and self.herdr_pilot)

    def evaluate(self, beads: Iterable[Any], bindings: Iterable[Any], *, now: dt.datetime | None = None) -> list[AttentionRecord]:
        binding_rows = tuple(bindings)
        records = [derive_state(bead, binding_rows, now=now, stale_after_seconds=self.stale_after_seconds) for bead in beads]
        return [dataclasses.replace(record, notify=self.notification_gate_open and record.state != "live") for record in records]

    def notifications(self, records: Iterable[AttentionRecord]) -> list[dict[str, Any]]:
        if not self.notification_gate_open:
            return []
        return [{"bead_id": record.bead_id, "state": record.state, "reason": record.reason} for record in records if record.notify]


def notification_gate(*, enabled: bool, herdr_pilot: bool) -> bool:
    return bool(enabled and herdr_pilot)
