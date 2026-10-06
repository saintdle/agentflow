"""Comparable task-class yield and measurement provenance."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
import math
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
    usage_measurement_authority: str

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
        usage_authority = "provider-authoritative" if sources == {"provider-authoritative"} else "local-estimate"
        buckets.append(YieldBucket(task_class, len(rows), completed, accepted, findings, completed / len(rows), evidence, tuple(sorted(str(row.get("provider") or "unattributed") for row in rows)), "local-estimate", usage_authority))
    return buckets


def report(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    buckets = comparable_yield(records)
    return {
        "task_classes": [bucket.to_dict() for bucket in buckets],
        "comparison": "within identical explicit task_class values only",
        "measurement_note": "Provider dashboard/API quota values are authoritative; task outcomes, timing, file counts, and yield values are local estimates.",
    }


_EVALUATION_DIMENSIONS = ("provider", "model", "effort")
_EVALUATION_METRICS = ("rework_rounds", "unrequested_changes", "human_interventions", "elapsed_seconds", "retries")


def _valid_metric(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _accepted_result_status(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, str) and value in {"accepted", "rejected"}:
        return value
    return "invalid"


def _label(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "<missing>"


def _metric_summary(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for metric in _EVALUATION_METRICS:
        values = [row[metric] for row in rows if _valid_metric(row.get(metric))]
        invalid = sum(metric in row and row[metric] is not None and not _valid_metric(row[metric]) for row in rows)
        summary[metric] = {
            "n": len(values),
            "missing": len(rows) - len(values) - invalid,
            "invalid": invalid,
            "mean": sum(value / len(values) for value in values) if values else None,
        }
    return summary


def evaluation_report(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare explicitly paired evaluation cohorts without crossing dimensions.

    Only records with complete evaluation identity can contribute to paired
    results. Missing identity fields are retained as exclusions so the report
    exposes gaps rather than inferring matches.
    """
    groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    excluded: dict[str, int] = defaultdict(int)
    evaluation_rows = 0
    for row in records:
        evaluation_id = row.get("evaluation_id")
        variant = row.get("variant")
        if not evaluation_id and not variant:
            continue
        evaluation_rows += 1
        required = ("evaluation_id", "variant", "case_id", "task_class", *_EVALUATION_DIMENSIONS)
        missing = [key for key in required if _label(row.get(key)) == "<missing>"]
        if missing:
            excluded["missing:" + ",".join(missing)] += 1
            continue
        key = tuple(_label(row.get(field)) for field in ("evaluation_id", "task_class", *_EVALUATION_DIMENSIONS))
        groups[key].append(row)

    cohorts: list[dict[str, Any]] = []
    for key in sorted(groups):
        rows = groups[key]
        by_variant: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_variant[_label(row.get("variant"))].append(row)
        variants: dict[str, Any] = {}
        for variant in sorted(by_variant):
            variant_rows = by_variant[variant]
            sources = [measurement_authority(str(row.get("source") or "")) for row in variant_rows]
            variants[variant] = {
                "attempts": len(variant_rows),
                "completed": sum(str(row.get("outcome", "")).lower() in {"completed", "passed", "success"} for row in variant_rows),
                "metrics": _metric_summary(variant_rows),
                "usage_measurement_authority": "provider-authoritative" if all(value == "provider-authoritative" for value in sources) else "local-estimate",
                "local_observation_authority": "local-observation",
                "usage_sources": sorted({_label(row.get("source")) for row in variant_rows}),
            }

        paired: list[dict[str, Any]] = []
        unmatched: dict[str, int] = {}
        comparisons = [("baseline", "treatment")] if "baseline" in by_variant or "treatment" in by_variant else []
        for left, right in comparisons:
            left_cases: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            right_cases: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in by_variant.get(left, []):
                left_cases[_label(row.get("case_id"))].append(row)
            for row in by_variant.get(right, []):
                right_cases[_label(row.get("case_id"))].append(row)
            common = set(left_cases) & set(right_cases)
            matched = sorted(case for case in common if len(left_cases[case]) == len(right_cases[case]) == 1)
            unmatched_left = sum(len(left_cases[case]) for case in set(left_cases) - set(right_cases))
            unmatched_right = sum(len(right_cases[case]) for case in set(right_cases) - set(left_cases))
            ambiguous_case_ids = [
                case for case in set(left_cases) | set(right_cases)
                if len(left_cases.get(case, [])) > 1 or len(right_cases.get(case, [])) > 1
            ]
            ambiguous_cases = len(ambiguous_case_ids)
            ambiguous_records = sum(len(left_cases.get(case, [])) + len(right_cases.get(case, [])) for case in ambiguous_case_ids)
            unmatched.update({
                f"{left}_only": unmatched_left,
                f"{right}_only": unmatched_right,
                "ambiguous_duplicate_cases": ambiguous_cases,
                "ambiguous_duplicate_records": ambiguous_records,
            })
            metric_deltas: dict[str, dict[str, Any]] = {}
            for metric in _EVALUATION_METRICS:
                deltas: list[float] = []
                missing_values = 0
                invalid_values = 0
                for case in matched:
                    baseline_value = left_cases[case][0].get(metric)
                    treatment_value = right_cases[case][0].get(metric)
                    if _valid_metric(baseline_value) and _valid_metric(treatment_value):
                        deltas.append(treatment_value - baseline_value)
                    else:
                        missing_values += any(metric not in cases[case][0] or cases[case][0].get(metric) is None for cases in (left_cases, right_cases))
                        invalid_values += any(metric in cases[case][0] and cases[case][0].get(metric) is not None and not _valid_metric(cases[case][0][metric]) for cases in (left_cases, right_cases))
                metric_deltas[metric] = {
                    "n": len(deltas),
                    "missing": missing_values,
                    "invalid": invalid_values,
                    "mean_treatment_minus_baseline": sum(value / len(deltas) for value in deltas) if deltas else None,
                }

            accepted_counts: dict[str, dict[str, int]] = {}
            complete_counts = {left: {"accepted": 0, "rejected": 0}, right: {"accepted": 0, "rejected": 0}}
            accepted_deltas: list[int] = []
            for variant, cases in ((left, left_cases), (right, right_cases)):
                counts = {"accepted": 0, "rejected": 0, "unknown": 0, "invalid": 0}
                for case in matched:
                    counts[_accepted_result_status(cases[case][0].get("accepted_result"))] += 1
                accepted_counts[variant] = counts
            for case in matched:
                baseline_result = _accepted_result_status(left_cases[case][0].get("accepted_result"))
                treatment_result = _accepted_result_status(right_cases[case][0].get("accepted_result"))
                if baseline_result in {"accepted", "rejected"} and treatment_result in {"accepted", "rejected"}:
                    complete_counts[left][baseline_result] += 1
                    complete_counts[right][treatment_result] += 1
                    accepted_deltas.append(int(treatment_result == "accepted") - int(baseline_result == "accepted"))
            paired_accepted = {
                "paired_cases": len(matched),
                "counts": accepted_counts,
                "complete_pair_counts": complete_counts,
                "complete_pairs": len(accepted_deltas),
                "missing_pairs": len(matched) - len(accepted_deltas),
                "accepted_count_delta": complete_counts[right]["accepted"] - complete_counts[left]["accepted"],
                "acceptance_rate_delta": sum(accepted_deltas) / len(accepted_deltas) if accepted_deltas else None,
            }
            paired.append({
                "left": left, "right": right, "matched_cases": len(matched),
                "unmatched_cases": unmatched_left + unmatched_right,
                "ambiguous_duplicate_cases": ambiguous_cases,
                "ambiguous_duplicate_records": ambiguous_records,
                "case_ids": matched, "metric_deltas": metric_deltas,
                "accepted_results": paired_accepted,
            })

        cohorts.append({
            "evaluation_id": key[0], "task_class": key[1],
            "provider": key[2], "model": key[3], "effort": key[4],
            "variants": variants, "comparisons": paired,
            "unmatched_case_counts": unmatched,
            "has_both_variants": "baseline" in variants and "treatment" in variants,
            "comparable": any(item["matched_cases"] for item in paired),
        })
    return {
        "records": evaluation_rows,
        "cohorts": cohorts,
        "excluded": dict(sorted(excluded.items())),
        "note": "Paired comparisons require matching case_id within the same evaluation_id, task_class, provider, model, and effort. Metric deltas are treatment minus baseline; missing and invalid metric values are reported separately and never imputed.",
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
