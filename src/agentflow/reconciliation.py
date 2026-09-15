"""Pure Beads/Herdr lifecycle reconciliation.

The report never treats a pane exit or a closed Bead alone as authenticated
completion.  It identifies divergent state so the root controller can either
consume a valid result or stop for a human decision.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


TERMINAL = frozenset({"closed", "done", "completed", "cancelled", "canceled"})
ACTIVE = frozenset({"launching", "launched", "identity_pending", "running"})


def reconcile(
    descendants: Iterable[Mapping[str, Any]],
    sessions: Mapping[str, Any],
) -> dict[str, Any]:
    issues = {str(item.get("id") or ""): item for item in descendants if str(item.get("id") or "")}
    findings: list[dict[str, Any]] = []
    for task_id, issue in issues.items():
        status = str(issue.get("status") or "").lower()
        record = sessions.get(task_id)
        if status == "in_progress" and not isinstance(record, Mapping):
            findings.append({
                "id": "claim-without-session", "severity": "high", "task": task_id,
                "message": "Bead is in progress but has no Herdr launch record",
            })
            continue
        if not isinstance(record, Mapping):
            continue
        session_status = str(record.get("status") or "")
        has_result = isinstance(record.get("result"), Mapping)
        if status in TERMINAL and session_status in ACTIVE:
            findings.append({
                "id": "terminal-bead-active-session", "severity": "high", "task": task_id,
                "message": f"Bead is terminal while Herdr remains {session_status}",
            })
        elif status not in TERMINAL and has_result:
            findings.append({
                "id": "result-awaiting-disposition", "severity": "medium", "task": task_id,
                "message": "Herdr has a result but the Bead is not terminal",
            })
    for task_id in sorted(set(str(key) for key in sessions) - set(issues)):
        findings.append({
            "id": "session-outside-root", "severity": "info", "task": task_id,
            "message": "Herdr session is not a descendant of this workflow root",
        })
    return {
        "schema": "agentflow.lifecycle-reconciliation@1",
        "ok": not any(item["severity"] == "high" for item in findings),
        "descendants": len(issues),
        "sessions": len(sessions),
        "findings": findings,
    }


__all__ = ["ACTIVE", "TERMINAL", "reconcile"]
