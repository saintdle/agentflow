"""Comparable task-class yield and measurement provenance."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Any, Iterable, Mapping


PROVIDER_AUTHORITATIVE_SOURCES = frozenset({"/status", "/usage", "provider", "dashboard", "api"})


def measurement_authority(source: str) -> str:
    return "provider-authoritative" if source in PROVIDER_AUTHORITATIVE_SOURCES or source.startswith("provider:") else "local-estimate"


@dataclasses.dataclass(frozen=True)
class YieldBucket:
    task_class: str
    attempts: int
    completed: int
    accepted_findings: int
    findings: int
    success_yield: float
    evidence_yield: float | None
    providers: tuple[str, ...]
    measurement: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _number(value: Any, default: int = 0) -> int:
    return value if isinstance(value, (int, float)) and value >= 0 else default


def comparable_yield(records: Iterable[Mapping[str, Any]]) -> list[YieldBucket]:
    """Group only records with the same explicit task class.

    Missing classes are excluded rather than guessed from task names. A result
    is successful only when its explicit outcome is ``completed`` or ``passed``.
    """
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        task_class = record.get("task_class")
        if isinstance(task_class, str) and task_class:
            groups[task_class].append(record)
    buckets: list[YieldBucket] = []
    for task_class in sorted(groups):
        rows = groups[task_class]
        completed = sum(str(row.get("outcome", "")).lower() in {"completed", "passed", "success"} for row in rows)
        findings = sum(_number(row.get("findings")) for row in rows)
        accepted = sum(_number(row.get("accepted_findings")) for row in rows)
        sources = {measurement_authority(str(row.get("source") or "")) for row in rows}
        evidence = accepted / findings if findings else None
        buckets.append(YieldBucket(task_class, len(rows), completed, accepted, findings, completed / len(rows), evidence, tuple(sorted(str(row.get("provider") or "unattributed") for row in rows)), "provider-authoritative" if sources == {"provider-authoritative"} else "local-estimate"))
    return buckets


def report(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    buckets = comparable_yield(records)
    return {
        "task_classes": [bucket.to_dict() for bucket in buckets],
        "comparison": "within identical explicit task_class values only",
        "measurement_note": "Provider dashboard/API values are authoritative; local timing, file, and yield values are estimates.",
    }


def reconcile_codeburn(
    codeburn: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    project: str = "",
    workflow_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile CodeBurn heuristics with Agentflow's explicit delivery evidence.

    CodeBurn's optimize output is intentionally advisory.  Its current JSON
    exposes aggregate finding prose rather than structured per-session delivery
    identities, so Agentflow never claims an exact session match or automatic
    saving.  It adds the durable evidence it *can* prove and prevents destructive
    cleanup of managed workflow assets.
    """

    raw_findings = codeburn.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("CodeBurn report must contain a findings list")
    selected = [dict(row) for row in records if isinstance(row, Mapping)]
    if project:
        selected = [row for row in selected if str(row.get("project") or "") == project]
    completed = [
        row for row in selected
        if str(row.get("outcome") or "").lower() in {"completed", "passed", "success"}
    ]
    checked = [row for row in selected if row.get("checks")]
    accepted_findings = sum(_number(row.get("accepted_findings")) for row in selected)
    files = sum(_number(row.get("files")) for row in selected)

    protected = {"unused-skills", "unused-agents"}
    review = {
        "low-worth-sessions", "context-heavy-sessions", "cost-outliers",
        "retry-heavy-capabilities",
    }
    findings: list[dict[str, Any]] = []
    for item in raw_findings:
        if not isinstance(item, Mapping):
            continue
        identifier = str(item.get("id") or "")
        if identifier in protected:
            disposition = "do-not-auto-apply"
            recommendation = (
                "Keep Agentflow-managed skills and agents unless an explicit configuration "
                "review proves they are unnecessary; unused metadata is not delivery evidence."
            )
        elif identifier in review:
            disposition = "review-with-agentflow-evidence"
            recommendation = (
                "Review the sessions, but do not treat retry counts or missing shell delivery "
                "commands as proof of waste. Compare with Beads, Herdr, checks, and accepted output."
            )
        else:
            disposition = "consider"
            recommendation = "Evaluate this advisory against the affected provider and workflow."
        findings.append(
            {
                "id": identifier,
                "title": str(item.get("title") or ""),
                "severity": str(item.get("severity") or ""),
                "disposition": disposition,
                "recommendation": recommendation,
                "estimated_savings_usd": item.get("estimatedSavingsUSD"),
                "savings_measurement": "third-party heuristic; not verified savings",
            }
        )
    return {
        "schema": "agentflow.codeburn-reconciliation@1",
        "advisory_only": True,
        "project": project,
        "agentflow_evidence": {
            "usage_records": len(selected),
            "completed_records": len(completed),
            "records_with_checks": len(checked),
            "accepted_findings": accepted_findings,
            "files_reported": files,
            "workflow": dict(workflow_evidence or {}),
        },
        "findings": findings,
        "limitations": [
            "CodeBurn optimize JSON does not provide structured session identities for every finding.",
            "Edit-test-edit loops and worker-owned delivery can look retry-heavy to session-local heuristics.",
            "Provider dashboards remain authoritative for billed usage and cost.",
        ],
    }
