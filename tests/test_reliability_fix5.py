from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import shlex
import sys
import tempfile
import threading
import unittest
import venv
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import beads, cli, provider_argv
from tests import _state_home  # noqa: F401  # external controller authority


class ReliabilityFix5Tests(unittest.TestCase):
    def _attest_model(self, provider: str, session_id: str, model: str) -> None:
        cli.events_backend.EventSpool(cli._state_dir() / "events.jsonl").append(
            cli.events_backend.normalize_event(
                provider,
                {
                    "event": "session.start",
                    "session_id": session_id,
                    "model": model,
                },
                event_id=f"attest-{session_id}",
            )
        )

    def _authority(self, root: Path, workflow: str, lease) -> str:
        _, credentials = cli._controller_credentials(
            argparse.Namespace(
                root=str(root), workflow_root=workflow, resume_key_file=""
            ),
            lease,
        )
        return credentials["authority_secret"]

    def _handoff(self, root: Path, task: str = "task-auth", provider: str = "codex"):
        directory = root / ".agentflow/tmp/handoffs"
        directory.mkdir(parents=True)
        path = directory / f"{task}-{provider}.md"
        path.write_text("# Authenticated handoff\n", encoding="utf-8")
        report = {
            "schema": "agentflow.handoff-preflight@1",
            "path": str(path.resolve()), "root": str(root.resolve()),
            "provider": provider, "lane": "external", "checks": [],
            "context_files": 0, "context_bytes": 0,
            "required_skills": [], "required_tools": [],
            "acceptance_matrix": "", "errors": [],
        }
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "provider": provider,
                    "lane": "external",
                    "task_id": task,
                    "handoff": str(path),
                    "context": [],
                    "required_tools": [],
                    "output_boundary": str(root),
                    "machine_return_contract": {
                        "schema": "agentflow.return@1",
                        "acceptance_ids": ["AFREL-SMOKE-1"],
                        "approved_waivers": [],
                        "submit_command": provider_argv.fixed_submit_command(),
                    },
                    "preflight": {
                        "report": report,
                        "report_sha256": hashlib.sha256(
                            json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        return path, provider_argv.validate_confined_handoff(
            path, root=root, provider=provider, task_id=task
        )

    def test_confined_handoff_and_fixed_instruction_are_filesystem_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path, handoff = self._handoff(root)
            argv = provider_argv.build_confined_argv(
                "codex", "gpt-5.6-luna", "medium", handoff
            )
            self.assertIn(str(path), argv[-1])
            self.assertTrue(argv[-1].startswith("Read the validated Agentflow handoff artifact at "))

            outside = root / "outside.md"
            outside.write_text("# outside\n", encoding="utf-8")
            with self.assertRaises(provider_argv.ProviderArgvError):
                provider_argv.validate_confined_handoff(
                    outside, root=root, provider="codex", task_id="task-auth"
                )

    def test_direct_external_handoff_launch_requires_controller_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path, handoff = self._handoff(root)
            args = argparse.Namespace(
                provider="codex", file=str(path), cwd=str(root), role="coding",
                model="gpt-5.6-luna", effort="medium", policy="",
                print_command=False,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch.object(cli.subprocess, "call") as launch:
                self.assertEqual(cli.handoff_launch(args), 2)
            launch.assert_not_called()

    def test_persistent_transports_reject_hardened_handoffs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path, _ = self._handoff(root)
            manifest_path = path.with_suffix(".json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["isolation_profile"] = "hardened"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            handoff = provider_argv.validate_confined_handoff(
                path, root=root, provider="codex", task_id="task-auth"
            )

            for transport in ("direct launch", "Herdr"):
                with self.subTest(transport=transport), self.assertRaisesRegex(
                    ValueError, "refusing a hardened handoff"
                ):
                    cli._require_supported_launch_isolation(
                        handoff, transport=transport
                    )

            args = argparse.Namespace(
                provider="codex", file=str(path), cwd=str(root), role="coding",
                model="gpt-5.6-luna", effort="medium", policy="",
                print_command=False,
            )
            with mock.patch.object(cli, "_provider_command", return_value="/fake/codex"), \
                 mock.patch.object(cli.subprocess, "call") as launch:
                self.assertEqual(cli.handoff_launch(args), 2)
                launch.assert_not_called()

            path.write_bytes(b"# invalid\x00handoff\n")
            with self.assertRaises(provider_argv.ProviderArgvError):
                provider_argv.validate_confined_handoff(
                    path, root=root, provider="codex", task_id="task-auth"
                )

    def test_acceptance_disposition_rejects_unrelated_and_unapproved_results(self) -> None:
        contract = {"actor": "writer", "approved_waivers": []}
        with self.assertRaises(ValueError):
            cli._validate_acceptance_results(
                [{"acceptance_id": "OTHER", "status": "passed", "evidence": "x"}],
                ("A1",),
                contract,
            )
        with self.assertRaises(ValueError):
            cli._validate_acceptance_results(
                [{"acceptance_id": "A1", "status": "waived", "evidence": "x"}],
                ("A1",),
                contract,
            )
        with self.assertRaises(ValueError):
            cli._validate_acceptance_results(
                [{"acceptance_id": "A1", "status": "failed", "evidence": "x"}],
                ("A1",),
                contract,
            )

    def test_claim_identity_is_opaque_and_per_issuance(self) -> None:
        first = beads.ClaimIdentity("workflow", "task", "actor", "claim")
        second = beads.ClaimIdentity("workflow", "task", "actor", "claim")
        self.assertNotEqual(first.token, second.token)
        self.assertGreaterEqual(len(first.token), 32)
        self.assertNotEqual(first.token, "workflow/task/actor")

    def test_authenticated_result_consumes_once_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workflow = "workflow-auth-smoke"
            lease = cli.controller_backend.RootController(
                str(root),
                "agentflow-controller",
                state_path=cli._controller_state_dir(root, workflow) / "state.json",
            ).acquire()
            authority_secret = self._authority(root, workflow, lease)
            task, actor, provider, launch_id = "task-auth", "writer", "codex", "launch-auth"
            claim_token = "opaque-" + ("x" * 48)
            _, handoff = self._handoff(root, task, provider)
            state_path = root / ".agentflow/herdr/sessions.json"
            channel = cli._mint_return_channel(
                root,
                workflow,
                task_id=task,
                actor=actor,
                claim_token=claim_token,
                lease_id=lease.token,
                launch_id=launch_id,
                provider=provider,
                model="gpt-5.6-luna",
                effort="high",
                handoff=handoff,
                acceptance_ids=("AFREL-SMOKE-1",),
                state_path=state_path,
                controller_id=lease.controller,
                lease_epoch=lease.epoch,
                continuity_id=lease.continuity_id,
                authority_secret=authority_secret,
            )
            binding = {
                "root": str(root),
                "task_id": task,
                "lease_id": lease.token,
                "launch_id": launch_id,
                "provider": provider,
                "session_id": "provider-smoke",
                "claim_id": "claim-auth",
            }
            self._attest_model(provider, "provider-smoke", "gpt-5.6-luna")
            cli._herdr_write(
                state_path,
                {
                    "schema": "agentflow.herdr",
                    "version": 1,
                    "sessions": {
                        task: {
                            "root": str(root),
                            "task_id": task,
                            "lease_id": lease.token,
                            "launch_id": launch_id,
                            "provider": provider,
                            "status": "launched",
                            "binding": binding,
                            "return_channel": {
                                "state": "issued",
                                "capability_sha256": channel["capability_digest"],
                                "contract_sha256": channel["contract_sha256"],
                                "contract_binding": channel["contract"],
                                "contract_path": str(channel["contract_path"]),
                                "capability_file": str(channel["capability_path"]),
                                "result_path": str(channel["result_path"]),
                                "acceptance_ids": ["AFREL-SMOKE-1"],
                                "approved_waivers": [],
                            },
                        }
                    },
                },
            )
            channel["result_path"].write_text(
                json.dumps(
                    {
                        "outcome": "completed",
                        "session_id": "provider-smoke",
                        "acceptance_results": [
                            {
                                "acceptance_id": "AFREL-SMOKE-1",
                                "status": "passed",
                                "evidence": "real locked transaction",
                                "source": "provider-smoke",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                root=str(root),
                contract=str(channel["contract_path"]),
                file=str(channel["result_path"]),
                json=True,
                _controller_ingest=True,
                _capability_file=str(channel["capability_path"]),
                _authority_secret=authority_secret,
            )
            issue = {
                "id": task,
                "status": "in_progress",
                "assignee": actor,
                "parent_id": workflow,
                "metadata": {
                    "agentflow": {
                        "root": workflow,
                        "task": task,
                        "actor": actor,
                        "claim_token": claim_token,
                        "claim_id": "claim-auth",
                        "acceptance": {
                            "version": 1,
                            "task_id": task,
                            "rows": [{
                                "id": "AFREL-SMOKE-1",
                                "outcome": "authenticated result is consumed",
                                "owner": "controller",
                                "lane": "local-runtime",
                                "planned_evidence": "locked transaction",
                                "status": "planned",
                            }],
                        },
                    }
                },
            }
            results: list[int] = []

            def submit() -> None:
                with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                     mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None):
                    results.append(cli.herdr_result(args))

            threads = [threading.Thread(target=submit) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            record = json.loads(state_path.read_text(encoding="utf-8"))["sessions"][task]
            self.assertEqual(sorted(results), [0, 2])
            self.assertEqual(record["return_channel"]["state"], "consumed")
            self.assertFalse(channel["capability_path"].exists())
            self.assertEqual(record["result"]["acceptance_results"][0]["acceptance_id"], "AFREL-SMOKE-1")

    def test_authenticated_result_survives_same_controller_reattach(self) -> None:
        """A rotated public lease token does not strand an in-flight result."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workflow = "workflow-reattach"
            state_dir = cli._controller_state_dir(root, workflow)
            controller = cli.controller_backend.RootController(
                str(root), "controller-a", state_path=state_dir / "state.json"
            )
            lease = controller.acquire()
            authority_secret = self._authority(root, workflow, lease)
            _, handoff = self._handoff(root, "task-reattach", "codex")
            state_path = root / ".agentflow/herdr/sessions.json"
            channel = cli._mint_return_channel(
                root, workflow, task_id="task-reattach", actor="writer",
                claim_token="opaque-" + ("x" * 48), lease_id=lease.token,
                launch_id="launch-reattach", provider="codex",
                model="gpt-5.6-luna", effort="high", handoff=handoff,
                acceptance_ids=("AFREL-SMOKE-1",), state_path=state_path,
                controller_id=lease.controller, lease_epoch=lease.epoch,
                continuity_id=lease.continuity_id,
                authority_secret=authority_secret,
            )
            cli._private_atomic_json(state_path, {
                "schema": "agentflow.herdr", "version": 1,
                "sessions": {"task-reattach": {
                    "root": str(root), "task_id": "task-reattach", "lease_id": lease.token,
                    "launch_id": "launch-reattach", "provider": "codex", "status": "launched",
                    "binding": {
                        "root": str(root), "task_id": "task-reattach", "lease_id": lease.token,
                        "launch_id": "launch-reattach", "provider": "codex", "session_id": "provider-session",
                    },
                    "return_channel": {
                        "state": "issued", "capability_sha256": channel["capability_digest"],
                        "contract_sha256": channel["contract_sha256"],
                        "contract_binding": channel["contract"],
                        "contract_path": str(channel["contract_path"]), "result_path": str(channel["result_path"]),
                        "capability_file": str(channel["capability_path"]),
                        "acceptance_ids": ["AFREL-SMOKE-1"], "approved_waivers": [],
                    },
                }},
            })
            self._attest_model("codex", "provider-session", "gpt-5.6-luna")
            channel["result_path"].write_text(json.dumps({
                "outcome": "completed", "session_id": "provider-session",
                "acceptance_results": [{
                    "acceptance_id": "AFREL-SMOKE-1", "status": "passed",
                    "evidence": "reattach-safe result", "source": "provider",
                }],
            }), encoding="utf-8")
            reattached = cli.controller_backend.RootController(
                str(root), "controller-a", state_path=state_dir / "state.json"
            ).acquire(resume_proof=lease.resume_secret)
            self.assertNotEqual(reattached.token, lease.token)
            issue = {
                "id": "task-reattach", "status": "in_progress", "assignee": "writer",
                "parent_id": workflow,
                "metadata": {"agentflow": {
                    "root": workflow, "task": "task-reattach", "actor": "writer",
                    "claim_id": "claim-reattach", "claim_token": "opaque-" + ("x" * 48),
                    "acceptance": {
                        "version": 1,
                        "task_id": "task-reattach",
                        "rows": [{
                            "id": "AFREL-SMOKE-1",
                            "outcome": "result survives authenticated reattach",
                            "owner": "controller",
                            "lane": "local-runtime",
                            "planned_evidence": "provider return",
                            "status": "planned",
                        }],
                    },
                }},
            }
            args = argparse.Namespace(
                root=str(root), contract=str(channel["contract_path"]),
                file=str(channel["result_path"]), json=True,
                _controller_ingest=True,
                _capability_file=str(channel["capability_path"]),
                _authority_secret=authority_secret,
            )
            with mock.patch.object(cli.beads_backend, "get_issue", return_value=issue), \
                 mock.patch.object(cli.beads_backend, "verify_task_ancestry_and_ownership", return_value=None):
                self.assertEqual(cli.herdr_result(args), 0)
            record = json.loads(state_path.read_text(encoding="utf-8"))["sessions"]["task-reattach"]
            self.assertEqual(record["return_channel"]["state"], "consumed")

    def test_gitignore_migration_is_idempotent_and_git_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".gitignore").write_text(
                "# BEGIN agentflow local artifacts\n"
                ".agentflow/handoffs/\n.agentflow/tmp/\n"
                "# END agentflow local artifacts\ncustom/\n",
                encoding="utf-8",
            )
            self.assertEqual(cli._ensure_gitignore(root), "updated")
            self.assertIn("custom/", (root / ".gitignore").read_text(encoding="utf-8"))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertEqual(cli._ensure_gitignore(root), "unchanged")
            for path in (
                ".agentflow/controller/x",
                ".agentflow/herdr/x",
                ".agentflow/claims/x",
                ".agentflow/runtime/x",
                ".agentflow/handoffs/x",
                ".agentflow/tmp/x",
                ".agentflow/logs/x",
                ".agentflow/worktrees/x",
            ):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", path],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, path)

    def test_public_init_halts_on_tracked_runtime_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runtime_secret = root / ".agentflow/runtime/launch/secret"
            runtime_secret.parent.mkdir(parents=True)
            runtime_secret.write_text("private", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", ".agentflow/runtime/launch/secret"], check=True)
            result = cli.init_project(argparse.Namespace(path=str(root), beads=False))
            self.assertEqual(result, 2)

    def test_emitted_submit_command_parses_without_a_task_argument(self) -> None:
        command = shlex.split(provider_argv.fixed_submit_command())
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(command[1:4], ["-I", "-m", "agentflow"])
        parsed = cli.build_parser().parse_args(command[4:])
        self.assertEqual(parsed.herdr_command, "submit")
        self.assertFalse(hasattr(parsed, "task"))
        self.assertEqual(parsed.contract, "$AGENTFLOW_RESULT_CONTRACT")

    @unittest.skipUnless(os.name == "posix", "requires POSIX shell expansion")
    def test_installed_submit_command_runs_with_sparse_daemon_path(self) -> None:
        """The signed handoff uses its installed interpreter, not daemon PATH."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            environment = base / "installed-env"
            venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            site_probe = subprocess.run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                capture_output=True, text=True, check=True,
            )
            installed_package = Path(site_probe.stdout.strip()) / "agentflow"
            shutil.copytree(Path(__file__).resolve().parents[1] / "src/agentflow", installed_package)

            sparse_path = base / "sparse-path"
            sparse_path.mkdir()
            stale_import = base / "stale-import"
            stale_package = stale_import / "agentflow"
            stale_package.mkdir(parents=True)
            (stale_package / "__init__.py").write_text("raise RuntimeError('stale import')\n", encoding="utf-8")
            worker_env = {key: value for key, value in os.environ.items()
                          if key not in {"PYTHONPATH", "PYTHONHOME"}}
            worker_env.update({
                "PATH": str(sparse_path), "PYTHONPATH": str(stale_import),
                "HOME": str(base / "home"),
                "AGENTFLOW_STATE_HOME": str(base / "state-home"),
            })
            Path(worker_env["HOME"]).mkdir()
            handoff_path = workspace / ".agentflow/tmp/handoffs/sparse-task.md"
            generated = subprocess.run(
                [
                    str(python), "-I", "-m", "agentflow", "handoff", "create",
                    "--to", "claude", "--title", "Sparse path return",
                    "--goal", "Submit a bounded result", "--task-id", "sparse-task",
                    "--lane", "external", "--tool-profile", "shell-write",
                    "--out", str(handoff_path), "--cwd", str(workspace),
                ],
                cwd=workspace, env=worker_env, capture_output=True, text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            manifest = json.loads(handoff_path.with_suffix(".json").read_text(encoding="utf-8"))
            command = manifest["machine_return_contract"]["submit_command"]
            command_argv = shlex.split(command)
            self.assertEqual(command_argv[0], str(python.absolute()))
            self.assertEqual(command_argv[1:4], ["-I", "-m", "agentflow"])

            launch_dir = workspace / ".agentflow/runtime" / ("a" * 32) / "sparse-launch"
            launch_dir.mkdir(parents=True)
            contract_path = launch_dir / "return.contract.json"
            result_path = launch_dir / "result.json"
            submission_path = launch_dir / "submitted.json"
            contract_path.write_text(json.dumps({
                "schema": "agentflow.return@1", "task_id": "sparse-task",
                "result_path": str(result_path), "submission_file": str(submission_path),
            }), encoding="utf-8")
            result_path.write_text(json.dumps({
                "outcome": "completed", "acceptance_results": [],
            }), encoding="utf-8")
            submit_env = {
                **worker_env,
                "AGENTFLOW_RESULT_CONTRACT": str(contract_path),
                "AGENTFLOW_RESULT_FILE": str(result_path),
            }
            old_command = (
                'agentflow herdr submit --contract "$AGENTFLOW_RESULT_CONTRACT" '
                '--file "$AGENTFLOW_RESULT_FILE"'
            )
            before_fix = subprocess.run(
                ["/bin/sh", "-c", old_command], cwd=workspace, env=submit_env,
                capture_output=True, text=True,
            )
            self.assertNotEqual(before_fix.returncode, 0)
            self.assertFalse(submission_path.exists())
            submitted = subprocess.run(
                ["/bin/sh", "-c", command], cwd=workspace, env=submit_env,
                capture_output=True, text=True,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            marker = json.loads(submission_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["schema"], "agentflow.result-submission@1")
            self.assertEqual(marker["contract_sha256"], hashlib.sha256(contract_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
