from __future__ import annotations

import unittest
from types import SimpleNamespace

from agentflow.copilot_sdk import (
    CopilotSdkEvidenceError,
    CopilotSdkModelEvidenceGate,
    sdk_availability,
)


def event(event_type: str, event_id: str, parent_id: str | None, data: dict, *, agent_id: str | None = None):
    return SimpleNamespace(
        type=event_type,
        id=event_id,
        parent_id=parent_id,
        agent_id=agent_id,
        data=SimpleNamespace(**data),
    )


class CopilotSdkAvailabilityTests(unittest.TestCase):
    def test_core_python_310_does_not_require_optional_sdk(self):
        result = sdk_availability(python_version=(3, 10), distribution_version="1.0.11")
        self.assertFalse(result.available)
        self.assertIn("Python 3.11+", result.reason)

    def test_supported_optional_sdk_release_is_detected_without_starting_it(self):
        result = sdk_availability(python_version=(3, 11), distribution_version="1.0.11")
        self.assertTrue(result.available)
        self.assertEqual(result.sdk_version, "1.0.11")
        self.assertIn("does not enable", result.reason)

    def test_unknown_or_new_major_sdk_fails_closed(self):
        for version in ("not-a-version", "2.0.0"):
            with self.subTest(version=version):
                self.assertFalse(sdk_availability(
                    python_version=(3, 11), distribution_version=version
                ).available)


