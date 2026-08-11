"""Controller-session progress budgets and transcript-free rotation packets.

This module deliberately measures workflow evidence, not tokens or wall-clock
"time without an edit".  A review can make progress by recording evidence and
an external wait should be owned by a deterministic watcher, so edit-count
heuristics are not safe controller stop conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from typing import Any, Iterable, Mapping


SCHEMA = "agentflow.controller-session@1"
PACKET_SCHEMA = "agentflow.controller-handoff@1"
TASK_CLASSES = frozenset({"coding", "research", "review", "external-wait"})
EVENTS = frozenset(
    {
        "dispatch",
        "edit",
        "check",
        "evidence",
        "finding",
        "decision",
        "watcher",
        "state-change",
        "failure",
        "completed",
    }
)
PROGRESS_EVENTS = {
    "coding": frozenset({"edit", "check", "completed"}),
    "research": frozenset({"evidence", "finding", "decision", "completed"}),
    "review": frozenset({"evidence", "finding", "decision", "completed"}),
    "external-wait": frozenset({"watcher", "state-change", "completed"}),
}


class SessionControlError(ValueError):
    """A session-control event or packet is invalid."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionBudget:
    """Advisory controller-context limits plus one enforced retry limit."""

    rotate_after_completed_tasks: int = 4
    rotate_after_phases: int = 2
    same_approach_failure_limit: int = 2

    def __post_init__(self) -> None:
        if self.rotate_after_completed_tasks < 1:
            raise SessionControlError("rotate_after_completed_tasks must be positive")
        if self.rotate_after_phases < 1:
            raise SessionControlError("rotate_after_phases must be positive")
        if self.same_approach_failure_limit < 1:
            raise SessionControlError("same_approach_failure_limit must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "rotate_after_completed_tasks": self.rotate_after_completed_tasks,
            "rotate_after_phases": self.rotate_after_phases,
            "same_approach_failure_limit": self.same_approach_failure_limit,
        }


def new_ledger(*, budget: SessionBudget | None = None) -> dict[str, Any]:
    policy = budget or SessionBudget()
    return {
        "schema": SCHEMA,
        "started_at": _now(),
        "updated_at": _now(),
        "generation": 1,
        "events": 0,
        "completed_tasks": [],
        "completed_phases": [],
        "approach_failures": {},
        "last_progress": {},
        "blocked": False,
        "block_reason": "",
        "rotation": {"recommended": False, "reasons": []},
        "budget": policy.to_dict(),
    }


def _policy(value: Mapping[str, Any]) -> SessionBudget:
    raw = value.get("budget")
    raw = raw if isinstance(raw, Mapping) else {}
    return SessionBudget(
        rotate_after_completed_tasks=int(raw.get("rotate_after_completed_tasks", 4)),
        rotate_after_phases=int(raw.get("rotate_after_phases", 2)),
        same_approach_failure_limit=int(raw.get("same_approach_failure_limit", 2)),
    )


def record_event(
    ledger: Mapping[str, Any] | None,
    *,
    event: str,
    task_class: str,
    task: str = "",
    phase: str = "",
    approach: str = "",
    evidence: str = "",
    budget: SessionBudget | None = None,
) -> dict[str, Any]:
    """Return an updated durable ledger.

    Rotation thresholds are advisory so an autonomous deterministic controller
    can finish its root.  The same-approach failure limit is a real halt signal:
    callers must persist a blocker/decision instead of silently retrying.
    """

    if event not in EVENTS:
        raise SessionControlError(f"unsupported session event: {event}")
    if task_class not in TASK_CLASSES:
        raise SessionControlError(f"unsupported task class: {task_class}")
    if event == "failure" and not approach.strip():
        raise SessionControlError("failure events require a named --approach")
    current = dict(ledger) if isinstance(ledger, Mapping) and ledger.get("schema") == SCHEMA else new_ledger(budget=budget)
    if budget is not None:
        current["budget"] = budget.to_dict()
    policy = _policy(current)
    current["events"] = int(current.get("events", 0)) + 1
    current["updated_at"] = _now()

    completed_tasks = list(current.get("completed_tasks") or [])
    completed_phases = list(current.get("completed_phases") or [])
    failures = dict(current.get("approach_failures") or {})
    if event == "completed":
        if task and task not in completed_tasks:
            completed_tasks.append(task)
        if phase and phase not in completed_phases:
            completed_phases.append(phase)
    if event == "failure":
        key = approach.strip()
        failures[key] = int(failures.get(key, 0)) + 1
        if failures[key] >= policy.same_approach_failure_limit:
            current["blocked"] = True
            current["block_reason"] = (
                f"approach '{key}' failed {failures[key]} times; record a durable "
                "decision or change approach before continuing"
            )

    current["completed_tasks"] = completed_tasks
    current["completed_phases"] = completed_phases
    current["approach_failures"] = failures
    if event in PROGRESS_EVENTS[task_class]:
        current["last_progress"] = {
            "at": current["updated_at"],
            "event": event,
            "task_class": task_class,
            "task": task,
            "phase": phase,
            "evidence": evidence,
        }

    reasons: list[str] = []
    if len(completed_tasks) >= policy.rotate_after_completed_tasks:
        reasons.append(
            f"{len(completed_tasks)} tasks completed in this controller context "
            f"(budget {policy.rotate_after_completed_tasks})"
        )
    if len(completed_phases) >= policy.rotate_after_phases:
        reasons.append(
            f"{len(completed_phases)} phases completed in this controller context "
            f"(budget {policy.rotate_after_phases})"
        )
    current["rotation"] = {"recommended": bool(reasons), "reasons": reasons}
    return current


