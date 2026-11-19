"""Opt-in real Herdr contract test; never starts an AI provider."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentflow import cli, execution_limits, provider_argv


@unittest.skipUnless(
    os.environ.get("AGENTFLOW_HERDR_INTEGRATION") == "1" and shutil.which("herdr"),
    "set AGENTFLOW_HERDR_INTEGRATION=1 with Herdr installed (no provider calls)",
)
class HerdrRuntimeTests(unittest.TestCase):
    def test_real_pane_preserves_argv_environment_and_supervised_deadline(self):
        executable = shutil.which("herdr")
        with tempfile.TemporaryDirectory(prefix="af-herdr-") as temp:
            root = Path(temp).resolve()
            home = root / "home"
            home.mkdir()
            environment = {
                "HOME": str(home), "PATH": os.defpath, "SHELL": "/bin/sh",
                "TERM": "xterm-256color", "LANG": "en_US.UTF-8",
                "HERDR_SOCKET_PATH": str(root / "herdr.sock"),
                "HERDR_CONFIG_PATH": str(root / "config.toml"),
            }
            def herdr(*args):
                return subprocess.run(
                    [executable, *args], env=environment, capture_output=True,
                    text=True, timeout=15, check=True,
                )
            server = subprocess.Popen(
                [executable, "server"], env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    self.assertIsNone(server.poll(), "isolated Herdr server exited")
                    if (root / "herdr.sock").exists():
                        break
                    time.sleep(0.1)
                self.assertIn("status: running", herdr("status", "server").stdout)
                created = json.loads(herdr(
                    "workspace", "create", "--cwd", str(root), "--no-focus",
                    "--env", "AF_RUNTIME_PROBE=workspace-value",
                ).stdout)
                pane = created["result"]["root_pane"]["pane_id"]
                output = root / "result with space ; literal.json"
                arguments = ["two  spaces", "$HOME", "; echo unwanted", "Bash(git:*)", "it's", "", "x" * 2048]
                code = (
                    "import json,os,sys; from pathlib import Path; "
                    "Path(sys.argv[1]).write_text(json.dumps({"
                    "'argv':sys.argv[2:],'env':os.environ['AF_RUNTIME_PROBE']}))"
                )
                arguments = [
                    "env", "AF_RUNTIME_PROBE=provider-value",
                    sys.executable, "-c", code, str(output), *arguments,
                ]
                canonical_ready = root / "canonical-ready"
                herdr("pane", "run", pane, "stty icanon; touch " + shlex.quote(str(canonical_ready)) + "; sleep 3")
                deadline = time.monotonic() + 10
                while not canonical_ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(canonical_ready.exists())
                with self.assertRaises(subprocess.CalledProcessError) as rejected:
                    herdr("pane", "run", pane, provider_argv.pane_command(arguments))
                self.assertIn("canonical mode", rejected.exception.stderr)
                herdr("pane", "run", pane, cli._herdr_launch_command(arguments, root / "launch.sh"))
                deadline = time.monotonic() + 10
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(output.exists(), "Herdr did not execute the quoted command")
                self.assertEqual(json.loads(output.read_text()), {
                    "argv": arguments[6:], "env": "provider-value",
                })
                exit_file = root / "deadline-exit"
                bounded = provider_argv.pane_command([
                    sys.executable, str(Path(execution_limits.__file__).resolve()),
                    "--deadline-epoch", str(time.time() + 2), "--",
                    sys.executable, "-c", "import time; time.sleep(30)",
                ])
                started = time.monotonic()
                herdr("pane", "run", pane, bounded + "; printf '%s' $? > " + shlex.quote(str(exit_file)))
                while not exit_file.exists() and time.monotonic() - started < 10:
                    time.sleep(0.1)
                self.assertTrue(exit_file.exists(), "deadline supervisor did not exit")
                self.assertEqual(exit_file.read_text(), "124")
                self.assertLess(time.monotonic() - started, 10)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
