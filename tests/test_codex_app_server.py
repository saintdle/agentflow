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

    def test_diagnostic_constructor_errors_are_redacted(self) -> None:
        sdk = SimpleNamespace(__version__=app_server.SUPPORTED_SDK_VERSION)
        for client_factory, config_type in (
            (None, lambda: (_ for _ in ()).throw(RuntimeError("secret account config"))),
            (lambda _config: (_ for _ in ()).throw(RuntimeError("private client secret")), lambda: object()),
        ):
            with mock.patch.object(
                app_server, "_sdk_modules",
                return_value=({}, sdk, lambda _config: None, config_type),
            ):
                report = app_server.diagnostics_report(client_factory=client_factory)
            self.assertFalse(report["ok"])
            self.assertEqual(report["error_code"], "rpc_failed")
            self.assertNotIn("secret", json.dumps(report))


class CodexPermissionAndTurnTests(unittest.TestCase):
    def test_shell_readonly_fails_closed_without_full_runtime_boundary_proof(self) -> None:
        with self.assertRaisesRegex(app_server.CodexAppServerError, "complete effective-tool inventory"):
            app_server.permission_for_profile(
                "shell-readonly", cwd="/workspace", output_boundary=".",
            )
        for profile in ("shell-write", "no-shell", "provider-default"):
            with self.assertRaises(app_server.CodexAppServerError):
                app_server.permission_for_profile(profile, cwd="/workspace", output_boundary=".")
        with self.assertRaises(app_server.CodexAppServerError):
            app_server.permission_for_profile(
                "shell-readonly", cwd="/workspace", output_boundary=".", sterile=True,
            )

    def test_turn_start_is_blocked_before_sdk_client_or_spend(self) -> None:
        with mock.patch.object(app_server, "_sdk_modules") as sdk:
            with self.assertRaisesRegex(app_server.CodexAppServerError, "complete effective-tool inventory"):
                app_server.start_background_turn(
                    cwd="/workspace", model="gpt-6-luna", effort="medium", instruction="exact",
                    skills=[], tool_profile="shell-readonly", output_boundary=".", sterile=False,
                    output_schema={"type": "object"}, on_thread=lambda _value: None,
                    on_turn=lambda _value: None, on_complete=lambda _observation, _text: None,
                )
        sdk.assert_not_called()

    def test_direct_supervisor_start_cannot_bypass_permission_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            app_server, "_sdk_modules",
        ) as sdk:
            with self.assertRaisesRegex(app_server.CodexAppServerError, "complete effective-tool inventory"):
                app_server.start_supervised_turn(
                    runtime_dir=Path(temporary) / "runtime", request_id="launch",
                    cwd=temporary, model="gpt-6-luna", effort="medium", instruction="exact",
                    skills=[], skill_manifest={"required_skills": [], "resolved_skills": []},
                    tool_profile="shell-readonly", output_boundary=".",
                    output_schema={"type": "object"}, timeout_seconds=30,
                    on_thread=lambda _value: None, on_turn=lambda _value: None,
                )
        sdk.assert_not_called()

    def test_skill_package_is_revalidated_after_thread_ack_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            skill = root / ".agents/skills/domain-skill/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: domain-skill\ndescription: Pinned.\n---\n", encoding="utf-8")
            with mock.patch.object(cli, "_repository_root", return_value=root):
                pin = cli._skill_pin(skill, root)
                manifest = {
                    "required_skills": ["domain-skill"],
                    "resolved_skills": [{
                        "name": "domain-skill", "provider": "codex",
                        "entrypoint": str(skill), **pin,
                    }],
                }
                pinned_inputs = cli._codex_skill_inputs(root, manifest)
                captured: dict[str, object] = {"turn_called": False}

                class FieldParams:
                    def __init__(self, **kwargs):
                        self.kwargs = kwargs

                class FakeThread:
                    def __init__(self, _client, _thread_id):
                        pass

                    def turn(self, *_args, **_kwargs):
                        captured["turn_called"] = True
                        return SimpleNamespace(id="turn-1", stream=lambda: iter(()))

                class Client:
                    def __init__(self, _config):
                        pass

                    def start(self):
                        pass

                    def initialize(self):
                        pass

                    def thread_start(self, _params):
                        return SimpleNamespace(thread=SimpleNamespace(
                            id="thread-1", model="gpt-6-luna", cwd=str(root),
                        ))

                    def close(self):
                        pass

                modules = {
                    "api": SimpleNamespace(
                        Thread=FakeThread, Sandbox=SimpleNamespace(read_only="read-only"),
                        ApprovalMode=SimpleNamespace(deny_all="deny_all"),
                    ),
                    "ThreadStartParams": FieldParams, "AskForApproval": FieldParams,
                    "AskForApprovalValue": SimpleNamespace(never="never"),
                    "ReasoningEffort": lambda value: value,
                    "SkillInput": lambda **kwargs: kwargs,
                    "TextInput": lambda **kwargs: kwargs,
                }

                def mutate_after_thread(_thread_id):
                    skill.write_text("---\nname: domain-skill\ndescription: Changed.\n---\n", encoding="utf-8")

                with mock.patch.object(app_server, "permission_for_profile", return_value={}), \
                     mock.patch.object(app_server, "_sdk_modules", return_value=(modules, SimpleNamespace(__version__=app_server.SUPPORTED_SDK_VERSION), Client, lambda **kwargs: kwargs)), \
                     mock.patch.object(app_server, "_bounded_call", side_effect=lambda _client, function: function()):
                    with self.assertRaisesRegex(ValueError, "changed after preflight"):
                        app_server.start_background_turn(
                            cwd=str(root), model="gpt-6-luna", effort="medium", instruction="exact",
                            skills=pinned_inputs, tool_profile="shell-readonly", output_boundary=".", sterile=False,
                            output_schema={"type": "object"}, on_thread=mutate_after_thread,
                            on_turn=lambda _value: None, on_complete=lambda _observation, _text: None,
                            skill_validator=lambda: cli._codex_skill_inputs(root, manifest),
                        )
                self.assertFalse(captured["turn_called"])

    def test_thread_ack_rechecks_live_claim_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ValidLaunch(
                Path(temporary).resolve(), provider="codex", model="gpt-6-luna",
            )
            fixture.task_issue["metadata"]["agentflow"]["tool_profile"] = "shell-readonly"
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            turn_starts: list[str] = []

            def mutate_claim_then_ack(**kwargs):
                fixture.task_issue["metadata"]["agentflow"]["claim_token"] = "changed-claim-token-012345678901234567890123"
                kwargs["on_thread"]("thread-stale")
                turn_starts.append("turn-started")
                kwargs["on_turn"]("turn-stale")
                return "thread-stale", "turn-stale"

            args = fixture.launch_args(transport="app-server")
            with fixture.beads_patches(), \
                 mock.patch.object(app_server, "permission_for_profile", return_value={}), \
                 mock.patch.object(cli.codex_app_server_backend, "start_supervised_turn", side_effect=mutate_claim_then_ack):
                self.assertEqual(cli.herdr_launch(args), 2)
            self.assertEqual(turn_starts, [])
            self.assertNotEqual(
                fixture.task_issue["metadata"]["agentflow"]["claim_token"],
                fixture.claim_token,
            )

    def test_stream_deadline_records_interrupted_without_blocking_controller(self) -> None:
        closed = threading.Event()
        completed = threading.Event()
        observed = []

        class Client:
            def close(self):
                closed.set()

        class Handle:
            id = "turn-timeout"

            def stream(self):
                threading.Event().wait(2)
                return iter(())

        app_server._watch_turn(
            Client(), Handle(), "thread-timeout",
            lambda observation, _output: (observed.append(observation), completed.set()),
            timeout_seconds=0.02,
        )
        self.assertTrue(completed.wait(0.5))
        self.assertTrue(closed.wait(0.5))
        self.assertEqual(observed[0].status, "interrupted")
        self.assertEqual(observed[0].reason_code, "worker_timeout")


