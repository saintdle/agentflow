from __future__ import annotations

import dataclasses
import json
import os
import platform
import resource
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class IsolationError(RuntimeError):
    """A safe, user-facing hardened-isolation error."""


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DEFAULT_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM")
CREDENTIAL_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
    "SESSION",
)
# Roots holding another user's (or the real invoking user's) credentials and
# personal data. Reads are allow-by-default (see build_profile) because the
# set of system paths a launched interpreter/binary needs merely to start
# (dyld shared cache, framework metadata, locale/tzdata, Homebrew/Xcode CLT
# layouts, ...) is not enumerable and changes across OS/toolchain versions;
# a hand-maintained system-path allowlist silently goes stale and the child
# is killed (SIGABRT) mid-startup with no output. These roots are the
# exclusions carved back out of that default allow; the caller's declared
# read/write roots (and the ephemeral $HOME) are then re-allowed on top of
# the exclusion, since sandbox-exec resolves overlapping subpath rules by
# specificity: a more specific allow wins over a less specific deny, which
# itself wins over the blanket allow.
PROTECTED_READ_ROOTS = (
    "/Users",
    "/private/var/root",
    "/var/root",
)
DANGEROUS_ROOTS = ("/", "")


@dataclasses.dataclass(frozen=True)
class IsolationSpec:
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    allow_network: bool = False
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    extra_env: tuple[tuple[str, str], ...] = ()
    cpu_seconds: int = 120
    # RLIMIT_NPROC is checked against the *system-wide* process count for the
    # real UID, not a per-subtree count, so this must stay well above a busy
    # workstation's ambient process count or even a single legitimate fork
    # inside the sandbox will fail with EAGAIN.
    max_processes: int = 4096
    max_open_files: int = 256
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for root in (*self.read_roots, *self.write_roots):
            if root in DANGEROUS_ROOTS:
                raise IsolationError(f"refusing an unbounded isolation root: {root!r}")
        for name, _ in self.extra_env:
            if _is_credential_shaped(name):
                raise IsolationError(f"refusing to inject credential-shaped env var: {name}")

    def normalized_read_roots(self) -> tuple[Path, ...]:
        return tuple(sorted({Path(root).expanduser().resolve() for root in self.read_roots}))

    def normalized_write_roots(self) -> tuple[Path, ...]:
        return tuple(sorted({Path(root).expanduser().resolve() for root in self.write_roots}))


def _is_credential_shaped(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in CREDENTIAL_ENV_MARKERS)


def platform_supported() -> bool:
    return platform.system() == "Darwin" and Path(SANDBOX_EXEC).is_file()


def capabilities() -> dict[str, Any]:
    return {
        "platform": platform.system(),
        "sandbox_exec": platform_supported(),
        "read_policy": "protected-root-denylist",
        "rlimits": True,
        "process_group_teardown": True,
    }


