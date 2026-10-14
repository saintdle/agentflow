from __future__ import annotations

import ast
import builtins
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/trials/codex_preflight_no_inference.py"
SPEC = importlib.util.spec_from_file_location("codex_preflight_no_inference", RUNNER)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
PARENT_CHILD_MODE = "parent-issued-v1"


def _fake_sdk(*, authenticated: bool = False, profile: str = preflight.PROFILE,
              wait: bool = False, close_fails: bool = False, fail_at: str | None = None,
              failure: BaseException | None = None,
              prefix_notifications: list[object] | None = None,
              settings_thread_id: str = "private-thread-id",
              settings_inner_override: object | None = None):
    class Typed:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_dump(self, **_kwargs):
            return dict(self.__dict__)

    class SettingsNotification(Typed):
        pass

    class StartedNotification(Typed):
        pass

    class AccountUpdatedNotification(Typed):
        pass

    class AppListUpdatedNotification(Typed):
        pass

    class Thread(Typed):
        pass

    class ThreadSettings(Typed):
        pass

    class ActivePermissionProfile(Typed):
        pass

    class FakeClient:
        instances = []

        def __init__(self, config):
            self.config = config
            self.calls = []
            self.prefix_notifications = list(prefix_notifications or [])
            self.instances.append(self)

        def fail_if(self, phase):
            if fail_at == phase:
                if failure is not None:
                    raise failure
                raise ValueError("private failure detail must not escape")

        def start(self):
            self.calls.append("start")
            self.fail_if("client_start")

        def initialize(self):
            self.calls.append("initialize")
            self.fail_if("initialize")

        def request(self, method, _params, *, response_model):
            self.calls.append(method)
            self.fail_if("config_read")
            return SimpleNamespace(config={
                "default_permissions": preflight.PROFILE,
                "model": preflight.MODEL,
                "model_reasoning_effort": preflight.EFFORT,
                "approval_policy": "never",
            }, layers=[])

        def account_read(self, params):
            self.calls.append(("account_read", params.refresh_token))
            self.fail_if("account_read")
            account = SimpleNamespace(
                root=SimpleNamespace(type="chatgpt", plan_type="pro"),
            ) if authenticated else None
            # A valid ChatGPT account may correctly report that it requires
            # OpenAI authentication; the boolean is not an auth-presence test.
            return SimpleNamespace(account=account, requires_openai_auth=True)

        def thread_start(self, params):
            self.calls.append("thread_start")
            self.fail_if("thread_start")
            self.params = params
            return SimpleNamespace(
                thread=SimpleNamespace(id="private-thread-id"), model=preflight.MODEL,
                cwd=str(Path.cwd().resolve()), instruction_sources=[],
            )

        def next_notification(self):
            self.calls.append("next_notification")
            self.fail_if("settings_notification")
            if wait:
                import threading
                threading.Event().wait(0.05)
            notification_index = self.calls.count("next_notification")
            if notification_index <= len(self.prefix_notifications):
                event = self.prefix_notifications[notification_index - 1]
                if type(event) is tuple and len(event) == 2:
                    method, payload = event
                    return SimpleNamespace(method=method, payload=payload)
                return event
            if notification_index == len(self.prefix_notifications) + 1:
                return SimpleNamespace(
                    method="thread/started",
                    payload=StartedNotification(thread=Thread(id="private-thread-id")),
                )
            settings = SimpleNamespace(
                active_permission_profile=ActivePermissionProfile(id=profile, extends=":read-only"),
                cwd=str(Path.cwd().resolve()), model=preflight.MODEL, effort=preflight.EFFORT,
                approval_policy="never",
            )
            thread_settings = ThreadSettings(**settings.__dict__)
            if settings_inner_override is not None:
                thread_settings = settings_inner_override
            return SimpleNamespace(
                method="thread/settings/updated",
                payload=SettingsNotification(
                    thread_id=settings_thread_id,
                    thread_settings=thread_settings,
                ),
            )

        def close(self):
            self.calls.append("close")
            if close_fails:
                raise RuntimeError("private close detail")

        def __getattr__(self, name):
            if name.startswith(("turn", "login", "logout", "thread_resume", "thread_compact", "command_exec")):
                raise AssertionError(f"forbidden SDK API accessed: {name}")
            raise AttributeError(name)

    sdk = SimpleNamespace(
        __version__=preflight.SDK_PIN,
        AccountUpdatedNotification=AccountUpdatedNotification,
        AppListUpdatedNotification=AppListUpdatedNotification,
        Thread=Thread,
        ThreadSettings=ThreadSettings,
        ActivePermissionProfile=ActivePermissionProfile,
        ThreadStartedNotification=StartedNotification,
        ThreadSettingsUpdatedNotification=SettingsNotification,
    )
    config = type("Config", (), {"__init__": lambda self, **values: self.__dict__.update(values)})
    approval = type("Approval", (), {"__init__": lambda self, **values: self.__dict__.update(values)})
    approval_value = SimpleNamespace(never="never")
    return (sdk, FakeClient, config, approval, approval_value, Typed, Typed,
            Typed, SettingsNotification, Typed, StartedNotification,
            AccountUpdatedNotification, AppListUpdatedNotification, Thread,
            ThreadSettings, ActivePermissionProfile), FakeClient


def _trial_context(root: Path, attempt: int = 1):
    root.mkdir(mode=0o700)
    (root / ".trial-marker").write_text("codex-preflight-v1\n", encoding="utf-8")
    os.chmod(root / ".trial-marker", 0o600)
    for name in ("home", "codex_home", "config", "cache", "tmp", "authority"):
        (root / name).mkdir(mode=0o700)
    workspace, _authority, env, _config_hash, _fixture_hash = preflight._prepare_trial(root, attempt)
    return workspace, env


