from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import copilot_sdk


class CopilotSDKOptionalTests(unittest.TestCase):
    def test_import_is_lazy_and_sdk_and_runtime_versions_are_exact(self) -> None:
        with mock.patch.object(copilot_sdk.importlib.metadata, "version") as version:
            importlib.reload(copilot_sdk)
            version.assert_not_called()

        reject = type("PermissionDecisionReject", (), {"__init__": lambda self, **kwargs: None})
        copilot = types.ModuleType("copilot")
        copilot.CopilotClient = object
        copilot.RuntimeConnection = object
        version_module = types.ModuleType("copilot._cli_version")
        version_module.CLI_VERSION = copilot_sdk.SUPPORTED_RUNTIME_VERSION
        rpc_module = types.ModuleType("copilot.rpc")
        rpc_module.PermissionDecisionReject = reject
        with (
            mock.patch.dict(sys.modules, {
                "copilot": copilot,
                "copilot._cli_version": version_module,
                "copilot.rpc": rpc_module,
            }),
            mock.patch.object(
                copilot_sdk.importlib.metadata, "version",
                return_value=copilot_sdk.SUPPORTED_SDK_VERSION,
            ),
        ):
            self.assertEqual(copilot_sdk._sdk_components()[2], reject)

        with (
            mock.patch.dict(sys.modules, {
                "copilot": copilot,
                "copilot._cli_version": version_module,
                "copilot.rpc": rpc_module,
            }),
            mock.patch.object(copilot_sdk.importlib.metadata, "version", return_value="1.0.16"),
            self.assertRaises(copilot_sdk.CopilotSDKError),
        ):
            copilot_sdk._sdk_components()

    def test_proof_client_is_managed_empty_mode_and_does_not_cache_token(self) -> None:
        class RuntimeConnection:
            connection = None

            @staticmethod
            def for_stdio():
                RuntimeConnection.connection = SimpleNamespace(env=None)
                return RuntimeConnection.connection

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            workspace = parent / "checkout"
            home = parent / "private-home"
            workspace.mkdir()
            with mock.patch.object(copilot_sdk, "_sdk_components", return_value=(Client, RuntimeConnection, object)):
                with mock.patch.dict("os.environ", {
                    "AGENTFLOW_AUTHORITY_SECRET": "must-not-cross-runtime",
                    "GITHUB_TOKEN": "ambient-token-must-not-cross-runtime",
                }):
                    client = copilot_sdk.create_proof_client(
                        base_directory=home,
                        workspace_root=workspace,
                        github_token="ephemeral-test-credential",
                    )
            self.assertIs(client.kwargs["connection"], RuntimeConnection.connection)
            self.assertEqual(client.kwargs["connection"].env["HOME"], str(home.resolve()))
            self.assertEqual(client.kwargs["connection"].env["COPILOT_HOME"], str(home.resolve()))
            self.assertEqual(client.kwargs["connection"].env["COPILOT_PLUGIN_DIR_ONLY"], "true")
            self.assertNotIn("AGENTFLOW_AUTHORITY_SECRET", client.kwargs["connection"].env)
            self.assertNotIn("GITHUB_TOKEN", client.kwargs["connection"].env)
            self.assertEqual(client.kwargs["mode"], "empty")
            self.assertEqual(client.kwargs["base_directory"], str(home.resolve()))
            self.assertEqual(client.kwargs["working_directory"], str(home.resolve()))
            self.assertEqual(client.kwargs["github_token"], "ephemeral-test-credential")
            self.assertFalse(client.kwargs["use_logged_in_user"])
            self.assertEqual(client.kwargs["builtin_plugin_directories"], [])
            self.assertNotIn("env", client.kwargs)
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_proof_run_supplies_empty_allowlist_and_checks_runtime_twice(self) -> None:
        class Session:
            session_id = "session-proof"

            def __init__(self):
                self.handler = None
                self.inventory_calls = 0
                self.rpc = SimpleNamespace(tools=SimpleNamespace(get_current_metadata=self.inventory))

            def on(self, handler):
                self.handler = handler
                return lambda: None

            async def inventory(self):
                self.inventory_calls += 1
                return SimpleNamespace(tools=[])

            async def send_and_wait(self, _prompt):
                self.handler(SimpleNamespace(
                    type="assistant.usage", id="event-proof", agent_id=None,
                    data=SimpleNamespace(
                        model="claude-sonnet-4.6", api_call_id="call-proof",
                        input_tokens=2, output_tokens=1, reasoning_tokens=0, cost=1.0,
                    ),
                ))

            async def disconnect(self):
                return None

        class Client:
            def __init__(self):
                self.session = Session()
                self.session_config = None
                self.stopped = False

            async def start(self):
                return None

            async def create_session(self, **kwargs):
                self.session_config = kwargs
                return self.session

            async def stop(self):
                self.stopped = True

        identity = copilot_sdk.CopilotLaunchIdentity(
            workflow_root="root-1", task_id="task-1", claim_id="claim-1",
            lease_epoch=2, continuity_id="continuity-1", launch_id="launch-1",
            role="writer", requested_model="claude-sonnet-4.6", effort="medium",
        )
        evidence_key = copilot_sdk.derive_evidence_key(
            "controller-authority-test-key-0123456789",
            identity,
            run_nonce="distinct-run-nonce-for-proof-test",
        )
        client = Client()

        async def exercise() -> copilot_sdk.CopilotUsageReport:
            with mock.patch.object(copilot_sdk, "create_proof_client", return_value=client):
                run = await copilot_sdk.CopilotProofRun.open(
                    identity=identity,
                    evidence_key=evidence_key,
                    base_directory=Path("/tmp/private-proof-home"),
                    workspace_root=Path("/tmp/workspace"),
                    github_token="ephemeral-test-credential",
                )
            report = await run.send_once("Reply with a short acknowledgement.")
            await run.close()
            return report

        report = asyncio.run(exercise())
        self.assertTrue(report.verified)
        self.assertFalse(report.persistent_admission)
        self.assertEqual(client.session.inventory_calls, 2)
        self.assertEqual(client.session_config["available_tools"], [])
        self.assertEqual(client.session_config["tools"], [])
        self.assertEqual(client.session_config["mcp_servers"], {})
        for field in (
            "enable_config_discovery", "enable_on_demand_instruction_discovery",
            "enable_file_hooks", "enable_host_git_operations", "enable_session_store",
            "enable_skills",
        ):
            self.assertFalse(client.session_config[field])
        self.assertTrue(client.session_config["skip_custom_instructions"])
        self.assertEqual(client.session_config["included_builtin_skills"], [])
        self.assertTrue(client.stopped)

    def test_runtime_path_overrides_and_workspace_home_fail_closed(self) -> None:
        class RuntimeConnection:
            @staticmethod
            def for_stdio():
                return SimpleNamespace()

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            workspace = parent / "checkout"
            workspace.mkdir()
            with mock.patch.object(copilot_sdk, "_sdk_components", return_value=(Client, RuntimeConnection, object)):
                with self.assertRaises(copilot_sdk.CopilotSDKError):
                    copilot_sdk.create_proof_client(
                        base_directory=workspace / ".copilot",
                        workspace_root=workspace,
                        github_token="ephemeral-test-credential",
                    )
                with mock.patch.dict("os.environ", {"COPILOT_CLI_PATH": "/tmp/unpinned-copilot"}):
                    with self.assertRaises(copilot_sdk.CopilotSDKError):
                        copilot_sdk.create_proof_client(
                            base_directory=parent / "private-home",
                            workspace_root=workspace,
                            github_token="ephemeral-test-credential",
                        )


