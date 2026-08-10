from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.provider_argv import ProviderArgvError, build_argv


class ProviderArgvTests(unittest.TestCase):
    """Argv shapes captured from each installed provider CLI's real --help."""

    def test_codex_has_no_effort_flag(self) -> None:
        argv = build_argv("codex", "gpt-5.6-luna", "medium")
        self.assertEqual(argv, ["codex", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=medium"])
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--json", argv)

    def test_claude_uses_native_effort_flag(self) -> None:
        argv = build_argv("claude", "claude-sonnet-5", "high")
        self.assertEqual(argv, ["claude", "--model", "claude-sonnet-5", "--effort", "high"])

    def test_copilot_uses_native_effort_flag(self) -> None:
        argv = build_argv("copilot", "claude-sonnet-4.6", "medium")
        self.assertEqual(argv, ["copilot", "--model", "claude-sonnet-4.6", "--effort", "medium"])

    def test_claude_opus_5_uses_pinned_model_and_max_effort(self) -> None:
        argv = build_argv("claude", "claude-opus-5", "max")
        self.assertEqual(argv, ["claude", "--model", "claude-opus-5", "--effort", "max"])

    def test_copilot_opus_5_uses_pinned_model_and_xhigh_effort(self) -> None:
        argv = build_argv("copilot", "claude-opus-5", "xhigh")
        self.assertEqual(argv, ["copilot", "--model", "claude-opus-5", "--effort", "xhigh"])

    def test_command_override_replaces_argv0(self) -> None:
        argv = build_argv("codex", "gpt-5.6-luna", "high", command="/opt/bin/codex")
        self.assertEqual(argv[0], "/opt/bin/codex")

    def test_unsupported_provider_rejected(self) -> None:
        with self.assertRaises(ProviderArgvError):
            build_argv("unknown", "model", "medium")

    def test_missing_model_or_effort_rejected(self) -> None:
        with self.assertRaises(ProviderArgvError):
            build_argv("claude", "", "medium")
        with self.assertRaises(ProviderArgvError):
            build_argv("claude", "claude-sonnet-5", "")

    def test_codex_and_claude_take_a_positional_prompt(self) -> None:
        """AFREL-030: `codex [OPTIONS] [PROMPT]` and Claude Code's `[prompt]`
        argument are both positional, captured from --help."""
        for provider, expected_prefix in (
            ("codex", ["codex", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=medium"]),
            ("claude", ["claude", "--model", "claude-sonnet-5", "--effort", "high"]),
        ):
            model, effort = ("gpt-5.6-luna", "medium") if provider == "codex" else ("claude-sonnet-5", "high")
            argv = build_argv(provider, model, effort, prompt="Read and execute /tmp/handoff.md.")
            self.assertEqual(argv, expected_prefix + ["Read and execute /tmp/handoff.md."])

    def test_copilot_prompt_uses_interactive_flag_not_non_interactive_prompt_flag(self) -> None:
        """AFREL-030: copilot's `-p/--prompt` exits after completion (wrong
        for a persistent Herdr pane); `-i/--interactive <prompt>` starts
        interactively and executes it, captured from --help."""
        argv = build_argv("copilot", "claude-sonnet-4.6", "medium", prompt="Read and execute /tmp/handoff.md.")
        self.assertEqual(
            argv,
            ["copilot", "--model", "claude-sonnet-4.6", "--effort", "medium",
             "--interactive", "Read and execute /tmp/handoff.md."],
        )
        self.assertNotIn("-p", argv)
        self.assertNotIn("--prompt", argv)

    def test_no_prompt_means_no_prompt_argument(self) -> None:
        for provider, model, effort in (
            ("codex", "gpt-5.6-luna", "medium"),
            ("claude", "claude-sonnet-5", "high"),
            ("copilot", "claude-sonnet-4.6", "medium"),
        ):
            argv = build_argv(provider, model, effort)
            self.assertNotIn("--interactive", argv)


if __name__ == "__main__":
    unittest.main()
