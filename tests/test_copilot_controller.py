from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
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
    CopilotControllerContinuity,
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

    def write_review_attestation(self):
        authority = copilot_controller._load_controller_authority(self.workspace, self.workflow_root)
        claim = dict(copilot_controller._active_claim(authority, self.task_id, self.claim_id))
        identity = copilot_sdk.CopilotLaunchIdentity(
            workflow_root=self.workflow_root, task_id=self.task_id, claim_id=self.claim_id,
            lease_epoch=authority.lease.epoch, continuity_id=authority.lease.continuity_id,
            launch_id="launch-112-proof", role="proof", requested_model="claude-sonnet-4.6", effort="low",
        )
        source = copilot_controller._source_fingerprint()
        document = {
            "schema": copilot_controller._REVIEW_SCHEMA,
            "workspace_root": str(self.workspace), "workflow_root": self.workflow_root,
            "identity": identity.to_dict(),
            "task_claim_sha256": hashlib.sha256(copilot_controller._canonical(claim)).hexdigest(),
            "source": source,
            "preflight_sha256": copilot_controller._preflight_digest(authority, identity, claim, source),
            "reviews": [
                {"reviewer": "reviewer-one", "artifact_sha256": "a" * 64, "disposition": "accepted"},
                {"reviewer": "reviewer-two", "artifact_sha256": "b" * 64, "disposition": "accepted"},
            ],
            "authority_key_id": hashlib.sha256(authority.authority_secret.encode()).hexdigest()[:24],
        }
        document["attestation_hmac"] = copilot_controller._review_mac(authority.authority_secret, document)
        self.review_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.review_path, 0o400)

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
        os.chmod(self.review_path, 0o400)
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.issue()
        self.assertFalse(cli._execution_limit_ledger_path(self.workspace, self.workflow_root).exists())

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
        self.issue()
        with mock.patch("agentflow.copilot_controller.time.time", return_value=self.context().deadline_epoch + 1):
            with self.assertRaises(copilot_sdk.CopilotSDKError):
                self.context()
        with mock.patch.object(copilot_sdk.CopilotProofRun, "open", side_effect=AssertionError("SDK startup")):
            with mock.patch.object(copilot_proof_runner, "_provider_credential", side_effect=AssertionError("prompt")):
                args = SimpleNamespace(
                    prepare_manifest=False, verify_only=True, workspace_root=self.workspace,
                    workflow_root=self.workflow_root, manifest=self.manifest_path,
                    task_id=None, claim_id=None, launch_id=None, role=None, model=None,
                    effort=None, review_attestation=None, credential_fd=None,
                )
                with mock.patch("agentflow.copilot_controller.time.time", return_value=self.context().deadline_epoch + 1):
                    with self.assertRaises(copilot_sdk.CopilotSDKError):
                        asyncio.run(copilot_proof_runner._run(args))

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
