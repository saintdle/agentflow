from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import beads, checkpoint
from agentflow.controller import (
    TERMINAL_STATES,
    DuplicateController,
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
