import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow import cli
from tests.test_cli import ValidLaunch, _controller_args, _run_controller_json


class BoundExecutionExpiryTests(unittest.TestCase):
    @contextlib.contextmanager
    def _launched_budgeted_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            fixture = ValidLaunch(base / "workspace", seed_lease=False)
            fixture.task_issue["metadata"]["agentflow"]["launch"].update(
                execution_limits={"deadline_seconds": 1800, "max_retries": 0},
                sterile=True,
            )
            fixture.task_issue["metadata"]["agentflow"].update(
                tool_profile="shell-readonly", output_boundary=".",
            )
            args = _controller_args(fixture.root, workflow_root=fixture.workflow_root)
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(base / "state")}), \
                 mock.patch.object(cli.beads_backend, "get_issue", side_effect=fixture.get_issue), \
                 mock.patch.object(cli.beads_backend, "root_descendants", return_value=[fixture.task_issue]), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None), \
                 mock.patch.object(cli.beads_backend, "claim_ready", return_value=fixture.task_issue), \
                 mock.patch.object(cli.beads_backend, "update_agentflow_metadata"), \
                 mock.patch.object(cli, "_provider_command", side_effect=fixture.provider_command), \
                 mock.patch.object(cli.subprocess, "run", side_effect=fixture.herdr_run()):
                payload = _run_controller_json(cli.controller_resume, args)
                self.assertEqual(payload["result"]["state"], "running")
                fixture.task_issue.update(status="in_progress", assignee=fixture.controller)
                controller, _ = cli._controller_instance(args)
                record = cli._herdr_session_record(fixture.root, fixture.task_id)
                yield fixture, args, controller, controller._current_lease(), record

    @contextlib.contextmanager
    def _expired_draining_execution(
        self, reason, failed_task="", *, cause_kind="unknown",
        cause_provenance="controller_unclassified", cause_launch="", cause_sequence=None,
    ):
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            controller.migrate_active_tasks(lease=lease)
            if failed_task:
                controller.reserve_active_task(
                    {"id": failed_task, "root": str(fixture.root), "phase": "dispatch"},
                    lease=lease,
                )
            if cause_sequence is None:
                controller.begin_draining(
                    reason, failed_task=failed_task, cause_kind=cause_kind,
                    cause_provenance=cause_provenance,
                    cause_task=failed_task, cause_launch=cause_launch, lease=lease,
                )
            else:
                for index, (kind, provenance, task_id, launch_id) in enumerate(cause_sequence):
                    controller.begin_draining(
                        reason if index == 0 else f"additional {kind} cause",
                        failed_task=task_id if kind == "managed_task_failure" else "",
                        cause_kind=kind, cause_provenance=provenance,
                        cause_task=task_id, cause_launch=launch_id, lease=lease,
                    )
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)
            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            yield fixture, args, controller, lease, record

    def test_predeadline_submission_stays_on_normal_ingestion_path(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            channel = record["return_channel"]
            Path(channel["submission_file"]).write_text("{}", encoding="utf-8")
            self.assertIsNone(cli._expire_bound_execution_if_due(args, controller, fixture.root, lease))
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "launched")
            self.assertEqual(current["return_channel"]["state"], "issued")

    def test_due_result_race_refuses_expiry_without_revoking(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            channel = record["return_channel"]
            deadline = channel["contract_binding"]["deadline_epoch"]

            def inject_result(_pane_id):
                Path(channel["result_path"]).write_text("{}", encoding="utf-8")

            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane", side_effect=inject_result):
                with self.assertRaisesRegex(ValueError, "already has a result"):
                    cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "launched")
            self.assertEqual(current["return_channel"]["state"], "issued")

    def test_due_absent_pane_revokes_the_signed_channel_and_halts_once(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "expired_execution")
            self.assertEqual(current["return_channel"]["state"], "revoked")
            self.assertTrue(cli._authenticated_expired_bound_execution(
                fixture.root, fixture.workflow_root, fixture.task_id,
                authority_secret=args._authority_secret,
            ))
            self.assertEqual(controller.active_tasks(), [])
            self.assertIsNone(cli._expire_bound_execution_if_due(args, controller, fixture.root, lease))

    def test_inconsistent_open_expiry_receipt_denies_before_any_protected_write(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            document = controller._load_checkpoint()
            document["drain_causes"] = [{
                "cause_id": "prior-no-ready",
                "kind": "no_ready_work",
                "provenance": "scheduler_no_ready_work",
                "task_id": "",
                "launch_id": "",
                "diagnostic_sha256": "",
            }]
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, document)
            ledger_path = cli._execution_limit_ledger_path(fixture.root, fixture.workflow_root)
            protected_paths = (
                controller.checkpoint_path,
                controller.state_path,
                fixture.root / ".agentflow/herdr/sessions.json",
                ledger_path,
            )
            before = {path: path.read_bytes() for path in protected_paths}
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]

            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"), \
                 self.assertRaisesRegex(ValueError, "prior controller state is inconsistent"):
                cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertEqual({path: path.read_bytes() for path in protected_paths}, before)
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "launched")
            self.assertEqual(current["return_channel"]["state"], "issued")

    def test_due_absent_pane_expires_the_exact_bound_task_while_draining(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            controller.migrate_active_tasks(lease=lease)
            draining = controller.begin_draining("existing owned diagnostic drain", lease=lease)
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]

            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            self.assertEqual(
                result.checkpoint["terminal_reason"], draining.checkpoint["terminal_reason"],
            )
            self.assertEqual(
                result.checkpoint["last_check"],
                f"expired bound execution launch {record['launch_id']} for {fixture.task_id}",
            )
            self.assertEqual(controller.active_tasks(), [])
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "expired_execution")
            self.assertEqual(current["return_channel"]["state"], "revoked")
            self.assertTrue(cli._authenticated_expired_bound_execution(
                fixture.root, fixture.workflow_root, fixture.task_id,
                authority_secret=args._authority_secret,
            ))

            dispatch = mock.Mock(return_value={"session_id": "unexpected"})
            resumed = controller.resume(
                [{"task": "ready-child", "root": fixture.workflow_root, "ready": True}],
                dispatch=dispatch,
                lease=lease,
            )
            self.assertTrue(resumed.terminal)
            dispatch.assert_not_called()

    def test_draining_expiry_receipt_replays_after_checkpoint_crash(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            controller.migrate_active_tasks(lease=lease)
            draining = controller.begin_draining("existing owned diagnostic drain", lease=lease)
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            snapshot = cli._bound_execution_expiry_snapshot(
                args, controller, fixture.root, fixture.workflow_root, lease, fixture.task_id,
                authority_secret=args._authority_secret,
            )
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                # Simulate a crash after signed Herdr/ledger retirement but before the checkpoint halt.
                cli._commit_expired_bound_execution(
                    fixture.root, fixture.workflow_root, fixture.task_id, snapshot,
                    authority_secret=args._authority_secret,
                )
                self.assertEqual(len(controller.active_tasks()), 1)
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            self.assertEqual(
                result.checkpoint["terminal_reason"], draining.checkpoint["terminal_reason"],
            )
            self.assertEqual(controller.active_tasks(), [])
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "expired_execution")
            self.assertEqual(current["return_channel"]["state"], "revoked")

    def test_draining_expiry_rejects_wrong_claim_or_session_without_mutation(self) -> None:
        for field in ("claim_id", "session_id"):
            with self.subTest(field=field), self._launched_budgeted_execution() as (
                fixture, args, controller, lease, record,
            ):
                controller.migrate_active_tasks(lease=lease)
                controller.begin_draining("existing owned diagnostic drain", lease=lease)
                document = controller._load_checkpoint()
                document["active_tasks"][0][field] = f"forged-{field}"
                cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, document)
                before = controller.checkpoint_path.read_bytes()
                deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]

                with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                     mock.patch.object(cli, "_require_definitively_absent_herdr_pane"), \
                     self.assertRaises(ValueError):
                    cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

                self.assertEqual(controller.checkpoint_path.read_bytes(), before)
                current = cli._herdr_session_record(fixture.root, fixture.task_id)
                self.assertEqual(current["status"], "launched")
                self.assertEqual(current["return_channel"]["state"], "issued")

    def test_due_bound_expiry_reconciles_exact_pending_identity_before_retiring(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            checkpoint = controller._load_checkpoint()
            checkpoint["active_tasks"] = controller.active_tasks()
            checkpoint["active_tasks"][0]["state"] = "identity_pending"
            checkpoint["active_tasks"][0]["session_id"] = ""
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint)
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            self.assertEqual(
                cli._herdr_session_record(fixture.root, fixture.task_id)["status"],
                "expired_execution",
            )

    def test_pending_identity_reconcile_rejects_a_wrong_claim(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, _record):
            checkpoint = controller._load_checkpoint()
            checkpoint["active_tasks"] = controller.active_tasks()
            checkpoint["active_tasks"][0]["state"] = "identity_pending"
            checkpoint["active_tasks"][0]["session_id"] = ""
            checkpoint["active_tasks"][0]["claim_id"] = "forged-claim"
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint)
            with self.assertRaisesRegex(ValueError, "exact pending reservation"):
                cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)
            row = controller.active_tasks()[0]
            self.assertEqual(row["state"], "identity_pending")
            self.assertEqual(row["claim_id"], "forged-claim")

    def test_pending_identity_reconcile_rejects_a_wrong_provider(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, _record):
            checkpoint = controller._load_checkpoint()
            checkpoint["active_tasks"] = controller.active_tasks()
            checkpoint["active_tasks"][0]["state"] = "identity_pending"
            checkpoint["active_tasks"][0]["session_id"] = ""
            cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint)
            with cli._herdr_transaction(fixture.root / ".agentflow/herdr/sessions.json") as state:
                state["sessions"][fixture.task_id]["provider"] = "forged-provider"
            with self.assertRaisesRegex(ValueError, "exact pending reservation"):
                cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)
            row = controller.active_tasks()[0]
            self.assertEqual(row["state"], "identity_pending")
            self.assertEqual(row["session_id"], "")

    def test_pending_identity_reconcile_preserves_checkpoint_for_bad_token_or_pane(self) -> None:
        for case in ("claim-token", "empty-pane", "conflicting-pane"):
            with self.subTest(case=case), self._launched_budgeted_execution() as (
                fixture, args, controller, lease, _record,
            ):
                checkpoint = controller._load_checkpoint()
                checkpoint["active_tasks"] = controller.active_tasks()
                checkpoint["active_tasks"][0]["state"] = "identity_pending"
                checkpoint["active_tasks"][0]["session_id"] = ""
                cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, checkpoint)
                before = controller.checkpoint_path.read_bytes()
                if case == "claim-token":
                    fixture.task_issue["metadata"]["agentflow"]["claim_token"] = "changed-token"
                else:
                    with cli._herdr_transaction(fixture.root / ".agentflow/herdr/sessions.json") as state:
                        record = state["sessions"][fixture.task_id]
                        if case == "empty-pane":
                            record["binding"]["pane_id"] = ""
                        else:
                            record["pane_id"] = "conflicting-pane"
                with self.assertRaises(ValueError):
                    cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)
                self.assertEqual(controller.checkpoint_path.read_bytes(), before)
                row = controller.active_tasks()[0]
                self.assertEqual(row["state"], "identity_pending")
                self.assertEqual(row["session_id"], "")

    def test_revoked_expiry_receipt_replays_after_checkpoint_crash(self) -> None:
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            snapshot = cli._bound_execution_expiry_snapshot(
                args, controller, fixture.root, fixture.workflow_root, lease, fixture.task_id,
                authority_secret=args._authority_secret,
            )
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                # Simulate the crash boundary after the signed Herdr/ledger
                # retirement commits but before RootController writes its halt.
                cli._commit_expired_bound_execution(
                    fixture.root, fixture.workflow_root, fixture.task_id, snapshot,
                    authority_secret=args._authority_secret,
                )
                self.assertEqual(len(controller.active_tasks()), 1)
                result = cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)

            self.assertIsNotNone(result)
            self.assertTrue(result.terminal)
            self.assertEqual(controller.active_tasks(), [])
            current = cli._herdr_session_record(fixture.root, fixture.task_id)
            self.assertEqual(current["status"], "expired_execution")
            self.assertEqual(
                current["return_channel"]["contract_binding"]["deadline_epoch"], deadline,
            )

    def test_bound_retirement_verifier_replays_the_staged_target_checkpoint(self) -> None:
        """A crash after target persistence re-verifies this exact bound retirement."""
        with self._launched_budgeted_execution() as (fixture, args, controller, lease, record):
            controller.begin_draining(
                "typed no-ready cause before bound expiry",
                cause_kind="no_ready_work",
                cause_provenance="scheduler_no_ready_work",
                lease=lease,
            )
            deadline = record["return_channel"]["contract_binding"]["deadline_epoch"]
            with mock.patch.object(cli.time, "time", return_value=deadline + 1), \
                 mock.patch.object(cli, "_require_definitively_absent_herdr_pane"):
                self.assertIsNotNone(
                    cli._expire_bound_execution_if_due(args, controller, fixture.root, lease)
                )

            ready = {
                "id": "ready-task", "status": "open", "assignee": "",
                "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
            }

            def get_issue(_root, task_id):
                return {
                    fixture.workflow_root: fixture.root_issue,
                    fixture.task_id: fixture.task_issue,
                    ready["id"]: ready,
                }[task_id]

            def ready_command(_root, *_argv):
                return subprocess.CompletedProcess([], 0, json.dumps([{"id": ready["id"]}]), "")

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(
                     cli.beads_backend, "root_descendants",
                     return_value=[fixture.task_issue, ready],
                 ), \
                 mock.patch.object(cli.beads_backend, "run", side_effect=ready_command):
                self.assertEqual(
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    ),
                    ready["id"],
                )
                controller.acknowledge_cancelled_preidentity_halt(
                    fixture.workflow_root, fixture.task_id, ready["id"],
                    authority_secret=args._authority_secret, lease=lease,
                )
                # The target checkpoint is durable. Replaying after the
                # simulated crash must take the staged/open path, not reject
                # it as a terminal checkpoint or accept another retirement kind.
                self.assertEqual(
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        allow_staged_selection=True,
                        expected_retirement_kind="expired_execution",
                    ),
                    ready["id"],
                )
                with self.assertRaisesRegex(cli.controller_backend.ControllerError, "does not match"):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        allow_staged_selection=True,
                        expected_retirement_kind="cancelled_preidentity",
                    )
                checkpoint = controller._load_checkpoint()
                self.assertEqual(checkpoint["drain_causes"], [])
                state = json.loads(controller.state_path.read_text(encoding="utf-8"))
                state["drain_audit"][0]["cause_ids"] = ["forged-cause-id"]
                cli._private_atomic_json(controller.state_path, state)
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError,
                    "current checkpoint drain causes differ from the signed expiry receipt",
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        allow_staged_selection=True,
                        expected_retirement_kind="expired_execution",
                    )

            with cli._herdr_transaction(fixture.root / ".agentflow/herdr/sessions.json") as state:
                current = state["sessions"][fixture.task_id]
                current["binding"]["provider"] = "forged-provider"
            self.assertFalse(cli._authenticated_expired_bound_execution(
                fixture.root, fixture.workflow_root, fixture.task_id,
                authority_secret=args._authority_secret,
            ))

    def test_expired_bound_continuation_rejects_unknown_prior_drain(self) -> None:
        with self._expired_draining_execution("existing owned diagnostic drain") as (
            fixture, args, controller, lease, _record,
        ):
            ready = {
                "id": "ready-task", "status": "open", "assignee": "",
                "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
            }

            def get_issue(_root, task_id):
                return {
                    fixture.workflow_root: fixture.root_issue,
                    fixture.task_id: fixture.task_issue,
                    ready["id"]: ready,
                }[task_id]

            def ready_command(_root, *_argv):
                return subprocess.CompletedProcess([], 0, json.dumps([{"id": ready["id"]}]), "")

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(
                     cli.beads_backend, "root_descendants",
                     return_value=[fixture.task_issue, ready],
                 ), \
                 mock.patch.object(cli.beads_backend, "run", side_effect=ready_command):
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError, "prior drain cause",
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )

    def test_expired_bound_continuation_preserves_mixed_causes_in_both_orders(self) -> None:
        reason = "USER_ACTION_REQUIRED: NO_READY_WORK: diagnostic text"
        no_ready = ("no_ready_work", "scheduler_no_ready_work", "", "")
        failure = ("managed_task_failure", "authenticated_task_outcome", "failed-task", "failed-launch")
        unknown = ("unknown", "controller_unclassified", "", "")
        for sequence in ([no_ready, failure, unknown], [unknown, failure, no_ready]):
            with self.subTest(order=[item[0] for item in sequence]), self._expired_draining_execution(
                reason, cause_sequence=sequence,
            ) as (fixture, args, controller, lease, _record):
                receipt = cli._herdr_session_record(fixture.root, fixture.task_id)["execution_disposition"]
                causes = receipt["prior_controller_state"]["drain_causes"]
                self.assertEqual([cause["kind"] for cause in causes], [item[0] for item in sequence])
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError, "unknown prior drain cause",
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, "ready-task",
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )

    def test_expired_bound_continuation_rejects_blocked_native_sibling_without_herdr_record(self) -> None:
        no_ready_reason = (
            "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: "
            "ready-task (ready-task)"
        )
        with self._expired_draining_execution(
            no_ready_reason, failed_task="native-failure",
            cause_kind="managed_task_failure",
            cause_provenance="authenticated_task_outcome", cause_launch="native-launch",
        ) as (
            fixture, args, controller, lease, expired_record,
        ):
            ready = {
                "id": "ready-task", "status": "open", "assignee": "",
                "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
            }
            blocked = {
                "id": "native-failure", "status": "blocked", "assignee": fixture.controller,
                "parent": fixture.workflow_root,
                "metadata": {"agentflow": {
                    "root": fixture.workflow_root, "task": "native-failure",
                    "actor": fixture.controller, "claim_id": "native-claim",
                }},
            }
            self.assertIsNone(cli._herdr_session_record(fixture.root, blocked["id"]))

            def get_issue(_root, task_id):
                return {
                    fixture.workflow_root: fixture.root_issue,
                    fixture.task_id: fixture.task_issue,
                    ready["id"]: ready,
                    blocked["id"]: blocked,
                }[task_id]

            def ready_command(_root, *_argv):
                return subprocess.CompletedProcess([], 0, json.dumps([{"id": ready["id"]}]), "")

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(
                     cli.beads_backend, "root_descendants",
                     return_value=[fixture.task_issue, ready, blocked],
                 ), \
                 mock.patch.object(cli.beads_backend, "run", side_effect=ready_command):
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError, "causal failure task remains unresolved",
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )
                blocked["status"] = "closed"
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError, "no authenticated launch record",
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )
                native_token = "native-claim-token"
                native_claim = "native-claim"
                native_binding = {
                    "root": str(fixture.root), "task_id": "native-failure",
                    "claim_id": native_claim, "launch_id": "native-launch",
                    "provider": "codex", "session_id": "native-session",
                }
                native_result = {
                    **native_binding, "outcome": "failed",
                }
                native_record = {
                    "root": str(fixture.root), "workflow_root": fixture.workflow_root,
                    "task_id": "native-failure", "launch_id": "native-launch",
                    "claim_id": native_claim, "status": "completed",
                    "binding": native_binding, "result": native_result,
                    "return_channel": {
                        "state": "consumed",
                        "result_sha256": cli._canonical_json_digest(native_result),
                        "contract_binding": {
                            "launch_id": "native-launch", "claim_id": native_claim,
                            "claim_token_sha256": hashlib.sha256(native_token.encode()).hexdigest(),
                        },
                    },
                }
                blocked["status"] = "closed"
                blocked["metadata"]["agentflow"]["claim_id"] = native_claim
                blocked["metadata"]["agentflow"]["claim_token"] = native_token
                current_expired_record = cli._herdr_session_record(
                    fixture.root, fixture.task_id,
                )

                def session_record(_root, task_id):
                    if task_id == "native-failure":
                        return native_record
                    if task_id == fixture.task_id:
                        return current_expired_record
                    return None

                with mock.patch.object(cli, "_herdr_session_record", side_effect=session_record):
                    with self.assertRaisesRegex(
                        cli.controller_backend.ControllerError,
                        "causal failure task lacks its exact authenticated outcome",
                    ):
                        cli._verify_cancelled_preidentity_continuation(
                            fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                            controller, lease, authority_secret=args._authority_secret,
                            expected_retirement_kind="expired_execution",
                        )

    def test_expired_bound_continuation_allows_known_no_ready_cause_with_exact_current_ready(self) -> None:
        no_ready_reason = (
            "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: "
            "ready-task (ready-task)"
        )
        with self._expired_draining_execution(
            no_ready_reason, cause_kind="no_ready_work",
            cause_provenance="scheduler_no_ready_work",
        ) as (
            fixture, args, controller, lease, _record,
        ):
            ready = {
                "id": "ready-task", "status": "open", "assignee": "",
                "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
            }
            unrelated_blocked = {
                "id": "foundation-task", "status": "blocked", "assignee": fixture.controller,
                "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
            }
            self.assertIsNone(cli._herdr_session_record(fixture.root, unrelated_blocked["id"]))

            def get_issue(_root, task_id):
                return {
                    fixture.workflow_root: fixture.root_issue,
                    fixture.task_id: fixture.task_issue,
                    ready["id"]: ready,
                }[task_id]

            def ready_command(_root, *_argv):
                return subprocess.CompletedProcess([], 0, json.dumps([{"id": ready["id"]}]), "")

            with mock.patch.object(cli.beads_backend, "get_issue", side_effect=get_issue), \
                 mock.patch.object(
                     cli.beads_backend, "root_descendants",
                     return_value=[fixture.task_issue, ready, unrelated_blocked],
                 ), \
                 mock.patch.object(cli.beads_backend, "run", side_effect=ready_command):
                self.assertEqual(
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    ),
                    ready["id"],
                )
                with self.assertRaises(cli.controller_backend.ControllerError):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, "stale-ready",
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )

    def test_expired_bound_continuation_rejects_tampered_signed_prior_cause(self) -> None:
        no_ready_reason = (
            "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: "
            "ready-task (ready-task)"
        )
        with self._expired_draining_execution(
            no_ready_reason, cause_kind="no_ready_work",
            cause_provenance="scheduler_no_ready_work",
        ) as (
            fixture, args, controller, _lease, _record,
        ):
            before = controller.checkpoint_path.read_bytes()
            with cli._herdr_transaction(fixture.root / ".agentflow/herdr/sessions.json") as state:
                receipt = state["sessions"][fixture.task_id]["execution_disposition"]
                causes = receipt["prior_controller_state"]["drain_causes"]
                self.assertEqual(causes[0]["kind"], "no_ready_work")
                causes[0]["kind"] = "unknown"

            self.assertFalse(cli._authenticated_expired_bound_execution(
                fixture.root, fixture.workflow_root, fixture.task_id,
                authority_secret=args._authority_secret,
            ))
            self.assertEqual(controller.checkpoint_path.read_bytes(), before)

    def test_expired_bound_continuation_rejects_stale_lease_and_foreign_checkpoint(self) -> None:
        for case in ("stale-lease", "foreign-root"):
            with self.subTest(case=case), self._expired_draining_execution(
                "ordinary open checkpoint",
            ) as (fixture, args, controller, lease, _record):
                if case == "stale-lease":
                    credentials = cli._read_controller_credentials(cli._resume_key_path(args))
                    replacement = cli.controller_backend.RootController(
                        controller.root, controller.controller,
                        state_path=controller.state_path, checkpoint_path=controller.checkpoint_path,
                    )
                    replacement.acquire(resume_proof=credentials["resume_secret"])
                    expected_error = "lease is no longer current"
                else:
                    document = controller._load_checkpoint()
                    document["root"] = str(fixture.root / "foreign-workspace")
                    cli.checkpoint_backend.write_checkpoint(controller.checkpoint_path, document)
                    expected_error = "safely retired preidentity halt"

                ready = {
                    "id": "ready-task", "status": "open", "assignee": "",
                    "parent": fixture.workflow_root, "metadata": {"agentflow": {}},
                }
                with self.assertRaisesRegex(
                    cli.controller_backend.ControllerError, expected_error,
                ):
                    cli._verify_cancelled_preidentity_continuation(
                        fixture.root, fixture.workflow_root, fixture.task_id, ready["id"],
                        controller, lease, authority_secret=args._authority_secret,
                        expected_retirement_kind="expired_execution",
                    )
