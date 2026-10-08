"""Pure decisions for reconciling a claimed task with its Herdr launch record.

The scheduler may reattach only to a committed Herdr identity. An incomplete
``launching`` reservation is deliberately not evidence that startup failed:
the provider pane may already exist, so recovery must stop for an operator
instead of authorizing another launch.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping


_BOUND_LIFECYCLE_STATES = frozenset({"launched", "running", "completed"})
_TERMINAL_LIFECYCLE_STATES = frozenset({"failed", "blocked", "cancelled", "canceled"})


@dataclass(frozen=True)
class LaunchRecoveryDecision:
    """A scheduler-independent decision for one checkpoint/lifecycle pair."""

    action: str
    reason: str = ""
    session_id: str = ""
    provider_terminal: bool = False


def herdr_pane_is_definitively_absent(returncode: int, output: str) -> bool:
    """Recognize only Herdr's structured ``pane_not_found`` response.

    A nonzero command status by itself is ambiguous: daemon, transport, and
    parse failures must never authorize retiring a launch. Herdr identifies
    ``pane get`` responses with a fixed CLI request id.
    """

    if returncode != 1 or not isinstance(output, str):
        return False
    try:
        response = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(response, Mapping) or response.get("id") != "cli:pane:get":
        return False
    error = response.get("error")
    return isinstance(error, Mapping) and error.get("code") == "pane_not_found"


def _operator_required(
    task_id: str,
    message: str,
    *,
    provider_terminal: bool = False,
) -> LaunchRecoveryDecision:
    task = f"task {task_id} " if task_id else "the claimed task "
    return LaunchRecoveryDecision(
        action="require_operator",
        reason=f"USER_ACTION_REQUIRED: {task}{message}",
        provider_terminal=provider_terminal,
    )


def reduce_incomplete_launch(
    checkpoint_state: str,
    session_record: Mapping[str, object] | None,
    *,
    task_id: str = "",
) -> LaunchRecoveryDecision:
    """Reduce durable launch state to a safe recovery action.

    The reducer has no I/O and performs no transitions itself. Callers share
    its decisions, then use their existing controller methods to bind a
    committed identity, wait for a result, or persist the operator halt.
    ``observed_identity`` is intentionally never treated as a committed
    ``binding``.
    """

    state = str(checkpoint_state or "").strip().lower()
    record = session_record if isinstance(session_record, Mapping) else None
    lifecycle = str(record.get("status") or "").strip().lower() if record else ""
    binding = record.get("binding") if record else None
    binding = binding if isinstance(binding, Mapping) else {}
    session_id = str(binding.get("session_id") or "")
    pane_id = str(record.get("pane_id") or "") if record else ""

    if lifecycle == "launching":
        if record and str(record.get("launch_outcome") or "").lower() == "ambiguous":
            observation = record.get("launch_observation")
            observed_pane = (
                str(observation.get("pane_id") or "")
                if isinstance(observation, Mapping) else ""
            )
            pane_note = f"; Herdr reported pane {observed_pane}" if observed_pane else ""
            return _operator_required(
                task_id,
                "Herdr start timed out with an ambiguous outcome"
                f"{pane_note}. Inspect the Herdr pane/session and determine whether it started; "
                "do not relaunch while the outcome is uncertain",
            )
        return _operator_required(
            task_id,
            "has an incomplete Herdr launching reservation without a committed binding. "
            "Inspect Herdr directly and reconcile whether the pane started before any retry",
        )

    if state == "claimed_no_session":
        if record is None:
            return _operator_required(
                task_id,
                "has a durable claim but no Herdr lifecycle record. Inspect the launch and "
                "reconcile its identity before any retry",
            )
        if lifecycle == "identity_pending":
            if pane_id and str(record.get("identity_pending_since") or ""):
                return LaunchRecoveryDecision(action="reattach_identity_pending")
            return _operator_required(
                task_id,
                "has incomplete identity-pending lifecycle metadata; inspect Herdr directly",
            )
        if lifecycle in _BOUND_LIFECYCLE_STATES and session_id:
            return LaunchRecoveryDecision(
                action="reattach_running", session_id=session_id,
            )
        if lifecycle in _TERMINAL_LIFECYCLE_STATES:
            return _operator_required(
                task_id,
                f"has terminal Herdr state {lifecycle} but no committed provider session "
                "binding; reconcile the terminal lifecycle before continuing",
                provider_terminal=True,
            )
        return _operator_required(
            task_id,
            f"has a durable claim but Herdr state {lifecycle or 'unknown'} has no committed "
            "provider identity. Inspect the live pane before any further action",
        )

    if state == "identity_pending":
        if record is None:
            return _operator_required(
                task_id,
                "is identity-pending in the checkpoint but has no Herdr lifecycle record; "
                "reconcile the pane before continuing",
            )
        if lifecycle == "identity_pending":
            if pane_id and str(record.get("identity_pending_since") or ""):
                return LaunchRecoveryDecision(action="poll_identity")
            return _operator_required(
                task_id,
                "has incomplete identity-pending lifecycle metadata; inspect Herdr directly",
            )
        if lifecycle in _BOUND_LIFECYCLE_STATES and session_id:
            return LaunchRecoveryDecision(action="continue")
        if lifecycle in _TERMINAL_LIFECYCLE_STATES:
            # Preserve the established authenticated-result/failure handling
            # for terminal provider records; this reducer does not grant
            # result authority or consume a return channel.
            return LaunchRecoveryDecision(action="continue")
        return _operator_required(
            task_id,
            f"is identity-pending in the checkpoint but Herdr state {lifecycle or 'unknown'} "
            "has no committed pane/session identity; inspect the launch before further action",
        )

    if state in {"running", "launched"}:
        # A running checkpoint already carries the controller's committed
        # session identity. Preserve its established result-authentication
        # path for missing, malformed, or terminal Herdr records; only the
        # explicit ``launching`` mismatch above changes it to an operator
        # halt. This reducer grants no result authority.
        return LaunchRecoveryDecision(action="continue", session_id=session_id)

    return LaunchRecoveryDecision(action="continue")