def _parent_child_context(root: Path, token: str = "a" * 64):
    workspace, env = _trial_context(root)
    fd, attempt = preflight._reserve_attempt(root / "authority")
    os.close(fd)
    if attempt != 1:
        raise AssertionError("fake parent reservation was not the first attempt")
    env.update({
        "AGENTFLOW_PREFLIGHT_WORKSPACE": str(workspace),
        "AGENTFLOW_PREFLIGHT_NORMAL_CODEX_HOME": str(preflight._host_codex_home()),
        "AGENTFLOW_PREFLIGHT_CHILD_MODE": PARENT_CHILD_MODE,
        "AGENTFLOW_PREFLIGHT_CHILD_TOKEN": token,
    })
    return workspace, env, token


def _child_result(**changes):
    value = {
        "schema": preflight.RESULT_SCHEMA, "status": "blocked", "code": "authentication_required",
        "authenticated": False, "thread_start_attempted": False, "profile_observed": False,
        "thread_id_present": False, "instruction_sources_empty": None,
        "inventory_status": "unverified", "cleanup_ok": True, "elapsed_ms": 1,
        "failure_phase": None, "failure_kind": None, "failure_category": None,
        "notification_count": 0, "startup_notifications_skipped": 0,
        "first_notification_method": None, "first_notification_payload": None,
        "rejected_notification_method": None, "rejected_notification_payload": None,
        "notification_mismatch_reason": None,
    }
    value.update(changes)
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


