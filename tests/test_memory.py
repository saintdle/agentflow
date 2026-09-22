import datetime as dt
import tempfile
import unittest
from pathlib import Path

from agentflow.memory import MemoryStore, find_first
from agentflow.search import KnowledgeDocument, KnowledgeIndex, SearchError


class MemoryRecallTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)

    def test_find_first_requires_approval_freshness_and_scope(self) -> None:
        with KnowledgeIndex() as index:
            index.add(KnowledgeDocument("candidate", "Runbook", "recover a stale worker", "runbook.md", authority=90, scope="project", last_verified_at="2026-09-21T00:00:00+00:00"))
            index.add(KnowledgeDocument("approved", "Runbook", "recover a stale worker", "runbook-approved.md", authority=90, scope="project", last_verified_at="2026-09-21T00:00:00+00:00"))
            index.approve("approved", approval_by="controller:root", approval_ref="review-1", approved_at="2026-09-21T00:00:00+00:00")
            plan = find_first(index, "stale worker", scope="project", max_chars=160, max_items=1, now=self.NOW, max_age_days=7)
            self.assertEqual([item.document_id for item in plan.items], ["approved"])
            self.assertLessEqual(plan.character_count, 160)
            self.assertEqual(index.get("approved").injection_count, 1)  # type: ignore[union-attr]

    def test_expiry_and_strict_budget(self) -> None:
        with MemoryStore() as store:
            store.candidate(KnowledgeDocument("expired", "Runbook", "recover a stale worker", "expired.md", authority=90, expires_at="2026-09-21T00:00:00+00:00", last_verified_at="2026-09-21T00:00:00+00:00"))
            store.approve("expired", approval_by="human:alice", approval_ref="review-2", approved_at="2026-09-20T00:00:00+00:00")
            plan = store.find_first("stale worker", max_chars=20, max_items=4, now=self.NOW)
            self.assertEqual(plan.items, ())
            self.assertEqual(plan.text, "")

    def test_candidate_stage_fetch_and_persistent_session_digest_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "memory.sqlite3"
            with MemoryStore(path=str(path)) as store:
                store.candidate(KnowledgeDocument("approved", "Runbook", "recover a stale worker", "runbook.md", authority=90, last_verified_at="2026-09-21T00:00:00+00:00"))
                store.approve("approved", approval_by="controller:root", approval_ref="review-3", approved_at="2026-09-21T00:00:00+00:00")
                candidates = store.index.find_candidates("stale worker")
                self.assertEqual(candidates[0].document_id, "approved")
                self.assertFalse(hasattr(candidates[0], "summary"))
                self.assertIsNotNone(store.fetch("approved"))
                first = store.find_first("stale worker", session_id="session-a", now=self.NOW)
                self.assertEqual(first.item_count, 1)
                self.assertEqual(store.find_first("stale worker", session_id="session-a", now=self.NOW).item_count, 0)
                self.assertEqual(store.find_first("stale worker", session_id="session-b", now=self.NOW).item_count, 1)
            with MemoryStore(path=str(path)) as reopened:
                self.assertEqual(reopened.find_first("stale worker", session_id="session-a", now=self.NOW).item_count, 0)

    def test_find_stage_and_approval_fail_closed(self) -> None:
        with MemoryStore() as store:
            with self.assertRaises(SearchError):
                store.approve("missing", approval_by="worker:luna", approval_ref="r", approved_at="2026-09-22T00:00:00+00:00")
            store.candidate(KnowledgeDocument("candidate", "Runbook", "recover a stale worker", "candidate.md"))
            with self.assertRaises(SearchError):
                store.approve("candidate", approval_by="worker:luna", approval_ref="r", approved_at="2026-09-22T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
