from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import copy

from agentflow import __version__
from agentflow import cli
from agentflow import project_config
from agentflow import resources
from agentflow import model_policy
from agentflow import preflight


class PackagingConfigTests(unittest.TestCase):
    def test_version_and_resources_are_distribution_owned(self) -> None:
        self.assertEqual(__version__, "0.0.1")
        self.assertTrue(resources.item("templates", "project", "agentflow.json").is_file())
        self.assertTrue(resources.item("policies", "models-v1.json").is_file())
        self.assertEqual(len(resources.names("skills")), 7)
        self.assertEqual(len(resources.names("agents", "codex")), 4)
        self.assertEqual(len(resources.names("agents", "claude")), 4)
        self.assertEqual(len(resources.names("agents", "copilot")), 4)

    def test_install_dry_run_enumerates_bundled_assets_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, "home", return_value=Path(temp)), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            result = cli.install(argparse.Namespace(path=str(Path(temp) / "uninitialized"), force=False, dry_run=True))
            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertEqual(rendered.count("would-install"), 34)
            self.assertIn("agentflow-controller.toml", rendered)
            self.assertIn("shape-goal", rendered)

    def test_init_creates_live_valid_config_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            data = project_config.load(root)
            self.assertEqual(data["schema"], project_config.SCHEMA)
            self.assertTrue((root / data["model_policy"]).is_file())

    def test_add_list_sync_and_doctor_local_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            skill = root / "local-skills/example-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: Test\n---\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            add = argparse.Namespace(path=str(root), source=str(skill), name="", provider=["codex"])
            self.assertEqual(cli.skills_add(add), 0)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "provider/codex")}), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                args = argparse.Namespace(path=str(root), force=False, dry_run=False)
                self.assertEqual(cli.skills_sync(args), 0)
                self.assertEqual(cli.skills_doctor(argparse.Namespace(path=str(root))), 0)
                self.assertIn("example-skill", output.getvalue())
            destination = root / "provider/codex/skills/example-skill"
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), skill.resolve())

    def test_skill_source_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            (real / "SKILL.md").write_text("ok", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            data = project_config.default_data()
            data["skills"] = [{"name": "example", "path": "alias", "providers": ["codex"]}]
            self.assertTrue(any("symlink" in error for error in project_config.validate(data, root)))

    def test_project_policy_controls_preflight_and_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".agentflow").mkdir()
            document = json.loads(model_policy.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
            custom = copy.deepcopy(document)
            custom["routes"][0]["model"] = "custom-controller"
            custom_path = root / ".agentflow/custom-policy.json"
            custom_path.write_text(json.dumps(custom), encoding="utf-8")
            config = project_config.default_data()
            config["model_policy"] = ".agentflow/custom-policy.json"
            (root / ".agentflow/config.json").write_text(json.dumps(config), encoding="utf-8")

            args = argparse.Namespace(root=str(root), policy="")
            configured = model_policy.load_policy(cli._resolve_model_policy(args, root))
            spec = preflight.LaunchSpec(
                base="main", context=(), boundary="src", matrix=(), tools=(),
                model="custom-controller", session_id="session", provider="codex",
                role="controller", effort="high", policy_version="models-v1",
            )
            snapshot = preflight.RootSnapshot(
                root=str(root), taken_at="now", instructions_entrypoint=None,
                has_checks=False, has_tests=False, has_skills=False, tool_availability=(),
            )
            configured_report = preflight.check_launch(spec, snapshot, policy=configured)
            self.assertNotIn("policy-route-invalid", {finding.id for finding in configured_report.findings})
            bundled_report = preflight.check_launch(spec, snapshot, policy=model_policy.load_policy())
            self.assertIn("policy-route-invalid", {finding.id for finding in bundled_report.findings})

            explicit = root / "explicit.json"
            explicit.write_text(json.dumps(document), encoding="utf-8")
            args.policy = str(explicit)
            self.assertEqual(cli._resolve_model_policy(args, root), explicit.resolve())


if __name__ == "__main__":
    unittest.main()
