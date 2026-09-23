from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli
from agentflow import prose


LONG = " ".join(f"word{number}" for number in range(40)) + "."


class ProseCheckTests(unittest.TestCase):
    def test_ignores_frontmatter_and_fenced_code_but_checks_reader_prose(self) -> None:
        text = f"""---
title: {LONG}
---

```sh
{LONG}
```

{LONG}
"""
        report = prose.check_text(text, path="draft.md", profile_name="technical-blog")
        long_findings = [item for item in report.findings if item.code == "sentence-too-long"]
        self.assertEqual(len(long_findings), 1)
        self.assertEqual(long_findings[0].line, 9)

    def test_instruqt_profile_is_stricter_than_blog_profile(self) -> None:
        sentence = " ".join(f"term{number}" for number in range(30)) + "."
        self.assertTrue(prose.check_text(sentence, path="a.md", profile_name="technical-blog").passed)
        self.assertFalse(prose.check_text(sentence, path="a.md", profile_name="instruqt").passed)

    def test_flags_filler_and_repeated_sentence(self) -> None:
        sentence = "This repeated sentence contains enough ordinary words to trigger deterministic duplicate detection."
        report = prose.check_text(
            f"It is important to note that this is direct.\n\n{sentence}\n\n{sentence}",
            path="a.md", profile_name="technical-blog",
        )
        self.assertIn("filler-phrase", {item.code for item in report.findings})
        self.assertIn("repeated-sentence", {item.code for item in report.findings})

    def test_verify_preserves_technical_material_and_rejects_expansion(self) -> None:
        source = """Long framing uses `agentflow run` with [the guide](https://example.com/v1) for 12 users.

```sh
agentflow doctor
```
"""
        edited = """Use `agentflow run` with [the guide](https://example.com/v1) for 12 users.

```sh
agentflow doctor
```
"""
        self.assertTrue(prose.verify_texts(
            source, edited, source_path="source.md", edited_path="edited.md",
            profile_name="technical-blog",
        ).passed)
        changed = edited.replace("12", "13").replace("doctor", "status")
        report = prose.verify_texts(
            source, changed, source_path="source.md", edited_path="edited.md",
            profile_name="technical-blog",
        )
        codes = {item.code for item in report.findings}
        self.assertIn("protected-numbers-changed", codes)
        self.assertIn("protected-fenced-code-changed", codes)

    def test_verify_preserves_paths_and_markdown_structure(self) -> None:
        source = "## Run\n\n- Open `tracks/demo/assignment.md`.\n"
        edited = "### Run\n\nOpen `tracks/demo/assignment.md`.\n"
        report = prose.verify_texts(
            source, edited, source_path="source.md", edited_path="edited.md",
            profile_name="instruqt",
        )
        self.assertIn(
            "protected-markdown-structure-changed",
            {item.code for item in report.findings},
        )

    def test_read_rejects_symlink_and_non_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "draft.md"
            target.write_text("Direct prose.\n", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(target)
            with self.assertRaises(prose.ProseError):
                prose.read_markdown(link)
            other = root / "draft.txt"
            other.write_text("text", encoding="utf-8")
            with self.assertRaises(prose.ProseError):
                prose.read_markdown(other)


class ProseCliTests(unittest.TestCase):
    def run_cli(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_check_json_returns_one_for_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "draft.md"
            source.write_text(LONG, encoding="utf-8")
            code, output, _ = self.run_cli([
                "prose", "check", str(source), "--profile", "technical-blog", "--json",
            ])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output)["passed"])

    def test_prepare_skips_non_claude_and_passing_claude_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "draft.md"
            source.write_text("Use the command, then inspect the result.\n", encoding="utf-8")
            for provider, expected in (("codex", "not-applicable"), ("claude", "not-needed")):
                code, output, _ = self.run_cli([
                    "prose", "prepare", "draft.md", "--cwd", str(root),
                    "--profile", "technical-blog", "--writer-provider", provider, "--json",
                ])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["status"], expected)
            self.assertEqual({path.name for path in root.iterdir()}, {"draft.md"})

    def test_prepare_recognizes_copilot_claude_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            code, output, _ = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "copilot",
                "--writer-model", "claude-opus-4.8",
                "--require-skill", "isovalent-ai-tme-skill", "--check", "true", "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["status"], "handoff-created")

    def test_prepare_failing_claude_creates_bounded_handoff_not_an_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "draft.md"
            source.write_text(LONG, encoding="utf-8")
            skill = root / ".agents/skills/isovalent-ai-tme-skill/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: isovalent-ai-tme-skill\ndescription: Test editing skill\n---\n",
                encoding="utf-8",
            )
            original = source.read_bytes()
            code, output, error = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude",
                "--require-skill", "isovalent-ai-tme-skill",
                "--check", "python3 scripts/check_blog.py draft.edited.md", "--json",
            ])
            self.assertEqual((code, error), (0, ""))
            result = json.loads(output)
            handoff = Path(result["handoff"])
            manifest = json.loads(handoff.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["lane"], "native")
            self.assertNotIn("machine_return_contract", manifest)
            self.assertNotIn(
                "AGENTFLOW_RESULT_CONTRACT", handoff.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["prose"]["editor"], {
                "provider": "codex", "model": "gpt-5.6-luna", "role": "editing",
                "effort": "medium", "policy": "models-v2",
            })
            self.assertEqual(manifest["prose"]["artifact_kind"], "reader-facing")
            self.assertEqual(manifest["prose"]["max_passes"], 1)
            self.assertIn("never overwrite", handoff.read_text(encoding="utf-8"))
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((root / "draft.edited.md").exists())
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli.handoff_preflight(type("Args", (), {
                        "file": str(handoff), "cwd": str(root), "require_matrix": False,
                    })()),
                    0,
                )
            launch_args = type("Args", (), {
                "provider": "codex", "file": str(handoff), "cwd": str(root),
                "role": "editing", "model": "gpt-5.6-luna", "effort": "medium",
                "policy": "", "print_command": False, "selective_model": False,
            })()
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch.object(cli.subprocess, "call", return_value=0) as spawned:
                self.assertEqual(cli.handoff_launch(launch_args), 0)
            spawned.assert_called_once()

    def test_prepare_accepts_explicit_non_openai_editor_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            code, output, error = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude",
                "--editor-provider", "copilot", "--editor-model", "claude-sonnet-4.6",
                "--editor-effort", "medium", "--editor-max-ai-credits", "30",
                "--require-skill", "domain-skill",
                "--check", "true", "--json",
            ])
            self.assertEqual((code, error), (0, ""))
            result = json.loads(output)
            self.assertIn("handoff launch copilot", result["launch"])
            manifest = json.loads(Path(result["handoff"]).with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["prose"]["editor"]["model"], "claude-sonnet-4.6")

    def test_prepare_uses_configured_non_openai_editor_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(cli.init_project(type("Args", (), {"path": str(root), "beads": False})()), 0)
            local = {
                "schema": "agentflow.project-local@1", "version": 1,
                "prose": {"editor": {
                    "provider": "claude", "model": "claude-sonnet-5", "effort": "medium",
                }},
                "skills": [],
            }
            (root / ".agentflow/config.local.json").write_text(json.dumps(local), encoding="utf-8")
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            code, output, error = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude",
                "--require-skill", "domain-skill", "--check", "true", "--json",
            ])
            self.assertEqual((code, error), (0, ""))
            result = json.loads(output)
            self.assertIn("handoff launch claude", result["launch"])
            manifest = json.loads(Path(result["handoff"]).with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["prose"]["editor"]["model"], "claude-sonnet-5")

    def test_prepare_honors_disabled_editor_without_requiring_domain_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(cli.init_project(type("Args", (), {"path": str(root), "beads": False})()), 0)
            config = json.loads((root / ".agentflow/config.json").read_text(encoding="utf-8"))
            config["prose"] = {"editor": None}
            (root / ".agentflow/config.json").write_text(json.dumps(config), encoding="utf-8")
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            code, output, _ = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude", "--json",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["status"], "edit-required-no-route")

    def test_prepare_rejects_partial_or_unapproved_editor_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            common = [
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude",
            ]
            code, _, error = self.run_cli([*common, "--editor-provider", "copilot"])
            self.assertEqual(code, 2)
            self.assertIn("must be supplied together", error)
            code, _, error = self.run_cli([*common, "--editor-max-ai-credits", "30"])
            self.assertEqual(code, 2)
            self.assertIn("requires --editor-provider", error)
            code, _, error = self.run_cli([
                *common, "--editor-provider", "copilot", "--editor-model", "claude-opus-4.8",
                "--editor-effort", "high", "--editor-max-ai-credits", "30",
            ])
            self.assertEqual(code, 2)
            self.assertIn("not approved", error)

    def test_prepare_requires_domain_skill_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "draft.md").write_text(LONG, encoding="utf-8")
            code, _, error = self.run_cli([
                "prose", "prepare", "draft.md", "--cwd", str(root),
                "--profile", "technical-blog", "--writer-provider", "claude",
            ])
            self.assertEqual(code, 2)
            self.assertIn("--require-skill", error)


if __name__ == "__main__":
    unittest.main()
