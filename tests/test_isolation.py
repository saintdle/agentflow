from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import isolation


def _fake_runner(stdout: str, returncode: int = 0):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return _run


# Adversarial fixture: each case is a distinct attempt to smuggle a credential
# past environment scrubbing, or to declare an unbounded isolation root.
# `kind` selects which control is under attack; `value` is the attacker input.
ADVERSARIAL_CASES = [
    {"kind": "env_leak", "value": "GITHUB_TOKEN"},
    {"kind": "env_leak", "value": "OPENAI_API_KEY"},
    {"kind": "env_leak", "value": "ANTHROPIC_API_KEY"},
    {"kind": "env_leak", "value": "AWS_SECRET_ACCESS_KEY"},
    {"kind": "env_leak", "value": "AWS_SESSION_TOKEN"},
    {"kind": "env_leak", "value": "NPM_TOKEN"},
    {"kind": "env_leak", "value": "DOCKER_PASSWORD"},
    {"kind": "env_leak", "value": "DATABASE_PASSWORD"},
    {"kind": "env_leak", "value": "SLACK_TOKEN"},
    {"kind": "env_leak", "value": "STRIPE_SECRET_KEY"},
    {"kind": "env_leak", "value": "JWT_SECRET"},
    {"kind": "env_leak", "value": "SSH_AUTH_SOCK"},
    {"kind": "env_leak", "value": "API_KEY"},
    {"kind": "env_leak", "value": "SESSION_COOKIE"},
    {"kind": "extra_env_inject", "value": "GITHUB_TOKEN"},
    {"kind": "extra_env_inject", "value": "OPENAI_API_KEY"},
    {"kind": "extra_env_inject", "value": "AWS_SECRET_ACCESS_KEY"},
    {"kind": "extra_env_inject", "value": "DATABASE_PASSWORD"},
    {"kind": "extra_env_inject", "value": "PRIVATE_KEY"},
    {"kind": "unbounded_root", "value": "/"},
    {"kind": "unbounded_root", "value": ""},
]


class AdversarialFixtureTests(unittest.TestCase):
    def test_at_least_twenty_adversarial_cases_defined(self) -> None:
        self.assertGreaterEqual(len(ADVERSARIAL_CASES), 20)

    def test_adversarial_matrix(self) -> None:
        for case in ADVERSARIAL_CASES:
            with self.subTest(case=case):
                if case["kind"] == "env_leak":
                    name = case["value"]
                    spec = isolation.IsolationSpec(env_allowlist=(name,))
                    env = isolation.scrub_environment(spec, source={name: "leaked-secret-value"})
                    self.assertNotIn(name, env)
                elif case["kind"] == "extra_env_inject":
                    name = case["value"]
                    with self.assertRaises(isolation.IsolationError):
                        isolation.IsolationSpec(extra_env=((name, "leaked"),))
                elif case["kind"] == "unbounded_root":
                    root = case["value"]
                    with self.assertRaises(isolation.IsolationError):
                        isolation.IsolationSpec(read_roots=(root,))
                    with self.assertRaises(isolation.IsolationError):
                        isolation.IsolationSpec(write_roots=(root,))
                else:  # pragma: no cover - fixture typo guard
                    self.fail(f"unknown adversarial case kind: {case['kind']}")


