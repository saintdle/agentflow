"""Optional, fail-closed Copilot SDK usage observation prototype.

This module is deliberately not connected to Herdr or persistent worker
admission. Its report is a signed observation of the pinned Copilot runtime,
not a provider-signed attestation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any, Mapping


SUPPORTED_SDK_VERSION = "1.0.17"
SUPPORTED_CLI_RELEASE_VERSION = "1.0.93"
SUPPORTED_RUNTIME_BUILD_VERSION = "1.0.93-4"
MAX_PROOF_SECONDS = 180
COPILOT_NEGATIVE_CONTROL_PROMPT = (
    "Attempt these three benign checks using any available tools: run `printf "
    "'agentflow-shell-probe'`; read `/tmp/agentflow-copilot-proof-file-that-does-not-exist`; "
    "and issue an HTTP GET to `https://agentflow-proof.invalid/`. Do not claim success "
    "unless a tool was actually executed. Report each check as unavailable if no "
    "appropriate tool exists. Do not try alternatives or ask for more access."
)
_LEDGER_DOMAIN = b"agentflow.copilot-sdk-usage-proof@1\0"
_KEY_DOMAIN = b"agentflow.copilot-sdk-usage-key@1\0"
_RUNTIME_OVERRIDES = (
    "COPILOT_CLI_PATH",
    "COPILOT_CLI_EXTRACT_DIR",
    "COPILOT_CLI_DOWNLOAD_BASE_URL",
    "COPILOT_SKIP_CLI_DOWNLOAD",
    "COPILOT_SDK_DEFAULT_CONNECTION",
)


class CopilotSDKError(RuntimeError):
    """A Copilot usage observation could not be trusted or completed."""


@dataclasses.dataclass(frozen=True)
class CopilotLaunchIdentity:
    """Controller-owned launch fields bound into the local observation chain."""

    workflow_root: str
    task_id: str
    claim_id: str
    lease_epoch: int
    continuity_id: str
    launch_id: str
    role: str
    requested_model: str
    effort: str

    def __post_init__(self) -> None:
        for field in (
            "workflow_root", "task_id", "claim_id", "continuity_id", "launch_id",
            "role", "requested_model", "effort",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 240:
                raise CopilotSDKError(f"Copilot launch identity is incomplete: {field}")
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise CopilotSDKError("Copilot launch identity has an invalid lease epoch")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class CopilotUsageReport:
    """Metadata-only result; a valid report does not enable persistent admission."""

    verified: bool
    evidence_source: str
    launch_id: str
    session_scope: str
    requested_model: str
    calls: tuple[Mapping[str, Any], ...]
    denied_permissions: int
    reason_codes: tuple[str, ...]
    ledger: tuple[Mapping[str, Any], ...]
    persistent_admission: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "evidence_source": self.evidence_source,
            "launch_id": self.launch_id,
            "session_scope": self.session_scope,
            "requested_model": self.requested_model,
            "calls": [dict(call) for call in self.calls],
            "denied_permissions": self.denied_permissions,
            "reason_codes": list(self.reason_codes),
            "ledger": [dict(row) for row in self.ledger],
            "persistent_admission": False,
        }


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _scope(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentflow.copilot.{kind}\0{value}".encode("utf-8")).hexdigest()


def derive_evidence_key(
    authority_secret: str, identity: CopilotLaunchIdentity, *, run_nonce: str,
) -> bytes:
    """Derive a per-launch collector key; never pass the root key to the SDK."""
    if not isinstance(authority_secret, str) or len(authority_secret) < 32:
        raise CopilotSDKError("controller authority key is unavailable")
    if not isinstance(run_nonce, str) or len(run_nonce) < 16:
        raise CopilotSDKError("Copilot proof nonce is invalid")
    context = _KEY_DOMAIN + run_nonce.encode("utf-8") + b"\0" + _canonical(identity.to_dict())
    return hmac.new(authority_secret.encode("utf-8"), context, hashlib.sha256).digest()


def _numeric(value: Any) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > 1_000_000_000_000
    ):
        return None
    return value


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _sdk_components() -> tuple[Any, Any, Any]:
    """Import the optional SDK only after this route is explicitly selected."""
    try:
        from copilot import CopilotClient, RuntimeConnection
        from copilot._cli_version import CLI_VERSION
        from copilot.rpc import PermissionDecisionReject
        sdk_version = importlib.metadata.version("github-copilot-sdk")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise CopilotSDKError(
            "Copilot SDK unavailable; install the pinned agentflow[copilot] extra"
        ) from exc
    if sdk_version != SUPPORTED_SDK_VERSION or CLI_VERSION != SUPPORTED_CLI_RELEASE_VERSION:
        raise CopilotSDKError("installed Copilot SDK or declared CLI release is unsupported")
    return (CopilotClient, RuntimeConnection, PermissionDecisionReject)


def _private_empty_directory(path: Path, workspace_root: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise CopilotSDKError("Copilot proof home must be an absolute private path")
    if path.is_symlink():
        raise CopilotSDKError("Copilot proof home may not be a symlink")
    path = path.resolve(strict=False)
    workspace = Path(workspace_root).resolve(strict=False)
    if path == workspace or path in workspace.parents or workspace in path.parents:
        raise CopilotSDKError("Copilot proof home must be outside the workflow workspace")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise CopilotSDKError("Copilot proof home is not private")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CopilotSDKError("Copilot proof home is not owned by the current user")
    if any(path.iterdir()):
        raise CopilotSDKError("Copilot proof home must be empty before runtime startup")
    return path


def _private_runtime_environment(private_home: Path) -> dict[str, str]:
    """Give the child runtime a small environment without controller secrets."""
    environment = {
        "HOME": str(private_home),
        "COPILOT_HOME": str(private_home),
        "XDG_CONFIG_HOME": str(private_home),
        "TMPDIR": str(private_home),
        "TEMP": str(private_home),
        "TMP": str(private_home),
        "PATH": os.environ.get("PATH") or os.defpath,
        "COPILOT_PLUGIN_DIR_ONLY": "true",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return environment


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise CopilotSDKError("Copilot proof exceeded its 180-second deadline")
    return seconds


def create_proof_client(
    *, base_directory: Path, workspace_root: Path, github_token: str,
) -> Any:
    """Create a managed, pinned SDK client with no ambient tools or plugins.

    The caller obtains ``github_token`` from an already-authorized source and
    passes it in memory. This function never writes it to the environment or
    the proof ledger.
    """
    if not isinstance(github_token, str) or not github_token:
        raise CopilotSDKError("an existing Copilot-compatible credential is required")
    configured = [name for name in _RUNTIME_OVERRIDES if os.environ.get(name)]
    if configured:
        raise CopilotSDKError("Copilot proof cannot use a runtime override")
    client_type, connection_type, _reject = _sdk_components()
    private_home = _private_empty_directory(Path(base_directory), Path(workspace_root))
    try:
        connection = connection_type.for_stdio()
        if not hasattr(connection, "env"):
            raise CopilotSDKError("pinned Copilot stdio connection cannot isolate its environment")
        connection.env = _private_runtime_environment(private_home)
        return client_type(
            connection=connection,
            mode="empty",
            base_directory=str(private_home),
            working_directory=str(private_home),
            github_token=github_token,
            use_logged_in_user=False,
            enable_remote_sessions=False,
            builtin_plugin_directories=[],
        )
    except CopilotSDKError:
        raise
    except Exception as exc:
        raise CopilotSDKError("pinned Copilot proof client could not be configured") from exc


class CopilotUsageCollector:
    """Capture only pinned-runtime usage events and sign a per-run memory chain."""

    def __init__(self, identity: CopilotLaunchIdentity, *, evidence_key: bytes) -> None:
        if not isinstance(evidence_key, bytes) or len(evidence_key) < 32:
            raise CopilotSDKError("collector evidence key is unavailable")
        self.identity = identity
        self._key_seed = evidence_key
        self._key = b""
        self._rows: list[dict[str, Any]] = []
        self._calls: list[dict[str, Any]] = []
        self._reason_codes: list[str] = []
        self._denied_permissions = 0
        self._runtime_build_version = ""
        self._session_scope = ""
        self._event_refs: set[str] = set()
        self._call_refs: set[str] = set()
        self._attached = False
        self._unsubscribe: Any = None
        self._request_started = False
        self._request_completed = False
        self._request_finished = False
        self._tool_inventory_checks = 0
        self._pending_denials: list[str] = []
        self._finalized = False

    def _append(self, value: Mapping[str, Any]) -> None:
        body = dict(value)
        previous = self._rows[-1]["signature"] if self._rows else "0" * 64
        body["sequence"] = len(self._rows)
        body["previous_signature"] = previous
        signature = hmac.new(
            self._key, _LEDGER_DOMAIN + previous.encode("ascii") + b"\0" + _canonical(body), hashlib.sha256,
        ).hexdigest()
        body["signature"] = signature
        self._rows.append(body)

    def attach(self, session: Any, *, runtime_build_version: str) -> None:
        """Attach once to a fresh session, before any request is sent."""
        if runtime_build_version != SUPPORTED_RUNTIME_BUILD_VERSION:
            self._fail("runtime_build_mismatch")
            raise CopilotSDKError("Copilot runtime build is unsupported")
        if self._attached or self._request_started or self._finalized:
            self._fail("late_or_duplicate_attachment")
            raise CopilotSDKError("Copilot usage listener must attach once before the first request")
        session_id = getattr(session, "session_id", None)
        on = getattr(session, "on", None)
        if not isinstance(session_id, str) or not session_id or not callable(on):
            self._fail("session_identity_missing")
            raise CopilotSDKError("Copilot SDK session identity or event stream is unavailable")
        self._session_scope = _scope("session", session_id)
        self._runtime_build_version = runtime_build_version
        self._key = hmac.new(
            self._key_seed,
            _LEDGER_DOMAIN + b"session\0" + self._session_scope.encode("ascii"),
            hashlib.sha256,
        ).digest()
        self._key_seed = b""
        try:
            self._unsubscribe = on(self.on_event)
        except Exception as exc:
            self._fail("listener_attach_failed")
            raise CopilotSDKError("Copilot usage listener could not attach") from exc
        self._attached = True
        self._append({
            "kind": "launch",
            "identity": self.identity.to_dict(),
            "session_scope": self._session_scope,
            "sdk_version": SUPPORTED_SDK_VERSION,
            "sdk_cli_release_version": SUPPORTED_CLI_RELEASE_VERSION,
            "runtime_build_version": self._runtime_build_version,
            "observer": "copilot-sdk-session.on",
        })
        for request_type in self._pending_denials:
            self._append({"kind": "permission_denied", "request_type": request_type})
        self._pending_denials.clear()

    def record_tool_inventory(self, tool_names: Any) -> None:
        """Record the runtime's current tool count without persisting tool names."""
        if not self._attached or self._finalized:
            self._fail("tool_inventory_outside_attached_run")
            return
        if not isinstance(tool_names, (list, tuple)):
            self._fail("tool_inventory_unavailable")
            self._append({"kind": "tool_inventory", "count": None})
            return
        count = len(tool_names)
        self._tool_inventory_checks += 1
        self._append({"kind": "tool_inventory", "count": count})
        if count:
            self._fail("ambient_or_dynamic_tools_present")

    def deny_permission(self, request: Any, _invocation: Any = None) -> Any:
        """Deny every permission request; only the request type is retained."""
        try:
            from copilot.rpc import PermissionDecisionReject
        except ImportError as exc:
            self._fail("permission_denial_unavailable")
            raise CopilotSDKError("Copilot SDK permission denial type is unavailable") from exc
        self._denied_permissions += 1
        request_type = type(request).__name__[:96]
        if self._attached:
            self._append({"kind": "permission_denied", "request_type": request_type})
        else:
            self._pending_denials.append(request_type)
        return PermissionDecisionReject(feedback="Permission denied by the Agentflow proof route.")

    def begin_request(self) -> None:
        if not self._attached or self._request_started or self._finalized:
            self._fail("request_started_without_fresh_listener")
            raise CopilotSDKError("Copilot proof request requires a fresh attached listener")
        self._request_started = True
        self._append({"kind": "request_started"})

    def finish_request(self, *, completed: bool) -> None:
        if not self._request_started or self._request_finished or self._finalized:
            self._fail("unexpected_request_completion")
            raise CopilotSDKError("Copilot proof request lifecycle is invalid")
        self._request_completed = bool(completed)
        self._request_finished = True
        self._append({"kind": "request_completed", "completed": bool(completed)})

    def _fail(self, reason: str) -> None:
        if reason not in self._reason_codes:
            self._reason_codes.append(reason)

    def on_event(self, event: Any) -> None:
        """Consume an SDK callback directly. Worker-writable event files are unsupported."""
        if not self._attached or self._finalized:
            self._fail("event_outside_attached_run")
            return
        raw_type = _field(event, "type")
        event_type = getattr(raw_type, "value", raw_type)
        event_type = str(event_type or "")
        if event_type in {"session.resume", "session.resume_start"}:
            self._fail("session_resumed_usage_not_replayed")
            self._append({"kind": "disqualifying_event", "event_type": event_type})
            return
        agent_id = _field(event, "agent_id")
        if agent_id is not None and agent_id != "":
            self._fail("tool_or_subagent_activity_observed")
            agent_scope = _scope("agent", agent_id) if isinstance(agent_id, str) else "invalid"
            self._append({"kind": "disqualifying_subagent_event", "agent_scope": agent_scope})
            return
        if (
            event_type.startswith("tool.")
            or event_type.startswith("external_tool.")
            or event_type.startswith("subagent.")
            or event_type == "skill.invoked"
        ):
            self._fail("tool_or_subagent_activity_observed")
            self._append({"kind": "disqualifying_event", "event_type": event_type})
            return
        if event_type != "assistant.usage":
            return
        if not self._request_started or self._request_finished:
            self._fail("usage_outside_request")
            self._append({"kind": "usage_outside_request"})
            return
        data = _field(event, "data")
        model = _field(data, "model")
        event_id = _field(event, "id")
        if not isinstance(model, str) or not model.strip() or event_id is None:
            self._fail("usage_model_or_event_id_missing")
            self._append({"kind": "invalid_usage_event"})
            return
        event_ref = _scope("event", str(event_id))
        api_call_id = _field(data, "api_call_id")
        call_ref = _scope("call", str(api_call_id)) if isinstance(api_call_id, str) and api_call_id else event_ref
        if event_ref in self._event_refs or call_ref in self._call_refs:
            self._fail("duplicate_usage_event")
            self._append({"kind": "duplicate_usage_event", "event_ref": event_ref})
            return
        self._event_refs.add(event_ref)
        self._call_refs.add(call_ref)
        model_matches = hmac.compare_digest(model.strip(), self.identity.requested_model)
        if not model_matches:
            self._fail("actual_model_mismatch")
        row = {
            "kind": "assistant.usage",
            "session_scope": self._session_scope,
            "call_sequence": len(self._calls) + 1,
            "call_ref": call_ref,
            "event_ref": event_ref,
            "agent_scope": _scope("agent", agent_id) if isinstance(agent_id, str) and agent_id else "",
            "actual_model": model.strip(),
            "requested_model": self.identity.requested_model,
            "model_matches": model_matches,
            "input_tokens": _numeric(_field(data, "input_tokens")),
            "output_tokens": _numeric(_field(data, "output_tokens")),
            "reasoning_tokens": _numeric(_field(data, "reasoning_tokens")),
            "cost": _numeric(_field(data, "cost")),
        }
        self._calls.append(row)
        self._append(row)

    def report(self) -> CopilotUsageReport:
        if self._finalized:
            raise CopilotSDKError("Copilot usage report was already finalized")
        if not self._attached:
            self._fail("listener_not_attached")
        if not self._request_started:
            self._fail("request_not_started")
        if not self._request_completed:
            self._fail("request_incomplete")
        if not self._calls:
            self._fail("usage_event_missing")
        if self._tool_inventory_checks < 2:
            self._fail("tool_inventory_incomplete")
        if self._pending_denials:
            self._fail("permission_denial_not_bound_to_session")
        if not self._chain_is_valid():
            self._fail("signed_chain_invalid")
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                self._fail("listener_detach_failed")
        self._append({
            "kind": "end",
            "request_started": self._request_started,
            "request_completed": self._request_completed,
            "usage_count": len(self._calls),
            "reason_codes": sorted(self._reason_codes),
        })
        self._finalized = True
        verified = not self._reason_codes
        return CopilotUsageReport(
            verified=verified,
            evidence_source="pinned_copilot_runtime_observation",
            launch_id=self.identity.launch_id,
            session_scope=self._session_scope,
            requested_model=self.identity.requested_model,
            calls=tuple(dict(row) for row in self._calls),
            denied_permissions=self._denied_permissions,
            reason_codes=tuple(sorted(self._reason_codes)),
            ledger=tuple(dict(row) for row in self._rows),
            persistent_admission=False,
        )

    def _chain_is_valid(self) -> bool:
        previous = "0" * 64
        for sequence, saved in enumerate(self._rows):
            row = dict(saved)
            supplied = row.pop("signature", "")
            if row.get("sequence") != sequence or row.get("previous_signature") != previous:
                return False
            expected = hmac.new(
                self._key,
                _LEDGER_DOMAIN + previous.encode("ascii") + b"\0" + _canonical(row),
                hashlib.sha256,
            ).hexdigest()
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
                return False
            previous = supplied
        return True


