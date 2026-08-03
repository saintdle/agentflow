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