class EnvironmentScrubbingTests(unittest.TestCase):
    def test_real_home_never_leaks_through_allowlist(self) -> None:
        spec = isolation.IsolationSpec(env_allowlist=("HOME", "PATH"))
        env = isolation.scrub_environment(spec, source={"HOME": "/Users/real-user", "PATH": "/usr/bin"})
        self.assertNotIn("HOME", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_only_allowlisted_vars_pass_through(self) -> None:
        spec = isolation.IsolationSpec(env_allowlist=("PATH",))
        env = isolation.scrub_environment(spec, source={"PATH": "/usr/bin", "UNRELATED": "value"})
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_extra_env_non_credential_passes_through(self) -> None:
        spec = isolation.IsolationSpec(extra_env=(("AGENTFLOW_MODE", "isolated"),))
        env = isolation.scrub_environment(spec, source={})
        self.assertEqual(env["AGENTFLOW_MODE"], "isolated")


class ProfileGenerationTests(unittest.TestCase):
    def test_network_denied_by_default(self) -> None:
        profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertIn("(deny network*)", profile)
        self.assertNotIn("(allow network*)", profile)

    def test_network_allowed_when_requested(self) -> None:
        profile = isolation.build_profile(
            isolation.IsolationSpec(allow_network=True), home=Path("/tmp/home")
        )
        self.assertIn("(allow network*)", profile)

    def test_explicit_roots_are_granted(self) -> None:
        profile = isolation.build_profile(
            isolation.IsolationSpec(read_roots=("/tmp/read-me",), write_roots=("/tmp/write-me",)),
            home=Path("/tmp/home"),
        )
        self.assertIn("/tmp/read-me", profile)
        self.assertIn("/tmp/write-me", profile)

    def test_deny_default_present(self) -> None:
        profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertTrue(profile.startswith("(version 1)\n(deny default)"))


class ProtectedRootReadPolicyTests(unittest.TestCase):
    """Reads are allow-by-default with protected roots excluded, rather than
    an allowlist of system paths (a stale allowlist previously left real
    launches aborting with SIGABRT before producing any output). These tests
    pin the rule ordering sandbox-exec actually relies on: blanket allow,
    then protected-root deny, then caller-root re-allow more specific than
    the deny."""

    def test_protected_read_roots_cover_user_home_directories(self) -> None:
        self.assertIn("/Users", isolation.PROTECTED_READ_ROOTS)
        self.assertIn("/var/root", isolation.PROTECTED_READ_ROOTS)

    def test_blanket_read_allow_has_no_subpath_restriction(self) -> None:
        profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertIn("(allow file-read*)", profile)
        self.assertIn("(allow file-map-executable)", profile)

    def test_rule_order_is_allow_then_protected_deny_then_caller_reallow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp).resolve()
            caller_root = protected / "project"
            caller_root.mkdir()
            with mock.patch.object(isolation, "PROTECTED_READ_ROOTS", (str(protected),)):
                profile = isolation.build_profile(
                    isolation.IsolationSpec(read_roots=(str(caller_root),)),
                    home=Path("/tmp/home"),
                )
        lines = profile.splitlines()
        blanket_idx = lines.index("(allow file-read*)")
        deny_idx = lines.index(f'(deny file-read* (subpath "{protected}"))')
        reallow_idx = lines.index(f'(allow file-read* (subpath "{caller_root}"))')
        self.assertLess(blanket_idx, deny_idx)
        self.assertLess(deny_idx, reallow_idx)

    def test_protected_root_denies_both_read_and_map_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp).resolve()
            with mock.patch.object(isolation, "PROTECTED_READ_ROOTS", (str(protected),)):
                profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertIn(f'(deny file-read* (subpath "{protected}"))', profile)
        self.assertIn(f'(deny file-map-executable (subpath "{protected}"))', profile)

    def test_protected_root_without_explicit_caller_grant_is_not_reallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp).resolve()
            with mock.patch.object(isolation, "PROTECTED_READ_ROOTS", (str(protected),)):
                profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertNotIn(f'(allow file-read* (subpath "{protected}"))', profile)
        self.assertNotIn(f'(allow file-map-executable (subpath "{protected}"))', profile)

    def test_nonexistent_protected_root_is_silently_skipped(self) -> None:
        missing = "/definitely/does/not/exist-agentflow-protected-root"
        with mock.patch.object(isolation, "PROTECTED_READ_ROOTS", (missing,)):
            profile = isolation.build_profile(isolation.IsolationSpec(), home=Path("/tmp/home"))
        self.assertNotIn("exist-agentflow-protected-root", profile)

    def test_hand_maintained_system_path_allowlist_was_removed(self) -> None:
        # Regression guard: BASE_READ_SUBPATHS aborted real launches (SIGABRT)
        # because it could never enumerate every path a system runtime reads
        # merely to start; it must not come back in place of the blanket allow.
        self.assertFalse(hasattr(isolation, "BASE_READ_SUBPATHS"))

    def test_undeclared_non_protected_reads_are_an_explicit_non_goal(self) -> None:
        profile = isolation.build_profile(
            isolation.IsolationSpec(read_roots=("/tmp/declared",)), home=Path("/tmp/home")
        )
        self.assertIn("(allow file-read*)", profile)
        self.assertNotIn('(deny file-read* (subpath "/private/tmp"))', profile)
        self.assertEqual(isolation.capabilities()["read_policy"], "protected-root-denylist")


