"""Immutable original launch baseline, append-only revisions, and reforecast.

A root freezes its approved graph size (N0) and launch cap (L0) once. Later
graph growth, retries, correction tasks, fallback, and context rotation never
change that baseline. Only an append-only revision, tied to a recorded
decision, raises the current cap. Reaching the current cap blocks the next
launch until a reforecast is reviewed and a revision is recorded.

The module is provider-neutral and metadata-only: it never reads prompts,
provider transcripts, or model reasoning.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping

from agentflow.execution import AdmissionFinding


SCHEMA = "agentflow.launch-baseline@1"
REFORECAST_SCHEMA = "agentflow.launch-reforecast@1"
METADATA_KEY = "launch_baseline"
APPROACH_STOP_FAILURES = 2
APPROACH_OUTCOMES = ("failure", "decision")
MAX_TEXT_LENGTH = 512
_BASELINE_FIELDS = frozenset({
    "schema", "frozen_at", "original_tasks", "launch_budget_multiplier",
    "original_launch_budget", "revisions", "approach_events",
})
_REVISION_FIELDS = frozenset({
    "revision", "recorded_at", "decision_ref", "reason",
    "previous_cap", "new_cap", "consumed_at_revision",
})
_EVENT_FIELDS = frozenset({"approach", "outcome", "task", "reference", "recorded_at"})


class LaunchBaselineError(ValueError):
    """A launch baseline, revision, or approach event is invalid."""


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise LaunchBaselineError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise LaunchBaselineError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise LaunchBaselineError(f"{field} is required")
    if text != value or len(text) > MAX_TEXT_LENGTH or any(ord(char) < 32 for char in text):
        raise LaunchBaselineError(
            f"{field} must be trimmed single-line text of at most {MAX_TEXT_LENGTH} characters"
        )
    return text


def _count(value: Any, field: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise LaunchBaselineError(f"{field} must be an integer of at least {minimum}")
    return value


@dataclasses.dataclass(frozen=True)
class Revision:
    revision: int
    recorded_at: str
    decision_ref: str
    reason: str
    previous_cap: int
    new_cap: int
    consumed_at_revision: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ApproachEvent:
    approach: str
    outcome: str
    task: str
    reference: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ApproachState:
    approach: str
    failures: int
    decisions: int
    failures_since_decision: int

    @property
    def blocked(self) -> bool:
        return self.failures_since_decision >= APPROACH_STOP_FAILURES

    def to_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "blocked": self.blocked}


@dataclasses.dataclass(frozen=True)
class LaunchBaseline:
    frozen_at: str
    original_tasks: int
    launch_budget_multiplier: int
    original_launch_budget: int
    revisions: tuple[Revision, ...] = ()
    approach_events: tuple[ApproachEvent, ...] = ()

    @property
    def current_cap(self) -> int:
        return self.revisions[-1].new_cap if self.revisions else self.original_launch_budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "frozen_at": self.frozen_at,
            "original_tasks": self.original_tasks,
            "launch_budget_multiplier": self.launch_budget_multiplier,
            "original_launch_budget": self.original_launch_budget,
            "revisions": [item.to_dict() for item in self.revisions],
            "approach_events": [item.to_dict() for item in self.approach_events],
        }

    def approach_state(self, approach: str) -> ApproachState:
        failures = decisions = since = 0
        for event in self.approach_events:
            if event.approach != approach:
                continue
            if event.outcome == "failure":
                failures += 1
                since += 1
            else:
                decisions += 1
                since = 0
        return ApproachState(approach, failures, decisions, since)

    def approaches(self) -> list[ApproachState]:
        names = sorted({event.approach for event in self.approach_events})
        return [self.approach_state(name) for name in names]


def parse(value: Any) -> LaunchBaseline | None:
    """Return the root's typed baseline, ``None`` when absent, or fail closed."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise LaunchBaselineError(f"launch baseline must use {SCHEMA}")
    if set(value) != _BASELINE_FIELDS:
        raise LaunchBaselineError("launch baseline must contain exactly the typed baseline fields")
    original_tasks = _count(value["original_tasks"], "original_tasks", minimum=1)
    multiplier = _count(value["launch_budget_multiplier"], "launch_budget_multiplier", minimum=1)
    original_budget = _count(value["original_launch_budget"], "original_launch_budget", minimum=1)
    if original_budget != original_tasks * multiplier:
        raise LaunchBaselineError("original_launch_budget must equal original_tasks x multiplier")
    raw_revisions = value["revisions"]
    raw_events = value["approach_events"]
    if not isinstance(raw_revisions, list) or not isinstance(raw_events, list):
        raise LaunchBaselineError("revisions and approach_events must be lists")

    revisions: list[Revision] = []
    cap = original_budget
    consumed_floor = 0
    for index, raw in enumerate(raw_revisions, start=1):
        if not isinstance(raw, Mapping) or set(raw) != _REVISION_FIELDS:
            raise LaunchBaselineError(f"revision {index} is malformed")
        revision = Revision(
            revision=_count(raw["revision"], "revision", minimum=1),
            recorded_at=_text(raw["recorded_at"], "recorded_at"),
            decision_ref=_text(raw["decision_ref"], "decision_ref"),
            reason=_text(raw["reason"], "reason"),
            previous_cap=_count(raw["previous_cap"], "previous_cap", minimum=1),
            new_cap=_count(raw["new_cap"], "new_cap", minimum=1),
            consumed_at_revision=_count(raw["consumed_at_revision"], "consumed_at_revision", minimum=0),
        )
        if revision.revision != index:
            raise LaunchBaselineError("revisions must be numbered consecutively from 1")
        if revision.previous_cap != cap or revision.new_cap <= cap:
            raise LaunchBaselineError(
                f"revision {index} must raise the preceding cap {cap}; history is not append-only"
            )
        if revision.consumed_at_revision < consumed_floor:
            raise LaunchBaselineError(f"revision {index} records lower consumption than an earlier revision")
        revisions.append(revision)
        cap = revision.new_cap
        consumed_floor = revision.consumed_at_revision

    events: list[ApproachEvent] = []
    pending: dict[str, int] = {}
    for index, raw in enumerate(raw_events, start=1):
        if not isinstance(raw, Mapping) or set(raw) != _EVENT_FIELDS:
            raise LaunchBaselineError(f"approach event {index} is malformed")
        outcome = raw["outcome"]
        if outcome not in APPROACH_OUTCOMES:
            raise LaunchBaselineError(f"approach event {index} has unsupported outcome")
        event = ApproachEvent(
            approach=_text(raw["approach"], "approach"),
            outcome=outcome,
            task=_text(raw["task"], "task", required=outcome == "failure"),
            reference=_text(raw["reference"], "reference", required=outcome == "decision"),
            recorded_at=_text(raw["recorded_at"], "recorded_at"),
        )
        if outcome == "decision":
            if pending.get(event.approach, 0) < APPROACH_STOP_FAILURES:
                raise LaunchBaselineError(
                    f"approach event {index} records a decision before the approach was stopped"
                )
            pending[event.approach] = 0
        else:
            pending[event.approach] = pending.get(event.approach, 0) + 1
        events.append(event)

    return LaunchBaseline(
        frozen_at=_text(value["frozen_at"], "frozen_at"),
        original_tasks=original_tasks,
        launch_budget_multiplier=multiplier,
        original_launch_budget=original_budget,
        revisions=tuple(revisions),
        approach_events=tuple(events),
    )


