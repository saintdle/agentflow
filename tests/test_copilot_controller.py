from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentflow import cli, copilot_sdk, copilot_controller
from agentflow.controller import RootController
from agentflow.copilot_controller import (
    CopilotReviewJudgement,
    CopilotControllerContinuity,
    issue_copilot_review_attestation,
    issue_copilot_proof_manifest,
    load_authenticated_proof_manifest,
)
import copilot_proof_runner


class CopilotControllerContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.private_state = self.root / "private-state"
        self.private_state.mkdir(mode=0o700)
        os.chmod(self.private_state, 0o700)
        self.env_patch = mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(self.private_state)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workflow_root = "agentflow-k6x"
        self.task_id = "agentflow-k6x.112"
        self.claim_id = "claim-112"
        self.controller_id = "audit-copilot-continuity-writer"
        self.args = SimpleNamespace(
            root=str(self.workspace), workflow_root=self.workflow_root, resume_key_file="",
        )
        self.state_path = cli._controller_state_dir(self.workspace, self.workflow_root) / "state.json"
        self.root_controller = RootController(
            str(self.workspace), self.controller_id, state_path=self.state_path,
            checkpoint_path=self.state_path.with_name("checkpoint.json"),
        )
        self.lease = self.root_controller.acquire()
        self.credential_path, self.credentials = cli._controller_credentials(self.args, self.lease)
        self.root_controller.reserve_active_task({
            "task": self.task_id,
            "root": str(self.workspace),
            "actor": self.controller_id,
            "claim_id": self.claim_id,
            "phase": "fix",
        }, lease=self.lease)
        self.manifest_dir = self.private_state / "manifests"
        self.manifest_dir.mkdir(mode=0o700)
        os.chmod(self.manifest_dir, 0o700)
        self.manifest_path = self.manifest_dir / "copilot-proof.json"
        self.review_path = self.manifest_dir / "accepted-review.json"
        self.review_artifacts = (self.manifest_dir / "review-one.md", self.manifest_dir / "review-two.md")

    def review_judgements(self):
        review_bytes = (b"fixture artifact one", b"fixture artifact two")
        for path, payload in zip(self.review_artifacts, review_bytes):
            path.write_bytes(payload)
            os.chmod(path, 0o600)
        return (
            CopilotReviewJudgement("reviewer-one", self.review_artifacts[0], hashlib.sha256(review_bytes[0]).hexdigest(), "accepted"),
            CopilotReviewJudgement("reviewer-two", self.review_artifacts[1], hashlib.sha256(review_bytes[1]).hexdigest(), "accepted"),
        )

    def write_review_attestation(self):
        return self.attest(path=self.review_path)

    def attest(self, **overrides):
        values = {
            "path": self.review_path,
            "workspace_root": self.workspace,
            "workflow_root": self.workflow_root,
            "task_id": self.task_id,
            "claim_id": self.claim_id,
            "launch_id": "launch-112-proof",
            "role": "proof",
            "requested_model": "claude-sonnet-4.6",
            "effort": "low",
            "resume_proof": self.credentials["resume_secret"],
            "accepted_by": self.controller_id,
            "reviews": self.review_judgements(),
        }
        values.update(overrides)
        return issue_copilot_review_attestation(**values)

    def issue(self, **overrides):
        if not self.review_path.exists():
            self.write_review_attestation()
        values = {
            "path": self.manifest_path,
            "workspace_root": self.workspace,
            "workflow_root": self.workflow_root,
            "task_id": self.task_id,
            "claim_id": self.claim_id,
            "launch_id": "launch-112-proof",
            "role": "proof",
            "requested_model": "claude-sonnet-4.6",
            "effort": "low",
            "review_attestation_path": self.review_path,
        }
        values.update(overrides)
        return issue_copilot_proof_manifest(**values)

    def context(self):
        return load_authenticated_proof_manifest(
            self.manifest_path,
            workspace_root=self.workspace,
            workflow_root=self.workflow_root,
        )

    def continuity(self, *, readonly=False):
        return CopilotControllerContinuity(self.context(), readonly=readonly)

    def _open_collector(self, *, actual_model=None, event_type="assistant.usage"):
        continuity = self.continuity()
        ledger = continuity.new_ledger()
        pin = copilot_sdk.CopilotRuntimePin(
            platform="darwin-arm64", managed_cache_path="/cache",
            executable_path="/cache/copilot-runtime", executable_sha256="a" * 64,
            payload_path="/cache/runtime.node", payload_sha256="b" * 64,
        )
        collector = copilot_sdk.CopilotUsageCollector(
            continuity.identity, evidence_key=continuity.evidence_key,
            runtime_pin=pin, protected_ledger=ledger,
        )

        class Session:
            session_id = "native-session"

            def on(self, _handler):
                return lambda: None

        with mock.patch.object(copilot_sdk, "_verify_runtime_pin"):
            collector.record_runtime_pin()
        collector.record_runtime_status("1.0.93", 3)
        collector.attach(Session())
        collector.record_tool_inventory([])
        collector.begin_request()
        collector.on_event(SimpleNamespace(
            type=event_type, id="event", agent_id=None,
            data=SimpleNamespace(
                model=actual_model or continuity.identity.requested_model,
                api_call_id="call", input_tokens=2, output_tokens=1,
                reasoning_tokens=0, cost=0.01,
            ),
        ))
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        return continuity, ledger, collector

    def complete_proof(self, **kwargs):
        continuity, ledger, collector = self._open_collector(**kwargs)
        return continuity, ledger, collector.report()

    def test_authenticated_manifest_binds_current_lease_claim_and_budget(self) -> None:
        issued = self.issue()
        loaded = self.context()
        self.assertEqual(loaded.identity, issued.identity)
        self.assertEqual(loaded.task_claim["claim_id"], self.claim_id)
        self.assertEqual(loaded.identity.lease_epoch, self.lease.epoch)
        self.assertEqual(loaded.deadline_epoch, issued.deadline_epoch)
        self.assertEqual(self.manifest_path.stat().st_mode & 0o777, 0o400)

        ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
        saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        row = next(iter(saved["entries"].values()))
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["max_attempts"], 1)
        self.assertEqual(row["execution_limits"], {"deadline_seconds": 180, "max_retries": 0})
        self.assertEqual(row["deadline_epoch"], loaded.deadline_epoch)
        self.assertEqual(row["launch_identity"]["claim_id"], self.claim_id)

    def test_unsigned_private_approval_json_and_manifest_tampering_are_rejected(self) -> None:
        self.issue()
        document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        document["preflight"]["proof_invocation_status"] = "started"
        os.chmod(self.manifest_path, 0o600)
        self.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.manifest_path, 0o400)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.context()

        unsigned = self.manifest_dir / "unsigned.json"
        unsigned.write_text(json.dumps({
            "schema": 1, "decision": "approved", "approved_by": "controller",
            "budget": {"deadline_seconds": 180, "retry_count": 0},
        }), encoding="utf-8")
        os.chmod(unsigned, 0o400)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            load_authenticated_proof_manifest(
                unsigned, workspace_root=self.workspace, workflow_root=self.workflow_root,
            )

    def test_canonical_credential_origin_rejects_missing_or_wrong_root_key(self) -> None:
        self.issue()
        alternate_state = self.root / "alternate-state"
        alternate_state.mkdir(mode=0o700)
        os.chmod(alternate_state, 0o700)
        with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(alternate_state)}):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                self.context()

        document = json.loads(self.credential_path.read_text(encoding="utf-8"))
        document["authority_secret"] = "different-controller-authority-secret-at-least-32-chars"
        os.chmod(self.credential_path, 0o600)
        self.credential_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.credential_path, 0o600)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.context()

    def test_review_attestation_tampering_fails_before_budget_reservation(self) -> None:
        self.write_review_attestation()
        document = json.loads(self.review_path.read_text(encoding="utf-8"))
        document["reviews"][0]["disposition"] = "rejected"
        os.chmod(self.review_path, 0o600)
        self.review_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.review_path, 0o600)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.issue()
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_controller_review_issuer_is_write_once_private_and_reserves_nothing(self) -> None:
        ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
        with mock.patch.object(copilot_proof_runner, "_provider_credential", side_effect=AssertionError("prompt")):
            with mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("SDK startup")):
                issued = self.write_review_attestation()
        self.assertEqual(issued, self.review_path)
        self.assertEqual(self.review_path.stat().st_mode & 0o777, 0o600)
        document = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual([row["disposition"] for row in document["reviews"]], ["accepted", "accepted"])
        self.assertEqual(len({row["reviewer"] for row in document["reviews"]}), 2)
        self.assertEqual(len({row["artifact_sha256"] for row in document["reviews"]}), 2)
        self.assertFalse(ledger_path.exists())
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.write_review_attestation()
        self.assertFalse(ledger_path.exists())

    def test_controller_review_issuer_rejects_wrong_resume_identity_disposition_and_digest(self) -> None:
        reviews = self.review_judgements()
        bad_cases = (
            {"resume_proof": "not-the-canonical-resume-proof"},
            {"accepted_by": "different-controller"},
            {"claim_id": "wrong-claim"},
            {"reviews": (reviews[0], CopilotReviewJudgement(
                reviews[1].reviewer, reviews[1].artifact_path, reviews[1].artifact_sha256, "rejected",
            ))},
            {"reviews": (reviews[0], CopilotReviewJudgement(
                reviews[0].reviewer, reviews[1].artifact_path, reviews[1].artifact_sha256, "accepted",
            ))},
            {"reviews": (reviews[0], CopilotReviewJudgement(
                reviews[1].reviewer, reviews[1].artifact_path, "f" * 64, "accepted",
            ))},
        )
        for index, case in enumerate(bad_cases):
            output = self.manifest_dir / f"rejected-{index}.json"
            arguments = {"path": output, "reviews": reviews}
            arguments.update(case)
            with self.subTest(case=index), self.assertRaises(copilot_sdk.CopilotSDKError):
                self.attest(**arguments)
            self.assertFalse(output.exists())
        alternate_state = self.root / "untrusted-state"
        alternate_state.mkdir(mode=0o700)
        os.chmod(alternate_state, 0o700)
        alternate_output = self.manifest_dir / "wrong-key-origin.json"
        with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(alternate_state)}):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                self.attest(path=alternate_output, reviews=reviews)
        self.assertFalse(alternate_output.exists())
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_controller_review_issuer_rejects_unsafe_and_existing_output_without_changes(self) -> None:
        self.review_judgements()
        inside_workspace = self.workspace / "review-attestation.json"
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest(path=inside_workspace)
        existing = self.manifest_dir / "existing.json"
        existing.write_bytes(b"preserve these bytes")
        os.chmod(existing, 0o600)
        before = existing.read_bytes()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest(path=existing)
        self.assertEqual(existing.read_bytes(), before)
        alias = self.manifest_dir / "alias.json"
        alias.symlink_to(existing)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest(path=alias)
        self.assertEqual(existing.read_bytes(), before)

    def test_review_artifact_change_and_source_drift_reject_before_reservation(self) -> None:
        self.write_review_attestation()
        self.review_artifacts[0].write_bytes(b"changed after review")
        os.chmod(self.review_artifacts[0], 0o600)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.issue()
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())
        self.review_path.unlink()
        self.review_artifacts[0].write_bytes(b"fixture artifact one")
        os.chmod(self.review_artifacts[0], 0o600)
        self.write_review_attestation()
        original = copilot_controller._source_fingerprint()
        drifted = {**original, "module_sha256": {**original["module_sha256"], "copilot_sdk.py": "f" * 64}}
        with mock.patch("agentflow.copilot_controller._source_fingerprint", return_value=drifted):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                self.issue()
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_source_fingerprint_binds_installed_authority_and_runner_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "site-packages" / "agentflow"
            package.mkdir(parents=True)
            runner = root / "bin" / "copilot_proof_runner.py"
            runner.parent.mkdir()
            for relative in (
                "src/agentflow/copilot_controller.py", "src/agentflow/copilot_sdk.py",
                "src/agentflow/cli.py", "src/agentflow/controller.py",
                "src/agentflow/checkpoint.py", "src/agentflow/execution_limits.py",
                "scripts/copilot_proof_runner.py",
            ):
                filename = Path(relative).name
                destination = runner if filename == "copilot_proof_runner.py" else package / filename
                source = Path(__file__).resolve().parents[1] / relative
                destination.write_bytes(source.read_bytes())
            with (
                mock.patch.object(copilot_controller, "__file__", str(package / "copilot_controller.py")),
                mock.patch.dict(sys.modules, {"copilot_proof_runner": SimpleNamespace(__file__=str(runner))}),
            ):
                before = copilot_controller._source_fingerprint()
                with (package / "cli.py").open("ab") as handle:
                    handle.write(b"\n# trusted controller helper changed\n")
                after = copilot_controller._source_fingerprint()
            self.assertIsNone(before["source_sha"])
            self.assertEqual(before["artifact_kind"], "installed_runner")
            self.assertIn("runner/copilot_proof_runner.py", before["module_sha256"])
            self.assertNotEqual(before["module_sha256"]["agentflow/cli.py"],
                                after["module_sha256"]["agentflow/cli.py"])
            self.assertNotEqual(before, after)

    def test_lease_cannot_rotate_between_reservation_and_manifest_commit(self) -> None:
        self.write_review_attestation()
        original_reserve = cli._reserve_execution_attempt
        started = threading.Event()
        rotated = threading.Event()
        state = {"manifest_committed_before_rotation": False}

        def rotate_after_release() -> None:
            started.set()
            replacement = RootController(
                str(self.workspace), self.controller_id, state_path=self.state_path,
                checkpoint_path=self.state_path.with_name("checkpoint.json"),
                owner_id="concurrent-reattach",
            )
            replacement.acquire(resume_proof=self.lease.resume_secret)
            state["manifest_committed_before_rotation"] = self.manifest_path.exists()
            rotated.set()

        def reserve_while_fenced(*args, **kwargs):
            worker = threading.Thread(target=rotate_after_release, daemon=True)
            worker.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.03)
            self.assertFalse(rotated.is_set())
            result = original_reserve(*args, **kwargs)
            return result

        with mock.patch.object(cli, "_reserve_execution_attempt", side_effect=reserve_while_fenced):
            self.issue()
        self.assertTrue(rotated.wait(3), "concurrent lease rotation did not resume after manifest commit")
        self.assertTrue(state["manifest_committed_before_rotation"])

    def test_review_issuer_rejects_incompatible_active_or_draining_controller_state(self) -> None:
        self.review_judgements()
        self.root_controller.reserve_active_task({
            "task": "agentflow-k6x.113", "root": str(self.workspace),
            "actor": self.controller_id, "claim_id": "claim-113", "phase": "fix",
        }, lease=self.lease)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest()
        self.assertFalse(self.review_path.exists())
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_review_issuer_rejects_draining_root_without_writes(self) -> None:
        self.review_judgements()
        checkpoint_path = self.state_path.with_name("checkpoint.json")
        self.root_controller.begin_draining("fixture drain", lease=self.lease)
        before = checkpoint_path.read_bytes()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest()
        self.assertFalse(self.review_path.exists())
        self.assertEqual(checkpoint_path.read_bytes(), before)
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_review_issuer_rejects_invalid_global_budget_snapshot_without_changes(self) -> None:
        self.review_judgements()
        ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
        ledger_path.write_text(json.dumps({
            "schema": "agentflow.execution-limit-ledger@1", "entries": {"other-task": {"attempt": 1}},
        }), encoding="utf-8")
        os.chmod(ledger_path, 0o600)
        before = ledger_path.read_bytes()
        output_before = self.review_path.exists()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.attest()
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertEqual(self.review_path.exists(), output_before)

    def test_lease_epoch_change_invalidates_manifest(self) -> None:
        self.issue()
        replacement_controller = RootController(
            str(self.workspace), self.controller_id, state_path=self.state_path,
            checkpoint_path=self.state_path.with_name("checkpoint.json"),
            owner_id="reattached-owner",
        )
        replacement = replacement_controller.acquire(resume_proof=self.lease.resume_secret)
        cli._controller_credentials(self.args, replacement)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.context()

    def test_exact_claim_and_same_workflow_root_are_required_before_budget_reservation(self) -> None:
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.issue(claim_id="some-other-claim")
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            issue_copilot_proof_manifest(
                self.manifest_path, workspace_root=self.workspace,
                workflow_root="different-root", task_id=self.task_id,
                claim_id=self.claim_id, launch_id="launch-other-root", role="proof",
                requested_model="claude-sonnet-4.6", effort="low",
                review_attestation_path=self.review_path,
            )
        self.assertFalse(self.manifest_path.exists())
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

    def test_first_deadline_and_one_attempt_are_never_reset(self) -> None:
        issued = self.issue()
        ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
        before = json.loads(ledger_path.read_text(encoding="utf-8"))
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.issue(path=self.manifest_dir / "second.json", launch_id="launch-112-second")
        after = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(after, before)
        row = next(iter(after["entries"].values()))
        self.assertEqual(row["deadline_epoch"], issued.deadline_epoch)
        self.assertEqual(row["attempt"], 1)

    def test_expired_first_deadline_fails_before_any_proof_startup(self) -> None:
        issued = self.issue()
        self.complete_proof()
        expired_now = issued.deadline_epoch + 1
        with mock.patch("agentflow.copilot_controller.time.time", return_value=expired_now):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                self.context()
        expired_run = SimpleNamespace(
            prepare_manifest=False, verify_only=False, workspace_root=self.workspace,
            workflow_root=self.workflow_root, manifest=self.manifest_path,
            task_id=None, claim_id=None, launch_id=None, role=None, model=None,
            effort=None, review_attestation=None, credential_fd=None,
        )
        with (
            mock.patch("agentflow.copilot_controller.time.time", return_value=expired_now),
            mock.patch.object(copilot_proof_runner, "_provider_credential", side_effect=AssertionError("prompt")),
            mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("SDK startup")),
        ):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                asyncio.run(copilot_proof_runner._run(expired_run))
        with mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("SDK startup")):
            with mock.patch.object(copilot_proof_runner, "_provider_credential", side_effect=AssertionError("prompt")):
                args = SimpleNamespace(
                    prepare_manifest=False, verify_only=True, workspace_root=self.workspace,
                    workflow_root=self.workflow_root, manifest=self.manifest_path,
                    task_id=None, claim_id=None, launch_id=None, role=None, model=None,
                    effort=None, review_attestation=None, credential_fd=None,
                )
                ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
                before = ledger_path.read_bytes()
                with mock.patch("agentflow.copilot_controller.time.time", return_value=expired_now):
                    self.assertEqual(asyncio.run(copilot_proof_runner._run(args)), 0)
                self.assertEqual(ledger_path.read_bytes(), before)

    def test_provider_startup_rechecks_claim_after_credential_read(self) -> None:
        self.issue()
        original_unused = copilot_controller.SQLiteCopilotControllerStore.assert_unused
        state = {"checks": 0, "rotated": False, "started": False}

        def rotate_on_second_check(store):
            original_unused(store)
            state["checks"] += 1
            if state["checks"] == 2:
                replacement = RootController(
                    str(self.workspace), self.controller_id, state_path=self.state_path,
                    checkpoint_path=self.state_path.with_name("checkpoint.json"),
                    owner_id="reattached-before-sdk",
                )
                replacement.acquire(resume_proof=self.lease.resume_secret)
                state["rotated"] = True

        async def observe_open(**_kwargs):
            state["started"] = True
            raise copilot_sdk.CopilotSDKError("fixture must never start SDK")

        args = SimpleNamespace(
            prepare_manifest=False, verify_only=False, workspace_root=self.workspace,
            workflow_root=self.workflow_root, manifest=self.manifest_path,
            task_id=None, claim_id=None, launch_id=None, role=None, model=None,
            effort=None, review_attestation=None, credential_fd=None,
        )
        with (
            mock.patch.object(copilot_controller.SQLiteCopilotControllerStore, "assert_unused", rotate_on_second_check),
            mock.patch.object(copilot_proof_runner, "_provider_credential", return_value="fixture-placeholder"),
            mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=observe_open),
            mock.patch.object(copilot_proof_runner, "verify_sdk_import_identity"),
            mock.patch.object(copilot_controller, "verify_sdk_import_identity"),
        ):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                asyncio.run(copilot_proof_runner._run(args))
        self.assertTrue(state["rotated"])
        self.assertFalse(state["started"])
        ledger = json.loads(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).read_text())
        self.assertEqual(next(iter(ledger["entries"].values()))["attempt"], 1)

    def test_removed_claim_never_reaches_sdk_startup(self) -> None:
        self.issue()
        continuity = self.continuity()
        self.root_controller.complete_active_task(self.task_id, lease=self.lease)
        with mock.patch.object(
            copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("stale claim reached SDK"),
        ):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                asyncio.run(continuity.run_proof(
                    base_directory=self.private_state / "fixture-home",
                    github_token="fixture-placeholder",
                ))

    def test_storage_symlink_case_alias_and_other_workspace_are_rejected(self) -> None:
        self.issue()
        alias = self.manifest_dir / "alias.json"
        alias.symlink_to(self.manifest_path)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            load_authenticated_proof_manifest(
                alias, workspace_root=self.workspace, workflow_root=self.workflow_root,
            )
        case_alias = self.manifest_dir / self.manifest_path.name.upper()
        if case_alias != self.manifest_path:
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                load_authenticated_proof_manifest(
                    case_alias, workspace_root=self.workspace, workflow_root=self.workflow_root,
                )
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            load_authenticated_proof_manifest(
                self.manifest_path, workspace_root=other_workspace, workflow_root=self.workflow_root,
            )

    def test_verified_head_and_rows_survive_read_only_restart(self) -> None:
        context = self.issue()
        continuity, ledger, report = self.complete_proof()
        self.assertTrue(report.verified)
        self.assertFalse(report.persistent_admission)
        checkpoint = continuity.store.read_expected_checkpoint()
        self.assertEqual(checkpoint.head, ledger.head)
        readonly = CopilotControllerContinuity(context, readonly=True)
        files = (readonly.store.ledger_path, readonly.store.authority_path)
        before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in files}
        rows = readonly.verify_existing()
        after = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in files}
        self.assertEqual(before, after)
        self.assertEqual(list(rows), list(report.ledger))

    def test_verify_only_prompts_reserves_and_starts_nothing(self) -> None:
        context = self.issue()
        self.complete_proof()
        ledger_path = cli._execution_limit_ledger_path(self.workspace, self.workflow_root)
        before = ledger_path.read_bytes()
        args = SimpleNamespace(
            prepare_manifest=False, verify_only=True, workspace_root=self.workspace,
            workflow_root=self.workflow_root, manifest=self.manifest_path,
            task_id=None, claim_id=None, launch_id=None, role=None, model=None,
            effort=None, review_attestation=None, credential_fd=None,
        )
        with mock.patch.object(copilot_proof_runner, "_provider_credential", side_effect=AssertionError("prompt")):
            with mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("SDK startup")):
                self.assertEqual(asyncio.run(copilot_proof_runner._run(args)), 0)
        self.assertEqual(ledger_path.read_bytes(), before)

    def test_missing_reviewed_sdk_identity_fails_before_credential_input(self) -> None:
        self.issue()
        args = SimpleNamespace(
            prepare_manifest=False, verify_only=False, workspace_root=self.workspace,
            workflow_root=self.workflow_root, manifest=self.manifest_path,
            task_id=None, claim_id=None, launch_id=None, role=None, model=None,
            effort=None, review_attestation=None, credential_fd=None,
        )
        with mock.patch.object(
            copilot_proof_runner, "_provider_credential", side_effect=AssertionError("credential prompt"),
        ):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                asyncio.run(copilot_proof_runner._run(args))

    def test_preloaded_sdk_injection_fails_before_credential_input(self) -> None:
        self.issue()
        args = SimpleNamespace(
            prepare_manifest=False, verify_only=False, workspace_root=self.workspace,
            workflow_root=self.workflow_root, manifest=self.manifest_path,
            task_id=None, claim_id=None, launch_id=None, role=None, model=None,
            effort=None, review_attestation=None, credential_fd=None,
        )
        with (
            mock.patch.dict(sys.modules, {"copilot": object()}),
            mock.patch.object(
                copilot_proof_runner, "_provider_credential", side_effect=AssertionError("credential prompt"),
            ),
        ):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                asyncio.run(copilot_proof_runner._run(args))

    def test_head_tampering_fails_authentication(self) -> None:
        context = self.issue()
        continuity, _ledger, _report = self.complete_proof()
        head_path = continuity.store.authority_path
        with closing(sqlite3.connect(head_path)) as db:
            db.execute("BEGIN")
            db.execute("UPDATE copilot_expected_head SET signature=?", ("f" * 64,))
            db.commit()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            CopilotControllerContinuity(context, readonly=True).verify_existing()

    def test_row_tampering_fails_signature_validation(self) -> None:
        context = self.issue()
        continuity, _ledger, _report = self.complete_proof()
        with closing(sqlite3.connect(continuity.store.ledger_path)) as db:
            db.execute("BEGIN")
            db.execute("UPDATE copilot_ledger_row SET payload=REPLACE(payload, 'claude-sonnet-4.6', 'claude-opus-4.8')")
            db.commit()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            CopilotControllerContinuity(context, readonly=True).verify_existing()

    def test_ledger_rollback_is_detected_by_authenticated_expected_head(self) -> None:
        context = self.issue()
        continuity, _ledger, _report = self.complete_proof()
        with closing(sqlite3.connect(continuity.store.ledger_path)) as db:
            db.execute("BEGIN")
            db.execute("DELETE FROM copilot_ledger_row WHERE sequence > 0")
            first = json.loads(db.execute(
                "SELECT payload FROM copilot_ledger_row WHERE sequence=0",
            ).fetchone()[0])
            db.execute("UPDATE copilot_ledger_head SET sequence=0, signature=?", (first["signature"],))
            db.commit()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            CopilotControllerContinuity(context, readonly=True).verify_existing()

    def test_session_checkpoint_tampering_fails_authentication(self) -> None:
        context = self.issue()
        continuity, _ledger, _report = self.complete_proof()
        with closing(sqlite3.connect(continuity.store.authority_path)) as db:
            db.execute("BEGIN")
            db.execute("UPDATE copilot_expected_head SET session_scope=?", ("f" * 64,))
            db.commit()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            CopilotControllerContinuity(context, readonly=True).verify_existing()

    def test_actual_model_fallback_and_resume_gap_are_rejected(self) -> None:
        self.issue()
        continuity, _ledger, report = self.complete_proof(actual_model="claude-opus-4.8")
        self.assertFalse(report.verified)
        self.assertIn("actual_model_mismatch", report.reason_codes)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            continuity.verify_existing()

        self.tearDown()
        self.setUp()
        self.issue()
        continuity, _ledger, report = self.complete_proof(event_type="session.resume")
        self.assertFalse(report.verified)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            continuity.verify_existing()

    def test_crash_gap_after_session_attachment_is_incomplete(self) -> None:
        context = self.issue()
        continuity = self.continuity()
        ledger = continuity.new_ledger()
        pin = copilot_sdk.CopilotRuntimePin(
            platform="darwin-arm64", managed_cache_path="/cache",
            executable_path="/cache/copilot-runtime", executable_sha256="a" * 64,
            payload_path="/cache/runtime.node", payload_sha256="b" * 64,
        )
        collector = copilot_sdk.CopilotUsageCollector(
            continuity.identity, evidence_key=continuity.evidence_key,
            runtime_pin=pin, protected_ledger=ledger,
        )

        class Session:
            session_id = "native-session"

            def on(self, _handler):
                return lambda: None

        with mock.patch.object(copilot_sdk, "_verify_runtime_pin"):
            collector.record_runtime_pin()
        collector.record_runtime_status("1.0.93", 3)
        collector.attach(Session())
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            CopilotControllerContinuity(context, readonly=True).verify_existing()

    def test_controller_authority_cannot_be_supplied_as_arbitrary_constructor_secret(self) -> None:
        with self.assertRaises(TypeError):
            CopilotControllerContinuity(
                identity=copilot_sdk.CopilotLaunchIdentity(
                    workflow_root=self.workflow_root, task_id=self.task_id, claim_id=self.claim_id,
                    lease_epoch=1, continuity_id="continuity", launch_id="launch", role="proof",
                    requested_model="claude-sonnet-4.6", effort="low",
                ),
                authority_secret="user-entered-secret-that-is-not-canonical",
                workspace_root=self.workspace,
                ledger_path=self.private_state / "ledger.sqlite3",
                authority_path=self.private_state / "head.sqlite3",
            )


if __name__ == "__main__":
    unittest.main()
