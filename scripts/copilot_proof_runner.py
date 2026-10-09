#!/usr/bin/env python3
"""Interactively run or verify one explicitly approved Copilot proof launch."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.copilot_controller import (  # noqa: E402
    CopilotControllerContinuity,
    load_approved_proof_manifest,
)
from agentflow.copilot_sdk import CopilotLaunchIdentity, CopilotSDKError  # noqa: E402


def _private_directory(path: Path, *, workspace_root: Path, create: bool) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise CopilotSDKError("Copilot controller state directory path is invalid")
    unresolved = candidate.resolve(strict=False)
    if (
        unresolved == workspace_root
        or unresolved in workspace_root.parents
        or workspace_root in unresolved.parents
    ):
        raise CopilotSDKError("Copilot controller state directory must be outside the worker workspace")
    if create and not candidate.exists():
        candidate.mkdir(parents=True, mode=0o700)
    resolved = candidate.resolve(strict=True)
    if resolved == workspace_root or resolved in workspace_root.parents or workspace_root in resolved.parents:
        raise CopilotSDKError("Copilot controller state directory must be outside the worker workspace")
    info = resolved.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or hasattr(os, "getuid") and info.st_uid != os.getuid()
    ):
        raise CopilotSDKError("Copilot controller state directory is not private and owned")
    return resolved


def _identity_from_args(args: argparse.Namespace) -> CopilotLaunchIdentity:
    return CopilotLaunchIdentity(
        workflow_root=args.workflow_root,
        task_id=args.task_id,
        claim_id=args.claim_id,
        lease_epoch=args.lease_epoch,
        continuity_id=args.continuity_id,
        launch_id=args.launch_id,
        role=args.role,
        requested_model=args.model,
        effort=args.effort,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--proof-approval", type=Path, required=True)
    parser.add_argument("--workflow-root", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--lease-epoch", type=int, required=True)
    parser.add_argument("--continuity-id", required=True)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify a completed ledger without prompting for a provider credential",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    workspace = args.workspace_root.expanduser().resolve(strict=True)
    identity = _identity_from_args(args)
    load_approved_proof_manifest(
        args.proof_approval,
        identity=identity,
        workspace_root=workspace,
    )
    state = _private_directory(args.state_dir, workspace_root=workspace, create=True)
    ledger_directory = _private_directory(state / "ledger", workspace_root=workspace, create=True)
    authority_directory = _private_directory(state / "authority", workspace_root=workspace, create=True)

    authority_secret = getpass.getpass("Controller authority secret (32+ characters; not stored): ")
    continuity = CopilotControllerContinuity(
        identity=identity,
        authority_secret=authority_secret,
        workspace_root=workspace,
        ledger_path=ledger_directory / "copilot-ledger.sqlite3",
        authority_path=authority_directory / "expected-head.sqlite3",
    )
    del authority_secret

    if args.verify_only:
        rows = continuity.verify_existing()
        print(json.dumps({"verified": True, "row_count": len(rows), "persistent_admission": False}))
        return 0

    continuity.store.assert_unused()
    github_token = getpass.getpass("Copilot-compatible GitHub credential (not stored): ")
    try:
        with tempfile.TemporaryDirectory(prefix="copilot-proof-home-", dir=state) as raw_home:
            report = await continuity.run_proof(base_directory=Path(raw_home), github_token=github_token)
    finally:
        del github_token
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (CopilotSDKError, OSError, ValueError, RuntimeError) as exc:
        print(f"Copilot proof runner stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
