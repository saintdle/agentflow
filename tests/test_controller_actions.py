from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import secrets
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import action_grants, controller_actions as controller_actions_module
from agentflow.action_grants import ActionGrantRequest
from agentflow.controller import RootController
from agentflow.controller_actions import (
    ControllerActionAuthority,
    ControllerActionDenied,
    ControllerActionStatus,
    ControllerActionUnavailable,
)


class ControllerActionAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.worktree = self.base / "worker-one"
        self.workspace.mkdir()
        self.worktree.mkdir()
        self.control = self.base / "controller"
        self.control.mkdir(mode=0o700)
        self.credentials_dir = self.base / "credentials"
        self.credentials_dir.mkdir(mode=0o700)
        self.credentials_path = self.credentials_dir / "workflow.json"
        self.controller = RootController(
            self.workspace,
            "controller-1",
            state_path=self.control / "state.json",
            checkpoint_path=self.control / "checkpoint.json",
        )
        self.lease = self.controller.acquire()
        self.authority_secret = secrets.token_urlsafe(32)
        self._write_credentials(
            self.credentials_path,
            workspace=self.workspace,
            workflow="workflow-root",
            lease=self.lease,
            authority_secret=self.authority_secret,
        )
        self.authority = self._authority(self.controller, self.lease, self.credentials_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_credentials(
        self,
        path: Path,
        *,
        workspace: Path,
        workflow: str,
        lease,
        authority_secret: str,
    ) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "agentflow.controller_credentials",
            "version": 2,
            "resume_secret": lease.resume_secret,
            "authority_secret": authority_secret,
            "continuity_id": lease.continuity_id,
            "workspace_root": str(workspace.resolve()),
            "workflow_root": workflow,
        }), encoding="utf-8")
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)

    @staticmethod
    def _authority(controller, lease, credential_path: Path) -> ControllerActionAuthority:
        return ControllerActionAuthority(
            controller,
            lease,
            workflow_root="workflow-root",
            credential_path=credential_path,
        )

    def _activate(self) -> None:
        self.authority.register_worktrees((self.worktree,))
        self.authority.activate()

    def _request(self, *, task="task-1", paths=("result.txt",), expiry=None, root=None):
        return ActionGrantRequest(
            action="dispatch",
            task_id=task,
            declared_paths=paths,
            expires_at=expiry,
            worktree_root=root or self.worktree,
        )

    def test_owned_controller_registers_activates_issues_and_executes_exact_scope(self) -> None:
        self.assertEqual(self.authority.status().mode, "legacy")
        roots = self.authority.register_worktrees((self.worktree,))
        self.assertEqual(roots, (str(self.worktree),))
        self.authority.activate()

        request = self._request(paths=("nested/result.txt", "result.txt"))
        issued = self.authority.issue(request)
        self.assertEqual(issued.task_id, "task-1")
        self.assertEqual(self.authority.require(request), issued)

        observed = []

        def fenced_callback():
            descriptor = os.open(self.controller.lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            observed.append("ran")
            return "done"

        result = self.authority.execute(request, fenced_callback)
        self.assertEqual(result, "done")
        self.assertEqual(observed, ["ran"])

        status = self.authority.status()
        self.assertEqual(status.mode, "protected")
        self.assertTrue(status.available)
        self.assertEqual(status.registered_worktree_roots, (str(self.worktree),))
        self.assertEqual([grant.state for grant in status.grants], ["valid"])
        safe = json.dumps(status.to_dict())
        for secret in (self.authority_secret, self.lease.resume_secret, self.lease.token):
            self.assertNotIn(secret, safe)
        self.assertNotIn("authority_hmac", safe)

    def test_missing_foreign_broadened_and_unregistered_grants_never_call_callback(self) -> None:
        self._activate()
        calls = []
        callback = lambda: calls.append("ran")

        with self.assertRaises(ControllerActionDenied):
            self.authority.execute(self._request(), callback)
        self.authority.issue(self._request(paths=("result.txt",)))
        for request in (
            self._request(task="foreign-task"),
            self._request(paths=("result.txt", "extra.txt")),
            self._request(root=self.workspace),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ControllerActionDenied):
                    self.authority.execute(request, callback)
        self.assertEqual(calls, [])

    def test_expired_grants_are_denied_and_projected_without_mutation(self) -> None:
        self._activate()
        expiring = self._request(expiry=100.0)
        self.authority.issue(expiring)
        grants_path = self.authority.grants_path
        before = grants_path.read_bytes()

        with mock.patch.object(action_grants.time, "time", return_value=100.0):
            status = self.authority.status()
            self.assertEqual([grant.state for grant in status.grants], ["expired"])
            calls = []
            with self.assertRaises(ControllerActionDenied):
                self.authority.execute(expiring, lambda: calls.append("ran"))
        self.assertEqual(calls, [])
        self.assertEqual(grants_path.read_bytes(), before)

    def test_stale_lease_and_foreign_credential_origin_are_unavailable(self) -> None:
        self._activate()
        self.authority.issue(self._request())
        replacement = RootController(
            self.workspace,
            "controller-1",
            state_path=self.control / "state.json",
            checkpoint_path=self.control / "checkpoint.json",
        )
        replacement.acquire(resume_proof=self.lease.resume_secret)
        called = []
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.execute(self._request(), lambda: called.append("ran"))
        self.assertEqual(called, [])

        other_workspace = self.base / "other-workspace"
        other_workspace.mkdir()
        other_control = self.base / "other-controller"
        other_control.mkdir(mode=0o700)
        foreign_controller = RootController(
            other_workspace,
            "controller-2",
            state_path=other_control / "state.json",
            checkpoint_path=other_control / "checkpoint.json",
        )
        foreign_lease = foreign_controller.acquire()
        foreign = self._authority(foreign_controller, foreign_lease, self.credentials_path)
        self.assertEqual(foreign.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            foreign.execute(self._request(root=other_workspace), lambda: called.append("foreign"))
        self.assertEqual(called, [])

    def test_alias_and_immutable_registry_changes_are_rejected(self) -> None:
        for alias in (f"{self.worktree}/.", f"{self.worktree}/../{self.worktree.name}"):
            with self.subTest(alias=alias):
                fresh = self._authority(self.controller, self.lease, self.credentials_path)
                with self.assertRaises(ControllerActionDenied):
                    fresh.register_worktrees((alias,))

        self._activate()
        with self.assertRaises(ControllerActionDenied):
            self.authority.register_worktrees((self.worktree, self.workspace))
        self.assertEqual(self.authority.status().registered_worktree_roots, (str(self.worktree),))

    def test_storage_inside_a_registered_root_is_rejected_before_write(self) -> None:
        nested_credentials = self.base / "nested-authority"
        nested_credentials.mkdir(mode=0o700)
        nested_path = nested_credentials / "workflow.json"
        self._write_credentials(
            nested_path,
            workspace=self.workspace,
            workflow="workflow-root",
            lease=self.lease,
            authority_secret=self.authority_secret,
        )
        helper = self._authority(self.controller, self.lease, nested_path)
        before = list(nested_credentials.iterdir())
        with self.assertRaises(ControllerActionUnavailable):
            helper.register_worktrees((nested_credentials,))
        self.assertFalse((nested_credentials / "action-authority").exists())
        self.assertEqual(set(nested_credentials.iterdir()), set(before))

    def test_partial_or_downgraded_protected_state_never_falls_back_to_legacy(self) -> None:
        self._activate()
        marker = json.loads(self.authority.mode_path.read_text(encoding="utf-8"))
        marker["mode"] = "legacy"
        self.authority.mode_path.write_text(json.dumps(marker), encoding="utf-8")
        os.chmod(self.authority.mode_path, 0o600)
        self.assertEqual(self.authority.status().mode, "unavailable")
        called = []
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.execute(self._request(), lambda: called.append("ran"))
        self.assertEqual(called, [])

        self.authority.mode_path.unlink()
        self.assertEqual(self.authority.status().mode, "unavailable")
        self.authority.registry_path.unlink()
        self.assertEqual(self.authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.activate()

    def test_activation_refuses_any_preexisting_checkpoint_and_preserves_state_extensions(self) -> None:
        state = json.loads(self.controller.state_path.read_text(encoding="utf-8"))
        state["extension_from_owner"] = {"preserve": True}
        self.controller.state_path.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(self.controller.state_path, 0o600)
        self.authority.register_worktrees((self.worktree,))
        self.controller.checkpoint_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ControllerActionDenied):
            self.authority.activate()
        self.assertEqual(
            json.loads(self.controller.state_path.read_text(encoding="utf-8"))["extension_from_owner"],
            {"preserve": True},
        )

    def test_status_is_read_only_when_protected_mode_has_no_grants(self) -> None:
        self._activate()
        self.assertTrue(self.authority.grants_path.exists())
        before = self.authority.grants_path.read_bytes()
        status = self.authority.status()
        self.assertEqual(status.mode, "protected")
        self.assertTrue(status.available)
        self.assertEqual(json.loads(self.authority.grants_path.read_text()), {
            "schema": action_grants.SCHEMA,
            "version": 2,
            "grants": {},
            "hmac": json.loads(before)["hmac"],
        })
        self.assertEqual(self.authority.grants_path.read_bytes(), before)

    def test_activated_state_loss_never_recreates_grants_or_protected_metadata(self) -> None:
        self._activate()
        request = self._request()
        self.authority.issue(request)
        self.authority.grants_path.unlink()

        self.assertEqual(self.authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.issue(self._request(task="task-2"))
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.require(request)
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.execute(request, lambda: self.fail("callback ran after store loss"))
        self.assertFalse(self.authority.grants_path.exists())

        self.authority.mode_path.unlink()
        self.authority.activation_path.unlink()
        self.authority.registry_path.unlink()
        shutil.rmtree(self.authority.state_directory)

        self.assertEqual(self.authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.register_worktrees((self.worktree,))
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.activate()
        self.assertFalse(self.authority.state_directory.exists())

    def test_crash_after_pending_anchor_cannot_resume_or_reinitialize_activation(self) -> None:
        self.authority.register_worktrees((self.worktree,))
        original_write = controller_actions_module._atomic_private_json

        def fail_mode_write(path, value):
            if path == self.authority.mode_path:
                raise OSError("injected activation crash")
            original_write(path, value)

        with mock.patch("agentflow.controller_actions._atomic_private_json", side_effect=fail_mode_write):
            with self.assertRaises(ControllerActionUnavailable):
                self.authority.activate()

        credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        self.assertEqual(credentials["protected_action_anchor"]["state"], "pending")
        self.assertFalse(self.authority.mode_path.exists())
        self.assertEqual(self.authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.register_worktrees((self.worktree,))
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.activate()

    def test_anchored_helper_rejects_downgraded_v1_store_before_any_action(self) -> None:
        self._activate()
        request = self._request()
        self.authority.issue(request)
        legacy = json.loads(self.authority.grants_path.read_text(encoding="utf-8"))
        legacy.pop("hmac")
        legacy["schema"] = "agentflow.action-grants@1"
        legacy["version"] = 1
        self.authority.grants_path.write_text(json.dumps(legacy), encoding="utf-8")
        os.chmod(self.authority.grants_path, 0o600)

        self.assertEqual(self.authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.issue(self._request(task="new-task"))
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.require(request)
        calls = []
        with self.assertRaises(ControllerActionUnavailable):
            self.authority.execute(request, lambda: calls.append("ran"))
        self.assertEqual(calls, [])

    def test_custom_and_canonical_credential_anchors_must_remain_equal(self) -> None:
        selected_path = self.credentials_dir / "selected.json"
        selected_path.write_bytes(self.credentials_path.read_bytes())
        os.chmod(selected_path, 0o600)
        authority = ControllerActionAuthority(
            self.controller,
            self.lease,
            workflow_root="workflow-root",
            credential_path=selected_path,
            canonical_credential_path=self.credentials_path,
        )
        authority.register_worktrees((self.worktree,))
        authority.activate()
        canonical = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        self.assertEqual(canonical["protected_action_anchor"], selected["protected_action_anchor"])
        self.assertEqual(authority.status().mode, "protected")
        default_authority = ControllerActionAuthority(
            self.controller,
            self.lease,
            workflow_root="workflow-root",
            credential_path=self.credentials_path,
        )
        self.assertEqual(default_authority.status().mode, "protected")

        selected.pop("protected_action_anchor")
        selected_path.write_text(json.dumps(selected), encoding="utf-8")
        os.chmod(selected_path, 0o600)
        self.assertEqual(authority.status().mode, "unavailable")
        with self.assertRaises(ControllerActionUnavailable):
            authority.activate()

    def test_custom_mirror_crash_preserves_the_signed_registration_root_set(self) -> None:
        selected_path = self.credentials_dir / "selected.json"
        selected_path.write_bytes(self.credentials_path.read_bytes())
        os.chmod(selected_path, 0o600)
        self.credentials_path.unlink()
        authority = ControllerActionAuthority(
            self.controller,
            self.lease,
            workflow_root="workflow-root",
            credential_path=selected_path,
            canonical_credential_path=self.credentials_path,
        )
        original_write = controller_actions_module._atomic_private_json

        def fail_canonical_mirror(path, value):
            if path == self.credentials_path and value.get("schema") == "agentflow.controller_credentials":
                raise OSError("injected canonical mirror crash")
            original_write(path, value)

        with mock.patch.object(
            controller_actions_module,
            "_atomic_private_json",
            side_effect=fail_canonical_mirror,
        ):
            with self.assertRaises(ControllerActionUnavailable):
                authority.register_worktrees((self.worktree,))

        self.assertTrue(authority.registry_path.is_file())
        self.assertFalse(self.credentials_path.exists())
        alternate = self.base / "worker-two"
        alternate.mkdir()
        with self.assertRaises(ControllerActionDenied):
            authority.register_worktrees((alternate,))
        self.assertEqual(authority.register_worktrees((self.worktree,)), (str(self.worktree),))
        self.assertTrue(self.credentials_path.is_file())

    def test_custom_activation_crashes_at_each_anchor_write_boundary_fail_closed(self) -> None:
        phases = (
            ("pending-canonical", "pending", "canonical", True),
            ("pending-selected", "pending", "selected", False),
            ("active-canonical", "active", "canonical", False),
            ("active-selected", "active", "selected", False),
        )
        for label, anchor_state, target_path, retry_allowed in phases:
            with self.subTest(phase=label), tempfile.TemporaryDirectory() as temp:
                base = Path(temp).resolve()
                workspace = base / "workspace"
                worker = base / "worker"
                control = base / "control"
                credentials_dir = base / "credentials"
                for directory in (workspace, worker, control, credentials_dir):
                    directory.mkdir(mode=0o700)
                canonical_path = credentials_dir / "canonical.json"
                selected_path = credentials_dir / "selected.json"
                controller = RootController(
                    workspace,
                    "controller-1",
                    state_path=control / "state.json",
                    checkpoint_path=control / "checkpoint.json",
                )
                lease = controller.acquire()
                secret = secrets.token_urlsafe(32)
                self._write_credentials(
                    canonical_path,
                    workspace=workspace,
                    workflow="workflow-root",
                    lease=lease,
                    authority_secret=secret,
                )
                selected_path.write_bytes(canonical_path.read_bytes())
                os.chmod(selected_path, 0o600)
                authority = ControllerActionAuthority(
                    controller,
                    lease,
                    workflow_root="workflow-root",
                    credential_path=selected_path,
                    canonical_credential_path=canonical_path,
                )
                authority.register_worktrees((worker,))
                original_write = controller_actions_module._atomic_private_json

                def fail_selected_boundary(path, value):
                    anchor = value.get("protected_action_anchor") if isinstance(value, dict) else None
                    target = canonical_path if target_path == "canonical" else selected_path
                    if path == target and isinstance(anchor, dict) and anchor.get("state") == anchor_state:
                        raise OSError("injected credential anchor crash")
                    original_write(path, value)

                with mock.patch.object(
                    controller_actions_module,
                    "_atomic_private_json",
                    side_effect=fail_selected_boundary,
                ):
                    with self.assertRaises(ControllerActionUnavailable):
                        authority.activate()

                self.assertEqual(authority.status().mode, "unavailable")
                with self.assertRaises(ControllerActionUnavailable):
                    authority.issue(self._request())
                if retry_allowed:
                    authority.activate()
                    self.assertEqual(authority.status().mode, "protected")
                else:
                    with self.assertRaises(ControllerActionUnavailable):
                        authority.activate()


if __name__ == "__main__":
    unittest.main()
