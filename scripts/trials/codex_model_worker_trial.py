"""Source-only, synthetic Codex model-worker trial rehearsal.

This module intentionally has no Agentflow, Codex SDK, subprocess, network, or
provider imports. It is a bounded fake-client state-machine fixture, not a
worker launcher, scheduler, SDK probe, or evidence for real model isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


PROFILE_ID = "synthetic-codex-readonly"
MODEL = "gpt-6-luna"
EFFORT = "medium"
SYNTHETIC_OWNER_ID = "synthetic-controller-owner"
SYNTHETIC_CONTINUITY_ID = "synthetic-continuity-001"
MAX_TURN_STARTS = 2
MAX_PREFLIGHT_ATTEMPTS = 2
MAX_PROFILE_WAIT_MS = 30_000
WARNING_TOKENS_PER_TURN = 20_000
MAX_CUMULATIVE_REPORTED_TOKENS = 40_000
WORKER_TIMEOUT_SECONDS = 180
BARRIER_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 10
OWNED_PROCESS_CLEANUP_SECONDS = 5
TOTAL_TRIAL_SECONDS = 600
MAX_PROMPT_BYTES = 2 * 1024
MAX_WORKSPACE_BYTES = 8 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024
MAX_STATE_BYTES = 64 * 1024

_FIXTURE_TEXT = "Synthetic fixture only. Marker: lime-otter-73.\n"
_INSTRUCTIONS_TEXT = (
    "Read the synthetic fixture and report its marker. Do not access other data.\n"
)


@dataclass(frozen=True)
class InventoryEntry:
    name: str
    sha256: str
    size_bytes: int


def _inventory_entry(name: str, data: str) -> InventoryEntry:
    encoded = data.encode("utf-8")
    return InventoryEntry(name, hashlib.sha256(encoded).hexdigest(), len(encoded))


SYNTHETIC_OUTBOUND_INVENTORY = (
    _inventory_entry("synthetic-instructions.txt", _INSTRUCTIONS_TEXT),
    _inventory_entry("synthetic-fixture.txt", _FIXTURE_TEXT),
)


class TrialHalt(RuntimeError):
    """A synthetic admission or recovery condition failed closed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class UsageCase(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    LIMIT = "limit"
    UNKNOWN = "unknown"
    UNATTRIBUTABLE = "unattributable"


_USAGE_CASES: Mapping[UsageCase, dict[str, Any]] = {
    UsageCase.NORMAL: {"input_tokens": 120, "output_tokens": 30, "attributable": True},
    UsageCase.WARNING: {
        "input_tokens": WARNING_TOKENS_PER_TURN,
        "output_tokens": 0,
        "attributable": True,
    },
    UsageCase.LIMIT: {
        "input_tokens": MAX_CUMULATIVE_REPORTED_TOKENS,
        "output_tokens": 0,
        "attributable": True,
    },
    UsageCase.UNKNOWN: {"input_tokens": None, "output_tokens": None, "attributable": None},
    UsageCase.UNATTRIBUTABLE: {
        "input_tokens": 120,
        "output_tokens": 30,
        "attributable": False,
    },
}


@dataclass(frozen=True)
class ProfileEvidence:
    """Finite synthetic evidence shape; catalogues and legacy fields are not proof."""

    event: str
    source: str
    active_profile: str | None
    elapsed_ms: int
    before_first_turn: bool
    legacy_sandbox_present: bool = False
    tool_surfaces: tuple[str, ...] = ("shell",)


def active_profile_evidence() -> ProfileEvidence:
    """Return the one passing fake observation used by the fixed demo."""

    return ProfileEvidence(
        event="settings_updated",
        source="typed_active_permission_profile",
        active_profile=PROFILE_ID,
        elapsed_ms=1,
        before_first_turn=True,
    )


@dataclass(frozen=True)
class RestartEvidence:
    owner_id: str
    claim_id: str
    request_id: str
    continuity_id: str
    thread_id: str
    turn_id: str
    lease_epoch: int
    lease_token: str
    helper_state: str = "running"
    server_state: str = "running"


def _validate_profile_evidence(value: object) -> None:
    if not isinstance(value, ProfileEvidence):
        raise TrialHalt("profile_evidence_unsupported")
    if (
        value.event != "settings_updated"
        or value.source != "typed_active_permission_profile"
        or value.active_profile != PROFILE_ID
        or isinstance(value.elapsed_ms, bool)
        or not isinstance(value.elapsed_ms, int)
        or value.elapsed_ms < 0
        or value.elapsed_ms > MAX_PROFILE_WAIT_MS
        or value.before_first_turn is not True
        or value.legacy_sandbox_present is not False
        or value.tool_surfaces != ("shell",)
    ):
        raise TrialHalt("active_profile_proof_failed")


_RETRYABLE_PREFLIGHT_FAILURES = frozenset({
    "profile_evidence_unsupported", "active_profile_proof_failed",
})


class _FakeClient:
    """Internal deterministic fake. It accepts no client/runtime injection."""

    def __init__(self, state_path: Path):
        self._state_path = state_path

    def thread_start(self, request_id: str) -> str:
        state = _read_state(self._state_path)
        request = _request_by_id(state, request_id)
        if request["thread_start_state"] != "reserved":
            raise TrialHalt("thread_start_reservation_missing")
        return str(request["expected_thread_id"])

    def turn_start(self, request_id: str, *, ambiguous: bool) -> str | None:
        state = _read_state(self._state_path)
        request = _request_by_id(state, request_id)
        if request["turn_start_state"] != "reserved":
            raise TrialHalt("turn_start_reservation_missing")
        if ambiguous:
            return None
        return str(request["expected_turn_id"])


class TrialHarness:
    """One fixed two-slot fake trial ledger, never a general task scheduler."""

    def __init__(self, state_path: str | os.PathLike[str]):
        self.state_path = Path(state_path)
        if self.state_path.exists():
            self.state = _read_state(self.state_path)
        else:
            self.state = _new_state()
            self._save()

    def _save(self) -> None:
        _save_state(self.state_path, self.state)

    def preflight(self, evidence: object) -> None:
        self._refresh()
        if self.state["halt_code"] and self.state["halt_code"] not in _RETRYABLE_PREFLIGHT_FAILURES:
            raise TrialHalt(str(self.state["halt_code"]))
        if self.state["preflight_attempts"] >= MAX_PREFLIGHT_ATTEMPTS:
            self._halt("preflight_attempt_budget_exhausted")
        self.state["preflight_attempts"] += 1
        self.state["profile_ready"] = False
        self._save()
        try:
            _validate_profile_evidence(evidence)
        except TrialHalt as exc:
            self.state["halt_code"] = exc.code
            self._save()
            raise
        self.state["profile_ready"] = True
        self.state["halt_code"] = ""
        self._save()

    def start_fake_worker(self) -> dict[str, str]:
        return self._start_fake_worker(ambiguous=False)

    def simulate_ambiguous_turn_start(self) -> None:
        self._start_fake_worker(ambiguous=True)

    def _start_fake_worker(self, *, ambiguous: bool) -> dict[str, str]:
        self._refresh()
        if self.state["halt_code"]:
            raise TrialHalt(str(self.state["halt_code"]))
        if not self.state["profile_ready"]:
            self._halt("active_profile_proof_required")
        if self.state["attempted_turn_starts"] >= MAX_TURN_STARTS:
            self._halt("turn_start_budget_exhausted")
        if not self._usage_allows_admission():
            self._halt("usage_admission_halted")
        if self.state["requests"]:
            previous = self.state["requests"][-1]
            if previous["result_state"] != "consumed":
                self._halt("previous_result_not_consumed")
        if len(self.state["requests"]) >= MAX_TURN_STARTS:
            self._halt("turn_start_budget_exhausted")

        slot = len(self.state["requests"]) + 1
        request_id = f"synthetic-request-{slot:03d}"
        request = {
            "owner_id": SYNTHETIC_OWNER_ID,
            "claim_id": f"synthetic-claim-{slot:03d}",
            "request_id": request_id,
            "continuity_id": self.state["continuity_id"],
            "lease_epoch": self.state["lease_epoch"],
            "lease_token": self.state["lease_token"],
            "expected_thread_id": f"synthetic-thread-{slot:03d}",
            "thread_id": "",
            "thread_start_state": "not_started",
            "expected_turn_id": f"synthetic-turn-{slot:03d}",
            "turn_id": "",
            "turn_start_state": "not_started",
            "worker_state": "reserved",
            "result_state": "pending",
            "usage": {"input_tokens": None, "output_tokens": None, "attributable": None},
        }
        self.state["requests"].append(request)

        # Reservation and attempt accounting are durable before each fake call.
        self.state["attempted_thread_starts"] += 1
        self.state["simulated_thread_start_calls"] += 1
        request["thread_start_state"] = "reserved"
        self._save()
        client = _FakeClient(self.state_path)
        try:
            thread_id = client.thread_start(request_id)
        except Exception:
            self._halt("simulated_thread_start_failed")
        if thread_id != request["expected_thread_id"]:
            self._halt("simulated_thread_identity_mismatch")
        request["thread_id"] = thread_id
        request["thread_start_state"] = "completed"

        self.state["attempted_turn_starts"] += 1
        self.state["simulated_turn_start_calls"] += 1
        request["turn_start_state"] = "reserved"
        self._save()
        try:
            turn_id = client.turn_start(request_id, ambiguous=ambiguous)
        except Exception:
            request["turn_start_state"] = "ambiguous"
            request["worker_state"] = "ambiguous"
            self._halt("turn_start_outcome_ambiguous")
        self.state["profile_ready"] = False
        if turn_id is None:
            request["turn_start_state"] = "ambiguous"
            request["worker_state"] = "ambiguous"
            self._halt("turn_start_outcome_ambiguous")
        if turn_id != request["expected_turn_id"]:
            self._halt("turn_identity_mismatch")
        request["turn_id"] = turn_id
        request["turn_start_state"] = "completed"
        request["worker_state"] = "running"
        self.state["halt_code"] = ""
        self._save()
        return self._identity(request)

    def restart_evidence(self) -> RestartEvidence:
        self._refresh()
        request = self._current_request()
        return RestartEvidence(
            owner_id=str(request["owner_id"]),
            claim_id=str(request["claim_id"]),
            request_id=str(request["request_id"]),
            continuity_id=str(request["continuity_id"]),
            thread_id=str(request["thread_id"]),
            turn_id=str(request["turn_id"]),
            lease_epoch=int(self.state["lease_epoch"]),
            lease_token=str(self.state["lease_token"]),
        )

    def resume_after_controller_restart(self, evidence: object) -> dict[str, str]:
        self._refresh()
        if self.state["halt_code"]:
            raise TrialHalt(str(self.state["halt_code"]))
        if not isinstance(evidence, RestartEvidence):
            self._halt("restart_evidence_unsupported")
        request = self._current_request()
        if request["turn_start_state"] == "reserved":
            request["turn_start_state"] = "ambiguous"
            request["worker_state"] = "ambiguous"
            self._halt("turn_start_outcome_ambiguous")
        expected = self.restart_evidence()
        if (
            request["worker_state"] != "running"
            or evidence.helper_state != "running"
            or evidence.server_state != "running"
            or evidence != replace(
                expected,
                helper_state=evidence.helper_state,
                server_state=evidence.server_state,
            )
            or self.state["controller_restart_count"] >= 1
        ):
            self._halt("restart_continuity_unproven")
        self.state["lease_epoch"] += 1
        self.state["lease_token"] = f"synthetic-lease-epoch-{self.state['lease_epoch']}"
        self.state["controller_restart_count"] += 1
        self._save()
        return self._identity(request)

    def complete_fake_turn(self, usage_case: UsageCase = UsageCase.NORMAL) -> None:
        self._refresh()
        if self.state["halt_code"]:
            raise TrialHalt(str(self.state["halt_code"]))
        if not isinstance(usage_case, UsageCase):
            self._halt("usage_evidence_unsupported")
        request = self._current_request()
        if request["worker_state"] != "running":
            self._halt("fake_turn_not_running")
        request["usage"] = dict(_USAGE_CASES[usage_case])
        request["worker_state"] = "completed"
        request["result_state"] = "ready"
        self._save()

    def consume_fake_result(self) -> dict[str, Any]:
        self._refresh()
        if self.state["halt_code"]:
            raise TrialHalt(str(self.state["halt_code"]))
        request = self._current_request()
        if request["worker_state"] != "completed" or request["result_state"] != "ready":
            self._halt("fake_result_unavailable")
        result = {
            "simulation_only": True,
            "status": "completed",
            "request_id": request["request_id"],
            "thread_id": request["thread_id"],
            "turn_id": request["turn_id"],
            "fixture_sha256": SYNTHETIC_OUTBOUND_INVENTORY[1].sha256,
            "usage_status": _usage_status(request["usage"]),
        }
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_BYTES:
            self._halt("sanitized_result_size_exceeded")
        request["result_state"] = "consumed"
        self.state["consumption_count"] += 1
        self._save()
        return result

    def report(self) -> dict[str, Any]:
        self._refresh()
        return {
            "simulation_only": True,
            "profile_ready": bool(self.state["profile_ready"]),
            "preflight_attempts": self.state["preflight_attempts"],
            "attempted_thread_starts": self.state["attempted_thread_starts"],
            "attempted_turn_starts": self.state["attempted_turn_starts"],
            "simulated_thread_start_calls": self.state["simulated_thread_start_calls"],
            "simulated_turn_start_calls": self.state["simulated_turn_start_calls"],
            "controller_restart_count": self.state["controller_restart_count"],
            "consumption_count": self.state["consumption_count"],
            "requests": [
                {
                    key: request[key]
                    for key in (
                        "claim_id", "request_id", "continuity_id", "lease_epoch",
                        "thread_id", "turn_id", "thread_start_state",
                        "turn_start_state", "worker_state", "result_state", "usage",
                    )
                }
                for request in self.state["requests"]
            ],
            "halt_code": self.state["halt_code"],
            "limits": {
                "max_turn_starts": MAX_TURN_STARTS,
                "max_preflight_attempts": MAX_PREFLIGHT_ATTEMPTS,
                "profile_wait_ms": MAX_PROFILE_WAIT_MS,
                "worker_timeout_seconds": WORKER_TIMEOUT_SECONDS,
                "barrier_timeout_seconds": BARRIER_TIMEOUT_SECONDS,
                "rpc_timeout_seconds": RPC_TIMEOUT_SECONDS,
                "owned_process_cleanup_seconds": OWNED_PROCESS_CLEANUP_SECONDS,
                "total_trial_seconds": TOTAL_TRIAL_SECONDS,
                "prompt_bytes": MAX_PROMPT_BYTES,
                "workspace_bytes": MAX_WORKSPACE_BYTES,
                "sanitized_evidence_bytes": MAX_EVIDENCE_BYTES,
                "warning_tokens_per_turn": WARNING_TOKENS_PER_TURN,
                "cumulative_reported_tokens": MAX_CUMULATIVE_REPORTED_TOKENS,
                "hard_spend_cap": False,
                "cancellation_best_effort": True,
            },
        }

    def _usage_allows_admission(self) -> bool:
        if not self.state["requests"]:
            return True
        request = self.state["requests"][-1]
        usage = request["usage"]
        if usage["attributable"] is not True:
            return False
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if (
            isinstance(input_tokens, bool)
            or isinstance(output_tokens, bool)
            or not isinstance(input_tokens, int)
            or not isinstance(output_tokens, int)
            or input_tokens < 0
            or output_tokens < 0
        ):
            return False
        prior_total = sum(
            int(item["usage"]["input_tokens"]) + int(item["usage"]["output_tokens"])
            for item in self.state["requests"]
            if item["usage"]["attributable"] is True
            and isinstance(item["usage"]["input_tokens"], int)
            and not isinstance(item["usage"]["input_tokens"], bool)
            and isinstance(item["usage"]["output_tokens"], int)
            and not isinstance(item["usage"]["output_tokens"], bool)
        )
        return prior_total < MAX_CUMULATIVE_REPORTED_TOKENS

    def _current_request(self) -> dict[str, Any]:
        if not self.state["requests"]:
            self._halt("no_reserved_request")
        return self.state["requests"][-1]

    def _identity(self, request: Mapping[str, Any]) -> dict[str, str]:
        return {
            key: str(request[key])
            for key in ("owner_id", "claim_id", "request_id", "continuity_id", "thread_id", "turn_id")
        }

    def _halt(self, code: str) -> None:
        self.state["halt_code"] = code
        self.state["profile_ready"] = False
        self._save()
        raise TrialHalt(code)

    def _refresh(self) -> None:
        self.state = _read_state(self.state_path)


def _usage_status(value: Mapping[str, Any]) -> str:
    if value.get("attributable") is not True:
        return "unavailable_or_unattributable"
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return "unavailable_or_unattributable"
    total = input_tokens + output_tokens
    if total >= MAX_CUMULATIVE_REPORTED_TOKENS:
        return "admission_threshold_reached"
    if total >= WARNING_TOKENS_PER_TURN:
        return "warning_threshold_reached"
    return "reported_and_attributed"


def _request_by_id(state: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    matches = [
        value for value in state["requests"]
        if isinstance(value, dict) and value.get("request_id") == request_id
    ]
    if len(matches) != 1:
        raise TrialHalt("request_identity_mismatch")
    return matches[0]


def _new_state() -> dict[str, Any]:
    return {
        "schema": "agentflow.codex-model-worker-trial.fake@1",
        "simulation_only": True,
        "fixture_inventory": [
            {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in SYNTHETIC_OUTBOUND_INVENTORY
        ],
        "preflight_attempts": 0,
        "profile_ready": False,
        "attempted_thread_starts": 0,
        "attempted_turn_starts": 0,
        "simulated_thread_start_calls": 0,
        "simulated_turn_start_calls": 0,
        "lease_epoch": 1,
        "lease_token": "synthetic-lease-epoch-1",
        "continuity_id": SYNTHETIC_CONTINUITY_ID,
        "controller_restart_count": 0,
        "consumption_count": 0,
        "requests": [],
        "halt_code": "",
    }


def _read_state(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
            raise TrialHalt("trial_state_missing_or_oversized")
        state = json.loads(path.read_text(encoding="utf-8"))
    except TrialHalt:
        raise
    except (OSError, json.JSONDecodeError):
        raise TrialHalt("trial_state_unavailable") from None
    if (
        not isinstance(state, dict)
        or state.get("schema") != "agentflow.codex-model-worker-trial.fake@1"
        or state.get("simulation_only") is not True
        or state.get("fixture_inventory") != [
            {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in SYNTHETIC_OUTBOUND_INVENTORY
        ]
    ):
        raise TrialHalt("trial_state_identity_invalid")
    requests = state.get("requests")
    integer_fields = (
        "preflight_attempts", "attempted_thread_starts", "attempted_turn_starts",
        "simulated_thread_start_calls", "simulated_turn_start_calls", "lease_epoch",
        "controller_restart_count", "consumption_count",
    )
    if any(type(state.get(field)) is not int for field in integer_fields):
        raise TrialHalt("trial_state_accounting_invalid")
    if (
        not isinstance(requests, list)
        or len(requests) > MAX_TURN_STARTS
        or not 0 <= state["attempted_turn_starts"] <= MAX_TURN_STARTS
        or not 0 <= state["attempted_thread_starts"] <= MAX_TURN_STARTS
        or not 0 <= state["preflight_attempts"] <= MAX_PREFLIGHT_ATTEMPTS
        or state["simulated_thread_start_calls"] != state["attempted_thread_starts"]
        or state["simulated_turn_start_calls"] != state["attempted_turn_starts"]
        or state["attempted_thread_starts"] != len(requests)
        or state["attempted_turn_starts"] != sum(
            request.get("turn_start_state") != "not_started"
            for request in requests if isinstance(request, dict)
        )
        or state["lease_epoch"] < 1
        or state["lease_epoch"] != 1 + state["controller_restart_count"]
        or state["lease_token"] != f"synthetic-lease-epoch-{state['lease_epoch']}"
        or state["continuity_id"] != SYNTHETIC_CONTINUITY_ID
        or state["controller_restart_count"] not in (0, 1)
        or state["consumption_count"] > MAX_TURN_STARTS
        or not isinstance(state.get("lease_token"), str)
        or not isinstance(state.get("continuity_id"), str)
        or not isinstance(state.get("halt_code"), str)
        or type(state.get("profile_ready")) is not bool
    ):
        raise TrialHalt("trial_state_accounting_invalid")
    for index, request in enumerate(requests, start=1):
        if not isinstance(request, dict) or not _valid_request_state(request, index, state):
            raise TrialHalt("trial_state_request_identity_invalid")
    if state["consumption_count"] != sum(request["result_state"] == "consumed" for request in requests):
        raise TrialHalt("trial_state_accounting_invalid")
    return state


def _valid_request_state(request: Mapping[str, Any], index: int, state: Mapping[str, Any]) -> bool:
    expected = {
        "owner_id": SYNTHETIC_OWNER_ID,
        "claim_id": f"synthetic-claim-{index:03d}",
        "request_id": f"synthetic-request-{index:03d}",
        "continuity_id": state["continuity_id"],
        "expected_thread_id": f"synthetic-thread-{index:03d}",
        "expected_turn_id": f"synthetic-turn-{index:03d}",
    }
    if any(request.get(key) != value for key, value in expected.items()):
        return False
    if (
        type(request.get("lease_epoch")) is not int
        or not 1 <= request["lease_epoch"] <= state["lease_epoch"]
        or request.get("lease_token") != f"synthetic-lease-epoch-{request['lease_epoch']}"
    ):
        return False
    if request.get("thread_id") not in ("", expected["expected_thread_id"]):
        return False
    if request.get("turn_id") not in ("", expected["expected_turn_id"]):
        return False
    thread_state = request.get("thread_start_state")
    turn_state = request.get("turn_start_state")
    worker_state = request.get("worker_state")
    result_state = request.get("result_state")
    if thread_state not in {"reserved", "completed"}:
        return False
    if (thread_state == "reserved") != (request["thread_id"] == ""):
        return False
    if turn_state not in {"not_started", "reserved", "completed", "ambiguous"}:
        return False
    if (turn_state == "completed") != (request["turn_id"] == expected["expected_turn_id"]):
        return False
    if turn_state in {"not_started", "reserved", "ambiguous"} and request["turn_id"] != "":
        return False
    if worker_state not in {"reserved", "ambiguous", "running", "completed"}:
        return False
    if result_state not in {"pending", "ready", "consumed"}:
        return False
    if thread_state == "reserved" and turn_state != "not_started":
        return False
    if turn_state == "ambiguous" and worker_state != "ambiguous":
        return False
    if turn_state == "completed" and worker_state not in {"running", "completed"}:
        return False
    if worker_state == "completed" and result_state not in {"ready", "consumed"}:
        return False
    if result_state in {"ready", "consumed"} and worker_state != "completed":
        return False
    usage = request.get("usage")
    return (
        isinstance(usage, dict)
        and set(usage) == {"input_tokens", "output_tokens", "attributable"}
        and (usage["input_tokens"] is None or (type(usage["input_tokens"]) is int and usage["input_tokens"] >= 0))
        and (usage["output_tokens"] is None or (type(usage["output_tokens"]) is int and usage["output_tokens"] >= 0))
        and (usage["attributable"] is None or type(usage["attributable"]) is bool)
    )


def _save_state(path: Path, state: Mapping[str, Any]) -> None:
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise TrialHalt("trial_state_size_exceeded")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _run_fixed_simulation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-model-worker-fake-") as directory:
        harness = TrialHarness(Path(directory) / "trial-state.json")
        harness.preflight(active_profile_evidence())
        harness.start_fake_worker()
        restarted = TrialHarness(Path(directory) / "trial-state.json")
        restarted.resume_after_controller_restart(restarted.restart_evidence())
        restarted.complete_fake_turn()
        result = restarted.consume_fake_result()
        return {"result": result, "report": restarted.report()}


def main() -> int:
    if len(sys.argv) != 1:
        raise SystemExit("This source-only command accepts no options and runs only fixed fake data.")
    print(json.dumps(_run_fixed_simulation(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
