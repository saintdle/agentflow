from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import cli, model_policy
from agentflow.provider_argv import build_argv
from agentflow.provider_capabilities import (
    CapabilityState,
    ProviderCapabilityError,
    capability_for,
    provider_capabilities,
)


PROVIDERS_DOC = Path(__file__).resolve().parents[1] / "docs/PROVIDERS.md"


def _smoke_shell_block() -> str:
    markdown = PROVIDERS_DOC.read_text(encoding="utf-8")
    smoke_section = markdown.split("## Opt-in live smoke", 1)[1]
    match = re.search(r"```sh\n(.*?)\n```", smoke_section, re.DOTALL)
    if match is None:
        raise AssertionError("provider live-smoke shell block is missing")
    return match.group(1)


class ProviderCapabilityContractTests(unittest.TestCase):
    def test_capability_matrix_keeps_copilot_native_usable_but_herdr_disabled(self) -> None:
        by_provider = {item.provider: item for item in provider_capabilities()}
        self.assertEqual(set(by_provider), {"codex", "claude", "copilot"})
        for provider in by_provider.values():
            self.assertEqual(provider.chat_controller, CapabilityState.SUPPORTED)
            self.assertEqual(provider.native_subagents, CapabilityState.SUPPORTED)
            self.assertEqual(provider.direct_cli, CapabilityState.SUPPORTED)
        self.assertEqual(by_provider["codex"].persistent_herdr, CapabilityState.SUPPORTED)
        self.assertEqual(by_provider["claude"].persistent_herdr, CapabilityState.CONDITIONAL)
        self.assertEqual(by_provider["claude"].herdr_minimum_version, "2.1.251")
        self.assertEqual(by_provider["copilot"].persistent_herdr, CapabilityState.DISABLED)

    def test_unknown_provider_and_lane_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProviderCapabilityError, "unsupported provider"):
            capability_for("unknown-provider")
        with self.assertRaisesRegex(ProviderCapabilityError, "unknown provider lane"):
            capability_for("copilot").lane_state("future-lane")

    def test_policy_approved_routes_use_the_real_provider_argv_contracts(self) -> None:
        policy = model_policy.load_policy()
        for capability in provider_capabilities():
            route = next(item for item in policy.routes if item.provider == capability.provider)
            role = route.roles[0]
            effort = route.efforts[0]
            decision = policy.validate_route(
                provider=capability.provider, role=role, model=route.model, effort=effort,
            )
            self.assertTrue(decision.ok, decision.reason)
            argv = build_argv(
                capability.provider, route.model, effort, prompt="synthetic compatibility probe",
            )
            self.assertEqual(argv[0], capability.provider)
            self.assertIn(route.model, argv)
            if capability.provider == "codex":
                self.assertEqual(argv[3:5], ["-c", f"model_reasoning_effort={effort}"])
                self.assertNotIn("--effort", argv)
            elif capability.provider == "claude":
                self.assertEqual(argv[3:5], ["--effort", effort])
                self.assertEqual(argv[-1], "synthetic compatibility probe")
            else:
                self.assertEqual(argv[3:5], ["--effort", effort])
                self.assertEqual(argv[-2:], ["--interactive", "synthetic compatibility probe"])

    def test_unknown_model_fails_closed_before_provider_lookup_or_spawn(self) -> None:
        args = argparse.Namespace(
            root=str(Path.cwd()), workflow_root="synthetic-root", provider="codex",
            role="controller", model="unknown-synthetic-model", effort="high",
        )
        output = io.StringIO()
        with mock.patch.object(cli, "_provider_command") as provider_command, \
             redirect_stdout(output), redirect_stderr(output):
            result = cli.herdr_launch(args)
        self.assertEqual(result, 2)
        self.assertIn("no exact route approves", output.getvalue())
        provider_command.assert_not_called()

    def test_copilot_persistent_launch_is_rejected_before_provider_lookup(self) -> None:
        args = argparse.Namespace(
            root=str(Path.cwd()), workflow_root="synthetic-root", provider="copilot",
            role="controller", model="claude-opus-4.8", effort="high",
        )
        output = io.StringIO()
        with mock.patch.object(cli, "_provider_command") as provider_command, \
             redirect_stdout(output), redirect_stderr(output):
            result = cli.herdr_launch(args)
        self.assertEqual(result, 2)
        self.assertIn("persistent Herdr Copilot actual-model attestation is unsupported", output.getvalue())
        provider_command.assert_not_called()

    def test_claude_native_model_switch_gate_accepts_minimum_and_rejects_older_or_unknown(self) -> None:
        def fake_version(version: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["claude", "--version"], returncode, version, "")

        with mock.patch.object(cli, "_provider_command", return_value="/fake/claude"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake_version("2.1.251")) as run:
            cli._require_claude_model_switch_version()
        self.assertEqual(run.call_args.args[0], ["/fake/claude", "--version"])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

        rejected = ("2.1.250", "not-a-version")
        for version in rejected:
            with self.subTest(version=version), \
                 mock.patch.object(cli, "_provider_command", return_value="/fake/claude"), \
                 mock.patch.object(cli.subprocess, "run", return_value=fake_version(version)), \
                 self.assertRaisesRegex(ValueError, "2.1.251 or newer|too old"):
                cli._require_claude_model_switch_version()

        with mock.patch.object(cli, "_provider_command", return_value=None), \
             self.assertRaisesRegex(ValueError, "2.1.251 or newer"):
            cli._require_claude_model_switch_version()