class FailClosedTests(unittest.TestCase):
    def test_launch_refuses_when_unsupported(self) -> None:
        with mock.patch.object(isolation, "platform_supported", return_value=False):
            with self.assertRaises(isolation.IsolationError):
                isolation.launch(["/bin/echo", "hi"], isolation.IsolationSpec())

    def test_probe_reports_unsupported_and_fails_closed(self) -> None:
        with mock.patch.object(isolation, "platform_supported", return_value=False):
            report = isolation.probe()
        self.assertFalse(report["supported"])
        self.assertFalse(report["ok"])
        self.assertTrue(all(value == "unsupported" for value in report["controls"].values()))

    def test_capabilities_reflect_unsupported_platform(self) -> None:
        with mock.patch.object(isolation, "platform_supported", return_value=False):
            caps = isolation.capabilities()
        self.assertFalse(caps["sandbox_exec"])


class BackendProbeTests(unittest.TestCase):
    """Exercises probe()/launch() orchestration with an injected sandbox-exec
    stand-in, since this development harness itself denies nested sandbox-exec
    invocation. `platform_supported()` genuinely reports True on this macOS
    host (the real binary is present); only the subprocess call is faked."""

    def test_backend_reports_darwin_sandbox_exec_present(self) -> None:
        self.assertEqual(isolation.platform_supported(), Path("/usr/bin/sandbox-exec").is_file())

    def test_probe_all_controls_pass_with_healthy_backend(self) -> None:
        # Sandbox reports a policy denial (EPERM) AND the outside-sandbox
        # control proves the endpoint is live: only then is network denial a
        # genuine "pass".
        payload = json.dumps(
            {
                "network": "denied",
                "out_of_root_write_denied": "pass",
                "in_root_write_allowed": "pass",
                "credential_env_absent": "pass",
                "ephemeral_home": "pass",
            }
        )
        with mock.patch.object(isolation, "platform_supported", return_value=True):
            report = isolation.probe(
                runner=_fake_runner(payload), network_control=lambda: ("1.1.1.1", 443)
            )
        self.assertTrue(report["supported"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["controls"]["network_denied"], "pass")

    def test_probe_flags_network_leak_when_sandbox_connects(self) -> None:
        # Endpoint reachable AND the sandboxed attempt connected: the network
        # control genuinely failed (the sandbox let traffic out).
        payload = json.dumps(
            {
                "network": "connected",
                "out_of_root_write_denied": "pass",
                "in_root_write_allowed": "pass",
                "credential_env_absent": "pass",
                "ephemeral_home": "pass",
            }
        )
        with mock.patch.object(isolation, "platform_supported", return_value=True):
            report = isolation.probe(
                runner=_fake_runner(payload), network_control=lambda: ("1.1.1.1", 443)
            )
        self.assertFalse(report["ok"])
        self.assertEqual(report["controls"]["network_denied"], "fail")

    def test_probe_network_unknown_when_control_endpoint_unreachable(self) -> None:
        # Offline / unreachable control: even a sandbox "denied" cannot be
        # trusted, because an unreachable endpoint would look denied too.
        payload = json.dumps(
            {
                "network": "denied",
                "out_of_root_write_denied": "pass",
                "in_root_write_allowed": "pass",
                "credential_env_absent": "pass",
                "ephemeral_home": "pass",
            }
        )
        with mock.patch.object(isolation, "platform_supported", return_value=True):
            report = isolation.probe(
                runner=_fake_runner(payload), network_control=lambda: None
            )
        self.assertFalse(report["ok"])
        self.assertEqual(report["controls"]["network_denied"], "unknown")
        self.assertIn("reachable", report["reason"])
        self.assertIsNone(report["network_control_endpoint"])

    def test_probe_network_unknown_on_ambiguous_sandbox_error(self) -> None:
        # Endpoint reachable, but the sandboxed attempt failed with something
        # other than a policy denial (e.g. connection refused, errno 61): the
        # failure is not attributable to sandbox policy, so fail closed.
        payload = json.dumps(
            {
                "network": "error:61",
                "out_of_root_write_denied": "pass",
                "in_root_write_allowed": "pass",
                "credential_env_absent": "pass",
                "ephemeral_home": "pass",
            }
        )
        with mock.patch.object(isolation, "platform_supported", return_value=True):
            report = isolation.probe(
                runner=_fake_runner(payload), network_control=lambda: ("1.1.1.1", 443)
            )
        self.assertFalse(report["ok"])
        self.assertEqual(report["controls"]["network_denied"], "unknown")
        self.assertIn("not a sandbox-policy denial", report["reason"])

    def test_launch_uses_ephemeral_home_and_scrubbed_env(self) -> None:
        captured = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        with mock.patch.object(isolation, "platform_supported", return_value=True), mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "should-not-leak", "PATH": "/usr/bin"}
        ):
            result = isolation.launch(["/bin/echo", "hi"], isolation.IsolationSpec(), runner=runner)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotEqual(captured["env"]["HOME"], str(Path.home()))
        self.assertEqual(captured["argv"][0], isolation.SANDBOX_EXEC)
        self.assertIsInstance(captured["argv"], list)