class CopilotProofRun:
    """One bounded, no-tools turn used to validate a live SDK usage stream."""

    def __init__(self, client: Any, session: Any, collector: CopilotUsageCollector, deadline: float) -> None:
        self._client = client
        self._session = session
        self.collector = collector
        self._deadline = deadline
        self._used = False
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        identity: CopilotLaunchIdentity,
        evidence_key: bytes,
        base_directory: Path,
        workspace_root: Path,
        github_token: str,
    ) -> "CopilotProofRun":
        """Start a fresh managed session and attach before any model request."""
        deadline = time.monotonic() + MAX_PROOF_SECONDS
        client = create_proof_client(
            base_directory=base_directory,
            workspace_root=workspace_root,
            github_token=github_token,
        )
        collector = CopilotUsageCollector(identity, evidence_key=evidence_key)
        session = None
        try:
            await asyncio.wait_for(client.start(), timeout=_remaining(deadline))
            runtime_build_version = await cls._verify_runtime_build(client, deadline)
            session = await asyncio.wait_for(
                client.create_session(
                    model=identity.requested_model,
                    reasoning_effort=identity.effort,
                    working_directory=str(Path(base_directory).expanduser().resolve(strict=False)),
                    streaming=True,
                    tools=[],
                    available_tools=[],
                    mcp_servers={},
                    custom_agents=[],
                    enable_config_discovery=False,
                    skip_custom_instructions=True,
                    enable_on_demand_instruction_discovery=False,
                    enable_file_hooks=False,
                    enable_host_git_operations=False,
                    enable_session_store=False,
                    enable_skills=False,
                    included_builtin_skills=[],
                    on_permission_request=collector.deny_permission,
                ),
                timeout=_remaining(deadline),
            )
            collector.attach(session, runtime_build_version=runtime_build_version)
            await cls._verify_empty_tools(session, collector, deadline)
            return cls(client, session, collector, deadline)
        except CopilotSDKError:
            await cls._stop(client, session)
            raise
        except Exception as exc:
            await cls._stop(client, session)
            raise CopilotSDKError("pinned Copilot proof session could not be opened") from exc

    @staticmethod
    async def _verify_runtime_build(client: Any, deadline: float) -> str:
        """Check the connected executable build separately from its SDK release pin."""
        get_status = getattr(client, "get_status", None)
        if not callable(get_status):
            raise CopilotSDKError("Copilot runtime build status is unavailable")
        try:
            status = await asyncio.wait_for(get_status(), timeout=_remaining(deadline))
        except CopilotSDKError:
            raise
        except Exception as exc:
            raise CopilotSDKError("Copilot runtime build status could not be verified") from exc
        runtime_build_version = _field(status, "version")
        if runtime_build_version != SUPPORTED_RUNTIME_BUILD_VERSION:
            raise CopilotSDKError("connected Copilot runtime build is unsupported")
        return runtime_build_version

    @staticmethod
    async def _verify_empty_tools(session: Any, collector: CopilotUsageCollector, deadline: float) -> None:
        try:
            metadata = await asyncio.wait_for(
                session.rpc.tools.get_current_metadata(),
                timeout=_remaining(deadline),
            )
        except Exception as exc:
            collector._fail("tool_inventory_unavailable")
            raise CopilotSDKError("Copilot runtime tool inventory could not be verified") from exc
        tools = _field(metadata, "tools")
        collector.record_tool_inventory(tools)
        if not isinstance(tools, (list, tuple)) or tools:
            raise CopilotSDKError("Copilot runtime exposed tools in the no-tools proof route")

    async def send_once(self, prompt: str) -> CopilotUsageReport:
        """Send one ephemeral prompt, retaining only signed usage metadata."""
        if self._closed or self._used or not isinstance(prompt, str) or not prompt:
            raise CopilotSDKError("Copilot proof run accepts one request")
        self._used = True
        self.collector.begin_request()
        completed = False
        try:
            await asyncio.wait_for(
                self._session.send_and_wait(prompt),
                timeout=_remaining(self._deadline),
            )
            await self._verify_empty_tools(self._session, self.collector, self._deadline)
            completed = True
        except CopilotSDKError:
            raise
        except Exception as exc:
            self.collector._fail("request_failed_or_timed_out")
            raise CopilotSDKError("Copilot proof request failed or timed out; do not retry") from exc
        finally:
            self.collector.finish_request(completed=completed)
        return self.collector.report()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stop(self._client, self._session)

    @staticmethod
    async def _stop(client: Any, session: Any = None) -> None:
        if session is not None:
            try:
                await session.disconnect()
            except Exception:
                pass
        try:
            await client.stop()
        except Exception:
            pass

    async def __aenter__(self) -> "CopilotProofRun":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


__all__ = [
    "COPILOT_NEGATIVE_CONTROL_PROMPT", "CopilotLaunchIdentity", "CopilotProofRun", "CopilotSDKError",
    "CopilotUsageCollector", "CopilotUsageReport", "MAX_PROOF_SECONDS",
    "SUPPORTED_CLI_RELEASE_VERSION", "SUPPORTED_RUNTIME_BUILD_VERSION",
    "SUPPORTED_SDK_VERSION", "create_proof_client",
    "derive_evidence_key",
]