class CopilotUsageCollectorTests(unittest.TestCase):
    def _identity(self) -> copilot_sdk.CopilotLaunchIdentity:
        return copilot_sdk.CopilotLaunchIdentity(
            workflow_root="root-1", task_id="task-1", claim_id="claim-1",
            lease_epoch=2, continuity_id="continuity-1", launch_id="launch-1",
            role="writer", requested_model="claude-sonnet-4.6", effort="medium",
        )

    def _collector(self) -> copilot_sdk.CopilotUsageCollector:
        evidence_key = copilot_sdk.derive_evidence_key(
            "controller-authority-test-key-0123456789",
            self._identity(),
            run_nonce="one-run-nonce-for-unit-tests",
        )
        return copilot_sdk.CopilotUsageCollector(self._identity(), evidence_key=evidence_key)

    def _attach_and_begin(self, collector: copilot_sdk.CopilotUsageCollector) -> None:
        class Session:
            session_id = "private-session-id"

            def on(self, _handler):
                return lambda: None

        collector.attach(Session())
        collector.record_tool_inventory([])
        collector.begin_request()

    def _usage_event(
        self,
        *,
        model: str = "claude-sonnet-4.6",
        event_id: str = "event-1",
        api_call_id: str = "call-1",
        agent_id: str | None = None,
    ) -> object:
        return SimpleNamespace(
            type="assistant.usage",
            id=event_id,
            agent_id=agent_id,
            data=SimpleNamespace(
                model=model,
                api_call_id=api_call_id,
                input_tokens=16,
                output_tokens=4,
                reasoning_tokens=0,
                cost=1.0,
            ),
        )

    def test_live_event_fixture_binds_launch_session_call_and_actual_model(self) -> None:
        collector = self._collector()
        self._attach_and_begin(collector)
        collector.on_event(self._usage_event())
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        report = collector.report()

        self.assertTrue(report.verified)
        self.assertFalse(report.persistent_admission)
        self.assertEqual(report.evidence_source, "pinned_copilot_runtime_observation")
        self.assertEqual(report.calls[0]["actual_model"], "claude-sonnet-4.6")
        self.assertEqual(report.calls[0]["requested_model"], "claude-sonnet-4.6")
        self.assertEqual(report.calls[0]["call_sequence"], 1)
        self.assertEqual(report.ledger[0]["identity"]["launch_id"], "launch-1")
        self.assertEqual(report.ledger[0]["session_scope"], report.session_scope)
        encoded = json.dumps(report.to_dict())
        self.assertNotIn("private-session-id", encoded)
        self.assertNotIn("call-1", encoded)
        self.assertEqual(
            [row["sequence"] for row in report.ledger],
            list(range(len(report.ledger))),
        )
        changed_run = copilot_sdk.derive_evidence_key(
            "controller-authority-test-key-0123456789",
            self._identity(),
            run_nonce="different-run-nonce-for-unit-tests",
        )
        self.assertNotEqual(changed_run, copilot_sdk.derive_evidence_key(
            "controller-authority-test-key-0123456789",
            self._identity(),
            run_nonce="one-run-nonce-for-unit-tests",
        ))

    def test_actual_model_mismatch_is_recorded_and_fails_closed(self) -> None:
        collector = self._collector()
        self._attach_and_begin(collector)
        collector.on_event(self._usage_event(model="gpt-5.4"))
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        report = collector.report()
        self.assertFalse(report.verified)
        self.assertIn("actual_model_mismatch", report.reason_codes)
        self.assertEqual(report.calls[0]["actual_model"], "gpt-5.4")
        self.assertFalse(report.persistent_admission)

    def test_missing_duplicate_resume_and_tool_events_are_rejected(self) -> None:
        missing = self._collector()
        self._attach_and_begin(missing)
        missing.record_tool_inventory([])
        missing.finish_request(completed=True)
        self.assertIn("usage_event_missing", missing.report().reason_codes)

        duplicate = self._collector()
        self._attach_and_begin(duplicate)
        event = self._usage_event()
        duplicate.on_event(event)
        duplicate.on_event(event)
        duplicate.record_tool_inventory([])
        duplicate.finish_request(completed=True)
        self.assertIn("duplicate_usage_event", duplicate.report().reason_codes)

        for event_type, expected in (
            ("session.resume", "session_resumed_usage_not_replayed"),
            ("tool.execution_start", "tool_or_subagent_activity_observed"),
            ("subagent.started", "tool_or_subagent_activity_observed"),
        ):
            collector = self._collector()
            self._attach_and_begin(collector)
            collector.on_event(SimpleNamespace(type=event_type, id="event-x", data=SimpleNamespace()))
            collector.on_event(self._usage_event())
            collector.record_tool_inventory([])
            collector.finish_request(completed=True)
            self.assertIn(expected, collector.report().reason_codes)

        subagent_usage = self._collector()
        self._attach_and_begin(subagent_usage)
        subagent_usage.on_event(self._usage_event(agent_id="worker-agent-id"))
        subagent_usage.record_tool_inventory([])
        subagent_usage.finish_request(completed=True)
        report = subagent_usage.report()
        self.assertIn("tool_or_subagent_activity_observed", report.reason_codes)
        self.assertNotIn("worker-agent-id", json.dumps(report.to_dict()))

    def test_late_attachment_and_tool_inventory_changes_cannot_verify(self) -> None:
        collector = self._collector()
        with self.assertRaises(copilot_sdk.CopilotSDKError):
            collector.begin_request()
        self.assertIn("request_started_without_fresh_listener", collector._reason_codes)

        attached = self._collector()
        self._attach_and_begin(attached)
        attached.on_event(self._usage_event())
        attached.record_tool_inventory(["builtin:shell"])
        attached.finish_request(completed=True)
        report = attached.report()
        self.assertFalse(report.verified)
        self.assertIn("ambient_or_dynamic_tools_present", report.reason_codes)

    def test_denied_permission_records_only_request_type_and_still_denies(self) -> None:
        reject = type("PermissionDecisionReject", (), {"__init__": lambda self, **kwargs: setattr(self, "kwargs", kwargs)})
        copilot_module = types.ModuleType("copilot")
        rpc_module = types.ModuleType("copilot.rpc")
        rpc_module.PermissionDecisionReject = reject
        collector = self._collector()
        self._attach_and_begin(collector)
        with mock.patch.dict(sys.modules, {"copilot": copilot_module, "copilot.rpc": rpc_module}):
            decision = collector.deny_permission(SimpleNamespace(command="private command"))
        self.assertIsInstance(decision, reject)
        self.assertEqual(decision.kwargs["feedback"], "Permission denied by the Agentflow proof route.")
        collector.on_event(self._usage_event())
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        report = collector.report()
        self.assertEqual(report.denied_permissions, 1)
        self.assertNotIn("private command", json.dumps(report.to_dict()))
        self.assertTrue(report.verified)
        self.assertFalse(report.persistent_admission)

    def test_signature_chain_tampering_is_detected(self) -> None:
        collector = self._collector()
        self._attach_and_begin(collector)
        collector.on_event(self._usage_event())
        collector.record_tool_inventory([])
        collector.finish_request(completed=True)
        collector._rows[3]["actual_model"] = "tampered"
        self.assertFalse(collector._chain_is_valid())
        report = collector.report()
        self.assertFalse(report.verified)
        self.assertIn("signed_chain_invalid", report.reason_codes)


if __name__ == "__main__":
    unittest.main()
