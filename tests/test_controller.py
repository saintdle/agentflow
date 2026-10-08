from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import beads, checkpoint, controller as controller_module
from agentflow.controller import (
    TERMINAL_STATES,
    ControllerError,
    DuplicateController,
    DuplicateSupervisor,
    FencedLease,
    Lease,
    RootController,
    RootViolation,
    StaleLease,
)


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ControllerTests(unittest.TestCase):
    def test_supervisor_lock_is_exclusive_per_root_and_released_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            first = RootController("root", "one", state_path=state)
            second = RootController("root", "one", state_path=state)
            other_root = RootController(
                "other-root", "one", state_path=Path(tmp) / "other.json"
            )

            with first.supervisor_lock():
                with self.assertRaises(DuplicateSupervisor):
                    with second.supervisor_lock():
                        pass
                # Independent workflow roots have independent supervisors.
                with other_root.supervisor_lock():
                    pass

            # A process restart may acquire the lock once the prior owner exits.
            with second.supervisor_lock():
                pass

    def test_supervisor_lock_is_exclusive_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            source_root = Path(__file__).resolve().parents[1] / "src"
            script = "\n".join((
                "import sys",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "from agentflow.controller import RootController",
                "controller = RootController('root', 'one', state_path=Path(sys.argv[2]))",
                "with controller.supervisor_lock():",
                "    print('ready', flush=True)",
                "    sys.stdin.readline()",
            ))
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(source_root), str(state)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                contender = RootController("root", "one", state_path=state)
                with self.assertRaises(DuplicateSupervisor):
                    with contender.supervisor_lock():
                        pass
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        pass
                    process.stdin.close()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_deadline_incomplete_preserves_live_task_for_safe_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            checkpoint_path = Path(tmp) / "checkpoint.json"
            controller = RootController(
                "root", "one", state_path=state, checkpoint_path=checkpoint_path
            )
            lease = controller.acquire()
            controller.reserve_active_task(
                {"task": "task-1", "root": "root", "actor": "one", "claim_id": "claim-1"},
                lease=lease,
            )
            controller.bind_active_task("task-1", session_id="session-1", lease=lease)

            incomplete = controller.mark_incomplete("DEADLINE_EXCEEDED", lease=lease)

            self.assertEqual(incomplete.state, "incomplete")
            self.assertFalse(incomplete.terminal)
            self.assertEqual(incomplete.checkpoint["terminal_reason"], "DEADLINE_EXCEEDED")
            self.assertEqual(incomplete.checkpoint["active_tasks"], [{
                "task": "task-1", "phase": "dispatch", "actor": "one",
                "claim_id": "claim-1", "session_id": "session-1", "state": "running",
            }])

    def test_draining_deadline_checkpoint_rejects_new_resume_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            checkpoint_path = Path(tmp) / "checkpoint.json"
            controller = RootController(
                "root", "one", state_path=state, checkpoint_path=checkpoint_path
            )
            lease = controller.acquire()
            draining = controller.begin_draining("task-1 failed", lease=lease)
            incomplete = controller.mark_incomplete(lease=lease)
            self.assertEqual(incomplete.state, "incomplete")
            self.assertEqual(incomplete.checkpoint["state"], "draining")
            self.assertEqual(incomplete.checkpoint["status"], "incomplete")
            self.assertEqual(
                incomplete.checkpoint["terminal_reason"],
                draining.checkpoint["terminal_reason"],
            )

            dispatch = mock.Mock(return_value={"session_id": "unexpected"})
            result = controller.resume(
                [{"task": "ready-child", "root": "root", "ready": True}],
                dispatch=dispatch,
                lease=lease,
            )

            self.assertEqual(result.state, "incomplete")
            self.assertEqual(result.checkpoint["active_tasks"], [])
            self.assertEqual(checkpoint.admission_phase(result.checkpoint), "draining")
            dispatch.assert_not_called()

    def test_deadline_does_not_reopen_terminal_checkpoint(self) -> None:
        for state in ("completed", "blocked"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                state_path = Path(tmp) / "controller.json"
                checkpoint_path = Path(tmp) / "checkpoint.json"
                controller = RootController(
                    "root", "one", state_path=state_path, checkpoint_path=checkpoint_path
                )
                lease = controller.acquire()
                terminal = controller.halt(state, "original terminal evidence", lease=lease)

                after_deadline = controller.mark_incomplete(lease=lease)

                self.assertTrue(after_deadline.terminal)
                self.assertEqual(after_deadline.state, state)
                self.assertEqual(after_deadline.checkpoint, terminal.checkpoint)
                self.assertEqual(checkpoint.load_checkpoint(checkpoint_path)["terminal_reason"],
                                 "original terminal evidence")

    def test_duplicate_controllers_and_epoch_safe_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            first = RootController("root", "one", state_path=state, stale_after=10, clock=clock)
            lease = first.acquire()
            duplicate = RootController("root", "two", state_path=state, stale_after=10, clock=clock)
            with self.assertRaises(DuplicateController):
                duplicate.acquire()
            clock.value = 111
            with self.assertRaises(StaleLease):
                duplicate.acquire()
            replacement = duplicate.acquire(takeover=True)
            self.assertEqual(replacement.epoch, lease.epoch + 1)
            self.assertEqual(replacement.token, "root/two/2")
            with self.assertRaises(FencedLease):
                first.heartbeat(lease)

    def test_same_logical_name_requires_authenticated_reattach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            first = RootController("root", "controller", state_path=state, clock=clock)
            lease = first.acquire()
            duplicate = RootController("root", "controller", state_path=state, clock=clock)
            with self.assertRaises(DuplicateController):
                duplicate.acquire()
            reattached = duplicate.acquire(resume_proof=lease.resume_secret)
            # AFREL-032: reattach rotates the owner-bound fencing identity
            # (epoch + public token) too, not just the private secret --
            # otherwise a displaced owner's remembered token string keeps
            # authorizing heartbeat/release/fence/launch forever.
            self.assertEqual(reattached.epoch, lease.epoch + 1)
            self.assertNotEqual(reattached.token, lease.token)
            # AFREL-028: every reattach rotates the credential, so the
            # secret that was just spent is not the one now valid.
            self.assertNotEqual(reattached.resume_secret, lease.resume_secret)
            with self.assertRaises(FencedLease):
                first.assert_lease(lease)
            with self.assertRaises(FencedLease):
                # The stale PUBLIC TOKEN STRING -- the only form that
                # crosses a process boundary (herdr_launch --lease,
                # _controller_fence) -- must also be rejected after reattach.
                first.assert_lease(lease.token)

    def test_sideband_authorization_does_not_fence_live_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            live = RootController("root", "controller", state_path=state, clock=clock)
            lease = live.acquire()
            sideband = RootController("root", "controller", state_path=state, clock=clock)
            authorized = sideband.authorize(lease.resume_secret)
            self.assertEqual(authorized.epoch, lease.epoch)
            self.assertEqual(authorized.token, lease.token)
            clock.value = 101
            renewed = live.heartbeat(lease)
            self.assertEqual(renewed.epoch, lease.epoch)

    def test_resume_proof_is_random_and_not_the_guessable_public_token(self) -> None:
        """AFREL-014: the public token (root/controller/epoch) is derivable;
        it must never work as the reattach credential."""
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            first = RootController("root", "controller", state_path=state, clock=clock)
            lease = first.acquire()
            self.assertNotEqual(lease.resume_secret, "")
            self.assertNotEqual(lease.resume_secret, lease.token)
            guesser = RootController("root", "controller", state_path=state, clock=clock)
            with self.assertRaises(DuplicateController):
                guesser.acquire(resume_proof=lease.token)
            with self.assertRaises(DuplicateController):
                guesser.acquire(resume_proof=f"root/controller/{lease.epoch}")

    def test_resume_secret_rotates_on_every_reattach_and_on_takeover(self) -> None:
        """AFREL-028: rotate on reattach too, not just takeover -- a spent
        or leaked credential is only ever usable once."""
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            first = RootController("root", "one", state_path=state, stale_after=10, clock=clock)
            lease = first.acquire()
            reattached = first.acquire(resume_proof=lease.resume_secret)
            self.assertNotEqual(reattached.resume_secret, lease.resume_secret)
            # The just-spent secret cannot reattach a second time.
            with self.assertRaises(DuplicateController):
                RootController(
                    "root", "one", state_path=state, stale_after=10, clock=clock
                ).acquire(resume_proof=lease.resume_secret)
            clock.value = 111
            takeover = RootController("root", "two", state_path=state, stale_after=10, clock=clock)
            replacement = takeover.acquire(takeover=True)
            self.assertNotEqual(replacement.resume_secret, reattached.resume_secret)
            with self.assertRaises(DuplicateController):
                RootController(
                    "root", "two", state_path=state, stale_after=10, clock=clock
                ).acquire(resume_proof=reattached.resume_secret)

    def test_resume_secret_hash_only_persisted_never_plaintext(self) -> None:
        """AFREL-023/AFREL-028: reading the state file alone must never
        yield a usable credential -- only its SHA-256 digest is stored."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            first = RootController("root", "one", state_path=state)
            lease = first.acquire()
            raw = json.loads(state.read_text(encoding="utf-8"))
            self.assertNotIn("resume_secret", raw["lease"])
            self.assertIn("resume_secret_hash", raw["lease"])
            self.assertNotEqual(raw["lease"]["resume_secret_hash"], lease.resume_secret)
            loaded = Lease.from_dict(raw["lease"])
            self.assertEqual(loaded.resume_secret, "")
            self.assertTrue(loaded.verify_resume_proof(lease.resume_secret))
            self.assertFalse(loaded.verify_resume_proof("guessed-wrong-secret"))

    def test_released_lease_requires_proof_and_preserves_incarnation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            first = RootController("root", "controller", state_path=state)
            original = first.acquire()
            first.release(original)

            next_controller = RootController("root", "controller", state_path=state)
            with self.assertRaises(controller_module.LeaseConflict):
                next_controller.acquire()
            resumed = next_controller.acquire(resume_proof=original.resume_secret)

            self.assertEqual(resumed.continuity_id, original.continuity_id)
            self.assertEqual(resumed.epoch, original.epoch + 1)
            self.assertNotEqual(resumed.resume_secret, original.resume_secret)

    def test_legacy_released_lease_recovery_requires_expired_signed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(200.0)
            state = Path(tmp) / "controller.json"
            controller = RootController("root", "controller", state_path=state, clock=clock)
            original = controller.acquire()
            controller.release(original)
            raw = json.loads(state.read_text(encoding="utf-8"))
            raw.pop("dormant_lease", None)
            state.write_text(json.dumps(raw), encoding="utf-8")
            authority = "authority-secret-for-test"
            contract = {
                "schema": "agentflow.return@1", "workspace_root": "root",
                "workflow_root": "workflow", "controller_id": "controller",
                "lease_epoch": original.epoch, "continuity_id": original.continuity_id,
                "task_id": "task-1", "claim_id": "claim-1", "launch_id": "launch-1",
                "deadline_epoch": 100.0,
                "authority_key_id": hashlib.sha256(authority.encode()).hexdigest()[:24],
            }
            contract["authority_hmac"] = controller_module._authority_mac(
                authority, contract, domain="return-contract-v1",
            )

            resumed = controller.recover_released_incarnation(
                workflow_root="workflow", contract=contract,
                authority_secret=authority, resume_proof=original.resume_secret,
            )
            self.assertEqual(resumed.continuity_id, original.continuity_id)
            self.assertEqual(resumed.epoch, original.epoch + 1)

    def test_legacy_released_lease_recovery_rejects_unsigned_and_unexpired_contracts(self) -> None:
        for deadline, sign in ((300.0, True), (100.0, False)):
            with self.subTest(deadline=deadline, sign=sign), tempfile.TemporaryDirectory() as tmp:
                clock = Clock(200.0)
                state = Path(tmp) / "controller.json"
                controller = RootController("root", "controller", state_path=state, clock=clock)
                original = controller.acquire()
                controller.release(original)
                raw = json.loads(state.read_text(encoding="utf-8"))
                raw.pop("dormant_lease", None)
                state.write_text(json.dumps(raw), encoding="utf-8")
                authority = "authority-secret-for-test"
                contract = {
                    "schema": "agentflow.return@1", "workspace_root": "root",
                    "workflow_root": "workflow", "controller_id": "controller",
                    "lease_epoch": original.epoch, "continuity_id": original.continuity_id,
                    "task_id": "task-1", "claim_id": "claim-1", "launch_id": "launch-1",
                    "deadline_epoch": deadline,
                    "authority_key_id": hashlib.sha256(authority.encode()).hexdigest()[:24],
                }
                if sign:
                    contract["authority_hmac"] = controller_module._authority_mac(
                        authority, contract, domain="return-contract-v1",
                    )
                with self.assertRaises(controller_module.LeaseConflict):
                    controller.recover_released_incarnation(
                        workflow_root="workflow", contract=contract,
                        authority_secret=authority, resume_proof=original.resume_secret,
                    )

    def test_dispatch_is_fenced_before_post_launch_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            state = Path(tmp) / "controller.json"
            checkpoint_path = Path(tmp) / "checkpoint.json"
            first = RootController(
                "root", "one", state_path=state, checkpoint_path=checkpoint_path,
                stale_after=10, clock=clock,
            )
            lease = first.acquire()
            replacement = RootController(
                "root", "two", state_path=state, checkpoint_path=checkpoint_path,
                stale_after=10, clock=clock,
            )

            def takeover(_task: dict[str, str]) -> dict[str, str]:
                clock.value = 111
                replacement.acquire(takeover=True)
                return {"session_id": "stale-session", "status": "running"}

            with self.assertRaises(FencedLease):
                first.resume(
                    [{"task": "task-1", "root": "root"}],
                    dispatch=takeover,
                    lease=lease,
                )
            self.assertEqual(checkpoint.load_checkpoint(checkpoint_path)["status"], "claimed_no_session")

    def test_crash_marker_is_idempotent_and_never_relaunches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            state = root / "controller.json"
            cp = root / "resume.json"
            controller = RootController("root", "controller", state_path=state, checkpoint_path=cp)
            lease = controller.acquire()
            launches: list[str] = []

            def crash(task: dict[str, str]) -> str:
                launches.append(task["task"])
                raise RuntimeError("process died")

            with self.assertRaises(RuntimeError):
                controller.resume(
                    [{"task": "task-1", "root": "root", "claim_id": "claim-1"}],
                    dispatch=crash,
                    lease=lease,
                )
            self.assertEqual(checkpoint.load_checkpoint(cp)["status"], "claimed_no_session")
            resumed = controller.resume(
                [{"task": "task-1", "root": "root"}], dispatch=crash, lease=lease
            )
            self.assertEqual(resumed.state, "claimed_no_session")
            self.assertTrue(resumed.resumed)
            self.assertEqual(launches, ["task-1"])

    def test_scheduler_rejects_wrong_root_and_terminal_halts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = RootController(
                "root", "controller", state_path=Path(tmp) / "controller.json"
            )
            lease = controller.acquire()
            dispatch = mock.Mock(return_value={"status": "completed", "session_id": "s1"})
            with self.assertRaises(RootViolation):
                controller.resume([{"task": "other", "root": "other"}], dispatch=dispatch, lease=lease)
            dispatch.assert_not_called()
            done = controller.resume(
                [{"task": "task-1", "root": "root"}], dispatch=dispatch, lease=lease
            )
            self.assertEqual(done.state, "completed")
            again = controller.resume(
                [{"task": "task-2", "root": "root"}], dispatch=dispatch, lease=lease
            )
            self.assertEqual(again.state, "completed")
            self.assertEqual(dispatch.call_count, 1)

    def test_save_checkpoint_rejects_a_superseded_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            clock = Clock()
            first = RootController(
                "root", "one", state_path=state, checkpoint_path=cp, stale_after=10, clock=clock
            )
            lease = first.acquire()
            clock.value = 111
            second = RootController(
                "root", "two", state_path=state, checkpoint_path=cp, stale_after=10, clock=clock
            )
            second.acquire(takeover=True)
            with self.assertRaises(FencedLease):
                first._save_checkpoint(
                    {"task": "root", "phase": "controller", "next_action": "x", "state": "running"},
                    lease=lease,
                )
            self.assertFalse(cp.exists())

    def test_checkpoint_write_holds_the_lease_lock_across_check_and_write(self) -> None:
        """AFREL-015: the lease check and the checkpoint write must be one
        fenced transaction. Reproduce the prior TOCTOU by making the write
        slow and confirming a concurrent takeover cannot interleave with
        it -- the takeover only completes once our write has returned."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            clock = Clock()
            first = RootController(
                "root", "one", state_path=state, checkpoint_path=cp, stale_after=10, clock=clock
            )
            lease = first.acquire()
            clock.value = 111  # the lease already looks stale to a takeover

            entered = threading.Event()
            release = threading.Event()
            original_write = checkpoint.write_checkpoint

            def slow_write(path, document):
                entered.set()
                release.wait(timeout=5)
                return original_write(path, document)

            save_done = threading.Event()

            def do_save() -> None:
                first._save_checkpoint(
                    {"task": "root", "phase": "controller", "next_action": "x", "state": "running"},
                    lease=lease,
                )
                save_done.set()

            takeover_done = threading.Event()

            def attempt_takeover() -> None:
                entered.wait(timeout=5)
                second = RootController(
                    "root", "two", state_path=state, stale_after=10, clock=clock
                )
                second.acquire(takeover=True)
                takeover_done.set()

            saver = threading.Thread(target=do_save)
            taker = threading.Thread(target=attempt_takeover)
            try:
                with mock.patch.object(checkpoint, "write_checkpoint", side_effect=slow_write):
                    saver.start()
                    self.assertTrue(entered.wait(timeout=5))
                    taker.start()
                    # The takeover thread must be blocked on our held lock,
                    # not racing ahead while our write is in flight.
                    self.assertFalse(takeover_done.wait(timeout=0.3))
                    release.set()
                    saver.join(timeout=5)
            finally:
                release.set()
                taker.join(timeout=5)
            self.assertTrue(save_done.is_set())
            self.assertTrue(takeover_done.is_set())
            self.assertEqual(checkpoint.load_checkpoint(cp)["state"], "running")

    def test_advance_clears_pointer_so_resume_selects_a_new_task(self) -> None:
        """AFREL-010: once a caller confirms a task is finished and calls
        advance(), the next resume() must select a NEW ready descendant
        instead of being stuck re-returning the finished task forever."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            controller = RootController("root", "one", state_path=state, checkpoint_path=cp)
            lease = controller.acquire()
            first = controller.resume([{"task": "task-1", "root": "root"}], lease=lease)
            self.assertEqual(first.state, "claimed_no_session")
            self.assertEqual(first.task, "task-1")
            # Without advance(), resume() would keep re-returning task-1.
            stuck = controller.resume([{"task": "task-2", "root": "root"}], lease=lease)
            self.assertEqual(stuck.task, "task-1")

            advanced = controller.advance(lease=lease)
            self.assertFalse(advanced.terminal)
            self.assertNotIn(advanced.state, TERMINAL_STATES)

            second = controller.resume([{"task": "task-2", "root": "root"}], lease=lease)
            self.assertEqual(second.state, "claimed_no_session")
            self.assertEqual(second.task, "task-2")

    def test_v2_live_checkpoint_is_preserved_in_the_v3_active_task_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            controller = RootController("root", "one", state_path=state, checkpoint_path=cp)
            lease = controller.acquire()
            cp.write_text(json.dumps({
                "schema": checkpoint.SCHEMA, "version": 2,
                "task": "legacy-task", "phase": "dispatch", "next_action": "await session",
                "root": "root", "controller": "one", "actor": "one", "claim_id": "claim-legacy",
                "session_id": "session-legacy", "lease_token": lease.token,
                "state": "running", "status": "running", "epoch": lease.epoch,
            }), encoding="utf-8")

            migrated = controller.migrate_active_tasks(lease=lease)

            self.assertEqual(migrated.checkpoint["task"], "root")
            self.assertEqual(migrated.checkpoint["version"], checkpoint.SCHEMA_VERSION)
            self.assertEqual(migrated.checkpoint["active_tasks"], [{
                "task": "legacy-task", "phase": "dispatch", "actor": "one",
                "claim_id": "claim-legacy", "session_id": "session-legacy", "state": "running",
            }])

    def test_v1_live_checkpoint_is_preserved_in_the_v3_active_task_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            controller = RootController("root", "one", state_path=state, checkpoint_path=cp)
            lease = controller.acquire()
            cp.write_text(json.dumps({
                "schema": checkpoint.SCHEMA, "version": 1,
                "task": "legacy-task-v1", "phase": "dispatch", "next_action": "await session",
                "root": "root", "controller": "one", "actor": "one", "claim_id": "claim-v1",
                "session_id": "session-v1", "lease_token": lease.token,
                "state": "running", "status": "running", "epoch": lease.epoch,
            }), encoding="utf-8")

            migrated = controller.migrate_active_tasks(lease=lease)

            self.assertEqual(migrated.checkpoint["task"], "root")
            self.assertEqual(migrated.checkpoint["version"], checkpoint.SCHEMA_VERSION)
            self.assertEqual(migrated.checkpoint["active_tasks"], [{
                "task": "legacy-task-v1", "phase": "dispatch", "actor": "one",
                "claim_id": "claim-v1", "session_id": "session-v1", "state": "running",
            }])

    def test_concurrent_active_task_reservations_do_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            controller = RootController("root", "one", state_path=state, checkpoint_path=cp)
            lease = controller.acquire()
            read_barrier = threading.Barrier(2)
            original = controller_module._active_tasks_from_checkpoint
            original_assert = controller._assert_task_root

            def synchronized_read(document, root):
                rows = original(document, root)
                # Hold the read window open. Without a single lock around the
                # read/check/write, both callers deterministically read the
                # same old task set before either replaces it.
                time.sleep(0.05)
                return rows

            def synchronized_task_check(task):
                task_id = original_assert(task)
                read_barrier.wait(timeout=3)
                return task_id

            failures: list[BaseException] = []

            def reserve(task_id: str) -> None:
                try:
                    controller.reserve_active_task(
                        {"task": task_id, "root": "root", "actor": "one", "claim_id": f"claim-{task_id}"},
                        lease=lease,
                    )
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.object(
                controller_module, "_active_tasks_from_checkpoint", side_effect=synchronized_read,
            ), mock.patch.object(controller, "_assert_task_root", side_effect=synchronized_task_check):
                threads = [threading.Thread(target=reserve, args=(task_id,)) for task_id in ("task-a", "task-b")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads), "reservation threads did not finish")
            self.assertEqual(failures, [])
            self.assertEqual({row["task"] for row in controller.active_tasks()}, {"task-a", "task-b"})

    def test_prelaunch_reservation_is_not_duplicated_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            cp = Path(tmp) / "checkpoint.json"
            first = RootController("root", "one", state_path=state, checkpoint_path=cp)
            lease = first.acquire()
            first.reserve_active_task(
                {"task": "task-1", "root": "root", "actor": "one", "claim_id": "claim-1"},
                lease=lease,
            )

            resumed = RootController("root", "one", state_path=state, checkpoint_path=cp)
            next_lease = resumed.acquire(resume_proof=lease.resume_secret)
            rows = resumed.active_tasks()
            self.assertEqual(rows[0]["state"], "claimed_no_session")
            with self.assertRaises(ControllerError):
                resumed.reserve_active_task(
                    {"task": "task-1", "root": "root", "actor": "one", "claim_id": "claim-1"},
                    lease=next_lease,
                )
            self.assertEqual([item["task"] for item in resumed.active_tasks()], ["task-1"])

    def test_active_task_checkpoint_rejects_secret_fields_and_duplicate_ids(self) -> None:
        base = {"task": "root", "phase": "controller", "next_action": "await workers"}
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint({
                **base,
                "active_tasks": [{
                    "task": "task-1", "state": "running", "session_id": "s1",
                    "claim_token": "must-not-be-persisted",
                }],
            })
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint({
                **base,
                "active_tasks": [
                    {"task": "task-1", "state": "running"},
                    {"task": "task-1", "state": "launched"},
                ],
            })

    def test_fence_blocks_concurrent_takeover_for_its_entire_duration(self) -> None:
        """AFREL-027/AFREL-009: fence() holds the controller lock across an
        external critical section (e.g. a Herdr spawn or binding commit) so
        a concurrent takeover cannot land mid-section -- proven with a real
        thread, not a mock of the entry point."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "controller.json"
            clock = Clock()
            first = RootController("root", "one", state_path=state, stale_after=10, clock=clock)
            lease = first.acquire()
            clock.value = 111  # already stale to a takeover attempt

            entered = threading.Event()
            release = threading.Event()
            takeover_done = threading.Event()

            def hold_fence() -> None:
                with first.fence(lease):
                    entered.set()
                    release.wait(timeout=5)

            def attempt_takeover() -> None:
                entered.wait(timeout=5)
                RootController(
                    "root", "two", state_path=state, stale_after=10, clock=clock
                ).acquire(takeover=True)
                takeover_done.set()

            holder = threading.Thread(target=hold_fence)
            taker = threading.Thread(target=attempt_takeover)
            try:
                holder.start()
                self.assertTrue(entered.wait(timeout=5))
                taker.start()
                self.assertFalse(takeover_done.wait(timeout=0.3))
                release.set()
                holder.join(timeout=5)
            finally:
                release.set()
                taker.join(timeout=5)
            self.assertTrue(takeover_done.is_set())

    def test_v1_checkpoint_migrates_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            path.write_text(
                json.dumps({
                    "schema": checkpoint.SCHEMA,
                    "version": 1,
                    "task": "task-1",
                    "phase": "code",
                    "next_action": "test",
                }),
                encoding="utf-8",
            )
            migrated = checkpoint.load_checkpoint(path)
            self.assertEqual(migrated["version"], checkpoint.SCHEMA_VERSION)
            self.assertEqual(migrated["status"], "")
            checkpoint.update_checkpoint(path, {"status": "claimed", "session_id": ""})
            self.assertEqual(checkpoint.load_checkpoint(path)["status"], "claimed_no_session")


class ExactClaimTests(unittest.TestCase):
    def _issue(self, **overrides: object) -> dict[str, object]:
        issue: dict[str, object] = {
            "id": "task-1",
            "parent": "root",
            "status": "open",
            "assignee": "",
            "labels": ["agentflow", "af:stage:code"],
        }
        issue.update(overrides)
        return issue

    def test_wrong_root_and_labels_do_not_fall_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.object(beads, "get_issue", return_value=self._issue(parent="other")):
                with self.assertRaises(beads.ClaimConflict) as raised:
                    beads.claim_issue_exact(
                        cwd, task="task-1", root="root", actor="writer", labels=["agentflow"]
                    )
            self.assertEqual(raised.exception.reason, "wrong_root")
            with mock.patch.object(beads, "get_issue", return_value=self._issue()), mock.patch.object(
                beads, "run"
            ) as run:
                with self.assertRaises(beads.ClaimConflict) as raised:
                    beads.claim_issue_exact(
                        cwd,
                        task="task-1",
                        root="root",
                        actor="writer",
                        labels=["missing"],
                    )
            self.assertEqual(raised.exception.reason, "labels_mismatch")
            run.assert_not_called()

    def test_exact_claim_rechecks_actor_and_never_uses_ready_queue(self) -> None:
        before = self._issue()
        after = self._issue(status="in_progress", assignee="writer")
        result = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            beads, "get_issue", side_effect=[before, after]
        ), mock.patch.object(beads, "run", result):
            claim = beads.claim_issue_exact(
                Path(tmp), task="task-1", root="root", actor="writer", claim_id="c1"
            )
        self.assertEqual(claim.identity.to_dict()["claim_id"], "c1")
        result.assert_called_once_with(
            Path(tmp), "update", "task-1", "--claim", "--actor", "writer"
        )

    def test_root_descendants_are_selected_from_the_durable_graph(self) -> None:
        snapshot = [
            {"id": "root", "status": "open"},
            {"id": "child", "parent": "root", "status": "open"},
            {"id": "grandchild", "parent": "child", "status": "open"},
            {"id": "other", "parent": "unrelated", "status": "open"},
        ]
        completed = mock.Mock(returncode=0, stdout=json.dumps(snapshot), stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(beads, "run", return_value=completed):
            rows = beads.root_descendants(Path(tmp), "root")
        self.assertEqual([row["id"] for row in rows], ["child", "grandchild"])


if __name__ == "__main__":
    unittest.main()