class CodexPreflightTests(unittest.TestCase):
    def setUp(self):
        self.sdk_import_attempts = []
        real_import = builtins.__import__

        def deny_sdk_import(name, *args, **kwargs):
            if name == "openai_codex" or name.startswith("openai_codex."):
                self.sdk_import_attempts.append(name)
                raise AssertionError("real SDK import forbidden in fake tests")
            return real_import(name, *args, **kwargs)

        self.loader_denial = mock.patch.object(
            preflight, "_load_sdk", side_effect=AssertionError("real SDK loader forbidden in fake tests")
        )
        self.spawn_denial = mock.patch.object(
            preflight.subprocess, "Popen", side_effect=AssertionError("real process spawn forbidden in fake tests")
        )
        self.import_denial = mock.patch.object(builtins, "__import__", side_effect=deny_sdk_import)
        self.loader_guard = self.loader_denial.start()
        self.spawn_guard = self.spawn_denial.start()
        self.import_denial.start()
        self.addCleanup(self.import_denial.stop)
        self.addCleanup(self.spawn_denial.stop)
        self.addCleanup(self.loader_denial.stop)

    def _invoke_child(self, env, *, input_token, cwd, child_result=73):
        child_calls = []

        def fake_child(_supplied_token):
            child_calls.append(True)
            return child_result

        supplied = (input_token + "\n").encode("ascii") if input_token is not None else b""
        with mock.patch.dict(preflight.main.__globals__, {"_child_main": fake_child}), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(preflight.Path, "cwd", return_value=cwd), \
             mock.patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(supplied))), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = preflight.main(["--child"])
        return code, child_calls, stdout.getvalue()

    def _diagnose_prefix_notification(self, factory, **sdk_options):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            notifications = []
            modules, _FakeClient = _fake_sdk(
                authenticated=True, prefix_notifications=notifications, **sdk_options,
            )
            produced = factory(modules[0])
            if type(produced) is list:
                notifications.extend(produced)
            else:
                notifications.append(produced)
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                return preflight._child_diagnostic(env)

    def test_default_and_help_are_no_side_effects(self):
        with mock.patch.object(preflight.subprocess, "Popen", side_effect=AssertionError("spawned")):
            with mock.patch("sys.stdout") as stdout:
                self.assertEqual(preflight.main([]), 0)
                self.assertIn("not_run", "".join(call.args[0] for call in stdout.write.call_args_list))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                preflight.main(["--help"])
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(preflight.main(["--child"]), 2)

    def test_child_mode_rejects_token_only_direct_invocation_before_fake_child(self):
        token = "a" * 64
        with self.subTest(reason="matching-token-without-parent-mode-or-layout"):
            code, child_calls, output = self._invoke_child(
                {"AGENTFLOW_PREFLIGHT_CHILD_TOKEN": token},
                input_token=token, cwd=Path.cwd(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(child_calls, [])
            self.assertIn("trial_layout_invalid", output)
        self.assertEqual(self.loader_guard.call_count, 0)
        self.assertEqual(self.spawn_guard.call_count, 0)
        self.assertEqual(self.sdk_import_attempts, [])

    def test_valid_parent_layout_reaches_only_the_fake_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, env, token = _parent_child_context(Path(temporary) / "trial")
            code, child_calls, output = self._invoke_child(
                env, input_token=token, cwd=workspace,
            )
        self.assertEqual(code, 73)
        self.assertEqual(child_calls, [True])
        self.assertEqual(output, "")
        self.assertEqual(self.loader_guard.call_count, 0)
        self.assertEqual(self.spawn_guard.call_count, 0)
        self.assertEqual(self.sdk_import_attempts, [])

    def test_child_mode_rejects_layout_environment_and_path_confusion(self):
        mutations = (
            ("mode", lambda _root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_CHILD_MODE="wrong")),
            ("token_malformed", lambda _root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_CHILD_TOKEN="not-a-token")),
            ("token_mismatch", lambda _root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_CHILD_TOKEN="b" * 64)),
            ("unexpected_credential", lambda _root, _workspace, env: env.update(OPENAI_API_KEY="must-not-pass")),
            ("workspace_mismatch", lambda _root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_WORKSPACE=str(_root))),
            ("normal_auth_overlap", lambda root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_NORMAL_CODEX_HOME=str(root))),
            ("normal_auth_identity_forged", lambda root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_NORMAL_CODEX_HOME=str(root.parent / "fake-codex-home"))),
            ("config_path_mismatch", lambda root, _workspace, env: env.update(AGENTFLOW_PREFLIGHT_CONFIG=str(root / "home" / "config.toml"))),
        )
        for name, mutate in mutations:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "trial"
                workspace, env, token = _parent_child_context(root)
                mutate(root, workspace, env)
                input_token = token if name != "token_mismatch" else token
                code, child_calls, output = self._invoke_child(
                    env, input_token=input_token, cwd=workspace,
                )
                self.assertEqual(code, 2)
                self.assertEqual(child_calls, [])
                self.assertIn("trial_layout_invalid", output)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env, token = _parent_child_context(root)
            (root / "config" / "untrusted-link").symlink_to(workspace, target_is_directory=True)
            code, child_calls, output = self._invoke_child(env, input_token=token, cwd=workspace)
            self.assertEqual(code, 2)
            self.assertEqual(child_calls, [])
            self.assertIn("trial_layout_invalid", output)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env, token = _parent_child_context(root)
            (root / "config" / "permission-profile.toml").write_text("[unexpected]\n", encoding="utf-8")
            os.chmod(root / "config" / "permission-profile.toml", 0o600)
            code, child_calls, output = self._invoke_child(env, input_token=token, cwd=workspace)
            self.assertEqual(code, 2)
            self.assertEqual(child_calls, [])
            self.assertIn("trial_layout_invalid", output)

        token = "a" * 64
        workspace = ROOT / "scripts" / "trials" / "workspace-01"
        root = workspace.parent
        project_env = {
            "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex_home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "TMPDIR": str(root / "tmp"), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
            "AGENTFLOW_PREFLIGHT_CONFIG": str(root / "config" / "permission-profile.toml"),
            "AGENTFLOW_PREFLIGHT_WORKSPACE": str(workspace),
            "AGENTFLOW_PREFLIGHT_NORMAL_CODEX_HOME": str(preflight._host_codex_home()),
            "AGENTFLOW_PREFLIGHT_CHILD_MODE": PARENT_CHILD_MODE,
            "AGENTFLOW_PREFLIGHT_CHILD_TOKEN": token,
        }
        code, child_calls, output = self._invoke_child(project_env, input_token=token, cwd=workspace)
        self.assertEqual(code, 2)
        self.assertEqual(child_calls, [])
        self.assertIn("trial_layout_invalid", output)

        self.assertEqual(self.loader_guard.call_count, 0)
        self.assertEqual(self.spawn_guard.call_count, 0)
        self.assertEqual(self.sdk_import_attempts, [])

    def test_darwin_text_encoding_marker_is_platform_and_value_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, env, token = _parent_child_context(Path(temporary) / "trial")
            user_id = os.getuid()
            valid_markers = (
                ("decimal_selectors", f"0x{user_id:X}:0:1"),
                ("hex_selectors", f"0x{user_id:X}:0x0:0x1"),
                ("mixed_selectors", f"0x{user_id:X}:0:0x1"),
                ("hexadecimal_limit", f"0x{user_id:X}:0xFFFF:65535"),
            )
            invalid_markers = (
                ("malformed_fields", "malformed"),
                ("wrong_uid", f"0x{user_id + 1:X}:0x0:0x1"),
                ("malformed_hex", f"0x{user_id:X}:0xGG:0"),
                ("uppercase_hex_prefix", f"0x{user_id:X}:0X1:0"),
                ("hex_overflow", f"0x{user_id:X}:0x10000:0"),
                ("decimal_overflow", f"0x{user_id:X}:65536:0"),
                ("hex_too_wide", f"0x{user_id:X}:0x00000:0"),
                ("decimal_leading_zero", f"0x{user_id:X}:00:0"),
                ("non_ascii_selector", f"0x{user_id:X}:１:0"),
                ("missing_field", f"0x{user_id:X}:0"),
                ("extra_field", f"0x{user_id:X}:0:1:0"),
            )
            with mock.patch.object(preflight.sys, "platform", "darwin"), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                for label, marker in valid_markers:
                    with self.subTest(case=label):
                        env[preflight.DARWIN_TEXT_ENCODING_ENV] = marker
                        preflight._validate_child_context(env, token)
                for label, marker in invalid_markers:
                    with self.subTest(case=label):
                        env[preflight.DARWIN_TEXT_ENCODING_ENV] = marker
                        with self.assertRaises(preflight.Halt) as raised:
                            preflight._validate_child_context(env, token)
                        self.assertEqual(raised.exception.code, "trial_layout_invalid")
                env[preflight.DARWIN_TEXT_ENCODING_ENV] = valid_markers[0][1]
                env["UNRELATED_RUNTIME_VALUE"] = "rejected"
                with self.assertRaises(preflight.Halt) as raised:
                    preflight._validate_child_context(env, token)
                self.assertEqual(raised.exception.code, "trial_layout_invalid")

            env.pop("UNRELATED_RUNTIME_VALUE")
            with mock.patch.object(preflight.sys, "platform", "linux"):
                with self.assertRaises(preflight.Halt) as raised:
                    preflight._validate_child_context(env, token)
                self.assertEqual(raised.exception.code, "trial_layout_invalid")

    def test_prepare_trial_synthesizes_canonical_darwin_marker(self):
        user_id = os.getuid()
        canonical = f"0x{user_id:X}:0:0"
        ambient_markers = (
            ("unset", None),
            ("malformed", "ambient-marker"),
            ("different_selectors", f"0x{user_id:X}:0:1"),
            ("different_uid", f"0x{user_id + 1:X}:0:0"),
        )
        for label, ambient_marker in ambient_markers:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.object(preflight.sys, "platform", "darwin"), \
                     mock.patch.dict(os.environ):
                    if ambient_marker is None:
                        os.environ.pop(preflight.DARWIN_TEXT_ENCODING_ENV, None)
                    else:
                        os.environ[preflight.DARWIN_TEXT_ENCODING_ENV] = ambient_marker
                    _workspace, _authority, env, _config_hash, _fixture_hash = (
                        preflight._prepare_trial(Path(temporary) / "trial", 1)
                    )
                self.assertEqual(env[preflight.DARWIN_TEXT_ENCODING_ENV], canonical)

    @unittest.skipUnless(sys.platform == "darwin", "macOS child runtime injects the CoreFoundation marker")
    def test_real_isolated_child_accepts_only_the_runtime_darwin_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env, token = _parent_child_context(root)
            # Reproduce the reported launch: Popen receives only the synthesized
            # environment while macOS adds this one runtime marker to Python -I.
            env.pop(preflight.DARWIN_TEXT_ENCODING_ENV, None)
            child_code = """
import json
import os
import runpy

marker = os.environ.get(__MARKER_ENV__)
field_count = 0 if marker is None else marker.count(":") + 1
if field_count <= 3:
    field_count_bucket = str(field_count)
else:
    field_count_bucket = "4plus"
if marker is None:
    length_bucket = "missing"
elif len(marker) == 0:
    length_bucket = "0"
elif len(marker) <= 16:
    length_bucket = "1-16"
elif len(marker) <= 32:
    length_bucket = "17-32"
elif len(marker) <= 64:
    length_bucket = "33-64"
else:
    length_bucket = "65plus"

parts = marker.split(":") if marker is not None and len(marker) <= 64 else []
uid_matches_current = False
if parts:
    uid_field = parts[0]
    uid_digits = uid_field[2:]
    if (uid_field[:2].lower() == "0x" and 0 < len(uid_digits) <= 16
            and all(character in "0123456789abcdefABCDEF" for character in uid_digits)):
        uid_matches_current = int(uid_digits, 16) == os.getuid()

selector_diagnostics = []
for selector in parts[1:3]:
    if selector.isascii() and selector.isdecimal():
        parsed_selector = int(selector, 10)
        selector_format = "decimal"
    elif (selector[:2].lower() == "0x" and selector[2:]
          and all(character in "0123456789abcdefABCDEF" for character in selector[2:])):
        parsed_selector = int(selector[2:], 16)
        selector_format = "hex"
    else:
        parsed_selector = None
        selector_format = "other"
    selector_diagnostics.append({
        "format": selector_format,
        "within_16bit": None if parsed_selector is None else parsed_selector <= 0xFFFF,
    })

print(json.dumps({
    "field_count_bucket": field_count_bucket,
    "length_bucket": length_bucket,
    "uid_matches_current": uid_matches_current,
    "selectors": selector_diagnostics,
}, sort_keys=True, separators=(",", ":")), flush=True)
ns = runpy.run_path(__RUNNER_PATH__)
ns["_validate_child_context"](dict(os.environ), __CHILD_TOKEN__)
print("validated", flush=True)
""".replace("__MARKER_ENV__", repr(preflight.DARWIN_TEXT_ENCODING_ENV)).replace(
    "__RUNNER_PATH__", repr(str(RUNNER)),
).replace("__CHILD_TOKEN__", repr(token))
            self.spawn_denial.stop()
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", child_code], cwd=workspace, env=env,
                    capture_output=True, text=True, check=False,
                )
            finally:
                self.spawn_guard = self.spawn_denial.start()

        self.assertEqual(completed.returncode, 0, completed.stdout)
        output_lines = completed.stdout.splitlines()
        self.assertEqual(len(output_lines), 2, completed.stdout)
        self.assertEqual(output_lines[-1], "validated", completed.stdout)
        classification = json.loads(output_lines[0])
        self.assertEqual(set(classification), {
            "field_count_bucket", "length_bucket", "uid_matches_current", "selectors",
        })
        self.assertIn(classification["field_count_bucket"], {"0", "1", "2", "3", "4plus"})
        self.assertIn(classification["length_bucket"], {
            "missing", "0", "1-16", "17-32", "33-64", "65plus",
        })
        self.assertIs(type(classification["uid_matches_current"]), bool)
        self.assertLessEqual(len(classification["selectors"]), 2)
        for selector in classification["selectors"]:
            self.assertIn(selector["format"], {"decimal", "hex", "other"})
            if selector["format"] == "other":
                self.assertIsNone(selector["within_16bit"])
            else:
                self.assertIs(type(selector["within_16bit"]), bool)
        self.assertEqual(self.loader_guard.call_count, 0)
        self.assertEqual(self.spawn_guard.call_count, 0)
        self.assertEqual(self.sdk_import_attempts, [])

    def test_parent_trial_paths_normalize_benign_platform_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            physical = base / "physical"
            physical.mkdir()
            alias = base / "alias"
            alias.symlink_to(physical, target_is_directory=True)
            workspace, env, token = _parent_child_context(alias / "trial")
            self.assertEqual(workspace, (physical / "trial" / "workspace-01").resolve())
            self.assertEqual(env["AGENTFLOW_PREFLIGHT_WORKSPACE"], str(workspace))
            code, child_calls, output = self._invoke_child(env, input_token=token, cwd=workspace)
        self.assertEqual(code, 73)
        self.assertEqual(child_calls, [True])
        self.assertEqual(output, "")
        self.assertEqual(self.sdk_import_attempts, [])

    def test_missing_fcntl_does_not_break_help_or_unsupported_platform_halt(self):
        original_import = builtins.__import__
        blocked_imports = []

        def import_without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                blocked_imports.append(name)
                raise ImportError("fcntl intentionally unavailable in this test")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("codex_preflight_no_fcntl", RUNNER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        candidate = importlib.util.module_from_spec(spec)
        with mock.patch.object(builtins, "__import__", side_effect=import_without_fcntl):
            spec.loader.exec_module(candidate)
            with mock.patch.object(candidate, "_load_sdk", side_effect=AssertionError("SDK loader called")), \
                 mock.patch.object(candidate, "_child_main", side_effect=AssertionError("child reached")):
                with mock.patch.object(candidate.subprocess, "Popen", side_effect=AssertionError("spawned")):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        self.assertEqual(candidate.main([]), 0)
                        self.assertIn("not_run", stdout.getvalue())
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as help_exit:
                        candidate.main(["--help"])
                    self.assertEqual(help_exit.exception.code, 0)
                    with mock.patch.object(candidate, "os", SimpleNamespace(name="nt")):
                        output = io.BytesIO()
                        with mock.patch("sys.stdout", SimpleNamespace(buffer=output)):
                            self.assertEqual(candidate.main(["--run", "--trial-dir", "/tmp/unused-trial"]), 2)
                        self.assertIn(b"unsupported_platform", output.getvalue())
        self.assertEqual(blocked_imports, [])

    def test_source_has_no_turn_or_auth_mutation_api_calls(self):
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        forbidden = {"turn", "turn_start", "turn_steer", "turn_interrupt", "thread_resume",
                     "thread_compact", "login", "logout", "command_exec", "run_command"}
        self.assertFalse(called & forbidden)
        self.assertEqual(sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Attribute) and n.func.attr == "thread_start"), 1)

    def test_generated_permission_profile_uses_structure_preserving_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, _authority, _env, _config_hash, _fixture_hash = preflight._prepare_trial(root, 1)
            paths = {name: root / name for name in ("home", "codex_home", "config", "cache", "tmp", "authority")}
            protected_path = root / 'protected path.with:colon "quotes"'
            protected_path.mkdir(mode=0o700)
            paths["home"] = protected_path
            config_text = preflight._render_profile_config(workspace, paths).decode("utf-8")

        parsed_config = tomllib.loads(config_text)
        overrides = preflight._toml_overrides(config_text)
        override_keys = [assignment.partition("=")[0] for assignment in overrides]
        reconstructed = {}
        for assignment in overrides:
            key, separator, _value = assignment.partition("=")
            self.assertTrue(separator)
            self.assertNotIn(".", key)
            parsed_assignment = tomllib.loads(assignment)
            self.assertTrue(len(parsed_assignment) == 1 and key in parsed_assignment)
            reconstructed.update(parsed_assignment)

        profile = parsed_config["permissions"][preflight.PROFILE]
        filesystem = profile["filesystem"]
        workspace_rules = filesystem[":workspace_roots"]
        self.assertTrue(set(override_keys) == set(parsed_config))
        self.assertTrue(len(overrides) == len(parsed_config))
        self.assertTrue(reconstructed == parsed_config)
        self.assertTrue(filesystem[":root"] == "deny")
        self.assertTrue(filesystem[":minimal"] == "read")
        self.assertTrue(workspace_rules == {
            ".": "read", ".agentflow": "deny", ".agentflow/controller-state": "deny",
        })
        self.assertTrue(filesystem.get(str(protected_path.resolve())) == "deny")

    def test_missing_auth_halts_before_thread_start_and_never_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, FakeClient = _fake_sdk()
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "authentication_required")
            self.assertFalse(result["thread_start_attempted"])
            self.assertIn(("account_read", False), FakeClient.instances[-1].calls)
            self.assertNotIn("thread_start", FakeClient.instances[-1].calls)
            self.assertNotIn("OPENAI_API_KEY", FakeClient.instances[-1].config.env)
            self.assertNotIn("AGENTFLOW_PREFLIGHT_CONFIG", FakeClient.instances[-1].config.env)
            self.assertIn('cli_auth_credentials_store="file"', FakeClient.instances[-1].config.config_overrides)
            self.assertNotIn("cli_auth_credentials_store=file", FakeClient.instances[-1].config.config_overrides)

    def test_rpc_exceptions_report_safe_phase_and_kind_not_timeout(self):
        cases = (
            ("client_start", "rpc_failed", "halted", False),
            ("initialize", "rpc_failed", "halted", False),
            ("config_read", "rpc_failed", "halted", False),
            ("account_read", "rpc_failed", "halted", False),
            ("thread_start", "thread_start_ambiguous", "ambiguous", True),
            ("settings_notification", "rpc_failed", "halted", True),
        )
        for phase, code, status, authenticated in cases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "trial"
                workspace, env = _trial_context(root)
                modules, _FakeClient = _fake_sdk(authenticated=authenticated, fail_at=phase)
                with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                     mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                    result = preflight._child_diagnostic(env)
                self.assertEqual(result["code"], code)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["failure_phase"], phase)
                self.assertEqual(result["failure_kind"], "exception")
                self.assertEqual(result["failure_category"], "value_error")
                self.assertNotIn("private failure detail", json.dumps(result))

    def test_initialize_exception_categories_are_fixed_and_redacted(self):
        class FakeCodexError(Exception):
            pass

        class FakeTransportClosedError(FakeCodexError):
            pass

        class FakeJsonRpcError(FakeCodexError):
            pass

        class FakeValidationError(Exception):
            pass

        class HostileCustomError(Exception):
            def __str__(self):
                raise AssertionError("exception text must not be inspected")

        errors_module = SimpleNamespace(
            CodexError=FakeCodexError,
            TransportClosedError=FakeTransportClosedError,
            JsonRpcError=FakeJsonRpcError,
        )
        validation_module = SimpleNamespace(ValidationError=FakeValidationError)
        hostile = HostileCustomError("private message")
        hostile.__cause__ = RuntimeError("private cause")
        hostile.__context__ = ValueError("private context")
        cases = (
            (ValueError("private value"), "value_error"),
            (TypeError("private type"), "type_error"),
            (TimeoutError("private timeout"), "timeout_exception"),
            (OSError("private OS detail"), "os_error"),
            (FakeTransportClosedError("private transport detail"), "sdk_transport_closed"),
            (FakeJsonRpcError("private RPC detail"), "sdk_rpc_error"),
            (FakeCodexError("private SDK detail"), "sdk_error"),
            (FakeValidationError("private schema detail"), "response_validation"),
            (hostile, "unknown"),
        )
        for failure, category in cases:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "trial"
                workspace, env = _trial_context(root)
                modules, _FakeClient = _fake_sdk(fail_at="initialize", failure=failure)
                with mock.patch.dict(sys.modules, {
                    "openai_codex.errors": errors_module,
                    "pydantic": validation_module,
                }), mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                     mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                    result = preflight._child_diagnostic(env)
                self.assertEqual(result["code"], "rpc_failed")
                self.assertEqual(result["failure_phase"], "initialize")
                self.assertEqual(result["failure_kind"], "exception")
                self.assertEqual(result["failure_category"], category)
                rendered = json.dumps(result)
                for private in ("private message", "private cause", "private context",
                                "private transport detail", "private RPC detail"):
                    self.assertNotIn(private, rendered)

    def test_deadline_has_no_invented_exception_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, _FakeClient = _fake_sdk(authenticated=True, wait=True)
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight, "PROFILE_WAIT", 0.005), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["failure_kind"], "deadline")
            self.assertIsNone(result["failure_category"])

    def test_lazy_module_class_lookup_is_inert_and_fails_safe(self):
        lookups = []

        class LazyErrorsModule(ModuleType):
            def __getattr__(self, name):
                lookups.append(name)
                raise RuntimeError("private lazy-module detail")

        class OpaqueCustomError(Exception):
            def __str__(self):
                raise AssertionError("exception text must not be inspected")

        failure = OpaqueCustomError("private exception text")
        failure.__cause__ = RuntimeError("private cause")
        failure.__context__ = ValueError("private context")
        lazy_errors = LazyErrorsModule("openai_codex.errors")

        def fail_once():
            raise failure

        with mock.patch.dict(sys.modules, {"openai_codex.errors": lazy_errors}), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(preflight.RPCFailure) as raised:
                preflight._bounded_call(None, fail_once, 0.5, "initialize")

        self.assertEqual(raised.exception.category, "unknown")
        self.assertEqual(lookups, [])
        self.assertEqual(stderr.getvalue(), "")

    def test_rpc_deadline_is_distinct_from_immediate_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, _FakeClient = _fake_sdk(authenticated=True, wait=True)
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight, "PROFILE_WAIT", 0.005), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "settings_timeout")
            self.assertEqual(result["failure_phase"], "settings_notification")
            self.assertEqual(result["failure_kind"], "deadline")

    def test_profile_mismatch_halts_after_one_typed_thread_and_never_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, FakeClient = _fake_sdk(authenticated=True, profile="other-profile")
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "settings_mismatch")
            self.assertTrue(result["thread_start_attempted"])
            self.assertEqual(FakeClient.instances[-1].calls.count("thread_start"), 1)
            self.assertNotIn("turn_start", FakeClient.instances[-1].calls)

    def test_matching_profile_still_blocks_unknown_tool_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, FakeClient = _fake_sdk(authenticated=True)
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "tool_inventory_unverified")
            self.assertTrue(result["profile_observed"])
            self.assertEqual(result["status"], "halted")
            self.assertNotIn("private-thread-id", json.dumps(result))

    def test_empty_app_list_startup_notification_is_diagnosed_and_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            notifications = []
            modules, _FakeClient = _fake_sdk(
                authenticated=True, prefix_notifications=notifications,
            )
            notifications.append(("app/list/updated", modules[0].AppListUpdatedNotification(data=[])))
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "tool_inventory_unverified")
            self.assertEqual(result["notification_count"], 3)
            self.assertEqual(result["startup_notifications_skipped"], 2)
            self.assertEqual(result["first_notification_method"], "app_list_updated")
            self.assertEqual(result["first_notification_payload"], "app_list_updated")
            self.assertIsNone(result["notification_mismatch_reason"])
            self.assertNotIn("private-thread-id", json.dumps(result))

    def test_typed_account_update_is_skipped_only_when_account_decision_matches(self):
        matching = self._diagnose_prefix_notification(
            lambda sdk: ("account/updated", sdk.AccountUpdatedNotification(
                auth_mode="chatgpt", plan_type="pro",
            )),
        )
        self.assertEqual(matching["code"], "tool_inventory_unverified")
        self.assertEqual(matching["first_notification_method"], "account_updated")
        self.assertEqual(matching["first_notification_payload"], "account_updated")
        self.assertEqual(matching["startup_notifications_skipped"], 2)

        changed_auth = self._diagnose_prefix_notification(
            lambda sdk: ("account/updated", sdk.AccountUpdatedNotification(
                auth_mode="apiKey", plan_type="pro",
            )),
        )
        self.assertEqual(changed_auth["code"], "settings_unsupported")
        self.assertEqual(changed_auth["notification_mismatch_reason"], "account_state_mismatch")
        self.assertNotIn("apiKey", json.dumps(changed_auth))

        changed_plan = self._diagnose_prefix_notification(
            lambda sdk: ("account/updated", sdk.AccountUpdatedNotification(
                auth_mode="chatgpt", plan_type="private-plan-label",
            )),
        )
        self.assertEqual(changed_plan["notification_mismatch_reason"], "account_state_mismatch")
        self.assertNotIn("private-plan-label", json.dumps(changed_plan))

        incomplete_or_changed_states = (
            ("both_null", {"auth_mode": None, "plan_type": None}),
            ("null_auth_mode", {"auth_mode": None, "plan_type": "pro"}),
            ("null_plan_type", {"auth_mode": "chatgpt", "plan_type": None}),
            ("missing_auth_mode", {"plan_type": "pro"}),
            ("missing_plan_type", {"auth_mode": "chatgpt"}),
            ("alternate_chatgpt_mode", {"auth_mode": "chatgptAuthTokens", "plan_type": "pro"}),
        )
        for label, fields in incomplete_or_changed_states:
            with self.subTest(account_update=label):
                result = self._diagnose_prefix_notification(
                    lambda sdk, fields=fields: (
                        "account/updated", sdk.AccountUpdatedNotification(**fields),
                    ),
                )
                self.assertEqual(result["code"], "settings_unsupported")
                self.assertEqual(result["notification_mismatch_reason"], "account_state_mismatch")
                self.assertFalse(result["profile_observed"])

    def test_unknown_malformed_nonempty_and_wrong_target_notifications_fail_closed(self):
        cases = (
            (
                "malformed_app_list",
                lambda sdk: ("app/list/updated", sdk.AppListUpdatedNotification(data=None)),
                "startup_payload_malformed", "app_list_updated", "app_list_updated",
            ),
            (
                "nonempty_app_list",
                lambda sdk: ("app/list/updated", sdk.AppListUpdatedNotification(
                    data=["private-app-name"],
                )),
                "startup_payload_not_empty", "app_list_updated", "app_list_updated",
            ),
            (
                "unknown_method",
                lambda sdk: SimpleNamespace(
                    method="private/credential-like-method",
                    payload=sdk.AppListUpdatedNotification(data=[]),
                    params={"credential": "private-auth-token", "threadId": "private-foreign-thread-id",
                            "detail": "arbitrary-private-detail"},
                ),
                "method_unknown", "unknown", "app_list_updated",
            ),
            (
                "unknown_parser_class",
                lambda _sdk: ("app/list/updated", SimpleNamespace(data=[])),
                "payload_type_mismatch", "app_list_updated", "unknown",
            ),
            (
                "missing_payload",
                lambda _sdk: SimpleNamespace(
                    method="app/list/updated", params={"credential": "private-auth-token"},
                ),
                "payload_type_mismatch", "app_list_updated", "unknown",
            ),
            (
                "wrong_target_thread_started",
                lambda sdk: ("thread/started", sdk.ThreadStartedNotification(
                    thread=sdk.Thread(id="private-foreign-thread-id"),
                )),
                "target_thread_mismatch", "thread_started", "thread_started",
            ),
        )
        for name, factory, reason, method, payload in cases:
            with self.subTest(case=name):
                result = self._diagnose_prefix_notification(factory)
                self.assertEqual(result["code"], "settings_unsupported")
                self.assertEqual(result["notification_mismatch_reason"], reason)
                self.assertEqual(result["rejected_notification_method"], method)
                self.assertEqual(result["rejected_notification_payload"], payload)
                rendered = json.dumps(result)
                for private in (
                    "private/credential-like-method", "private-app-name",
                    "private-foreign-thread-id", "private-thread-id", "credential",
                    "private-auth-token", "arbitrary-private-detail",
                ):
                    self.assertNotIn(private, rendered)

    def test_wrong_target_settings_notification_is_reported_without_thread_id(self):
        result = self._diagnose_prefix_notification(
            lambda sdk: ("account/updated", sdk.AccountUpdatedNotification(
                auth_mode="chatgpt", plan_type="pro",
            )),
            settings_thread_id="private-foreign-thread-id",
        )
        self.assertEqual(result["code"], "settings_mismatch")
        self.assertEqual(result["rejected_notification_method"], "thread_settings_updated")
        self.assertEqual(result["rejected_notification_payload"], "thread_settings_updated")
        self.assertEqual(result["notification_mismatch_reason"], "target_thread_mismatch")
        self.assertNotIn("private-foreign-thread-id", json.dumps(result))

    def test_settings_notification_requires_exact_typed_thread_settings(self):
        result = self._diagnose_prefix_notification(
            lambda sdk: ("account/updated", sdk.AccountUpdatedNotification(
                auth_mode="chatgpt", plan_type="pro",
            )),
            settings_inner_override=SimpleNamespace(
                active_permission_profile=SimpleNamespace(id=preflight.PROFILE, extends=":read-only"),
                cwd="/private/workspace", model=preflight.MODEL,
                effort=preflight.EFFORT, approval_policy="never",
            ),
        )
        self.assertEqual(result["code"], "settings_mismatch")
        self.assertEqual(result["notification_mismatch_reason"], "settings_mismatch")
        self.assertEqual(result["rejected_notification_payload"], "thread_settings_updated")
        self.assertNotIn("/private/workspace", json.dumps(result))

    def test_notification_diagnostic_counters_are_capped(self):
        result = self._diagnose_prefix_notification(
            lambda sdk: [
                ("account/updated", sdk.AccountUpdatedNotification(
                    auth_mode="chatgpt", plan_type="pro",
                ))
                for _ in range(preflight.MAX_NOTIFICATION_EVENTS + 1)
            ],
        )
        self.assertEqual(result["code"], "settings_notification_budget_exhausted")
        self.assertEqual(result["notification_count"], preflight.MAX_NOTIFICATION_EVENTS)
        self.assertEqual(result["startup_notifications_skipped"], preflight.MAX_NOTIFICATION_EVENTS)
        self.assertEqual(result["notification_mismatch_reason"], "notification_limit")

    def test_profile_notification_wait_and_cleanup_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, _FakeClient = _fake_sdk(authenticated=True, wait=True, close_fails=True)
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight, "PROFILE_WAIT", 0.01), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "cleanup_failed")
            self.assertEqual(result["status"], "halted")
            self.assertFalse(result["cleanup_ok"])

    def test_sdk_pin_mismatch_halts_before_client_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            workspace, env = _trial_context(root)
            modules, FakeClient = _fake_sdk()
            modules = (SimpleNamespace(__version__="0.160.2"), *modules[1:])
            with mock.patch.object(preflight, "_load_sdk", return_value=modules), \
                 mock.patch.object(preflight.Path, "cwd", return_value=workspace):
                result = preflight._child_diagnostic(env)
            self.assertEqual(result["code"], "sdk_pin_mismatch")
            self.assertEqual(FakeClient.instances, [])

    def test_run_uses_allowlisted_child_env_and_redacts_private_outputs(self):
        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self, _command, **kwargs):
                self.kwargs = kwargs
                captured.append(kwargs)

            def communicate(self, input=None, timeout=None):
                self.input = input
                self.timeout = timeout
                return _child_result(), None

        with tempfile.TemporaryDirectory() as temporary:
            captured = []
            root = Path(temporary) / "trial"
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret-key", "HTTPS_PROXY": "private-proxy"}), \
                 mock.patch.object(preflight.subprocess, "Popen", FakeProcess), \
                 mock.patch.object(preflight.os, "killpg", side_effect=ProcessLookupError):
                report = preflight._run_trial(root)
            kwargs = captured[0]
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("HTTPS_PROXY", kwargs["env"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(root.resolve() / "codex_home"))
            self.assertTrue(kwargs["env"]["AGENTFLOW_PREFLIGHT_CHILD_TOKEN"])
            self.assertEqual(kwargs["start_new_session"], True)
            self.assertEqual(report["code"], "authentication_required")
            self.assertNotIn(str(root), json.dumps(report))
            self.assertNotIn("secret-key", json.dumps(report))

    def test_nonzero_child_exit_preserves_only_the_sanitized_pre_sdk_halt(self):
        class EarlyHaltProcess:
            pid = 987655
            returncode = 2

            def __init__(self, _command, **_kwargs):
                pass

            def communicate(self, input=None, timeout=None):
                return b'{"status":"halted","code":"trial_layout_invalid"}', None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            with mock.patch.object(preflight.subprocess, "Popen", EarlyHaltProcess), \
                 mock.patch.object(preflight.os, "killpg", side_effect=ProcessLookupError):
                report = preflight._run_trial(root)

            self.assertEqual(report["status"], "halted")
            self.assertEqual(report["code"], "trial_layout_invalid")
            self.assertFalse(report["thread_start_attempted"])
            self.assertFalse(report["profile_observed"])
            state = json.loads((root / "authority" / "attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["attempts"]), 1)
            self.assertEqual(state["attempts"][0]["status"], "halted")

    def test_pre_sdk_child_halt_decoder_rejects_unbounded_diagnostic_shapes(self):
        for payload in (
            b'{"status":"halted","code":"rpc_failed"}',
            b'{"status":"ambiguous","code":"trial_layout_invalid"}',
            b'{"status":"halted","code":"trial_layout_invalid","private":"detail"}',
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                preflight.Halt, "child_result_invalid",
            ):
                preflight._decode_child_preflight_halt(payload)

    def test_timeout_is_durably_ambiguous_and_not_retried(self):
        class TimedOut:
            pid = 876543
            returncode = None

            def __init__(self, *_args, **_kwargs):
                pass

            def communicate(self, input=None, timeout=None):
                raise preflight.subprocess.TimeoutExpired("child", timeout)

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trial"
            with mock.patch.object(preflight.subprocess, "Popen", TimedOut), \
                 mock.patch.object(preflight.os, "killpg", side_effect=ProcessLookupError):
                report = preflight._run_trial(root)
            self.assertEqual(report["status"], "ambiguous")
            self.assertEqual(report["code"], "child_timeout")
            with self.assertRaisesRegex(preflight.Halt, "attempt_state_invalid"):
                preflight._run_trial(root)

    def test_child_result_is_size_bounded_and_rejects_extra_fields(self):
        with self.assertRaisesRegex(preflight.Halt, "child_result_invalid"):
            preflight._decode_child(_child_result(account_id="private"))
        with self.assertRaisesRegex(preflight.Halt, "child_result_invalid"):
            preflight._decode_child(b"x" * (preflight.MAX_EVIDENCE + 1))

    def test_child_result_rejects_invalid_category_tuples_and_keeps_cleanup_override(self):
        invalid = (
            {"failure_phase": "initialize", "failure_kind": "exception", "failure_category": None},
            {"failure_phase": "initialize", "failure_kind": "exception", "failure_category": "private_type"},
            {"failure_phase": "initialize", "failure_kind": "exception", "failure_category": ["unknown"]},
            {"failure_phase": "initialize", "failure_kind": "deadline", "failure_category": "value_error"},
            {"failure_phase": ["initialize"], "failure_kind": "exception", "failure_category": "unknown"},
            {"failure_phase": None, "failure_kind": None, "failure_category": "unknown"},
            {"failure_phase": "thread_start", "failure_kind": "exception",
             "failure_category": "sdk_rpc_error", "thread_start_attempted": False,
             "status": "ambiguous", "code": "thread_start_ambiguous"},
            {"notification_count": True},
            {"notification_count": preflight.MAX_NOTIFICATION_EVENTS + 1},
            {"startup_notifications_skipped": 1},
            {"notification_count": 1, "first_notification_method": "thread_started"},
            {"notification_count": 1, "first_notification_method": "private_method",
             "first_notification_payload": "unknown"},
            {"notification_count": 1, "first_notification_method": "thread_started",
             "first_notification_payload": "thread_started", "rejected_notification_method": "unknown",
             "rejected_notification_payload": "unknown"},
            {"notification_count": 1, "first_notification_method": "thread_started",
             "first_notification_payload": "thread_started", "rejected_notification_method": "unknown",
             "rejected_notification_payload": "unknown", "notification_mismatch_reason": "private"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaisesRegex(preflight.Halt, "child_result_invalid"):
                preflight._decode_child(_child_result(**changes))

        ambiguous = preflight._decode_child(_child_result(
            status="ambiguous", code="thread_start_ambiguous", thread_start_attempted=True,
            failure_phase="thread_start", failure_kind="exception", failure_category="sdk_rpc_error",
        ))
        self.assertTrue(ambiguous["thread_start_attempted"])
        safe_notification = preflight._decode_child(_child_result(
            notification_count=1, startup_notifications_skipped=0,
            first_notification_method="unknown", first_notification_payload="unknown",
            rejected_notification_method="unknown", rejected_notification_payload="unknown",
            notification_mismatch_reason="method_unknown",
        ))
        self.assertEqual(safe_notification["rejected_notification_method"], "unknown")
        self.assertNotIn("private_method", json.dumps(safe_notification))
        cleanup_override = preflight._decode_child(_child_result(
            status="halted", code="cleanup_failed", cleanup_ok=False, thread_start_attempted=True,
            failure_phase="thread_start", failure_kind="exception", failure_category="sdk_rpc_error",
        ))
        self.assertEqual(cleanup_override["code"], "cleanup_failed")


if __name__ == "__main__":
    unittest.main()
