"""Measure the exact outbound package a handoff sends to a provider.

A worker receives the handoff text, its generated sidecar manifest, the local
context files, and every file in the pinned skill closure. Preflight measures
those bytes as listed: an input that appears twice counts twice. A byte limit
is owner-declared per task class and provider. Without a declared limit the
package is reported as uncalibrated, never as within a limit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "agentflow.outbound-package@1"
LIMIT_SCHEMA = "agentflow.package-limit@1"
KINDS = ("handoff", "manifest", "context", "skill")
STATUSES = ("uncalibrated", "within-limit", "exception-approved", "over-limit")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_DECISION_REF = 200


class PackageError(ValueError):
    """An outbound package input or exception is invalid."""


@dataclass(frozen=True)
class PackageInput:
    kind: str
    label: str
    path: Path | None = None
    content: bytes | None = None


def limit_key(task_class: str, provider: str) -> str:
    return f"{task_class}/{provider}"


def measure(inputs: Sequence[PackageInput]) -> tuple[dict[str, Any] | None, list[str]]:
    """Hash and size every input; any missing or unreadable input rejects the package."""
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in inputs:
        if item.kind not in KINDS:
            raise PackageError(f"unsupported outbound package input kind: {item.kind}")
        if item.content is not None:
            data = item.content
        elif item.path is None:
            raise PackageError(f"outbound package input has no source: {item.label}")
        else:
            try:
                data = item.path.read_bytes()
            except FileNotFoundError:
                errors.append(f"outbound package input is missing: {item.label}")
                continue
            except OSError as exc:
                errors.append(f"outbound package input is unreadable: {item.label}: {exc}")
                continue
        entries.append({
            "kind": item.kind,
            "path": item.label,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    if errors:
        return None, errors
    total = sum(entry["bytes"] for entry in entries)
    unique = sum({entry["sha256"]: entry["bytes"] for entry in entries}.values())
    encoded = json.dumps(
        {"schema": SCHEMA, "entries": entries}, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema": SCHEMA,
        "entries": entries,
        "file_count": len(entries),
        "total_bytes": total,
        "unique_bytes": unique,
        "duplicate_bytes": total - unique,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }, []


def parse_exception(digest: str | None, decision_ref: str | None) -> dict[str, str] | None:
    """Validate an owner exception; it applies only to one exact package digest."""
    if not digest and not decision_ref:
        return None
    if not digest or not decision_ref:
        raise PackageError("a package exception needs both the package digest and a decision reference")
    if not DIGEST_PATTERN.fullmatch(digest):
        raise PackageError("package exception digest must be a lowercase SHA-256 hex digest")
    ref = decision_ref.strip()
    if not ref or len(ref) > MAX_DECISION_REF or any(ord(ch) < 32 for ch in ref):
        raise PackageError(f"package exception decision reference must be 1-{MAX_DECISION_REF} printable characters")
    return {"digest": digest, "decision_ref": ref}


def evaluate(
    package: Mapping[str, Any], *, key: str, limit: int | None,
    exception: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Classify a measured package against its declared limit, if any."""
    total = int(package["total_bytes"])
    digest = str(package["digest"])
    result: dict[str, Any] = {
        "schema": LIMIT_SCHEMA,
        "key": key,
        "limit_bytes": limit,
        "total_bytes": total,
        "package_digest": digest,
        "excess_bytes": None if limit is None else max(0, total - limit),
        "exception": None,
    }
    if limit is None:
        result["status"] = "uncalibrated"
    elif total <= limit:
        result["status"] = "within-limit"
    elif exception is not None and exception.get("digest") == digest:
        result["status"] = "exception-approved"
        result["exception"] = dict(exception)
    else:
        result["status"] = "over-limit"
        if exception is not None:
            result["exception_rejected"] = "exception is bound to a different package digest"
    return result


def over_limit_message(evaluation: Mapping[str, Any]) -> str:
    return (
        f"outbound package is {evaluation['total_bytes']} bytes, over the declared "
        f"{evaluation['key']} limit of {evaluation['limit_bytes']} bytes by "
        f"{evaluation['excess_bytes']}; reduce the package or record an approved exception "
        f"for digest {evaluation['package_digest']}"
        + (f" ({evaluation['exception_rejected']})" if evaluation.get("exception_rejected") else "")
    )
