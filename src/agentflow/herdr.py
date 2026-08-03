"""First-class Herdr launch/session/result primitives.

Identity hierarchy (all persisted in HerdrBinding, immutable per launch):
  root → task_id → claim_id → lease_id → launch_id → pane_id → provider/session_id

Invariants:
- Pane state is attention-only: raw pane status is never returned to callers;
  only AttentionRecord values derived from the binding are exposed.
- Restart preserves root/task/claim/lease; issues a fresh launch_id and pane_id.
- A matching structured HerdrResult (same task_id, launch_id, provider,
  session_id) is the sole terminal completion signal. A session that exits
  without ingesting a result is never considered complete.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any, Mapping

from agentflow.attention import AttentionRecord, SessionBinding, derive_state


class HerdrError(RuntimeError):
    """A safe, user-facing Herdr protocol error."""


HERDR_OUTCOMES = ("pending", "completed", "failed", "blocked")

# Internal pane states — never exposed directly to callers.
_PANE_RUNNING = "running"
_PANE_EXITED = "exited"
_PANE_VANISHED = "vanished"


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclasses.dataclass(frozen=True)
class HerdrBinding:
    """Immutable identity record for a single Herdr-managed session.

    claim_id and lease_id survive restart. launch_id and pane_id are fresh
    after each restart. This makes it possible to detect collisions (same
    claim re-registered) and to distinguish a stale binding from a live one
    without inspecting raw process state.
    """

    root: str
    task_id: str
    claim_id: str
    lease_id: str
    launch_id: str
    pane_id: str
    provider: str
    session_id: str
    created_at: str
    launched_at: str = ""

    def __post_init__(self) -> None:
        for field in (
            "root", "task_id", "claim_id", "lease_id",
            "launch_id", "pane_id", "provider", "session_id",
        ):
            if not getattr(self, field):
                raise HerdrError(f"HerdrBinding.{field} must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_session_binding(
        self, *, heartbeat_at: str = "", status: str = "live"
    ) -> SessionBinding:
        """Project identity into an attention-compatible SessionBinding."""
        return SessionBinding(
            bead_id=self.task_id,
            session_id=self.session_id,
            provider=self.provider,
            model="",
            role="writer",
            heartbeat_at=heartbeat_at,
            status=status,
        )


@dataclasses.dataclass(frozen=True)
class HerdrResult:
    """Structured terminal result.

    Only a result whose (task_id, launch_id, provider, session_id) all match
    the current binding is accepted as terminal completion evidence.
    """

    task_id: str
    launch_id: str
    provider: str
    session_id: str
    outcome: str
    evidence: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for field in ("task_id", "launch_id", "provider", "session_id"):
            if not getattr(self, field):
                raise HerdrError(f"HerdrResult.{field} must not be empty")
        if self.outcome not in HERDR_OUTCOMES:
            raise HerdrError(
                f"HerdrResult.outcome must be one of {HERDR_OUTCOMES!r}, got {self.outcome!r}"
            )

    def matches(self, binding: HerdrBinding) -> bool:
        """Return True iff this result's full identity matches *binding*."""
        return (
            self.task_id == binding.task_id
            and self.launch_id == binding.launch_id
            and self.provider == binding.provider
            and self.session_id == binding.session_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "launch_id": self.launch_id,
            "provider": self.provider,
            "session_id": self.session_id,
            "outcome": self.outcome,
            "evidence": [dict(e) for e in self.evidence],
        }


