#!/usr/bin/env python3
"""Prepare, verify, or run one controller-authorized Copilot SDK proof."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.copilot_controller import (  # noqa: E402
    CopilotControllerContinuity,
    issue_copilot_proof_manifest,
    load_authenticated_proof_manifest,
)
from agentflow.copilot_sdk import CopilotSDKError, verify_sdk_import_identity  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workflow-root", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-manifest", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--task-id")
    parser.add_argument("--claim-id")
    parser.add_argument("--launch-id")
    parser.add_argument("--role")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--review-attestation", type=Path,
                        help="controller-signed accepted-review attestation (protected, immutable file)")
    parser.add_argument(
        "--credential-fd", type=int,
        help="read a provider credential from an inherited pipe, socket, or tty file descriptor",
    )
    return parser


def _provider_credential(fd: int | None) -> str:
    if fd is not None:
        try:
            info = os.fstat(fd)
            if not (stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode) or os.isatty(fd)):
                raise CopilotSDKError("provider credential FD must be a protected pipe, socket, or tty")
            chunks: list[bytes] = []
            total = 0
            while total <= 4096:
                chunk = os.read(fd, min(4097 - total, 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            if len(raw) > 4096:
                raise CopilotSDKError("provider credential FD exceeded its size limit")
            value = raw.decode("utf-8").rstrip("\r\n")
        except CopilotSDKError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise CopilotSDKError("provider credential FD could not be read safely") from exc
    else:
        if os.name == "nt" or not sys.stdin.isatty() or not hasattr(getpass, "unix_getpass"):
            raise CopilotSDKError("provider credential requires a no-echo terminal or protected FD")
        try:
            import termios
            descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
            try:
                termios.tcgetattr(descriptor)
            finally:
                os.close(descriptor)
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.unix_getpass("Copilot-compatible GitHub credential: ")
        except CopilotSDKError:
            raise
        except Exception as exc:
            raise CopilotSDKError("provider credential requires a working no-echo terminal or protected FD") from exc
    if not value or "\x00" in value:
        raise CopilotSDKError("provider credential is empty or malformed")
    return value


def _prepare(args: argparse.Namespace) -> int:
    required = (args.task_id, args.claim_id, args.launch_id, args.role, args.model, args.effort)
    if not all(required) or args.review_attestation is None:
        raise CopilotSDKError("manifest preparation requires an exact claimed task and controller-signed review attestation")
    manifest = issue_copilot_proof_manifest(
        args.manifest,
        workspace_root=args.workspace_root,
        workflow_root=args.workflow_root,
        task_id=args.task_id,
        claim_id=args.claim_id,
        launch_id=args.launch_id,
        role=args.role,
        requested_model=args.model,
        effort=args.effort,
        review_attestation_path=args.review_attestation,
    )
    print(json.dumps({
        "manifest_issued": True,
        "launch_id": manifest.identity.launch_id,
        "deadline_epoch": manifest.deadline_epoch,
        "attempt": 1,
        "max_attempts": 1,
        "persistent_admission": False,
    }, sort_keys=True))
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.prepare_manifest:
        return _prepare(args)
    if any((args.task_id, args.claim_id, args.launch_id, args.role, args.model, args.effort,
            args.review_attestation)):
        raise CopilotSDKError("identity fields are accepted only when preparing a new manifest")
    context = load_authenticated_proof_manifest(
        args.manifest, workspace_root=args.workspace_root, workflow_root=args.workflow_root,
        verify_only=args.verify_only,
    )
    if args.verify_only:
        continuity = CopilotControllerContinuity(context, readonly=True)
        rows = continuity.verify_existing()
        print(json.dumps({
            "verified": True,
            "row_count": len(rows),
            "launch_id": context.identity.launch_id,
            "persistent_admission": False,
        }, sort_keys=True))
        return 0

    sdk_identity = context.source_fingerprint.get("sdk", {}).get("import_identity")
    verify_sdk_import_identity(sdk_identity, require_clean=True)
    continuity = CopilotControllerContinuity(context)
    continuity.store.assert_unused()
    github_token = _provider_credential(args.credential_fd)
    try:
        context = load_authenticated_proof_manifest(
            args.manifest, workspace_root=args.workspace_root, workflow_root=args.workflow_root,
        )
        continuity = CopilotControllerContinuity(context)
        continuity.store.assert_unused()
        state = continuity.store.ledger_path.parent
        with tempfile.TemporaryDirectory(prefix="copilot-proof-home-", dir=state) as raw_home:
            report = await continuity.run_proof(base_directory=Path(raw_home), github_token=github_token)
    finally:
        del github_token
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (CopilotSDKError, OSError, ValueError, RuntimeError) as exc:
        print(f"Copilot proof runner stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
