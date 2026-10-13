from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import action_grants
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
        self.authority = self.authority_with_roots()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def authority_with_roots(self, *roots) -> AuthenticatedGrantAuthority:
        return AuthenticatedGrantAuthority(
            workspace_root=self.workspace,
            workflow_root="workflow-root",
            controller_id="controller-1",
            continuity_id="continuity-1",
            authority_secret="authenticated-controller-key",
            bound_worktree_roots=roots,
        )

    def store(self, authority=None) -> ActionGrantStore:
        return ActionGrantStore(self.store_path, authority or self.authority)

    @staticmethod
    def request(*, action="dispatch", task="task-1", paths=("output.txt",), expiry=None,
                worktree_root=None):
        return ActionGrantRequest(
            action=action, task_id=task, declared_paths=paths, expires_at=expiry,
            worktree_root=worktree_root,
        )

    def test_issue_and_require_exact_signed_scope(self) -> None:
        issued = self.store().issue(self.request(paths=("output.txt", "nested/result.txt")))
        verified = self.store().require(self.request(paths=("nested/result.txt", "output.txt")))

        self.assertEqual(issued, verified)
        self.assertEqual(
            verified.declared_paths,
            (str(self.workspace / "nested/result.txt"), str(self.workspace / "output.txt")),
        )
        self.assertEqual(verified.worktree_root, str(self.workspace))
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

    def test_empty_store_is_authenticated_and_protected_issue_never_recreates_it(self) -> None:
        store = self.store()
        store.initialize_empty()
        document = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "agentflow.action-grants@2")
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["grants"], {})
        self.assertRegex(document["hmac"], r"^[0-9a-f]{64}$")
        self.assertTrue(store.status().available)

        self.store_path.unlink()
        with self.assertRaises(ActionGrantUnavailable):
            store.issue(self.request(), initialize_if_missing=False)
        self.assertFalse(self.store_path.exists())

        store.initialize_empty()
        document = json.loads(self.store_path.read_text(encoding="utf-8"))
        document["grants"] = {"injected": {}}
        self.store_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.store_path, 0o600)
        self.assertFalse(store.status().available)
        with self.assertRaises(ActionGrantDenied):
            store.require(self.request())

    def test_status_accepts_an_absent_lock_without_creating_it(self) -> None:
        store = self.store()
        request = self.request()
        store.issue(request)
        store.lock_path.unlink()

        status = store.status()

        self.assertEqual(status.mode, "protected")
        self.assertTrue(status.available)
        self.assertFalse(store.lock_path.exists())
        store.require(request)
        self.assertTrue(store.lock_path.exists())

    def test_status_rejects_lock_directory_and_unsafe_permissions_read_only(self) -> None:
        store = self.store()
        request = self.request()
        store.issue(request)
        lock_path = store.lock_path
        lock_path.unlink()
        lock_path.mkdir(mode=0o700)
        directory_mode = stat.S_IMODE(lock_path.stat().st_mode)

        status = store.status()
        self.assertEqual(status.mode, "unavailable")
        self.assertFalse(status.available)
        self.assertTrue(lock_path.is_dir())
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), directory_mode)
        with self.assertRaisesRegex(ActionGrantError, "not a regular file"):
            store.require(request)
        self.assertTrue(lock_path.is_dir())

        lock_path.rmdir()
        lock_path.write_text("", encoding="utf-8")
        os.chmod(lock_path, 0o644)
        original_mode = stat.S_IMODE(lock_path.stat().st_mode)
        status = store.status()
        self.assertEqual(status.mode, "unavailable")
        self.assertIn("permissions", status.reason)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), original_mode)
        with self.assertRaisesRegex(ActionGrantError, "permissions"):
            store.require(request)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), original_mode)

    def test_status_rejects_lock_owned_by_another_user_without_mutating_it(self) -> None:
        store = self.store()
        store.issue(self.request())
        lock_path = store.lock_path
        original_bytes = lock_path.read_bytes()
        path_class = type(lock_path)
        original_lstat = path_class.lstat

        def foreign_owner(path):
            info = original_lstat(path)
            if path == lock_path:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
            return info

        with mock.patch.object(path_class, "lstat", autospec=True, side_effect=foreign_owner):
            status = store.status()

        self.assertEqual(status.mode, "unavailable")
        self.assertIn("owned", status.reason)
        self.assertEqual(lock_path.read_bytes(), original_bytes)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

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

    def test_expiry_is_sampled_after_locked_read_and_match(self) -> None:
        store = self.store()
        store.issue(self.request(expiry=100.0))
        original_locked = store._locked
        entered_lock = threading.Event()
        release_lock = threading.Event()
        failures = []

        @contextmanager
        def delayed_lock(*, create_directory):
            with original_locked(create_directory=create_directory):
                entered_lock.set()
                if not release_lock.wait(timeout=5):
                    raise TimeoutError("test did not release the grant lock")
                yield

        def verify() -> None:
            try:
                store.require(self.request())
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        # The old implementation sampled 99 before waiting in ``_locked``;
        # this implementation samples 101 only after the delayed lock yields.
        with mock.patch.object(store, "_locked", side_effect=delayed_lock), \
             mock.patch("agentflow.action_grants.time.time", side_effect=lambda: 101.0 if entered_lock.is_set() else 99.0):
            thread = threading.Thread(target=verify)
            thread.start()
            self.assertTrue(entered_lock.wait(timeout=5))
            release_lock.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ActionGrantDenied)
        self.assertIn("expired", str(failures[0]))

    def test_explicit_worktree_root_is_signed_and_exact(self) -> None:
        approved = self.base / "approved-worktree"
        unapproved = self.base / "unapproved-worktree"
        approved.mkdir()
        unapproved.mkdir()
        store = self.store(self.authority_with_roots(approved))
        issued = store.issue(self.request(
            paths=("result.txt",), worktree_root=str(approved),
        ))
        verified = store.require(self.request(
            paths=("result.txt",), worktree_root=str(approved),
        ))
        self.assertEqual(issued, verified)
        self.assertEqual(verified.worktree_root, str(approved))
        self.assertEqual(verified.declared_paths, (str(approved / "result.txt"),))

        with self.assertRaises(ActionGrantDenied):
            store.require(self.request(paths=("result.txt",), worktree_root=str(unapproved)))
        with self.assertRaisesRegex(ActionGrantError, "outside the exact bound worktree root"):
            store.issue(self.request(
                paths=(str(self.workspace / "output.txt"),), worktree_root=str(approved),
            ))

    def test_external_store_supports_default_and_explicit_worktree_roots(self) -> None:
        approved = self.base / "approved-worktree"
        second = self.base / "second-approved-worktree"
        approved.mkdir()
        second.mkdir()
        authority = self.authority_with_roots(approved, second)
        store = self.store(authority)
        store.issue(self.request(paths=("workspace-result.txt",)))
        store.issue(self.request(paths=("worktree-result.txt",), worktree_root=str(approved)))
        store.issue(self.request(paths=("second-result.txt",), worktree_root=str(second)))
        reopened = ActionGrantStore(self.store_path, authority)
        self.assertEqual(
            reopened.require(self.request(paths=("workspace-result.txt",))).worktree_root,
            str(self.workspace),
        )
        self.assertEqual(
            reopened.require(self.request(
                paths=("worktree-result.txt",), worktree_root=str(approved),
            )).worktree_root,
            str(approved),
        )
        self.assertEqual(
            reopened.require(self.request(
                paths=("second-result.txt",), worktree_root=str(second),
            )).worktree_root,
            str(second),
        )
        self.assertEqual(reopened.status().mode, "protected")

    def test_store_inside_explicit_worktree_fails_closed_for_issue_require_and_status(self) -> None:
        approved = self.base / "approved-worktree"
        approved.mkdir()
        request = self.request(paths=("result.txt",), worktree_root=str(approved))
        unsafe_path = approved / "controller-state" / "action-grants.json"
        with self.assertRaisesRegex(ActionGrantError, "bound worktree root"):
            ActionGrantStore(unsafe_path, self.authority_with_roots(approved))
        self.assertFalse(unsafe_path.parent.exists())
        self.assertFalse(unsafe_path.with_name(".action-grants.json.lock").exists())

        safe_store = self.store(self.authority_with_roots(approved))
        safe_store.issue(request)
        unsafe_path.parent.mkdir(mode=0o700)
        unsafe_path.write_bytes(self.store_path.read_bytes())
        os.chmod(unsafe_path, 0o600)
        unsafe_store = ActionGrantStore(unsafe_path, self.authority)
        self.assertEqual(unsafe_store.status().mode, "unavailable")
        self.assertFalse(unsafe_store.lock_path.exists())
        with self.assertRaisesRegex(ActionGrantDenied, "not registered"):
            unsafe_store.require(request)

    def test_document_swap_locations_inside_trusted_roots_are_denied_prelock(self) -> None:
        approved = self.base / "previously-approved-worktree"
        second = self.base / "second-approved-worktree"
        approved.mkdir()
        second.mkdir()
        authority = self.authority_with_roots(approved, second)
        for root in (approved, second):
            for operation in ("issue", "require"):
                unsafe_path = root / operation / "controller-state" / "action-grants.json"
                lock_path = unsafe_path.with_name(".action-grants.json.lock")
                with self.subTest(root=root, operation=operation):
                    with self.assertRaisesRegex(ActionGrantError, "bound worktree root"):
                        ActionGrantStore(unsafe_path, authority)
                    self.assertFalse(unsafe_path.parent.exists())
                    self.assertFalse(lock_path.exists())

    def test_authority_freezes_registry_roots_and_rejects_unregistered_scope_prelock(self) -> None:
        approved = self.base / "approved-worktree"
        unapproved = self.base / "unapproved-worktree"
        approved.mkdir()
        unapproved.mkdir()
        mutable_roots = [approved]
        authority = AuthenticatedGrantAuthority(
            workspace_root=self.workspace,
            workflow_root="workflow-root",
            controller_id="controller-1",
            continuity_id="continuity-1",
            authority_secret="authenticated-controller-key",
            bound_worktree_roots=mutable_roots,
        )
        mutable_roots.append(unapproved)
        self.assertEqual(authority.bound_worktree_roots, (approved,))

        path = self.base / "unregistered-state" / "grants.json"
        store = ActionGrantStore(path, authority)
        with self.assertRaisesRegex(ActionGrantDenied, "not registered"):
            store.issue(self.request(paths=("result.txt",), worktree_root=str(unapproved)))
        self.assertFalse(path.parent.exists())
        self.assertFalse(store.lock_path.exists())

    def test_case_insensitive_worktree_alias_is_rejected_before_creation(self) -> None:
        approved = self.base / "approved-worktree"
        approved.mkdir()
        case_alias = self.base / "APPROVED-WORKTREE"
        if not case_alias.exists() or not approved.samefile(case_alias):
            self.skipTest("temporary filesystem is case sensitive")

        unsafe_path = case_alias / "controller-state" / "action-grants.json"
        with self.assertRaisesRegex(ActionGrantError, "bound worktree root"):
            ActionGrantStore(unsafe_path, self.authority_with_roots(approved))
        self.assertFalse(unsafe_path.parent.exists())
        self.assertFalse(unsafe_path.with_name(".action-grants.json.lock").exists())

    def test_worktree_root_and_declared_paths_reject_symlinks_and_lexical_aliases(self) -> None:
        approved = self.base / "approved-worktree"
        approved.mkdir()
        store = self.store(self.authority_with_roots(approved))
        root_alias = self.base / "worktree-alias"
        root_alias.symlink_to(approved, target_is_directory=True)
        with self.assertRaisesRegex(ActionGrantError, "symlink"):
            store.issue(self.request(paths=("result.txt",), worktree_root=str(root_alias)))
        with self.assertRaisesRegex(ActionGrantError, "aliases"):
            store.issue(self.request(paths=("dir/./result.txt",), worktree_root=str(approved)))
        with self.assertRaisesRegex(ActionGrantError, "aliases"):
            store.issue(self.request(paths=(".",), worktree_root=str(approved)))
        with self.assertRaisesRegex(ActionGrantError, "aliases"):
            store.issue(self.request(
                paths=("result.txt",), worktree_root=f"{approved}/.",
            ))

    @unittest.skipUnless(sys.platform == "darwin", "macOS system alias behavior")
    def test_macos_var_alias_is_allowed_but_arbitrary_symlink_ancestors_are_rejected(self) -> None:
        var = Path("/var")
        if not var.is_symlink() or var.resolve() != Path("/private/var"):
            self.skipTest("host does not expose the standard /var -> /private/var alias")
        action_grants._reject_symlink_ancestors(Path("/var/folders"))

        target = self.base / "real-state"
        target.mkdir()
        alias = self.base / "arbitrary-state-alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ActionGrantError, "symlink"):
            action_grants._reject_symlink_ancestors(alias / "nested")

    def test_repeated_and_trailing_separators_are_rejected_before_normalization(self) -> None:
        store = self.store()
        absolute_path = str(self.workspace / "absolute-result.txt")
        store.issue(self.request(paths=(absolute_path,)))
        self.assertEqual(store.require(self.request(paths=(absolute_path,))).declared_paths, (absolute_path,))
        for paths in (("dir//result.txt",), ("dir/result.txt/",)):
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(ActionGrantError, "aliases"):
                    store.issue(self.request(paths=paths))
                with self.assertRaisesRegex(ActionGrantError, "aliases"):
                    store.require(self.request(paths=paths))

    def test_tampered_stored_worktree_root_fails_closed(self) -> None:
        approved = self.base / "approved-worktree"
        approved.mkdir()
        store = self.store(self.authority_with_roots(approved))
        store.issue(self.request(paths=("result.txt",), worktree_root=str(approved)))
        value = json.loads(self.store_path.read_text(encoding="utf-8"))
        only = next(iter(value["grants"].values()))
        only["worktree_root"] = str(self.workspace)
        self.store_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.store_path, 0o600)
        with self.assertRaises(ActionGrantDenied):
            store.require(self.request(paths=("result.txt",), worktree_root=str(approved)))

    def test_paths_reject_traversal_wildcards_duplicates_and_symlink_aliases(self) -> None:
        store = self.store()
        for paths in (
            ("../escape.txt",), ("*.txt",), ("output.txt", "output.txt"),
            (".",), ("dir/.",), ("dir/./file",),
        ):
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
