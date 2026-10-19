from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import beads, cli, controller as controller_backend
from tests import _state_home  # noqa: F401


def receipt(value, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(value), "failure" if code else "")


class BeadsCompatibilityTests(unittest.TestCase):
    def test_minimum_is_131_and_rejects_older_release_lines(self):
        self.assertEqual(beads.MINIMUM_VERSION, (1, 3, 1))
        for version in ((1, 1, 0), (1, 2, 1), (1, 3, 0)):
            with self.subTest(version=version), mock.patch.object(beads, "version", return_value=(version, str(version))):
                with self.assertRaises(beads.BeadsError):
                    beads.require_supported_version()

    def test_heartbeat_checks_exact_actor_task_and_receipt(self):
        with mock.patch.object(beads, "run", return_value=receipt({"id": "task", "owner": "actor", "status": "heartbeat", "schema_version": 1})) as run:
            beads.heartbeat_issue(Path("/tmp"), "task", actor="actor")
        self.assertEqual(run.call_args.args[1:], ("heartbeat", "task", "--actor", "actor", "--json"))
        for body in ({"id": "other", "owner": "actor", "status": "heartbeat"}, {}, []):
            with mock.patch.object(beads, "run", return_value=receipt(body)), self.assertRaises(beads.BeadsError):
                beads.heartbeat_issue(Path("/tmp"), "task", actor="actor")
        with mock.patch.object(beads, "run", return_value=receipt({}, 1)), self.assertRaises(beads.BeadsError):
            beads.heartbeat_issue(Path("/tmp"), "task", actor="actor")

    def test_topology_uses_metadata_and_both_config_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "metadata.json").write_text(json.dumps({"dolt_mode": "server"}))
            for config in ("dolt.shared-server: true\n", "dolt:\n  shared-server: true # shared\n"):
                (directory / "config.yaml").write_text(config)
                self.assertEqual(beads.workspace_mode({"path": str(directory)}), "shared-server")
            (directory / "config.yaml").write_text("other:\n  shared-server: true\n")
            self.assertEqual(beads.workspace_mode({"path": str(directory)}), "server")
            (directory / "config.yaml").write_text("dolt:\n  unrelated:\n    shared-server: true\n")
            self.assertEqual(beads.workspace_mode({"path": str(directory)}), "server")
            (directory / "metadata.json").write_text(json.dumps({"dolt_mode": "proxied-server"}))
            self.assertEqual(beads.workspace_mode({"path": str(directory), "database_path": str(directory / "dolt")}), "proxied-server")

    def test_proxy_cap_preflight_does_not_spawn_or_silently_drop_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".beads"
            directory.mkdir()
            (directory / "metadata.json").write_text(json.dumps({"dolt_mode": "proxied-server"}))
            environment = {"BEADS_DIR": str(directory), "BEADS_MAX_ROWS": "25"}
            with mock.patch.dict(os.environ, environment), mock.patch.object(beads, "executable", return_value="bd"), mock.patch.object(beads, "_is_git_repository", return_value=False), mock.patch.object(beads.subprocess, "run") as run:
                with self.assertRaisesRegex(beads.BeadsError, "will not discard"):
                    beads.run(Path(tmp), "ready", "--brief", "--json")
                run.assert_not_called()
            with self.assertRaises(beads.BeadsError):
                beads.preflight_topology(Path(tmp), environment={"BEADS_DIR": str(directory)}, arguments=("ready", "--max-rows=10"))
            beads.preflight_topology(Path(tmp), environment={"BEADS_DIR": str(directory)})

    def test_close_passes_the_recorded_actor_without_forcing_ownership(self):
        with mock.patch.object(beads, "run", return_value=receipt({})) as run:
            beads.close_issue(Path("/tmp"), "task", "accepted evidence", actor="original-actor")
            self.assertEqual(run.call_args.args[1:], ("close", "task", "--reason", "accepted evidence", "--actor", "original-actor"))
            self.assertNotIn("--force", run.call_args.args)

    def test_brief_selection_exact_claims_and_returns_full_issue(self):
        full = {"id": "task", "description": "full contract", "acceptance_criteria": "evidence"}
        exact = mock.Mock(issue=full)
        with mock.patch.object(beads, "run", side_effect=[receipt([]), receipt([{"id": "task"}])]) as run, mock.patch.object(beads, "claim_issue_exact", return_value=exact) as claim:
            self.assertEqual(beads.claim_ready(Path("/tmp"), parent="root", labels=["af:stage:implement"], actor="actor"), full)
            claim.assert_called_once_with(Path("/tmp"), "task", root="root", actor="actor", labels=["af:stage:implement"])
            for call in run.call_args_list:
                self.assertIn("--brief", call.args)
                self.assertNotIn("--claim", call.args)

    def test_selection_race_does_not_fall_through_to_another_task(self):
        with mock.patch.object(beads, "run", return_value=receipt([{"id": "task"}])) as run, mock.patch.object(beads, "claim_issue_exact", side_effect=beads.ClaimConflict("claim_rejected")):
            with self.assertRaises(beads.ClaimConflict):
                beads.claim_ready(Path("/tmp"), parent="root", labels=[], actor="actor")
            self.assertEqual(run.call_count, 1)

    def test_summary_projection_is_explicit_and_keeps_task_contract(self):
        with mock.patch.object(beads, "run", return_value=receipt([{"id": "task", "description": "full"}])) as run:
            issue = beads.get_issue(Path("/tmp"), "task", brief_dependencies=True)
            self.assertIn("--brief-deps", run.call_args.args)
            self.assertEqual(issue["description"], "full")
            self.assertEqual(issue["_agentflow_projection"], "brief-deps")

    def test_graph_discovery_is_brief_and_explicitly_complete(self):
        rows = [{"id": "root"}, {"id": "child", "parent": "root"}]
        with mock.patch.object(beads, "run", return_value=receipt(rows)) as run:
            result = beads.root_descendants(Path("/tmp"), "root")
            self.assertIn("--brief", run.call_args.args)
            arguments = run.call_args.args
            self.assertEqual(arguments[arguments.index("--limit") + 1], "0")
            self.assertEqual(result[0]["_agentflow_projection"], "brief")