def scrub_environment(spec: IsolationSpec, *, source: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment: allowlisted, non-credential-shaped vars only."""
    source_env = os.environ if source is None else source
    scrubbed: dict[str, str] = {}
    for name in spec.env_allowlist:
        if name == "HOME" or _is_credential_shaped(name):
            continue
        if name in source_env:
            scrubbed[name] = source_env[name]
    for name, value in spec.extra_env:
        if _is_credential_shaped(name):
            raise IsolationError(f"refusing to inject credential-shaped env var: {name}")
        scrubbed[name] = value
    return scrubbed


def _sbpl_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_profile(spec: IsolationSpec, *, home: Path) -> str:
    """Render an sbpl (sandbox-exec) profile.

    Process/network/write controls stay deny-default with explicit opt-in.
    Reads use a separate, deliberately different shape: allow-by-default,
    then deny the protected roots (`PROTECTED_READ_ROOTS`), then re-allow the
    caller's declared roots plus the ephemeral home. Rule order in the
    profile text mirrors how sandbox-exec actually resolves overlap (most
    specific subpath wins), so the deny-then-reallow sequence below is load
    bearing, not cosmetic: a protected-root deny must exist before a caller
    root can be carved back out of it.
    """
    read_roots = set(spec.normalized_read_roots()) | {home}
    write_roots = set(spec.normalized_write_roots()) | {home}
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read*)",
        "(allow file-map-executable)",
    ]
    protected_roots = {Path(root).resolve() for root in PROTECTED_READ_ROOTS if Path(root).exists()}
    for root in sorted(protected_roots):
        lines.append(f'(deny file-read* (subpath "{_sbpl_quote(root)}"))')
        lines.append(f'(deny file-map-executable (subpath "{_sbpl_quote(root)}"))')
    for root in sorted(read_roots):
        lines.append(f'(allow file-read* (subpath "{_sbpl_quote(root)}"))')
        lines.append(f'(allow file-map-executable (subpath "{_sbpl_quote(root)}"))')
    for root in sorted(write_roots):
        lines.append(f'(allow file-write* (subpath "{_sbpl_quote(root)}"))')
    lines.append("(allow network*)" if spec.allow_network else "(deny network*)")
    return "\n".join(lines) + "\n"


def _preexec_fn(spec: IsolationSpec) -> Callable[[], None]:
    def _set_soft_limit(kind: int, requested: int) -> None:
        _, hard = resource.getrlimit(kind)
        bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        # Preserve the host's hard limit. Lowering or attempting to raise it in
        # a child preexec hook can fail on hosted macOS runners even when the
        # requested soft limit itself is valid.
        resource.setrlimit(kind, (bounded, hard))

    def _apply() -> None:
        _set_soft_limit(resource.RLIMIT_CPU, spec.cpu_seconds)
        _set_soft_limit(resource.RLIMIT_NPROC, spec.max_processes)
        _set_soft_limit(resource.RLIMIT_NOFILE, spec.max_open_files)

    return _apply


def spawn_supervised(
    argv: list[str],
    spec: IsolationSpec,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    popen: Callable[..., subprocess.Popen[str]] | None = None,
) -> subprocess.Popen[str]:
    """Start argv rlimited, in a fresh session/process-group. Caller owns teardown()."""
    launcher = popen or subprocess.Popen
    return launcher(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else scrub_environment(spec),
        preexec_fn=_preexec_fn(spec),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish_teardown(process: subprocess.Popen[str], status: str) -> str:
    """Reap the leader and close parent-side pipes after a successful teardown."""
    process.poll()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except OSError:
            pass
    return status


def teardown(process: subprocess.Popen[str], *, grace_seconds: float = 2.0) -> str:
    """Kill the process's entire group (children included); escalate to SIGKILL.

    Since start_new_session=True makes process.pid the process-group ID, we
    retain that pgid even after the leader exits. Returns "terminated" only
    when the group is actually gone. The leader exiting is not sufficient: a
    child that ignores SIGTERM still holds the group alive, and we continue
    the grace period until the whole group is reaped or SIGKILL is required.
    """
    # Capture pgid before anything can exit: start_new_session=True makes
    # process.pid the group ID, and that remains the pgid even if the leader exits.
    pgid = process.pid
    # Don't return early just because the leader has exited - descendants may remain
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        # Group is already gone
        return _finish_teardown(process, "already-exited")
    except PermissionError:
        # Can't signal the group - fail closed
        raise RuntimeError(f"cannot signal process group {pgid}: permission denied")
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        # Check if the group still exists
        try:
            os.killpg(pgid, 0)  # Test if group still exists (signal 0 is a no-op probe)
            # Group still exists, keep waiting
        except ProcessLookupError:
            # Group is gone
            return _finish_teardown(process, "terminated")
        except PermissionError:
            # Can't probe - might be in transition. Continue waiting and retry.
            pass
        time.sleep(0.02)
    # Grace period expired and group might still exist: escalate to SIGKILL
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        # Group disappeared during escalation
        return _finish_teardown(process, "terminated")
    except PermissionError:
        # On macOS, EPERM when sending SIGKILL might mean the group is already gone,
        # or the processes are zombies waiting to be reaped. Verify state before failing.
        try:
            import subprocess as sp
            result = sp.run(
                ["ps", "-g", str(pgid), "-o", "pid=,state="],
                capture_output=True,
                text=True,
                timeout=1.0
            )
            if result.returncode == 0 and result.stdout.strip():
                # Check if all processes are zombies (state Z)
                lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
                all_zombies = all('Z' in line.split()[1] if len(line.split()) > 1 else False for line in lines)
                if all_zombies:
                    # All processes are zombies - they're effectively dead, just need reaping
                    return _finish_teardown(process, "killed")
                # Live processes remain - this is a real permission problem
                raise RuntimeError(f"cannot SIGKILL process group {pgid}: permission denied, processes remain: {result.stdout.strip()}")
            # No processes found - group is gone
            return _finish_teardown(process, "terminated")
        except sp.TimeoutExpired:
            raise RuntimeError(f"cannot SIGKILL process group {pgid}: permission denied")
        except Exception as e:
            if "RuntimeError" in str(type(e)):
                raise
            # ps command failed - can't verify
            raise RuntimeError(f"cannot SIGKILL process group {pgid}: permission denied (verification failed)")
    # Wait bounded time for the leader (if still present) and verify group destruction
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    # Final verification with retry: is the group actually gone?
    # After SIGKILL, give processes time to fully terminate
    verification_deadline = time.monotonic() + grace_seconds
    while time.monotonic() < verification_deadline:
        try:
            os.killpg(pgid, 0)
            # Group still exists, wait and retry
            time.sleep(0.05)
        except ProcessLookupError:
            # Group is confirmed gone
            return _finish_teardown(process, "killed")
        except PermissionError:
            # On macOS, EPERM can occur when the group no longer exists but the pgid
            # is in transition. Verify by checking if we can find any process with
            # that pgid. If we can't verify, fail closed.
            try:
                # Try to list processes in the group (macOS-specific check)
                import subprocess as sp
                result = sp.run(
                    ["ps", "-g", str(pgid), "-o", "pid="],
                    capture_output=True,
                    text=True,
                    timeout=1.0
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Processes still exist in the group, wait and retry
                    time.sleep(0.05)
                    continue
                # No processes found in the group - it's gone
                return _finish_teardown(process, "killed")
            except Exception:
                # Can't verify - fail closed
                raise RuntimeError(f"cannot verify process group {pgid} destruction: permission denied")
    # Final check after timeout
    try:
        os.killpg(pgid, 0)
        # Group still exists even after grace period post-SIGKILL - fail closed
        raise RuntimeError(f"process group {pgid} survived SIGKILL + {grace_seconds}s grace period")
    except ProcessLookupError:
        return _finish_teardown(process, "killed")
    except PermissionError:
        # Try ps one last time
        try:
            import subprocess as sp
            result = sp.run(
                ["ps", "-g", str(pgid), "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=1.0
            )
            if result.returncode == 0 and result.stdout.strip():
                raise RuntimeError(
                    f"process group {pgid} has surviving members after SIGKILL: {result.stdout.strip()}"
                )
            return _finish_teardown(process, "killed")
        except Exception:
            raise RuntimeError(f"cannot verify process group {pgid} destruction: permission denied")


@dataclasses.dataclass(frozen=True)
class LaunchResult:
    returncode: int
    stdout: str
    stderr: str
    profile: str
    home: str


def _launch_supervised(
    full_argv: list[str],
    spec: IsolationSpec,
    *,
    cwd: Path | None,
    env: dict[str, str],
    input_text: str | None,
    popen: Callable[..., subprocess.Popen[str]] | None = None,
    teardown_fn: Callable[..., str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``full_argv`` in its own session/process-group and enforce the
    timeout by tearing the *entire* group down, never just the direct child.

    ``subprocess.run(..., timeout=...)`` only kills the immediate child on a
    timeout; any grandchildren the sandboxed command forked into the same new
    session keep running as orphans. Here a timeout escalates SIGTERM then
    SIGKILL to the whole process group via ``teardown()``, reaps it, and then
    raises an explicit ``IsolationError`` so no survivor outlives the launch.
    """
    launcher = popen or subprocess.Popen
    reap = teardown_fn or teardown
    process = launcher(
        full_argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=_preexec_fn(spec),
        start_new_session=True,
    )
    # Capture pgid immediately: start_new_session=True makes process.pid the
    # process-group ID, and it remains valid even if the leader exits.
    pgid = process.pid
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=spec.timeout_seconds)
    except subprocess.TimeoutExpired:
        status = reap(process)
        # Drain the pipes and fully reap; teardown() already waited on the
        # leader, but this guarantees the file objects are closed and no
        # descendant is left buffering output.
        try:
            process.communicate(timeout=grace_timeout(spec))
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        # Verify no survivors: the group must be gone
        try:
            os.killpg(pgid, 0)
            # Group still exists - this should not happen after teardown
            raise IsolationError(
                f"sandboxed launch exceeded its {spec.timeout_seconds:g}s timeout; teardown "
                f"returned {status!r} but process group {pgid} still exists (fail-closed)."
            )
        except ProcessLookupError:
            pass  # Group is gone, as expected
        except PermissionError:
            # Can't verify - fail closed
            raise IsolationError(
                f"sandboxed launch exceeded its {spec.timeout_seconds:g}s timeout; cannot verify "
                f"process group {pgid} destruction: permission denied (fail-closed)."
            )
        raise IsolationError(
            f"sandboxed launch exceeded its {spec.timeout_seconds:g}s timeout; the entire "
            f"process group was terminated ({status}) with no survivors (fail-closed)."
        )
    return subprocess.CompletedProcess(full_argv, process.returncode, stdout=stdout, stderr=stderr)


