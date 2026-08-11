from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agentflow import migration


class LegacyMigrationTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        home = root / "home"
        legacy = root / "legacy-agentflow"
        state = root / "state/migrations"
        legacy_command = legacy / "bin/agentflow"
        legacy_command.parent.mkdir(parents=True)
        legacy_command.write_text("#!/bin/sh\n", encoding="utf-8")
        legacy_command.chmod(0o755)
        legacy_skill = legacy / ".agents/skills/to-tickets"
        legacy_skill.mkdir(parents=True)
        (legacy_skill / "SKILL.md").write_text("legacy\n", encoding="utf-8")
        legacy_profile = legacy / ".codex/agents/agentflow-controller.toml"
        legacy_profile.parent.mkdir(parents=True)
        legacy_profile.write_text("legacy\n", encoding="utf-8")
        legacy_hook = legacy / "templates/user/codex-hooks.json"
        legacy_hook.parent.mkdir(parents=True)
        legacy_hook.write_text("{}\n", encoding="utf-8")

        links = {
            home / ".local/bin/agentflow": legacy_command,
            home / ".agents/skills/to-tickets": legacy_skill,
            home / ".claude/skills/to-tickets": legacy_skill,
            home / ".codex/agents/agentflow-controller.toml": legacy_profile,
            home / ".codex/hooks.json": legacy_hook,
        }
        for destination, source in links.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=source.is_dir())

        new_command = root / "isolated/bin/agentflow"
        new_command.parent.mkdir(parents=True)
        new_command.write_text("#!/bin/sh\nprintf 'agentflow 0.0.3\\n'\n", encoding="utf-8")
        new_command.chmod(0o755)
        return home, legacy, state, new_command

    def test_plan_selects_only_exact_legacy_owned_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, _, new_command = self.fixture(root)
            domain_source = root / "domain-skill"
            domain_source.mkdir()
            unmanaged = home / ".agents/skills/domain-skill"
            unmanaged.symlink_to(domain_source, target_is_directory=True)

            result = migration.plan(legacy, home=home, new_command=new_command)

            destinations = {Path(item["destination"]) for item in result["operations"]}
            resolved_home = home.resolve()
            self.assertIn(resolved_home / ".local/bin/agentflow", destinations)
            self.assertIn(resolved_home / ".agents/skills/to-tickets", destinations)
            self.assertNotIn(unmanaged.resolve(strict=False), destinations)
            self.assertIn(str(resolved_home / ".agents/skills/domain-skill"), result["preserved_unmanaged_entries"])

    def test_apply_and_rollback_restore_links_without_touching_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            project = root / "project"
            project.mkdir()
            sentinel = project / ".agentflow/controller/lease.json"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("private project state\n", encoding="utf-8")
            before = sentinel.read_bytes()

            applied = migration.apply(
                legacy,
                home=home,
                state_root=state,
                new_command=new_command,
                process_check=False,
            )

            self.assertEqual(applied["status"], "applied")
            self.assertEqual((home / ".local/bin/agentflow").resolve(), new_command.resolve())
            self.assertFalse((home / ".agents/skills/to-tickets").is_symlink())
            self.assertIn("name: to-tickets", (home / ".agents/skills/to-tickets/SKILL.md").read_text())
            self.assertFalse((home / ".codex/agents/agentflow-controller.toml").is_symlink())
            self.assertEqual(sentinel.read_bytes(), before)
            manifest_path = state / applied["id"] / "manifest.json"
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)

            rolled_back = migration.rollback(applied["id"], state_root=state)

            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertEqual((home / ".local/bin/agentflow").resolve(), (legacy / "bin/agentflow").resolve())
            self.assertTrue((home / ".agents/skills/to-tickets").is_symlink())
            self.assertEqual(sentinel.read_bytes(), before)

    def test_rollback_refuses_drift_before_changing_any_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            applied = migration.apply(
                legacy, home=home, state_root=state, new_command=new_command, process_check=False
            )
            changed = home / ".codex/hooks.json"
            changed.write_text("user change\n", encoding="utf-8")
            command_digest = migration._tree_digest(home / ".local/bin/agentflow")

            with self.assertRaisesRegex(migration.MigrationError, "changed after migration"):
                migration.rollback(applied["id"], state_root=state)

            self.assertEqual(changed.read_text(encoding="utf-8"), "user change\n")
            self.assertEqual(migration._tree_digest(home / ".local/bin/agentflow"), command_digest)

    def test_active_process_gate_fails_before_creating_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            with mock.patch.object(migration, "active_legacy_processes", return_value=[1234]):
                with self.assertRaisesRegex(migration.MigrationError, "still active"):
                    migration.apply(
                        legacy, home=home, state_root=state, new_command=new_command
                    )
            self.assertEqual((home / ".local/bin/agentflow").resolve(), (legacy / "bin/agentflow").resolve())
            self.assertFalse(any(path.name == "manifest.json" for path in state.rglob("manifest.json")))

    def test_apply_rejects_replacement_inside_legacy_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, _ = self.fixture(root)
            with self.assertRaisesRegex(migration.MigrationError, "legacy checkout"):
                migration.apply(
                    legacy,
                    home=home,
                    state_root=state,
                    new_command=legacy / "bin/agentflow",
                    process_check=False,
                )

    def test_apply_failure_after_install_restores_every_legacy_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            destinations = [
                home / ".local/bin/agentflow",
                home / ".agents/skills/to-tickets",
                home / ".claude/skills/to-tickets",
                home / ".codex/agents/agentflow-controller.toml",
                home / ".codex/hooks.json",
            ]
            original_targets = {path: path.resolve() for path in destinations}
            with mock.patch.object(migration, "_tree_digest", side_effect=OSError("disk")):
                with self.assertRaisesRegex(migration.MigrationError, "rolled back"):
                    migration.apply(
                        legacy,
                        home=home,
                        state_root=state,
                        new_command=new_command,
                        process_check=False,
                    )
            for path, target in original_targets.items():
                self.assertTrue(path.is_symlink())
                self.assertEqual(path.resolve(), target)

    def test_apply_rejects_an_executable_from_a_different_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            new_command.write_text("#!/bin/sh\nprintf 'not-agentflow 9.9.9\\n'\n", encoding="utf-8")
            with self.assertRaisesRegex(migration.MigrationError, "running Agentflow 0.0.3"):
                migration.apply(
                    legacy,
                    home=home,
                    state_root=state,
                    new_command=new_command,
                    process_check=False,
                )
            self.assertEqual((home / ".local/bin/agentflow").resolve(), (legacy / "bin/agentflow").resolve())

    def test_rollback_failure_restores_fully_applied_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            applied = migration.apply(
                legacy, home=home, state_root=state, new_command=new_command, process_check=False
            )
            destinations = [Path(record["destination"]) for record in applied["operations"]]
            applied_digests = {path: migration._tree_digest(path) for path in destinations}
            real_replace = os.replace
            backup_moves = 0

            def fail_second_backup(source, destination):
                nonlocal backup_moves
                source_path = Path(source)
                if source_path.parent.name == "backup":
                    backup_moves += 1
                    if backup_moves == 2:
                        raise OSError("injected rollback failure")
                return real_replace(source, destination)

            with mock.patch.object(migration.os, "replace", side_effect=fail_second_backup):
                with self.assertRaisesRegex(migration.MigrationError, "applied migration was restored"):
                    migration.rollback(applied["id"], state_root=state)

            for path, digest in applied_digests.items():
                self.assertEqual(migration._tree_digest(path), digest)
            manifest = json.loads((state / applied["id"] / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "applied")

    def test_manifest_contains_no_project_state_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, legacy, state, new_command = self.fixture(root)
            result = migration.apply(
                legacy, home=home, state_root=state, new_command=new_command, process_check=False
            )
            manifest = json.loads((state / result["id"] / "manifest.json").read_text())
            serialized = json.dumps(manifest)
            self.assertNotIn("private project state", serialized)
            self.assertEqual(
                manifest["protected_scopes"],
                ["project repositories", "Beads databases", "sessions and evidence", "Git state"],
            )


if __name__ == "__main__":
    unittest.main()
