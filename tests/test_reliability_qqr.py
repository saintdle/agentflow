from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli, events, workspace_binding
from tests import _state_home  # noqa: F401


class WorkflowWorkspaceBindingTests(unittest.TestCase):
    def test_controller_session_binding_overrides_editor_cwd_and_rejects_stale_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "workflow"
            wrong = directory / "skill-repository"
            root.mkdir()
            wrong.mkdir()
            workflow = "workflow-qqr"
            controller = cli.controller_backend.RootController(
                str(root), "controller", state_path=cli._controller_state_dir(root, workflow) / "state.json"
            )
            lease = controller.acquire()
            state_home = directory / "state"
            with mock.patch.dict(
                os.environ,
                {
                    "AGENTFLOW_STATE_HOME": str(state_home),
                    "CODEX_THREAD_ID": "private-thread-id",
                    "CODEX_SESSION_ID": "provider-session-id",
                },
                clear=False,
            ):
                cli._bind_current_controller_sessions(root, workflow, lease)
                registry = (state_home / "workspace-bindings.json").read_text(encoding="utf-8")
                self.assertNotIn("private-thread-id", registry)
                self.assertNotIn("provider-session-id", registry)
                self.assertEqual(
                    cli._validated_bound_workspace(
                        "codex", {"session_id": "private-thread-id", "cwd": str(wrong)}
                    ),
                    root.resolve(),
                )
                self.assertEqual(
                    cli._validated_bound_workspace(
                        "codex", {"session_id": "provider-session-id", "cwd": str(wrong)}
                    ),
                    root.resolve(),
                )
                runtime = mock.Mock()
                runtime.process.return_value = (None, None, None)
                payload = {
                    "event": "session.start",
                    "session_id": "private-thread-id",
                    "cwd": str(wrong),
                    "model": "gpt-5.6-sol",
                }
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                     mock.patch.object(cli.memory_runtime_backend, "MemoryRuntime", return_value=runtime) as runtime_class, \
                     mock.patch.object(cli.beads_backend, "prime", return_value=""), \
                     mock.patch("sys.stdout", io.StringIO()):
                    self.assertEqual(cli.hook(argparse.Namespace(provider="codex", event="")), 0)
                self.assertEqual(runtime_class.call_args.args[0], root.resolve())
                state_path = cli._controller_state_dir(root, workflow) / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["lease"]["continuity_id"] = "superseded"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                self.assertIsNone(
                    cli._validated_bound_workspace(
                        "codex", {"session_id": "private-thread-id", "cwd": str(wrong)}
                    )
                )


class SterileLaunchPackageTests(unittest.TestCase):
    def _create_external_handoff(self, root: Path, context: Path) -> Path:
        output = root / ".agentflow/handoffs/task.md"
        args = argparse.Namespace(
            to="codex", title="Bounded task", goal="Inspect only declared context",
            task_id="task-sterile", task_class="focused-review", role="reviewer",
            artifact_kind="internal", writer_model="", lane="external",
            tool_profile="shell-readonly", output_boundary=str(root / "output"),
            require_tool=[], require_skill=[], allow_delegation=False, return_type="result",
            max_ai_credits=None, acceptance_matrix="", isolation_profile="none",
            require_asset=[], base="main@" + ("a" * 40), dependency=[], done_when=["Return evidence"],
            context=[str(context)], constraint=["Do not inspect other files"], check=[],
            budget=["10 minutes; one retry; stop on blocker"], issue="", branch="", out=str(output), cwd=str(root),
            untrusted_task_data=False,
        )
        self.assertEqual(cli.handoff_create(args), 0)
        return output

    def test_package_contains_only_inventory_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            stage = directory / "sterile"
            root.mkdir()
            context = root / "allowed.md"
            context.write_text("bounded evidence\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("must not leak\n", encoding="utf-8")
            handoff = self._create_external_handoff(root, context)
            packaged = cli._package_handoff_sterile(handoff, stage)
            self.assertEqual(cli._validate_sterile_package(stage), packaged)
            names = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
            self.assertFalse(any(name.endswith("AGENTS.md") for name in names))
            packaged_context = next((stage / "context").iterdir())
            packaged_context.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                cli._validate_sterile_package(stage)

    def test_package_rejects_instruction_file_as_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            root.mkdir()
            instructions = root / "AGENTS.md"
            instructions.write_text("implicit authority\n", encoding="utf-8")
            handoff = self._create_external_handoff(root, instructions)
            with self.assertRaisesRegex(ValueError, "refuses implicit provider instructions"):
                cli._package_handoff_sterile(handoff, directory / "sterile")


class ProviderModelFidelityTests(unittest.TestCase):
    def test_result_model_attestation_matches_exact_route_or_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"AGENTFLOW_STATE_HOME": temporary}, clear=False
        ):
            spool = events.EventSpool(Path(temporary) / "events.jsonl")
            spool.append(events.normalize_event(
                "copilot",
                {"event": "session.start", "session_id": "session-1", "model": "claude-sonnet-5"},
                event_id="model-attestation",
            ))
            cli._require_attested_model("copilot", "session-1", "claude-sonnet-5")
            with self.assertRaisesRegex(ValueError, "provider model mismatch"):
                cli._require_attested_model("copilot", "session-1", "claude-sonnet-4.6")
            with self.assertRaisesRegex(ValueError, "no native model attestation"):
                cli._require_attested_model("copilot", "session-missing", "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