def grace_timeout(spec: IsolationSpec) -> float:
    """A short, bounded window to drain output after a group teardown."""
    return min(5.0, max(1.0, spec.timeout_seconds))


def launch(
    argv: list[str],
    spec: IsolationSpec,
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> LaunchResult:
    """Run argv confined by sandbox-exec. Fails closed if hardened controls are unavailable."""
    if not platform_supported():
        raise IsolationError(
            "hardened isolation requires macOS sandbox-exec, which is unavailable here; "
            "refusing to launch unconfined (fail-closed)."
        )
    with tempfile.TemporaryDirectory(prefix="agentflow-home-") as home_dir:
        home = Path(home_dir).resolve()
        profile_text = build_profile(spec, home=home)
        with tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False) as profile_file:
            profile_file.write(profile_text)
            profile_path = Path(profile_file.name)
        try:
            env = scrub_environment(spec)
            env["HOME"] = str(home)
            full_argv = [SANDBOX_EXEC, "-f", str(profile_path), *argv]
            if runner is not None:
                # Deterministic test seam: an injected runner stands in for the
                # real supervised execution and must honour the same call shape.
                result = runner(
                    full_argv,
                    cwd=str(cwd) if cwd else None,
                    env=env,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout_seconds,
                    preexec_fn=_preexec_fn(spec),
                    start_new_session=True,
                )
            else:
                result = _launch_supervised(
                    full_argv, spec, cwd=cwd, env=env, input_text=input_text
                )
        finally:
            profile_path.unlink(missing_ok=True)
    return LaunchResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        profile=profile_text,
        home=str(home),
    )


