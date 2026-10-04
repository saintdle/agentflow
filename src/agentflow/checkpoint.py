from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class CheckpointError(RuntimeError):
    """A safe, user-facing checkpoint validation error."""


SCHEMA = "agentflow.checkpoint"
SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, 2, SCHEMA_VERSION})

# The schema is closed: only these keys are accepted. Rejecting unknown keys is
# what keeps transcripts, prompts, and free-form context out of the checkpoint.
TEXT_FIELDS = (
    "task",
    "phase",
    "completed_evidence",
    "next_action",
    "blocker",
    "last_check",
    "remaining_risk",
    "session_hash",
)
STATE_TEXT_FIELDS = (
    "root",
    "controller",
    "actor",
    "claim_id",
    "session_id",
    "lease_token",
    "state",
    "status",
    "terminal_reason",
    "updated_at",
)
REQUIRED_FIELDS = ("task", "phase", "next_action")
LIST_FIELDS = ("changed_files",)
ALL_FIELDS = TEXT_FIELDS + STATE_TEXT_FIELDS + LIST_FIELDS + ("epoch", "terminal", "active_tasks")

FIELD_MAX = 500
SESSION_HASH_MAX = 128
MAX_CHANGED_FILES = 50
CHANGED_FILE_MAX = 240
MAX_ACTIVE_TASKS = 8
ACTIVE_TASK_FIELDS = frozenset({"task", "phase", "actor", "claim_id", "session_id", "state"})
ACTIVE_TASK_STATES = frozenset({"claimed_no_session", "running", "launched", "identity_pending"})
MAX_TOTAL_BYTES = 4096
TERMINAL_STATES = frozenset({"completed", "failed", "blocked", "halted", "terminal"})

_SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\b")),
    (
        "labeled-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|access[_-]?token|client[_-]?secret|"
            r"password|passwd|authorization|bearer)\b\s*[:=]\s*\S{6,}"
        ),
    ),
)


def scan_for_secrets(text: str) -> list[str]:
    """Return the names of every secret pattern that matches ``text``."""

    if not isinstance(text, str):
        return []
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(text)]


