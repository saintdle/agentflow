from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import checkpoint


class AdmissionPhaseTests(unittest.TestCase):
    def _document(self, **updates: object) -> dict[str, object]:
        document: dict[str, object] = {
            "task": "root",
            "phase": "controller",
            "next_action": "schedule",
        }
        document.update(updates)
        return document

    def test_open_checkpoint_admits_candidates(self) -> None:
        self.assertEqual(
            checkpoint.admission_phase(self._document(state="running", status="running")),
            "open",
        )

    def test_draining_incomplete_checkpoint_preserves_no_claim_barrier(self) -> None:
        self.assertEqual(
            checkpoint.admission_phase(
                self._document(state="draining", status="incomplete", terminal=False)
            ),
            "draining",
        )

    def test_terminal_status_or_flag_is_terminal(self) -> None:
        for document in (
            self._document(state="blocked", status="blocked"),
            self._document(state="running", status="running", terminal=True),
        ):
            with self.subTest(document=document):
                self.assertEqual(checkpoint.admission_phase(document), "terminal")

    def test_classifier_revalidates_checkpoint_fields(self) -> None:
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.admission_phase(
                self._document(state="draining", status="incomplete", terminal="false")
            )


if __name__ == "__main__":
    unittest.main()
