import contextlib
import json
import os
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
