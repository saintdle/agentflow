"""Structured provider-neutral task limits and POSIX process-group supervision."""

from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


class ExecutionLimitError(ValueError):
    """Invalid or incomplete structured execution limits."""


@dataclass(frozen=True)
class ExecutionLimits:
    deadline_seconds: int
    max_retries: int

    def to_dict(self) -> dict[str, int]:
        return {"deadline_seconds": self.deadline_seconds, "max_retries": self.max_retries}


def parse_limits(value: Any) -> ExecutionLimits | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"deadline_seconds", "max_retries"}:
        raise ExecutionLimitError("execution_limits requires deadline_seconds and max_retries")
    deadline, retries = value.get("deadline_seconds"), value.get("max_retries")
    if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline <= 0:
        raise ExecutionLimitError("deadline_seconds must be a positive integer")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ExecutionLimitError("max_retries must be a nonnegative integer")
    return ExecutionLimits(deadline, retries)


def capability_status(limits: ExecutionLimits | None, *, enforced: bool = False) -> dict[str, str]:
    return {
        "deadline": (
            "process_group_enforced_for_confined_provider_argv"
            if limits is not None and enforced
            else "advisory" if limits is None
            else "advisory_until_supervised_launch"
        ),
        "retries": (
            "agentflow_task_launches_enforced_within_policy_caps"
            if limits is not None and enforced
            else "unavailable" if limits is None
            else "advisory_until_controller_launch"
        ),
        "provider_spend": "unavailable",
        "detached_descendants": "outside_process_group_cap",
    }


def effective_attempt_limit(policy_max_attempts: int, limits: ExecutionLimits | None) -> int:
    """Apply an optional retry cap without raising the project policy cap."""
    if limits is None:
        return policy_max_attempts
    return min(policy_max_attempts, 1 + limits.max_retries)


def supervise(deadline_epoch: float, argv: Sequence[str]) -> int:
    """Run confined argv until its absolute deadline, killing its process group."""
    remaining = deadline_epoch - time.time()
    if not argv:
        return 2
    if remaining <= 0:
        return 124
    try:
        child = subprocess.Popen(list(argv), preexec_fn=os.setpgrp)
    except OSError as exc:
        print(f"agentflow execution supervisor: cannot start provider: {exc}", file=sys.stderr)
        return 127

    def terminate_group() -> None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        stop_at = time.monotonic() + 1.0
        while time.monotonic() < stop_at:
            try:
                os.killpg(child.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    previous_handlers: dict[int, Any] = {}

    def stop_child(signum: int, _frame: Any) -> None:
        terminate_group()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_group()
            child.wait()
        raise SystemExit(128 + signum)

    for caught in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        previous_handlers[caught] = signal.signal(caught, stop_child)
    terminal_fd: int | None = None
    previous_foreground = 0
    try:
        try:
            terminal_fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            previous_foreground = os.tcgetpgrp(terminal_fd)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            os.tcsetpgrp(terminal_fd, child.pid)
            # An interactive provider can be stopped by SIGTTIN if it reads
            # before this handoff completes. Resume it after foregrounding.
            try:
                os.killpg(child.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        except OSError:
            if terminal_fd is not None:
                os.close(terminal_fd)
                terminal_fd = None
        deadline = time.monotonic() + remaining
        exited_codes = {
            getattr(os, "CLD_EXITED", -1), getattr(os, "CLD_KILLED", -1),
            getattr(os, "CLD_DUMPED", -1),
        }
        waitid = getattr(os, "waitid", None)
        while time.monotonic() < deadline:
            if waitid is not None:
                status = waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                exited = status is not None and status.si_pid == child.pid and status.si_code in exited_codes
            else:
                # Python/platforms without waitid can use Popen polling. If
                # the leader is reaped, a surviving descendant keeps the
                # process group present; cleanup remains scoped to this PGID.
                exited = child.poll() is not None
            if exited:
                # Keep the leader zombie (and PGID) until descendant cleanup.
                terminate_group()
                return child.wait()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        terminate_group()
        child.wait()
        return 124
    finally:
        if terminal_fd is not None:
            try:
                os.tcsetpgrp(terminal_fd, previous_foreground)
            except OSError:
                pass
            os.close(terminal_fd)
        for caught, handler in previous_handlers.items():
            signal.signal(caught, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--deadline-epoch" or args[2] != "--":
        print("usage: execution_limits.py --deadline-epoch EPOCH -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        deadline = float(args[1])
    except ValueError:
        print("execution supervisor requires a numeric absolute deadline", file=sys.stderr)
        return 2
    return supervise(deadline, args[3:])


if __name__ == "__main__":
    raise SystemExit(main())
