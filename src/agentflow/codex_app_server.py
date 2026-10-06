"""Optional, typed adapter for the official Codex Python SDK.

The SDK and its bundled Codex runtime are imported only when this transport is
selected. Agentflow's Herdr CLI transport remains the compatibility default.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import re
import threading
from typing import Any, Callable, Mapping


SUPPORTED_SDK_VERSION = "0.160.1"
RPC_TIMEOUT_SECONDS = 10.0
_ACTIVE_TURNS_LOCK = threading.Lock()
_ACTIVE_TURNS: dict[tuple[str, str], dict[str, Any]] = {}


class CodexAppServerError(RuntimeError):
    """Safe provider adapter error; never carries raw SDK/stdout diagnostics."""


@dataclasses.dataclass(frozen=True)
class TurnObservation:
    status: str
    rerouted: bool = False
    reason_code: str = ""


@dataclasses.dataclass(frozen=True)
class TurnIdentity:
    """Persisted protocol identity bound to one root/task/claim/lease/workspace."""

    workspace_root: str
    workflow_root: str
    task_id: str
    claim_id: str
    lease_epoch: int
    lease_continuity_id: str
    lease_token_sha256: str
    cwd: str
    model: str
    effort: str
    thread_id: str
    turn_id: str
    sdk_version: str = SUPPORTED_SDK_VERSION
    model_evidence: str = "codex-app-server-protocol-cooperative"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def bind_identity(
    value: Mapping[str, Any], *,
    workspace_root: str,
    workflow_root: str,
    task_id: str,
    claim_id: str,
    lease_epoch: int,
    lease_continuity_id: str,
    lease_token: str,
    cwd: str,
    model: str,
    effort: str,
) -> TurnIdentity:
    """Validate provider-returned thread/turn IDs against controller authority."""
    if not isinstance(value, Mapping):
        raise CodexAppServerError("persisted Codex SDK identity is missing")
    expected = {
        "workspace_root": workspace_root,
        "workflow_root": workflow_root,
        "task_id": task_id,
        "claim_id": claim_id,
        "lease_epoch": lease_epoch,
        "lease_continuity_id": lease_continuity_id,
        "lease_token_sha256": hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
        "cwd": cwd,
        "model": model,
        "effort": effort,
        "sdk_version": SUPPORTED_SDK_VERSION,
        "model_evidence": "codex-app-server-protocol-cooperative",
    }
    for field, wanted in expected.items():
        observed = value.get(field)
        if isinstance(wanted, str):
            if not isinstance(observed, str) or not hmac.compare_digest(observed, wanted):
                raise CodexAppServerError(f"persisted Codex SDK {field} identity changed")
        elif observed != wanted:
            raise CodexAppServerError(f"persisted Codex SDK {field} identity changed")
    thread_id = value.get("thread_id")
    turn_id = value.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(turn_id, str) or not turn_id:
        raise CodexAppServerError("persisted Codex SDK thread/turn identity is incomplete")
    return TurnIdentity(**{**expected, "thread_id": thread_id, "turn_id": turn_id})


def bind_recovered_identity(
    value: Mapping[str, Any], *, launch_snapshot: Mapping[str, Any],
    current_continuity_id: str,
) -> TurnIdentity:
    """Rebind to the signed original launch after a same-owner lease reattach.

    Lease epochs and bearer tokens legitimately rotate on reattach. The
    identity is checked against the controller-signed launch snapshot, while
    the current lease must retain the same continuity ID. A different-owner
    takeover rotates continuity and therefore cannot adopt the old turn.
    The caller must verify the snapshot's controller MAC before calling this.
    """
    continuity_id = str(launch_snapshot.get("continuity_id") or "")
    if not continuity_id or not hmac.compare_digest(continuity_id, current_continuity_id):
        raise CodexAppServerError("persisted Codex SDK continuity identity is stale")
    try:
        return bind_identity(
            value,
            workspace_root=str(launch_snapshot["workspace_root"]),
            workflow_root=str(launch_snapshot["workflow_root"]),
            task_id=str(launch_snapshot["task_id"]),
            claim_id=str(launch_snapshot["claim_id"]),
            lease_epoch=int(launch_snapshot["lease_epoch"]),
            lease_continuity_id=continuity_id,
            lease_token=str(launch_snapshot["lease_id"]),
            cwd=str(launch_snapshot["cwd"]),
            model=str(launch_snapshot["model"]),
            effort=str(launch_snapshot["effort"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CodexAppServerError("signed Codex launch snapshot is incomplete") from exc


def _sdk_modules() -> tuple[Any, Any, Any, Any]:
    try:
        import openai_codex
        from openai_codex import api as sdk_api
        from openai_codex.client import CodexClient, CodexConfig
        from openai_codex.generated.v2_all import (
            AccountTokenUsageSummary,
            GetAccountParams,
            GetAccountRateLimitsParams,
            GetAccountRateLimitsResponse,
            GetAccountTokenUsageParams,
            GetAccountTokenUsageResponse,
            GetAccountResponse,
            AskForApproval,
            AskForApprovalValue,
            ThreadReadResponse,
            ThreadResumeParams,
            ThreadResumeResponse,
            ThreadStartParams,
            ThreadStartResponse,
            TurnStartParams,
            TurnCompletedNotification,
            ModelReroutedNotification,
            ReasoningEffort,
            TurnStatus,
        )
        from openai_codex._inputs import SkillInput, TextInput
    except ImportError as exc:
        raise CodexAppServerError(
            "Codex SDK unavailable; install the optional saintdle-agentflow[codex] extra "
            "(or source install with .[codex])"
        ) from exc
    if str(getattr(openai_codex, "__version__", "")) != SUPPORTED_SDK_VERSION:
        raise CodexAppServerError("installed Codex SDK version is unsupported")
    return (
        {
            "version": openai_codex.__version__, "api": sdk_api,
            "client": CodexClient, "config": CodexConfig,
            "GetAccountParams": GetAccountParams,
            "GetAccountResponse": GetAccountResponse,
            "AskForApproval": AskForApproval,
            "AskForApprovalValue": AskForApprovalValue,
            "GetAccountRateLimitsParams": GetAccountRateLimitsParams,
            "GetAccountRateLimitsResponse": GetAccountRateLimitsResponse,
            "GetAccountTokenUsageParams": GetAccountTokenUsageParams,
            "GetAccountTokenUsageResponse": GetAccountTokenUsageResponse,
            "ThreadReadResponse": ThreadReadResponse,
            "ThreadResumeParams": ThreadResumeParams,
            "ThreadResumeResponse": ThreadResumeResponse,
            "ThreadStartParams": ThreadStartParams,
            "ThreadStartResponse": ThreadStartResponse,
            "TurnStartParams": TurnStartParams,
            "TurnCompletedNotification": TurnCompletedNotification,
            "ModelReroutedNotification": ModelReroutedNotification,
            "ReasoningEffort": ReasoningEffort,
            "TurnStatus": TurnStatus,
            "SkillInput": SkillInput,
            "TextInput": TextInput,
        },
        openai_codex,
        CodexClient,
        CodexConfig,
    )


def _bounded_call(client: Any, function: Callable[[], Any], *, timeout: float = RPC_TIMEOUT_SECONDS) -> Any:
    """Bound a synchronous SDK request; close the stdio process to unblock it."""
    done = threading.Event()
    result: list[Any] = []
    failure: list[BaseException] = []

    def invoke() -> None:
        try:
            result.append(function())
        except BaseException as exc:  # forwarded without rendering its contents
            failure.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=invoke, name="agentflow-codex-rpc", daemon=True)
    worker.start()
    if not done.wait(timeout):
        try:
            client.close()
        except Exception:
            pass
        raise CodexAppServerError("Codex App Server request timed out; launch outcome may be ambiguous")
    if failure:
        raise CodexAppServerError("Codex App Server request failed") from failure[0]
    return result[0] if result else None


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 1_000_000_000_000:
        return None
    return value


def _wire_params(value: Any) -> dict[str, Any]:
    """Serialize a typed SDK model to the JSON object CodexClient.request expects."""
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise CodexAppServerError("Codex SDK request parameters are unavailable")
    result = model_dump(by_alias=True, exclude_none=True)
    if not isinstance(result, dict):
        raise CodexAppServerError("Codex SDK request parameters are malformed")
    return result


def _safe_window(window: Any) -> dict[str, int | None] | None:
    if window is None:
        return None
    def field(name: str, alias: str) -> Any:
        if isinstance(window, Mapping):
            return window.get(name, window.get(alias))
        return getattr(window, name, None)
    return {
        "used_percent": _numeric(field("used_percent", "usedPercent")),
        "window_duration_mins": _numeric(field("window_duration_mins", "windowDurationMins")),
        "resets_at": _numeric(field("resets_at", "resetsAt")),
    }


def permission_for_profile(
    tool_profile: str, *, cwd: str, output_boundary: str, sterile: bool = False,
) -> dict[str, Any]:
    """Map only explicitly approved handoff profiles to bounded Codex policy."""
    if sterile:
        raise CodexAppServerError("Codex App Server cannot preserve the sterile outbound boundary")
    if tool_profile == "shell-readonly":
        return {"sandbox": "read-only", "network_access": False, "approval_policy": "never"}
    raise CodexAppServerError(f"Codex App Server does not support tool profile {tool_profile!r}")


def _turn_text(turn: Any) -> str:
    items = getattr(turn, "items", None)
    if not isinstance(items, list):
        return ""
    fallback = ""
    for item in reversed(items):
        item = getattr(item, "root", item)
        if getattr(item, "type", "") != "agentMessage":
            continue
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        phase = getattr(item, "phase", None)
        phase = getattr(phase, "value", phase)
        if phase == "final_answer":
            return text
        if phase is None and not fallback:
            fallback = text
    return fallback


def _watch_turn(
    client: Any, handle: Any, thread_id: str,
    on_complete: Callable[[TurnObservation, str], None],
) -> None:
    rerouted = False
    try:
        completed_turn: Any = None
        for event in handle.stream():
            payload = getattr(event, "payload", None)
            method = str(getattr(event, "method", ""))
            if method == "model/rerouted":
                turn_id = str(getattr(payload, "turn_id", ""))
                if turn_id == str(getattr(handle, "id", "")):
                    # A route change is disqualifying even if it later returns
                    # to the requested model; no implicit retry/fallback.
                    rerouted = True
            if method == "turn/completed":
                turn = getattr(payload, "turn", None)
                if str(getattr(turn, "id", "")) == str(getattr(handle, "id", "")):
                    completed_turn = turn
        status_value = getattr(getattr(completed_turn, "status", None), "value", "")
        if completed_turn is None:
            observation = TurnObservation("server_lost", reason_code="completion_missing")
            output = ""
        elif rerouted:
            observation = TurnObservation("failed", rerouted=True, reason_code="model_rerouted")
            output = ""
        elif status_value == "completed":
            observation = TurnObservation("completed")
            output = _turn_text(completed_turn)
        elif status_value in {"failed", "interrupted", "inProgress"}:
            normalized = {"inProgress": "interrupted"}.get(status_value, status_value)
            observation = TurnObservation(normalized, reason_code=f"turn_{normalized}")
            output = ""
        else:
            observation = TurnObservation("server_lost", reason_code="turn_status_unknown")
            output = ""
        if observation.status == "completed" and not output:
            observation = TurnObservation("failed", reason_code="final_output_missing")
        _record_turn_state(handle, observation, thread_id)
        on_complete(observation, output)
    except BaseException:
        # Never leak SDK exceptions, raw stderr, history, or provider output.
        try:
            observation = TurnObservation("server_lost", reason_code="rpc_failed")
            _record_turn_state(handle, observation, thread_id)
            on_complete(observation, "")
        except Exception:
            pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def start_background_turn(
    *, cwd: str, model: str, effort: str, instruction: str,
    skills: list[tuple[str, str]], tool_profile: str, output_boundary: str,
    sterile: bool, output_schema: Mapping[str, Any],
    on_thread: Callable[[str], None], on_turn: Callable[[str], None],
    on_complete: Callable[[TurnObservation, str], None],
    client_factory: Callable[[Any], Any] | None = None,
) -> tuple[str, str]:
    """Start one exact typed SDK turn; inference then runs outside controller polling.

    The caller must persist the thread callback before this function requests a
    turn, then persist the returned turn ID before it returns to dispatch. Any
    timeout after a request starts is ambiguous and must not be retried.
    """
    permission_for_profile(
        tool_profile, cwd=cwd, output_boundary=output_boundary, sterile=sterile,
    )
    try:
        modules, _sdk, client_type, config_type = _sdk_modules()
    except CodexAppServerError:
        raise
    config = config_type(cwd=cwd)
    client = client_factory(config) if client_factory else client_type(config)
    try:
        client.start()
        _bounded_call(client, client.initialize)
        sandbox_mode = modules["api"].Sandbox.read_only
        start_params = modules["ThreadStartParams"](
            cwd=cwd, model=model, sandbox=sandbox_mode,
            config={"features": {"multi_agent": False}},
            approval_policy=modules["AskForApproval"](
                root=modules["AskForApprovalValue"].never,
            ),
        )
        started = _bounded_call(client, lambda: client.thread_start(start_params))
        thread = getattr(started, "thread", None)
        thread_id = str(getattr(thread, "id", ""))
        if not thread_id:
            raise CodexAppServerError("Codex App Server did not return a thread identity")
        observed_model = str(getattr(thread, "model", ""))
        observed_cwd = str(getattr(thread, "cwd", ""))
        if observed_model != model or os.path.realpath(observed_cwd) != os.path.realpath(cwd):
            raise CodexAppServerError("Codex App Server thread configuration mismatched the approved route")
        on_thread(thread_id)
        sdk_thread = modules["api"].Thread(client, thread_id)
        inputs = [modules["SkillInput"](name=name, path=path) for name, path in skills]
        inputs.append(modules["TextInput"](text=instruction))
        try:
            reasoning = modules["ReasoningEffort"](effort)
        except ValueError as exc:
            raise CodexAppServerError("approved Codex reasoning effort is unsupported") from exc
        handle = _bounded_call(
            client,
            lambda: sdk_thread.turn(
                inputs, cwd=cwd, model=model, effort=reasoning,
                output_schema=dict(output_schema),
                approval_mode=modules["api"].ApprovalMode.deny_all,
                sandbox=modules["api"].Sandbox.read_only,
            ),
        )
        turn_id = str(getattr(handle, "id", ""))
        if not turn_id:
            raise CodexAppServerError("Codex App Server did not return a turn identity")
        on_turn(turn_id)
        with _ACTIVE_TURNS_LOCK:
            _ACTIVE_TURNS[(thread_id, turn_id)] = {"status": "running"}
        watcher = threading.Thread(
            target=_watch_turn, args=(client, handle, thread_id, on_complete),
            name="agentflow-codex-turn", daemon=True,
        )
        watcher.start()
        return thread_id, turn_id
    except BaseException:
        # If any launch request may already have reached the server, callers
        # mark the attempt ambiguous and never issue a replacement turn.
        try:
            client.close()
        except Exception:
            pass
        raise


def _record_turn_state(handle: Any, observation: TurnObservation, thread_id: str) -> None:
    key = (thread_id, str(getattr(handle, "id", "")))
    with _ACTIVE_TURNS_LOCK:
        _ACTIVE_TURNS[key] = {
            "status": observation.status,
            "rerouted": observation.rerouted,
            "reason_code": observation.reason_code,
        }


def local_turn_state(thread_id: str, turn_id: str) -> dict[str, Any] | None:
    """Return process-local watcher state; None means recovery cannot attach."""
    with _ACTIVE_TURNS_LOCK:
        value = _ACTIVE_TURNS.get((thread_id, turn_id))
        return dict(value) if isinstance(value, Mapping) else None


def diagnostics_report(*, client_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Read account/auth/rate-limit/token data through typed SDK calls only.

    No login, logout, token refresh, reset-credit consume, catalogue inference,
    free-form provider error, email, or account identifier is returned.
    """
    try:
        modules, sdk, client_type, config_type = _sdk_modules()
    except CodexAppServerError as exc:
        return {
            "operation": "codex diagnostics", "ok": False,
            "sdk_status": "unavailable", "support": {},
            "error_code": "sdk_unavailable" if "unavailable" in str(exc) else "sdk_version_unsupported",
        }
    config = config_type()
    client = client_factory(config) if client_factory else client_type(config)
    report: dict[str, Any] = {
        "operation": "codex diagnostics", "ok": False,
        "sdk_version": str(sdk.__version__), "runtime_version": None,
        "sdk_status": "available", "authentication_type": "unknown",
        "authenticated": False,
        "support": {"account_read": "unsupported", "rate_limits": "unsupported", "token_usage": "unsupported"},
        "quota": None, "tokens": None,
    }
    try:
        client.start()
        initialized = _bounded_call(client, client.initialize)
        # This SDK version may omit serverInfo.version. Only expose a strict
        # semver from the documented initialize userAgent field, never the
        # raw string (which can carry host/platform details).
        user_agent = getattr(initialized, "userAgent", None)
        match = re.fullmatch(
            r"(?:Codex(?: CLI)? )?(?:codex/|v)?(\d+\.\d+\.\d+)(?:\b|[-+][A-Za-z0-9.-]*)[^\r\n]*",
            user_agent.strip() if isinstance(user_agent, str) else "",
        )
        if match:
            report["runtime_version"] = match.group(1)
        account = _bounded_call(
            client,
            lambda: client.account_read(modules["GetAccountParams"](refresh_token=False)),
        )
        report["support"]["account_read"] = "supported"
        account_value = getattr(account, "account", None)
        account_root = getattr(account_value, "root", None)
        auth_type = getattr(account_root, "type", None)
        auth_value = getattr(auth_type, "value", auth_type)
        report["authentication_type"] = (
            auth_value if auth_value in {"chatgpt", "apiKey", "amazonBedrock"} else "unknown"
        )
        report["authenticated"] = bool(account_value is not None)

        rate_limits = _bounded_call(
            client,
            lambda: client.request(
                "account/rateLimits/read",
                _wire_params(modules["GetAccountRateLimitsParams"](
                    exclude_reset_credit_details=True, supports_luna_reserve=False,
                )),
                response_model=modules["GetAccountRateLimitsResponse"],
            ),
        )
        snapshot = getattr(rate_limits, "rate_limits", None)
        by_limit_id = getattr(rate_limits, "rate_limits_by_limit_id", None)
        codex_snapshot = (
            by_limit_id.get("codex") if isinstance(by_limit_id, Mapping) else None
        )
        if codex_snapshot is None:
            codex_snapshot = snapshot
        report["support"]["rate_limits"] = "supported"
        report["quota"] = {
            "ordinary_usage_allowed": getattr(rate_limits, "ordinary_usage_allowed", None)
            if isinstance(getattr(rate_limits, "ordinary_usage_allowed", None), bool) else None,
            "primary": _safe_window(
                codex_snapshot.get("primary") if isinstance(codex_snapshot, Mapping)
                else getattr(codex_snapshot, "primary", None)
            ),
            "secondary": _safe_window(
                codex_snapshot.get("secondary") if isinstance(codex_snapshot, Mapping)
                else getattr(codex_snapshot, "secondary", None)
            ),
        }

        token_usage = _bounded_call(
            client,
            lambda: client.request(
                "account/usage/read", _wire_params(modules["GetAccountTokenUsageParams"]()),
                response_model=modules["GetAccountTokenUsageResponse"],
            ),
        )
        report["support"]["token_usage"] = "supported"
        summary = getattr(token_usage, "summary", None)
        report["tokens"] = {
            "lifetime": _numeric(getattr(summary, "lifetime_tokens", None)),
            "peak_daily": _numeric(getattr(summary, "peak_daily_tokens", None)),
        }
        report["ok"] = True
    except Exception as exc:
        # Exception text and provider output can contain account metadata.
        report["error_code"] = "rpc_unsupported" if type(exc).__name__ == "MethodNotFoundError" else "rpc_failed"
    finally:
        try:
            client.close()
        except Exception:
            pass
    return report


__all__ = [
    "SUPPORTED_SDK_VERSION", "RPC_TIMEOUT_SECONDS", "CodexAppServerError",
    "TurnObservation", "TurnIdentity", "bind_identity", "bind_recovered_identity",
    "permission_for_profile", "start_background_turn", "local_turn_state", "diagnostics_report",
]
