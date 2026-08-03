from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import adapters, attention, audit, search, usage
from agentflow.privacy import PrivacyError


class AttentionTests(unittest.TestCase):
    NOW = dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc)

    def test_states_require_explicit_bindings_and_notification_gate(self) -> None:
        beads = [
            {"id": "claimed", "assignee": "writer"},
            {"id": "live"},
            {"id": "stale"},
            {"id": "blocked", "status": "blocked"},
            {"id": "ci", "status": "failed_ci"},
            {"id": "approval", "status": "approval_required"},
            {"id": "ambiguous"},
        ]
        bindings = [
            {"bead_id": "live", "session_id": "s-live", "heartbeat_at": "2026-07-24T00:00:00+00:00", "provider": "codex"},
            {"bead_id": "stale", "session_id": "s-stale", "heartbeat_at": "2026-07-23T00:00:00+00:00"},
            {"bead_id": "ambiguous", "session_id": "one", "heartbeat_at": "2026-07-24T00:00:00+00:00"},
            {"bead_id": "ambiguous", "session_id": "two", "heartbeat_at": "2026-07-24T00:00:00+00:00"},
        ]
        registry = attention.AttentionRegistry(stale_after_seconds=300, notifications_enabled=True, herdr_pilot=False)
        records = registry.evaluate(beads, bindings, now=self.NOW)
        self.assertEqual({record.bead_id: record.state for record in records}, {
            "claimed": "claimed_no_session", "live": "live", "stale": "stale", "blocked": "blocked",
            "ci": "failed_ci", "approval": "approval_required", "ambiguous": "ambiguous",
        })
        self.assertEqual(registry.notifications(records), [])
        registry.herdr_pilot = True
        # Pilot is a deliberate opt-in in this in-memory controller.
        self.assertTrue(registry.notification_gate_open)


class SearchAuditUsageTests(unittest.TestCase):
    def test_search_is_deterministic_and_returns_provenance_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with search.KnowledgeIndex(Path(temp) / "knowledge.sqlite3") as index:
                index.add(search.KnowledgeDocument("one", "Recovery guide", "recover a stale worker", "docs/recovery.md", authority=90, provenance={"commit": "abc"}))
                index.add(search.KnowledgeDocument("two", "Low trust", "recover a stale worker", "scratch.txt", authority=10, provenance={"commit": "def"}))
                result = index.search("stale worker", min_authority=50)
                self.assertEqual([item.document_id for item in result], ["one"])
                self.assertEqual(result[0].privacy, "metadata-only")
                self.assertEqual(result[0].provenance["commit"], "abc")
                with self.assertRaises(PrivacyError):
                    index.add(search.KnowledgeDocument("secret", "bad", "api_key: nope", "x"))

    def test_audit_round_trip_marks_missing_fields_unattributed(self) -> None:
        timestamp = "2026-07-24T09:00:00.123456Z"
        event = audit.AuditEvent.create("route", actor="writer", evidence=[{"check": "tests"}], timestamp=timestamp)
        self.assertEqual(event.attribution, audit.UNATTRIBUTED)
        self.assertEqual(audit.AuditEvent.from_dict(event.to_dict()).to_dict(), event.to_dict())
        with self.assertRaises(PrivacyError):
            audit.AuditEvent.create("route", evidence=[{"prompt": "secret"}])
        with self.assertRaises(PrivacyError):
            audit.AuditEvent.create("", timestamp=timestamp)
        with self.assertRaises(PrivacyError):
            audit.AuditEvent.create("route", timestamp="not-a-timestamp")

    def test_usage_compares_only_explicit_task_classes(self) -> None:
        result = usage.report([
            {"provider": "codex", "task_class": "focused-review", "outcome": "completed", "findings": 4, "accepted_findings": 2, "source": "/usage"},
            {"provider": "claude", "task_class": "focused-review", "outcome": "failed", "findings": 2, "accepted_findings": 0, "source": "local"},
            {"provider": "copilot", "task": "looks-like-a-class", "outcome": "completed"},
        ])
        bucket = result["task_classes"][0]
        self.assertEqual(bucket["attempts"], 2)
        self.assertEqual(bucket["success_yield"], 0.5)
        self.assertEqual(bucket["measurement"], "local-estimate")