def next_generation(ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    """Start a fresh controller-context budget without losing prior counts."""

    previous = dict(ledger or {})
    result = new_ledger(budget=_policy(previous) if previous else None)
    result["generation"] = int(previous.get("generation", 0)) + 1
    result["previous"] = {
        "completed_task_count": len(previous.get("completed_tasks") or []),
        "completed_phase_count": len(previous.get("completed_phases") or []),
        "ended_at": _now(),
    }
    return result


def build_handoff_packet(
    *,
    workspace_root: str,
    workflow_root: str,
    controller: str,
    continuity_id: str,
    checkpoint: Mapping[str, Any] | None,
    ready_tasks: Iterable[Mapping[str, Any]],
    ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a public-safe packet: identifiers and distilled state, no secrets."""

    current = dict(checkpoint or {})
    tasks = []
    for item in ready_tasks:
        tasks.append(
            {
                "id": str(item.get("id") or item.get("task") or ""),
                "status": str(item.get("status") or ""),
                "stage": next(
                    (str(label)[9:] for label in item.get("labels", []) if str(label).startswith("af:stage:")),
                    "",
                ),
            }
        )
    return {
        "schema": PACKET_SCHEMA,
        "created_at": _now(),
        "workspace_root": workspace_root,
        "workflow_root": workflow_root,
        "controller": controller,
        "continuity_id": continuity_id,
        "checkpoint": {
            "task": str(current.get("task") or ""),
            "phase": str(current.get("phase") or ""),
            "state": str(current.get("state") or current.get("status") or "idle"),
            "next_action": str(current.get("next_action") or "inspect durable state"),
        },
        "ready_tasks": tasks,
        "session": {
            "generation": int((ledger or {}).get("generation", 1)),
            "rotation": dict((ledger or {}).get("rotation") or {}),
        },
        "transcript_included": False,
        "instructions": [
            "Resume this exact root; do not create a replacement root or duplicate tasks.",
            "Read Beads, controller state, and linked files instead of prior chat transcripts.",
            "Preserve the root's model policy, permissions, acceptance, and halt conditions.",
        ],
    }


def render_resume_prompt(packet: Mapping[str, Any]) -> str:
    root = str(packet.get("workflow_root") or "")
    workspace = str(packet.get("workspace_root") or "")
    return (
        f"Resume existing Agentflow root {root} in {workspace}. Do not create a new root, "
        "graph, controller identity, or replacement tasks. Inspect its durable Beads, "
        "controller, Herdr, acceptance, and repository state; use linked files and distilled "
        "evidence rather than prior chat transcripts. Reattach safely and continue the "
        "persistent controller until GOAL_COMPLETE or a genuine USER_ACTION_REQUIRED halt. "
        "Preserve the persisted model policy, permissions, budgets, and hosted-action boundaries."
    )


__all__ = [
    "EVENTS",
    "PACKET_SCHEMA",
    "PROGRESS_EVENTS",
    "SCHEMA",
    "TASK_CLASSES",
    "SessionBudget",
    "SessionControlError",
    "build_handoff_packet",
    "new_ledger",
    "next_generation",
    "record_event",
    "render_resume_prompt",
]