def _check_text(field: str, value: Any, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CheckpointError(f"field {field!r} must be a string")
    value = value.strip()
    if required and not value:
        raise CheckpointError(f"field {field!r} is required")
    cap = SESSION_HASH_MAX if field == "session_hash" else FIELD_MAX
    if len(value) > cap:
        raise CheckpointError(
            f"field {field!r} exceeds {cap} characters ({len(value)}); "
            "checkpoints store bounded state, not transcripts"
        )
    for char in value:
        code = ord(char)
        if code < 32 and char not in ("\t", "\n"):
            raise CheckpointError(f"field {field!r} contains unsafe control characters")
        if code == 127:
            raise CheckpointError(f"field {field!r} contains unsafe control characters")
    found = scan_for_secrets(value)
    if found:
        raise CheckpointError(
            f"field {field!r} rejected: matched secret pattern(s) {', '.join(found)}"
        )
    return value


def _check_changed_files(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CheckpointError("field 'changed_files' must be a list of paths")
    if len(value) > MAX_CHANGED_FILES:
        raise CheckpointError(
            f"field 'changed_files' exceeds {MAX_CHANGED_FILES} entries ({len(value)})"
        )
    files: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CheckpointError("field 'changed_files' entries must be strings")
        item = item.strip()
        if not item:
            continue
        if len(item) > CHANGED_FILE_MAX:
            raise CheckpointError("a 'changed_files' entry exceeds the path length cap")
        for char in item:
            code = ord(char)
            if code < 32 or code == 127:
                raise CheckpointError("field 'changed_files' contains unsafe control characters")
        found = scan_for_secrets(item)
        if found:
            raise CheckpointError(
                f"field 'changed_files' rejected: matched secret pattern(s) {', '.join(found)}"
            )
        files.append(item)
    return files


def _check_active_tasks(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckpointError("field 'active_tasks' must be a list")
    if len(value) > MAX_ACTIVE_TASKS:
        raise CheckpointError(
            f"field 'active_tasks' exceeds {MAX_ACTIVE_TASKS} entries ({len(value)})"
        )
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise CheckpointError(f"active_tasks entry {index} must be an object")
        unknown = sorted(set(raw) - ACTIVE_TASK_FIELDS)
        if unknown:
            raise CheckpointError(
                f"active_tasks entry {index} has unknown field(s): {', '.join(unknown)}"
            )
        task = _check_text(f"active_tasks[{index}].task", raw.get("task"), required=True)
        if task in seen:
            raise CheckpointError(f"active_tasks contains duplicate task {task!r}")
        seen.add(task)
        state = _check_text(f"active_tasks[{index}].state", raw.get("state"), required=True)
        if state not in ACTIVE_TASK_STATES:
            raise CheckpointError(f"active_tasks entry {index} has unsupported state {state!r}")
        item = {
            field: _check_text(
                f"active_tasks[{index}].{field}", raw.get(field),
                required=field in {"task", "state"},
            )
            for field in ("task", "phase", "actor", "claim_id", "session_id", "state")
        }
        tasks.append(item)
    return tasks


def build_checkpoint(data: Any) -> dict[str, Any]:
    """Validate ``data`` and return a normalized, current-version document.

    Version one and two checkpoints remain readable. The v3 ``active_tasks``
    collection is bounded and optional so old single-task records can be
    migrated without losing a live claim or provider session.
    """

    if not isinstance(data, dict):
        raise CheckpointError("checkpoint payload must be an object")
    unknown = sorted(set(data) - set(ALL_FIELDS) - {"schema", "version"})
    if unknown:
        raise CheckpointError(
            "unknown checkpoint field(s) rejected: "
            + ", ".join(unknown)
            + "; only the fixed schema is persisted"
        )
    if "schema" in data and data["schema"] != SCHEMA:
        raise CheckpointError(f"unexpected schema {data['schema']!r}")
    version = data.get("version", LEGACY_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise CheckpointError("checkpoint version must be an integer")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CheckpointError(f"unsupported checkpoint version {version!r}")

    document: dict[str, Any] = {"schema": SCHEMA, "version": SCHEMA_VERSION}
    for field in TEXT_FIELDS:
        document[field] = _check_text(field, data.get(field), required=field in REQUIRED_FIELDS)
    for field in STATE_TEXT_FIELDS:
        document[field] = _check_text(field, data.get(field), required=False)
    document["changed_files"] = _check_changed_files(data.get("changed_files"))
    document["active_tasks"] = _check_active_tasks(data.get("active_tasks"))

    epoch = data.get("epoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise CheckpointError("field 'epoch' must be a non-negative integer")
    document["epoch"] = epoch
    terminal = data.get("terminal", False)
    if not isinstance(terminal, bool):
        raise CheckpointError("field 'terminal' must be a boolean")
    status = document["status"] or document["state"]
    if status == "claimed" and not document["session_id"]:
        # A claim is durable before a provider session is launched.  It is
        # unsafe to interpret that state as permission to launch again.
        status = "claimed_no_session"
    document["status"] = status
    # A deadline can interrupt a parallel failure-drain.  Preserve the
    # typed combination state=draining/status=incomplete: ``status`` exposes
    # the resumable deadline to operators while ``state`` prevents resumed
    # controllers from admitting new work before active siblings reconcile.
    draining_incomplete = status == "incomplete" and document["state"] == "draining"
    document["state"] = "draining" if draining_incomplete else status or document["state"]
    document["terminal"] = bool(terminal or status in TERMINAL_STATES)

    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_TOTAL_BYTES:
        raise CheckpointError(
            f"checkpoint exceeds the {MAX_TOTAL_BYTES}-byte size cap"
        )
    return document


def write_checkpoint(path: Path, data: Any) -> dict[str, Any]:
    document = build_checkpoint(data)
    _atomic_write(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` only after the complete document reaches disk."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = -1
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def update_checkpoint(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Apply a bounded update to an existing checkpoint atomically."""

    if not isinstance(updates, dict):
        raise CheckpointError("checkpoint updates must be an object")
    current = load_checkpoint(path)
    current.update(updates)
    return write_checkpoint(path, current)


def load_checkpoint(path: Path, *, migrate: bool = True) -> dict[str, Any]:
    """Read and re-validate a checkpoint, upgrading legacy state atomically."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read checkpoint: {exc}") from exc
    document = build_checkpoint(raw)
    legacy_version = raw.get("version", LEGACY_SCHEMA_VERSION)
    if migrate and legacy_version != SCHEMA_VERSION:
        try:
            write_checkpoint(path, document)
        except OSError as exc:
            raise CheckpointError(f"cannot migrate checkpoint: {exc}") from exc
    return document


def admission_phase(document: dict[str, Any]) -> str:
    """Classify whether a validated checkpoint admits new work.

    A deadline may make a draining checkpoint visibly incomplete while its
    state remains draining.  Admission follows that durable drain intent,
    not the resumable status presented to operators.
    """

    current = build_checkpoint(document)
    if current["terminal"] or current["status"] in TERMINAL_STATES:
        return "terminal"
    if current["state"] == "draining" or current["status"] == "draining":
        return "draining"
    return "open"


def resume_state(document: dict[str, Any]) -> str:
    """Return the safe controller action for a validated checkpoint."""

    current = build_checkpoint(document)
    if admission_phase(current) == "terminal":
        return current["status"] or "terminal"
    if current["status"] in {"claimed", "claimed_no_session"} and not current["session_id"]:
        return "claimed_no_session"
    return current["status"] or current["state"] or "new"


def render(document: dict[str, Any]) -> str:
    lines = ["Checkpoint (resume state):"]
    for field in TEXT_FIELDS:
        value = document.get(field) or "-"
        lines.append(f"  {field.replace('_', ' ')}: {value}")
    files = document.get("changed_files") or []
    lines.append(f"  changed files: {', '.join(files) if files else '-'}")
    return "\n".join(lines)
