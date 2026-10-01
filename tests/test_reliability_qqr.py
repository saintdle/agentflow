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

    def test_corrupt_binding_registry_keeps_provider_hook_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_home = directory / "state"
            workspace = directory / "workspace"
            state_home.mkdir()
            workspace.mkdir()
            (state_home / "workspace-bindings.json").write_text(
                "{not-json", encoding="utf-8"
            )
            payload = {
                "event": "session.start",
                "session_id": "provider-session",
                "cwd": str(workspace),
                "model": "gpt-5.6-sol",
            }
            runtime = mock.Mock()
            runtime.process.return_value = (None, None, None)
            with mock.patch.dict(
                os.environ, {"AGENTFLOW_STATE_HOME": str(state_home)}, clear=False
            ), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch.object(
                     cli.memory_runtime_backend, "MemoryRuntime", return_value=runtime
                 ), mock.patch.object(cli.beads_backend, "prime", return_value=""):
                self.assertEqual(
                    cli.hook(argparse.Namespace(provider="codex", event="")), 0
                )


class SterileLaunchPackageTests(unittest.TestCase):
    def _create_external_handoff(
        self, root: Path, context: Path, *, required_skills: list[str] | None = None,
        provider: str = "codex",
    ) -> Path:
        (root / "output").mkdir(exist_ok=True)
        output = root / ".agentflow/handoffs/task.md"
        args = argparse.Namespace(
            to=provider, title="Bounded task", goal="Inspect only declared context",
            task_id="task-sterile", task_class="focused-review", role="reviewer",
            artifact_kind="internal", writer_model="", lane="external",
            tool_profile="shell-readonly", output_boundary=str(root / "output"),
            require_tool=[], require_skill=required_skills or [], allow_delegation=False, return_type="result",
            max_ai_credits=30 if provider == "copilot" else None,
            acceptance_matrix="", isolation_profile="none",
            require_asset=[], base="", dependency=[], done_when=["Return evidence"],
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
            with mock.patch.object(
                cli, "_provider_command", return_value="/fake/codex"
            ):
                packaged = cli._package_handoff_sterile(handoff, stage)
            self.assertEqual(cli._validate_sterile_package(stage), packaged)
            names = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
            self.assertFalse(any(name.endswith("AGENTS.md") for name in names))
            self.assertFalse((stage / ".claude/settings.json").exists())
            self.assertFalse((stage / ".github/hooks/agentflow.json").exists())
            packaged_context = next((stage / "context").iterdir())
            packaged_context.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                cli._validate_sterile_package(stage)

    def test_package_contains_only_bundled_session_hook_for_claude_and_copilot(self) -> None:
        cases = (
            (
                "claude", Path(".claude/settings.json"),
                ("templates", "project", "claude-settings.json"), "SessionStart",
            ),
            (
                "copilot", Path(".github/hooks/agentflow.json"),
                ("templates", "project", "copilot-hooks.json"), "sessionStart",
            ),
        )
        for provider, relative_hook, resource_parts, event_name in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                root = directory / "source"
                root.mkdir()
                context = root / "allowed.md"
                context.write_text("bounded evidence\n", encoding="utf-8")
                # Existing project hook instructions are untrusted and must
                # not be carried into the separate provider cwd.
                source_hook = root / relative_hook
                source_hook.parent.mkdir(parents=True)
                source_hook.write_text(
                    json.dumps({"hooks": {event_name: [{"command": "run-untrusted-command"}]}}),
                    encoding="utf-8",
                )
                handoff = self._create_external_handoff(
                    root, context, provider=provider
                )
                stage = directory / "sterile"
                with mock.patch.object(cli, "_provider_command", return_value="/fake/provider"):
                    packaged = cli._package_handoff_sterile(handoff, stage)

                bundled = json.loads(
                    cli.packaged_resources.item(*resource_parts).read_text(encoding="utf-8")
                )
                expected = {"hooks": {event_name: bundled["hooks"][event_name]}}
                if provider == "claude":
                    expected["hooks"]["PostModelSwitch"] = [{
                        "hooks": [{
                            "type": "command",
                            "command": "~/.local/bin/agentflow hook --provider claude --event PostModelSwitch",
                            "timeout": 5,
                        }],
                    }]
                if provider == "copilot":
                    expected["version"] = bundled["version"]
                hook_path = stage / relative_hook
                self.assertEqual(json.loads(hook_path.read_text(encoding="utf-8")), expected)
                self.assertNotIn("run-untrusted-command", hook_path.read_text(encoding="utf-8"))
                self.assertEqual(cli._validate_sterile_package(stage), packaged)
                inventory = json.loads(
                    (stage / ".agentflow/sterile-manifest.json").read_text(encoding="utf-8")
                )["files"]
                inventory_by_path = {entry["path"]: entry["sha256"] for entry in inventory}
                self.assertIn(relative_hook.as_posix(), inventory_by_path)
                self.assertEqual(
                    inventory_by_path[relative_hook.as_posix()],
                    cli._file_sha256(hook_path),
                )
                self.assertEqual(
                    set(json.loads(hook_path.read_text(encoding="utf-8"))["hooks"]),
                    ({event_name, "PostModelSwitch"} if provider == "claude" else {event_name}),
                )

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

    def test_package_rejects_skill_changed_after_source_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            root.mkdir()
            context = root / "allowed.md"
            context.write_text("bounded evidence\n", encoding="utf-8")
            skill = root / ".agents/skills/domain-skill/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: domain-skill\ndescription: approved\n---\nOriginal.\n",
                encoding="utf-8",
            )
            handoff = self._create_external_handoff(
                root, context, required_skills=["domain-skill"]
            )
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    cli.handoff_preflight(
                        argparse.Namespace(
                            file=str(handoff), cwd=str(root), require_matrix=False
                        )
                    ),
                    0,
                )
            skill.write_text(
                "---\nname: domain-skill\ndescription: changed\n---\nChanged.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "skill pin changed after preflight"):
                cli._package_handoff_sterile(handoff, directory / "sterile")

    def test_package_rejects_registered_external_skill_config_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "source"
            external = directory / "external/domain-skill"
            replacement = directory / "replacement/domain-skill"
            root.mkdir()
            for skill_root in (external, replacement):
                skill_root.mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(
                    "---\nname: domain-skill\ndescription: approved\n---\nOriginal.\n",
                    encoding="utf-8",
                )
            shared = cli.project_config_backend.default_data()
            local = cli.project_config_backend.default_local_data()
            local["skills"] = [{
                "name": "domain-skill", "path": str(external), "providers": ["codex"]
            }]
            cli.project_config_backend.write_layer(root, shared, local=False)
            cli.project_config_backend.write_layer(root, local, local=True)
            skill_link = root / ".agents/skills/domain-skill"
            skill_link.parent.mkdir(parents=True)
            skill_link.symlink_to(external, target_is_directory=True)
            context = root / "allowed.md"
            context.write_text("bounded evidence\n", encoding="utf-8")
            handoff = self._create_external_handoff(
                root, context, required_skills=["domain-skill"]
            )
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    cli.handoff_preflight(
                        argparse.Namespace(
                            file=str(handoff), cwd=str(root), require_matrix=False
                        )
                    ),
                    0,
                )

            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"):
                packaged = cli._package_handoff_sterile(
                    handoff, directory / "sterile-ok"
                )
            self.assertEqual(
                cli._validate_sterile_package(directory / "sterile-ok"), packaged
            )

            local["skills"][0]["path"] = str(replacement)
            cli.project_config_backend.write_layer(root, local, local=True)
            with self.assertRaisesRegex(ValueError, "registered source changed after preflight"):
                cli._package_handoff_sterile(handoff, directory / "sterile")


