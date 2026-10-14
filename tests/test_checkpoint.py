from __future__ import annotations

import sys
import json
import hashlib
import tempfile
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

    def test_legacy_open_migrates_with_empty_causes_and_legacy_halt_is_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            opened = self._document(version=3, state="running", status="running")
            path.write_text(json.dumps(opened), encoding="utf-8")
            migrated_open = checkpoint.load_checkpoint(path)
            self.assertEqual(migrated_open["drain_causes"], [])

            legacy = self._document(
                version=3, state="blocked", status="blocked", terminal=True,
                terminal_reason="diagnostic drain", last_check="opaque marker",
            )
            path.write_text(json.dumps(legacy), encoding="utf-8")
            migrated_halt = checkpoint.load_checkpoint(path)
            cause, = migrated_halt["drain_causes"]
            expected = hashlib.sha256(json.dumps({
                key: legacy.get(key)
                for key in ("phase", "state", "status", "terminal", "terminal_reason", "last_check")
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            self.assertEqual(cause["kind"], "unknown")
            self.assertEqual(cause["provenance"], "legacy_checkpoint")
            self.assertEqual(cause["diagnostic_sha256"], expected)
            self.assertEqual(migrated_halt["version"], checkpoint.SCHEMA_VERSION)

    def test_drain_cause_schema_is_closed_and_bounded(self) -> None:
        valid = {
            "cause_id": "cause-1", "kind": "unknown",
            "provenance": "controller_unclassified", "task_id": "",
            "launch_id": "", "diagnostic_sha256": "",
        }
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(self._document(drain_causes=[{**valid, "extra": "x"}]))
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(self._document(drain_causes=[valid] * (checkpoint.MAX_DRAIN_CAUSES + 1)))

    def test_load_checkpoint_rejects_non_object_json_with_checkpoint_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            for payload in ("[]", "null", "\"checkpoint\""):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(checkpoint.CheckpointError, "payload must be an object"):
                        checkpoint.load_checkpoint(path)


if __name__ == "__main__":
    unittest.main()
