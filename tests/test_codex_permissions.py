from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
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


@unittest.skipUnless(
    os.environ.get("AGENTFLOW_CODEX_PERMISSION_PROBE") == "1",
    "set AGENTFLOW_CODEX_PERMISSION_PROBE=1 to run bounded local Codex isolation probe",
)
class CodexReadonlyShellIsolationProbe(unittest.TestCase):
    """Direct command/exec canary only; this does not exercise model tools."""

    def _bounded_sdk_call(self, client, operation, call, timeout_seconds):
        result = []
        failure = []

        def invoke():
            try:
                result.append(call())
            except BaseException as exc:  # Keep provider output/errors out of test logs.
                failure.append(exc)

        worker = threading.Thread(target=invoke, name="codex-permission-probe", daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            client.close()
            worker.join(2)
            self.fail(f"Codex {operation} exceeded its bounded probe deadline")
        if failure:
            self.fail(f"Codex {operation} failed ({type(failure[0]).__name__})")
        if len(result) != 1:
            self.fail(f"Codex {operation} returned no result")
        return result[0]

    def test_typed_app_server_command_exec_enforces_synthetic_profile(self) -> None:
        try:
            import openai_codex
            from openai_codex.client import CodexClient, CodexConfig
            from openai_codex.generated.v2_all import CommandExecParams, CommandExecResponse
        except ImportError as exc:
            self.skipTest(f"optional Codex SDK unavailable: {type(exc).__name__}")
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
                self._bounded_sdk_call(client, "start", client.start, 8)
                self._bounded_sdk_call(client, "initialize", client.initialize, 12)
                params = CommandExecParams(
                    command=["/bin/sh", "-c", script],
                    cwd=str(workspace),
                    timeout_ms=10_000,
                    output_bytes_cap=2048,
                )
                response = self._bounded_sdk_call(
                    client,
                    "command/exec",
                    lambda: client.request(
                        "command/exec",
                        params.model_dump(by_alias=True, exclude_none=True),
                        response_model=CommandExecResponse,
                    ),
                    12,
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
                client.close()


if __name__ == "__main__":
    unittest.main()
