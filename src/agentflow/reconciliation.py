"""Pure Beads/Herdr lifecycle reconciliation and root completion audit.

The report never treats a pane exit or a closed Bead alone as authenticated
completion.  It identifies divergent state so the root controller can either
consume a valid result or stop for a human decision.
"""

from __future__ import annotations

import hashlib
import json
import re
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


AUDIT_SCHEMA = "agentflow.completion-audit@1"
SUPERSESSION_SCHEMA = "agentflow.ci-supersession@1"
OBSERVED_RESULTS = frozenset({"passed", "failed", "pending", "unavailable"})
_SUPERSESSION_TEXT = (
    "successor", "successor_head", "successor_identity_sha256", "reason",
    "decision_ref", "original_ci_sha256",
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _agentflow(issue: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = issue.get("metadata")
    value = metadata.get("agentflow") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _status(issue: Mapping[str, Any]) -> str:
    return str(issue.get("status") or "").lower()


def _parent(issue: Mapping[str, Any]) -> str:
    for field in ("parent", "parent_id", "root_id", "workflow_root"):
        if field in issue and issue[field] not in (None, ""):
            return str(issue[field])
    return ""


def _observed(ci: Mapping[str, Any]) -> str:
    for field in ("observed_result", "status"):
        if ci.get(field) in OBSERVED_RESULTS:
            return str(ci[field])
    return "unavailable"


def _head(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def ci_successor_error(old: Mapping[str, Any], new: Mapping[str, Any] | None) -> str:
    """Return why *new* cannot supersede the CI result on *old*, or ``""``."""

    old_id = str(old.get("id") or "")
    old_ci = _agentflow(old).get("ci")
    if not isinstance(old_ci, Mapping):
        return f"{old_id} has no recorded CI result"
    old_identity = old_ci.get("identity")
    if (
        not isinstance(old_identity, Mapping) or old_identity.get("gate") != old_id
        or old_identity.get("parent") != old.get("parent") or not _head(old_identity.get("head"))
    ):
        return f"{old_id} has no bound CI identity"
    if old_ci.get("observed_result") == "passed":
        return f"{old_id} already passed; there is nothing to supersede"
    if not isinstance(new, Mapping):
        return "successor is not a descendant of the workflow root"
    new_id = str(new.get("id") or "")
    if new_id == old_id:
        return "a CI gate cannot supersede itself"
    if _status(new) != "closed":
        return f"successor {new_id} is not closed"
    new_ci = _agentflow(new).get("ci")
    if (
        not isinstance(new_ci, Mapping)
        or not new_ci.get("status") == new_ci.get("observed_result") == "passed"
        or new_ci.get("disposition") != "closed" or new_ci.get("obligation") != "accepted"
    ):
        return f"successor {new_id} has no closed, accepted, passed CI receipt"
    new_identity = new_ci.get("identity")
    if (
        not isinstance(new_identity, Mapping) or new_identity.get("gate") != new_id
        or new_identity.get("parent") != new.get("parent")
    ):
        return f"successor {new_id} CI identity does not match the gate"
    if not old_identity.get("repo") or new_identity.get("repo") != old_identity.get("repo"):
        return "successor CI result is for a different repository"
    if not _head(new_identity.get("head")) or new_identity["head"] == old_identity["head"]:
        return "successor CI result must be for a different commit"
    return ""


def _supersession_error(
    issue: Mapping[str, Any], record: Any, ci: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    if not isinstance(record, Mapping) or record.get("schema") != SUPERSESSION_SCHEMA or any(
        not isinstance(record.get(field), str) or not record[field].strip()
        for field in _SUPERSESSION_TEXT
    ):
        return "supersession record is malformed"
    if record["original_ci_sha256"] != digest(ci):
        return "original CI record changed after supersession"
    successor = by_id.get(record["successor"])
    problem = ci_successor_error(issue, successor)
    if problem:
        return problem
    identity = _agentflow(successor or {}).get("ci", {}).get("identity")
    if digest(identity) != record["successor_identity_sha256"] or (
        identity.get("head") != record["successor_head"]
    ):
        return "successor CI identity changed after supersession"
    return ""


def _dependencies(issue: Mapping[str, Any]) -> list[tuple[str, str]]:
    issue_id = str(issue.get("id") or "")
    result = []
    for entry in issue.get("dependencies") or []:
        if isinstance(entry, str):
            result.append((entry, ""))
        elif isinstance(entry, Mapping) and str(entry.get("issue_id") or issue_id) == issue_id:
            target = entry.get("depends_on_id") or entry.get("id")
            kind = str(entry.get("type") or entry.get("dependency_type") or "")
            if target:
                result.append((str(target), kind))
    return result


def audit_completion(enumeration: Mapping[str, Any]) -> dict[str, Any]:
    """Audit one complete root snapshot for unfinished work and unaccepted CI.

    ``enumeration`` is the result of ``beads.audit_root_descendants``. An
    unavailable snapshot yields null counts, never an empty success.
    """

    root_id = str(enumeration.get("root_id") or "")
    root = enumeration.get("root")
    if not enumeration.get("available") or not isinstance(root, Mapping):
        return {
            "schema": AUDIT_SCHEMA, "available": False, "complete": False,
            "root": {"id": root_id, "status": None}, "counts": None,
            "open_descendants": None, "ci": None, "findings": [],
            "error": str(enumeration.get("error") or "Beads enumeration is unavailable"),
        }
    by_id = {
        str(item.get("id")): item for item in enumeration.get("descendants") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    known = {root_id, *by_id}
    findings: list[dict[str, Any]] = []
    open_descendants: list[dict[str, Any]] = []

    def terminal_ancestor(item: Mapping[str, Any]) -> str:
        current, seen = _parent(item), set()
        while current and current not in seen:
            seen.add(current)
            ancestor = root if current == root_id else by_id.get(current)
            if ancestor is None:
                return ""
            if _status(ancestor) in TERMINAL:
                return current
            if current == root_id:
                return ""
            current = _parent(ancestor)
        return ""

    for item_id, item in sorted(by_id.items()):
        status = _status(item)
        if status not in TERMINAL:
            open_descendants.append({"id": item_id, "status": status, "parent": _parent(item)})
            ancestor = terminal_ancestor(item)
            if ancestor:
                findings.append({
                    "id": "open-under-terminal-ancestor", "severity": "high", "task": item_id,
                    "ancestor": ancestor,
                    "message": f"Task is {status or 'unknown'} under terminal ancestor {ancestor}",
                })
    for item in [root, *(by_id[key] for key in sorted(by_id))]:
        for target, kind in _dependencies(item):
            if kind == "parent-child" or target in known:
                continue
            findings.append({
                "id": "outside-root-dependency", "severity": "info", "task": str(item.get("id")),
                "depends_on": target, "type": kind,
                "message": "Dependency is outside this workflow root; reported, never claimed",
            })

    gates: list[dict[str, Any]] = []
    for item_id, item in sorted(by_id.items()):
        agentflow = _agentflow(item)
        ci = agentflow.get("ci")
        if not isinstance(ci, Mapping):
            continue
        observed = _observed(ci)
        identity = ci.get("identity")
        bound = isinstance(identity, Mapping)
        head = identity.get("head") if bound else None
        terminal = _status(item) in TERMINAL
        record = agentflow.get("supersession")
        problem = _supersession_error(item, record, ci, by_id) if record is not None else ""
        valid = record is not None and not problem
        if valid:
            disposition = "superseded" if terminal else "closure_unresolved"
            obligation = "accepted" if terminal else "blocked"
        else:
            disposition = "closed" if terminal else "open"
            if observed == "passed" and terminal:
                obligation = "accepted"
            elif observed == "unavailable" or ci.get("obligation") == "blocked":
                obligation = "blocked"
            else:
                obligation = "required"
        summary = None
        if isinstance(record, Mapping):
            summary = {
                key: record.get(key) for key in (
                    "successor", "successor_head", "reason", "decision_ref", "actor", "recorded_at",
                )
            }
            summary["valid"] = valid
            if problem:
                summary["error"] = problem
        elif record is not None:
            summary = {"valid": False, "error": problem}
        gates.append({
            "id": item_id, "status": _status(item), "observed_result": observed,
            "disposition": disposition, "obligation": obligation, "head": head,
            "bound": bound, "supersession": summary,
        })
        if not bound:
            findings.append({
                "id": "legacy-unbound-ci", "severity": "info", "task": item_id,
                "observed_result": observed,
                "message": "CI record has no bound gate identity",
            })
        if valid:
            findings.append({
                "id": "superseded", "severity": "info", "task": item_id,
                "observed_result": observed, "head": head,
                "successor": record["successor"], "successor_head": record["successor_head"],
                "message": f"{observed} CI result superseded by {record['successor']}",
            })
        elif observed != "passed":
            finding = {
                "id": "ci-result-unaccepted", "severity": "high", "task": item_id,
                "observed_result": observed, "head": head,
                "message": f"CI result is {observed} and has no valid supersession",
            }
            if problem:
                finding["supersession_error"] = problem
            findings.append(finding)

    findings.sort(key=lambda item: (item["task"], item["id"], item.get("depends_on", "")))
    high = sum(item["severity"] == "high" for item in findings)
    return {
        "schema": AUDIT_SCHEMA,
        "available": True,
        "complete": not open_descendants and not high,
        "root": {"id": root_id, "status": _status(root)},
        "counts": {
            "descendants": len(by_id),
            "open": len(open_descendants),
            "terminal": len(by_id) - len(open_descendants),
            "ci_gates": len(gates),
            "superseded": sum(item["disposition"] == "superseded" for item in gates),
            "high_findings": high,
            "info_findings": len(findings) - high,
        },
        "open_descendants": open_descendants,
        "ci": gates,
        "findings": findings,
        "error": "",
    }


__all__ = [
    "ACTIVE", "AUDIT_SCHEMA", "OBSERVED_RESULTS", "SUPERSESSION_SCHEMA", "TERMINAL",
    "audit_completion", "ci_successor_error", "digest", "reconcile",
]
