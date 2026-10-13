"""Controller-owned durable continuity for the optional Copilot proof route.

The authority key is supplied by the controller in memory. A distinct SQLite
database stores an authenticated expected head; SQLite coordinates changes to
that database and the ledger database in one rollback-journal transaction.
This is a local controller boundary, not a same-user isolation mechanism, and
never enables persistent worker admission.
"""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import hmac
import importlib.metadata
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentflow.copilot_sdk import (
    COPILOT_NEGATIVE_CONTROL_PROMPT,
    CopilotLaunchIdentity,
    CopilotLedgerHead,
    CopilotLedgerSnapshot,
    CopilotProofRun,
    CopilotSDKError,
    CopilotUsageReport,
    ProtectedCopilotLedger,
    derive_evidence_key,
    sdk_import_identity_fingerprint,
    verify_sdk_import_identity,
)
from agentflow import checkpoint


_CHECKPOINT_DOMAIN = b"agentflow.copilot-controller.expected-head@1\0"
_MANIFEST_DOMAIN = b"agentflow.copilot-controller.proof-manifest@1\0"
_REVIEW_DOMAIN = b"agentflow.copilot-controller.accepted-review@1\0"
_MANIFEST_SCHEMA = "agentflow.copilot-proof-manifest@1"
_REVIEW_SCHEMA = "agentflow.copilot-accepted-review@1"
_PROOF_DEADLINE_SECONDS = 180
_PROOF_RETRIES = 0
_PROOF_MAX_ATTEMPTS = 1
_ACTIVE_CLAIM_STATES = frozenset({"claimed_no_session", "running", "launched", "identity_pending"})