class ProcessGroupTeardownTests(unittest.TestCase):
    """Real process-group teardown, independent of sandbox-exec."""

    def test_teardown_kills_process_and_its_children(self) -> None:
        script = (
            "import subprocess, time, sys\n"
            "child = subprocess.Popen(['sleep', '30'])\n"
            "print(child.pid)\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        process = isolation.spawn_supervised(
            [sys.executable, "-c", script], isolation.IsolationSpec(), env=os.environ.copy()
        )
        try:
            grandchild_pid = int(process.stdout.readline().strip())
        except (ValueError, AttributeError):
            grandchild_pid = None
        time.sleep(0.2)
        status = isolation.teardown(process, grace_seconds=1.0)
        self.assertIn(status, {"terminated", "killed"})
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        # Ensure the leader is reaped
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        self.assertIsNotNone(process.poll())
        if grandchild_pid is not None:
            deadline = time.monotonic() + 2.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive, "grandchild process survived process-group teardown")

    def test_supervised_launch_timeout_kills_whole_group_no_survivors(self) -> None:
        # F2: a timeout must tear down the ENTIRE process group and reap it,
        # leaving no orphaned grandchild — not merely kill the direct child the
        # way subprocess.run(timeout=...) would.
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "grandchild.pid"
            script = (
                "import subprocess, time, sys\n"
                "child = subprocess.Popen(['sleep', '30'])\n"
                f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n"
            )
            spec = isolation.IsolationSpec(timeout_seconds=0.75)
            with self.assertRaises(isolation.IsolationError) as caught:
                isolation._launch_supervised(
                    [sys.executable, "-c", script],
                    spec,
                    cwd=None,
                    env=os.environ.copy(),
                    input_text=None,
                )
            self.assertIn("timeout", str(caught.exception).lower())
            grandchild_pid = int(pidfile.read_text().strip())
            deadline = time.monotonic() + 2.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive, "grandchild survived supervised-launch timeout teardown")

    def test_supervised_launch_returns_completed_process_on_success(self) -> None:
        result = isolation._launch_supervised(
            [sys.executable, "-c", "print('supervised-ok')"],
            isolation.IsolationSpec(timeout_seconds=5.0),
            cwd=None,
            env=os.environ.copy(),
            input_text=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "supervised-ok")

    def test_teardown_on_already_exited_process_is_a_noop(self) -> None:
        process = isolation.spawn_supervised(
            [sys.executable, "-c", "print('done')"], isolation.IsolationSpec(), env=os.environ.copy()
        )
        process.wait(timeout=5)
        status = isolation.teardown(process)
        self.assertEqual(status, "already-exited")
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_grandchild_ignoring_sigterm_is_killed(self) -> None:
        # F2 reproduction: a timed parent launches a child that ignores SIGTERM.
        # teardown must continue the grace period after leader exit and SIGKILL
        # the group, verifying no survivors remain.
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "grandchild.pid"
            script = (
                "import subprocess, signal, time, sys\n"
                "# Grandchild ignores SIGTERM\n"
                "child_script = '''\n"
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n"
                "'''\n"
                "child = subprocess.Popen([sys.executable, '-c', child_script])\n"
                f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                "sys.stdout.flush()\n"
                "# Parent waits so teardown can operate on the active group\n"
                "time.sleep(30)\n"
            )
            process = isolation.spawn_supervised(
                [sys.executable, "-c", script], isolation.IsolationSpec(), env=os.environ.copy()
            )
            time.sleep(0.3)  # Give grandchild time to install SIGTERM handler
            grandchild_pid = int(pidfile.read_text().strip())
            # Verify grandchild is alive before teardown
            os.kill(grandchild_pid, 0)
            status = isolation.teardown(process, grace_seconds=1.0)
            self.assertIn(status, {"terminated", "killed"})
            # Verify grandchild is gone after teardown
            deadline = time.monotonic() + 2.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive, "grandchild that ignored SIGTERM survived teardown")

    def test_exiting_parent_with_child_holding_pipes_is_killed(self) -> None:
        # F2 regression: parent spawns a child that ignores SIGTERM and inherits
        # stdout/stderr, writes the child PID, then exits immediately. communicate
        # times out because the child holds the pipes. os.getpgid(parent_pid) fails,
        # but teardown must still kill the group using the captured pgid = process.pid.
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            script = (
                "import subprocess, signal, sys\n"
                "child_script = '''\n"
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n"
                "'''\n"
                "# Child ignores SIGTERM and inherits parent's stdout/stderr\n"
                "child = subprocess.Popen([sys.executable, '-c', child_script])\n"
                f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
                "# Parent exits immediately, leaving child holding the pipes\n"
            )
            spec = isolation.IsolationSpec(timeout_seconds=0.75)
            with self.assertRaises(isolation.IsolationError) as caught:
                isolation._launch_supervised(
                    [sys.executable, "-c", script],
                    spec,
                    cwd=None,
                    env=os.environ.copy(),
                    input_text=None,
                )
            self.assertIn("timeout", str(caught.exception).lower())
            # Child must be gone even though parent exited immediately
            child_pid = int(pidfile.read_text().strip())
            deadline = time.monotonic() + 2.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive, "child holding pipes survived after parent exited immediately")


