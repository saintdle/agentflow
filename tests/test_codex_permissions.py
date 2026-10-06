from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.codex_permissions import (
    CodexPermissionConfigError,
    DEFAULT_PROFILE_NAME,
    build_readonly_permission_config,
    validate_caller_config,
    validate_readonly_permission_config,
)


_EXPECTED_PROBE_RESULTS = {
    "fixture": "allowed",
    "inside_token": "denied",
    "inside_lease": "denied",
    "external_capability": "denied",
    "external_lease": "denied",
    "symlink_alias": "denied",
    "token_write": "denied",
    "external_write": "denied",
    "workspace_write": "denied",
}


def _build_shell_probe(paths: dict[str, Path]) -> str:
    q = shlex.quote
    read_probe = r'''probe_read() {
  path=$1; expected=$2
  output=$(/bin/cat "$path" 2>&1); status=$?
  if [ "$status" -eq 0 ]; then
    if [ "$output" = "$expected" ]; then printf allowed; else printf wrong_content; fi
  else
    case "$output" in
      *"Operation not permitted"*|*"Permission denied"*) printf denied ;;
      *"No such file or directory"*|*"not found"*) printf missing ;;
      *) printf command_error ;;
    esac
  fi
}
probe_write() {
  path=$1
  output=$(/bin/sh -c 'printf x > "$1"' probe "$path" 2>&1); status=$?
  if [ "$status" -eq 0 ]; then printf allowed
  else
    case "$output" in
      *"Operation not permitted"*|*"Permission denied"*) printf denied ;;
      *"No such file or directory"*|*"not found"*) printf missing ;;
      *) printf command_error ;;
    esac
  fi
}'''
    calls = [
        ("fixture", paths["fixture"], "SYNTHETIC_FIXTURE"),
        ("inside_token", paths["inside_token"], "FAKE_TOKEN"),
        ("inside_lease", paths["inside_lease"], "FAKE_LEASE"),
        ("external_capability", paths["external_capability"], "FAKE_CAPABILITY"),
        ("external_lease", paths["external_lease"], "FAKE_EXTERNAL_LEASE"),
        ("symlink_alias", paths["symlink_alias"], "FAKE_CAPABILITY"),
    ]
    lines = [read_probe, "printf '{" + ",".join(f'\"{name}\":\"%s\"' for name in _EXPECTED_PROBE_RESULTS) + "}\\n' \\"]
    lines.extend(f'  "$(probe_read {q(str(path))} {expected})" \\' for _name, path, expected in calls)
    lines.extend((
        f'  "$(probe_write {q(str(paths["inside_token"]))})" \\',
        f'  "$(probe_write {q(str(paths["external_capability"]))})" \\',
        f'  "$(probe_write {q(str(paths["workspace_write"]))})"',
    ))
    return "\n".join(lines)


def _parse_probe_results(stdout: str) -> dict[str, str]:
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("probe output is not the fixed status record") from exc
    if result != _EXPECTED_PROBE_RESULTS:
        raise ValueError("probe did not establish every required allow/deny outcome")
    return result


class _OptionalProbeSdkUnavailable(Exception):
    pass


class _ProbeSdkImportError(RuntimeError):
    pass


class _ProbeCallError(RuntimeError):
    pass


def _load_probe_sdk(importer=None):
    importer = importer or importlib.import_module
    try:
        sdk = importer("openai_codex")
    except ModuleNotFoundError as exc:
        if exc.name == "openai_codex":
            raise _OptionalProbeSdkUnavailable("optional Codex SDK is not installed") from None
        raise _ProbeSdkImportError("installed Codex SDK has a missing dependency") from None
    except Exception:
        raise _ProbeSdkImportError("optional Codex SDK could not be imported") from None

    try:
        client_module = importer("openai_codex.client")
        generated = importer("openai_codex.generated.v2_all")
        return (
            sdk,
            client_module.CodexClient,
            client_module.CodexConfig,
            generated.CommandExecParams,
            generated.CommandExecResponse,
        )
    except Exception:
        raise _ProbeSdkImportError("installed Codex SDK is incomplete or incompatible") from None