def freeze(*, planned_tasks: int, multiplier: int, now: str) -> LaunchBaseline:
    tasks = _count(planned_tasks, "planned tasks", minimum=1)
    factor = _count(multiplier, "launch_budget_multiplier", minimum=1)
    return LaunchBaseline(
        frozen_at=_text(now, "frozen_at"),
        original_tasks=tasks,
        launch_budget_multiplier=factor,
        original_launch_budget=tasks * factor,
    )


def revise(
    baseline: LaunchBaseline, *, new_cap: int, decision_ref: str, reason: str,
    consumed: int, now: str,
) -> LaunchBaseline:
    """Append one approved cap increase without touching earlier history."""

    cap = _count(new_cap, "new cap", minimum=1)
    used = _count(consumed, "consumed launches", minimum=0)
    if cap <= baseline.current_cap:
        raise LaunchBaselineError(
            f"new cap {cap} must exceed the current cap {baseline.current_cap}"
        )
    floor = baseline.revisions[-1].consumed_at_revision if baseline.revisions else 0
    if used < floor:
        raise LaunchBaselineError(
            f"observed consumption {used} is lower than the last revision's {floor}; "
            "launch history is incomplete"
        )
    revision = Revision(
        revision=len(baseline.revisions) + 1,
        recorded_at=_text(now, "recorded_at"),
        decision_ref=_text(decision_ref, "decision_ref"),
        reason=_text(reason, "reason"),
        previous_cap=baseline.current_cap,
        new_cap=cap,
        consumed_at_revision=used,
    )
    return dataclasses.replace(baseline, revisions=(*baseline.revisions, revision))


def record_approach(
    baseline: LaunchBaseline, *, approach: str, outcome: str, task: str = "",
    reference: str = "", now: str,
) -> LaunchBaseline:
    """Append a failed attempt or the owner decision that unblocks an approach."""

    if outcome not in APPROACH_OUTCOMES:
        raise LaunchBaselineError(f"outcome must be one of {', '.join(APPROACH_OUTCOMES)}")
    event = ApproachEvent(
        approach=_text(approach, "approach"),
        outcome=outcome,
        task=_text(task, "task", required=outcome == "failure"),
        reference=_text(reference, "reference", required=outcome == "decision"),
        recorded_at=_text(now, "recorded_at"),
    )
    if outcome == "decision" and not baseline.approach_state(event.approach).blocked:
        raise LaunchBaselineError(
            f"approach {event.approach!r} is not stopped; a decision is recorded only after "
            f"{APPROACH_STOP_FAILURES} failures"
        )
    return dataclasses.replace(baseline, approach_events=(*baseline.approach_events, event))