class NativeDirectHandoffTests(unittest.TestCase):
    def test_native_launch_has_no_unavailable_machine_return_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "output"
            output.mkdir()
            handoff = root / ".agentflow/tmp/handoffs/native.md"
            args = argparse.Namespace(
                to="codex", title="Native review", goal="Return a bounded review",
                task_id="native-task", task_class="focused-review", role="review",
                artifact_kind="internal", writer_model="", lane="native",
                tool_profile="shell-readonly", output_boundary=str(output),
                require_tool=[], require_skill=[], allow_delegation=False,
                return_type="result", max_ai_credits=None, acceptance_matrix="",
                isolation_profile="none", require_asset=[], base="", dependency=[],
                done_when=["Return evidence"], context=[], constraint=[], check=[],
                budget=[], issue="", branch="", out=str(handoff), cwd=str(root),
                untrusted_task_data=False,
            )
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(cli.handoff_create(args), 0)
            manifest = json.loads(handoff.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertNotIn("machine_return_contract", manifest)
            self.assertNotIn("AGENTFLOW_RESULT_CONTRACT", handoff.read_text(encoding="utf-8"))
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    cli.handoff_preflight(
                        argparse.Namespace(
                            file=str(handoff), cwd=str(root), require_matrix=False
                        )
                    ),
                    0,
                )
            launch = argparse.Namespace(
                provider="codex", file=str(handoff), cwd=str(root), role="review",
                model="gpt-5.6-sol", effort="high", policy="",
                print_command=False, selective_model=False,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch.object(cli.subprocess, "call", return_value=0) as spawned:
                self.assertEqual(cli.handoff_launch(launch), 0)
            spawned.assert_called_once()


class ProviderModelFidelityTests(unittest.TestCase):
    def test_copilot_model_attestation_fails_closed_even_for_forged_hook_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"AGENTFLOW_STATE_HOME": temporary}, clear=False
        ):
            spool = events.EventSpool(Path(temporary) / "events.jsonl")
            spool.append(events.normalize_event(
                "copilot",
                {"event": "session.start", "session_id": "session-1", "model": "claude-sonnet-5"},
                event_id="forged-model-attestation",
            ))
            with self.assertRaisesRegex(ValueError, "Copilot.*resolved-model evidence.*unsupported"):
                cli._require_attested_model("copilot", "session-1", "claude-sonnet-5")
            with self.assertRaisesRegex(ValueError, "Copilot.*resolved-model evidence.*unsupported"):
                cli._require_attested_model("copilot", "session-missing", "claude-sonnet-5")

    def test_claude_missing_model_on_latest_session_event_invalidates_stale_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"AGENTFLOW_STATE_HOME": temporary}, clear=False
        ):
            spool = events.EventSpool(Path(temporary) / "events.jsonl")
            spool.append(events.normalize_event(
                "claude",
                {"event": "session.start", "session_id": "session-1", "model": "claude-sonnet-5"},
                event_id="claude-model-attestation",
            ))
            spool.append(events.normalize_event(
                "claude",
                {"event": "session.start", "session_id": "session-1"},
                event_id="claude-model-omitted-on-resume",
            ))
            with self.assertRaisesRegex(ValueError, "no usable local lifecycle model evidence"):
                cli._require_attested_model("claude", "session-1", "claude-sonnet-5")

    def test_copilot_herdr_launch_is_rejected_before_provider_lookup_or_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = mock.Mock(id="test-policy", version=1)
            policy.validate_route.return_value = mock.Mock(ok=True, reason="")
            payloads: list[dict] = []
            args = argparse.Namespace(
                root=str(root), provider="copilot", role="coding",
                model="claude-sonnet-5", effort="medium", selective_model=False,
                json=True,
            )
            with mock.patch.object(cli.model_policy_backend, "load_policy", return_value=policy), \
                 mock.patch.object(cli, "_provider_command") as provider_command, \
                 mock.patch.object(cli.subprocess, "run") as spawn, \
                 mock.patch.object(cli, "_json_or_status", side_effect=lambda value, **_: payloads.append(value)):
                self.assertEqual(cli.herdr_launch(args), 2)
            provider_command.assert_not_called()
            spawn.assert_not_called()
            self.assertEqual(len(payloads), 1)
            self.assertFalse(payloads[0]["ok"])
            self.assertRegex(
                payloads[0]["error"],
                r"Copilot.*actual-model attestation.*unsupported.*before provider spawn",
            )
            self.assertFalse((root / ".agentflow/herdr/sessions.json").exists())


if __name__ == "__main__":
    unittest.main()
