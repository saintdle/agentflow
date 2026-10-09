import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from agentflow.action_grants import (
    ActionGrantDenied,
    ActionGrantError,
    ActionGrantRequest,
    ActionGrantStore,
    ActionGrantUnavailable,
    AuthenticatedGrantAuthority,
    legacy_action_status,
)


class ActionGrantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.store_path = self.base / "controller-state" / "action-grants.json"
        self.authority = AuthenticatedGrantAuthority(
            workspace_root=self.workspace,
            workflow_root="workflow-root",
            controller_id="controller-1",
            continuity_id="continuity-1",
            authority_secret="authenticated-controller-key",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, authority=None) -> ActionGrantStore:
        return ActionGrantStore(self.store_path, authority or self.authority)

    @staticmethod
    def request(*, action="dispatch", task="task-1", paths=("output.txt",), expiry=None):
        return ActionGrantRequest(
            action=action, task_id=task, declared_paths=paths, expires_at=expiry,
        )

    def test_issue_and_require_exact_signed_scope(self) -> None:
        issued = self.store().issue(self.request(paths=("output.txt", "nested/result.txt")))
        verified = self.store().require(self.request(paths=("nested/result.txt", "output.txt")))

        self.assertEqual(issued, verified)
        self.assertEqual(
            verified.declared_paths,
            (str(self.workspace / "nested/result.txt"), str(self.workspace / "output.txt")),
        )
        self.assertNotIn("hmac", verified.to_dict())
        self.assertNotIn("authority", json.dumps(verified.to_dict()))
        self.assertNotIn("token", json.dumps(verified.to_dict()))
        self.assertEqual(stat.S_IMODE(self.store_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store_path.stat().st_mode), 0o600)

    def test_missing_store_denies_without_legacy_fallback(self) -> None:
        store = self.store()
        with self.assertRaises(ActionGrantUnavailable):
            store.require(self.request())
        self.assertEqual(store.status().mode, "unavailable")
        legacy = legacy_action_status()
        self.assertEqual(legacy.mode, "legacy")
        self.assertFalse(legacy.available)

    def test_tampered_grant_fails_closed(self) -> None:
        self.store().issue(self.request())
        value = json.loads(self.store_path.read_text(encoding="utf-8"))
        only = next(iter(value["grants"].values()))
        only["task_id"] = "task-2"
        self.store_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.store_path, 0o600)

        with self.assertRaises(ActionGrantDenied):
            self.store().require(self.request())

    def test_wrong_key_and_foreign_workspace_fail_closed(self) -> None:
        self.store().issue(self.request())
        wrong_key = AuthenticatedGrantAuthority(
            workspace_root=self.workspace, workflow_root="workflow-root",
            controller_id="controller-1", continuity_id="continuity-1",
            authority_secret="different-authenticated-controller-key",
        )
        with self.assertRaises(ActionGrantDenied):
            self.store(wrong_key).require(self.request())

        other_workspace = self.base / "other-workspace"
        other_workspace.mkdir()
        foreign = AuthenticatedGrantAuthority(
            workspace_root=other_workspace, workflow_root="workflow-root",
            controller_id="controller-1", continuity_id="continuity-1",
            authority_secret="authenticated-controller-key",
        )
        with self.assertRaises(ActionGrantDenied):
            self.store(foreign).require(self.request())

        foreign_controller = AuthenticatedGrantAuthority(
            workspace_root=self.workspace, workflow_root="workflow-root",
            controller_id="controller-2", continuity_id="continuity-2",
            authority_secret="authenticated-controller-key",
        )
        with self.assertRaises(ActionGrantDenied):
            self.store(foreign_controller).require(self.request())

    def test_wrong_task_action_and_broadened_paths_fail_closed(self) -> None:
        store = self.store()
        store.issue(self.request(paths=("output.txt",)))
        for request in (
            self.request(task="task-2"),
            self.request(action="publish"),
            self.request(paths=("output.txt", "another.txt")),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ActionGrantDenied):
                    store.require(request)

    def test_expired_grant_fails_closed(self) -> None:
        store = self.store()
        store.issue(self.request(expiry=100.0))
        self.assertEqual(store.require(self.request(), now=99.0).expires_at, 100.0)
        with self.assertRaisesRegex(ActionGrantDenied, "expired"):
            store.require(self.request(), now=100.0)

    def test_paths_reject_traversal_wildcards_duplicates_and_symlink_aliases(self) -> None:
        store = self.store()
        for paths in (("../escape.txt",), ("*.txt",), ("output.txt", "output.txt")):
            with self.subTest(paths=paths):
                with self.assertRaises(ActionGrantError):
                    store.issue(self.request(paths=paths))

        real = self.workspace / "real"
        real.mkdir()
        alias = self.workspace / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ActionGrantError, "symlink"):
            store.issue(self.request(paths=("alias/output.txt",)))

    def test_store_inside_workspace_or_with_broad_permissions_is_rejected(self) -> None:
        with self.assertRaisesRegex(ActionGrantError, "outside the worker workspace"):
            ActionGrantStore(self.workspace / "state" / "grants.json", self.authority)

        insecure = self.base / "insecure-state"
        insecure.mkdir(mode=0o755)
        os.chmod(insecure, 0o755)
        with self.assertRaisesRegex(ActionGrantError, "0700"):
            ActionGrantStore(insecure / "grants.json", self.authority)

    def test_store_symlink_is_rejected(self) -> None:
        real = self.base / "real-state"
        real.mkdir()
        alias = self.base / "state-alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ActionGrantError, "symlink"):
            ActionGrantStore(alias / "grants.json", self.authority)

    def test_concurrent_issues_preserve_every_existing_grant(self) -> None:
        store = self.store()
        errors = []
        count = 12

        def issue(index: int) -> None:
            try:
                store.issue(self.request(task=f"task-{index}", paths=(f"out/{index}.txt",)))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=issue, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

        document = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(len(document["grants"]), count)
        for index in range(count):
            store.require(self.request(task=f"task-{index}", paths=(f"out/{index}.txt",)))


if __name__ == "__main__":
    unittest.main()
