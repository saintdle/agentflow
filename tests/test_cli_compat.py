from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentflow import cli_compat


class CliCompatibilityTests(unittest.TestCase):
    def test_provider_uses_actual_required_flags(self):
        for provider, flags in cli_compat.PROVIDER_FLAGS.items():
            with self.subTest(provider=provider):
                def run(argv, **kwargs):
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="v1" if argv[-1] == "--version" else " ".join(flags),
                    )
                with mock.patch.object(cli_compat.subprocess, "run", side_effect=run):
                    self.assertTrue(cli_compat.probe_provider(provider, provider)["compatible"])
                with mock.patch.object(cli_compat.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="--model --effort-unsupported",
                )):
                    with self.assertRaises(cli_compat.CliCompatibilityError):
                        cli_compat.probe_provider(provider, provider)

    def test_herdr_requires_schema_and_commands(self):
        schema = {
            "protocol": 22, "schemas": {"request": {"$defs": {
                "WorkspaceCreateParams": {"properties": dict.fromkeys(("cwd", "env", "focus", "label"))},
                "PaneSendInputParams": {"properties": dict.fromkeys(("pane_id", "text", "keys"))},
            }}},
        }
        def run(argv, **kwargs):
            output = json.dumps(schema) if argv[-1] == "--json" else (
                "v0.9.5" if argv[-1] == "--version"
                else "--cwd --env --no-focus --label PANE_ID COMMAND target"
            )
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        with mock.patch.object(cli_compat.subprocess, "run", side_effect=run):
            self.assertEqual(cli_compat.probe_herdr("herdr")["protocol"], 22)
            del schema["schemas"]["request"]["$defs"]["PaneSendInputParams"]["properties"]["text"]
            with self.assertRaises(cli_compat.CliCompatibilityError):
                cli_compat.probe_herdr("herdr")

    def test_timeout_and_nonzero_are_explicit_errors(self):
        for result in (subprocess.TimeoutExpired("probe", 10), OSError("missing")):
            with mock.patch.object(cli_compat.subprocess, "run", side_effect=result):
                with self.assertRaises(cli_compat.CliCompatibilityError):
                    cli_compat.probe_provider("claude", "claude")
        with mock.patch.object(cli_compat.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 2, stdout="")):
            with self.assertRaises(cli_compat.CliCompatibilityError):
                cli_compat.probe_provider("codex", "codex")