class HerdrSession:
    """Manages Herdr session lifecycle.

    Thread-safety: not thread-safe; callers must synchronise externally.
    """

    def __init__(self) -> None:
        # task_id → current binding
        self._bindings: dict[str, HerdrBinding] = {}
        # task_id → internal pane status (never exposed directly)
        self._pane_status: dict[str, str] = {}
        # task_id → accepted terminal result
        self._results: dict[str, HerdrResult] = {}

    # ------------------------------------------------------------------
    # Launch / restart
    # ------------------------------------------------------------------

    def launch(
        self,
        *,
        root: str,
        task_id: str,
        provider: str,
        session_id: str,
        pane_id: str | None = None,
        claim_id: str | None = None,
        lease_id: str | None = None,
        now: str | None = None,
    ) -> HerdrBinding:
        """Create and register a new HerdrBinding.

        Raises HerdrError on collision (task_id already claimed).
        """
        if task_id in self._bindings:
            existing = self._bindings[task_id]
            raise HerdrError(
                f"collision: task {task_id!r} is already claimed by "
                f"binding {existing.claim_id!r}"
            )
        ts = now or _utcnow()
        binding = HerdrBinding(
            root=root,
            task_id=task_id,
            claim_id=claim_id or _new_id(),
            lease_id=lease_id or _new_id(),
            launch_id=_new_id(),
            pane_id=pane_id or _new_id(),
            provider=provider,
            session_id=session_id,
            created_at=ts,
            launched_at=ts,
        )
        self._bindings[task_id] = binding
        self._pane_status[task_id] = _PANE_RUNNING
        return binding

    def restart(
        self,
        binding: HerdrBinding,
        *,
        new_session_id: str | None = None,
        new_pane_id: str | None = None,
        now: str | None = None,
    ) -> HerdrBinding:
        """Return a new binding preserving root/task/claim/lease; fresh launch_id/pane_id.

        Raises HerdrError when *binding* does not match the registered claim
        (wrong identity or unknown task).
        """
        current = self._bindings.get(binding.task_id)
        if current is None:
            raise HerdrError(
                f"wrong identity: no binding registered for task {binding.task_id!r}"
            )
        if current.claim_id != binding.claim_id or current.lease_id != binding.lease_id:
            raise HerdrError(
                f"wrong identity: claim/lease mismatch for task {binding.task_id!r}"
            )
        ts = now or _utcnow()
        new_binding = HerdrBinding(
            root=binding.root,
            task_id=binding.task_id,
            claim_id=binding.claim_id,   # preserved
            lease_id=binding.lease_id,   # preserved
            launch_id=_new_id(),          # fresh
            pane_id=new_pane_id or _new_id(),
            provider=binding.provider,
            session_id=new_session_id or binding.session_id,
            created_at=binding.created_at,  # preserved
            launched_at=ts,
        )
        self._bindings[binding.task_id] = new_binding
        self._pane_status[binding.task_id] = _PANE_RUNNING
        return new_binding

    # ------------------------------------------------------------------
    # Pane state (attention-only)
    # ------------------------------------------------------------------

    def mark_pane_vanished(self, task_id: str) -> None:
        """Record that the pane no longer exists. Exposed only via pane_attention."""
        if task_id not in self._bindings:
            raise HerdrError(f"no binding for task {task_id!r}")
        self._pane_status[task_id] = _PANE_VANISHED

    def pane_attention(
        self,
        task_id: str,
        *,
        now: dt.datetime | None = None,
        stale_after_seconds: float = 300,
    ) -> AttentionRecord:
        """Return the attention state for this task's pane.

        Raw pane status is never returned. The pane state is encoded as a
        heartbeat age so that derive_state() produces a stable AttentionRecord:
        - running  → fresh heartbeat → "live"
        - vanished → stale heartbeat → "stale"
        - exited without result → stale heartbeat → "stale"
        - exited with result    → fresh heartbeat → "live"
        """
        binding = self._bindings.get(task_id)
        if binding is None:
            raise HerdrError(f"no binding for task {task_id!r}")
        pane_status = self._pane_status.get(task_id, _PANE_RUNNING)
        current = now or dt.datetime.now(dt.timezone.utc)

        if pane_status == _PANE_RUNNING:
            heartbeat_at = current.isoformat()
        elif pane_status == _PANE_EXITED and task_id in self._results:
            heartbeat_at = current.isoformat()
        else:
            # vanished or exited-without-result → force stale
            heartbeat_at = (
                current - dt.timedelta(seconds=stale_after_seconds + 1)
            ).isoformat()

        session_binding = binding.to_session_binding(heartbeat_at=heartbeat_at)
        bead = {"id": task_id}
        return derive_state(
            bead, [session_binding], now=current, stale_after_seconds=stale_after_seconds
        )

    # ------------------------------------------------------------------
    # Result ingestion
    # ------------------------------------------------------------------

    def ingest_result(self, binding: HerdrBinding, result: object) -> bool:
        """Attempt to accept *result* as terminal completion evidence.

        Returns True only when result is a HerdrResult whose full identity
        matches the current binding. A non-matching or wrong-typed value is
        rejected with no side-effects (or raises HerdrError for protocol
        mismatches that require caller attention).
        """
        if not isinstance(result, HerdrResult):
            raise HerdrError(
                "protocol mismatch: result must be a HerdrResult instance"
            )
        if not result.matches(binding):
            return False
        # Guard against stale binding (launch has been superseded by restart)
        current = self._bindings.get(binding.task_id)
        if current is None or current.launch_id != binding.launch_id:
            return False
        self._results[binding.task_id] = result
        self._pane_status[binding.task_id] = _PANE_EXITED
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_complete(self, task_id: str) -> bool:
        """Return True only when a matching result has been ingested."""
        return task_id in self._results

    def get_result(self, task_id: str) -> HerdrResult | None:
        return self._results.get(task_id)

    def get_binding(self, task_id: str) -> HerdrBinding | None:
        return self._bindings.get(task_id)
