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
    """Count distinct reserved launches, collapsing events from one launch.

    New events carry a launch ID. Older history often carries only the task
    attempt number, while very old anonymous rows remain conservatively
    additive. The record-level attempt preserves an empty reservation and
    histories whose lifecycle row was not yet appended.
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

    raw_events = record.get("attempts")
    if raw_events is None:
        return record_attempt
    if not isinstance(raw_events, list):
        raise _accounting_indeterminate("launch attempt history is not a list")

    groups: set[tuple[str, str | int]] = set()
    launch_groups: dict[str, tuple[str, str | int]] = {}
    attempt_groups: dict[int, tuple[str, str | int]] = {}
    pending_groups: list[tuple[str, str | int]] = []
    anonymous_index = 0

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

        if launch_id:
            group = launch_groups.setdefault(launch_id, ("launch", launch_id))
            if event_attempt is not None:
                attempt_groups.setdefault(event_attempt, group)
        elif event_attempt is not None and event_attempt in attempt_groups:
            group = attempt_groups[event_attempt]
        elif event_attempt is not None:
            group = ("attempt", event_attempt)
            attempt_groups[event_attempt] = group
        elif event.get("resolved_from") == "identity_pending" and pending_groups:
            group = pending_groups.pop()
        else:
            anonymous_index += 1
            group = ("anonymous", anonymous_index)

        groups.add(group)
        status = str(event.get("status") or "")
        if status == "identity_pending":
            pending_groups.append(group)
        elif event.get("resolved_from") == "identity_pending":
            # A resolution may have an ID while its pending observation did
            # not. Pair it with that prior observation instead of billing a
            # second launch.
            if group in pending_groups:
                pending_groups.remove(group)

    has_reserved_launch = bool(record_launch or binding_launch)
    return max(record_attempt, len(groups), int(has_reserved_launch))


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
