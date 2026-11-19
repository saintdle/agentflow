import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentflow import cli, config_commands, execution, model_policy, project_config


class ModelRouterTests(unittest.TestCase):
    def test_uncatalogued_models_and_roles_are_controller_selected(self):
        router = model_policy.parse_router(model_policy.default_router())
        for provider in ("codex", "claude", "copilot"):
            for model in ("future-model-2099", "vendor/new-model:v2", "haiku-new", "gpt-6-sol"):
                with self.subTest(provider=provider, model=model):
                    self.assertTrue(router.validate_route(
                        provider=provider, model=model, role="coding", effort="max",
                    ).ok)
        for model in ("", "auto", "-option", "model\ncommand", " model "):
            self.assertFalse(router.validate_route(
                provider="codex", model=model, role="coding", effort="medium",
            ).ok)

    def test_preferences_do_not_restrict_other_models_or_select_a_default(self):
        router = model_policy.parse_router({
            "mode": "controller-selected",
            "routes": [{"provider": "claude", "model": "claude-example", "effort": "high", "when": "Reviews"}],
        })
        self.assertTrue(router.validate_route(
            provider="codex", model="new-model", role="review", effort="medium",
        ).ok)
        self.assertFalse(router.validate_route(provider="codex", model="", role="review").ok)
        self.assertIsNone(project_config.prose_editor(project_config.default_data()))

    def test_router_preserves_non_model_execution_limits(self):
        report = execution.evaluate_launch(
            policy=execution.ExecutionPolicy(), model="gpt-6-sol", role="coding",
            task_attempt=3, total_attempts=0, planned_tasks=1, controller_selected_model=True,
        )
        self.assertEqual([f.id for f in report.findings], ["task-attempt-budget-exhausted"])

    def test_explicit_migration_is_backed_up_idempotent_and_preserves_custom_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = project_config.config_path(root)
            path.parent.mkdir()
            policy_path = path.parent / "custom.json"
            policy_path.write_bytes(model_policy.DEFAULT_POLICY_PATH.read_bytes())
            data = project_config.default_data()
            data.pop("modelrouter")
            data["model_policy"] = ".agentflow/custom.json"
            data["execution"]["max_attempts_per_task"] = 1
            path.write_text(json.dumps(data))
            original = path.read_bytes()
            args = argparse.Namespace(root=str(root), local=False, apply=False)
            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(config_commands.memory_runtime_backend, "state_home",
                                   return_value=root / "private"):
                self.assertEqual(config_commands.modelrouter_migrate(args), 0)
                self.assertEqual(path.read_bytes(), original)
                args.apply = True
                self.assertEqual(config_commands.modelrouter_migrate(args), 0)
                updated = path.read_bytes()
                self.assertEqual(config_commands.modelrouter_migrate(args), 0)
                self.assertEqual(path.read_bytes(), updated)
            active = project_config.load(root)
            self.assertNotIn("model_policy", active)
            self.assertEqual(active["execution"]["max_attempts_per_task"], 1)
            self.assertTrue(active["modelrouter"]["routes"])
            self.assertTrue(model_policy.load_policy(cli._resolve_model_policy(args, root)).validate_route(
                provider="copilot", model="new-release", role="coding", effort="high",
            ).ok)
            self.assertTrue(policy_path.exists())

    def test_local_policy_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project_config.write_layer(root, project_config.default_data(), local=False)
            local = project_config.default_local_data()
            local["model_policy"] = "custom.json"
            project_config.write_layer(root, local, local=True)
            self.assertNotIn("modelrouter", project_config.load(root))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(config_commands.modelrouter_migrate(argparse.Namespace(
                    root=str(root), local=False, apply=True,
                )), 2)

    def test_migration_previews_removal_of_the_implicit_legacy_editor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            policy_path = root / "policy.json"
            policy_path.write_bytes(model_policy.DEFAULT_POLICY_PATH.read_bytes())
            data = {
                "schema": project_config.SCHEMA, "version": 1,
                "model_policy": "policy.json", "skills": [],
            }
            project_config.write_layer(root, data, local=False)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(config_commands.modelrouter_migrate(argparse.Namespace(
                    root=str(root), local=False, apply=False,
                )), 0)
            preview = json.loads(output.getvalue())
            self.assertIn('"editor": null', preview["diff"])
            self.assertIn("implicit legacy prose editor", preview["notice"])
            self.assertEqual(project_config.load(root), data)