@dataclass(frozen=True)
class CopilotReviewJudgement:
    """One explicit controller-accepted independent review artifact disposition."""

    reviewer: str
    artifact_path: Path
    artifact_sha256: str
    disposition: str


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _scope(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentflow.copilot.{kind}\0{value}".encode("utf-8")).hexdigest()


def _identity_digest(identity: CopilotLaunchIdentity) -> str:
    return hashlib.sha256(_canonical(identity.to_dict())).hexdigest()


def _source_fingerprint() -> dict[str, Any]:
    module_path = Path(__file__).resolve()
    package_root = module_path.parent
    artifact_root = package_root.parent.parent
    authority_sources = {
        "agentflow/copilot_controller.py": module_path,
        "agentflow/copilot_sdk.py": package_root / "copilot_sdk.py",
        "agentflow/cli.py": package_root / "cli.py",
        "agentflow/controller.py": package_root / "controller.py",
        "agentflow/checkpoint.py": package_root / "checkpoint.py",
        "agentflow/execution_limits.py": package_root / "execution_limits.py",
    }
    files = {
        name: path for name, path in authority_sources.items()
        if path.is_file()
    }
    for module_name, source_name in (
        ("agentflow.copilot_controller", "agentflow/copilot_controller.py"),
        ("agentflow.copilot_sdk", "agentflow/copilot_sdk.py"),
        ("agentflow.cli", "agentflow/cli.py"),
        ("agentflow.controller", "agentflow/controller.py"),
        ("agentflow.checkpoint", "agentflow/checkpoint.py"),
        ("agentflow.execution_limits", "agentflow/execution_limits.py"),
    ):
        module = sys.modules.get(module_name)
        origin = getattr(module, "__file__", None) if module is not None else None
        if not origin:
            continue
        actual_path = Path(origin).resolve()
        expected_path = authority_sources[source_name]
        if actual_path.is_file() and actual_path != expected_path.resolve():
            files[f"loaded/{module_name.replace('.', '/')}.py"] = actual_path
    runner_candidates = []
    for name in ("copilot_proof_runner", "__main__"):
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None) if module is not None else None
        if origin and Path(origin).name == "copilot_proof_runner.py":
            runner_candidates.append(Path(origin).resolve())
    source_runner = artifact_root / "scripts" / "copilot_proof_runner.py"
    if source_runner.is_file():
        runner_candidates.append(source_runner.resolve())
    if not runner_candidates:
        raise CopilotSDKError("Copilot proof runner source identity is unavailable")
    runner = runner_candidates[0]
    files["runner/copilot_proof_runner.py"] = runner
    try:
        hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
        try:
            sdk_version: str | None = importlib.metadata.version("github-copilot-sdk")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = None
            sdk_hashes = None
        else:
            distribution = importlib.metadata.distribution("github-copilot-sdk")
            sdk_sources = {
                f"copilot/{name}": Path(distribution.locate_file(f"copilot/{name}"))
                for name in (
                    "client.py", "_cli_download.py", "_cli_version.py",
                    "_sdk_protocol_version.py", "_jsonrpc.py", "rpc.py", "session.py",
                )
            }
            sdk_hashes = {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in sdk_sources.items()
            }
        sdk_import_identity = sdk_import_identity_fingerprint(require_clean=True)
        try:
            source_sha = subprocess.run(
                ["git", "-C", str(artifact_root), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if len(source_sha) != 40:
                source_sha = None
        except (OSError, subprocess.SubprocessError):
            source_sha = None
    except OSError as exc:
        raise CopilotSDKError("Copilot reviewed source identity is unavailable") from exc
    if any(len(value) != 64 for value in hashes.values()):
        raise CopilotSDKError("Copilot reviewed source identity is malformed")
    return {
        "source_sha": source_sha,
        "artifact_kind": "source_tree" if source_runner.is_file() else "installed_runner",
        "module_sha256": hashes,
        "sdk": {
            "required_version": "1.0.17",
            "installed_version": sdk_version,
            "source_sha256": sdk_hashes,
            "import_identity": sdk_import_identity,
        },
    }


def _workspace_scope(workspace_root: Path) -> str:
    resolved = Path(workspace_root).expanduser().resolve(strict=False)
    return _scope("workspace-root", str(resolved))


def _cli_backend():
    # Keep CLI startup and optional provider discovery out of import time.
    from agentflow import cli
    return cli


def _owned_by_current_user(info: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or info.st_uid == os.getuid()


def _strict_path(path: Path, *, must_exist: bool, name: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate != Path(os.path.normpath(str(candidate))):
        raise CopilotSDKError(f"Copilot {name} path is not canonical")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CopilotSDKError(f"Copilot {name} parent directory is unavailable") from exc
    if parent != candidate.parent:
        raise CopilotSDKError(f"Copilot {name} path uses a symlink or path alias")
    try:
        names = {entry.name.casefold(): entry.name for entry in parent.iterdir()}
    except OSError as exc:
        raise CopilotSDKError(f"Copilot {name} parent directory is unavailable") from exc
    actual_case = names.get(candidate.name.casefold())
    if actual_case is not None and actual_case != candidate.name:
        raise CopilotSDKError(f"Copilot {name} path uses a case alias")
    if must_exist and actual_case is None:
        raise CopilotSDKError(f"Copilot {name} file is unavailable")
    return candidate


def _private_directory(path: Path, *, workspace_root: Path, create: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate != Path(os.path.normpath(str(candidate))):
        raise CopilotSDKError("Copilot controller state directory path is not canonical")
    if create and not candidate.exists():
        candidate.mkdir(parents=True, mode=0o700)
        os.chmod(candidate, 0o700)
    try:
        resolved = candidate.resolve(strict=True)
        info = candidate.lstat()
    except (OSError, RuntimeError) as exc:
        raise CopilotSDKError("Copilot controller state directory is unavailable") from exc
    if resolved != candidate or not stat.S_ISDIR(info.st_mode):
        raise CopilotSDKError("Copilot controller state directory uses a symlink or path alias")
    if resolved == workspace_root or resolved in workspace_root.parents or workspace_root in resolved.parents:
        raise CopilotSDKError("Copilot controller state directory must be separate from the worker workspace")
    if stat.S_IMODE(info.st_mode) & 0o077 or not _owned_by_current_user(info):
        raise CopilotSDKError("Copilot controller state directory is not private and owned")
    return resolved


def _private_json(
    path: Path, *, workspace_root: Path, immutable: bool = False,
    allow_within_workspace: bool = False,
) -> dict[str, Any]:
    candidate = _strict_path(path, must_exist=True, name="controller file")
    try:
        info = candidate.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or immutable and stat.S_IMODE(info.st_mode) & 0o222
            or not _owned_by_current_user(info)
            or info.st_nlink != 1
            or info.st_size > 16_384
        ):
            raise CopilotSDKError("Copilot controller file is not private, owned, and immutable")
        if not allow_within_workspace and (
            candidate == workspace_root or candidate in workspace_root.parents or workspace_root in candidate.parents
        ):
            raise CopilotSDKError("Copilot controller file must be separate from the worker workspace")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise CopilotSDKError("Copilot controller file changed while it was opened")
            value = json.load(handle)
    except CopilotSDKError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CopilotSDKError("Copilot controller file is unavailable or malformed") from exc
    if not isinstance(value, dict):
        raise CopilotSDKError("Copilot controller file has an invalid shape")
    return value


@dataclass(frozen=True)
class _ControllerAuthority:
    workspace_root: Path
    workflow_root: str
    credential_path: Path
    controller_id: str
    lease: Any = field(repr=False)
    authority_secret: str = field(repr=False)
    controller: Any = field(repr=False)


def _load_controller_authority(
    workspace_root: Path, workflow_root: str, *, resume_proof: str | None = None,
) -> _ControllerAuthority:
    try:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        if not workspace.is_dir() or not workflow_root or workflow_root.strip() != workflow_root:
            raise CopilotSDKError("Copilot proof workspace or workflow root is invalid")
        cli = _cli_backend()
        args = argparse.Namespace(root=str(workspace), workflow_root=workflow_root, resume_key_file="")
        credential_path = _strict_path(
            cli._resume_key_path(args), must_exist=True, name="canonical controller credential",
        )
        parent_info = credential_path.parent.stat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) & 0o077
            or not _owned_by_current_user(parent_info)
        ):
            raise CopilotSDKError("canonical controller credential directory is not private and owned")
        credentials = _private_json(credential_path, workspace_root=workspace)
        if (
            credentials.get("schema") != "agentflow.controller_credentials"
            or credentials.get("version") != 2
            or credentials.get("workspace_root") != str(workspace)
            or credentials.get("workflow_root") != workflow_root
            or not isinstance(credentials.get("authority_secret"), str)
            or len(credentials["authority_secret"]) < 32
            or not isinstance(credentials.get("resume_secret"), str)
            or not credentials["resume_secret"]
            or not isinstance(credentials.get("continuity_id"), str)
            or not credentials["continuity_id"]
        ):
            raise CopilotSDKError("canonical controller credentials do not match this proof root")
        state_path = cli._controller_state_dir(workspace, workflow_root) / "state.json"
        checkpoint_path = state_path.with_name("checkpoint.json")
        state = _private_json(state_path, workspace_root=workspace, allow_within_workspace=True)
        lease_document = state.get("lease")
        if not isinstance(lease_document, Mapping):
            raise CopilotSDKError("Copilot proof requires a current controller lease")
        controller_id = str(lease_document.get("controller") or "")
        if not controller_id:
            raise CopilotSDKError("Copilot proof controller identity is missing")
        from agentflow.controller import RootController
        controller = RootController(
            str(workspace), controller_id,
            state_path=state_path, checkpoint_path=checkpoint_path,
        )
        canonical_resume_proof = credentials["resume_secret"]
        if resume_proof is not None and (
            not isinstance(resume_proof, str)
            or not hmac.compare_digest(resume_proof, canonical_resume_proof)
        ):
            raise CopilotSDKError("controller resume proof does not match canonical credentials")
        lease = controller.authorize(resume_proof or canonical_resume_proof)
        if (
            lease.root != str(workspace)
            or lease.continuity_id != credentials["continuity_id"]
            or lease.epoch != lease_document.get("epoch")
        ):
            raise CopilotSDKError("canonical controller credentials do not authenticate the current lease")
        with controller.fence(lease):
            controller.active_tasks()
        return _ControllerAuthority(
            workspace, workflow_root, credential_path, controller_id, lease,
            credentials["authority_secret"], controller,
        )
    except CopilotSDKError:
        raise
    except Exception as exc:
        raise CopilotSDKError("canonical controller authority could not be authenticated") from exc


def _active_claim_locked(authority: _ControllerAuthority, task_id: str, claim_id: str) -> Mapping[str, str]:
    matches = [
        row for row in authority.controller.active_tasks()
        if row.get("task") == task_id and row.get("claim_id") == claim_id
    ]
    if len(matches) != 1:
        raise CopilotSDKError("Copilot proof task is not the exact active claim for this workflow root")
    row = matches[0]
    if row.get("actor") != authority.controller_id or row.get("state") not in _ACTIVE_CLAIM_STATES:
        raise CopilotSDKError("Copilot proof task claim is not owned by the current controller")
    return row


def _active_claim(authority: _ControllerAuthority, task_id: str, claim_id: str) -> Mapping[str, str]:
    with authority.controller.fence(authority.lease):
        return _active_claim_locked(authority, task_id, claim_id)


def _execution_reservation_path(authority: _ControllerAuthority) -> Path:
    path = _cli_backend()._execution_limit_ledger_path(
        authority.workspace_root, authority.workflow_root,
    )
    _private_directory(path.parent, workspace_root=authority.workspace_root)
    if path.exists() or path.is_symlink():
        _private_json(path, workspace_root=authority.workspace_root)
    lock = path.with_name(f".{path.name}.lock")
    if lock.exists() or lock.is_symlink():
        info = lock.lstat()
        if not stat.S_ISREG(info.st_mode) or not _owned_by_current_user(info) or stat.S_IMODE(info.st_mode) & 0o077:
            raise CopilotSDKError("protected execution ledger lock is not private and owned")
    return path


@dataclass(frozen=True)
class AuthenticatedCopilotProofManifest:
    identity: CopilotLaunchIdentity
    workspace_root: Path
    workflow_root: str
    task_claim: Mapping[str, str]
    deadline_epoch: float
    execution_reservation_digest: str
    review_attestation_digest: str
    source_fingerprint: Mapping[str, Any]
    manifest_path: Path = field(repr=False)
    review_attestation_path: Path = field(repr=False)
    authority: _ControllerAuthority = field(repr=False)


def _manifest_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "manifest_hmac"}


def _manifest_mac(secret: str, document: Mapping[str, Any]) -> str:
    return hmac.new(
        secret.encode("utf-8"), _MANIFEST_DOMAIN + _canonical(_manifest_body(document)), hashlib.sha256,
    ).hexdigest()


def _review_mac(secret: str, document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "attestation_hmac"}
    return hmac.new(secret.encode("utf-8"), _REVIEW_DOMAIN + _canonical(body), hashlib.sha256).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _read_review_artifact(path: Path, workspace_root: Path) -> str:
    candidate = _strict_path(path, must_exist=True, name="review artifact")
    if candidate == workspace_root or candidate in workspace_root.parents or workspace_root in candidate.parents:
        raise CopilotSDKError("review artifacts must be outside the worker workspace")
    try:
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or not _owned_by_current_user(info) or info.st_nlink != 1 or info.st_size > 5_000_000):
            raise CopilotSDKError("review artifact is not a private, bounded controller input")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise CopilotSDKError("review artifact changed while it was opened")
            payload = handle.read(5_000_001)
            finished = os.fstat(handle.fileno())
            before = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            after = (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns, finished.st_ctime_ns)
            if len(payload) > 5_000_000 or len(payload) != info.st_size or before != after:
                raise CopilotSDKError("review artifact changed or exceeded its size limit")
    except CopilotSDKError:
        raise
    except OSError as exc:
        raise CopilotSDKError("review artifact is unavailable") from exc
    return hashlib.sha256(payload).hexdigest()