class CopilotSdkModelEvidenceGateTests(unittest.TestCase):
    def gate(self):
        return CopilotSdkModelEvidenceGate(
            launch_id="launch-1", session_id="session-1", expected_model="claude-sonnet-4.6"
        )

    def test_verified_root_response_is_buffered_until_model_usage_arrives(self):
        gate = self.gate()
        gate.on_event(event("assistant.message_delta", "delta-1", None, {"delta_content": "approved output"}))
        gate.on_event(event("assistant.message", "message-1", "delta-1", {"content": "approved output", "tool_requests": []}))
        with self.assertRaises(CopilotSdkEvidenceError):
            gate.validated_response()
        gate.on_event(event("assistant.usage", "usage-1", "message-1", {"model": "claude-sonnet-4.6"}))
        gate.on_event(event("session.idle", "idle-1", "usage-1", {}))
        self.assertEqual(gate.validated_response(), "approved output")
        self.assertEqual(len(gate.calls), 1)

    def test_every_subagent_api_call_is_checked_but_subagent_tools_fail_closed(self):
        gate = self.gate()
        gate.on_event(event("assistant.message", "message-root", None, {"content": "", "tool_requests": []}))
        gate.on_event(event("assistant.usage", "usage-root", "message-root", {"model": "claude-sonnet-4.6"}))
        gate.on_event(event("assistant.message", "message-child", "usage-root", {"content": "child result", "tool_requests": []}, agent_id="child-1"))
        gate.on_event(event("assistant.usage", "usage-child", "message-child", {"model": "claude-sonnet-4.6"}, agent_id="child-1"))
        gate.on_event(event("session.idle", "idle-child", "usage-child", {}))
        self.assertEqual([call.agent_id for call in gate.calls], [None, "child-1"])
        self.assertIn("sub-agent calls", gate.blocked_reason)
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "sub-agent calls"):
            gate.validated_response()

    def test_mismatched_model_blocks_and_suppresses_response(self):
        gate = self.gate()
        gate.on_event(event("assistant.message", "message-1", None, {"content": "must not escape", "tool_requests": []}))
        gate.on_event(event("assistant.usage", "usage-1", "message-1", {"model": "gpt-6-luna"}))
        gate.on_event(event("assistant.message", "message-2", "usage-1", {"content": "later result", "tool_requests": []}))
        gate.on_event(event("assistant.usage", "usage-2", "message-2", {"model": "claude-sonnet-4.6"}))
        gate.on_event(event("session.idle", "idle-1", "usage-2", {}))
        self.assertIn("mismatch", gate.blocked_reason)
        self.assertEqual([call.model for call in gate.calls], ["gpt-6-luna", "claude-sonnet-4.6"])
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "mismatch"):
            gate.validated_response()

    def test_message_delta_identity_must_match_the_usage_bound_message(self):
        mismatched = self.gate()
        mismatched.on_event(event(
            "assistant.message_delta", "delta-1", None,
            {"messageId": "unrelated-message", "delta_content": "UNVERIFIED"},
        ))
        mismatched.on_event(event(
            "assistant.message", "message-1", "delta-1",
            {"content": "", "tool_requests": []},
        ))
        mismatched.on_event(event(
            "assistant.usage", "usage-1", "message-1",
            {"model": "claude-sonnet-4.6"},
        ))
        mismatched.on_event(event("session.idle", "idle-1", "usage-1", {}))
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "message identity"):
            mismatched.validated_response()

        matching = self.gate()
        matching.on_event(event(
            "assistant.message_delta", "delta-1", None,
            {"messageId": "message-1", "delta_content": "verified"},
        ))
        matching.on_event(event(
            "assistant.message", "message-1", "delta-1",
            {"content": "", "tool_requests": []},
        ))
        matching.on_event(event(
            "assistant.usage", "usage-1", "message-1",
            {"model": "claude-sonnet-4.6"},
        ))
        matching.on_event(event("session.idle", "idle-1", "usage-1", {}))
        self.assertEqual(matching.validated_response(), "verified")

    def test_first_event_must_not_claim_an_unobserved_parent(self):
        gate = self.gate()
        gate.on_event(event(
            "assistant.message", "message-1", "missing-parent",
            {"content": "UNVERIFIED", "tool_requests": []},
        ))
        gate.on_event(event(
            "assistant.usage", "usage-1", "message-1",
            {"model": "claude-sonnet-4.6"},
        ))
        gate.on_event(event("session.idle", "idle-1", "usage-1", {}))
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "discontinuous"):
            gate.validated_response()

    def test_missing_usage_or_discontinuous_event_chain_fails_closed(self):
        missing = self.gate()
        missing.on_event(event("assistant.message", "message-1", None, {"content": "hidden", "tool_requests": []}))
        missing.on_event(event("session.idle", "idle-1", "message-1", {}))
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "idle before its model usage"):
            missing.validated_response()

        incomplete = self.gate()
        incomplete.on_event(event("assistant.message", "message-1", None, {"content": "not final", "tool_requests": []}))
        incomplete.on_event(event("assistant.usage", "usage-1", "message-1", {"model": "claude-sonnet-4.6"}))
        with self.assertRaisesRegex(CopilotSdkEvidenceError, "not emitted its single-turn idle"):
            incomplete.validated_response()

        discontinuous = self.gate()
        discontinuous.on_event(event("assistant.message", "message-1", None, {"content": "hidden", "tool_requests": []}))
        discontinuous.on_event(event("assistant.usage", "usage-1", "unrelated", {"model": "claude-sonnet-4.6"}))
        self.assertIn("discontinuous", discontinuous.blocked_reason)

    def test_pretool_requires_matching_session_and_still_denies_unattributable_tool(self):
        gate = self.gate()
        gate.on_event(event("assistant.message", "message-1", None, {"content": "", "tool_requests": [{"tool_call_id": "call-1", "name": "bash", "arguments": {"command": "true"}}]}))
        gate.on_event(event("assistant.usage", "usage-1", "message-1", {"model": "claude-sonnet-4.6"}))
        denied = gate.on_pre_tool_use({"session_id": "session-1", "tool_name": "bash"}, {"session_id": "session-1"})
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("diagnostic-only", denied["permissionDecisionReason"])

        wrong_session = self.gate()
        denied = wrong_session.on_pre_tool_use({}, {"session_id": "other"})
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("session identity", denied["permissionDecisionReason"])

    def test_ambiguous_or_multiple_tool_requests_are_denied(self):
        gate = self.gate()
        gate.on_event(event("assistant.message", "message-1", None, {"content": "", "tool_requests": [{}, {}]}))
        gate.on_event(event("assistant.usage", "usage-1", "message-1", {"model": "claude-sonnet-4.6"}))
        self.assertIn("tool requests are unsupported", gate.blocked_reason)

    def test_subagent_start_and_replayed_event_are_denied(self):
        gate = self.gate()
        gate.on_event(event("subagent.started", "subagent-1", None, {"agent_name": "worker"}))
        self.assertIn("sub-agent tool attribution", gate.blocked_reason)

        replay = self.gate()
        replay.on_event(event("assistant.message", "event-1", None, {"content": "", "tool_requests": []}))
        replay.on_event(event("session.idle", "event-1", "event-1", {}))
        self.assertIn("replayed", replay.blocked_reason)


if __name__ == "__main__":
    unittest.main()