PROBE_CONTROLS = (
    "network_denied",
    "out_of_root_write_denied",
    "in_root_write_allowed",
    "credential_env_absent",
    "ephemeral_home",
)
# Controls the sandboxed probe script decides on its own (pass/fail). The
# network control is deliberately NOT among these: a sandboxed process cannot
# distinguish "the sandbox denied me" from "the network is simply down", so
# network denial is adjudicated by probe() using an out-of-sandbox reachability
# control plus the sandboxed attempt's failure mode.
SANDBOX_REPORTED_CONTROLS = (
    "out_of_root_write_denied",
    "in_root_write_allowed",
    "credential_env_absent",
    "ephemeral_home",
)
# Public, unencrypted-reachable TCP endpoints used only as an outside-sandbox
# reachability control. Given as literal IPs so the control does not depend on
# DNS (itself a network operation the sandbox blocks). The first one reachable
# from the controlling process is the endpoint the sandboxed attempt targets.
NETWORK_PROBE_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("9.9.9.9", 443),
)


def _reachable_endpoint(
    endpoints: tuple[tuple[str, int], ...], *, timeout: float = 2.0
) -> tuple[str, int] | None:
    """Outside-sandbox control: the first endpoint the (unconfined) controlling
    process can actually open a TCP connection to, or None if none is reachable.
    """
    for host, port in endpoints:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            sock.close()
            continue
        sock.close()
        return (host, port)
    return None