class NativeTaskSupervisionTests(unittest.TestCase):
    def test_claim_fences_cannot_be_stored_in_worker_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = cli._native_claim_fence_path(root, "workflow", "task")
            with self.assertRaises(ValueError):
                path.relative_to(root)
            self.assertNotEqual(path, cli._native_claim_fence_path(root, "other-workflow", "task"))
            with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(root / "state")}):
                with self.assertRaises(ValueError):
                    cli._native_claim_fence_path(root, "workflow", "task")

    def test_persisted_fence_blocks_launch_and_result_authority_before_beads_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = cli._controller_state_dir(root, "workflow") / "state.json"
            controller = controller_backend.RootController(str(root), "controller", state_path=state)
            lease = controller.acquire()
            cli._fence_native_claim(root, "workflow", "task", "lost claim")
            # Both protected launch and authenticated result ingestion use
            # this exact authority check under the root controller fence.
            with mock.patch.object(beads, "get_issue") as read, mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                with self.assertRaisesRegex(ValueError, "fenced"):
                    cli._verify_launch_authority(root, "task", "opaque-token", lease.token, workflow_root="workflow", actor="controller")
                read.assert_not_called()
                heartbeat.assert_not_called()

    def test_all_actors_renew_and_loss_fences_before_result_or_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = cli._controller_state_dir(root, "workflow") / "state.json"
            controller = controller_backend.RootController(str(root), "controller", state_path=state)
            lease = controller.acquire()
            for task, actor in (("one", "original-actor"), ("two", "other-actor")):
                controller.reserve_active_task({"task": task, "actor": actor, "claim_id": task, "root": str(root)}, lease=lease)
            def issue(cwd, task):
                actor = "original-actor" if task == "one" else "other-actor"
                return {"id": task, "status": "in_progress", "parent": "workflow", "assignee": actor,
                        "metadata": {"agentflow": {"root": "workflow", "task": task, "actor": actor, "claim_id": task}}}
            for task in ("one", "two"):
                cli._private_atomic_json(
                    cli._native_claim_generation_path(root, "workflow", task),
                    cli._claim_generation_binding(issue(root, task)["metadata"]["agentflow"], "a" * 64),
                )
            with mock.patch.object(beads, "claim_generation", return_value="a" * 64), mock.patch.object(beads, "get_issue", side_effect=issue), mock.patch.object(beads, "heartbeat_issue", side_effect=[beads.BeadsError("claim lost"), None]) as heartbeat, mock.patch.object(cli, "_controller_step_parallel") as parallel, mock.patch.object(cli, "_controller_step_serial") as serial:
                payload, stop = cli._controller_step(argparse.Namespace(workflow_root="workflow"), controller, root, lease, operation="resume")
                self.assertTrue(stop)
                self.assertEqual(payload["stop_reason"], "USER_ACTION_REQUIRED")
                self.assertEqual([call.kwargs["actor"] for call in heartbeat.call_args_list], ["original-actor", "other-actor"])
                parallel.assert_not_called()
                serial.assert_not_called()
            # A restarted controller cannot make an old worker valid again.
            with self.assertRaisesRegex(ValueError, "fenced"):
                cli._assert_native_claim_unfenced(root, "workflow", "one")
            cli._assert_native_claim_unfenced(root, "workflow", "two")

    def test_terminal_without_authenticated_disposition_is_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            controller = mock.Mock()
            controller.active_tasks.return_value = [{"task": "task", "actor": "actor", "claim_id": "claim"}]
            with mock.patch.object(beads, "get_issue", return_value={"id": "task", "status": "closed"}), mock.patch.object(cli, "_herdr_session_record", return_value={}), mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                self.assertEqual(cli._renew_native_task_leases(controller, root, "workflow"), ["task"])
                heartbeat.assert_not_called()

    def test_accepted_close_crash_can_recover_without_renewing_closed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            controller = mock.Mock()
            controller.active_tasks.return_value = [{"task": "task", "actor": "actor", "claim_id": "claim"}]
            with mock.patch.object(beads, "get_issue", return_value={"id": "task", "status": "closed"}), mock.patch.object(cli, "_herdr_session_record", return_value={"return_channel": {"state": "consumed"}}), mock.patch.object(beads, "heartbeat_issue") as heartbeat:
                self.assertEqual(cli._renew_native_task_leases(controller, root, "workflow"), [])
                heartbeat.assert_not_called()
