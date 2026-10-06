"""Deterministic admission policy for controller-led execution.

This module is deliberately provider-neutral.  It evaluates metadata already
present in an approved root and its launch records; it never reads prompts,
provider transcripts, or model reasoning.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping

from agentflow import checkpoint


SCHEMA = "agentflow.execution-policy@1"
EXECUTION_ROLES = frozenset({"coding", "editing", "exploration"})
EXPENSIVE_MODELS = frozenset(
    {
        "gpt-6-sol",
        "gpt-5.6-sol",
        "claude-opus-4-8",
        "claude-opus-4.8",
        "claude-opus-5",
    }
)
SELECTIVE_MODELS = frozenset({"gpt-5.6-terra"})
POLICY_FIELDS = frozenset(
    {
        "controller_only",
        "max_parallel_workers",
        "max_delegation_depth",
        "max_attempts_per_task",
        "launch_budget_multiplier",
        "max_expensive_execution_children",
    }
)


class ExecutionPolicyError(ValueError):
    """An execution policy or launch observation is invalid."""


@dataclasses.dataclass(frozen=True)
class AccountingFinding:
    id: str
    message: str
    recommendation: str


class AccountingIndeterminate(ExecutionPolicyError):
    """A launch history cannot be safely included in or excluded from a budget."""

    def __init__(self, finding: AccountingFinding) -> None:
        super().__init__(finding.message)
        self.finding = finding


@dataclasses.dataclass(frozen=True)
class ExecutionPolicy:
    controller_only: bool = True
    max_parallel_workers: int = 3
    max_delegation_depth: int = 1
    max_attempts_per_task: int = 2
    launch_budget_multiplier: int = 2
    max_expensive_execution_children: int = 0

    def __post_init__(self) -> None:
        for field in (
            "max_parallel_workers",
            "max_delegation_depth",
            "max_attempts_per_task",
            "launch_budget_multiplier",
            "max_expensive_execution_children",
        ):
            value = getattr(self, field)
            minimum = 0 if field in {"max_delegation_depth", "max_expensive_execution_children"} else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ExecutionPolicyError(f"{field} must be an integer of at least {minimum}")
            if field == "max_parallel_workers" and value > checkpoint.MAX_ACTIVE_TASKS:
                raise ExecutionPolicyError(
                    "max_parallel_workers exceeds the maximum supported parallel worker count "
                    f"({checkpoint.MAX_ACTIVE_TASKS})"
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ExecutionPolicy":
        raw = dict(value or {})
        return cls(
            controller_only=bool(raw.get("controller_only", True)),
            max_parallel_workers=int(raw.get("max_parallel_workers", 3)),
            max_delegation_depth=int(raw.get("max_delegation_depth", 1)),
            max_attempts_per_task=int(raw.get("max_attempts_per_task", 2)),
            launch_budget_multiplier=int(raw.get("launch_budget_multiplier", 2)),
            max_expensive_execution_children=int(raw.get("max_expensive_execution_children", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **dataclasses.asdict(self)}

    def root_launch_budget(self, planned_tasks: int) -> int:
        return max(1, planned_tasks) * self.launch_budget_multiplier


@dataclasses.dataclass(frozen=True)
class AdmissionFinding:
    id: str
    message: str
    recommendation: str

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AdmissionReport:
    allowed: bool
    findings: tuple[AdmissionFinding, ...]
    root_launch_budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "agentflow.execution-admission@1",
            "allowed": self.allowed,
            "root_launch_budget": self.root_launch_budget,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def evaluate_launch(
    *,
    policy: ExecutionPolicy,
    model: str,
    role: str,
    task_attempt: int,
    total_attempts: int,
    planned_tasks: int,
    active_workers: int = 0,
    delegation_depth: int = 0,
    selective_model: bool = False,
    expensive_execution_children: int = 0,
) -> AdmissionReport:
    """Return a fail-closed launch admission decision.

    The launch budget is derived from the approved graph size instead of an
    arbitrary global child limit.  A retry consumes the same budget as a first
    launch, making repeated relaunches visible and bounded.
    """

    findings: list[AdmissionFinding] = []
    budget = policy.root_launch_budget(planned_tasks)
    if policy.controller_only and role == "controller":
        findings.append(AdmissionFinding(
            "controller-child-forbidden",
            "controller-only mode does not launch another model controller",
            "Represent controller work as deterministic root state; launch bounded execution or review roles.",
        ))
    if active_workers >= policy.max_parallel_workers:
        findings.append(AdmissionFinding(
            "parallel-budget-exhausted",
            f"{active_workers} workers are already active (limit {policy.max_parallel_workers})",
            "Wait for a current worker result before launching another task.",
        ))
    if delegation_depth > policy.max_delegation_depth:
        findings.append(AdmissionFinding(
            "delegation-depth-exceeded",
            f"delegation depth {delegation_depth} exceeds limit {policy.max_delegation_depth}",
            "Return the work to the root controller or record an explicit policy change.",
        ))
    if task_attempt > policy.max_attempts_per_task:
        findings.append(AdmissionFinding(
            "task-attempt-budget-exhausted",
            f"task attempt {task_attempt} exceeds limit {policy.max_attempts_per_task}",
            "Record a blocker or change approach instead of relaunching the same task.",
        ))
    if total_attempts >= budget:
        findings.append(AdmissionFinding(
            "root-launch-budget-exhausted",
            f"root has consumed {total_attempts} launches (budget {budget})",
            "Add approved graph work or record a typed budget decision before launching again.",
        ))
    if model in SELECTIVE_MODELS and not selective_model:
        findings.append(AdmissionFinding(
            "selective-model-not-approved",
            f"model {model!r} is selective and was not explicitly selected",
            "Persist selective_model=true with the exact launch route and rationale.",
        ))
    if model in EXPENSIVE_MODELS and role in EXECUTION_ROLES:
        allowed_expensive = expensive_execution_children < policy.max_expensive_execution_children
        if not allowed_expensive:
            findings.append(AdmissionFinding(
                "expensive-execution-forbidden",
                f"model {model!r} is not admitted for execution role {role!r}",
                "Use Luna or Sonnet, or raise the root's explicit expensive-execution allowance.",
            ))
    return AdmissionReport(not findings, tuple(findings), budget)


def policy_from_root_metadata(
    value: Any,
    *,
    fallback: Mapping[str, Any] | None = None,
) -> ExecutionPolicy:
    """Load an exact typed root override or the supplied project defaults."""

    if value is None:
        return ExecutionPolicy.from_mapping(fallback)
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ExecutionPolicyError(f"root execution policy must use {SCHEMA}")
    if set(value) != set(POLICY_FIELDS) | {"schema"}:
        raise ExecutionPolicyError(
            "root execution policy must contain the complete typed launch-budget contract"
        )
    return ExecutionPolicy.from_mapping({field: value[field] for field in POLICY_FIELDS})


def summarize_attempts(sessions: Iterable[Mapping[str, Any]]) -> tuple[int, int, int]:
    """Return total attempts, active workers, and expensive execution children."""

    total = active = expensive = 0
    active_states = {"launching", "launched", "identity_pending", "running"}
    for record in sessions:
        total += attempt_count(record)
        if str(record.get("status") or "") in active_states:
            active += 1
        if str(record.get("model") or "") in EXPENSIVE_MODELS and str(record.get("role") or "") in EXECUTION_ROLES:
            expensive += 1
    return total, active, expensive


def attempt_count(record: Mapping[str, Any]) -> int:
    """Count launches while enforcing immutable attempt and launch identities.

    ID-less legacy pending observations can join their resolution. Explicit
    launch IDs may only join the same ID, and numbered history must map each
    attempt number and launch ID one-to-one. Anonymous legacy rows remain
    conservatively additive; numbered history also preserves its highwater.
    """

    raw_attempt = record.get("attempt")
    if raw_attempt in (None, ""):
        record_attempt = 0
    elif isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool) and raw_attempt > 0:
        record_attempt = raw_attempt
    else:
        raise _accounting_indeterminate("record-level launch attempt is malformed")
    record_launch = record.get("launch_id")
    if record_launch not in (None, "") and not isinstance(record_launch, str):
        raise _accounting_indeterminate("record-level launch ID is malformed")
    record_binding = record.get("binding")
    if record_binding is not None and not isinstance(record_binding, Mapping):
        raise _accounting_indeterminate("provider binding is malformed")
    binding_launch = record_binding.get("launch_id") if isinstance(record_binding, Mapping) else None
    if binding_launch not in (None, "") and not isinstance(binding_launch, str):
        raise _accounting_indeterminate("provider binding launch ID is malformed")
    record_launch = str(record_launch or "")
    binding_launch = str(binding_launch or "")
    if record_launch and binding_launch and record_launch != binding_launch:
        raise _accounting_indeterminate(
            "current reservation launch ID conflicts with provider binding"
        )
    reserved_launch = record_launch or binding_launch

    raw_events = record.get("attempts")
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise _accounting_indeterminate("launch attempt history is not a list")

    normalized_events: list[tuple[Mapping[str, Any], str | None, int | None]] = []
    id_for_attempt: dict[int, str] = {}
    attempt_for_id: dict[str, int] = {}
    highwater = record_attempt
    for event in raw_events:
        if not isinstance(event, Mapping):
            raise _accounting_indeterminate("launch attempt history contains a malformed event")
        launch_id = event.get("launch_id")
        if launch_id not in (None, "") and not isinstance(launch_id, str):
            raise _accounting_indeterminate("launch attempt event has a malformed launch ID")
        event_attempt = event.get("attempt")
        if event_attempt in (None, ""):
            event_attempt = None
        elif not isinstance(event_attempt, int) or isinstance(event_attempt, bool) or event_attempt < 1:
            raise _accounting_indeterminate("launch attempt event has a malformed attempt number")

        launch_id = str(launch_id or "") or None
        normalized_events.append((event, launch_id, event_attempt))
        if event_attempt is not None:
            highwater = max(highwater, event_attempt)
        if launch_id and event_attempt is not None:
            previous_id = id_for_attempt.get(event_attempt)
            previous_attempt = attempt_for_id.get(launch_id)
            if previous_id is not None and previous_id != launch_id:
                raise _accounting_indeterminate(
                    f"attempt {event_attempt} is bound to conflicting launch IDs"
                )
            if previous_attempt is not None and previous_attempt != event_attempt:
                raise _accounting_indeterminate(
                    f"launch ID {launch_id!r} is bound to conflicting attempt numbers"
                )
            id_for_attempt[event_attempt] = launch_id
            attempt_for_id[launch_id] = event_attempt

    if reserved_launch and record_attempt:
        previous_id = id_for_attempt.get(record_attempt)
        previous_attempt = attempt_for_id.get(reserved_launch)
        if previous_id is not None and previous_id != reserved_launch:
            raise _accounting_indeterminate(
                f"current reservation conflicts with the launch ID for attempt {record_attempt}"
            )
        if previous_attempt is not None and previous_attempt != record_attempt:
            raise _accounting_indeterminate(
                f"current reservation launch ID {reserved_launch!r} conflicts with history"
            )
        id_for_attempt[record_attempt] = reserved_launch
        attempt_for_id[reserved_launch] = record_attempt

    groups: set[tuple[str, str | int]] = set()
    launch_groups: dict[str, tuple[str, str | int]] = {}
    attempt_groups: dict[int, tuple[str, str | int]] = {}
    # Each pending entry records its normalized group and any explicit ID or
    # attempt so a resolution cannot merge two distinct launch identities.
    pending_events: list[tuple[tuple[str, str | int], str | None, int | None]] = []
    anonymous_index = 0

    for event, launch_id, event_attempt in normalized_events:
        status = str(event.get("status") or "")
        is_resolution = event.get("resolved_from") == "identity_pending"
        pending_index: int | None = None
        if is_resolution and pending_events:
            if event_attempt is None:
                pending_index = len(pending_events) - 1
            else:
                pending_index = next(
                    (index for index in range(len(pending_events) - 1, -1, -1)
                     if pending_events[index][2] == event_attempt),
                    None,
                )

        alias_group: tuple[str, str | int] | None = None
        if pending_index is not None:
            pending_group, pending_launch_id, _pending_attempt = pending_events[pending_index]
            if pending_launch_id and launch_id != pending_launch_id:
                detail = (
                    "omits" if launch_id is None
                    else f"conflicts with {launch_id!r}"
                )
                raise _accounting_indeterminate(
                    f"identity-pending resolution {detail} its explicit launch ID {pending_launch_id!r}"
                )
            # Only an ID-less pending row may acquire an ID during resolution;
            # an explicit ID can never be replaced by another ID.
            alias_group = pending_group
            pending_events.pop(pending_index)
        elif is_resolution and launch_id is None and event_attempt in id_for_attempt:
            raise _accounting_indeterminate(
                "identity-pending resolution omits an explicit launch ID from its attempt"
            )

        if launch_id:
            group = alias_group or launch_groups.get(launch_id) or ("launch", launch_id)
            launch_groups[launch_id] = group
        elif alias_group is not None:
            group = alias_group
        elif event_attempt is not None and event_attempt in attempt_groups:
            if id_for_attempt.get(event_attempt) and status != "identity_pending":
                raise _accounting_indeterminate(
                    f"attempt {event_attempt} has an ID-less non-pending event alongside an explicit launch ID"
                )
            group = attempt_groups[event_attempt]
        elif event_attempt is not None:
            group = ("attempt", event_attempt)
        else:
            anonymous_index += 1
            group = ("anonymous", anonymous_index)

        if event_attempt is not None:
            attempt_groups.setdefault(event_attempt, group)
        groups.add(group)
        if status == "identity_pending":
            pending_events.append((group, launch_id, event_attempt))

    if reserved_launch:
        reserved_group = launch_groups.get(reserved_launch)
        if reserved_group is None and record_attempt:
            # A numbered legacy pending row may be upgraded by the current
            # reservation identity for that exact attempt.
            reserved_group = attempt_groups.get(record_attempt)
            if reserved_group is not None:
                launch_groups[reserved_launch] = reserved_group
        if reserved_group is None:
            reserved_group = ("launch", reserved_launch)
        groups.add(reserved_group)

    return max(highwater, len(groups))


def _accounting_indeterminate(message: str) -> AccountingIndeterminate:
    return AccountingIndeterminate(AccountingFinding(
        "execution-accounting-indeterminate",
        message,
        "Repair or verify the recorded launch history before dispatching another worker.",
    ))


__all__ = [
    "AdmissionFinding",
    "AdmissionReport",
    "AccountingFinding",
    "AccountingIndeterminate",
    "EXECUTION_ROLES",
    "EXPENSIVE_MODELS",
    "POLICY_FIELDS",
    "ExecutionPolicy",
    "ExecutionPolicyError",
    "SCHEMA",
    "SELECTIVE_MODELS",
    "evaluate_launch",
    "attempt_count",
    "policy_from_root_metadata",
    "summarize_attempts",
]