def _protected_budget_preflight(
    authority: _ControllerAuthority, identity: CopilotLaunchIdentity, *, allow_reserved: bool,
) -> tuple[str, int, str]:
    cli = _cli_backend()
    path = _execution_reservation_path(authority)
    if path.exists():
        ledger = _private_json(path, workspace_root=authority.workspace_root)
    else:
        ledger = {"schema": "agentflow.execution-limit-ledger@1", "entries": {}}
    entries = ledger.get("entries")
    if ledger.get("schema") != "agentflow.execution-limit-ledger@1" or not isinstance(entries, Mapping):
        raise CopilotSDKError("protected global execution budget ledger is malformed")
    task_key = cli._execution_ledger_key(authority.workspace_root, identity.workflow_root, identity.task_id)
    own = entries.get(task_key)
    if own is None:
        task_state = "first-attempt-ready"
    else:
        try:
            own = cli._verify_execution_ledger_entry(own, authority_secret=authority.authority_secret)
        except Exception as exc:
            raise CopilotSDKError("protected task execution budget could not be authenticated") from exc
        expected_launch = {
            "workspace_root": str(authority.workspace_root), "workflow_root": identity.workflow_root,
            "task_id": identity.task_id, "claim_id": identity.claim_id,
            "lease_id": authority.lease.token, "launch_id": identity.launch_id,
            "controller_id": authority.controller_id, "continuity_id": authority.lease.continuity_id,
            "lease_epoch": authority.lease.epoch,
        }
        if (not allow_reserved or own.get("launch_identity") != expected_launch
                or own.get("execution_limits") != {"deadline_seconds": _PROOF_DEADLINE_SECONDS, "max_retries": _PROOF_RETRIES}
                or own.get("attempt") != 1 or own.get("max_attempts") != 1
                or own.get("status") != "active"):
            raise CopilotSDKError("protected task budget is already consumed or incompatible")
        task_state = "first-attempt-ready"
    other_entries = []
    for key, value in sorted(entries.items()):
        if key == task_key:
            continue
        try:
            verified = cli._verify_execution_ledger_entry(value, authority_secret=authority.authority_secret)
        except Exception as exc:
            raise CopilotSDKError("protected global execution budget ledger contains an invalid entry") from exc
        other_entries.append([key, dict(verified)])
    global_digest = hashlib.sha256(_canonical({"entries": other_entries})).hexdigest()
    return global_digest, len(other_entries), task_state


def _preflight_digest(authority: _ControllerAuthority, identity: CopilotLaunchIdentity,
                      claim: Mapping[str, str], source: Mapping[str, Any], *,
                      controller_phase: str, global_budget_digest: str,
                      global_budget_count: int, task_budget_state: str) -> str:
    return hashlib.sha256(_canonical({
        "workspace_root": str(authority.workspace_root), "workflow_root": identity.workflow_root,
        "identity": identity.to_dict(), "claim": dict(claim),
        "lease_token_scope": _scope("lease-token", authority.lease.token),
        "controller_id": authority.controller_id, "source": dict(source),
        "controller_phase": controller_phase,
        "route": "copilot-sdk-negative-control-no-tools",
        "purpose": "diagnostic-only-never-persistent-admission",
        "global_budget_digest": global_budget_digest,
        "global_budget_entry_count": global_budget_count,
        "task_budget_state": task_budget_state,
        "budget": {"deadline_seconds": _PROOF_DEADLINE_SECONDS, "retry_count": _PROOF_RETRIES,
                   "attempt": 1, "max_attempts": 1},
        "checks": ["canonical-credential", "current-lease", "exact-active-claim", "same-root",
                   "non-draining-controller", "protected-task-budget", "protected-global-budget"],
    })).hexdigest()


def _controller_claim_preflight(
    authority: _ControllerAuthority, identity: CopilotLaunchIdentity,
) -> tuple[dict[str, str], str]:
    """Read exact current claim and reject draining or incompatible controller state under fence."""
    document = authority.controller._load_checkpoint()
    rows = authority.controller.active_tasks()
    phase = checkpoint.admission_phase(document)
    if (phase != "open" or document.get("root") != str(authority.workspace_root)
            or document.get("controller") != authority.controller_id
            or document.get("pending_continuation_task")
            or len(rows) != 1):
        raise CopilotSDKError("Copilot review preflight requires one open, non-draining controller claim")
    matches = [row for row in rows
               if row.get("task") == identity.task_id and row.get("claim_id") == identity.claim_id]
    if (len(matches) != 1 or matches[0].get("actor") != authority.controller_id
            or matches[0].get("state") not in _ACTIVE_CLAIM_STATES):
        raise CopilotSDKError("Copilot review preflight requires the exact active proof claim")
    return dict(matches[0]), phase


def _review_preflight_digest(
    authority: _ControllerAuthority, identity: CopilotLaunchIdentity, claim: Mapping[str, str],
    source: Mapping[str, Any], phase: str, *, allow_reserved: bool,
) -> str:
    global_digest, global_count, task_state = _protected_budget_preflight(
        authority, identity, allow_reserved=allow_reserved,
    )
    return _preflight_digest(
        authority, identity, claim, source, controller_phase=phase,
        global_budget_digest=global_digest, global_budget_count=global_count,
        task_budget_state=task_state,
    )


def _verify_review_attestation_locked(path: Path, authority: _ControllerAuthority,
                                      identity: CopilotLaunchIdentity, claim: Mapping[str, str], *,
                                      allow_reserved: bool = False) -> str:
    candidate = _review_attestation_path(path, authority.workspace_root, must_exist=True)
    document = _private_json(candidate, workspace_root=authority.workspace_root)
    current_claim, phase = _controller_claim_preflight(authority, identity)
    if dict(claim) != current_claim:
        raise CopilotSDKError("Copilot review attestation claim changed")
    source = _source_fingerprint()
    preflight = _review_preflight_digest(
        authority, identity, current_claim, source, phase, allow_reserved=allow_reserved,
    )
    reviews = document.get("reviews")
    fields = {"schema", "workspace_root", "workflow_root", "identity", "task_claim_sha256",
              "source", "preflight_sha256", "lease", "accepted_by", "reviews",
              "authority_key_id", "attestation_hmac"}
    valid_reviews = (
        isinstance(reviews, list) and len(reviews) == 2
        and all(isinstance(row, Mapping) and set(row) == {"reviewer", "artifact_path", "artifact_sha256", "disposition"}
                and isinstance(row.get("reviewer"), str) and row["reviewer"]
                and _is_sha256(row.get("artifact_sha256"))
                and row.get("disposition") == "accepted" for row in reviews)
        and reviews[0]["reviewer"] != reviews[1]["reviewer"]
        and reviews[0]["artifact_sha256"] != reviews[1]["artifact_sha256"]
    )
    if (
        set(document) != fields
        or document.get("schema") != _REVIEW_SCHEMA
        or not hmac.compare_digest(str(document.get("attestation_hmac") or ""), _review_mac(authority.authority_secret, document))
        or document.get("workspace_root") != str(authority.workspace_root)
        or document.get("workflow_root") != identity.workflow_root
        or document.get("identity") != identity.to_dict()
        or document.get("task_claim_sha256") != hashlib.sha256(_canonical(dict(claim))).hexdigest()
        or document.get("accepted_by") != authority.controller_id
        or document.get("lease") != {
            "controller_id": authority.controller_id,
            "continuity_id": authority.lease.continuity_id,
            "lease_epoch": authority.lease.epoch,
            "lease_token_scope": _scope("lease-token", authority.lease.token),
        }
        or document.get("source") != source
        or document.get("preflight_sha256") != preflight
        or document.get("authority_key_id") != hashlib.sha256(authority.authority_secret.encode()).hexdigest()[:24]
        or not valid_reviews
    ):
        raise CopilotSDKError("Copilot accepted-review attestation is missing, stale, or unauthenticated")
    for review in reviews:
        if _read_review_artifact(Path(review["artifact_path"]), authority.workspace_root) != review["artifact_sha256"]:
            raise CopilotSDKError("accepted review artifact changed after controller judgement")
    return hashlib.sha256(_canonical(document)).hexdigest()


