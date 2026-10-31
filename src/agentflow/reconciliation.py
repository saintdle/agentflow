"""Pure Beads/Herdr lifecycle reconciliation and root completion audit.

The report never treats a pane exit or a closed Bead alone as authenticated
completion.  It identifies divergent state so the root controller can either
consume a valid result or stop for a human decision.
"""

from __future__ import annotations

from datetime import datetime
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
UNPASSED_BUCKETS = frozenset({"fail", "cancel", "pending"})
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


def is_native_claimed(issue: Mapping[str, Any]) -> bool:
    """Return whether a Bead is in the native lane's claimed state."""
    return _status(issue) == "in_progress"


def _parent(issue: Mapping[str, Any]) -> str:
    for field in ("parent", "parent_id", "root_id", "workflow_root"):
        if field in issue and issue[field] not in (None, ""):
            return str(issue[field])
    return ""


def _observed(ci: Mapping[str, Any]) -> str:
    for field in ("observed_result", "status"):
        value = ci.get(field)
        if isinstance(value, str) and value in OBSERVED_RESULTS:
            return value
    return "unavailable"


def _head(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def ci_identity_error(issue: Mapping[str, Any]) -> str:
    """Return why *issue* lacks a stored CI identity bound to it, or ``""``."""

    issue_id = str(issue.get("id") or "")
    ci = _agentflow(issue).get("ci")
    if not isinstance(ci, Mapping):
        return f"{issue_id} has no recorded CI result"
    identity = ci.get("identity")
    if not (
        isinstance(identity, Mapping) and identity.get("gate") == issue_id
        and isinstance(identity.get("parent"), str) and identity["parent"]
        and identity["parent"] == issue.get("parent")
        and isinstance(identity.get("repo"), str) and identity["repo"]
        and type(identity.get("pr")) is int and _head(identity.get("head"))
        and type(identity.get("required_only")) is bool
        and isinstance(identity.get("check_set_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", identity["check_set_sha256"]) is not None
    ):
        return f"{issue_id} has no bound CI identity"
    return ""


def passed_ci_receipt_error(issue: Mapping[str, Any]) -> str:
    """Return why *issue* lacks a stored receipt of an accepted CI pass, or ``""``.

    Mirrors what ``ci watch`` persists for a pass: a bound identity, non-empty
    checks with no failing, cancelled or pending bucket, and a check set whose
    digest is the identity's ``check_set_sha256``.
    """

    issue_id = str(issue.get("id") or "")
    problem = ci_identity_error(issue)
    if problem:
        return problem
    ci = _agentflow(issue)["ci"]
    checks = ci.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, Mapping) or not isinstance(check.get("bucket"), str)
        or not check["bucket"]
        for check in checks
    ):
        return f"{issue_id} has no stored check evidence"
    if any(check["bucket"] in UNPASSED_BUCKETS for check in checks):
        return f"{issue_id} stored checks include a failing, cancelled or pending check"
    if digest(checks) != ci["identity"]["check_set_sha256"]:
        return f"{issue_id} stored checks do not match its CI identity"
    if (
        not ci.get("status") == ci.get("observed_result") == "passed"
        or ci.get("disposition") != "closed" or ci.get("obligation") != "accepted"
    ):
        return f"{issue_id} has no closed, accepted, passed CI receipt"
    return ""


def ci_successor_error(old: Mapping[str, Any], new: Mapping[str, Any] | None) -> str:
    """Return why *new* cannot supersede the CI result on *old*, or ``""``."""

    old_id = str(old.get("id") or "")
    problem = ci_identity_error(old)
    if problem:
        return problem
    old_ci = _agentflow(old)["ci"]
    old_identity = old_ci["identity"]
    if _observed(old_ci) == "passed":
        return f"{old_id} already passed; there is nothing to supersede"
    if not isinstance(new, Mapping):
        return "successor is not a descendant of the workflow root"
    new_id = str(new.get("id") or "")
    if new_id == old_id:
        return "a CI gate cannot supersede itself"
    if _status(new) != "closed":
        return f"successor {new_id} is not closed"
    problem = passed_ci_receipt_error(new)
    if problem:
        return f"successor {problem}"
    new_identity = _agentflow(new)["ci"]["identity"]
    if new_identity["repo"] != old_identity["repo"]:
        return "successor CI result is for a different repository"
    if new_identity["head"] == old_identity["head"]:
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
        if disposition == "superseded":
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


NATIVE_VISIBILITY_SCHEMA = "agentflow.native-visibility@1"
NATIVE_OBSERVATION_SCHEMA = "agentflow.native-observation@1"
NATIVE_AUTHORITY_SCHEMA = "agentflow.native-return-authority@1"
NATIVE_RESULT_SCHEMA = "agentflow.native-return@1"
NATIVE_RECEIPT_SCHEMA = "agentflow.native-return-receipt@1"
NATIVE_OBSERVATION_SOURCE = "exact-session-query"
NATIVE_OBSERVATION_STATES = frozenset({"present", "absent", "unavailable"})
NATIVE_STALE_AFTER_SECONDS = 300
NATIVE_VISIBILITY_STATES = ("confirmed_native", "stale", "unobservable", "absent")


