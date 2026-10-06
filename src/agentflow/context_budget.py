"""Metadata-only context, lineage, and model-routing audit.

The audit consumes Agentflow history manifest records.  It never opens raw
provider transcripts and its output uses stable archive bead identities rather
than provider session identifiers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Iterable, Mapping

from agentflow import execution


SCHEMA = "agentflow.context-audit@1"


@dataclasses.dataclass(frozen=True)
class ContextThresholds:
    max_children_per_parent: int = 12
    max_delegation_depth: int = 1
    context_pressure_percent: int = 75

    def __post_init__(self) -> None:
        if self.max_children_per_parent < 1:
            raise ValueError("max_children_per_parent must be positive")
        if self.max_delegation_depth < 0:
            raise ValueError("max_delegation_depth must not be negative")
        if not 1 <= self.context_pressure_percent <= 100:
            raise ValueError("context_pressure_percent must be between 1 and 100")


def _timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _role(value: Mapping[str, Any]) -> str:
    raw = str(value.get("role") or "")
    return {
        "worker": "coding",
        "explorer": "exploration",
        "reviewer": "review",
        "agentflow-worker": "coding",
        "agentflow-explorer": "exploration",
        "agentflow-reviewer": "review",
    }.get(raw, raw)


def audit(
    sessions: Iterable[Mapping[str, Any]],
    *,
    days: int = 30,
    now: dt.datetime | None = None,
    thresholds: ContextThresholds | None = None,
) -> dict[str, Any]:
    """Audit sanitized manifest sessions and return deterministic findings."""

    if days < 1:
        raise ValueError("days must be positive")
    limits = thresholds or ContextThresholds()
    current = now or dt.datetime.now(dt.timezone.utc)
    cutoff = current - dt.timedelta(days=days)
    selected: list[Mapping[str, Any]] = []
    for value in sessions:
        when = _timestamp(value.get("ended_at")) or _timestamp(value.get("started_at"))
        if when is not None and when >= cutoff:
            selected.append(value)

    by_source = {
        str(value.get("source_id") or ""): value
        for value in selected
        if str(value.get("source_id") or "")
    }
    child_counts: dict[str, int] = {}
    for value in selected:
        parent = str(value.get("parent_ref") or "")
        if parent:
            child_counts[parent] = child_counts.get(parent, 0) + 1

    findings: list[dict[str, Any]] = []
    model_counts: dict[str, int] = {}
    effort_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    total_tokens = 0
    pressure_basis_counts = {"request_input": 0, "legacy_total_proxy": 0, "unavailable": 0}
    for value in selected:
        bead_id = str(value.get("bead_id") or "unknown")
        raw_models = value.get("models")
        models = [str(item) for item in raw_models if str(item)] if isinstance(raw_models, list) else []
        raw_efforts = value.get("efforts")
        efforts = [str(item) for item in raw_efforts if str(item)] if isinstance(raw_efforts, list) else []
        role = _role(value)
        depth = int(value.get("delegation_depth") or 0)
        for model in models or ["unattributed"]:
            model_counts[model] = model_counts.get(model, 0) + 1
        for effort in efforts or ["unattributed"]:
            effort_counts[effort] = effort_counts.get(effort, 0) + 1
        role_counts[role or "unattributed"] = role_counts.get(role or "unattributed", 0) + 1
        total_tokens += int(value.get("total_tokens") or 0)
        if depth > limits.max_delegation_depth:
            findings.append({
                "id": "delegation-depth-exceeded", "severity": "high", "session": bead_id,
                "message": f"delegation depth {depth} exceeds {limits.max_delegation_depth}",
            })
        for model in models:
            if model in execution.EXPENSIVE_MODELS and role in execution.EXECUTION_ROLES:
                findings.append({
                    "id": "expensive-execution-route", "severity": "high", "session": bead_id,
                    "message": f"{model} ran execution role {role}",
                })
            if model in execution.SELECTIVE_MODELS:
                findings.append({
                    "id": "selective-route-review", "severity": "info", "session": bead_id,
                    "message": f"{model} requires an explicit selective-route record",
                })
            if any(token in model.lower() for token in ("auto", "generic", "haiku")):
                findings.append({
                    "id": "unapproved-model-route", "severity": "high", "session": bead_id,
                    "message": f"model route {model!r} is ambiguous or disallowed",
                })
        raw_window = value.get("context_window_tokens")
        window = raw_window if isinstance(raw_window, int) and not isinstance(raw_window, bool) and raw_window > 0 else 0
        usage = value.get("usage_metadata")
        request_input = (
            usage.get("max_request_input_tokens")
            if isinstance(usage, Mapping)
            and usage.get("availability") in {"complete", "partial"}
            else None
        )
        if isinstance(request_input, int) and not isinstance(request_input, bool) and request_input >= 0:
            pressure_basis = "request_input"
            measured_tokens = request_input
        else:
            raw_peak = value.get("peak_context_tokens")
            if isinstance(raw_peak, int) and not isinstance(raw_peak, bool) and raw_peak > 0:
                pressure_basis = "legacy_total_proxy"
                measured_tokens = raw_peak
            else:
                pressure_basis = "unavailable"
                measured_tokens = 0
        pressure_basis_counts[pressure_basis] += 1
        pressure = round((measured_tokens / window) * 100, 1) if window and measured_tokens else 0.0
        if pressure >= limits.context_pressure_percent:
            findings.append({
                "id": "context-pressure", "severity": "medium", "session": bead_id,
                "basis": pressure_basis,
                "message": (
                    f"maximum request input pressure was {pressure:.1f}%"
                    if pressure_basis == "request_input"
                    else f"legacy input-plus-output proxy pressure was {pressure:.1f}%"
                ),
            })
        parent = str(value.get("source_id") or "")
        children = child_counts.get(parent, 0)
        if children > limits.max_children_per_parent:
            findings.append({
                "id": "child-budget-exceeded", "severity": "high", "session": bead_id,
                "message": f"session launched {children} children (limit {limits.max_children_per_parent})",
            })

    parent_links = sum(bool(str(value.get("parent_ref") or "")) for value in selected)
    linked_parents = sum(
        bool(str(value.get("parent_ref") or "") in by_source) for value in selected
    )
    return {
        "schema": SCHEMA,
        "generated_at": current.isoformat(timespec="seconds"),
        "window_days": days,
        "sessions": len(selected),
        "lineage": {"child_links": parent_links, "linked_parents": linked_parents},
        "models": dict(sorted(model_counts.items())),
        "efforts": dict(sorted(effort_counts.items())),
        "roles": dict(sorted(role_counts.items())),
        "recorded_total_tokens": total_tokens,
        "context_pressure_basis": pressure_basis_counts,
        "findings": findings,
        "finding_counts": {
            severity: sum(item["severity"] == severity for item in findings)
            for severity in ("high", "medium", "info")
        },
        "privacy": {
            "source": "Agentflow metadata-only history manifest",
            "prompt_content_read": False,
            "transcript_content_read": False,
        },
        "measurement_note": (
            "Token counters are provider-recorded metadata when available; provider usage "
            "dashboards remain authoritative for billed usage and cost. Context pressure uses "
            "maximum request input when available; legacy peak_context_tokens is an "
            "input-plus-output proxy. Aggregate session totals are not occupancy."
        ),
    }


def add_execution_attempts(
    report: Mapping[str, Any],
    sessions: Mapping[str, Any],
    *,
    policy: execution.ExecutionPolicy,
) -> dict[str, Any]:
    """Attach Herdr attempt metadata without reading provider output."""

    result = dict(report)
    findings = list(result.get("findings") or [])
    values = [value for value in sessions.values() if isinstance(value, Mapping)]
    accounting_errors: dict[str, execution.AccountingFinding] = {}
    try:
        total, active, expensive = execution.summarize_attempts(values)
    except execution.AccountingIndeterminate:
        total = active = expensive = None
        for task_id, record in sessions.items():
            if not isinstance(record, Mapping):
                continue
            try:
                execution.attempt_count(record)
            except execution.AccountingIndeterminate as exc:
                accounting_errors[str(task_id)] = exc.finding
                findings.append({
                    "id": exc.finding.id, "severity": "high",
                    "session": str(task_id), "message": exc.finding.message,
                    "recommendation": exc.finding.recommendation,
                })
    over_budget: list[dict[str, Any]] = []
    for task_id, record in sessions.items():
        if not isinstance(record, Mapping):
            continue
        if str(task_id) in accounting_errors:
            continue
        count = execution.attempt_count(record)
        if count > policy.max_attempts_per_task:
            finding = {
                "id": "task-attempt-budget-exceeded", "severity": "high",
                "session": str(task_id),
                "message": f"task recorded {count} launches (limit {policy.max_attempts_per_task})",
            }
            over_budget.append(finding)
            findings.append(finding)
    result["findings"] = findings
    result["finding_counts"] = {
        severity: sum(item.get("severity") == severity for item in findings)
        for severity in ("high", "medium", "info")
    }
    result["execution"] = {
        "source": "Agentflow Herdr metadata",
        "recorded_attempts": total,
        "active_workers": active,
        "expensive_execution_children": expensive,
        "tasks_over_attempt_budget": len(over_budget),
        "accounting_indeterminate": bool(accounting_errors),
        "indeterminate_tasks": sorted(accounting_errors),
    }
    return result


def compaction_guidance(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build an original, transcript-free strategic compaction recommendation."""

    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    reasons = sorted({
        str(item.get("id")) for item in findings
        if isinstance(item, Mapping) and item.get("id") in {
            "context-pressure", "child-budget-exceeded", "expensive-execution-route"
        }
    })
    return {
        "schema": "agentflow.strategic-compaction@1",
        "recommended": bool(reasons),
        "reasons": reasons,
        "actions": [
            "Persist the current task, decisions, checks, blockers, and next action in Beads.",
            "Finish or disposition the current worker result; do not rotate mid-write.",
            "Run agentflow controller rotate at the next safe phase boundary.",
            "Resume the exact root from its protected handoff packet without copying transcripts.",
        ] if reasons else [],
        "transcript_required": False,
    }


__all__ = ["ContextThresholds", "SCHEMA", "add_execution_attempts", "audit", "compaction_guidance"]