class AdapterGateTests(unittest.TestCase):
    def results(self) -> list[dict[str, object]]:
        return [
            {"case_id": "a", "baseline_input_tokens": 100, "adapter_input_tokens": 60, "baseline_correct": True, "adapter_correct": True, "baseline_evidence_recall": 1.0, "adapter_evidence_recall": 1.0},
            {"case_id": "b", "baseline_input_tokens": 100, "adapter_input_tokens": 50, "baseline_correct": True, "adapter_correct": True, "baseline_evidence_recall": 0.8, "adapter_evidence_recall": 0.9},
        ]

    def manifest(self, **kwargs: object) -> adapters.AdapterManifest:
        return adapters.AdapterManifest(
            "compact", "1", approved=True, timeout_seconds=1, fallback="baseline",
            results_hash=adapters.compute_results_hash(self.results()), **kwargs,
        )

    def test_promotion_and_fail_closed_route(self) -> None:
        manifest = self.manifest()
        gate = adapters.evaluate(manifest, self.results())
        self.assertTrue(gate.passed)
        router = adapters.AdapterRouter()
        router.register(manifest, gate, lambda request: {"ok": request["value"]})
        self.assertEqual(router.route("compact", {"value": 3}), {"ok": 3})
        with self.assertRaises(adapters.AdapterError):
            router.route("unknown", {})

    def test_unsafe_and_unproven_adapters_fail_closed(self) -> None:
        manifest = adapters.AdapterManifest("unsafe", "1", approved=False, results_hash=adapters.compute_results_hash(self.results()))
        gate = adapters.evaluate(manifest, self.results())
        self.assertFalse(gate.passed)
        with self.assertRaises(adapters.AdapterError):
            adapters.AdapterRouter().register(manifest, gate, lambda request: request)
        with self.assertRaises(PrivacyError):
            adapters.AdapterManifest("bad", "1", capabilities=("system prompt",))

    def test_timeout_uses_bounded_fallback(self) -> None:
        manifest = adapters.AdapterManifest("slow", "1", approved=True, timeout_seconds=0.01, fallback="baseline", results_hash=adapters.compute_results_hash(self.results()))
        gate = adapters.evaluate(manifest, self.results())
        router = adapters.AdapterRouter()
        router.register(manifest, gate, lambda request: (time.sleep(0.2), request)[1])
        self.assertEqual(router.route("slow", {"x": 1}, fallback=lambda request: {"fallback": True}), {"fallback": True})

    def test_missing_or_changed_results_hash_fails_closed(self) -> None:
        missing = adapters.AdapterManifest("missing", "1", approved=True, timeout_seconds=1, fallback="baseline")
        report = adapters.evaluate(missing, self.results())
        self.assertFalse(report.passed)
        self.assertFalse(report.results_immutable)
        changed = self.manifest()
        tampered = self.results()
        tampered[0]["adapter_input_tokens"] = 59
        report = adapters.evaluate(changed, tampered)
        self.assertFalse(report.passed)
        self.assertFalse(report.results_immutable)

    def test_correctness_requires_every_adapter_case_to_be_correct(self) -> None:
        strict_results = self.results()
        strict_results[0]["baseline_correct"] = False
        strict_results[0]["adapter_correct"] = False
        manifest = adapters.AdapterManifest(
            "strict", "1", approved=True, timeout_seconds=1, fallback="baseline",
            results_hash=adapters.compute_results_hash(strict_results),
        )

        report = adapters.evaluate(manifest, strict_results)

        self.assertFalse(report.correctness)
        self.assertFalse(report.passed)

    def test_gate_report_binds_manifest_and_results_identity(self) -> None:
        manifest = self.manifest()
        report = adapters.evaluate(manifest, self.results())
        replacement = adapters.AdapterManifest(
            "compact", "2", approved=True, timeout_seconds=1, fallback="baseline",
            results_hash=manifest.results_hash,
        )
        with self.assertRaises(adapters.AdapterError):
            adapters.AdapterRouter().register(replacement, report, lambda request: request)

    def test_immutable_write_rejects_content_shaped_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            for value in (
                {"prompt": "harmless-looking"},
                {"note": "user: reveal the transcript"},
                {"note": "api_key: abc"},
            ):
                with self.assertRaises(PrivacyError):
                    adapters.immutable_write(path, value)
            self.assertFalse(path.exists())
