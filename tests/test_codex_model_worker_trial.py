from __future__ import annotations

import ast
import builtins
from dataclasses import FrozenInstanceError, replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentflow import codex_app_server
from agentflow.controller import FencedLease, RootController


HARNESS_PATH = ROOT / "scripts/trials/codex_model_worker_trial.py"
SPEC = importlib.util.spec_from_file_location("codex_model_worker_trial", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)


class TrialHarnessTests(unittest.TestCase):
    def _running(self, directory: str):
        path = Path(directory) / "trial-state.json"
        harness = trial.TrialHarness(path)
        harness.preflight(trial.active_profile_evidence())
        identity = harness.start_fake_worker()
        return harness, path, identity

    def test_source_and_fixed_command_have_no_sdk_or_transport_wiring(self) -> None:
        source = HARNESS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        self.assertTrue(all(name in {
            "__future__", "dataclasses", "enum", "hashlib", "json", "os",
            "pathlib", "sys", "tempfile", "typing",
        } for name in imports), imports)
        for forbidden in ("openai_codex", "start_supervised_turn", "_sdk_modules", "agentflow.cli"):
            self.assertNotIn(forbidden, source)

        original_import = builtins.__import__

        def deny_live_import(name, *args, **kwargs):
            if name.startswith(("openai_codex", "agentflow.codex_app_server", "agentflow.cli")):
                raise AssertionError("synthetic harness attempted a live/runtime import")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=deny_live_import):
            result = trial._run_fixed_simulation()
        self.assertTrue(result["result"]["simulation_only"])
        report = result["report"]
        self.assertEqual(report["attempted_thread_starts"], 1)
        self.assertEqual(report["attempted_turn_starts"], 1)
        self.assertEqual(report["simulated_thread_start_calls"], 1)
        self.assertEqual(report["simulated_turn_start_calls"], 1)
        self.assertEqual(report["controller_restart_count"], 1)
        self.assertEqual(report["consumption_count"], 1)
        self.assertFalse(report["limits"]["hard_spend_cap"])
        self.assertTrue(report["limits"]["cancellation_best_effort"])
        self.assertNotIn("lime-otter-73", json.dumps(result))

    def test_inventory_is_immutable_and_within_prompt_workspace_bounds(self) -> None:
        inventory = trial.SYNTHETIC_OUTBOUND_INVENTORY
        self.assertIsInstance(inventory, tuple)
        self.assertLessEqual(sum(item.size_bytes for item in inventory), trial.MAX_WORKSPACE_BYTES)
        self.assertLessEqual(len(trial._INSTRUCTIONS_TEXT.encode()), trial.MAX_PROMPT_BYTES)
        with self.assertRaises(FrozenInstanceError):
            inventory[0].name = "changed.txt"
        with self.assertRaises(TypeError):
            inventory[0] = inventory[1]

    def test_profile_proof_is_typed_exact_pre_turn_and_bounded(self) -> None:
        passing = trial.active_profile_evidence()
        cases = (
            replace(passing, source="profile_catalogue"),
            replace(passing, source="legacy_sandbox"),
            replace(passing, active_profile="different-profile"),
            replace(passing, active_profile=None),
            replace(passing, elapsed_ms=trial.MAX_PROFILE_WAIT_MS + 1),
            replace(passing, event="timeout", elapsed_ms=trial.MAX_PROFILE_WAIT_MS),
            replace(passing, before_first_turn=False),
            replace(passing, legacy_sandbox_present=True),
            replace(passing, tool_surfaces=("shell", "mcp")),
            replace(passing, elapsed_ms=True),
            object(),
        )
        for index, evidence in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                harness = trial.TrialHarness(Path(temporary) / "state.json")
                with self.assertRaises(trial.TrialHalt):
                    harness.preflight(evidence)
                report = harness.report()
                self.assertEqual(report["preflight_attempts"], 1)
                self.assertEqual(report["attempted_thread_starts"], 0)
                self.assertEqual(report["attempted_turn_starts"], 0)
                self.assertFalse(report["profile_ready"])

        with tempfile.TemporaryDirectory() as temporary:
            harness = trial.TrialHarness(Path(temporary) / "state.json")
            harness.preflight(replace(passing, elapsed_ms=trial.MAX_PROFILE_WAIT_MS))
            self.assertTrue(harness.report()["profile_ready"])

    def test_no_inference_preflight_is_capped_at_two_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = trial.TrialHarness(Path(temporary) / "state.json")
            invalid = replace(trial.active_profile_evidence(), source="catalogue")
            for _ in range(trial.MAX_PREFLIGHT_ATTEMPTS):
                with self.assertRaises(trial.TrialHalt):
                    harness.preflight(invalid)
            with self.assertRaisesRegex(trial.TrialHalt, "preflight_attempt_budget_exhausted"):
                harness.preflight(trial.active_profile_evidence())
            report = harness.report()
            self.assertEqual(report["preflight_attempts"], 2)
            self.assertEqual(report["simulated_turn_start_calls"], 0)

    def test_ambiguous_turn_start_is_durably_counted_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            harness = trial.TrialHarness(path)
            harness.preflight(trial.active_profile_evidence())
            with self.assertRaisesRegex(trial.TrialHalt, "turn_start_outcome_ambiguous"):
                harness.simulate_ambiguous_turn_start()

            resumed = trial.TrialHarness(path)
            before = resumed.report()
            self.assertEqual(before["attempted_thread_starts"], 1)
            self.assertEqual(before["attempted_turn_starts"], 1)
            self.assertEqual(before["simulated_thread_start_calls"], 1)
            self.assertEqual(before["simulated_turn_start_calls"], 1)
            self.assertEqual(before["requests"][0]["worker_state"], "ambiguous")
            with self.assertRaises(trial.TrialHalt):
                resumed.start_fake_worker()
            after = resumed.report()
            self.assertEqual(after["attempted_turn_starts"], before["attempted_turn_starts"])
            self.assertEqual(after["simulated_turn_start_calls"], before["simulated_turn_start_calls"])

    def test_malformed_state_and_fake_call_errors_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(trial.TrialHalt, "trial_state_unavailable"):
                trial.TrialHarness(path)

            invalid = trial._new_state()
            invalid["preflight_attempts"] = "two"
            trial._save_state(path, invalid)
            with self.assertRaisesRegex(trial.TrialHalt, "trial_state_accounting_invalid"):
                trial.TrialHarness(path)

            invalid = trial._new_state()
            invalid["continuity_id"] = "other-owner"
            trial._save_state(path, invalid)
            with self.assertRaisesRegex(trial.TrialHalt, "trial_state_accounting_invalid"):
                trial.TrialHarness(path)

        with tempfile.TemporaryDirectory() as temporary:
            harness = trial.TrialHarness(Path(temporary) / "state.json")
            harness.preflight(trial.active_profile_evidence())
            with mock.patch.object(
                trial._FakeClient, "thread_start", side_effect=RuntimeError("private provider detail")
            ):
                with self.assertRaises(trial.TrialHalt) as caught:
                    harness.start_fake_worker()
            self.assertEqual(caught.exception.code, "simulated_thread_start_failed")
            self.assertNotIn("private provider detail", str(caught.exception))
            self.assertEqual(harness.report()["attempted_thread_starts"], 1)
            self.assertEqual(harness.report()["simulated_turn_start_calls"], 0)

    def test_same_owner_restart_preserves_exact_turn_rotates_lease_and_consumes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness, path, first_identity = self._running(temporary)
            before = harness.report()
            evidence = trial.TrialHarness(path).restart_evidence()
            resumed = trial.TrialHarness(path)
            recovered = resumed.resume_after_controller_restart(evidence)
            after = resumed.report()
            raw = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(recovered, first_identity)
            self.assertEqual(after["controller_restart_count"], 1)
            self.assertEqual(after["attempted_thread_starts"], before["attempted_thread_starts"])
            self.assertEqual(after["attempted_turn_starts"], before["attempted_turn_starts"])
            self.assertEqual(after["simulated_thread_start_calls"], before["simulated_thread_start_calls"])
            self.assertEqual(after["simulated_turn_start_calls"], before["simulated_turn_start_calls"])
            self.assertEqual(raw["lease_epoch"], 2)
            self.assertNotEqual(raw["lease_token"], evidence.lease_token)
            self.assertEqual(raw["continuity_id"], evidence.continuity_id)
            self.assertEqual(
                (after["requests"][0]["request_id"], after["requests"][0]["thread_id"], after["requests"][0]["turn_id"]),
                (evidence.request_id, evidence.thread_id, evidence.turn_id),
            )

            resumed.complete_fake_turn()
            result = resumed.consume_fake_result()
            self.assertTrue(result["simulation_only"])
            self.assertEqual(result["request_id"], evidence.request_id)
            self.assertEqual(resumed.report()["consumption_count"], 1)
            with self.assertRaises(trial.TrialHalt):
                resumed.consume_fake_result()
            self.assertEqual(resumed.report()["consumption_count"], 1)
            self.assertEqual(resumed.report()["simulated_turn_start_calls"], 1)

    def test_stale_replay_owner_continuity_or_dead_process_evidence_halts_without_dispatch(self) -> None:
        failures = (
            {"owner_id": "different-owner"},
            {"claim_id": "stale-claim"},
            {"request_id": "stale-request"},
            {"continuity_id": "different-continuity"},
            {"thread_id": "different-thread"},
            {"turn_id": "different-turn"},
            {"lease_epoch": 0},
            {"lease_token": "stale-token"},
            {"helper_state": "dead"},
            {"server_state": "dead"},
        )
        for index, change in enumerate(failures):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                _harness, path, _identity = self._running(temporary)
                resumed = trial.TrialHarness(path)
                stale_evidence = replace(resumed.restart_evidence(), **change)
                with self.assertRaises(trial.TrialHalt):
                    resumed.resume_after_controller_restart(stale_evidence)
                report = resumed.report()
                self.assertEqual(report["attempted_thread_starts"], 1, index)
                self.assertEqual(report["attempted_turn_starts"], 1, index)
                self.assertEqual(report["simulated_thread_start_calls"], 1, index)
                self.assertEqual(report["simulated_turn_start_calls"], 1, index)
                self.assertEqual(report["controller_restart_count"], 0)

    def test_restart_evidence_replay_and_ambiguous_recovery_halt_without_new_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness, path, _identity = self._running(temporary)
            stale = harness.restart_evidence()
            harness.resume_after_controller_restart(stale)
            with self.assertRaises(trial.TrialHalt):
                harness.resume_after_controller_restart(stale)
            self.assertEqual(harness.report()["simulated_turn_start_calls"], 1)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            harness = trial.TrialHarness(path)
            harness.preflight(trial.active_profile_evidence())
            with self.assertRaises(trial.TrialHalt):
                harness.simulate_ambiguous_turn_start()
            resumed = trial.TrialHarness(path)
            with self.assertRaises(trial.TrialHalt):
                resumed.resume_after_controller_restart(resumed.restart_evidence())
            self.assertEqual(resumed.report()["simulated_turn_start_calls"], 1)

    def test_usage_unknown_unattributable_and_threshold_block_second_turn(self) -> None:
        for case in (trial.UsageCase.UNKNOWN, trial.UsageCase.UNATTRIBUTABLE, trial.UsageCase.LIMIT):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                harness, path, _identity = self._running(temporary)
                harness.complete_fake_turn(case)
                harness.consume_fake_result()
                next_controller = trial.TrialHarness(path)
                next_controller.preflight(trial.active_profile_evidence())
                with self.assertRaisesRegex(trial.TrialHalt, "usage_admission_halted"):
                    next_controller.start_fake_worker()
                report = next_controller.report()
                self.assertEqual(report["attempted_turn_starts"], 1)
                self.assertEqual(report["simulated_turn_start_calls"], 1)

    def test_two_turn_budget_persists_and_warning_threshold_is_not_a_spend_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness, path, first = self._running(temporary)
            harness.complete_fake_turn(trial.UsageCase.WARNING)
            warning_result = harness.consume_fake_result()
            self.assertEqual(warning_result["usage_status"], "warning_threshold_reached")

            second_controller = trial.TrialHarness(path)
            second_controller.preflight(trial.active_profile_evidence())
            second = second_controller.start_fake_worker()
            self.assertNotEqual(first["request_id"], second["request_id"])
            self.assertEqual(second_controller.report()["attempted_turn_starts"], 2)
            second_controller.complete_fake_turn()
            second_controller.consume_fake_result()

            last = trial.TrialHarness(path)
            with self.assertRaises(trial.TrialHalt):
                last.preflight(trial.active_profile_evidence())
            report = last.report()
            self.assertEqual(report["attempted_turn_starts"], 2)
            self.assertEqual(report["simulated_turn_start_calls"], 2)
            self.assertFalse(report["limits"]["hard_spend_cap"])

    def test_production_lease_and_recovery_identity_apis_are_used_without_sdk_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            controller_state = root / "controller.json"
            checkpoint = root / "checkpoint.json"
            first_controller = RootController(
                root, "synthetic-controller", state_path=controller_state,
                checkpoint_path=checkpoint, owner_id="process-before-restart",
            )
            first_lease = first_controller.acquire()
            first_controller.reserve_active_task(
                {"task": "synthetic-task", "root": str(root), "actor": "writer", "claim_id": "synthetic-claim-001"},
                lease=first_lease,
            )

            harness = trial.TrialHarness(root / "trial-state.json")
            harness.preflight(trial.active_profile_evidence())
            fake_identity = harness.start_fake_worker()
            session_id = f"codex-app-server:{fake_identity['thread_id']}:{fake_identity['turn_id']}"
            first_controller.bind_active_task("synthetic-task", session_id=session_id, lease=first_lease)

            snapshot = {
                "workspace_root": str(root), "workflow_root": "synthetic-workflow",
                "task_id": "synthetic-task", "claim_id": "synthetic-claim-001",
                "lease_epoch": first_lease.epoch, "continuity_id": first_lease.continuity_id,
                "lease_id": first_lease.token, "cwd": str(root), "model": trial.MODEL,
                "effort": trial.EFFORT,
            }
            identity = codex_app_server.bind_identity(
                {
                    **snapshot,
                    "lease_continuity_id": first_lease.continuity_id,
                    "lease_token_sha256": __import__("hashlib").sha256(first_lease.token.encode()).hexdigest(),
                    "thread_id": fake_identity["thread_id"], "turn_id": fake_identity["turn_id"],
                    "sdk_version": codex_app_server.SUPPORTED_SDK_VERSION,
                    "model_evidence": "codex-app-server-protocol-cooperative",
                },
                workspace_root=str(root), workflow_root="synthetic-workflow",
                task_id="synthetic-task", claim_id="synthetic-claim-001",
                lease_epoch=first_lease.epoch, lease_continuity_id=first_lease.continuity_id,
                lease_token=first_lease.token, cwd=str(root), model=trial.MODEL, effort=trial.EFFORT,
            )

            resumed_controller = RootController(
                root, "synthetic-controller", state_path=controller_state,
                checkpoint_path=checkpoint, owner_id="process-after-restart",
            )
            resumed_lease = resumed_controller.acquire(resume_proof=first_lease.resume_secret)
            self.assertEqual(resumed_lease.epoch, first_lease.epoch + 1)
            self.assertNotEqual(resumed_lease.token, first_lease.token)
            self.assertEqual(resumed_lease.continuity_id, first_lease.continuity_id)
            with self.assertRaises(FencedLease):
                first_controller.assert_lease(first_lease)
            recovered = codex_app_server.bind_recovered_identity(
                identity.to_dict(), launch_snapshot=snapshot,
                current_continuity_id=resumed_lease.continuity_id,
            )
            self.assertEqual((recovered.thread_id, recovered.turn_id), (
                fake_identity["thread_id"], fake_identity["turn_id"],
            ))
            with self.assertRaises(codex_app_server.CodexAppServerError):
                codex_app_server.bind_recovered_identity(
                    identity.to_dict(), launch_snapshot=snapshot,
                    current_continuity_id="different-owner-continuity",
                )

            harness_after_restart = trial.TrialHarness(root / "trial-state.json")
            harness_after_restart.resume_after_controller_restart(
                harness_after_restart.restart_evidence(),
            )
            harness_after_restart.complete_fake_turn()
            harness_after_restart.consume_fake_result()
            resumed_controller.complete_active_task("synthetic-task", lease=resumed_lease)
            self.assertEqual(harness_after_restart.report()["simulated_turn_start_calls"], 1)


if __name__ == "__main__":
    unittest.main()