class ProviderSmokeRunbookTests(unittest.TestCase):
    """Execute the actual Markdown recipe against fake local commands only."""

    def _run_smoke(
        self, shell: str, *, live: str | None, provider: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, list[str]]]:
        with tempfile.TemporaryDirectory(prefix="agentflow smoke ") as temporary:
            base = Path(temporary)
            root = base / "workspace with spaces"
            bin_dir = base / "bin"
            logs_dir = base / "logs"
            root.mkdir()
            bin_dir.mkdir()
            logs_dir.mkdir()
            for name in ("agentflow", "bd"):
                command = bin_dir / name
                command.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" >> \"$CALL_LOG_DIR/{name}.log\"\n",
                    encoding="utf-8",
                )
                command.chmod(0o755)

            environment = os.environ.copy()
            environment.pop("LIVE", None)
            environment.update({
                "ROOT": str(root),
                "ROOT_ID": "workflow-smoke",
                "TASK_ID": "task-smoke",
                "PROVIDER": provider,
                "MODEL": "synthetic-model",
                "ROLE": "coding",
                "EFFORT": "medium",
                "CALL_LOG_DIR": str(logs_dir),
                "PATH": str(bin_dir) + os.pathsep + environment.get("PATH", ""),
            })
            if live is not None:
                environment["LIVE"] = live
            result = subprocess.run(
                [shell, "-c", _smoke_shell_block()],
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )
            logs = {
                name: (logs_dir / f"{name}.log").read_text(encoding="utf-8").splitlines()
                for name in ("agentflow", "bd")
                if (logs_dir / f"{name}.log").is_file()
            }
            return result, logs

    def test_unset_zero_or_invalid_live_guards_never_reach_fake_commands(self) -> None:
        shells = [shell for shell in ("/bin/sh", "/bin/bash") if shutil.which(shell)]
        self.assertTrue(shells, "a POSIX shell is required for the runbook contract test")
        blocked = (
            ("unset-live", None, "codex"),
            ("zero-live", "0", "codex"),
            ("copilot-is-not-a-herdr-worker", "1", "copilot"),
            ("unknown-provider", "1", "not-a-provider"),
        )
        for shell in shells:
            for case, live, provider in blocked:
                with self.subTest(shell=shell, case=case):
                    result, logs = self._run_smoke(shell, live=live, provider=provider)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(logs, {})

    def test_live_one_invokes_exactly_one_fake_controller_start(self) -> None:
        shells = [shell for shell in ("/bin/sh", "/bin/bash") if shutil.which(shell)]
        for shell in shells:
            with self.subTest(shell=shell):
                result, logs = self._run_smoke(shell, live="1", provider="codex")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                agentflow_calls = logs.get("agentflow", [])
                controller_calls = [
                    call for call in agentflow_calls if call.startswith("controller start ")
                ]
                self.assertEqual(len(controller_calls), 1)
                self.assertIn("beads explain workflow-smoke task-smoke", agentflow_calls)
                self.assertEqual(logs.get("bd"), ["show task-smoke --json"])
                self.assertIn("--deadline 180", controller_calls[0])
                self.assertIn("--workflow-root workflow-smoke", controller_calls[0])
                self.assertIn("provider=codex model=synthetic-model role=coding effort=medium", result.stdout)


if __name__ == "__main__":
    unittest.main()
