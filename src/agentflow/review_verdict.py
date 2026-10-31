"""Explicit review verdicts bound to the exact reviewed subject and handoff.

A review worker that exits successfully has finished running; it has not
necessarily approved anything. External review returns therefore carry a
verdict object, and a missing, malformed, stale, or mismatched verdict is
reported as unavailable rather than treated as approval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "agentflow.review-verdict@1"
VERDICTS = ("approve", "changes-requested")
UNVERSIONED_SUBJECT = "unversioned"
UNAVAILABLE_STATES = ("missing", "malformed", "stale", "mismatched")
_FIELDS = frozenset({"verdict", "subject", "handoff_sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_SUBJECT = 512


class ReviewVerdictError(ValueError):
    """A review verdict that cannot count as a review decision."""

    def __init__(self, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail
        super().__init__(
            f"review verdict unavailable ({state}): {detail}; "
            "exit status and outcome are not approval"
        )


def subject_for(manifest: Mapping[str, Any]) -> str:
    """Return the reviewed subject a handoff manifest binds."""
    workspace_kind = str(manifest.get("workspace_kind") or "git")
    base = str(manifest.get("base") or "")
    if workspace_kind == "git" and base:
        return base
    return UNVERSIONED_SUBJECT


def requirement(subject: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "subject": subject, "verdicts": list(VERDICTS)}


def parse_requirement(value: Any) -> dict[str, Any] | None:
    """Validate a review requirement copied into a signed return contract."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"schema", "subject", "verdicts"}:
        raise ValueError("review verdict requirement is malformed")
    subject = value.get("subject")
    if (
        value.get("schema") != SCHEMA
        or not isinstance(subject, str)
        or not subject
        or len(subject) > _MAX_SUBJECT
        or list(value.get("verdicts") or []) != list(VERDICTS)
    ):
        raise ValueError("review verdict requirement is malformed")
    return {"schema": SCHEMA, "subject": subject, "verdicts": list(VERDICTS)}


def validate(value: Any, required: Mapping[str, Any], *, handoff_sha256: str) -> dict[str, str]:
    """Return a bound verdict or raise ``ReviewVerdictError``."""
    expected = parse_requirement(required)
    if expected is None:
        raise ValueError("review verdict requirement is unavailable")
    if value is None or value == "" or value == {}:
        raise ReviewVerdictError("missing", "the result has no review verdict")
    if not isinstance(value, Mapping) or set(value) != _FIELDS or not all(
        isinstance(value.get(field), str) for field in _FIELDS
    ):
        raise ReviewVerdictError(
            "malformed", "review must contain exactly verdict, subject, and handoff_sha256 strings"
        )
    verdict = str(value["verdict"])
    if verdict not in VERDICTS:
        raise ReviewVerdictError("malformed", f"verdict must be one of {', '.join(VERDICTS)}")
    supplied_digest = str(value["handoff_sha256"])
    if not _SHA256.fullmatch(supplied_digest):
        raise ReviewVerdictError("malformed", "handoff_sha256 must be 64 lowercase hex characters")
    if str(value["subject"]) != expected["subject"]:
        raise ReviewVerdictError(
            "stale", f"verdict covers {value['subject']!r}, not the bound subject {expected['subject']!r}"
        )
    if supplied_digest != handoff_sha256:
        raise ReviewVerdictError("mismatched", "verdict is bound to a different handoff")
    return {"verdict": verdict, "subject": expected["subject"], "handoff_sha256": supplied_digest}


def json_schema(required: Mapping[str, Any], *, handoff_sha256: str) -> dict[str, Any]:
    """Structured-output schema for providers that enforce result shape."""
    expected = parse_requirement(required) or {}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["verdict", "subject", "handoff_sha256"],
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "subject": {"type": "string", "enum": [expected.get("subject", "")]},
            "handoff_sha256": {"type": "string", "enum": [handoff_sha256]},
        },
    }
