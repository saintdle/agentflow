from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.herdr import (
    HerdrBinding, HerdrError, HerdrResult, HerdrSession, HERDR_OUTCOMES,
)


def _binding(session: HerdrSession, task_id: str = "task-1") -> HerdrBinding:
    return session.launch(
        root="/repo",
        task_id=task_id,
        provider="copilot",
        session_id="sess-001",
    )


def _result(binding: HerdrBinding, *, outcome: str = "completed") -> HerdrResult:
    return HerdrResult(
        task_id=binding.task_id,
        launch_id=binding.launch_id,
        provider=binding.provider,
        session_id=binding.session_id,
        outcome=outcome,
    )


class BindingIdentityTests(unittest.TestCase):
    """HerdrBinding persists all identity fields; frozen after creation."""

    def test_binding_fields_populated(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        for field in ("root", "task_id", "claim_id", "lease_id", "launch_id",
                      "pane_id", "provider", "session_id", "created_at"):
            self.assertTrue(getattr(b, field), f"{field} must not be empty")

    def test_binding_is_frozen(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        with self.assertRaises((AttributeError, TypeError)):
            b.task_id = "other"  # type: ignore[misc]

    def test_empty_field_raises(self) -> None:
        with self.assertRaises(HerdrError):
            HerdrBinding(
                root="", task_id="t", claim_id="c", lease_id="l",
                launch_id="x", pane_id="p", provider="copilot",
                session_id="s", created_at="ts",
            )

    def test_to_dict_round_trip(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        d = b.to_dict()
        self.assertEqual(d["task_id"], b.task_id)
        self.assertEqual(d["provider"], b.provider)


class CollisionTests(unittest.TestCase):
    """Launching the same task_id twice raises HerdrError."""

    def test_collision_raises(self) -> None:
        session = HerdrSession()
        _binding(session, "task-col")
        with self.assertRaises(HerdrError):
            _binding(session, "task-col")

    def test_different_task_ids_do_not_collide(self) -> None:
        session = HerdrSession()
        b1 = _binding(session, "task-a")
        b2 = _binding(session, "task-b")
        self.assertNotEqual(b1.task_id, b2.task_id)


class RestartTests(unittest.TestCase):
    """Restart preserves root/task/claim/lease; issues fresh launch_id/pane_id."""

    def test_restart_preserves_identity(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        b2 = session.restart(b)
        self.assertEqual(b2.root, b.root)
        self.assertEqual(b2.task_id, b.task_id)
        self.assertEqual(b2.claim_id, b.claim_id)
        self.assertEqual(b2.lease_id, b.lease_id)
        self.assertEqual(b2.created_at, b.created_at)

    def test_restart_issues_fresh_launch_and_pane(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        b2 = session.restart(b)
        self.assertNotEqual(b2.launch_id, b.launch_id)
        self.assertNotEqual(b2.pane_id, b.pane_id)

    def test_restart_wrong_identity_raises(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        impostor = HerdrBinding(
            root=b.root, task_id=b.task_id,
            claim_id="wrong-claim", lease_id=b.lease_id,
            launch_id=b.launch_id, pane_id=b.pane_id,
            provider=b.provider, session_id=b.session_id,
            created_at=b.created_at,
        )
        with self.assertRaises(HerdrError):
            session.restart(impostor)

    def test_restart_unknown_task_raises(self) -> None:
        session = HerdrSession()
        b = _binding(session, "known")
        ghost = HerdrBinding(
            root=b.root, task_id="unknown-task",
            claim_id=b.claim_id, lease_id=b.lease_id,
            launch_id=b.launch_id, pane_id=b.pane_id,
            provider=b.provider, session_id=b.session_id,
            created_at=b.created_at,
        )
        with self.assertRaises(HerdrError):
            session.restart(ghost)

    def test_stale_binding_after_restart_is_rejected(self) -> None:
        """Result for the old launch_id is rejected after restart."""
        session = HerdrSession()
        b_old = _binding(session)
        _b_new = session.restart(b_old)
        old_result = _result(b_old)
        accepted = session.ingest_result(b_old, old_result)
        self.assertFalse(accepted)
        self.assertFalse(session.is_complete(b_old.task_id))


class PaneAttentionTests(unittest.TestCase):
    """Pane state is exposed only via AttentionRecord; raw status is never returned."""

    _NOW = dt.datetime(2026, 7, 25, 20, 0, 0, tzinfo=dt.timezone.utc)

    def test_running_pane_is_live(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        record = session.pane_attention(b.task_id, now=self._NOW)
        self.assertEqual(record.state, "live")

    def test_vanished_pane_is_stale(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        session.mark_pane_vanished(b.task_id)
        record = session.pane_attention(b.task_id, now=self._NOW)
        self.assertEqual(record.state, "stale")

    def test_exited_without_result_is_stale(self) -> None:
        """A session that exits without ingesting a result is stale, not complete."""
        session = HerdrSession()
        b = _binding(session)
        # Manually set pane to exited (no result ingested)
        session._pane_status[b.task_id] = "exited"
        record = session.pane_attention(b.task_id, now=self._NOW)
        self.assertEqual(record.state, "stale")
        self.assertFalse(session.is_complete(b.task_id))

    def test_exited_with_result_is_live(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = _result(b)
        session.ingest_result(b, r)
        record = session.pane_attention(b.task_id, now=self._NOW)
        self.assertEqual(record.state, "live")
        self.assertTrue(session.is_complete(b.task_id))

    def test_pane_attention_unknown_task_raises(self) -> None:
        session = HerdrSession()
        with self.assertRaises(HerdrError):
            session.pane_attention("no-such-task")

    def test_mark_vanished_unknown_task_raises(self) -> None:
        session = HerdrSession()
        with self.assertRaises(HerdrError):
            session.mark_pane_vanished("ghost")

    def test_pane_attention_returns_attention_record(self) -> None:
        from agentflow.attention import AttentionRecord
        session = HerdrSession()
        b = _binding(session)
        record = session.pane_attention(b.task_id, now=self._NOW)
        self.assertIsInstance(record, AttentionRecord)
        self.assertEqual(record.bead_id, b.task_id)


class ResultIngestionTests(unittest.TestCase):
    """Only an identity-matching HerdrResult advances completion."""

    def test_matching_result_accepted(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = _result(b)
        self.assertTrue(session.ingest_result(b, r))
        self.assertTrue(session.is_complete(b.task_id))
        self.assertEqual(session.get_result(b.task_id), r)

    def test_wrong_task_id_rejected(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = HerdrResult(
            task_id="other-task", launch_id=b.launch_id,
            provider=b.provider, session_id=b.session_id, outcome="completed",
        )
        self.assertFalse(session.ingest_result(b, r))
        self.assertFalse(session.is_complete(b.task_id))

    def test_wrong_launch_id_rejected(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = HerdrResult(
            task_id=b.task_id, launch_id="other-launch",
            provider=b.provider, session_id=b.session_id, outcome="completed",
        )
        self.assertFalse(session.ingest_result(b, r))
        self.assertFalse(session.is_complete(b.task_id))

    def test_wrong_provider_rejected(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = HerdrResult(
            task_id=b.task_id, launch_id=b.launch_id,
            provider="wrong-provider", session_id=b.session_id, outcome="completed",
        )
        self.assertFalse(session.ingest_result(b, r))

    def test_wrong_session_id_rejected(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = HerdrResult(
            task_id=b.task_id, launch_id=b.launch_id,
            provider=b.provider, session_id="wrong-session", outcome="completed",
        )
        self.assertFalse(session.ingest_result(b, r))

    def test_done_without_result_is_not_complete(self) -> None:
        """Session can exit without a result; is_complete must remain False."""
        session = HerdrSession()
        b = _binding(session)
        # No ingest_result call — session "completes" without evidence
        self.assertFalse(session.is_complete(b.task_id))

    def test_protocol_mismatch_raises(self) -> None:
        """Passing a non-HerdrResult raises HerdrError (protocol mismatch)."""
        session = HerdrSession()
        b = _binding(session)
        with self.assertRaises(HerdrError):
            session.ingest_result(b, {"task_id": b.task_id})  # type: ignore[arg-type]
        with self.assertRaises(HerdrError):
            session.ingest_result(b, "completed")  # type: ignore[arg-type]

    def test_invalid_outcome_raises(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        with self.assertRaises(HerdrError):
            HerdrResult(
                task_id=b.task_id, launch_id=b.launch_id,
                provider=b.provider, session_id=b.session_id,
                outcome="invalid-outcome",
            )


class HerdrResultTests(unittest.TestCase):
    def test_to_dict(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = _result(b)
        d = r.to_dict()
        self.assertEqual(d["task_id"], b.task_id)
        self.assertEqual(d["outcome"], "completed")

    def test_matches_own_binding(self) -> None:
        session = HerdrSession()
        b = _binding(session)
        r = _result(b)
        self.assertTrue(r.matches(b))

    def test_does_not_match_other_binding(self) -> None:
        session = HerdrSession()
        b1 = _binding(session, "t1")
        b2 = _binding(session, "t2")
        r = _result(b1)
        self.assertFalse(r.matches(b2))


if __name__ == "__main__":
    unittest.main()
