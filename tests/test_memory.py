import datetime as dt
import unittest

from agentflow.memory import MemoryStore, find_first
from agentflow.search import KnowledgeDocument, KnowledgeIndex


class MemoryRecallTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)

    def test_find_first_requires_approval_freshness_and_scope(self) -> None:
        with KnowledgeIndex() as index:
            index.add(KnowledgeDocument("candidate", "Runbook", "recover a stale worker", "runbook.md", authority=90, scope="project", last_verified_at="2026-09-21T00:00:00+00:00"))
            index.add(KnowledgeDocument("approved", "Runbook", "recover a stale worker", "runbook-approved.md", authority=90, scope="project", last_verified_at="2026-09-21T00:00:00+00:00"))
            index.approve("approved")
            plan = find_first(index, "stale worker", scope="project", max_chars=160, max_items=1, now=self.NOW, max_age_days=7)
            self.assertEqual([item.document_id for item in plan.items], ["approved"])
            self.assertLessEqual(plan.character_count, 160)
            self.assertEqual(index.get("approved").injection_count, 1)  # type: ignore[union-attr]

    def test_expiry_and_strict_budget(self) -> None:
        with MemoryStore() as store:
            store.candidate(KnowledgeDocument("expired", "Runbook", "recover a stale worker", "expired.md", authority=90, expires_at="2026-09-21T00:00:00+00:00", last_verified_at="2026-09-21T00:00:00+00:00"))
            store.approve("expired")
            plan = store.find_first("stale worker", max_chars=20, max_items=4, now=self.NOW)
            self.assertEqual(plan.items, ())
            self.assertEqual(plan.text, "")


if __name__ == "__main__":
    unittest.main()
