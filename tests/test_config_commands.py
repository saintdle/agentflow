from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agentflow import cli
from agentflow import config_commands
from agentflow import installation
from agentflow import project_config
from agentflow import resources


def _write_config(root: Path, data: dict, *, local: bool = False) -> Path:
    path = project_config.local_config_path(root) if local else project_config.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


class ConfigCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state"
        manifest_path = self.state / "install/managed-assets.json"
        manifest_patch = mock.patch.object(
            installation, "managed_install_manifest_path", return_value=manifest_path
        )
        manifest_patch.start()
        self.addCleanup(manifest_patch.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capture(self, fn, args) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            result = fn(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_parser_keeps_legacy_wrappers_and_adds_config_commands(self) -> None:
        parser = cli.build_parser()
        status = parser.parse_args(["memory", "status", "--root", str(self.root)])
        show = parser.parse_args(["config", "show", str(self.root)])
        enable = parser.parse_args(["config", "memory", "enable", "--root", str(self.root)])
        hooks = parser.parse_args([
            "config", "hooks", "merge", "--provider", "claude", "--root", str(self.root), "--dry-run"
        ])
        self.assertIs(status.func, cli.memory_status)
        self.assertIs(show.func, cli.config_show)
        self.assertIs(enable.func, cli.memory_toggle)
        self.assertIs(hooks.func, cli.config_hooks_merge)

    def test_dispatch_defaults_only_when_both_config_layers_are_absent(self) -> None:
        loaded = project_config.load_for_dispatch(self.root)
        self.assertEqual(project_config.codex_transport(loaded), "herdr")
        self.assertEqual(project_config.codex_worker_timeout_seconds(loaded), 1800)
        bad = project_config.config_path(self.root)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{ malformed", encoding="utf-8")
        with self.assertRaises(project_config.ConfigError):
            project_config.load_for_dispatch(self.root)

    def test_codex_worker_timeout_is_bounded_and_local_layer_merges_transport(self) -> None:
        shared = project_config.default_data()
        shared["codex"] = {"transport": "app-server", "worker_timeout_seconds": 900}
        _write_config(self.root, shared)
        local = project_config.default_local_data()
        local["codex"] = {"worker_timeout_seconds": 180}
        _write_config(self.root, local, local=True)
        effective = project_config.load(self.root)
        self.assertEqual(project_config.codex_transport(effective), "app-server")
        self.assertEqual(project_config.codex_worker_timeout_seconds(effective), 180)
        for invalid in (0, 86401, True, 1.5, "180"):
            candidate = project_config.default_data()
            candidate["codex"]["worker_timeout_seconds"] = invalid
            self.assertTrue(project_config.validate(candidate, self.root))

    def test_memory_document_json_example_is_complete_and_schema_valid(self) -> None:
        documentation = Path(__file__).resolve().parents[1] / "docs/MEMORY.md"
        text = documentation.read_text(encoding="utf-8")
        marker = text.index("The JSON above is a complete valid minimal schema-v1")
        start = text.rfind("```json\n", 0, marker)
        end = text.index("\n```", start)
        snippet = text[start + len("```json\n"):end]
        data = json.loads(snippet)
        self.assertEqual(project_config.validate(data, self.root), [])

    def test_memory_enable_materializes_complete_defaults_and_private_backup(self) -> None:
        data = project_config.default_data()
        data.pop("memory")
        data["model_policy"] = ".agentflow/custom-policy.json"
        path = _write_config(self.root, data)
        original = path.read_bytes()
        args = argparse.Namespace(
            root=str(self.root), action="enable", local=False, dry_run=False, json=True
        )
        with mock.patch.object(cli.memory_runtime_backend, "state_home", return_value=self.state.resolve()):
            result, stdout, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual((result, stderr), (0, ""))
        output = json.loads(stdout)
        self.assertEqual(output["status"], "updated")
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(updated["memory"]), set(project_config.DEFAULT_MEMORY))
        self.assertTrue(updated["memory"]["enabled"])
        self.assertEqual(updated["model_policy"], ".agentflow/custom-policy.json")
        self.assertEqual(updated["execution"], data["execution"])
        if hasattr(os, "getuid"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            backup = Path(output["backup"])
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)

    def test_memory_dry_run_has_diff_and_makes_no_changes(self) -> None:
        data = project_config.default_data()
        data.pop("memory")
        path = _write_config(self.root, data)
        original = path.read_bytes()
        args = argparse.Namespace(
            root=str(self.root), action="enable", local=False, dry_run=True, json=False
        )
        result, stdout, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("would-update", stdout)
        self.assertIn("@@", stdout)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.state.exists())

    def test_shared_memory_toggle_refuses_shadowed_local_layer(self) -> None:
        shared = project_config.default_data()
        local = project_config.default_local_data()
        local["memory"] = dict(project_config.DEFAULT_MEMORY, enabled=True)
        shared_path = _write_config(self.root, shared)
        local_path = _write_config(self.root, local, local=True)
        before = shared_path.read_bytes()
        args = argparse.Namespace(
            root=str(self.root), action="disable", local=False, dry_run=False, json=False
        )
        result, _, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual(result, 2)
        self.assertIn("use --local", stderr)
        self.assertEqual(shared_path.read_bytes(), before)
        self.assertTrue(json.loads(local_path.read_text(encoding="utf-8"))["memory"]["enabled"])

    def test_local_toggle_validates_both_layers_and_preserves_shared_file(self) -> None:
        shared = project_config.default_data()
        shared_path = _write_config(self.root, shared)
        shared_before = shared_path.read_bytes()
        args = argparse.Namespace(
            root=str(self.root), action="enable", local=True, dry_run=False, json=True
        )
        with mock.patch.object(cli.memory_runtime_backend, "state_home", return_value=self.state.resolve()):
            result, stdout, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual((result, stderr), (0, ""))
        local_path = project_config.local_config_path(self.root)
        local = json.loads(local_path.read_text(encoding="utf-8"))
        self.assertEqual(set(local["memory"]), set(project_config.DEFAULT_MEMORY))
        self.assertTrue(local["memory"]["enabled"])
        self.assertEqual(shared_path.read_bytes(), shared_before)

        bad_local = project_config.default_local_data()
        bad_local["unexpected"] = "do not drop"
        _write_config(self.root, bad_local, local=True)
        shared_before = shared_path.read_bytes()
        args.local = False
        result, _, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual(result, 2)
        self.assertIn("unknown field", stderr)
        self.assertEqual(shared_path.read_bytes(), shared_before)

    def test_target_unknown_fields_are_refused_without_schema_migration(self) -> None:
        data = project_config.default_data()
        data["custom"] = {"keep": True}
        path = _write_config(self.root, data)
        before = path.read_bytes()
        args = argparse.Namespace(
            root=str(self.root), action="enable", local=False, dry_run=False, json=False
        )
        result, _, stderr = self._capture(cli.memory_toggle, args)
        self.assertEqual(result, 2)
        self.assertIn("unknown field", stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_claude_hooks_merge_preserves_metadata_custom_handlers_and_is_idempotent(self) -> None:
        target = self.root / ".claude/settings.json"
        target.parent.mkdir()
        existing = {
            "customTopLevel": {"keep": True},
            "hooks": {
                "SessionStart": [{
                    "matcher": "startup|resume",
                    "customRuleField": "retain",
                    "hooks": [
                        {"type": "command", "command": "custom-session-handler"},
                        {
                            "type": "command",
                            "command": "~/.local/bin/agentflow hook --provider claude --event SessionStart",
                            "timeout": 5,
                        },
                    ],
                }],
                "Stop": [{"customRuleField": True, "hooks": [{"type": "command", "command": "custom-stop"}]}],
            },
        }
        target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        before = target.read_bytes()
        fake_runtime = SimpleNamespace(state_home=lambda: self.state.resolve())
        args = argparse.Namespace(provider="claude", root=str(self.root), dry_run=False, json=False)
        result, stdout, stderr = self._capture(
            lambda value: config_commands.hooks_merge(
                value, installation=installation, memory_runtime=fake_runtime
            ),
            args,
        )
        self.assertEqual((result, stderr), (0, ""))
        merged = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(merged["customTopLevel"], {"keep": True})
        self.assertEqual(merged["hooks"]["SessionStart"][0]["matcher"], "startup|resume")
        self.assertEqual(merged["hooks"]["SessionStart"][0]["customRuleField"], "retain")
        self.assertEqual(merged["hooks"]["SessionStart"][0]["hooks"][0]["command"], "custom-session-handler")
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"], "custom-stop")
        self.assertIn("PostModelSwitch", merged["hooks"])
        self.assertIn("PostToolUseFailure", merged["hooks"])
        self.assertIn("backup:", stdout)
        self.assertNotEqual(target.read_bytes(), before)

        after_first = target.read_bytes()
        result, stdout, stderr = self._capture(
            lambda value: config_commands.hooks_merge(
                value, installation=installation, memory_runtime=fake_runtime
            ),
            args,
        )
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("unchanged", stdout)
        self.assertEqual(target.read_bytes(), after_first)
        manifest = installation.load_managed_install_manifest()
        record = manifest["resources"][installation.managed_destination_key(target.resolve())]
        self.assertEqual(record["kind"], "claude-hooks")
        self.assertIn("PostModelSwitch", record["managed_handlers"])

        packaged_path = self.base / "new-claude-settings.json"
        packaged = json.loads(resources.item(
            "templates", "project", "claude-settings.json"
        ).read_text(encoding="utf-8"))
        packaged["hooks"]["SessionStart"][0]["hooks"][0]["command"] += " --next-version"
        packaged_path.write_text(json.dumps(packaged), encoding="utf-8")
        with mock.patch.object(
            config_commands.packaged_resources, "item", return_value=packaged_path
        ):
            result, _, stderr = self._capture(
                lambda value: config_commands.hooks_merge(
                    value, installation=installation, memory_runtime=fake_runtime
                ),
                args,
            )
        self.assertEqual((result, stderr), (0, ""))
        updated = json.loads(target.read_text(encoding="utf-8"))
        commands = [
            item.get("command")
            for group in updated["hooks"]["SessionStart"]
            for item in group.get("hooks", [])
        ]
        self.assertIn(
            "~/.local/bin/agentflow hook --provider claude --event SessionStart --next-version",
            commands,
        )
        self.assertNotIn(
            "~/.local/bin/agentflow hook --provider claude --event SessionStart",
            commands,
        )
        self.assertIn("custom-session-handler", commands)

    def test_codex_hooks_dry_run_is_non_mutating_and_previewable(self) -> None:
        codex_dir = self.base / "home/.codex"
        codex_dir.mkdir(parents=True)
        target = codex_dir / "hooks.json"
        target.write_text(json.dumps({"custom": 7, "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "user-hook"}]}
        ]}}), encoding="utf-8")
        original = target.read_bytes()
        args = argparse.Namespace(provider="codex", root=str(self.root), dry_run=True, json=True)
        fake_runtime = SimpleNamespace(state_home=lambda: self.state.resolve())
        with (
            mock.patch.object(Path, "home", return_value=(self.base / "home").resolve()),
            mock.patch.object(installation, "load_managed_install_manifest", return_value={"resources": {}}),
        ):
            result, stdout, stderr = self._capture(
                lambda value: config_commands.hooks_merge(
                    value, installation=installation, memory_runtime=fake_runtime
                ),
                args,
            )
        self.assertEqual((result, stderr), (0, ""))
        output = json.loads(stdout)
        self.assertEqual(output["status"], "would-merge")
        self.assertIn("PostToolUseFailure", output["diff"])
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.state.exists())

    def test_codex_merge_preserves_agentflow_herdr_and_custom_handlers(self) -> None:
        codex_dir = self.base / "home/.codex"
        codex_dir.mkdir(parents=True)
        target = codex_dir / "hooks.json"
        agentflow_start = {
            "type": "command",
            "command": "~/.local/bin/agentflow hook --provider codex --event SessionStart",
            "statusMessage": "Loading Agentflow context",
            "timeout": 5,
        }
        herdr_start = {"type": "command", "command": "herdr session start"}
        existing = {
            "description": "User-owned Codex hooks",
            "customMetadata": {"keep": True},
            "hooks": {
                "SessionStart": [{
                    "matcher": "startup|resume",
                    "userMatcherMetadata": "retain",
                    "hooks": [agentflow_start, herdr_start],
                }],
                "Stop": [{"hooks": [{"type": "command", "command": "herdr session stop"}]}],
                "CustomEvent": [{
                    "matcher": "custom",
                    "hooks": [{"type": "command", "command": "user custom event"}],
                }],
            },
        }
        target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        fake_runtime = SimpleNamespace(state_home=lambda: self.state.resolve())
        args = argparse.Namespace(provider="codex", root=str(self.root), dry_run=False, json=False)
        with mock.patch.object(Path, "home", return_value=(self.base / "home").resolve()):
            result, stdout, stderr = self._capture(
                lambda value: config_commands.hooks_merge(
                    value, installation=installation, memory_runtime=fake_runtime
                ),
                args,
            )
            self.assertEqual((result, stderr), (0, ""))
            self.assertIn("managed hook ownership receipt updated", stdout)
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["description"], "User-owned Codex hooks")
            self.assertEqual(merged["customMetadata"], {"keep": True})
            start = merged["hooks"]["SessionStart"][0]
            self.assertEqual(start["matcher"], "startup|resume")
            self.assertEqual(start["userMatcherMetadata"], "retain")
            self.assertEqual(start["hooks"].count(agentflow_start), 1)
            self.assertIn(herdr_start, start["hooks"])
            self.assertEqual(
                merged["hooks"]["Stop"][0]["hooks"][0]["command"],
                "herdr session stop",
            )
            self.assertEqual(
                merged["hooks"]["CustomEvent"][0]["hooks"][0]["command"],
                "user custom event",
            )
            self.assertIn("PostToolUseFailure", merged["hooks"])
            after_first = target.read_bytes()
            result, stdout, stderr = self._capture(
                lambda value: config_commands.hooks_merge(
                    value, installation=installation, memory_runtime=fake_runtime
                ),
                args,
            )
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("unchanged", stdout)
        self.assertEqual(target.read_bytes(), after_first)

    def test_invalid_hook_json_fails_without_replacement(self) -> None:
        target = self.root / ".claude/settings.json"
        target.parent.mkdir()
        target.write_text("{broken", encoding="utf-8")
        original = target.read_bytes()
        args = argparse.Namespace(provider="claude", root=str(self.root), dry_run=False, json=False)
        result, _, stderr = self._capture(cli.config_hooks_merge, args)
        self.assertEqual(result, 2)
        self.assertIn("invalid JSON", stderr)
        self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
