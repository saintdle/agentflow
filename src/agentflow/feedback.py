"""Deterministic intake and disposition tracking for human feedback.

Feedback text and imported fields are data only.  This module never interprets
them as approval, commands, or changes to Agentflow policy.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "agentflow.feedback@1"
DISPOSITIONS = (
    "accepted",
    "rejected-factual",
    "rejected-preference",
    "deferred",
    "duplicate",
    "superseded",
    "fixed",
)
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ITEM_ID = re.compile(r"^fb-(?:[0-9a-f]{20}|[A-Za-z0-9][A-Za-z0-9._:-]{0,63})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FeedbackError(ValueError):
    """Raised when feedback data or a requested disposition is invalid."""


def empty_ledger(task_id: str) -> dict[str, Any]:
    if not task_id:
        raise FeedbackError("task ID is required")
    return {"schema": SCHEMA, "task_id": task_id, "items": []}


def validate_ledger(value: Any, *, task_id: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["feedback ledger must be an object"]
    if value.get("schema") != SCHEMA:
        errors.append(f"feedback schema must be {SCHEMA}")
    if value.get("task_id") != task_id:
        errors.append("feedback ledger task_id does not match the Bead")
    items = value.get("items")
    if not isinstance(items, list):
        return errors + ["feedback items must be a list"]
    seen: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append(f"feedback item {index} must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            errors.append(f"feedback item {index} has an invalid ID")
        elif item_id in seen:
            errors.append(f"duplicate feedback ID: {item_id}")
        else:
            seen.add(item_id)
        if not isinstance(item.get("source"), str) or not item.get("source"):
            errors.append(f"feedback item {item_id or index} requires source")
        if item.get("task_id") != task_id:
            errors.append(f"feedback item {item_id or index} task_id does not match the Bead")
        if not isinstance(item.get("text"), str) or not item.get("text").strip():
            errors.append(f"feedback item {item_id or index} requires text")
        if isinstance(item.get("text"), str) and len(item["text"].encode("utf-8")) > 8_000:
            errors.append(f"feedback item {item_id or index} text is too large")
        if isinstance(item.get("source"), str) and len(item["source"].encode("utf-8")) > 500:
            errors.append(f"feedback item {item_id or index} source is too large")
        source_ref = item.get("source_ref", "")
        if not isinstance(source_ref, str) or len(source_ref.encode("utf-8")) > 2_000:
            errors.append(f"feedback item {item_id or index} source_ref is invalid or too large")
        artifacts = item.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            errors.append(f"feedback item {item_id or index} artifacts must be a list")
        else:
            for artifact in artifacts:
                if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                    errors.append(f"feedback item {item_id or index} has invalid artifact metadata")
                    continue
                digest = artifact.get("sha256")
                if digest is not None and not _SHA256.fullmatch(str(digest)):
                    errors.append(f"feedback item {item_id or index} has invalid artifact digest")
        dispositions = item.get("dispositions", [])
        if not isinstance(dispositions, list):
            errors.append(f"feedback item {item_id or index} dispositions must be a list")
            continue
        for disposition in dispositions:
            if not isinstance(disposition, dict) or disposition.get("status") not in DISPOSITIONS:
                errors.append(f"feedback item {item_id or index} has an invalid disposition")
                continue
            if not isinstance(disposition.get("by"), str) or not disposition.get("by"):
                errors.append(f"feedback item {item_id or index} disposition requires by")
                continue
            status = disposition["status"]
            if status in {"accepted", "fixed"} and not disposition.get("acceptance_id"):
                errors.append(f"feedback item {item_id or index} {status} disposition requires acceptance_id")
            if status in {"rejected-factual", "rejected-preference", "deferred"} and not str(disposition.get("note") or "").strip():
                errors.append(f"feedback item {item_id or index} {status} disposition requires a reason")
            if status in {"duplicate", "superseded"} and not disposition.get("related_id"):
                errors.append(f"feedback item {item_id or index} {status} disposition requires related_id")
            if status == "fixed":
                resolution = disposition.get("resolution")
                if not isinstance(resolution, Mapping):
                    errors.append(f"feedback item {item_id or index} fixed disposition requires a verification snapshot")
                elif (
                    not resolution.get("actual_evidence")
                    or not isinstance(resolution.get("artifacts"), list)
                    or not isinstance(resolution.get("evidence_artifacts"), list)
                ):
                    errors.append(f"feedback item {item_id or index} fixed verification snapshot is incomplete")
    return errors


def _validated_key(value: str) -> str:
    if not _KEY.fullmatch(value):
        raise FeedbackError("explicit feedback key must be 1-64 letters, digits, '.', '_', ':', or '-' and start with a letter or digit")
    return value


def _safe_relative_artifact(root: Path, raw_path: str) -> tuple[str, Path]:
    candidate_path = Path(raw_path)
    if not raw_path or candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise FeedbackError(f"artifact path must be relative to the workspace: {raw_path!r}")
    relative = candidate_path.as_posix()
    if relative in {"", "."}:
        raise FeedbackError("artifact path must name a file")
    workspace = root.resolve()
    candidate = (workspace / candidate_path).resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise FeedbackError(f"artifact path escapes the workspace: {raw_path!r}") from exc
    if candidate.exists() and not candidate.is_file():
        raise FeedbackError(f"artifact is not a regular file: {raw_path!r}")
    return relative, candidate


def _snapshot_artifacts(root: Path, paths: Sequence[str], *, require_present: bool) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths:
        relative, path = _safe_relative_artifact(root, str(raw_path))
        if relative in seen:
            continue
        seen.add(relative)
        if not path.is_file():
            if require_present:
                raise FeedbackError(f"artifact is missing at verification time: {relative}")
            snapshots.append({"path": relative, "sha256": None, "exists": False})
            continue
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                raise FeedbackError(f"artifact exceeds the 64 MB limit: {relative}")
            digest = _file_digest(path)
        except FeedbackError:
            raise
        except OSError as exc:
            raise FeedbackError(f"cannot read artifact {relative}: {exc}") from exc
        snapshots.append({"path": relative, "sha256": digest, "exists": True})
    return sorted(snapshots, key=lambda item: item["path"])


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def intake_item(
    ledger: Mapping[str, Any],
    *,
    task_id: str,
    root: Path,
    source: str,
    text: str,
    source_ref: str = "",
    key: str = "",
    artifacts: Sequence[str] = (),
    timestamp: str = "",
) -> tuple[dict[str, Any], str, bool]:
    errors = validate_ledger(dict(ledger), task_id=task_id)
    if errors:
        raise FeedbackError("; ".join(errors))
    if not source.strip() or not text.strip():
        raise FeedbackError("source and feedback text are required")
    if len(source.encode("utf-8")) > 500 or len(text.encode("utf-8")) > 8_000:
        raise FeedbackError("source and feedback text exceed their size limits")
    if len(source_ref.encode("utf-8")) > 2_000:
        raise FeedbackError("source reference is too large")
    normalized = " ".join(text.split())
    if key:
        item_id = "fb-" + _validated_key(key)
    else:
        digest = hashlib.sha256(f"{source}\0{normalized}".encode("utf-8")).hexdigest()[:20]
        item_id = "fb-" + digest
    if len(artifacts) > 32:
        raise FeedbackError("at most 32 artifacts may be linked to one feedback item")
    artifact_snapshots = _snapshot_artifacts(root, artifacts, require_present=False)
    updated = {"schema": SCHEMA, "task_id": task_id, "items": [dict(item) for item in ledger["items"]]}
    existing = next((item for item in updated["items"] if item.get("id") == item_id), None)
    if existing is not None:
        same = (
            existing.get("source") == source
            and existing.get("source_ref", "") == source_ref
            and " ".join(str(existing.get("text") or "").split()) == normalized
            and existing.get("artifacts", []) == artifact_snapshots
        )
        if not same:
            raise FeedbackError(f"feedback ID already exists with different intake data: {item_id}")
        return updated, item_id, False
    updated["items"].append({
        "id": item_id,
        "task_id": task_id,
        "source": source,
        "source_ref": source_ref,
        "text": text,
        "artifacts": artifact_snapshots,
        "intake_at": timestamp,
        "dispositions": [],
    })
    return updated, item_id, True


def import_items(
    payload: Any,
    ledger: Mapping[str, Any],
    *,
    task_id: str,
    root: Path,
    source: str,
    source_ref: str = "",
    artifacts: Sequence[str] = (),
    timestamp: str = "",
) -> tuple[dict[str, Any], list[str], int]:
    """Import only feedback content and provenance, ignoring disposition fields."""
    if isinstance(payload, dict):
        rows = payload.get("items")
    else:
        rows = payload
    if not isinstance(rows, list) or not rows:
        raise FeedbackError("feedback input must be a non-empty item list")
    if len(rows) > 100:
        raise FeedbackError("feedback input must contain at most 100 items")
    updated = dict(ledger)
    updated["items"] = [dict(item) for item in ledger.get("items", [])]
    item_ids: list[str] = []
    added = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise FeedbackError(f"feedback input item {index} must be an object")
        text = row.get("text")
        if not isinstance(text, str):
            raise FeedbackError(f"feedback input item {index} requires text")
        item_source_ref = row.get("source_ref", source_ref)
        if not isinstance(item_source_ref, str):
            raise FeedbackError(f"feedback input item {index} source_ref must be text")
        item_key = row.get("key", "")
        if not isinstance(item_key, str):
            raise FeedbackError(f"feedback input item {index} key must be text")
        item_artifacts = row.get("artifacts", artifacts)
        if not isinstance(item_artifacts, (list, tuple)) or not all(isinstance(value, str) for value in item_artifacts):
            raise FeedbackError(f"feedback input item {index} artifacts must be a list of paths")
        updated, item_id, was_added = intake_item(
            updated,
            task_id=task_id,
            root=root,
            source=source,
            text=text,
            source_ref=item_source_ref,
            key=item_key,
            artifacts=item_artifacts,
            timestamp=timestamp,
        )
        item_ids.append(item_id)
        added += int(was_added)
    return updated, item_ids, added


def acceptance_row(acceptance: Any, *, task_id: str, acceptance_id: str) -> dict[str, Any]:
    if not isinstance(acceptance, Mapping) or acceptance.get("task_id") != task_id:
        raise FeedbackError("acceptance matrix is missing or belongs to another task")
    rows = acceptance.get("rows")
    if not isinstance(rows, list):
        raise FeedbackError("acceptance matrix rows are malformed")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("id") == acceptance_id]
    if len(matches) != 1:
        raise FeedbackError(f"acceptance row not found or ambiguous: {acceptance_id}")
    return dict(matches[0])


def link_acceptance(acceptance: Mapping[str, Any], *, task_id: str, acceptance_id: str, item_id: str) -> dict[str, Any]:
    # Validate the row before copying or mutating the matrix.
    acceptance_row(acceptance, task_id=task_id, acceptance_id=acceptance_id)
    updated = dict(acceptance)
    updated["rows"] = [dict(row) for row in acceptance["rows"]]
    row = next(row for row in updated["rows"] if row.get("id") == acceptance_id)
    feedback_ids = row.get("feedback_ids", [])
    if not isinstance(feedback_ids, list):
        raise FeedbackError(f"acceptance row {acceptance_id} feedback_ids are malformed")
    if item_id not in feedback_ids:
        row["feedback_ids"] = [*feedback_ids, item_id]
    return updated


def _evidence_artifact_paths(value: str) -> list[str]:
    """Recognize explicit file links and unambiguous workspace-relative paths."""
    text = value.strip()
    if text.startswith("file:"):
        path = text.removeprefix("file:").strip()
        return [path] if path else []
    if text.startswith(("https://", "http://")) or any(char.isspace() for char in text):
        return []
    candidate = Path(text)
    if "/" in text or candidate.suffix:
        return [text]
    return []


def disposition_item(
    ledger: Mapping[str, Any],
    *,
    task_id: str,
    item_id: str,
    status: str,
    by: str,
    note: str = "",
    acceptance: Mapping[str, Any] | None = None,
    acceptance_id: str = "",
    related_id: str = "",
    root: Path | None = None,
    timestamp: str = "",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    errors = validate_ledger(dict(ledger), task_id=task_id)
    if errors:
        raise FeedbackError("; ".join(errors))
    if status not in DISPOSITIONS:
        raise FeedbackError(f"unsupported feedback disposition: {status}")
    if not by.strip():
        raise FeedbackError("a disposition author is required")
    items = [dict(item) for item in ledger["items"]]
    item = next((value for value in items if value.get("id") == item_id), None)
    if item is None:
        raise FeedbackError(f"feedback item not found: {item_id}")
    previous = item.get("dispositions", [])
    latest = previous[-1] if previous else {}
    previous_status = str(latest.get("status") or "pending")
    if previous_status in {"rejected-factual", "rejected-preference", "duplicate", "superseded"}:
        raise FeedbackError(f"feedback item already has terminal disposition {previous_status}")
    if status in {"rejected-factual", "rejected-preference", "deferred", "duplicate", "superseded"} and not note.strip():
        raise FeedbackError(f"{status} disposition requires a reason")
    if status == "accepted":
        if previous_status not in {"pending", "deferred"}:
            raise FeedbackError("only pending or deferred feedback can be accepted")
        acceptance_row(acceptance, task_id=task_id, acceptance_id=acceptance_id)
    elif status == "fixed":
        if previous_status not in {"accepted", "fixed"}:
            raise FeedbackError("feedback must be explicitly accepted before it can be fixed")
        prior_acceptance_id = str(latest.get("acceptance_id") or "")
        if acceptance_id and acceptance_id != prior_acceptance_id:
            raise FeedbackError("fixed disposition must use the accepted item’s acceptance ID")
        acceptance_id = prior_acceptance_id
        row = acceptance_row(acceptance, task_id=task_id, acceptance_id=acceptance_id)
        if row.get("status") != "passed" or not isinstance(row.get("actual_evidence"), str) or not row.get("actual_evidence", "").strip():
            raise FeedbackError(f"acceptance row {acceptance_id} must be passed with actual evidence before feedback can be fixed")
        if root is None:
            raise FeedbackError("workspace root is required to verify feedback artifacts")
        artifacts = item.get("artifacts", [])
        paths = [str(entry.get("path") or "") for entry in artifacts if isinstance(entry, Mapping)]
        verification_artifacts = _snapshot_artifacts(root, paths, require_present=True)
        evidence_artifacts = _snapshot_artifacts(
            root,
            _evidence_artifact_paths(str(row["actual_evidence"])),
            require_present=True,
        )
        resolution = {
            "acceptance_id": acceptance_id,
            "actual_evidence": str(row["actual_evidence"]),
            "acceptance_updated_at": str(row.get("updated_at") or ""),
            "artifacts": verification_artifacts,
            "evidence_artifacts": evidence_artifacts,
        }
    else:
        resolution = None

    if status in {"duplicate", "superseded"}:
        if not related_id or related_id == item_id:
            raise FeedbackError(f"{status} disposition requires a different related feedback ID")
        if not any(value.get("id") == related_id for value in items):
            raise FeedbackError(f"related feedback item not found: {related_id}")

    disposition: dict[str, Any] = {
        "status": status,
        "by": by,
        "at": timestamp,
        "note": note,
    }
    if status in {"accepted", "fixed"}:
        disposition["acceptance_id"] = acceptance_id
    if status in {"duplicate", "superseded"}:
        disposition["related_id"] = related_id
        disposition["related_kind"] = "canonical" if status == "duplicate" else "replacement"
    if status == "fixed":
        disposition["resolution"] = resolution
    item.setdefault("dispositions", []).append(disposition)
    updated = {"schema": SCHEMA, "task_id": task_id, "items": items}
    updated_acceptance = None
    if status == "accepted":
        assert acceptance is not None
        updated_acceptance = link_acceptance(
            acceptance, task_id=task_id, acceptance_id=acceptance_id, item_id=item_id
        )
    return updated, updated_acceptance


def _current_artifact_state(root: Path, snapshots: Sequence[Mapping[str, Any]]) -> list[str]:
    changes: list[str] = []
    for snapshot in snapshots:
        relative = str(snapshot.get("path") or "")
        try:
            _, path = _safe_relative_artifact(root, relative)
        except FeedbackError:
            changes.append(f"artifact path is unsafe: {relative}")
            continue
        if not path.is_file():
            changes.append(f"artifact is missing: {relative}")
            continue
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                changes.append(f"artifact exceeds the 64 MB limit: {relative}")
                continue
            current = _file_digest(path)
        except OSError:
            changes.append(f"artifact cannot be read: {relative}")
            continue
        expected = str(snapshot.get("sha256") or "")
        if not expected or current != expected:
            changes.append(f"artifact changed after verification: {relative}")
    return changes


def report(ledger: Mapping[str, Any], *, task_id: str, root: Path, acceptance: Any) -> dict[str, Any]:
    errors = validate_ledger(dict(ledger), task_id=task_id)
    if errors:
        return {"task_id": task_id, "ok": False, "unresolved_count": 1, "items": [], "errors": errors}
    items = {str(item["id"]): item for item in ledger["items"]}
    states: dict[str, dict[str, Any]] = {}
    resolving: set[str] = set()

    def evaluate(item_id: str) -> dict[str, Any]:
        if item_id in states:
            return states[item_id]
        if item_id in resolving:
            return {"state": "unresolved", "reasons": ["feedback relation cycle"]}
        item = items.get(item_id)
        if item is None:
            return {"state": "unresolved", "reasons": ["related feedback item is missing"]}
        resolving.add(item_id)
        dispositions = item.get("dispositions", [])
        latest = dispositions[-1] if dispositions else {}
        status = str(latest.get("status") or "pending")
        reasons: list[str] = []
        result: dict[str, Any] = {"status": status, "acceptance_id": str(latest.get("acceptance_id") or "")}
        if status == "pending":
            reasons.append("awaiting explicit controller disposition")
        elif status == "accepted":
            acceptance_id = str(latest.get("acceptance_id") or "")
            result["acceptance_id"] = acceptance_id
            try:
                row = acceptance_row(acceptance, task_id=task_id, acceptance_id=acceptance_id)
            except FeedbackError as exc:
                reasons.append(str(exc))
            else:
                result["actual_evidence"] = str(row.get("actual_evidence") or "")
                reasons.append("accepted feedback has not been verified as fixed")
        elif status == "fixed":
            resolution = latest.get("resolution")
            if not isinstance(resolution, Mapping):
                reasons.append("fixed disposition has no verification snapshot")
            else:
                acceptance_id = str(resolution.get("acceptance_id") or "")
                result["acceptance_id"] = acceptance_id
                try:
                    row = acceptance_row(acceptance, task_id=task_id, acceptance_id=acceptance_id)
                except FeedbackError as exc:
                    reasons.append(str(exc))
                else:
                    result["actual_evidence"] = str(row.get("actual_evidence") or "")
                    if row.get("status") != "passed":
                        reasons.append(f"acceptance row {acceptance_id} is no longer passed")
                    if not str(row.get("actual_evidence") or "").strip():
                        reasons.append(f"acceptance row {acceptance_id} has no actual evidence")
                    if str(row.get("actual_evidence") or "") != str(resolution.get("actual_evidence") or ""):
                        reasons.append(f"acceptance row {acceptance_id} evidence changed after verification")
                    if str(row.get("updated_at") or "") != str(resolution.get("acceptance_updated_at") or ""):
                        reasons.append(f"acceptance row {acceptance_id} was revised after verification")
                snapshots = resolution.get("artifacts", [])
                if not isinstance(snapshots, list):
                    reasons.append("fixed disposition artifact snapshot is malformed")
                else:
                    reasons.extend(_current_artifact_state(root, snapshots))
                evidence_snapshots = resolution.get("evidence_artifacts", [])
                if not isinstance(evidence_snapshots, list):
                    reasons.append("fixed disposition evidence snapshot is malformed")
                else:
                    reasons.extend(_current_artifact_state(root, evidence_snapshots))
        elif status in {"rejected-factual", "rejected-preference"}:
            if not str(latest.get("note") or "").strip():
                reasons.append("rejection has no recorded reason")
        elif status == "deferred":
            reasons.append("deferred feedback remains an open obligation")
        elif status in {"duplicate", "superseded"}:
            related_id = str(latest.get("related_id") or "")
            result["related_id"] = related_id
            target = evaluate(related_id)
            if target.get("state") != "resolved":
                relation = "canonical feedback" if status == "duplicate" else "replacement feedback"
                reasons.append(f"{relation} {related_id or '(missing)'} is unresolved")
        else:
            reasons.append(f"unsupported disposition status: {status}")
        resolving.remove(item_id)
        value = {**result, "state": "unresolved" if reasons else "resolved", "reasons": reasons}
        states[item_id] = value
        return value

    output_items: list[dict[str, Any]] = []
    for item_id in sorted(items):
        item = items[item_id]
        state = evaluate(item_id)
        output_items.append({
            "id": item_id,
            "task_id": task_id,
            "source": str(item.get("source") or ""),
            "source_ref": str(item.get("source_ref") or ""),
            "text": str(item.get("text") or ""),
            "artifacts": item.get("artifacts", []),
            "disposition": dict(item.get("dispositions", [])[-1]) if item.get("dispositions") else {},
            **state,
        })
    unresolved = sum(entry["state"] == "unresolved" for entry in output_items)
    return {"task_id": task_id, "ok": unresolved == 0, "unresolved_count": unresolved, "items": output_items, "errors": []}