def _classify_network_denial(
    reachable: tuple[str, int] | None, sandbox_result: Any
) -> str:
    """Adjudicate the network control, failing closed on every ambiguity.

    "pass" is returned only when an outside-sandbox control proved the endpoint
    reachable AND the sandboxed attempt failed with a denial attributable to
    sandbox policy (``EPERM``). A successful sandboxed connect is "fail".
    Everything else — no reachable control, a refused/unreachable/timed-out or
    otherwise ambiguous sandboxed error — is "unknown" (fail-closed).
    """
    if reachable is None:
        return "unknown"
    if sandbox_result == "denied":
        return "pass"
    if sandbox_result == "connected":
        return "fail"
    return "unknown"


def probe(
    spec: IsolationSpec | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    network_control: Callable[[], tuple[str, int] | None] | None = None,
) -> dict[str, Any]:
    """Deterministic fresh-state probe of every claimed isolation control.

    Fails closed: if hardened controls are unavailable, every control is
    reported unsupported and `ok` is False rather than silently degrading. The
    network control fails closed too — it only passes when an out-of-sandbox
    reachability check proves the chosen endpoint is up and the sandboxed
    attempt is denied by policy (see ``_classify_network_denial``).
    """
    if not platform_supported():
        return {
            "supported": False,
            "ok": False,
            "controls": {name: "unsupported" for name in PROBE_CONTROLS},
            "reason": "sandbox-exec is unavailable on this host; failing closed",
        }
    control = network_control or (lambda: _reachable_endpoint(NETWORK_PROBE_ENDPOINTS))
    reachable = control()
    # Always hand the sandboxed script a concrete endpoint to attempt. When the
    # outside control found nothing reachable we still probe (the endpoint is
    # informational only), but network denial can never be "pass" in that case.
    target_host, target_port = reachable or NETWORK_PROBE_ENDPOINTS[0]
    with tempfile.TemporaryDirectory(prefix="agentflow-probe-write-") as write_dir:
        write_root = Path(write_dir).resolve()
        base_spec = spec or IsolationSpec()
        # The write-target the probe script actually exercises must always be
        # granted, even when the caller supplied their own spec (e.g. the CLI
        # forwarding --write): otherwise "in_root_write_allowed" fails on a
        # correctly-enforcing sandbox merely because this fresh directory
        # wasn't among the caller's declared write_roots.
        probe_spec = dataclasses.replace(
            base_spec, write_roots=(*base_spec.write_roots, str(write_root))
        )
        script = _PROBE_SCRIPT.format(
            write_root=write_root.as_posix(),
            net_host=target_host,
            net_port=target_port,
        )
        try:
            result = launch(["/usr/bin/env", "python3", "-c", script], probe_spec, runner=runner)
        except IsolationError as exc:
            return {
                "supported": True,
                "ok": False,
                "controls": {name: "unknown" for name in PROBE_CONTROLS},
                "reason": str(exc),
            }
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    except (ValueError, IndexError):
        payload = {}
    controls = {name: payload.get(name, "unknown") for name in SANDBOX_REPORTED_CONTROLS}
    controls["network_denied"] = _classify_network_denial(reachable, payload.get("network"))
    ok = all(controls[name] == "pass" for name in PROBE_CONTROLS)
    report: dict[str, Any] = {
        "supported": True,
        "ok": ok,
        "controls": controls,
        "returncode": result.returncode,
        "stderr": result.stderr[-2000:],
        "network_control_endpoint": f"{target_host}:{target_port}" if reachable else None,
    }
    if not payload:
        # The sandboxed probe produced no parseable output at all (as opposed
        # to reporting individual "fail" controls): it never ran its own
        # code, so every control is genuinely undetermined rather than
        # failing. Say so explicitly instead of leaving bare "unknown"
        # indistinguishable from a script bug.
        if result.returncode < 0:
            signal_desc = signal.strsignal(-result.returncode) or "unknown signal"
            report["reason"] = (
                f"sandboxed probe process was killed by signal {-result.returncode} "
                f"({signal_desc}) before producing any output; the isolation profile "
                "cannot be verified on this host"
            )
        else:
            report["reason"] = (
                f"sandboxed probe process exited {result.returncode} without producing "
                "parseable output; the isolation profile cannot be verified on this host"
            )
    elif controls["network_denied"] == "unknown":
        # The rest of the probe ran, but the network control could not be
        # adjudicated. Name why, so an "unknown" here is never mistaken for a
        # transient script bug.
        if reachable is None:
            report["reason"] = (
                "no control endpoint was reachable from outside the sandbox, so network "
                "denial could not be proven against a live endpoint; failing closed"
            )
        else:
            report["reason"] = (
                f"sandboxed attempt to reachable endpoint {target_host}:{target_port} "
                f"returned {payload.get('network')!r}, which is not a sandbox-policy denial; "
                "failing closed"
            )
    return report


