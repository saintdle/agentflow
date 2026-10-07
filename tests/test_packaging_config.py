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
        self.assertEqual(__version__, "0.0.9")
        self.assertEqual(
            model_policy.DEFAULT_POLICY_PATH,
            resources.item("policies", "models-v2.json"),
        )
        self.assertTrue(resources.item("templates", "project", "agentflow.json").is_file())
        self.assertTrue(resources.item("policies", "models-v1.json").is_file())
        self.assertTrue(resources.item("policies", "models-v2.json").is_file())
        self.assertEqual(len(resources.names("skills")), 7)
        self.assertEqual(len(resources.names("agents", "codex")), 5)
        self.assertEqual(len(resources.names("agents", "claude")), 5)
        self.assertEqual(len(resources.names("agents", "copilot")), 5)

    def test_install_dry_run_enumerates_bundled_assets_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, "home", return_value=Path(temp)), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            result = cli.install(argparse.Namespace(
                path=str(Path(temp) / "uninitialized"), force=False, dry_run=True,
                refresh_bundled=False,
            ))
            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertEqual(rendered.count("would-install"), 37)
            self.assertIn("agentflow-controller.toml", rendered)
            self.assertIn("shape-goal", rendered)

    def test_bundled_install_is_idempotent_and_preserves_unowned_edits(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as explicit_state,
            tempfile.TemporaryDirectory() as xdg_state,
            mock.patch.dict(
                os.environ,
                {"AGENTFLOW_STATE_HOME": explicit_state, "XDG_STATE_HOME": xdg_state},
                clear=False,
            ),
            mock.patch.object(Path, "home", return_value=Path(temp)),
        ):
            args = argparse.Namespace(
                path=str(Path(temp) / "uninitialized"), force=False, dry_run=False,
                refresh_bundled=False,
            )
            self.assertEqual(cli.install(args), 0)
            self.assertEqual(cli.install(args), 0)
            profile = Path(temp) / ".codex/agents/agentflow-controller.toml"
            profile.write_text("stale\n", encoding="utf-8")
            args.refresh_bundled = True
            self.assertEqual(cli.install(args), 2)
            self.assertEqual(profile.read_text(encoding="utf-8"), "stale\n")
            backups = list((Path(xdg_state) / "agentflow/backups").rglob(
                "agentflow-controller.toml"
            ))
            self.assertEqual(backups, [])

    def test_adapted_skills_install_with_self_contained_attribution(self) -> None:
        adapted = ("code-review", "diagnosing-bugs", "to-tickets", "wayfinder")
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            Path, "home", return_value=Path(temp)
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            result = cli.install(
                argparse.Namespace(
                    path=str(Path(temp) / "uninitialized"), force=False, dry_run=False
                )
            )
            self.assertEqual(result, 0)
            for provider_root in (".agents", ".claude", ".copilot"):
                for name in adapted:
                    skill = Path(temp) / provider_root / "skills" / name
                    notice = (skill / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
                    provenance = (skill / "PROVENANCE.md").read_text(encoding="utf-8")
                    self.assertIn("Copyright (c) 2026 Matt Pocock", notice)
                    self.assertIn("Permission is hereby granted", notice)
                    self.assertIn("https://github.com/mattpocock/skills", provenance)
                    self.assertIn("Upstream license: MIT", provenance)

    def test_init_creates_live_valid_config_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            data = project_config.load(root)
            self.assertEqual(data["schema"], project_config.SCHEMA)
            self.assertEqual(data["prose"]["editor"], project_config.DEFAULT_PROSE_EDITOR)
            self.assertTrue((root / data["model_policy"]).is_file())
            ignored = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".agentflow/config.local.json", ignored)
            self.assertIn(".agentflow/managed-skill-links.json", ignored)

    def test_project_init_and_provider_profiles_deliver_shared_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)

            template = Path(__file__).resolve().parents[1] / "templates/project"
            generated_agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(generated_agents, (template / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual(
                generated_agents,
                resources.item("templates", "project", "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n")
            self.assertIn(
                "AGENTS.md",
                (root / ".github/copilot-instructions.md").read_text(encoding="utf-8"),
            )

        shared_policy_reference = "AGENTS.md for shared coding discipline"
        for provider in ("codex", "claude", "copilot"):
            profiles = resources.names("agents", provider)
            self.assertEqual(len(profiles), 5)
            for name in profiles:
                profile = resources.item("agents", provider, name).read_text(encoding="utf-8")
                with self.subTest(provider=provider, profile=name):
                    self.assertIn(shared_policy_reference, profile)

    def test_local_layer_validates_and_overrides_shared_by_skill_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared_skill = root / "skills/shared"
            local_skill = root / "skills/local"
            for skill in (shared_skill, local_skill):
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: layered\ndescription: test\n---\n", encoding="utf-8")
            shared = project_config.default_data()
            shared["skills"] = [{"name": "layered", "path": "skills/shared", "providers": ["codex"]}]
            local = project_config.default_local_data()
            local["model_policy"] = ".agentflow/local-policy.json"
            local["prose"] = {"editor": None}
            local["skills"] = [{"name": "layered", "path": "skills/local", "providers": ["claude"]}]
            project_config.write_layer(root, shared, local=False)
            project_config.write_layer(root, local, local=True)
            merged = project_config.load(root)
            self.assertEqual(merged["model_policy"], ".agentflow/local-policy.json")
            self.assertIsNone(project_config.prose_editor(merged))
            self.assertEqual(merged["skills"], local["skills"])
            self.assertEqual(project_config.skill_origins(root), {"layered": "local"})

            local["unexpected"] = True
            (root / ".agentflow/config.local.json").write_text(json.dumps(local), encoding="utf-8")
            with self.assertRaises(project_config.ConfigError):
                project_config.load(root)

    def test_add_defaults_repository_skill_shared_and_external_skill_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            inside = root / "skills/inside"
            outside = base / "external/outside"
            forced = base / "external/forced"
            for skill in (inside, outside, forced):
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: test\ndescription: test\n---\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            common = {"path": str(root), "provider": ["codex"], "local": False, "shared": False}
            self.assertEqual(cli.skills_add(argparse.Namespace(source=str(inside), name="inside", **common)), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(source=str(outside), name="outside", **common)), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                source=str(forced), name="forced", path=str(root), provider=["codex"], local=False, shared=True
            )), 0)
            shared, local = project_config.load_layers(root)
            self.assertEqual([entry["name"] for entry in shared["skills"]], ["inside", "forced"])
            self.assertEqual([entry["name"] for entry in local["skills"]], ["outside"])
            self.assertEqual(shared["skills"][0]["path"], "skills/inside")
            self.assertTrue(Path(local["skills"][0]["path"]).is_absolute())

    def test_explicit_local_add_overrides_shared_but_implicit_duplicate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            shared_source = root / "skills/layered-shared"
            local_source = base / "external/layered-local"
            implicit_source = base / "external/layered-implicit"
            local_first = base / "external/local-first"
            shared_underlay = root / "skills/shared-underlay"
            for source in (shared_source, local_source, implicit_source, local_first, shared_underlay):
                source.mkdir(parents=True)
                (source / "SKILL.md").write_text(
                    "---\nname: layered\ndescription: test\n---\n", encoding="utf-8"
                )
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(shared_source), name="layered", provider=["codex"],
                local=False, shared=False,
            )), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(implicit_source), name="layered", provider=["codex"],
                local=False, shared=False,
            )), 2)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(local_source), name="layered", provider=["claude"],
                local=True, shared=False,
            )), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(implicit_source), name="layered", provider=["copilot"],
                local=True, shared=False,
            )), 2)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(local_first), name="underlay", provider=["claude"],
                local=True, shared=False,
            )), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(shared_underlay), name="underlay", provider=["codex"],
                local=False, shared=True,
            )), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(shared_underlay), name="underlay", provider=["codex"],
                local=False, shared=True,
            )), 2)
            shared, local = project_config.load_layers(root)
            self.assertEqual([entry["name"] for entry in shared["skills"]], ["layered", "underlay"])
            self.assertEqual([entry["name"] for entry in local["skills"]], ["layered", "underlay"])
            merged = project_config.skills(project_config.load(root), root)
            self.assertEqual(merged, [
                ("layered", local_source.resolve(), ("claude",)),
                ("underlay", local_first.resolve(), ("claude",)),
            ])

    def test_remove_preserves_replaced_and_unowned_destinations_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            external = base / "external/safe-skill"
            external.mkdir(parents=True)
            (external / "SKILL.md").write_text("---\nname: safe-skill\ndescription: test\n---\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            add = argparse.Namespace(
                path=str(root), source=str(external), name="safe-skill", provider=["codex"],
                local=False, shared=False,
            )
            self.assertEqual(cli.skills_add(add), 0)
            codex_home = root / "provider/codex"
            claude_home = root / "provider/claude"
            local = json.loads(project_config.local_config_path(root).read_text(encoding="utf-8"))
            local["skills"][0]["providers"] = ["codex", "claude"]
            project_config.write_layer(root, local, local=True)
            with mock.patch.dict(os.environ, {
                "CODEX_HOME": str(codex_home), "CLAUDE_HOME": str(claude_home)
            }):
                self.assertEqual(cli.skills_sync(argparse.Namespace(path=str(root), force=False, dry_run=False)), 0)
                destination = codex_home / "skills/safe-skill"
                destination.unlink()
                destination.write_text("user-owned\n", encoding="utf-8")
                directory = claude_home / "skills/safe-skill"
                directory.unlink()
                directory.mkdir()
                remove = argparse.Namespace(
                    path=str(root), name="safe-skill", local=False, shared=False, keep_links=False
                )
                self.assertEqual(cli.skills_remove(remove), 0)
                self.assertEqual(destination.read_text(encoding="utf-8"), "user-owned\n")
                self.assertTrue(directory.is_dir())
                self.assertEqual(cli.skills_remove(remove), 0)
            self.assertEqual(project_config.load(root)["skills"], [])

    def test_remove_explicit_layer_preserves_same_name_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared_source = root / "skills/shared"
            local_source = root / "skills/local"
            for source in (shared_source, local_source):
                source.mkdir(parents=True)
                (source / "SKILL.md").write_text("---\nname: layered\ndescription: test\n---\n", encoding="utf-8")
            shared = project_config.default_data()
            shared["skills"] = [{"name": "layered", "path": "skills/shared", "providers": ["codex"]}]
            local = project_config.default_local_data()
            local["skills"] = [{"name": "layered", "path": "skills/local", "providers": ["codex"]}]
            project_config.write_layer(root, shared, local=False)
            project_config.write_layer(root, local, local=True)
            args = argparse.Namespace(
                path=str(root), name="layered", local=False, shared=True, keep_links=False
            )
            self.assertEqual(cli.skills_remove(args), 0)
            remaining_shared, remaining_local = project_config.load_layers(root)
            self.assertEqual(remaining_shared["skills"], [])
            self.assertEqual(remaining_local["skills"], local["skills"])
            self.assertEqual(project_config.skills(project_config.load(root), root)[0][1], local_source.resolve())
            self.assertEqual(cli.skills_remove(args), 0)

    def test_remove_preserves_unrecorded_matching_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            source = root / "skills/user-linked"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: user-linked\ndescription: test\n---\n", encoding="utf-8")
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            self.assertEqual(cli.skills_add(argparse.Namespace(
                path=str(root), source=str(source), name="user-linked", provider=["codex"],
                local=False, shared=False,
            )), 0)
            codex_home = root / "provider/codex"
            destination = codex_home / "skills/user-linked"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(source, target_is_directory=True)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                self.assertEqual(cli.skills_remove(argparse.Namespace(
                    path=str(root), name="user-linked", local=False, shared=False, keep_links=False
                )), 0)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), source.resolve())

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
                self.assertIn("sha256=", output.getvalue())
            destination = root / "provider/codex/skills/example-skill"
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), skill.resolve())
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "provider/codex")}):
                self.assertEqual(
                    cli.skills_remove(
                        argparse.Namespace(path=str(root), name="example-skill", keep_links=False)
                    ),
                    0,
                )
            self.assertFalse(destination.exists())
            self.assertTrue(skill.is_dir())
            self.assertEqual(project_config.load(root)["skills"], [])

    def test_unenforced_workflow_switches_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = project_config.default_data()
            data["require_external_preflight"] = "no"
            self.assertIn("unknown field(s): require_external_preflight", project_config.validate(data, root))

    def test_doctor_uses_requested_root_and_fails_for_missing_configured_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            cli, "_version", return_value=("missing", "")
        ), mock.patch.object(cli, "_provider_command", return_value=None), mock.patch.object(
            cli.beads_backend, "version", return_value=((1, 1, 0), "bd version 1.1.0")
        ), mock.patch.object(cli.beads_backend, "workspace", return_value=None) as workspace:
            root = Path(temp)
            self.assertEqual(cli.init_project(argparse.Namespace(path=str(root), beads=False)), 0)
            (root / ".agentflow/models-v2.json").unlink()
            self.assertEqual(cli.doctor(argparse.Namespace(path=str(root))), 2)
            workspace.assert_called_once_with(root.resolve())

    def test_release_workflow_is_immutable_and_gated_on_green_main(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--clobber", workflow)
        self.assertIn('git merge-base --is-ancestor "${GITHUB_SHA}" origin/main', workflow)
        for required in ("ci.yml", "security.yml", "build-main.yml"):
            self.assertIn(required, workflow)
        self.assertIn("Release ${GITHUB_REF_NAME} already exists", workflow)

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
                role="controller", effort="high", policy_version="models-v2",
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