def _verify_review_attestation(path: Path, authority: _ControllerAuthority,
                               identity: CopilotLaunchIdentity, claim: Mapping[str, str], *,
                               allow_reserved: bool = False) -> str:
    with authority.controller.fence(authority.lease) as current:
        if current.token != authority.lease.token or current.epoch != authority.lease.epoch:
            raise CopilotSDKError("Copilot review attestation lease changed")
        return _verify_review_attestation_locked(
            path, authority, identity, claim, allow_reserved=allow_reserved,
        )


def _manifest_path(path: Path, workspace_root: Path, *, must_exist: bool) -> Path:
    candidate = _strict_path(path, must_exist=must_exist, name="proof manifest")
    if candidate == workspace_root or candidate in workspace_root.parents or workspace_root in candidate.parents:
        raise CopilotSDKError("Copilot proof manifest must be separate from the worker workspace")
    _private_directory(candidate.parent, workspace_root=workspace_root)
    if must_exist:
        _private_json(candidate, workspace_root=workspace_root, immutable=True)
    elif candidate.exists() or candidate.is_symlink():
        raise CopilotSDKError("Copilot proof manifest is immutable and cannot be overwritten")
    return candidate


def _review_attestation_path(path: Path, workspace_root: Path, *, must_exist: bool) -> Path:
    candidate = _strict_path(path, must_exist=must_exist, name="accepted-review attestation")
    if candidate == workspace_root or candidate in workspace_root.parents or workspace_root in candidate.parents:
        raise CopilotSDKError("Copilot review attestation must be outside the worker workspace")
    _private_directory(candidate.parent, workspace_root=workspace_root)
    if must_exist:
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise CopilotSDKError("Copilot review attestation is unavailable") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or not _owned_by_current_user(info) or info.st_nlink != 1 or info.st_size > 16_384):
            raise CopilotSDKError("Copilot review attestation is not a private controller file")
    elif candidate.exists() or candidate.is_symlink():
        raise CopilotSDKError("Copilot review attestation is write-once and cannot be overwritten")
    return candidate


def _write_controller_attestation(path: Path, document: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise CopilotSDKError("Copilot review attestation permissions changed during issuance")
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def issue_copilot_review_attestation(
    path: Path,
    *,
    workspace_root: Path,
    workflow_root: str,
    task_id: str,
    claim_id: str,
    launch_id: str,
    role: str,
    requested_model: str,
    effort: str,
    resume_proof: str,
    accepted_by: str,
    reviews: Sequence[CopilotReviewJudgement],
) -> Path:
    """Controller-only write-once record of two explicit accepted review judgements.

    This API signs the controller's disposition. It does not claim reviewer packets
    are provider-signed or infer acceptance from imported metadata.
    """
    authority = _load_controller_authority(workspace_root, workflow_root, resume_proof=resume_proof)
    if accepted_by != authority.controller_id or not isinstance(reviews, Sequence) or len(reviews) != 2:
        raise CopilotSDKError("review issuance requires an explicit judgement from the leased controller")
    review_rows: list[dict[str, str]] = []
    for review in reviews:
        if not isinstance(review, CopilotReviewJudgement):
            raise CopilotSDKError("review dispositions must use the controller judgement type")
        if (not isinstance(review.reviewer, str) or not review.reviewer or len(review.reviewer) > 256
                or review.reviewer == accepted_by or review.disposition != "accepted"
                or not _is_sha256(review.artifact_sha256)):
            raise CopilotSDKError("both independent review artifacts must have accepted, pinned digests")
        artifact_path = _strict_path(review.artifact_path, must_exist=True, name="review artifact")
        actual_digest = _read_review_artifact(artifact_path, authority.workspace_root)
        if actual_digest != review.artifact_sha256:
            raise CopilotSDKError("controller judgement digest does not match its review artifact")
        review_rows.append({
            "reviewer": review.reviewer,
            "artifact_path": str(artifact_path),
            "artifact_sha256": review.artifact_sha256,
            "disposition": review.disposition,
        })
    if (review_rows[0]["reviewer"] == review_rows[1]["reviewer"]
            or review_rows[0]["artifact_sha256"] == review_rows[1]["artifact_sha256"]):
        raise CopilotSDKError("accepted review judgements require distinct reviewers and artifacts")
    output = _review_attestation_path(path, authority.workspace_root, must_exist=False)
    try:
        identity = CopilotLaunchIdentity(
            workflow_root=workflow_root, task_id=task_id, claim_id=claim_id,
            lease_epoch=authority.lease.epoch, continuity_id=authority.lease.continuity_id,
            launch_id=launch_id, role=role, requested_model=requested_model, effort=effort,
        )
    except (TypeError, ValueError) as exc:
        raise CopilotSDKError("accepted-review launch identity is malformed") from exc
    with authority.controller.fence(authority.lease) as current:
        if (current.root != str(authority.workspace_root)
                or current.epoch != authority.lease.epoch
                or current.continuity_id != authority.lease.continuity_id
                or current.token != authority.lease.token):
            raise CopilotSDKError("accepted-review controller lease changed before issuance")
        claim, phase = _controller_claim_preflight(authority, identity)
        for judgement in reviews:
            if _read_review_artifact(judgement.artifact_path, authority.workspace_root) != judgement.artifact_sha256:
                raise CopilotSDKError("accepted review artifact changed before controller attestation")
        source = _source_fingerprint()
        preflight = _review_preflight_digest(
            authority, identity, claim, source, phase, allow_reserved=False,
        )
        document: dict[str, Any] = {
            "schema": _REVIEW_SCHEMA,
            "workspace_root": str(authority.workspace_root),
            "workflow_root": workflow_root,
            "identity": identity.to_dict(),
            "task_claim_sha256": hashlib.sha256(_canonical(claim)).hexdigest(),
            "source": source,
            "preflight_sha256": preflight,
            "lease": {
                "controller_id": authority.controller_id,
                "continuity_id": current.continuity_id,
                "lease_epoch": current.epoch,
                "lease_token_scope": _scope("lease-token", current.token),
            },
            "accepted_by": accepted_by,
            "reviews": review_rows,
            "authority_key_id": hashlib.sha256(authority.authority_secret.encode()).hexdigest()[:24],
        }
        document["attestation_hmac"] = _review_mac(authority.authority_secret, document)
        _write_controller_attestation(output, document)
    return output


def _write_immutable_manifest(path: Path, document: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o400)
    try:
        payload = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o400)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(path).unlink(missing_ok=True)
        raise


