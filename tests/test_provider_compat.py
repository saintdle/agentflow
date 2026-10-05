from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