def _close_probe_client_bounded(client, timeout_seconds: float) -> None:
    failures = []

    def close():
        try:
            client.close()
        except BaseException as exc:  # Never surface SDK exception text.
            failures.append(exc)

    worker = threading.Thread(target=close, name="codex-probe-close", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise _ProbeCallError("Codex client cleanup exceeded its deadline")
    if failures:
        raise _ProbeCallError("Codex client cleanup failed") from None


def _bounded_sdk_call(
    client,
    operation: str,
    call,
    timeout_seconds: float,
    *,
    cleanup_timeout_seconds: float = 2,
    settle_timeout_seconds: float = 1,
):
    safe_operation = operation if operation in {"start", "initialize", "command/exec"} else "SDK call"
    result = []
    failures = []

    def invoke():
        try:
            result.append(call())
        except BaseException as exc:  # Keep provider/config exception text out of logs.
            failures.append(exc)

    worker = threading.Thread(target=invoke, name="codex-permission-probe", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    timed_out = worker.is_alive()

    if timed_out or failures:
        cleanup_error = None
        try:
            _close_probe_client_bounded(client, cleanup_timeout_seconds)
        except _ProbeCallError as exc:
            cleanup_error = str(exc)
        if timed_out:
            worker.join(settle_timeout_seconds)
            if cleanup_error:
                raise _ProbeCallError(
                    f"Codex {safe_operation} timed out; {cleanup_error}"
                ) from None
            if worker.is_alive():
                raise _ProbeCallError(
                    f"Codex {safe_operation} timed out and did not stop after client close"
                ) from None
            raise _ProbeCallError(f"Codex {safe_operation} exceeded its deadline") from None
        if cleanup_error:
            raise _ProbeCallError(
                f"Codex {safe_operation} failed; {cleanup_error}"
            ) from None
        raise _ProbeCallError(f"Codex {safe_operation} failed") from None

    if len(result) != 1:
        raise _ProbeCallError(f"Codex {safe_operation} returned no result")
    return result[0]


class ReadonlyPermissionConfigTests(unittest.TestCase):
    def _fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        workspace = root / "workspace"
        authority = root / "authority"
        workspace.mkdir()
        authority.mkdir()
        profile = build_readonly_permission_config(
            workspace_root=workspace,
            protected_external_paths=(authority,),
        )
        return temporary, workspace, authority, profile

    def test_generates_and_validates_exact_fail_closed_toml_tables(self) -> None:
        temporary, workspace, authority, profile = self._fixture()
        with temporary:
            data = tomllib.loads(profile.config_toml)
            self.assertEqual(profile.profile_name, DEFAULT_PROFILE_NAME)
            self.assertEqual(profile.workspace_root, str(workspace.resolve()))
            self.assertEqual(profile.protected_external_paths, (str(authority.resolve()),))
            self.assertEqual(data["approval_policy"], "never")
            self.assertEqual(data["default_permissions"], DEFAULT_PROFILE_NAME)
            table = data["permissions"][DEFAULT_PROFILE_NAME]
            self.assertEqual(table["extends"], ":read-only")
            self.assertEqual(table["network"], {"enabled": False})
            self.assertEqual(table["filesystem"][":root"], "deny")
            self.assertEqual(table["filesystem"][":minimal"], "read")
            self.assertEqual(table["filesystem"][str(authority.resolve())], "deny")
            self.assertEqual(
                table["filesystem"][":workspace_roots"],
                {
                    ".": "read",
                    ".agentflow": "deny",
                    ".agentflow/controller-state": "deny",
                },
            )
            self.assertEqual(
                validate_readonly_permission_config(
                    profile.config_toml,
                    workspace_root=workspace,
                    protected_external_paths=(authority,),
                ),
                profile,
            )

    def test_rejects_unsafe_profile_and_path_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            authority = root / "authority"
            workspace.mkdir()
            authority.mkdir()
            for profile_name in ("../escape", "bad.name", "-invalid", "x\nvalue"):
                with self.subTest(profile_name=profile_name), self.assertRaises(CodexPermissionConfigError):
                    build_readonly_permission_config(
                        workspace_root=workspace,
                        protected_external_paths=(authority,),
                        profile_name=profile_name,
                    )
            for candidate in ("relative/path", str(workspace / "private"), str(root)):
                with self.subTest(path=candidate), self.assertRaises(CodexPermissionConfigError):
                    build_readonly_permission_config(
                        workspace_root=workspace,
                        protected_external_paths=(candidate,),
                    )
            with self.assertRaises(CodexPermissionConfigError):
                build_readonly_permission_config(
                    workspace_root=workspace,
                    protected_external_paths=str(authority),
                )
            with self.assertRaises(CodexPermissionConfigError):
                build_readonly_permission_config(
                    workspace_root="relative/workspace",
                    protected_external_paths=(authority,),
                )

    def test_shell_probe_is_syntactically_valid_and_rejects_false_denial_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / name for name in (
                "fixture", "inside_token", "inside_lease", "external_capability",
                "external_lease", "symlink_alias", "workspace_write",
            )}
            script = _build_shell_probe(paths)
            checked = subprocess.run(
                ["/bin/sh", "-n", "-c", script],
                capture_output=True,
                timeout=2,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, "generated shell probe syntax is invalid")
        self.assertEqual(_parse_probe_results(json.dumps(_EXPECTED_PROBE_RESULTS)), _EXPECTED_PROBE_RESULTS)
        for status in ("missing", "command_error", "wrong_content", "allowed"):
            false_result = dict(_EXPECTED_PROBE_RESULTS, external_capability=status)
            with self.subTest(status=status), self.assertRaises(ValueError):
                _parse_probe_results(json.dumps(false_result))

    def test_rejects_legacy_or_conflicting_caller_configuration(self) -> None:
        for caller in (
            {"sandbox_mode": "read-only"},
            {"sandbox_workspace_write": {"network_access": False}},
            {"profiles": {"old": {"sandbox_mode": "read-only"}}},
            {"permissions": {"other": {"extends": ":read-only"}}},
            'approval_policy = "on-request"',
        ):
            with self.subTest(caller=type(caller).__name__), self.assertRaises(CodexPermissionConfigError):
                validate_caller_config(caller)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            authority = root / "authority"
            workspace.mkdir()
            authority.mkdir()
            with self.assertRaises(CodexPermissionConfigError):
                build_readonly_permission_config(
                    workspace_root=workspace,
                    protected_external_paths=(authority,),
                    caller_config={"nested": {"sandbox_mode": "read-only"}},
                )

    def test_validator_rejects_weakened_or_ambiguous_rules(self) -> None:
        temporary, workspace, authority, profile = self._fixture()
        with temporary:
            variants = (
                profile.config_toml.replace('approval_policy = "never"', 'approval_policy = "on-request"'),
                profile.config_toml.replace('enabled = false', 'enabled = true'),
                profile.config_toml.replace('".agentflow" = "deny"', '".agentflow" = "read"'),
                profile.config_toml.replace('".agentflow/controller-state" = "deny"', ''),
                profile.config_toml + '\nsandbox_mode = "read-only"\n',
            )
            for candidate in variants:
                with self.subTest(candidate=candidate[-40:]), self.assertRaises(CodexPermissionConfigError):
                    validate_readonly_permission_config(
                        candidate,
                        workspace_root=workspace,
                        protected_external_paths=(authority,),
                    )


class ProbeBoundaryTests(unittest.TestCase):
    def test_only_absent_top_level_sdk_is_optional(self) -> None:
        def missing_package(_name):
            raise ModuleNotFoundError("synthetic absent package", name="openai_codex")

        with self.assertRaises(_OptionalProbeSdkUnavailable):
            _load_probe_sdk(missing_package)

        def broken_generated_module(name):
            if name == "openai_codex":
                return SimpleNamespace(__version__="0.160.1")
            if name == "openai_codex.client":
                return SimpleNamespace(CodexClient=object, CodexConfig=object)
            raise ModuleNotFoundError(
                "synthetic private path /tmp/private/controller-state", name=name,
            )

        with self.assertRaises(_ProbeSdkImportError) as raised:
            _load_probe_sdk(broken_generated_module)
        self.assertNotIn("private", str(raised.exception))
        self.assertNotIn("controller-state", "".join(traceback.format_exception(raised.exception)))

        def incompatible_package(_name):
            raise ImportError("synthetic secret token /private/auth/config")

        with self.assertRaises(_ProbeSdkImportError) as raised:
            _load_probe_sdk(incompatible_package)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("/private/auth", "".join(traceback.format_exception(raised.exception)))

    def test_bounded_call_failure_closes_and_redacts_provider_exception(self) -> None:
        class FakeClient:
            closed = False

            def close(self):
                self.closed = True

        client = FakeClient()
        private_detail = "/private/controller-state/fake-token"

        def fail_with_private_detail():
            raise RuntimeError(f"failed to read {private_detail}")

        with self.assertRaises(_ProbeCallError) as raised:
            _bounded_sdk_call(client, "command/exec", fail_with_private_detail, 1)
        self.assertTrue(client.closed)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(private_detail, formatted)
        self.assertIn("Codex command/exec failed", str(raised.exception))

    def test_bounded_call_timeout_closes_client_and_never_returns_success(self) -> None:
        class FakeClient:
            def __init__(self):
                self.closed = False
                self.release_operation = threading.Event()
                self.operation_finished = threading.Event()

            def close(self):
                self.closed = True
                self.release_operation.set()

        client = FakeClient()

        def blocked_operation():
            client.release_operation.wait(2)
            client.operation_finished.set()
            return "must not be returned as success"

        with self.assertRaises(_ProbeCallError) as raised:
            _bounded_sdk_call(
                client,
                "initialize",
                blocked_operation,
                0.02,
                cleanup_timeout_seconds=0.2,
                settle_timeout_seconds=0.2,
            )
        self.assertTrue(client.closed)
        self.assertTrue(client.operation_finished.is_set())
        self.assertIn("exceeded its deadline", str(raised.exception))

    def test_bounded_call_cleanup_timeout_is_bounded_and_redacted(self) -> None:
        class FakeClient:
            def __init__(self):
                self.release_close = threading.Event()
                self.release_operation = threading.Event()

            def close(self):
                self.release_close.wait(2)
                raise RuntimeError("synthetic secret /private/codex/home/fake-token")

        client = FakeClient()
        started = time.monotonic()
        try:
            with self.assertRaises(_ProbeCallError) as raised:
                _bounded_sdk_call(
                    client,
                    "command/exec",
                    lambda: client.release_operation.wait(2),
                    0.02,
                    cleanup_timeout_seconds=0.02,
                    settle_timeout_seconds=0.02,
                )
        finally:
            client.release_close.set()
            client.release_operation.set()
        self.assertLess(time.monotonic() - started, 1)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn("synthetic secret", formatted)
        self.assertNotIn("fake-token", formatted)
        self.assertIn("client cleanup exceeded its deadline", str(raised.exception))


@unittest.skipUnless(
    os.environ.get("AGENTFLOW_CODEX_PERMISSION_PROBE") == "1",
    "set AGENTFLOW_CODEX_PERMISSION_PROBE=1 to run bounded local Codex isolation probe",
)
class CodexReadonlyShellIsolationProbe(unittest.TestCase):
    """Direct command/exec canary only; this does not exercise model tools."""

    def test_typed_app_server_command_exec_enforces_synthetic_profile(self) -> None:
        try:
            (
                openai_codex,
                CodexClient,
                CodexConfig,
                CommandExecParams,
                CommandExecResponse,
            ) = _load_probe_sdk()
        except _OptionalProbeSdkUnavailable as exc:
            self.skipTest(str(exc))
        except _ProbeSdkImportError as exc:
            self.fail(str(exc))
        from agentflow.codex_app_server import SUPPORTED_SDK_VERSION

        self.assertEqual(openai_codex.__version__, SUPPORTED_SDK_VERSION)
        with tempfile.TemporaryDirectory(prefix="agentflow-codex-permissions-") as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            workspace = root / "workspace"
            authority = root / "authority"
            control = workspace / ".agentflow" / "controller-state"
            for directory in (codex_home, workspace, authority, control):
                directory.mkdir(parents=True, mode=0o700)
            fixture = workspace / "fixture.txt"
            inside_token = control / "fake-token.txt"
            inside_lease = control / "fake-lease.txt"
            external_capability = authority / "fake-capability.txt"
            external_lease = authority / "fake-lease.txt"
            fixture.write_text("SYNTHETIC_FIXTURE", encoding="utf-8")
            inside_token.write_text("FAKE_TOKEN", encoding="utf-8")
            inside_lease.write_text("FAKE_LEASE", encoding="utf-8")
            external_capability.write_text("FAKE_CAPABILITY", encoding="utf-8")
            external_lease.write_text("FAKE_EXTERNAL_LEASE", encoding="utf-8")
            (workspace / "authority-alias").symlink_to(external_capability)
            plan = build_readonly_permission_config(
                workspace_root=workspace,
                protected_external_paths=(authority,),
            )
            (codex_home / "config.toml").write_text(plan.config_toml, encoding="utf-8")

            script_paths = {
                "fixture": fixture,
                "inside_token": inside_token,
                "inside_lease": inside_lease,
                "external_capability": external_capability,
                "external_lease": external_lease,
                "symlink_alias": workspace / "authority-alias",
                "workspace_write": workspace / "write-probe.txt",
            }
            script = _build_shell_probe(script_paths)
            child_env = {
                key: "" for key in os.environ
                if any(marker in key.upper() for marker in (
                    "TOKEN", "KEY", "SECRET", "AUTH", "ACCOUNT", "SESSION", "COOKIE",
                ))
            }
            child_env["CODEX_HOME"] = str(codex_home)
            client = CodexClient(CodexConfig(cwd=str(workspace), env=child_env))
            try:
                _bounded_sdk_call(client, "start", client.start, 8)
                _bounded_sdk_call(client, "initialize", client.initialize, 10)
                params = CommandExecParams(
                    command=["/bin/sh", "-c", script],
                    cwd=str(workspace),
                    timeout_ms=10_000,
                    output_bytes_cap=2048,
                )
                response = _bounded_sdk_call(
                    client,
                    "command/exec",
                    lambda: client.request(
                        "command/exec",
                        params.model_dump(by_alias=True, exclude_none=True),
                        response_model=CommandExecResponse,
                    ),
                    10,
                )
                self.assertEqual(response.exit_code, 0, "direct shell canary process failed")
                try:
                    results = json.loads(response.stdout)
                except Exception as exc:
                    self.fail(f"direct shell canary did not return its fixed status record: {type(exc).__name__}")
                self.assertEqual(fixture.read_text(encoding="utf-8"), "SYNTHETIC_FIXTURE")
                self.assertEqual(inside_token.read_text(encoding="utf-8"), "FAKE_TOKEN")
                self.assertEqual(inside_lease.read_text(encoding="utf-8"), "FAKE_LEASE")
                self.assertEqual(external_capability.read_text(encoding="utf-8"), "FAKE_CAPABILITY")
                self.assertEqual(external_lease.read_text(encoding="utf-8"), "FAKE_EXTERNAL_LEASE")
                self.assertFalse((workspace / "write-probe.txt").exists())
                self.assertEqual(_parse_probe_results(json.dumps(results)), _EXPECTED_PROBE_RESULTS)
            finally:
                _close_probe_client_bounded(client, 3)


if __name__ == "__main__":
    unittest.main()
