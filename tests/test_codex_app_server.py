from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli, codex_app_server as app_server
from tests.test_cli import ValidLaunch


class CodexIdentityTests(unittest.TestCase):
    def _identity(self) -> dict[str, object]:
        return {
            "workspace_root": "/workspace", "workflow_root": "wf", "task_id": "task",
            "claim_id": "claim", "lease_epoch": 3, "lease_continuity_id": "continuity",
            "lease_token_sha256": __import__("hashlib").sha256(b"lease").hexdigest(),
            "cwd": "/workspace", "model": "gpt-6-luna", "effort": "medium",
            "thread_id": "thread-1", "turn_id": "turn-1",
            "sdk_version": app_server.SUPPORTED_SDK_VERSION,
            "model_evidence": "codex-app-server-protocol-cooperative",
        }

    def test_same_owner_reattach_accepts_signed_original_lease_but_takeover_does_not(self) -> None:
        snapshot = {
            "workspace_root": "/workspace", "workflow_root": "wf", "task_id": "task",
            "claim_id": "claim", "lease_epoch": 3, "continuity_id": "continuity",
            "lease_id": "lease", "cwd": "/workspace", "model": "gpt-6-luna",
            "effort": "medium",
        }
        value = app_server.bind_recovered_identity(
            self._identity(), launch_snapshot=snapshot, current_continuity_id="continuity",
        )
        self.assertEqual((value.thread_id, value.turn_id), ("thread-1", "turn-1"))
        with self.assertRaises(app_server.CodexAppServerError):
            app_server.bind_recovered_identity(
                self._identity(), launch_snapshot=snapshot, current_continuity_id="new-owner",
            )

    def test_changed_model_or_incomplete_thread_turn_is_rejected(self) -> None:
        expected = dict(
            workspace_root="/workspace", workflow_root="wf", task_id="task", claim_id="claim",
            lease_epoch=3, lease_continuity_id="continuity", lease_token="lease",
            cwd="/workspace", model="gpt-6-luna", effort="medium",
        )
        changed = self._identity()
        changed["model"] = "other-model"
        with self.assertRaises(app_server.CodexAppServerError):
            app_server.bind_identity(changed, **expected)
        incomplete = self._identity()
        incomplete["turn_id"] = ""
        with self.assertRaises(app_server.CodexAppServerError):
            app_server.bind_identity(incomplete, **expected)


class CodexDiagnosticsTests(unittest.TestCase):
    def test_base_import_is_lazy_and_missing_sdk_error_is_redacted(self) -> None:
        with mock.patch.object(
            app_server, "_sdk_modules",
            side_effect=app_server.CodexAppServerError("optional SDK unavailable"),
        ):
            report = app_server.diagnostics_report()
        self.assertEqual(report["sdk_status"], "unavailable")
        self.assertNotIn("optional SDK", json.dumps(report))

    def test_diagnostic_wire_params_are_json_and_allowlist_hides_account_data(self) -> None:
        class Params:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def model_dump(self, **kwargs):
                return {"refreshToken": False} if "refresh_token" in self.kwargs else {}

        class Client:
            def __init__(self, _config):
                self.calls = []

            def start(self):
                pass

            def initialize(self):
                return SimpleNamespace(userAgent="Codex CLI 1.2.3 linux")

            def account_read(self, _params):
                return SimpleNamespace(account=SimpleNamespace(
                    root=SimpleNamespace(type="chatgpt", email="private@example.invalid"),
                ))

            def request(self, method, params, *, response_model):
                self.calls.append((method, params))
                self.assert_dict(params)
                if method == "account/rateLimits/read":
                    return SimpleNamespace(
                        ordinary_usage_allowed=True,
                        rate_limits_by_limit_id={"codex": {"primary": {
                            "usedPercent": 42, "windowDurationMins": 300,
                            "resetsAt": 1234567890, "accountId": "private-id",
                        }}},
                    )
                return SimpleNamespace(summary=SimpleNamespace(
                    lifetime_tokens=12345, peak_daily_tokens=99,
                ))

            def assert_dict(self, params):
                if not isinstance(params, dict):
                    raise AssertionError("typed SDK params were not serialized to JSON")

            def close(self):
                pass

        modules = {
            "api": SimpleNamespace(), "GetAccountParams": Params,
            "GetAccountRateLimitsParams": Params, "GetAccountRateLimitsResponse": object,
            "GetAccountTokenUsageParams": Params, "GetAccountTokenUsageResponse": object,
        }
        sdk = SimpleNamespace(__version__=app_server.SUPPORTED_SDK_VERSION)
        with mock.patch.object(app_server, "_sdk_modules", return_value=(modules, sdk, Client, lambda: object())):
            report = app_server.diagnostics_report()
        self.assertTrue(report["ok"])
        self.assertEqual(report["authentication_type"], "chatgpt")
        self.assertEqual(report["runtime_version"], "1.2.3")
        self.assertEqual(report["quota"]["primary"]["used_percent"], 42)
        self.assertEqual(report["tokens"]["lifetime"], 12345)
        wire = json.dumps(report)
        for private in ("private@example.invalid", "private-id", "accountId"):
            self.assertNotIn(private, wire)


