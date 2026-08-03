from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import checkpoint, cli, readiness, wait, worktree


class CheckpointTests(unittest.TestCase):
    def _valid(self) -> dict:
        return {"task": "t-1", "phase": "code", "next_action": "run tests",
                "changed_files": ["src/a.py"], "last_check": "unittest"}

    def test_round_trip_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cp.json"
            checkpoint.write_checkpoint(path, self._valid())
            resumed = checkpoint.load_checkpoint(path)
            self.assertEqual(resumed["task"], "t-1")
            self.assertEqual(resumed["changed_files"], ["src/a.py"])
            self.assertEqual(resumed["schema"], checkpoint.SCHEMA)

    def test_rejects_secret(self) -> None:
        data = self._valid()
        synthetic_token = "gh" + "p_" + ("A" * 36)
        data["blocker"] = f"token={synthetic_token}"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_unknown_field(self) -> None:
        data = self._valid()
        data["transcript"] = "a long conversation"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_oversize(self) -> None:
        data = self._valid()
        data["remaining_risk"] = "x" * (checkpoint.FIELD_MAX + 1)
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_requires_core_fields(self) -> None:
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint({"phase": "code", "next_action": "x"})

    def test_rejects_control_characters(self) -> None:
        data = self._valid()
        data["next_action"] = "run\x01tests"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_carriage_return_in_text(self) -> None:
        data = self._valid()
        data["next_action"] = "run\rtests"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_del_in_text(self) -> None:
        data = self._valid()
        data["blocker"] = "issue\x7f here"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_allows_tab_and_newline_in_text(self) -> None:
        data = self._valid()
        data["next_action"] = "run\ttests\nand verify"
        doc = checkpoint.build_checkpoint(data)
        self.assertIn("\t", doc["next_action"])
        self.assertIn("\n", doc["next_action"])

    def test_rejects_control_in_changed_files(self) -> None:
        data = self._valid()
        data["changed_files"] = ["src/a.py", "src/\x02b.py"]
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_tab_in_changed_files(self) -> None:
        data = self._valid()
        data["changed_files"] = ["src/a\tb.py"]
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_newline_in_changed_files(self) -> None:
        data = self._valid()
        data["changed_files"] = ["src/a\nb.py"]
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)

    def test_rejects_del_in_changed_files(self) -> None:
        data = self._valid()
        data["changed_files"] = ["src/\x7fa.py"]
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.build_checkpoint(data)


class WaitTests(unittest.TestCase):
    def _contract(self, **kw) -> wait.WaitContract:
        base = dict(progress_event="PROGRESS", success_predicate="DONE",
                    failure_predicate="FATAL", max_silent_interval=10.0,
                    deadline=100.0, cleanup_owner="writer", poll_interval=1.0)
        base.update(kw)
        return wait.WaitContract(**base)

    def _clock(self, times):
        it = iter(times)
        return lambda: next(it)

    def test_success(self) -> None:
        outputs = iter(["", "PROGRESS", "DONE"])
        outcome = wait.run_wait(self._contract(), lambda: next(outputs),
                                now=self._clock([0, 0, 1, 1, 2, 2, 2]), sleep=lambda s: None)
        self.assertEqual(outcome.status, "success")

    def test_failure_reports_cleanup(self) -> None:
        outputs = iter(["", "FATAL"])
        outcome = wait.run_wait(self._contract(), lambda: next(outputs),
                                now=self._clock([0, 0, 1, 1, 1]), sleep=lambda s: None)
        self.assertEqual(outcome.status, "failure")
        self.assertEqual(outcome.cleanup_owner, "writer")

    def test_stale_records_last_predicate(self) -> None:
        outcome = wait.run_wait(self._contract(), lambda: "",
                                now=self._clock([0, 0, 5, 20]), sleep=lambda s: None)
        self.assertEqual(outcome.status, "stale")
        self.assertEqual(outcome.cleanup_owner, "writer")

    def test_deadline(self) -> None:
        outcome = wait.run_wait(self._contract(max_silent_interval=100.0), lambda: "",
                                now=self._clock([0, 0, 50, 101]), sleep=lambda s: None)
        self.assertEqual(outcome.status, "deadline")

    def test_invalid_contract(self) -> None:
        with self.assertRaises(wait.WaitError):
            self._contract(cleanup_owner="")

    def test_invalid_regex(self) -> None:
        with self.assertRaises(wait.WaitError):
            self._contract(success_predicate="[invalid")
        with self.assertRaises(wait.WaitError):
            self._contract(failure_predicate="(?P<bad")

    def test_metacharacter_regex(self) -> None:
        outputs = iter(["test.", "test.success"])
        outcome = wait.run_wait(self._contract(success_predicate=r"test\.success"),
                                lambda: next(outputs),
                                now=self._clock([0, 0, 1, 1, 1]), sleep=lambda s: None)
        self.assertEqual(outcome.status, "success")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


