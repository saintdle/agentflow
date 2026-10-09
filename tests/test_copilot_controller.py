from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import copilot_sdk
from agentflow.copilot_controller import CopilotControllerContinuity, load_approved_proof_manifest


class CopilotControllerContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "private-state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        for name in ("ledger", "authority"):
            (self.state / name).mkdir(mode=0o700)
            os.chmod(self.state / name, 0o700)
        self.identity = copilot_sdk.CopilotLaunchIdentity(
            workflow_root="root", task_id="task", claim_id="claim", lease_epoch=1,
            continuity_id="continuity", launch_id="launch-unique", role="proof",
            requested_model="claude-sonnet-4.6", effort="low",
        )

    def continuity(self, *, identity=None, workspace=None):
        return CopilotControllerContinuity(
            identity=identity or self.identity,
            authority_secret="controller-authority-secret-at-least-32-characters",
            workspace_root=workspace or self.workspace,
            ledger_path=self.state / "ledger" / "ledger.sqlite3",
            authority_path=self.state / "authority" / "expected.sqlite3",
        )

    def complete_proof(self):
        continuity = self.continuity()
        ledger = continuity.new_ledger()
        pin = copilot_sdk.CopilotRuntimePin(
            platform="darwin-arm64", managed_cache_path="/cache",
            executable_path="/cache/copilot-runtime", executable_sha256="a" * 64,
            payload_path="/cache/runtime.node", payload_sha256="b" * 64,
        )
        collector = copilot_sdk.CopilotUsageCollector(
            self.identity, evidence_key=continuity.evidence_key, runtime_pin=pin,
            protected_ledger=ledger,
        )
        class Session:
            session_id = "native-session"
            def on(self, _handler): return lambda: None
        with mock.patch.object(copilot_sdk, "_verify_runtime_pin"):
            collector.record_runtime_pin()
        collector.record_runtime_status("1.0.93", 3)
        collector.attach(Session())
        collector.record_tool_inventory([])
        collector.begin_request()
        collector.on_event(SimpleNamespace(
            type="assistant.usage", id="event", agent_id=None,
            data=SimpleNamespace(model=self.identity.requested_model, api_call_id="call",
                                 input_tokens=2, output_tokens=1, reasoning_tokens=0, cost=0.01),
        ))
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        return continuity, ledger, collector.report()

    def test_separate_checkpoint_reopens_completed_native_session_chain(self) -> None:
        continuity, ledger, report = self.complete_proof()
        self.assertTrue(report.verified)
        self.assertFalse(report.persistent_admission)
        checkpoint = continuity.store.read_expected_checkpoint()
        self.assertEqual(checkpoint.head, ledger.head)
        restarted = self.continuity()
        self.assertEqual(list(restarted.verify_existing()), list(report.ledger))

    def test_ledger_rollback_and_failed_cross_store_commit_fail_closed(self) -> None:
        continuity, _ledger, _report = self.complete_proof()
        snapshot = continuity.store.read_ledger(continuity.store.anchor_id)
        self.assertIsNotNone(snapshot)
        with sqlite3.connect(continuity.store.ledger_path) as db:
            db.execute("DELETE FROM copilot_ledger_row WHERE anchor_id=? AND sequence>0", (continuity.store.anchor_id,))
            db.execute("UPDATE copilot_ledger_head SET sequence=0, signature=? WHERE anchor_id=?",
                       (snapshot.rows[0]["signature"], continuity.store.anchor_id))
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            self.continuity().verify_existing()

        fresh = self.root / "atomic"
        fresh.mkdir(mode=0o700)
        os.chmod(fresh, 0o700)
        (fresh / "l").mkdir(mode=0o700); os.chmod(fresh / "l", 0o700)
        (fresh / "a").mkdir(mode=0o700); os.chmod(fresh / "a", 0o700)
        failed = CopilotControllerContinuity(
            identity=self.identity, authority_secret="controller-authority-secret-at-least-32-characters",
            workspace_root=self.workspace, ledger_path=fresh / "l" / "ledger.db", authority_path=fresh / "a" / "auth.db",
        )
        with sqlite3.connect(failed.store.authority_path) as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON copilot_expected_head BEGIN SELECT RAISE(ABORT,'test'); END")
        pin = copilot_sdk.CopilotRuntimePin(
            platform="darwin-arm64", managed_cache_path="/cache",
            executable_path="/cache/copilot-runtime", executable_sha256="a" * 64,
            payload_path="/cache/runtime.node", payload_sha256="b" * 64,
        )
        collector = copilot_sdk.CopilotUsageCollector(
            self.identity, evidence_key=failed.evidence_key, runtime_pin=pin,
            protected_ledger=failed.new_ledger(),
        )
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            with mock.patch.object(copilot_sdk, "_verify_runtime_pin"):
                collector.record_runtime_pin()
        self.assertIsNone(failed.store.read_ledger(failed.store.anchor_id))

    def test_manifest_requires_parent_approval_budget_and_exact_identity(self) -> None:
        manifest = {
            "schema": 1, "purpose": "copilot-negative-control-proof", "decision": "approved",
            "approved_by": "controller", "workspace_root": str(self.workspace.resolve()),
            "identity": self.identity.to_dict(), "budget": {"deadline_seconds": 180, "retry_count": 0},
            "preflight": {"controller_status": "passed", "independent_review_status": "passed",
                          "proof_invocation_status": "not_started"},
        }
        path = self.state / "approval.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(path, 0o600)
        load_approved_proof_manifest(path, identity=self.identity, workspace_root=self.workspace)
        manifest["budget"]["retry_count"] = 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            load_approved_proof_manifest(path, identity=self.identity, workspace_root=self.workspace)


if __name__ == "__main__":
    unittest.main()