def issue_copilot_proof_manifest(
    path: Path,
    *,
    workspace_root: Path,
    workflow_root: str,
    task_id: str,
    claim_id: str,
    launch_id: str,
    role: str,
    requested_model: str,
    effort: str,
    review_attestation_path: Path,
) -> AuthenticatedCopilotProofManifest:
    """Reserve one existing protected task attempt and issue a signed, write-once manifest."""
    authority = _load_controller_authority(workspace_root, workflow_root)
    identity = CopilotLaunchIdentity(
        workflow_root=workflow_root, task_id=task_id, claim_id=claim_id,
        lease_epoch=authority.lease.epoch, continuity_id=authority.lease.continuity_id,
        launch_id=launch_id, role=role, requested_model=requested_model, effort=effort,
    )
    manifest_path = _manifest_path(path, authority.workspace_root, must_exist=False)
    cli = _cli_backend()
    ledger_path = _execution_reservation_path(authority)
    with authority.controller.fence(authority.lease) as current:
        if (current.root != str(authority.workspace_root)
                or current.token != authority.lease.token
                or current.epoch != authority.lease.epoch
                or current.continuity_id != authority.lease.continuity_id):
            raise CopilotSDKError("Copilot proof lease changed before reservation")
        claim, _phase = _controller_claim_preflight(authority, identity)
        source_before = _source_fingerprint()
        review_digest = _verify_review_attestation_locked(
            review_attestation_path, authority, identity, claim,
        )
        try:
            reservation = cli._reserve_execution_attempt(
                authority.workspace_root, workflow_root, task_id,
                limits=cli.execution_limits_backend.ExecutionLimits(_PROOF_DEADLINE_SECONDS, _PROOF_RETRIES),
                policy_max_attempts=_PROOF_MAX_ATTEMPTS,
                authority_secret=authority.authority_secret,
                claim_id=claim_id, lease_id=current.token, launch_id=launch_id,
                controller_id=authority.controller_id,
                continuity_id=current.continuity_id, lease_epoch=current.epoch,
            )
        except Exception as exc:
            raise CopilotSDKError("protected one-attempt Copilot proof budget could not be reserved") from exc
        reservation = dict(reservation)
        expected_launch = {
            "workspace_root": str(authority.workspace_root), "workflow_root": workflow_root,
            "task_id": task_id, "claim_id": claim_id, "lease_id": current.token,
            "launch_id": launch_id, "controller_id": authority.controller_id,
            "continuity_id": current.continuity_id, "lease_epoch": current.epoch,
        }
        if (
            reservation.get("launch_identity") != expected_launch
            or reservation.get("attempt") != 1
            or reservation.get("max_attempts") != 1
            or reservation.get("status") != "active"
            or reservation.get("execution_limits") != {"deadline_seconds": _PROOF_DEADLINE_SECONDS, "max_retries": _PROOF_RETRIES}
            or float(reservation.get("deadline_epoch", 0)) <= time.time()
        ):
            raise CopilotSDKError("protected Copilot proof budget does not match the exact first attempt")
        if _source_fingerprint() != source_before:
            raise CopilotSDKError("Copilot reviewed source changed during protected reservation")
        budget = {
            "deadline_seconds": _PROOF_DEADLINE_SECONDS,
            "retry_count": _PROOF_RETRIES,
            "attempt": reservation["attempt"],
            "max_attempts": reservation["max_attempts"],
            "deadline_epoch": reservation["deadline_epoch"],
        }
        document: dict[str, Any] = {
            "schema": _MANIFEST_SCHEMA,
            "purpose": "copilot-negative-control-proof",
            "workspace_root": str(authority.workspace_root),
            "workflow_root": workflow_root,
            "identity": identity.to_dict(),
            "lease": {
                "controller_id": authority.controller_id,
                "continuity_id": current.continuity_id,
                "lease_epoch": current.epoch,
                "lease_token_scope": _scope("lease-token", current.token),
            },
            "task_claim": dict(claim),
            "review_attestation_path": str(_review_attestation_path(
                review_attestation_path, authority.workspace_root, must_exist=True,
            )),
            "budget": budget,
            "preflight": {
                "controller_status": "passed",
                "task_claim_status": "passed",
                "independent_review_status": "controller-attested",
                "proof_invocation_status": "not_started",
            },
            "review_attestation_digest": review_digest,
            "authority_key_id": hashlib.sha256(authority.authority_secret.encode("utf-8")).hexdigest()[:24],
            "execution_reservation_digest": hashlib.sha256(_canonical(reservation)).hexdigest(),
        }
        document["manifest_hmac"] = _manifest_mac(authority.authority_secret, document)
        if ledger_path != cli._execution_limit_ledger_path(authority.workspace_root, workflow_root):
            raise CopilotSDKError("protected Copilot proof budget came from an unexpected ledger origin")
        _write_immutable_manifest(manifest_path, document)
    return AuthenticatedCopilotProofManifest(
        identity, authority.workspace_root, workflow_root, claim,
        float(budget["deadline_epoch"]), document["execution_reservation_digest"], review_digest,
        source_before, manifest_path, Path(document["review_attestation_path"]), authority,
    )


