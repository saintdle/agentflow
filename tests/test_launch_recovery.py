from __future__ import annotations

from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.launch_recovery import (
    herdr_pane_is_definitively_absent,
    reduce_incomplete_launch,
)


class LaunchRecoveryReducerTests(unittest.TestCase):
    def test_only_structured_herdr_pane_not_found_proves_absence(self) -> None:
        response = json.dumps({
            "error": {"code": "pane_not_found", "message": "pane pane-1 not found"},
            "id": "cli:pane:get",
        })
        self.assertTrue(herdr_pane_is_definitively_absent(1, response, pane_id="pane-1"))
        self.assertFalse(herdr_pane_is_definitively_absent(
            1, '{"error":{"code":"daemon_offline"}}', pane_id="pane-1",
        ))
        self.assertFalse(herdr_pane_is_definitively_absent(
            1, response.replace("pane_not_found", "unknown"), pane_id="pane-1",
        ))
        self.assertFalse(herdr_pane_is_definitively_absent(0, response, pane_id="pane-1"))
        self.assertFalse(herdr_pane_is_definitively_absent(1, "not JSON", pane_id="pane-1"))
        self.assertFalse(herdr_pane_is_definitively_absent(
            1, response, pane_id="other-pane",
        ))

    def test_pane_not_found_is_accepted_from_either_single_exact_output_stream(self) -> None:
        response = json.dumps({
            "error": {"code": "pane_not_found", "message": "pane pane-1 not found"},
            "id": "cli:pane:get",
        })
        self.assertTrue(herdr_pane_is_definitively_absent(1, response, pane_id="pane-1"))
        self.assertTrue(herdr_pane_is_definitively_absent(1, "", response, pane_id="pane-1"))

    def test_ambiguous_or_conflicting_herdr_streams_do_not_prove_absence(self) -> None:
        absent = json.dumps({
            "error": {"code": "pane_not_found", "message": "pane pane-1 not found"},
            "id": "cli:pane:get",
        })
        success = json.dumps({"result": {"pane_id": "pane-1"}, "id": "cli:pane:get"})
        for stdout, stderr in (
            (success, absent),
            (absent, success),
            (absent, absent),
            (absent, absent + "\nwarning"),
            ("prefix " + absent, ""),
            ("", absent + "\nextra output"),
        ):
            with self.subTest(stdout=stdout[:20], stderr=stderr[:20]):
                self.assertFalse(herdr_pane_is_definitively_absent(
                    1, stdout, stderr, pane_id="pane-1",
                ))

    def test_ambiguous_timeout_is_operator_action_not_a_failed_or_retryable_launch(self) -> None:
        decision = reduce_incomplete_launch(
            "claimed_no_session",
            {
                "status": "launching",
                "launch_outcome": "ambiguous",
                "launch_observation": {"state": "uncommitted", "pane_id": "pane-1"},
            },
            task_id="task-1",
        )

        self.assertEqual(decision.action, "require_operator")
        self.assertIn("USER_ACTION_REQUIRED:", decision.reason)
        self.assertIn("ambiguous", decision.reason)
        self.assertIn("pane-1", decision.reason)
        self.assertIn("do not relaunch", decision.reason)
        self.assertFalse(decision.provider_terminal)

    def test_interrupted_launch_reservation_stops_in_both_claimed_and_bound_states(self) -> None:
        for state in ("claimed_no_session", "running", "identity_pending"):
            with self.subTest(state=state):
                decision = reduce_incomplete_launch(
                    state, {"status": "launching", "binding": None}, task_id="task-1",
                )
                self.assertEqual(decision.action, "require_operator")
                self.assertIn("incomplete Herdr launching reservation", decision.reason)

    def test_committed_session_and_pane_identities_remain_safely_reattachable(self) -> None:
        session = reduce_incomplete_launch(
            "claimed_no_session",
            {"status": "launched", "binding": {"session_id": "session-1"}},
            task_id="task-1",
        )
        pane = reduce_incomplete_launch(
            "claimed_no_session",
            {
                "status": "identity_pending", "pane_id": "pane-1",
                "identity_pending_since": "2026-10-05T12:00:00+00:00",
            },
            task_id="task-1",
        )

        self.assertEqual((session.action, session.session_id), ("reattach_running", "session-1"))
        self.assertEqual(pane.action, "reattach_identity_pending")

    def test_uncommitted_observation_never_counts_as_a_binding(self) -> None:
        decision = reduce_incomplete_launch(
            "claimed_no_session",
            {
                "status": "launching",
                "launch_outcome": "ambiguous",
                "launch_observation": {"state": "uncommitted", "session_id": "session-1"},
                "binding": None,
            },
            task_id="task-1",
        )

        self.assertEqual(decision.action, "require_operator")
        self.assertEqual(decision.session_id, "")

    def test_valid_running_lifecycle_continues_through_existing_result_checks(self) -> None:
        decision = reduce_incomplete_launch(
            "running",
            {"status": "launched", "binding": {"session_id": "session-1"}},
            task_id="task-1",
        )

        self.assertEqual((decision.action, decision.session_id), ("continue", "session-1"))


if __name__ == "__main__":
    unittest.main()
