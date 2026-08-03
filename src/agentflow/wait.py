from __future__ import annotations

import dataclasses
import re
import subprocess
import time
from typing import Callable


class WaitError(RuntimeError):
    """A safe, user-facing wait-contract error."""


@dataclasses.dataclass(frozen=True)
class WaitContract:
    """A deterministic readiness contract for long-running or E2E work."""

    progress_event: str
    success_predicate: str
    failure_predicate: str
    max_silent_interval: float
    deadline: float
    cleanup_owner: str
    poll_command: str = ""
    poll_interval: float = 5.0

    def __post_init__(self) -> None:
        if not self.success_predicate:
            raise WaitError("a success predicate is required")
        if not self.cleanup_owner:
            raise WaitError("a cleanup owner is required so no wait hangs unowned")
        for name in ("max_silent_interval", "deadline", "poll_interval"):
            if getattr(self, name) <= 0:
                raise WaitError(f"{name} must be positive")
        if self.max_silent_interval > self.deadline:
            raise WaitError("max_silent_interval cannot exceed the global deadline")
        for name in ("success_predicate", "failure_predicate", "progress_event"):
            value = getattr(self, name)
            if value:
                try:
                    re.compile(value)
                except re.error as exc:
                    raise WaitError(f"invalid {name} regex: {exc}") from exc


@dataclasses.dataclass(frozen=True)
class WaitOutcome:
    status: str  # success | failure | stale | deadline
    reason: str
    elapsed: float
    last_progress_at: float
    last_predicate: str
    cleanup_owner: str
    polls: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def classify(contract: WaitContract, output: str) -> str:
    """Return success, failure, progress, or none for one poll ``output``."""

    text = output or ""
    if re.search(contract.success_predicate, text):
        return "success"
    if contract.failure_predicate and re.search(contract.failure_predicate, text):
        return "failure"
    if contract.progress_event and re.search(contract.progress_event, text):
        return "progress"
    return "none"


def run_wait(
    contract: WaitContract,
    poll: Callable[[], str],
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitOutcome:
    """Drive ``poll`` until a terminal condition. Never returns without a verdict.

    ``poll`` and ``now`` are injectable so the contract can be exercised with a
    deterministic fake clock. Every terminal path records the last observed
    predicate and the owner responsible for cleanup, so a wait is always
    classified as success, failure, stale, or deadline rather than silently
    hanging.
    """

    start = now()
    last_progress = start
    last_predicate = "start"
    polls = 0

    while True:
        current = now()
        elapsed = current - start
        if elapsed >= contract.deadline:
            return WaitOutcome(
                status="deadline",
                reason=f"global deadline {contract.deadline}s exceeded",
                elapsed=elapsed,
                last_progress_at=last_progress - start,
                last_predicate=last_predicate,
                cleanup_owner=contract.cleanup_owner,
                polls=polls,
            )
        if current - last_progress >= contract.max_silent_interval:
            return WaitOutcome(
                status="stale",
                reason=(
                    f"no progress for {contract.max_silent_interval}s "
                    f"(last predicate: {last_predicate})"
                ),
                elapsed=elapsed,
                last_progress_at=last_progress - start,
                last_predicate=last_predicate,
                cleanup_owner=contract.cleanup_owner,
                polls=polls,
            )

        output = poll()
        polls += 1
        verdict = classify(contract, output)
        current = now()
        elapsed = current - start
        if verdict == "success":
            return WaitOutcome(
                status="success",
                reason="success predicate matched",
                elapsed=elapsed,
                last_progress_at=current - start,
                last_predicate="success",
                cleanup_owner=contract.cleanup_owner,
                polls=polls,
            )
        if verdict == "failure":
            return WaitOutcome(
                status="failure",
                reason="failure predicate matched",
                elapsed=elapsed,
                last_progress_at=last_progress - start,
                last_predicate="failure",
                cleanup_owner=contract.cleanup_owner,
                polls=polls,
            )
        if verdict == "progress":
            last_progress = current
            last_predicate = "progress"

        sleep(contract.poll_interval)


def _run_poll_command(command: str) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"poll-command-error: {exc}"
    return (result.stdout or "") + (result.stderr or "")


def run_wait_command(contract: WaitContract) -> WaitOutcome:
    """Run the contract against its real poll command using the wall clock."""

    if not contract.poll_command:
        raise WaitError("a poll command is required to run a live wait")
    return run_wait(contract, lambda: _run_poll_command(contract.poll_command))