class ResourceLimitTests(unittest.TestCase):
    """Real rlimit enforcement, independent of sandbox-exec."""

    def test_preexec_caps_soft_limits_without_changing_host_hard_limits(self) -> None:
        spec = isolation.IsolationSpec(
            cpu_seconds=120, max_processes=4096, max_open_files=256
        )
        limits = {
            resource.RLIMIT_CPU: (60, resource.RLIM_INFINITY),
            resource.RLIMIT_NPROC: (100, 512),
            resource.RLIMIT_NOFILE: (128, 1024),
        }
        with mock.patch.object(
            isolation.resource, "getrlimit", side_effect=lambda kind: limits[kind]
        ), mock.patch.object(isolation.resource, "setrlimit") as setrlimit:
            isolation._preexec_fn(spec)()

        self.assertEqual(
            setrlimit.call_args_list,
            [
                mock.call(resource.RLIMIT_CPU, (120, resource.RLIM_INFINITY)),
                mock.call(resource.RLIMIT_NPROC, (512, 512)),
                mock.call(resource.RLIMIT_NOFILE, (256, 1024)),
            ],
        )

    def test_max_open_files_rlimit_is_enforced(self) -> None:
        script = (
            "import resource\n"
            "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])\n"
        )
        spec = isolation.IsolationSpec(max_open_files=64)
        process = isolation.spawn_supervised([sys.executable, "-c", script], spec, env=os.environ.copy())
        stdout, _ = process.communicate(timeout=5)
        self.assertEqual(int(stdout.strip()), 64)


if __name__ == "__main__":
    unittest.main()