class CodexPermissionAndTurnTests(unittest.TestCase):
    def test_only_shell_readonly_is_supported(self) -> None:
        policy = app_server.permission_for_profile(
            "shell-readonly", cwd="/workspace", output_boundary=".",
        )
        self.assertEqual(policy, {
            "sandbox": "read-only", "network_access": False, "approval_policy": "never",
        })
        for profile in ("shell-write", "no-shell", "provider-default"):
            with self.assertRaises(app_server.CodexAppServerError):
                app_server.permission_for_profile(profile, cwd="/workspace", output_boundary=".")
        with self.assertRaises(app_server.CodexAppServerError):
            app_server.permission_for_profile(
                "shell-readonly", cwd="/workspace", output_boundary=".", sterile=True,
            )

    def test_typed_start_pins_model_cwd_skills_permissions_and_disables_subdelegation(self) -> None:
        captured: dict[str, object] = {}

        class FieldParams:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeThread:
            def __init__(self, _client, thread_id):
                captured["thread_id"] = thread_id

            def turn(self, inputs, **kwargs):
                captured["inputs"] = inputs
                captured["turn"] = kwargs
                return SimpleNamespace(id="turn-1", stream=lambda: iter(()))

        api = SimpleNamespace(
            Sandbox=SimpleNamespace(read_only="read-only"),
            ApprovalMode=SimpleNamespace(deny_all="deny_all"),
            Thread=FakeThread,
        )
        module_values = {
            "api": api, "ThreadStartParams": FieldParams,
            "AskForApproval": FieldParams,
            "AskForApprovalValue": SimpleNamespace(never="never"),
            "ReasoningEffort": lambda value: value,
            "SkillInput": lambda **kwargs: ("skill", kwargs),
            "TextInput": lambda **kwargs: ("text", kwargs),
        }

        class Client:
            def __init__(self, _config):
                pass

            def start(self):
                pass

            def initialize(self):
                pass

            def thread_start(self, params):
                captured["thread_start"] = params.kwargs
                return SimpleNamespace(thread=SimpleNamespace(
                    id="thread-1", model="gpt-6-luna", cwd="/workspace",
                ))

            def close(self):
                pass

        callbacks: list[str] = []
        with mock.patch.object(
            app_server, "_sdk_modules",
            return_value=(module_values, SimpleNamespace(__version__=app_server.SUPPORTED_SDK_VERSION), Client, lambda **kwargs: kwargs),
        ), mock.patch.object(app_server, "_bounded_call", side_effect=lambda _client, function: function()):
            thread_id, turn_id = app_server.start_background_turn(
                cwd="/workspace", model="gpt-6-luna", effort="medium", instruction="exact",
                skills=[("skill-a", "/skills/skill-a/SKILL.md")],
                tool_profile="shell-readonly", output_boundary=".", sterile=False,
                output_schema={"type": "object"}, on_thread=lambda value: callbacks.append("thread:" + value),
                on_turn=lambda value: callbacks.append("turn:" + value),
                on_complete=lambda _observation, _text: None,
            )
        self.assertEqual((thread_id, turn_id), ("thread-1", "turn-1"))
        self.assertEqual(callbacks, ["thread:thread-1", "turn:turn-1"])
        start = captured["thread_start"]
        self.assertEqual(start["model"], "gpt-6-luna")
        self.assertEqual(start["cwd"], "/workspace")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertEqual(start["approval_policy"].kwargs["root"], "never")
        self.assertEqual(start["config"], {"features": {"multi_agent": False}})
        turn = captured["turn"]
        self.assertEqual(turn["model"], "gpt-6-luna")
        self.assertEqual(turn["cwd"], "/workspace")
        self.assertEqual(turn["sandbox"], "read-only")
        self.assertEqual(turn["approval_mode"], "deny_all")
        self.assertIn(("skill", {"name": "skill-a", "path": "/skills/skill-a/SKILL.md"}), captured["inputs"])


class CodexControllerBridgeTests(unittest.TestCase):
    def test_real_launch_glue_collects_through_authenticated_result_and_replay_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ValidLaunch(
                Path(temporary).resolve(), provider="codex", model="gpt-6-luna", effort="medium",
            )
            fixture.task_issue["metadata"]["agentflow"]["tool_profile"] = "shell-readonly"
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            completed = threading.Event()

            def fake_start(**kwargs):
                kwargs["on_thread"]("thread-accepted")
                kwargs["on_turn"]("turn-accepted")
                def finish():
                    kwargs["on_complete"](
                        app_server.TurnObservation("completed"),
                        json.dumps({
                            "outcome": "completed",
                            "acceptance_results": [{
                                "acceptance_id": "R1", "status": "passed",
                                "evidence": "mocked acceptance passed", "source": "provider",
                            }],
                            "evidence": [],
                        }),
                    )
                    completed.set()
                threading.Thread(target=finish, daemon=True).start()
                return "thread-accepted", "turn-accepted"

            args = fixture.launch_args(transport="app-server")
            with fixture.beads_patches(), mock.patch.object(
                cli.codex_app_server_backend, "start_background_turn", side_effect=fake_start,
            ):
                self.assertEqual(cli.herdr_launch(args), 0)
                self.assertTrue(completed.wait(3), "controller collector did not finish")
                first = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
                second = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
            self.assertEqual(first.status, "consumed")
            self.assertEqual(second.status, "pending")
            state = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text())
            record = state["sessions"][fixture.task_id]
            self.assertEqual(record["provider_transport"], "app-server")
            self.assertEqual(record["codex_app_server"]["status"], "completed")
            self.assertEqual(record["result"]["session_id"], "codex-app-server:thread-accepted:turn-accepted")


if __name__ == "__main__":
    unittest.main()
