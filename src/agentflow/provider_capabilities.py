"""Descriptive compatibility contract for the supported provider lanes.

These capabilities describe Agentflow's integration surface, not provider
account access, subscription entitlements, or current model availability.
Exact external model routes remain governed by :mod:`agentflow.model_policy`
and are checked again by the controller before dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityState(str, Enum):
    """Whether a provider lane is available through Agentflow's contract."""

    SUPPORTED = "supported"
    CONDITIONAL = "conditional"
    DISABLED = "disabled"


class ProviderCapabilityError(ValueError):
    """Raised for an unknown provider or compatibility lane."""


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    chat_controller: CapabilityState
    native_subagents: CapabilityState
    direct_cli: CapabilityState
    persistent_herdr: CapabilityState
    persistent_app_server: CapabilityState = CapabilityState.DISABLED
    herdr_minimum_version: str | None = None
    herdr_note: str = ""
    app_server_note: str = ""

    def lane_state(self, lane: str) -> CapabilityState:
        """Return one named lane's state; reject unknown lanes fail-closed."""
        if not isinstance(lane, str):
            raise ProviderCapabilityError(f"unknown provider lane: {lane!r}")
        field_by_lane = {
            "chat-controller": "chat_controller",
            "native": "native_subagents",
            "direct-cli": "direct_cli",
            "persistent-herdr": "persistent_herdr",
            "persistent-app-server": "persistent_app_server",
        }
        field = field_by_lane.get(lane)
        if field is None:
            raise ProviderCapabilityError(f"unknown provider lane: {lane!r}")
        return getattr(self, field)


_CAPABILITIES = (
    ProviderCapability(
        provider="codex",
        chat_controller=CapabilityState.SUPPORTED,
        native_subagents=CapabilityState.SUPPORTED,
        direct_cli=CapabilityState.SUPPORTED,
        persistent_herdr=CapabilityState.SUPPORTED,
        persistent_app_server=CapabilityState.CONDITIONAL,
        herdr_note="Install and verify the Codex Herdr integration before dispatch.",
        app_server_note=(
            "Optional openai-codex 0.160.1 extra; only shell-readonly handoffs, pinned to "
            "read-only sandbox and never approval with network and native subdelegation off. "
            "Thread/model evidence is cooperative local protocol provenance, not attestation. "
            "Herdr remains the default; sterile/restricted launches are unsupported."
        ),
    ),
    ProviderCapability(
        provider="claude",
        chat_controller=CapabilityState.SUPPORTED,
        native_subagents=CapabilityState.SUPPORTED,
        direct_cli=CapabilityState.SUPPORTED,
        persistent_herdr=CapabilityState.CONDITIONAL,
        persistent_app_server=CapabilityState.DISABLED,
        herdr_minimum_version="2.1.251",
        herdr_note=(
            "Requires Claude Code 2.1.251+, controlled SessionStart and "
            "PostModelSwitch hooks, and an exact primary/fallback model pin."
        ),
    ),
    ProviderCapability(
        provider="copilot",
        chat_controller=CapabilityState.SUPPORTED,
        native_subagents=CapabilityState.SUPPORTED,
        direct_cli=CapabilityState.SUPPORTED,
        persistent_herdr=CapabilityState.DISABLED,
        persistent_app_server=CapabilityState.DISABLED,
        herdr_note=(
            "Persistent worker launch fails closed because Agentflow cannot "
            "attest Copilot's resolved model from a protected source."
        ),
    ),
)
_CAPABILITIES_BY_PROVIDER = {capability.provider: capability for capability in _CAPABILITIES}


def provider_capabilities() -> tuple[ProviderCapability, ...]:
    """Return the immutable provider matrix in stable display order."""
    return _CAPABILITIES


def capability_for(provider: str) -> ProviderCapability:
    """Return one provider's capability record or fail closed."""
    try:
        return _CAPABILITIES_BY_PROVIDER[provider]
    except (KeyError, TypeError) as exc:
        raise ProviderCapabilityError(f"unsupported provider: {provider!r}") from exc


__all__ = [
    "CapabilityState", "ProviderCapability", "ProviderCapabilityError",
    "provider_capabilities", "capability_for",
]