def admission_findings(
    baseline: LaunchBaseline, *, approach: str = "",
) -> list[AdmissionFinding]:
    """Return approach stop-rule findings for one proposed launch."""

    if not approach:
        return []
    state = baseline.approach_state(approach)
    if not state.blocked:
        return []
    return [AdmissionFinding(
        "approach-decision-required",
        f"approach {approach!r} has failed {state.failures_since_decision} times since its last "
        "decision; a further attempt needs a recorded owner decision",
        "Record the owner decision with agentflow budget approach --outcome decision, "
        "or change approach, before launching again.",
    )]


def _threshold(consumed: int, cap: int) -> str:
    if consumed < cap:
        return "below"
    return "at" if consumed == cap else "above"


def _dependency_ids(item: Mapping[str, Any]) -> list[str]:
    values = item.get("dependencies")
    result: list[str] = []
    for entry in values if isinstance(values, list) else []:
        if isinstance(entry, Mapping):
            if str(entry.get("type") or entry.get("dependency_type") or "") != "blocks":
                continue
            target = entry.get("depends_on_id") or entry.get("id")
        else:
            target = entry
        if isinstance(target, str) and target:
            result.append(target)
    return result


def _longest_open_chain(open_items: Mapping[str, Mapping[str, Any]]) -> int | None:
    """Return the longest blocking chain among open tasks, or None on a cycle."""

    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(item_id: str) -> int | None:
        if item_id in memo:
            return memo[item_id]
        if item_id in visiting:
            return None
        visiting.add(item_id)
        best = 0
        for dependency in _dependency_ids(open_items[item_id]):
            if dependency not in open_items:
                continue
            value = depth(dependency)
            if value is None:
                return None
            best = max(best, value)
        visiting.discard(item_id)
        memo[item_id] = best + 1
        return memo[item_id]

    longest = 0
    for item_id in sorted(open_items):
        value = depth(item_id)
        if value is None:
            return None
        longest = max(longest, value)
    return longest


def reforecast(
    baseline: LaunchBaseline | None, *, root: str, consumed: int, active: int,
    descendants: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a metadata-only reforecast tied to the original baseline."""

    open_items = {
        str(item.get("id")): item for item in descendants
        if isinstance(item, Mapping) and item.get("id")
        and str(item.get("status") or "") != "closed"
    }
    serial = _longest_open_chain(open_items)
    waits = sorted(
        item_id for item_id, item in open_items.items()
        if str(item.get("status") or "") == "blocked"
        or "af:stage:ci" in (item.get("labels") if isinstance(item.get("labels"), list) else [])
    )
    report: dict[str, Any] = {
        "schema": REFORECAST_SCHEMA,
        "root": root,
        "baseline": "present" if baseline else "absent",
        "cumulative_launches": consumed,
        "active_concurrency": active,
        "remaining_open_tasks": len(open_items),
        "remaining_serial_path": serial,
        "remaining_serial_path_basis": (
            "longest blocking chain among open descendants" if serial is not None
            else "unavailable: blocking dependencies contain a cycle"
        ),
        "external_waits": len(waits),
        "external_wait_tasks": waits,
        "consumption_basis": "managed launch records for this root; retries count as launches",
        "delivery_eta": None,
        "note": "Checkpoint estimates are not delivery ETAs.",
    }
    if baseline is None:
        report.update({
            "next_launch_admitted_by_cap": None,
            "recommendation": (
                "Freeze the approved graph with agentflow budget freeze at execution approval; "
                "until then the cap grows with the graph."
            ),
        })
        return report
    report.update({
        "original_tasks": baseline.original_tasks,
        "original_launch_budget": baseline.original_launch_budget,
        "current_cap": baseline.current_cap,
        "remaining_launches": max(0, baseline.current_cap - consumed),
        "original_threshold": _threshold(consumed, baseline.original_launch_budget),
        "current_threshold": _threshold(consumed, baseline.current_cap),
        "next_launch_admitted_by_cap": consumed < baseline.current_cap,
        "revisions": [item.to_dict() for item in baseline.revisions],
        "approaches": [item.to_dict() for item in baseline.approaches()],
        "recommendation": (
            "Within the current cap." if consumed < baseline.current_cap else
            "Reforecast reached: review remaining work and record an approved revision with "
            "agentflow budget revise before another launch."
        ),
    })
    return report


__all__ = [
    "APPROACH_STOP_FAILURES", "ApproachEvent", "ApproachState", "LaunchBaseline",
    "LaunchBaselineError", "METADATA_KEY", "REFORECAST_SCHEMA", "Revision", "SCHEMA",
    "admission_findings", "freeze", "parse", "record_approach", "reforecast", "revise",
]
