"""Experimental Copilot SDK model-evidence gate.

The gate intentionally does not launch a provider session. It centralizes the
strict event checks a future controller-owned SDK transport must pass before
it can expose a response or permit tools. Current Python SDK hook inputs omit
tool-call and sub-agent identities, so execution remains fail-closed.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import re
import sys
import threading
from typing import Any, Mapping


class CopilotSdkEvidenceError(ValueError):
    """Raised when Copilot SDK model evidence is unavailable or ambiguous."""


@dataclasses.dataclass(frozen=True)
class CopilotSdkAvailability:
    available: bool
    reason: str
    sdk_version: str = ""


def sdk_availability(
    *, python_version: tuple[int, int] | None = None,
    distribution_version: str | None = None,
) -> CopilotSdkAvailability:
    """Check optional SDK requirements without importing or starting it."""
    current_python = python_version or (sys.version_info.major, sys.version_info.minor)
    if current_python < (3, 11):
        return CopilotSdkAvailability(
            False,
            "The optional GitHub Copilot SDK requires Python 3.11+; Agentflow core still supports Python 3.10.",
        )
    if distribution_version is None:
        try:
            distribution_version = importlib.metadata.version("github-copilot-sdk")
        except importlib.metadata.PackageNotFoundError:
            return CopilotSdkAvailability(
                False,
                "The optional github-copilot-sdk package is not installed; install Agentflow with its copilot-sdk extra.",
            )
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$", distribution_version)
    if not match or int(match.group(1)) != 1:
        return CopilotSdkAvailability(
            False,
            "The model-evidence adapter requires a supported github-copilot-sdk 1.x release.",
            distribution_version,
        )
        return CopilotSdkAvailability(
            True,
            "Copilot SDK 1.x is available; this does not enable a workflow transport.",
            distribution_version,
        )


def _get(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


@dataclasses.dataclass(frozen=True)
class ModelCallEvidence:
    sequence: int
    model: str
    agent_id: str | None
    event_id: str
    bound_to_message: bool


class CopilotSdkModelEvidenceGate:
    """Validate supplied SDK events for one fresh session.

    The SDK event envelope does not carry a session id. A future live bridge
    must bind its callback to one session object; this diagnostic component is
    not itself a session or callback registration. Its session id is checked
    only when a tool-hook invocation is supplied. Events are cooperative local
    evidence, not provider-signed attestation.
    """

    def __init__(self, *, launch_id: str, session_id: str, expected_model: str) -> None:
        required = (launch_id, session_id, expected_model)
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise CopilotSdkEvidenceError("launch id, SDK session id, and exact expected model are required")
        self.launch_id = launch_id
        self.session_id = session_id
        self.expected_model = expected_model
        self._lock = threading.RLock()
        self._last_event_id: str | None = None
        self._seen_event_ids: set[str] = set()
        self._pending_message: tuple[str, str | None, tuple[Any, ...], bool] | None = None
        self._calls: list[ModelCallEvidence] = []
        self._blocked_reason = ""
        self._response_chunks: list[str] = []
        self._root_responses: list[str] = []
        self._session_idle = False

    @property
    def calls(self) -> tuple[ModelCallEvidence, ...]:
        with self._lock:
            return tuple(self._calls)

    @property
    def blocked_reason(self) -> str:
        with self._lock:
            return self._blocked_reason

    def _block(self, reason: str) -> None:
        if not self._blocked_reason:
            self._blocked_reason = reason

    def on_event(self, event: Any) -> None:
        """Consume one supplied SDK-shaped event; malformed signals block."""
        with self._lock:
            event_type = _get(event, "type", default="")
            event_id = _get(event, "id", default="")
            parent_id = _get(event, "parent_id", "parentId")
            if not isinstance(event_type, str) or not event_type:
                self._block("SDK event type is missing")
                return
            event_identity_valid = isinstance(event_id, str) and bool(event_id)
            if not event_identity_valid:
                self._block("SDK event identity is missing")
                event_id = ""
            if event_identity_valid and event_id in self._seen_event_ids:
                self._block("SDK event identity was replayed")
                return
            chain_valid = event_identity_valid
            if self._last_event_id is not None and parent_id != self._last_event_id:
                self._block("SDK event chain is discontinuous or ambiguous")
                chain_valid = False
            if event_identity_valid:
                self._seen_event_ids.add(event_id)
                self._last_event_id = event_id

            if self._session_idle:
                self._block("SDK emitted an event after the single-turn session became idle")

            data = _get(event, "data", default={})
            agent_id = _get(event, "agent_id", "agentId")
            agent_id = agent_id if isinstance(agent_id, str) and agent_id else None

            if event_type in ("assistant.message", "assistant_message"):
                if self._pending_message is not None:
                    self._block("a second assistant message arrived before the prior call's usage event")
                tool_requests = _get(data, "tool_requests", "toolRequests", default=())
                if tool_requests is None:
                    tool_requests = ()
                if not isinstance(tool_requests, (list, tuple)):
                    self._block("assistant.message tool request list is malformed")
                    return
                self._pending_message = (event_id, agent_id, tuple(tool_requests), chain_valid)
                content = _get(data, "content", default="")
                if agent_id is None and isinstance(content, str) and content:
                    self._root_responses.append(content)
            elif event_type in ("assistant.message_delta", "assistant_message_delta"):
                if agent_id is None:
                    delta = _get(data, "delta_content", "deltaContent", default="")
                    if isinstance(delta, str):
                        self._response_chunks.append(delta)
            elif event_type in ("assistant.usage", "assistant_usage"):
                pending = self._pending_message
                message_id, message_agent, tool_requests, message_chain_valid = (
                    pending or ("", None, (), False)
                )
                bound = (
                    chain_valid
                    and message_chain_valid
                    and pending is not None
                    and event_identity_valid
                    and parent_id == message_id
                    and agent_id == message_agent
                )
                if not bound:
                    self._block("model usage has no unambiguous preceding assistant message")
                model = _get(data, "model", default="")
                if not isinstance(model, str) or not model.strip():
                    self._block("SDK usage event has no resolved model")
                    self._pending_message = None
                    return
                sequence = len(self._calls) + 1
                self._calls.append(ModelCallEvidence(sequence, model, agent_id, event_id, bound))
                self._pending_message = None
                if model != self.expected_model:
                    self._block(f"resolved model mismatch on API call {sequence}")
                    return
                if not bound:
                    return
                if tool_requests:
                    self._block("tool requests are unsupported by the diagnostic-only adapter")
                    return
                if agent_id is not None:
                    # Subagent API calls are recorded and model-checked above,
                    # but cannot safely be joined to a root task/tool lifecycle.
                    self._block("sub-agent calls are observed but unsupported by the diagnostic-only adapter")
                    return
                self._pending_message = None
            elif event_type == "subagent.started":
                # Python on_pre_tool_use exposes no agent id/call id, so do not
                # let a nested worker run tools under ambiguous attribution.
                # Continue auditing subsequent usage events for observability.
                self._block("Copilot SDK sub-agent tool attribution is unsupported by the Python hook contract")

            elif event_type in ("tool.execution_start", "tool_execution_start"):
                self._block("SDK began a tool before an exact tool-call identity could be approved")
            elif event_type in ("session.idle", "session_idle"):
                if self._pending_message is not None:
                    self._block("SDK session became idle before its model usage event finalized")
                self._session_idle = True

    def on_pre_tool_use(self, input_data: Any, invocation: Any) -> dict[str, str]:
        """Fail closed for every tool hook while checking its session identity."""
        with self._lock:
            invoked_session = _get(invocation, "session_id", "sessionId", default="")
            input_session = _get(input_data, "session_id", "sessionId", default=invoked_session)
            if invoked_session != self.session_id or input_session != self.session_id:
                self._block("pre-tool hook session identity does not match this launch")
            if self._blocked_reason:
                return {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": self._blocked_reason,
                }
            # The Python hook omits tool-call id and agent id. Matching only
            # tool names/arguments cannot prove exact invocation correspondence,
            # therefore tools remain denied until that API contract gains IDs.
            self._block("Copilot Python pre-tool hook lacks tool-call identity; SDK workflow transport is disabled")
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": self._blocked_reason,
            }

    def validated_response(self) -> str:
        """Return buffered root text only if local evidence checks all passed."""
        with self._lock:
            if self._blocked_reason:
                raise CopilotSdkEvidenceError(self._blocked_reason)
            if not self._calls:
                raise CopilotSdkEvidenceError("SDK session produced no model usage evidence")
            if self._pending_message is not None:
                raise CopilotSdkEvidenceError("SDK session ended before model usage finalized its last message")
            if not self._session_idle:
                raise CopilotSdkEvidenceError("SDK session has not emitted its single-turn idle event")
            if any(call.model != self.expected_model for call in self._calls):
                raise CopilotSdkEvidenceError("SDK session contains a model mismatch")
            return "".join(self._response_chunks) or "\n".join(self._root_responses)


__all__ = [
    "CopilotSdkAvailability", "CopilotSdkEvidenceError", "CopilotSdkModelEvidenceGate",
    "ModelCallEvidence", "sdk_availability",
]