def _observed_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _native_task(
    task_id: str, observation: Any, generation: Any, now: datetime,
) -> dict[str, Any]:
    entry = {
        "task_id": task_id, "claim": "claimed", "observation": "missing",
        "state": "unobservable", "reason": "", "provider": "", "session_id": "", "observed_at": "",
    }

    def unobservable(kind: str, reason: str) -> dict[str, Any]:
        entry.update(observation=kind, state="unobservable", reason=reason)
        return entry

    if observation is None:
        return unobservable("missing", "no exact native session observation is recorded")
    if not isinstance(observation, Mapping):
        return unobservable("invalid", "native observation is malformed")
    for field in ("provider", "session_id", "observed_at"):
        if isinstance(observation.get(field), str):
            entry[field] = observation[field]
    state = observation.get("state")
    if (
        observation.get("schema") != NATIVE_OBSERVATION_SCHEMA
        or state not in NATIVE_OBSERVATION_STATES
        or any(
            not isinstance(observation.get(field), str)
            for field in ("provider", "session_id", "observer", "claim_generation")
        )
    ):
        return unobservable("invalid", "native observation is malformed")
    if observation.get("source") != NATIVE_OBSERVATION_SOURCE:
        return unobservable("invalid", "observation did not come from an exact session query")
    observed = _observed_time(observation.get("observed_at"))
    if observed is None:
        return unobservable("invalid", "observation time is unparseable")
    if not isinstance(generation, str) or not generation:
        return unobservable("mismatched", "current claim generation is unavailable")
    if observation["claim_generation"] != generation:
        return unobservable("mismatched", "observation is bound to a different claim generation")
    if state == "unavailable":
        return unobservable("unavailable", "exact session query was unavailable")
    required = ("session_id", "observer") if state == "absent" else ("provider", "session_id", "observer")
    if any(not observation[field].strip() for field in required):
        return unobservable("invalid", "observation lacks a provider, session or observer")
    if state == "absent":
        entry.update(observation="absent", state="absent",
                     reason="exact session query reported the session absent")
        return entry
    age = (now - observed).total_seconds()
    if age < 0:
        return unobservable("invalid", "observation time is in the future")
    entry["observation"] = "present"
    if age > NATIVE_STALE_AFTER_SECONDS:
        entry.update(
            state="stale",
            reason=f"observation is older than {NATIVE_STALE_AFTER_SECONDS} seconds",
        )
    else:
        entry.update(state="confirmed_native", reason="exact session query observed the session")
    return entry


def classify_native_visibility(
    enumeration: Mapping[str, Any],
    sessions: Mapping[str, Any] | None,
    claim_generations: Mapping[str, str | None],
    *,
    now: datetime,
    sessions_error: str = "",
) -> dict[str, Any]:
    """Classify claimed native tasks without a Herdr record from recorded observations.

    ``enumeration`` is the ``beads.audit_root_descendants`` snapshot,
    ``sessions`` the Herdr session mapping (``None`` when unreadable) and
    ``claim_generations`` each candidate's current claim generation (``None``
    when unavailable). Visibility is a report only: it never authorizes a
    result, a claim change or managed recovery.
    """

    report: dict[str, Any] = {
        "schema": NATIVE_VISIBILITY_SCHEMA,
        "stale_after_seconds": NATIVE_STALE_AFTER_SECONDS,
    }
    error = ""
    if not enumeration.get("available") or not isinstance(enumeration.get("root"), Mapping):
        error = str(enumeration.get("error") or "Beads enumeration is unavailable")
    elif not isinstance(sessions, Mapping):
        error = sessions_error or "Herdr session state is unavailable"
    if error:
        return {**report, "available": False, "error": error, "counts": None,
                "tasks": [], "managed_excluded": []}
    tasks: list[dict[str, Any]] = []
    managed: list[str] = []
    rows = sorted(
        (item for item in enumeration.get("descendants") or []
         if isinstance(item, Mapping) and item.get("id")),
        key=lambda item: str(item["id"]),
    )
    for item in rows:
        task_id = str(item["id"])
        if not is_native_claimed(item):
            continue
        if task_id in sessions:
            managed.append(task_id)
            continue
        tasks.append(_native_task(
            task_id, _agentflow(item).get("native_observation"),
            claim_generations.get(task_id), now,
        ))
    return {
        **report, "available": True,
        "counts": {state: sum(item["state"] == state for item in tasks)
                   for state in NATIVE_VISIBILITY_STATES},
        "tasks": tasks, "managed_excluded": managed,
    }


__all__ = [
    "ACTIVE", "AUDIT_SCHEMA", "NATIVE_AUTHORITY_SCHEMA", "NATIVE_OBSERVATION_SCHEMA",
    "NATIVE_OBSERVATION_SOURCE", "NATIVE_OBSERVATION_STATES", "NATIVE_RECEIPT_SCHEMA",
    "NATIVE_RESULT_SCHEMA", "NATIVE_STALE_AFTER_SECONDS", "NATIVE_VISIBILITY_SCHEMA",
    "NATIVE_VISIBILITY_STATES", "OBSERVED_RESULTS", "SUPERSESSION_SCHEMA", "TERMINAL",
    "UNPASSED_BUCKETS", "audit_completion", "ci_identity_error", "ci_successor_error",
    "classify_native_visibility", "digest", "passed_ci_receipt_error", "reconcile",
    "is_native_claimed",
]