# ``net_host``/``net_port`` are injected by probe(): the endpoint an outside
# control already proved reachable. Inside the sandbox we only classify the
# failure mode — a policy denial surfaces as PermissionError (EPERM); any other
# OSError is ambiguous and reported verbatim for the controller to adjudicate.
_PROBE_SCRIPT = """
import json, os, socket

report = {{}}
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(({net_host!r}, {net_port}))
    s.close()
    report["network"] = "connected"
except PermissionError:
    report["network"] = "denied"
except OSError as exc:
    report["network"] = "error:" + str(getattr(exc, "errno", None))

try:
    with open("/tmp/agentflow-probe-escape", "w") as handle:
        handle.write("escape")
    os.remove("/tmp/agentflow-probe-escape")
    report["out_of_root_write_denied"] = "fail"
except OSError:
    report["out_of_root_write_denied"] = "pass"

try:
    target = os.path.join({write_root!r}, "probe-write-ok")
    with open(target, "w") as handle:
        handle.write("ok")
    report["in_root_write_allowed"] = "pass"
except OSError:
    report["in_root_write_allowed"] = "fail"

markers = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")
leaked = [name for name in os.environ if any(marker in name.upper() for marker in markers)]
report["credential_env_absent"] = "fail" if leaked else "pass"

report["ephemeral_home"] = "pass" if os.environ.get("HOME", "").startswith("/private/var") or "agentflow-home-" in os.environ.get("HOME", "") else "fail"

print(json.dumps(report))
"""
