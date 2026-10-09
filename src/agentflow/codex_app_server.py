"""Optional, typed adapter for the official Codex Python SDK.

The SDK and its bundled Codex runtime are imported only when this transport is
selected. Agentflow's Herdr CLI transport remains the compatibility default.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


SUPPORTED_SDK_VERSION = "0.160.1"
RPC_TIMEOUT_SECONDS = 10.0
DEFAULT_WORKER_TIMEOUT_SECONDS = 1800
MAX_WORKER_TIMEOUT_SECONDS = 86400
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
    """Fail closed until the pinned SDK runtime proves the required boundary.

    SDK 0.160.1 can pass thread configuration and reports active permission
    profile metadata. Its generated schema does not prove profile enforcement,
    enumerate the complete effective tool inventory, or establish same-user
    isolation from controller authority, so profile metadata alone is not
    sufficient evidence to admit a worker.
    """
    if sterile:
        raise CodexAppServerError("Codex App Server cannot preserve the sterile outbound boundary")
    if tool_profile != "shell-readonly":
        raise CodexAppServerError(f"Codex App Server does not support tool profile {tool_profile!r}")
    raise CodexAppServerError(
        "Codex App Server launch blocked: active permission-profile metadata is available, but "
        "profile enforcement, complete effective-tool inventory, and same-user isolation from "
        "controller authority remain unverified"
    )


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
    *, timeout_seconds: int | None = None,
) -> None:
    stream_timeout = timeout_seconds if timeout_seconds is not None else RPC_TIMEOUT_SECONDS
    rerouted = False
    try:
        completed_turn: Any = None
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def stream_events() -> None:
            try:
                for streamed_event in handle.stream():
                    events.put(("event", streamed_event))
            except BaseException as exc:
                events.put(("error", exc))
            else:
                events.put(("end", None))

        stream_thread = threading.Thread(
            target=stream_events, name="agentflow-codex-stream", daemon=True,
        )
        stream_thread.start()
        deadline = time.monotonic() + stream_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                observation = TurnObservation("interrupted", reason_code="worker_timeout")
                _record_turn_state(handle, observation, thread_id)
                threading.Thread(target=client.close, name="agentflow-codex-close", daemon=True).start()
                on_complete(observation, "")
                return
            try:
                event_kind, event = events.get(timeout=remaining)
            except queue.Empty:
                observation = TurnObservation("interrupted", reason_code="worker_timeout")
                _record_turn_state(handle, observation, thread_id)
                threading.Thread(target=client.close, name="agentflow-codex-close", daemon=True).start()
                on_complete(observation, "")
                return
            if event_kind == "end":
                break
            if event_kind == "error":
                raise event
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
    skill_validator: Callable[[], None] | None = None,
    client_factory: Callable[[Any], Any] | None = None,
    timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
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
    if (
        not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= MAX_WORKER_TIMEOUT_SECONDS
    ):
        raise CodexAppServerError("Codex worker timeout is outside the supported range")
    config_overrides = (
        "mcp_servers={}", "plugins={}", "apps={}", 'web_search="disabled"',
        'sandbox_mode="read-only"', "sandbox_workspace_write.network_access=false",
        "features.web_search=false", "features.web_search_cached=false",
        "features.web_search_request=false", "features.multi_agent=false",
        "features.skill_mcp_dependency_install=false",
    )
    config = config_type(cwd=cwd, config_overrides=config_overrides)
    client = client_factory(config) if client_factory else client_type(config)
    try:
        client.start()
        _bounded_call(client, client.initialize)
        sandbox_mode = modules["api"].Sandbox.read_only
        start_params = modules["ThreadStartParams"](
            cwd=cwd, model=model, sandbox=sandbox_mode,
            config={
                "mcp_servers": {}, "plugins": {}, "apps": {},
                "web_search": "disabled", "sandbox_mode": "read-only",
                "sandbox_workspace_write": {"network_access": False},
                "features": {
                    "web_search": False, "web_search_cached": False,
                    "web_search_request": False, "multi_agent": False,
                    "skill_mcp_dependency_install": False,
                },
            },
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
        if skill_validator is not None:
            # The supervisor waits for the controller to recheck live claim
            # authority and persist the thread identity before acknowledging.
            # Revalidate the exact preflight package pins after that boundary
            # and immediately before the SDK can submit them to a turn.
            skill_validator()
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
            kwargs={"timeout_seconds": timeout_seconds},
            name="agentflow-codex-turn", daemon=True,
        )
        watcher.start()
        return thread_id, turn_id
    except BaseException:
        try:
            client.close()
        except Exception:
            pass
        raise


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_size > 512 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _supervisor_paths(runtime_dir: Path) -> tuple[Path, Path, Path, Path, Path]:
    runtime_dir = Path(runtime_dir)
    return (
        runtime_dir / "codex-worker.request.json",
        runtime_dir / "codex-worker.state.json",
        runtime_dir / "codex-worker.lock",
        runtime_dir / "codex-worker.thread-ack",
        runtime_dir / "codex-worker.output.json",
    )


def _supervised_worker(request_path: Path) -> int:
    """Private child entry point; owns the SDK client across controller exits."""
    try:
        import fcntl
    except ImportError:
        return 2
    runtime_dir = request_path.parent
    request_file, state_file, lock_file, ack_file, output_file = _supervisor_paths(runtime_dir)
    request = _read_private_json(request_file)
    if not isinstance(request, dict) or not isinstance(request.get("request_id"), str):
        return 2
    try:
        lock_handle = lock_file.open("a", encoding="utf-8")
        os.chmod(lock_file, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        return 3
    request_id = request["request_id"]
    state: dict[str, Any] = {"request_id": request_id, "status": "starting", "pid": os.getpid()}
    _write_private_json(state_file, state)
    finished = threading.Event()

    def save(status: str, **fields: Any) -> None:
        state.update({"request_id": request_id, "status": status, **fields})
        _write_private_json(state_file, state)

    def on_thread(thread_id: str) -> None:
        save("thread_created", thread_id=thread_id, model=request["model"], cwd=request["cwd"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ack_file.exists() and not ack_file.is_symlink():
                return
            time.sleep(0.05)
        save("failed", reason_code="controller_thread_ack_missing", thread_id=thread_id)
        raise CodexAppServerError("controller did not persist Codex thread identity")

    def on_turn(turn_id: str) -> None:
        save("running", thread_id=state.get("thread_id", ""), turn_id=turn_id,
             model=request["model"], cwd=request["cwd"])

    def on_complete(observation: TurnObservation, output: str) -> None:
        if observation.status == "completed" and len(output.encode("utf-8")) <= 128 * 1024:
            _write_private_json(output_file, {"request_id": request_id, "output": output})
        elif observation.status == "completed":
            observation = TurnObservation("failed", reason_code="output_limit_exceeded")
        save(
            observation.status, thread_id=state.get("thread_id", ""),
            turn_id=state.get("turn_id", ""), model=request["model"], cwd=request["cwd"],
            rerouted=observation.rerouted, reason_code=observation.reason_code,
            output_available=observation.status == "completed",
        )
        finished.set()

    def validate_skill_packages() -> None:
        manifest = request.get("skill_manifest")
        if not isinstance(manifest, dict):
            raise CodexAppServerError("Codex worker is missing its verified skill pins")
        try:
            # Reuse Agentflow's full transitive package hashing and registration
            # checks; do not replace these with an entrypoint-only digest here.
            from agentflow.cli import _codex_skill_inputs

            verified = _codex_skill_inputs(Path(request["cwd"]), manifest)
        except (ImportError, OSError, ValueError) as exc:
            raise CodexAppServerError("Codex worker skill package changed before turn submission") from exc
        if verified != [tuple(item) for item in request["skills"]]:
            raise CodexAppServerError("Codex worker skill package identity changed before turn submission")

    try:
        thread_id, _turn_id = start_background_turn(
            cwd=request["cwd"], model=request["model"], effort=request["effort"],
            instruction=request["instruction"], skills=request["skills"],
            tool_profile=request["tool_profile"], output_boundary=request["output_boundary"],
            sterile=False, output_schema=request["output_schema"], on_thread=on_thread,
            on_turn=on_turn, on_complete=on_complete,
            skill_validator=validate_skill_packages,
            timeout_seconds=request["timeout_seconds"],
        )
        # on_turn has persisted the ID before this wait can begin.
        if not finished.wait(request["timeout_seconds"] + RPC_TIMEOUT_SECONDS + 5):
            save("interrupted", thread_id=thread_id, turn_id=state.get("turn_id", ""),
                 reason_code="worker_timeout", model=request["model"], cwd=request["cwd"])
    except BaseException:
        if state.get("status") not in {"failed", "interrupted", "completed"}:
            save("server_lost", thread_id=state.get("thread_id", ""),
                 turn_id=state.get("turn_id", ""), reason_code="worker_failed")
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass
    return 0


def start_supervised_turn(
    *, runtime_dir: Path, request_id: str, cwd: str, model: str, effort: str,
    instruction: str, skills: list[tuple[str, str]], tool_profile: str,
    skill_manifest: Mapping[str, Any],
    output_boundary: str, output_schema: Mapping[str, Any], timeout_seconds: int,
    sterile: bool = False,
    on_thread: Callable[[str], None], on_turn: Callable[[str], None],
    _worker_command: list[str] | None = None,
) -> tuple[str, str]:
    """Start a detached local SDK helper; a controller restart does not kill its turn."""
    permission_for_profile(tool_profile, cwd=cwd, output_boundary=output_boundary, sterile=sterile)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise CodexAppServerError("Codex worker request identity is invalid")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_WORKER_TIMEOUT_SECONDS:
        raise CodexAppServerError("Codex worker timeout is outside the supported range")
    _modules, _sdk, _client_type, config_type = _sdk_modules()
    # Fail before creating a helper or requesting a thread if this pinned SDK
    # cannot express the restrictive inherited-config overrides.
    config_type(
        cwd=cwd,
        config_overrides=(
            "mcp_servers={}", "plugins={}", "apps={}", 'web_search="disabled"',
            'sandbox_mode="read-only"', "sandbox_workspace_write.network_access=false",
            "features.web_search=false", "features.web_search_cached=false",
            "features.web_search_request=false", "features.multi_agent=false",
            "features.skill_mcp_dependency_install=false",
        ),
    )
    runtime_dir = Path(runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    request_file, state_file, lock_file, ack_file, _output_file = _supervisor_paths(runtime_dir)
    existing = _read_private_json(state_file)
    if request_file.exists() or state_file.exists() or lock_file.exists():
        if existing and existing.get("request_id") != request_id:
            raise CodexAppServerError("Codex worker launch identity changed")
        raise CodexAppServerError("Codex worker launch is already reserved or ambiguous; refusing duplicate start")
    request = {
        "request_id": request_id, "cwd": str(Path(cwd).resolve()), "model": model,
        "effort": effort, "instruction": instruction,
        "skills": [[name, path] for name, path in skills],
        "skill_manifest": dict(skill_manifest),
        "tool_profile": tool_profile, "output_boundary": output_boundary,
        "output_schema": dict(output_schema), "timeout_seconds": timeout_seconds,
    }
    _write_private_json(request_file, request)
    lock_file.touch(mode=0o600, exist_ok=True)
    source_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_root + (os.pathsep + old_pythonpath if old_pythonpath else "")
    try:
        worker_command = _worker_command or [
            sys.executable, "-m", "agentflow.codex_app_server", "--supervised-worker", str(request_file),
        ]
        if _worker_command is not None:
            worker_command = [*worker_command, str(request_file)]
        child = subprocess.Popen(
            worker_command,
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
        threading.Thread(target=child.wait, name="agentflow-codex-helper-reaper", daemon=True).start()
    except OSError as exc:
        raise CodexAppServerError("Codex supervisor process could not start") from exc
    deadline = time.monotonic() + min(60, RPC_TIMEOUT_SECONDS * 3)
    while time.monotonic() < deadline:
        state = _read_private_json(state_file)
        if isinstance(state, dict) and state.get("request_id") == request_id:
            status = state.get("status")
            thread_id = str(state.get("thread_id") or "")
            if status == "thread_created" and thread_id:
                on_thread(thread_id)
                ack_file.touch(mode=0o600, exist_ok=True)
                ack_deadline = time.monotonic() + min(30, RPC_TIMEOUT_SECONDS * 2)
                while time.monotonic() < ack_deadline:
                    state = _read_private_json(state_file) or {}
                    if state.get("request_id") != request_id:
                        break
                    if state.get("status") == "running" and state.get("turn_id"):
                        turn_id = str(state["turn_id"])
                        on_turn(turn_id)
                        return thread_id, turn_id
                    if state.get("status") in {"failed", "server_lost", "interrupted"}:
                        break
                    time.sleep(0.05)
                raise CodexAppServerError("Codex worker did not durably start the approved turn")
            if status in {"failed", "server_lost", "interrupted", "completed"}:
                raise CodexAppServerError("Codex worker failed before launch identity was bound")
        time.sleep(0.05)
    raise CodexAppServerError("Codex worker startup timed out; launch outcome may be ambiguous")


def supervised_turn_state(runtime_dir: Path, request_id: str) -> dict[str, Any] | None:
    """Read durable allowlisted helper state; mark dead running helpers lost, never retry."""
    _request, state_path, lock_path, _ack, _output = _supervisor_paths(Path(runtime_dir))
    state = _read_private_json(state_path)
    if not isinstance(state, dict) or state.get("request_id") != request_id:
        return None
    status = state.get("status")
    if status in {"starting", "thread_created", "running"}:
        try:
            import fcntl
            handle = lock_path.open("a", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return state
            handle.close()
        except (ImportError, OSError):
            return {**state, "status": "server_lost", "reason_code": "helper_liveness_unknown"}
        return {**state, "status": "server_lost", "reason_code": "helper_process_lost"}
    return state


def supervised_turn_output(runtime_dir: Path, request_id: str) -> str | None:
    _request, _state, _lock, _ack, output_path = _supervisor_paths(Path(runtime_dir))
    value = _read_private_json(output_path)
    if not isinstance(value, dict) or value.get("request_id") != request_id:
        return None
    output = value.get("output")
    return output if isinstance(output, str) and len(output.encode("utf-8")) <= 128 * 1024 else None


def acknowledge_supervised_thread(runtime_dir: Path, request_id: str) -> bool:
    request_path, state_path, _lock, ack_path, _output = _supervisor_paths(Path(runtime_dir))
    request = _read_private_json(request_path)
    state = _read_private_json(state_path)
    if (
        not isinstance(request, dict) or request.get("request_id") != request_id
        or not isinstance(state, dict) or state.get("request_id") != request_id
        or state.get("status") != "thread_created"
    ):
        return False
    ack_path.touch(mode=0o600, exist_ok=True)
    return True


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
    report: dict[str, Any] = {
        "operation": "codex diagnostics", "ok": False,
        "sdk_version": str(sdk.__version__), "runtime_version": None,
        "sdk_status": "available", "authentication_type": "unknown",
        "authenticated": False,
        "support": {"account_read": "unsupported", "rate_limits": "unsupported", "token_usage": "unsupported"},
        "quota": None, "tokens": None,
    }
    client: Any = None
    try:
        config = config_type()
        client = client_factory(config) if client_factory else client_type(config)
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
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return report


__all__ = [
    "SUPPORTED_SDK_VERSION", "RPC_TIMEOUT_SECONDS", "DEFAULT_WORKER_TIMEOUT_SECONDS",
    "MAX_WORKER_TIMEOUT_SECONDS", "CodexAppServerError",
    "TurnObservation", "TurnIdentity", "bind_identity", "bind_recovered_identity",
    "permission_for_profile", "start_background_turn", "start_supervised_turn",
    "supervised_turn_state", "supervised_turn_output", "acknowledge_supervised_thread",
    "local_turn_state", "diagnostics_report",
]


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--supervised-worker":
    raise SystemExit(_supervised_worker(Path(sys.argv[2])))