class CodexControllerBridgeTests(unittest.TestCase):
    def test_real_launch_glue_collects_through_authenticated_result_and_replay_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ValidLaunch(
                Path(temporary).resolve(), provider="codex", model="gpt-6-luna", effort="medium",
            )
            fixture.task_issue["metadata"]["agentflow"]["tool_profile"] = "shell-readonly"
            fixture.handoff_path, fixture.handoff, fixture.root_preflight_sha256 = fixture._materialize()
            worker_script = fixture.root / "fake_sdk_worker.py"
            worker_script.write_text(
                "import fcntl, json, sys, time\n"
                "from pathlib import Path\n"
                "request_path=Path(sys.argv[1]); d=request_path.parent; r=json.loads(request_path.read_text())\n"
                "lock=(d/'codex-worker.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                "def save(status, **extra):\n"
                " value={'request_id':r['request_id'],'status':status,**extra}; p=d/'codex-worker.state.json'; t=p.with_suffix('.tmp'); t.write_text(json.dumps(value)); t.replace(p)\n"
                "save('thread_created',thread_id='thread-accepted',model=r['model'],cwd=r['cwd'])\n"
                "deadline=time.time()+3\n"
                "while not (d/'codex-worker.thread-ack').exists() and time.time()<deadline: time.sleep(.01)\n"
                "save('running',thread_id='thread-accepted',turn_id='turn-accepted',model=r['model'],cwd=r['cwd'])\n"
                "time.sleep(.2)\n"
                "out={'outcome':'completed','acceptance_results':[{'acceptance_id':'R1','status':'passed','evidence':'mocked acceptance passed','source':'provider'}],'evidence':[]}\n"
                "(d/'codex-worker.output.json').write_text(json.dumps({'request_id':r['request_id'],'output':json.dumps(out)}))\n"
                "save('completed',thread_id='thread-accepted',turn_id='turn-accepted',model=r['model'],cwd=r['cwd'],rerouted=False,reason_code='',output_available=True)\n"
                "lock.close()\n",
                encoding="utf-8",
            )
            real_start = app_server.start_supervised_turn
            startup_errors = []
            start_arguments = {}

            def fake_sdk_worker(**kwargs):
                try:
                    start_arguments.update(kwargs)
                    return real_start(**kwargs, _worker_command=[sys.executable, str(worker_script)])
                except Exception as exc:
                    startup_errors.append(str(exc))
                    raise

            args = fixture.launch_args(transport="app-server")
            with fixture.beads_patches(), mock.patch.object(
                cli.codex_app_server_backend, "start_supervised_turn", side_effect=fake_sdk_worker,
            ), mock.patch.object(
                app_server, "permission_for_profile", return_value={},
            ), mock.patch.object(
                app_server, "_sdk_modules",
                return_value=({}, SimpleNamespace(__version__=app_server.SUPPORTED_SDK_VERSION), object, lambda **kwargs: kwargs),
            ):
                launch_result = cli.herdr_launch(args)
                self.assertEqual(launch_result, 0, json.dumps(json.loads(
                    (fixture.root / ".agentflow/herdr/sessions.json").read_text()
                )["sessions"][fixture.task_id].get("codex_app_server")) + repr(startup_errors))
                with self.assertRaises(app_server.CodexAppServerError):
                    real_start(**start_arguments, _worker_command=[sys.executable, str(worker_script)])
                state_path = app_server._supervisor_paths(
                    cli._runtime_launch_dir(fixture.root, fixture.workflow_root,
                                            json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text())["sessions"][fixture.task_id]["launch_id"])
                )[1]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    worker_state = json.loads(state_path.read_text())
                    if worker_state.get("status") == "completed":
                        break
                    time.sleep(0.02)
                self.assertEqual(worker_state.get("status"), "completed")
                # This collection occurs after dispatch returned, from durable helper files.
                with app_server._ACTIVE_TURNS_LOCK:
                    app_server._ACTIVE_TURNS.clear()
                self.assertTrue(cli._collect_codex_app_server_result(
                    fixture.root, fixture.workflow_root, fixture.task_id,
                    authority_secret=fixture.authority_secret,
                ))
                first = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
                second = cli._ingest_submitted_result(
                    fixture.root, fixture.task_id, authority_secret=fixture.authority_secret,
                )
            self.assertEqual(first.status, "consumed", first.error)
            self.assertEqual(second.status, "pending")
            state = json.loads((fixture.root / ".agentflow/herdr/sessions.json").read_text())
            record = state["sessions"][fixture.task_id]
            self.assertEqual(record["provider_transport"], "app-server")
            self.assertEqual(record["codex_app_server"]["status"], "completed")
            self.assertEqual(record["result"]["session_id"], "codex-app-server:thread-accepted:turn-accepted")


if __name__ == "__main__":
    unittest.main()