class WorktreeTests(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        root = Path(tmp) / "repo"
        root.mkdir()
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "a@b.c")
        _git(root, "config", "user.name", "Test")
        (root / "f.txt").write_text("hi\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "init")
        return root

    def test_provision_status_retire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            wt = Path(tmp) / "wt"
            worktree.provision(root, bead="b1", actor="me", base=f"main@{sha[:12]}",
                               branch="agent/x", path=wt)
            st = worktree.status(root, wt)
            self.assertTrue(st["cleanup_eligible"])
            retired = worktree.retire(root, wt)
            self.assertTrue(retired["retired"])

    def test_base_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with self.assertRaises(worktree.WorktreeError):
                worktree.provision(root, bead="b1", actor="me",
                                   base="main@deadbeefdead", branch="agent/y",
                                   path=Path(tmp) / "wt2")

    def test_retire_refuses_dirty_and_unmerged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            wt = Path(tmp) / "wt"
            worktree.provision(root, bead="b1", actor="me", base="main",
                               branch="agent/z", path=wt)
            (wt / "new.txt").write_text("x\n", encoding="utf-8")
            with self.assertRaises(worktree.WorktreeError):
                worktree.retire(root, wt)
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "work")
            with self.assertRaises(worktree.WorktreeError):
                worktree.retire(root, wt)
            st = worktree.status(root, wt)
            self.assertFalse(st["cleanup_eligible"])
            self.assertEqual(st["commits_ahead_of_base"], 1)

    def test_collision_resistant_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            wt1 = Path(tmp) / "a" / "b"
            wt2 = Path(tmp) / "a-b"
            for wt in (wt1, wt2):
                worktree.provision(root, bead=f"b-{wt.name}", actor="me",
                                   base="main", branch=f"agent/{wt.name}", path=wt)
            registry_dir = root / worktree.REGISTRY_DIR
            records = list(registry_dir.glob("*.json"))
            self.assertEqual(len(records), 2)
            st1 = worktree.status(root, wt1)
            st2 = worktree.status(root, wt2)
            self.assertEqual(st1["bead"], "b-b")
            self.assertEqual(st2["bead"], "b-a-b")


class ReadinessTests(unittest.TestCase):
    def test_zero_write_and_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            before = sorted(p.name for p in root.rglob("*"))
            report = readiness.assess(root)
            after = sorted(p.name for p in root.rglob("*"))
            self.assertEqual(before, after)
            areas = {f["area"] for f in report["findings"]}
            self.assertIn("instructions", areas)
            self.assertIn("deterministic-checks", areas)
            self.assertFalse(report["ready"])

    def test_ready_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("Run check suite.\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts/validate.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests/test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
            (root / ".agents/skills").mkdir(parents=True)
            (root / "docs").mkdir()
            report = readiness.assess(root)
            self.assertTrue(report["ready"], report["findings"])


class CliWiringTests(unittest.TestCase):
    def test_checkpoint_cli_write_show(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cp.json"
            args = argparse.Namespace(
                task="t", phase="code", next_action="do", completed_evidence="",
                blocker="", changed_file=["a.py"], last_check="unittest",
                remaining_risk="", session_hash="abc", output=str(out))
            self.assertEqual(cli.checkpoint_write(args), 0)
            self.assertTrue(out.exists())

    def test_assess_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(cwd=tmp, json=True)
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(cli.assess_repository(args), 0)
            self.assertIn("findings", json.loads(stdout.getvalue()))

    def test_cli_entry_point_smoke(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        result = subprocess.run(
            [sys.executable, "-m", "agentflow.cli", "--help"],
            capture_output=True, text=True, timeout=5, check=False, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("checkpoint", result.stdout)
        result = subprocess.run(
            [sys.executable, "-m", "agentflow.cli", "checkpoint", "--help"],
            capture_output=True, text=True, timeout=5, check=False, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("write", result.stdout)


class SkillGuidanceTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_automation_first_in_skills(self) -> None:
        shape = (self.ROOT / ".agents/skills/shape-goal/SKILL.md").read_text(encoding="utf-8")
        orch = (self.ROOT / ".agents/skills/orchestrate-agents/SKILL.md").read_text(encoding="utf-8")
        for text in (shape.lower(), orch.lower()):
            self.assertIn("deterministic", text)
            self.assertIn("automation", text)

    def test_diagnosing_bugs_requires_evidence_before_hypotheses(self) -> None:
        text = (
            self.ROOT / ".agents/skills/diagnosing-bugs/SKILL.md"
        ).read_text(encoding="utf-8").lower()
        self.assertLess(text.index("build the feedback loop"), text.index("minimize and explain"))
        for phrase in (
            "red-capable",
            "before forming a cause theory",
            "diagnosis only",
            "regression test",
            "if no trustworthy loop can be built",
        ):
            self.assertIn(phrase, text)

    def test_wayfinder_is_a_beads_decision_graph_not_execution(self) -> None:
        text = (
            self.ROOT / ".agents/skills/wayfinder/SKILL.md"
        ).read_text(encoding="utf-8").lower()
        for phrase in (
            "beads",
            "af:stage:plan",
            "af:role:decision",
            "--no-inherit-labels",
            "dependency cycles",
            "do not implement",
            "<human title> (<id>)",
        ):
            self.assertIn(phrase, text)

    def test_code_review_has_independent_read_only_axes_and_dispositions(self) -> None:
        text = (
            self.ROOT / ".agents/skills/code-review/SKILL.md"
        ).read_text(encoding="utf-8").lower()
        for phrase in (
            "standards axis",
            "specification axis",
            "without sharing findings",
            "read-only",
            "agentflow review record",
            "rejected-factual",
            "route-fix",
            "never merge",
        ):
            self.assertIn(phrase, text)

    def test_engineering_skills_have_openai_metadata(self) -> None:
        for name in ("diagnosing-bugs", "wayfinder", "code-review"):
            metadata = (
                self.ROOT / ".agents/skills" / name / "agents/openai.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("display_name:", metadata)
            self.assertIn("short_description:", metadata)
            self.assertIn(f"${name}", metadata)


if __name__ == "__main__":
    unittest.main()