def load_authenticated_proof_manifest(
    path: Path, *, workspace_root: Path, workflow_root: str,
    verify_only: bool = False,
) -> AuthenticatedCopilotProofManifest:
    """Authenticate a write-once manifest against the canonical lease and protected attempt ledger."""
    authority = _load_controller_authority(workspace_root, workflow_root)
    manifest_path = _manifest_path(path, authority.workspace_root, must_exist=True)
    document = _private_json(manifest_path, workspace_root=authority.workspace_root, immutable=True)
    fields = {
        "schema", "purpose", "workspace_root", "workflow_root", "identity", "lease",
        "task_claim", "review_attestation_path", "budget", "preflight", "authority_key_id",
        "execution_reservation_digest", "review_attestation_digest", "manifest_hmac",
    }
    if set(document) != fields or not hmac.compare_digest(
        str(document.get("manifest_hmac") or ""), _manifest_mac(authority.authority_secret, document),
    ):
        raise CopilotSDKError("Copilot proof manifest authentication failed")
    try:
        identity_data = document["identity"]
        identity = CopilotLaunchIdentity(**identity_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise CopilotSDKError("Copilot proof manifest launch identity is malformed") from exc
    claim = _active_claim(authority, identity.task_id, identity.claim_id)
    attestation_path = Path(str(document.get("review_attestation_path") or ""))
    review_digest = _verify_review_attestation(
        attestation_path, authority, identity, claim, allow_reserved=True,
    )
    review_document = _private_json(
        _review_attestation_path(attestation_path, authority.workspace_root, must_exist=True),
        workspace_root=authority.workspace_root,
    )
    source_fingerprint = review_document.get("source")
    if (
        hashlib.sha256(_canonical(review_document)).hexdigest() != review_digest
        or not isinstance(source_fingerprint, Mapping)
        or _source_fingerprint() != dict(source_fingerprint)
    ):
        raise CopilotSDKError("Copilot reviewed source changed while loading the proof manifest")
    lease = document.get("lease")
    budget = document.get("budget")
    preflight = document.get("preflight")
    expected_lease = {
        "controller_id": authority.controller_id,
        "continuity_id": authority.lease.continuity_id,
        "lease_epoch": authority.lease.epoch,
        "lease_token_scope": _scope("lease-token", authority.lease.token),
    }
    expected_budget_fields = {
        "deadline_seconds": _PROOF_DEADLINE_SECONDS, "retry_count": _PROOF_RETRIES,
        "attempt": 1, "max_attempts": 1,
    }
    if (
        document.get("schema") != _MANIFEST_SCHEMA
        or document.get("purpose") != "copilot-negative-control-proof"
        or document.get("workspace_root") != str(authority.workspace_root)
        or document.get("workflow_root") != workflow_root
        or identity.workflow_root != workflow_root
        or identity.lease_epoch != authority.lease.epoch
        or identity.continuity_id != authority.lease.continuity_id
        or dict(lease) != expected_lease
        or dict(document.get("task_claim") or {}) != dict(claim)
        or not isinstance(budget, Mapping)
        or set(budget) != set(expected_budget_fields) | {"deadline_epoch"}
        or any(budget.get(key) != value for key, value in expected_budget_fields.items())
        or isinstance(budget.get("deadline_epoch"), bool)
        or not isinstance(budget.get("deadline_epoch"), (int, float))
        or not verify_only and float(budget["deadline_epoch"]) <= time.time()
        or not isinstance(preflight, Mapping)
        or dict(preflight) != {
            "controller_status": "passed", "task_claim_status": "passed",
            "independent_review_status": "controller-attested", "proof_invocation_status": "not_started",
        }
        or document.get("review_attestation_digest") != review_digest
        or document.get("authority_key_id") != hashlib.sha256(authority.authority_secret.encode("utf-8")).hexdigest()[:24]
    ):
        raise CopilotSDKError("Copilot proof manifest does not match the current lease, claim, or preflight")
    cli = _cli_backend()
    ledger_path = _execution_reservation_path(authority)
    reservation = cli._read_any_execution_ledger_entry(
        authority.workspace_root, workflow_root, identity.task_id,
        authority_secret=authority.authority_secret,
    )
    expected_launch = {
        "workspace_root": str(authority.workspace_root), "workflow_root": workflow_root,
        "task_id": identity.task_id, "claim_id": identity.claim_id,
        "lease_id": authority.lease.token, "launch_id": identity.launch_id,
        "controller_id": authority.controller_id,
        "continuity_id": authority.lease.continuity_id,
        "lease_epoch": authority.lease.epoch,
    }
    if (
        reservation is None
        or reservation.get("launch_identity") != expected_launch
        or reservation.get("execution_limits") != {"deadline_seconds": _PROOF_DEADLINE_SECONDS, "max_retries": _PROOF_RETRIES}
        or reservation.get("attempt") != 1
        or reservation.get("max_attempts") != 1
        or reservation.get("status") != "active"
        or reservation.get("deadline_epoch") != budget.get("deadline_epoch")
        or hashlib.sha256(_canonical(dict(reservation))).hexdigest() != document.get("execution_reservation_digest")
        or ledger_path != cli._execution_limit_ledger_path(authority.workspace_root, workflow_root)
    ):
        raise CopilotSDKError("Copilot proof manifest does not match its protected first-attempt budget")
    return AuthenticatedCopilotProofManifest(
        identity, authority.workspace_root, workflow_root, dict(claim),
        float(budget["deadline_epoch"]), str(document["execution_reservation_digest"]), review_digest,
        dict(source_fingerprint), manifest_path, attestation_path, authority,
    )


def copilot_proof_storage_directory(context: AuthenticatedCopilotProofManifest) -> Path:
    authority = context.authority
    workflow_scope = hashlib.sha256(context.workflow_root.encode("utf-8")).hexdigest()[:24]
    launch_scope = hashlib.sha256(context.identity.launch_id.encode("utf-8")).hexdigest()[:24]
    return authority.credential_path.parent / "copilot-proofs" / workflow_scope / launch_scope


def _create_private_storage_tree(path: Path, authority: _ControllerAuthority) -> Path:
    base = authority.credential_path.parent
    relative = path.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _private_directory(current, workspace_root=authority.workspace_root)
        else:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
    return _private_directory(path, workspace_root=authority.workspace_root)


@dataclass(frozen=True)
class CopilotControllerCheckpoint:
    """Authenticated high-water mark saved outside the ledger database."""

    head: CopilotLedgerHead
    identity_digest: str
    workspace_scope: str
    session_scope: str


class SQLiteCopilotControllerStore:
    """Atomically commit ledger rows and a separate authenticated head record.

    Both files must be private, controller-owned, and outside the worker
    workspace. The authority key is supplied by the caller and is never
    persisted. SQLite's rollback-journal super-journal coordinates writes to
    the two files in one transaction. This does not establish a same-UID
    security boundary; callers must keep admission disabled unless an
    independently enforced worker boundary protects both files and the key.
    """

    def __init__(
        self,
        ledger_path: Path,
        authority_path: Path,
        *,
        workspace_root: Path,
        identity: CopilotLaunchIdentity,
        authority_key: bytes,
        readonly: bool = False,
    ) -> None:
        if not isinstance(identity, CopilotLaunchIdentity):
            raise CopilotSDKError("controller Copilot launch identity is unavailable")
        if not isinstance(authority_key, bytes) or len(authority_key) < 32:
            raise CopilotSDKError("controller Copilot checkpoint key is unavailable")
        try:
            self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
            self.identity = identity
            self.readonly = bool(readonly)
            self.identity_digest = _identity_digest(identity)
            self.workspace_scope = _workspace_scope(self.workspace_root)
            self.anchor_id = _scope("protected-ledger", self.identity_digest)
            self._authority_key = authority_key
            self.ledger_path = self._private_database_path(ledger_path, "ledger")
            self.authority_path = self._private_database_path(authority_path, "authority")
            if self.ledger_path == self.authority_path:
                raise CopilotSDKError("Copilot ledger and authority stores must be separate files")
            if not self.readonly:
                self._initialize()
        except CopilotSDKError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise CopilotSDKError("Copilot controller continuity storage is unavailable") from exc

    def _private_database_path(self, path: Path, name: str) -> Path:
        candidate = _strict_path(path, must_exist=self.readonly, name=f"{name} store")
        parent = _private_directory(candidate.parent, workspace_root=self.workspace_root)
        resolved = parent / candidate.name
        if (
            resolved == self.workspace_root
            or resolved in self.workspace_root.parents
            or self.workspace_root in resolved.parents
        ):
            raise CopilotSDKError("Copilot controller stores must be separate from the worker workspace")
        if resolved.exists() or resolved.is_symlink():
            info = resolved.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or not _owned_by_current_user(info)
                or info.st_nlink != 1
            ):
                raise CopilotSDKError(f"Copilot controller {name} store is not private and owned")
        else:
            if self.readonly:
                raise CopilotSDKError(f"Copilot controller {name} store is unavailable for verification")
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(resolved, flags, 0o600)
            os.close(descriptor)
        return resolved

    def _connect_ledger(self) -> sqlite3.Connection:
        if self.readonly:
            connection = sqlite3.connect(
                f"{self.ledger_path.as_uri()}?mode=ro", timeout=10, isolation_level=None, uri=True,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.ledger_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        if not self.readonly:
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _connect_atomic(self) -> sqlite3.Connection:
        connection = self._connect_ledger()
        try:
            authority_uri = (
                f"{self.authority_path.as_uri()}?mode=ro" if self.readonly else str(self.authority_path)
            )
            connection.execute("ATTACH DATABASE ? AS authority", (authority_uri,))
            if not self.readonly:
                for schema in ("main", "authority"):
                    mode = connection.execute(f"PRAGMA {schema}.journal_mode = DELETE").fetchone()
                    if mode is None or mode[0].lower() != "delete":
                        raise CopilotSDKError("Copilot controller stores cannot enable atomic rollback journaling")
                    connection.execute(f"PRAGMA {schema}.synchronous = FULL")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self) -> None:
        with closing(self._connect_atomic()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS copilot_ledger_head ("
                "anchor_id TEXT PRIMARY KEY, identity_digest TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, signature TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS copilot_ledger_row ("
                "anchor_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(anchor_id, sequence), "
                "FOREIGN KEY(anchor_id) REFERENCES copilot_ledger_head(anchor_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS authority.copilot_expected_head ("
                "anchor_id TEXT PRIMARY KEY, identity_digest TEXT NOT NULL, "
                "workspace_scope TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "signature TEXT NOT NULL, session_scope TEXT NOT NULL, mac TEXT NOT NULL)"
            )
        os.chmod(self.ledger_path, 0o600)
        os.chmod(self.authority_path, 0o600)

    def _read_checkpoint_row(self, connection: sqlite3.Connection, anchor_id: str) -> CopilotControllerCheckpoint | None:
        saved = connection.execute(
            "SELECT identity_digest, workspace_scope, sequence, signature, session_scope, mac "
            "FROM authority.copilot_expected_head WHERE anchor_id = ?",
            (anchor_id,),
        ).fetchone()
        if saved is None:
            return None
        identity_digest, workspace_scope, sequence, signature, session_scope, supplied_mac = saved
        try:
            head = CopilotLedgerHead(identity_digest, sequence, signature)
        except CopilotSDKError as exc:
            raise CopilotSDKError("Copilot controller expected head is malformed") from exc
        body = self._checkpoint_body(anchor_id, head, workspace_scope, session_scope)
        expected_mac = hmac.new(self._authority_key, _CHECKPOINT_DOMAIN + _canonical(body), hashlib.sha256).hexdigest()
        if (
            identity_digest != self.identity_digest
            or workspace_scope != self.workspace_scope
            or not self._valid_session_scope(session_scope, allow_empty=True)
            or not isinstance(supplied_mac, str)
            or not hmac.compare_digest(supplied_mac, expected_mac)
        ):
            raise CopilotSDKError("Copilot controller expected head authentication failed")
        return CopilotControllerCheckpoint(head, identity_digest, workspace_scope, session_scope)

    @staticmethod
    def _valid_session_scope(value: object, *, allow_empty: bool = False) -> bool:
        return (
            isinstance(value, str)
            and (allow_empty and value == "" or len(value) == 64)
            and all(character in "0123456789abcdef" for character in value)
        )

    def _checkpoint_body(
        self,
        anchor_id: str,
        head: CopilotLedgerHead,
        workspace_scope: str,
        session_scope: str,
    ) -> dict[str, Any]:
        return {
            "anchor_id": anchor_id,
            "identity_digest": head.identity_digest,
            "workspace_scope": workspace_scope,
            "sequence": head.sequence,
            "signature": head.signature,
            "session_scope": session_scope,
        }

    def read_expected_checkpoint(self) -> CopilotControllerCheckpoint | None:
        try:
            with closing(self._connect_atomic()) as connection:
                connection.execute("BEGIN")
                checkpoint = self._read_checkpoint_row(connection, self.anchor_id)
                connection.commit()
                return checkpoint
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CopilotSDKError("Copilot controller expected head could not be read") from exc

    def read_ledger(self, anchor_id: str) -> CopilotLedgerSnapshot | None:
        try:
            with closing(self._connect_ledger()) as connection:
                connection.execute("BEGIN")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature FROM copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                if saved is None:
                    stray = connection.execute(
                        "SELECT 1 FROM copilot_ledger_row WHERE anchor_id = ? LIMIT 1", (anchor_id,),
                    ).fetchone()
                    connection.commit()
                    if stray is not None:
                        raise CopilotSDKError("Copilot controller ledger has rows without a head")
                    return None
                rows = connection.execute(
                    "SELECT payload FROM copilot_ledger_row WHERE anchor_id = ? ORDER BY sequence", (anchor_id,),
                ).fetchall()
                connection.commit()
            parsed = tuple(json.loads(row[0]) for row in rows)
            if any(not isinstance(row, Mapping) for row in parsed):
                raise CopilotSDKError("Copilot controller ledger has malformed rows")
            return CopilotLedgerSnapshot(CopilotLedgerHead(*saved), parsed)
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CopilotSDKError("Copilot controller ledger could not be read") from exc

    def assert_unused(self) -> None:
        snapshot = self.read_ledger(self.anchor_id)
        checkpoint = self.read_expected_checkpoint()
        if snapshot is not None or checkpoint is not None:
            raise CopilotSDKError("Copilot proof launch was already used; resume is unsupported")

    def compare_and_append(
        self,
        anchor_id: str,
        expected: CopilotLedgerHead | None,
        signed_row: Mapping[str, Any],
        replacement: CopilotLedgerHead,
    ) -> bool:
        if self.readonly:
            raise CopilotSDKError("Copilot verify-only storage cannot accept ledger writes")
        row = dict(signed_row)
        try:
            with closing(self._connect_atomic()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature FROM main.copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                current = None if saved is None else CopilotLedgerHead(*saved)
                checkpoint = self._read_checkpoint_row(connection, anchor_id)
                if current != expected:
                    connection.rollback()
                    return False
                if (current is None) != (checkpoint is None):
                    raise CopilotSDKError("Copilot controller expected head is missing or orphaned")
                if checkpoint is not None and checkpoint.head != current:
                    raise CopilotSDKError("Copilot controller expected head differs from the ledger")
                expected_sequence = 0 if current is None else current.sequence + 1
                expected_previous = "0" * 64 if current is None else current.signature
                if (
                    anchor_id != self.anchor_id
                    or row.get("sequence") != expected_sequence
                    or row.get("previous_signature") != expected_previous
                    or row.get("signature") != replacement.signature
                    or replacement.identity_digest != self.identity_digest
                    or replacement.sequence != expected_sequence
                    or current is not None and replacement.identity_digest != current.identity_digest
                ):
                    connection.rollback()
                    return False
                session_scope = "" if checkpoint is None else checkpoint.session_scope
                row_session_scope = row.get("session_scope")
                if row.get("kind") == "launch":
                    if not self._valid_session_scope(row_session_scope) or session_scope:
                        connection.rollback()
                        return False
                    session_scope = row_session_scope
                elif isinstance(row_session_scope, str) and row_session_scope:
                    if not self._valid_session_scope(row_session_scope) or session_scope != row_session_scope:
                        connection.rollback()
                        return False
                if current is None:
                    connection.execute(
                        "INSERT INTO main.copilot_ledger_head(anchor_id, identity_digest, sequence, signature) "
                        "VALUES (?, ?, ?, ?)",
                        (anchor_id, replacement.identity_digest, replacement.sequence, replacement.signature),
                    )
                else:
                    connection.execute(
                        "UPDATE main.copilot_ledger_head SET sequence = ?, signature = ? "
                        "WHERE anchor_id = ? AND identity_digest = ? AND sequence = ? AND signature = ?",
                        (replacement.sequence, replacement.signature, anchor_id,
                         current.identity_digest, current.sequence, current.signature),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        connection.rollback()
                        return False
                connection.execute(
                    "INSERT INTO main.copilot_ledger_row(anchor_id, sequence, payload) VALUES (?, ?, ?)",
                    (anchor_id, expected_sequence, json.dumps(row, sort_keys=True, separators=(",", ":"))),
                )
                body = self._checkpoint_body(anchor_id, replacement, self.workspace_scope, session_scope)
                mac = hmac.new(
                    self._authority_key, _CHECKPOINT_DOMAIN + _canonical(body), hashlib.sha256,
                ).hexdigest()
                connection.execute(
                    "INSERT INTO authority.copilot_expected_head "
                    "(anchor_id, identity_digest, workspace_scope, sequence, signature, session_scope, mac) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(anchor_id) DO UPDATE SET identity_digest=excluded.identity_digest, "
                    "workspace_scope=excluded.workspace_scope, sequence=excluded.sequence, "
                    "signature=excluded.signature, session_scope=excluded.session_scope, mac=excluded.mac",
                    (anchor_id, replacement.identity_digest, self.workspace_scope,
                     replacement.sequence, replacement.signature, session_scope, mac),
                )
                connection.commit()
            return True
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CopilotSDKError("Copilot controller ledger and expected head could not commit atomically") from exc


class CopilotControllerContinuity:
    """Bind a proof to a canonical credential, current lease, and signed manifest."""

    def __init__(
        self,
        context: AuthenticatedCopilotProofManifest,
        *,
        readonly: bool = False,
    ) -> None:
        if not isinstance(context, AuthenticatedCopilotProofManifest):
            raise CopilotSDKError("Copilot proof requires an authenticated controller manifest")
        authority = context.authority
        self.context = context
        self.identity = context.identity
        workspace = context.workspace_root
        workspace_scope = _workspace_scope(workspace)
        key_context = _canonical({
            "identity": context.identity.to_dict(),
            "workspace_scope": workspace_scope,
            "execution_reservation_digest": context.execution_reservation_digest,
        })
        checkpoint_key = hmac.new(
            authority.authority_secret.encode("utf-8"), _CHECKPOINT_DOMAIN + key_context, hashlib.sha256,
        ).digest()
        self.evidence_key = derive_evidence_key(
            authority.authority_secret, context.identity,
            run_nonce=context.execution_reservation_digest,
        )
        storage = copilot_proof_storage_directory(context)
        if readonly:
            _private_directory(storage, workspace_root=workspace)
        else:
            _create_private_storage_tree(storage, authority)
        self.store = SQLiteCopilotControllerStore(
            storage / "copilot-ledger.sqlite3",
            storage / "expected-head.sqlite3",
            workspace_root=workspace,
            identity=context.identity,
            authority_key=checkpoint_key,
            readonly=readonly,
        )

    def new_ledger(self) -> ProtectedCopilotLedger:
        if self.store.readonly:
            raise CopilotSDKError("Copilot verify-only storage cannot start a proof")
        self.store.assert_unused()
        return ProtectedCopilotLedger(
            identity=self.identity,
            evidence_key=self.evidence_key,
            anchor_store=self.store,
        )

    def verify_existing(self) -> tuple[Mapping[str, Any], ...]:
        checkpoint = self.store.read_expected_checkpoint()
        if checkpoint is None:
            raise CopilotSDKError("Copilot controller expected head is missing")
        rows = ProtectedCopilotLedger.verify_existing(
            identity=self.identity,
            evidence_key=self.evidence_key,
            anchor_store=self.store,
            expected_head=checkpoint.head,
        )
        launch_rows = [row for row in rows if row.get("kind") == "launch"]
        if len(launch_rows) != 1 or launch_rows[0].get("session_scope") != checkpoint.session_scope:
            raise CopilotSDKError("Copilot controller checkpoint belongs to another native session")
        end_rows = [row for row in rows if row.get("kind") == "end"]
        if (
            not end_rows
            or rows[-1].get("kind") != "end"
            or rows[-1].get("request_completed") is not True
            or rows[-1].get("reason_codes") != []
            or rows[-1].get("usage_count", 0) < 1
        ):
            raise CopilotSDKError("Copilot proof is incomplete or did not verify")
        return rows

    async def run_proof(
        self,
        *,
        base_directory: Path,
        github_token: str,
    ) -> CopilotUsageReport:
        """Run exactly one approved no-tools turn and verify its durable checkpoint."""
        if self.store.readonly:
            raise CopilotSDKError("Copilot verify-only context cannot start a proof")
        if self.context.deadline_epoch <= time.time():
            raise CopilotSDKError("Copilot proof first-attempt deadline expired; do not retry")
        latest = load_authenticated_proof_manifest(
            self.context.manifest_path,
            workspace_root=self.context.workspace_root,
            workflow_root=self.context.workflow_root,
        )
        if (
            latest.identity != self.context.identity
            or latest.execution_reservation_digest != self.context.execution_reservation_digest
            or latest.review_attestation_digest != self.context.review_attestation_digest
            or latest.deadline_epoch != self.context.deadline_epoch
        ):
            raise CopilotSDKError("Copilot proof authority changed after credential entry")
        ledger = self.new_ledger()
        authority = self.context.authority
        with authority.controller.fence(authority.lease) as current:
            if (current.root != str(authority.workspace_root)
                    or current.token != authority.lease.token
                    or current.epoch != authority.lease.epoch
                    or current.continuity_id != authority.lease.continuity_id):
                raise CopilotSDKError("Copilot lease changed before provider startup")
            claim, _phase = _controller_claim_preflight(authority, self.identity)
            if dict(claim) != dict(self.context.task_claim):
                raise CopilotSDKError("Copilot exact task claim changed before provider startup")
            review_digest = _verify_review_attestation_locked(
                self.context.review_attestation_path, authority, self.identity, claim,
                allow_reserved=True,
            )
            if review_digest != self.context.review_attestation_digest:
                raise CopilotSDKError("Copilot accepted-review authority changed before provider startup")
            if _source_fingerprint() != dict(self.context.source_fingerprint):
                raise CopilotSDKError("Copilot reviewed source changed before provider startup")
            sdk_identity = self.context.source_fingerprint.get("sdk", {}).get("import_identity")
            verify_sdk_import_identity(sdk_identity, require_clean=True)
            cli = _cli_backend()
            reservation = cli._read_any_execution_ledger_entry(
                authority.workspace_root, authority.workflow_root, self.identity.task_id,
                authority_secret=authority.authority_secret,
            )
            expected_launch = {
                "workspace_root": str(authority.workspace_root),
                "workflow_root": authority.workflow_root,
                "task_id": self.identity.task_id,
                "claim_id": self.identity.claim_id,
                "lease_id": current.token,
                "launch_id": self.identity.launch_id,
                "controller_id": authority.controller_id,
                "continuity_id": current.continuity_id,
                "lease_epoch": current.epoch,
            }
            if (
                reservation is None
                or reservation.get("launch_identity") != expected_launch
                or reservation.get("attempt") != 1
                or reservation.get("max_attempts") != 1
                or reservation.get("status") != "active"
                or reservation.get("execution_limits") != {"deadline_seconds": _PROOF_DEADLINE_SECONDS, "max_retries": _PROOF_RETRIES}
                or reservation.get("deadline_epoch") != self.context.deadline_epoch
                or hashlib.sha256(_canonical(dict(reservation))).hexdigest()
                != self.context.execution_reservation_digest
                or self.context.deadline_epoch <= time.time()
            ):
                raise CopilotSDKError("Copilot protected first-attempt authority changed before startup")
            async with await CopilotProofRun.open(
                identity=self.identity,
                evidence_key=self.evidence_key,
                protected_ledger=ledger,
                base_directory=base_directory,
                workspace_root=self.store.workspace_root,
                github_token=github_token,
                sdk_identity=sdk_identity,
                deadline_epoch=self.context.deadline_epoch,
            ) as run:
                report = await run.send_once(COPILOT_NEGATIVE_CONTROL_PROMPT)
        if not report.verified or report.persistent_admission:
            raise CopilotSDKError("Copilot proof did not meet its no-tools verification checks")
        rows = self.verify_existing()
        if list(rows) != list(report.ledger):
            raise CopilotSDKError("Copilot proof report differs from its authenticated controller ledger")
        return report


__all__ = [
    "AuthenticatedCopilotProofManifest",
    "CopilotReviewJudgement",
    "CopilotControllerCheckpoint",
    "CopilotControllerContinuity",
    "SQLiteCopilotControllerStore",
    "copilot_proof_storage_directory",
    "issue_copilot_proof_manifest",
    "issue_copilot_review_attestation",
    "load_authenticated_proof_manifest",
]
