"""Native claim generations cannot be revived by reusing an actor name."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tests.test_cli import ValidLaunch
from agentflow import beads, cli


class ClaimGenerationFencingTests(unittest.TestCase):
    def test_same_actor_regrant_rejects_old_launch_and_permanently_fences(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            with fixture.beads_patches(), mock.patch.object(
                beads, "claim_generation", return_value="b" * 64,
            ), mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                with self.assertRaisesRegex(ValueError, "generation"):
                    cli._verify_launch_authority(
                        fixture.root, fixture.task_id, fixture.claim_token,
                        fixture.lease.token, workflow_root=fixture.workflow_root,
                        actor=fixture.actor,
                    )
                heartbeat.assert_not_called()
            with self.assertRaisesRegex(ValueError, "fenced"):
                cli._assert_native_claim_unfenced(fixture.root, fixture.workflow_root, fixture.task_id)

    def test_regrant_during_heartbeat_rejects_old_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            with fixture.beads_patches(), mock.patch.object(
                beads, "claim_generation", side_effect=["a" * 64, "b" * 64],
            ):
                with self.assertRaisesRegex(ValueError, "generation"):
                    cli._verify_launch_authority(
                        fixture.root, fixture.task_id, fixture.claim_token,
                        fixture.lease.token, workflow_root=fixture.workflow_root,
                        actor=fixture.actor,
                    )

    def test_regrant_fences_controller_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            controller = mock.Mock()
            controller.active_tasks.return_value = [{
                "task": fixture.task_id, "actor": fixture.actor,
                "claim_id": "claim-1", "claim_token": fixture.claim_token,
            }]
            fixture.task_issue["status"] = "in_progress"
            with fixture.beads_patches(), mock.patch.object(
                beads, "claim_generation", return_value="b" * 64,
            ), mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                self.assertEqual(cli._renew_native_task_leases(
                    controller, fixture.root, fixture.workflow_root,
                ), [fixture.task_id])
                heartbeat.assert_not_called()

    def test_initial_issuance_seals_native_generation_outside_worker_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            identity = beads.ClaimIdentity("workflow", "task", "actor", "claim")
            with mock.patch.object(beads, "claim_generation", return_value="a" * 64):
                cli._persist_claim_identity(root, beads.ExactClaim(identity, {"id": "task"}))
            path = cli._native_claim_generation_path(root, "workflow", "task")
            self.assertFalse(path.is_relative_to(root))
            self.assertEqual(json.loads(path.read_text()), cli._claim_generation_binding(identity.to_dict(), "a" * 64))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unavailable_history_fences_before_renewal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            with fixture.beads_patches(), mock.patch.object(beads, "claim_generation", side_effect=beads.BeadsError("history unavailable")), \
                 mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                with self.assertRaisesRegex(ValueError, "fenced"):
                    cli._verify_launch_authority(fixture.root, fixture.task_id, fixture.claim_token, fixture.lease.token,
                                                workflow_root=fixture.workflow_root, actor=fixture.actor)
                heartbeat.assert_not_called()
            with self.assertRaisesRegex(ValueError, "fenced"):
                cli._assert_native_claim_unfenced(fixture.root, fixture.workflow_root, fixture.task_id)

    def test_missing_seal_cannot_be_inferred_from_existing_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            path = cli._native_claim_generation_path(fixture.root, fixture.workflow_root, fixture.task_id)
            path.unlink()
            identity = beads.ClaimIdentity(fixture.workflow_root, fixture.task_id, fixture.actor, "claim-1", fixture.claim_token)
            with fixture.beads_patches(), self.assertRaisesRegex(ValueError, "generation binding is missing"):
                cli._persist_claim_identity(fixture.root, beads.ExactClaim(identity, fixture.task_issue))
            self.assertFalse(path.exists())

    def test_existing_seal_cannot_be_rebased_at_claim_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            path = cli._native_claim_generation_path(fixture.root, fixture.workflow_root, fixture.task_id)
            before = path.read_bytes()
            identity = beads.ClaimIdentity(fixture.workflow_root, fixture.task_id, fixture.actor, "claim-1", fixture.claim_token)
            with fixture.beads_patches(), mock.patch.object(beads, "claim_generation", return_value="b" * 64):
                with self.assertRaisesRegex(ValueError, "generation changed"):
                    cli._persist_claim_identity(fixture.root, beads.ExactClaim(identity, fixture.task_issue))
            self.assertEqual(path.read_bytes(), before)

    def test_authenticated_resume_keeps_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ValidLaunch(Path(tmp).resolve())
            path = cli._native_claim_generation_path(fixture.root, fixture.workflow_root, fixture.task_id)
            before = path.read_bytes()
            identity = beads.ClaimIdentity(fixture.workflow_root, fixture.task_id, fixture.actor, "claim-1", fixture.claim_token)
            with fixture.beads_patches():
                cli._persist_claim_identity(fixture.root, beads.ExactClaim(identity, fixture.task_issue))
                cli._verify_launch_authority(fixture.root, fixture.task_id, fixture.claim_token, fixture.lease.token,
                                            workflow_root=fixture.workflow_root, actor=fixture.actor)
            self.assertEqual(path.read_bytes(), before)

    def test_same_actor_regrant_rejects_authenticated_result_without_consuming(self):
        for budgeted in (False, True):
            with self.subTest(budgeted=budgeted), tempfile.TemporaryDirectory() as tmp:
                fixture = ValidLaunch(Path(tmp).resolve())
                if budgeted:
                    fixture.task_issue["metadata"]["agentflow"]["launch"]["execution_limits"] = {
                        "deadline_seconds": 300, "max_retries": 0,
                    }
                    fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
                with fixture.beads_patches(), mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                     mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.herdr_launch(fixture.launch_args()), 0)
                state = fixture.root / ".agentflow/herdr/sessions.json"
                record = cli._herdr_session_record(fixture.root, fixture.task_id)
                channel = record["return_channel"]
                Path(channel["result_path"]).write_text(json.dumps({
                    "outcome": "completed", "acceptance_results": [{"acceptance_id": "R1", "status": "passed",
                    "evidence": "synthetic test", "source": "provider"}],
                }))
                before = state.read_bytes()
                ledger = cli._execution_limit_ledger_path(fixture.root, fixture.workflow_root)
                ledger_before = ledger.read_bytes() if ledger.exists() else None
                payloads = []
                args = argparse.Namespace(root=str(fixture.root), contract=channel["contract_path"],
                    file=channel["result_path"], json=True, _controller_ingest=True,
                    _capability_file=channel["capability_file"], _authority_secret=fixture.authority_secret)
                with fixture.beads_patches(), mock.patch.object(beads, "claim_generation", return_value="b" * 64), \
                     mock.patch.object(cli, "_json_or_status", side_effect=lambda p, **_: payloads.append(p)):
                    self.assertEqual(cli.herdr_result(args), 2)
                self.assertIn("generation", payloads[-1]["error"])
                self.assertEqual(state.read_bytes(), before)
                self.assertEqual(ledger.read_bytes() if ledger.exists() else None, ledger_before)
                self.assertEqual(cli._herdr_session_record(fixture.root, fixture.task_id)["return_channel"]["state"], "issued")


class NativeGenerationHistoryTests(unittest.TestCase):
    def event(self, id="claim-a", actor="same-actor", kind="claimed", task="task"):
        return {"id": id, "issue_id": task, "actor": actor, "event_type": kind,
                "created_at": "2026-10-19T12:00:00Z"}

    def generation(self, events):
        receipt = subprocess.CompletedProcess([], 0, json.dumps(events), "")
        with mock.patch.object(beads, "run", return_value=receipt) as run:
            result = beads.claim_generation(Path("/fixture"), "task")
        self.assertEqual(run.call_args.args[1:], ("history", "task", "--events", "--limit", "0", "--json"))
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        return result

    def test_same_actor_same_timestamp_new_event_changes_generation(self):
        first = self.event()
        self.assertNotEqual(self.generation([first]), self.generation([first, self.event("claim-b")]))

    def test_history_order_and_unrelated_updates_do_not_change_generation(self):
        claims = [self.event(), self.event("claim-b")]
        self.assertEqual(self.generation(claims), self.generation([self.event("edit", kind="updated"), *reversed(claims)]))

    def test_unavailable_missing_foreign_duplicate_malformed_history_fails_closed(self):
        for events in (None, {}, [], [self.event(kind="updated")], [self.event(task="foreign")],
                       [self.event(), self.event()], [self.event(id="")], [self.event(actor="")], [None]):
            with self.subTest(events=events), self.assertRaises(beads.BeadsError):
                self.generation(events)
        with mock.patch.object(beads, "run", return_value=subprocess.CompletedProcess([], 1, "", "unavailable")):
            with self.assertRaises(beads.BeadsError):
                beads.claim_generation(Path("/fixture"), "task")


if __name__ == "__main__":
    unittest.main()
