from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


BOUNDARY = r'''#!__PYTHON__
import json
import os
from pathlib import Path
import sys
import time

path = Path(os.environ["CI_FIXTURE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
state["calls"].append([tool, *args])

def finish(value=None, code=0, error=""):
    path.write_text(json.dumps(state))
    if value is not None:
        print(json.dumps(value))
    if error:
        print(error, file=sys.stderr)
    sys.exit(code)

def option(name):
    return args[args.index(name) + 1]

if tool == "gh":
    if args[:2] == ["pr", "view"]:
        state["head_reads"] = state.get("head_reads", 0) + 1
        head = state["head"]
        if state.get("drift") and state["head_reads"] > 1:
            head = "b" * 40
        finish({"number": state.get("pr_number", 42),
                "url": state.get("pr_url", "https://github.com/example/project/pull/42"),
                "headRefOid": head}, state.get("head_exit", 0))
    if args[:2] == ["pr", "checks"]:
        finish(state.get("legacy_checks", [
            {"name": n["name"], "bucket": ("pending" if n["status"] != "COMPLETED" else
             "fail" if n["conclusion"] == "FAILURE" else "pass"),
             "state": n["conclusion"], "link": n["detailsUrl"], "workflow": "CI"}
            if isinstance(n, dict) and "name" in n else n
            for n in state["nodes"]]), state.get("checks_exit", 0))
    if args[:2] == ["api", "graphql"]:
        if state.get("malformed"):
            path.write_text(json.dumps(state))
            print("{invalid")
            sys.exit(0)
        data = {"data": {"repository": {"nameWithOwner": "example/project", "object": {
            "oid": state.get("evidence_head", state["head"]),
            "statusCheckRollup": {"contexts": {
                "nodes": state["nodes"], "totalCount": len(state["nodes"]),
                "pageInfo": {"hasNextPage": state.get("truncated", False), "endCursor": None}
            }}
        }}}}
        if state.get("null_rollup"):
            data["data"]["repository"]["object"]["statusCheckRollup"] = None
        if state.get("graphql_errors"):
            data["errors"] = [{"message": "partial data"}]
        finish(data, state.get("checks_exit", 0), "query failed" if state.get("checks_exit") else "")
    finish(code=90, error="unexpected gh command")

if args[0] == "show":
    key = args[1]
    if state.get("close_attempts") and key == "gate-ci":
        state["readbacks"] = state.get("readbacks", 0) + 1
        if state.get("readback_unavailable"):
            finish(code=1, error="storage unavailable")
    if key not in state["issues"]:
        finish([], code=1, error="issue not found")
    issue = json.loads(json.dumps(state["issues"][key]))
    if key == "gate-ci":
        issue.update(state.get("gate_override", {}))
        if state.get("close_attempts"):
            for field, value in state.get("readback_override", {}).items():
                if field in ("id", "parent", "status"):
                    issue[field] = value
                else:
                    issue["metadata"]["agentflow"]["ci"]["identity"][field] = value
    elif state.get("triage_read_unavailable"):
        finish(code=1, error="triage storage unavailable")
    else:
        issue.update(state.get("triage_override", {}))
    finish([issue])
if args[0] == "update":
    state["updates"] = state.get("updates", 0) + 1
    fail = state.get("update_error_at") == state["updates"]
    if not fail or state.get("update_commits"):
        issue = state["issues"][args[1]]
        if "--metadata" in args:
            issue["metadata"] = json.loads(option("--metadata"))
        if "--status" in args:
            issue["status"] = option("--status")
        if "--assignee" in args:
            issue["assignee"] = option("--assignee")
    finish(code=1 if fail else 0, error="metadata unavailable" if fail else "")
if args[0] == "comment":
    finish(code=1 if state.get("comment_error") else 0, error="comment unavailable")
if args[0] == "close":
    state["close_attempts"] = state.get("close_attempts", 0) + 1
    if state.get("close_mode", "success") != "open-error":
        state["issues"][args[1]]["status"] = "closed"
    if state.get("close_mode") == "timeout":
        path.write_text(json.dumps(state))
        time.sleep(35)
    finish(code=0 if state.get("close_mode", "success") == "success" else 1,
           error="close timed out after commit")
if args[0] == "create":
    state["creates"] = state.get("creates", 0) + 1
    if "--id" in args and "--parent" in args:
        finish(code=1, error="cannot specify both --id and --parent flags")
    key = option("--id") if "--id" in args else "triage-" + str(state["creates"])
    if key in state["issues"]:
        finish(code=1, error="issue already exists")
    dependencies = option("--deps").split(",") if "--deps" in args else []
    parent = option("--parent") if "--parent" in args else next(
        (dep.removeprefix("parent-child:") for dep in dependencies if dep.startswith("parent-child:")), "")
    issue = {"id": key, "parent": parent, "title": args[1], "status": "open",
             "metadata": json.loads(option("--metadata"))}
    if state.get("create_mode") != "open-error":
        state["issues"][key] = issue
    state["create_echo"] = {field: value for field, value in issue.items() if field != "parent"}
    finish(state["create_echo"], 0 if state.get("create_mode", "success") == "success" else 1,
           error="create acknowledgement unavailable")
finish(code=90, error="unexpected bd command")
'''


def check(*, name="unit", conclusion="SUCCESS", required=True, check_id="run-1"):
    return {
        "__typename": "CheckRun", "id": check_id, "name": name,
        "status": "COMPLETED", "conclusion": conclusion, "isRequired": required,
        "detailsUrl": f"https://github.com/example/project/actions/runs/{check_id}",
    }


class CICompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".agentflow" / ("ci-fixture-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.state_path = self.directory / "state.json"
        self.state = {
            "calls": [], "head": HEAD, "nodes": [check()],
            "issues": {"gate-ci": {
                "id": "gate-ci", "title": "CI gate", "parent": "gate-root",
                "status": "open", "labels": ["af:stage:ci"],
                "metadata": {"unrelated": "preserved", "agentflow": {}},
            }},
        }
        for name in ("bd", "gh"):
            executable = self.directory / name
            executable.write_text(BOUNDARY.replace("__PYTHON__", sys.executable))
            executable.chmod(0o755)

    def invoke(self, *extra, process_timeout=15, once=True):
        self.state_path.write_text(json.dumps(self.state))
        environment = {
            **os.environ,
            "PATH": str(self.directory) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "CI_FIXTURE": str(self.state_path),
        }
        for key in ("BEADS_DIR", "BEADS_DB", "BEADS_DATABASE"):
            environment.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-m", "agentflow.cli", "ci", "watch",
             "--bead", "gate-ci", "--pr", "42", "--repo", "Example/Project",
             *(["--once"] if once else []),
             "--timeout-seconds", "0", "--cwd", str(self.directory), *extra],
            cwd=self.directory, env=environment, capture_output=True, text=True, timeout=process_timeout,
        )
        self.state = json.loads(self.state_path.read_text())
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result

    def record(self, result):
        records = [line.removeprefix("CI_STATE ") for line in result.stdout.splitlines()
                   if line.startswith("CI_STATE ")]
        self.assertTrue(records, result.stdout + result.stderr)
        return json.loads(records[-1])

    def assert_axes(self, result, code, status, disposition, obligation):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        record = self.record(result)
        self.assertEqual(
            (record["status"], record["disposition"], record["obligation"]),
            (status, disposition, obligation),
        )
        return record

    def assert_no_reruns(self):
        for call in self.state["calls"]:
            if call[0] == "gh":
                self.assertIn(call[1:3], [["pr", "view"], ["api", "graphql"]])
            if call[0] == "bd" and call[1] in ("close", "update", "comment"):
                self.assertEqual(call[2], "gate-ci")

    def test_committed_close_timeout_reconciles_exact_receipt(self):
        self.state["close_mode"] = "committed-error"
        result = self.invoke()
        record = self.assert_axes(result, 0, "passed", "closed", "accepted")
        self.assertEqual(self.state["close_attempts"], 1)
        self.assertEqual(self.state["readbacks"], 1)
        self.assertEqual(record["identity"]["gate"], "gate-ci")
        self.assertEqual(record["identity"]["parent"], "gate-root")
        self.assertEqual(record["identity"]["repo"], "example/project")
        self.assertEqual(record["identity"]["pr"], 42)
        self.assertEqual(record["identity"]["head"], HEAD)
        self.assertEqual(record["checks"][0]["id"], "run-1")
        query = next(c for c in self.state["calls"] if c[:3] == ["gh", "api", "graphql"])
        self.assertTrue(any(HEAD in argument for argument in query))
        persisted = self.state["issues"]["gate-ci"]["metadata"]
        self.assertEqual(persisted["agentflow"]["ci"]["disposition"], "closed")
        self.assertEqual(persisted["unrelated"], "preserved")
        self.assert_no_reruns()

    def test_open_close_timeout_keeps_passed_observation_required(self):
        self.state["close_mode"] = "open-error"
        self.assert_axes(self.invoke(), 2, "passed", "open", "required")
        self.assertEqual(self.state["close_attempts"], 1)
        self.assertEqual(self.state["readbacks"], 1)
        self.assertEqual(self.state.get("creates", 0), 0)
        self.assert_no_reruns()

    def test_committed_process_timeout_uses_one_readback(self):
        self.state["close_mode"] = "timeout"
        started = time.monotonic()
        self.assert_axes(self.invoke(process_timeout=45), 0, "passed", "closed", "accepted")
        self.assertGreaterEqual(time.monotonic() - started, 29)
        self.assertEqual(self.state["close_attempts"], 1)
        self.assertEqual(self.state["readbacks"], 1)
        self.assert_no_reruns()

    def test_unreadable_close_keeps_passed_observation_blocked(self):
        self.state.update(close_mode="committed-error", readback_unavailable=True)
        self.assert_axes(self.invoke(), 2, "passed", "closure_unresolved", "blocked")
        self.assertEqual(self.state["readbacks"], 1)
        self.assertEqual(self.state["close_attempts"], 1)

    def test_mismatched_readback_never_updates_another_identity(self):
        for field, value in [
            ("id", "other-gate"), ("parent", "other-parent"),
            ("repo", "other/project"), ("pr", 99), ("head", OTHER_HEAD),
            ("check_set_sha256", "0" * 64),
        ]:
            with self.subTest(field=field):
                self.state.update(close_mode="committed-error", readback_override={field: value})
                self.state["issues"]["gate-ci"]["status"] = "open"
                self.state["issues"]["gate-ci"]["metadata"]["agentflow"] = {}
                self.state["calls"] = []
                self.state["close_attempts"] = 0
                self.state["readbacks"] = 0
                record = self.assert_axes(self.invoke(), 2, "unavailable", "closure_unresolved", "blocked")
                self.assertEqual(record["last_valid_observation"]["status"], "passed")
                closing = next(i for i, c in enumerate(self.state["calls"]) if c[:2] == ["bd", "close"])
                self.assertEqual([c[1] for c in self.state["calls"][closing + 1:]], ["show"])
                self.assertEqual(self.state["readbacks"], 1)

    def test_already_closed_exact_replay_is_read_only(self):
        self.state["close_mode"] = "committed-error"
        self.assertEqual(self.invoke().returncode, 0)
        self.state["calls"] = []
        self.state["close_attempts"] = 0
        self.assert_axes(self.invoke(), 0, "passed", "closed", "accepted")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))
        self.assert_no_reruns()

    def test_malformed_scope_is_not_an_exact_readback(self):
        self.state.update(close_mode="committed-error", readback_override={"required_only": 0})
        self.assert_axes(self.invoke(), 2, "unavailable", "closure_unresolved", "blocked")
        self.assertEqual(self.state["readbacks"], 1)

    def test_closed_legacy_receipt_is_not_backfilled(self):
        gate = self.state["issues"]["gate-ci"]
        gate["status"] = "closed"
        gate["metadata"]["agentflow"]["ci"] = {"status": "passed", "pr": "42"}
        self.assert_axes(self.invoke(), 2, "unavailable", "closed", "blocked")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))

    def test_closed_changed_check_set_or_head_is_not_accepted(self):
        self.assertEqual(self.invoke().returncode, 0)
        self.state["nodes"] = [check(check_id="new-run")]
        self.state["calls"] = []
        self.assert_axes(self.invoke(), 2, "unavailable", "closed", "blocked")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))
        self.state["head"] = OTHER_HEAD
        self.assertEqual(self.invoke().returncode, 2)

    def test_pre_close_errors_do_not_attempt_closure(self):
        for failure in ({"update_error_at": 1}, {"comment_error": True}):
            with self.subTest(failure=failure):
                self.state.update(failure)
                self.state["updates"] = 0
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.record(result)["status"], "passed")
                self.assertEqual(self.state.get("close_attempts", 0), 0)
                self.state.pop("update_error_at", None)

    def test_no_checks_and_pending_are_not_success(self):
        for nodes in ([], [check(conclusion=None)]):
            with self.subTest(nodes=nodes):
                if nodes:
                    nodes[0]["status"] = "IN_PROGRESS"
                self.state["nodes"] = nodes
                self.assert_axes(self.invoke(), 2, "pending", "open", "required")
                self.assertEqual(self.state.get("close_attempts", 0), 0)

    def test_once_and_expired_deadline_observe_only_once(self):
        for once in (True, False):
            with self.subTest(once=once):
                self.state["nodes"] = [dict(check(), status="IN_PROGRESS", conclusion=None)]
                self.state["calls"] = []
                self.assert_axes(self.invoke(once=once), 2, "pending", "open", "required")
                self.assertEqual(sum(c[:3] == ["gh", "api", "graphql"] for c in self.state["calls"]), 1)
                self.assertEqual(sum(c[:3] == ["gh", "pr", "view"] for c in self.state["calls"]), 2)
                self.assert_no_reruns()

    def test_commit_status_contexts_are_classified(self):
        initial = json.loads(json.dumps(self.state))
        for state, code, status in (
            ("SUCCESS", 0, "passed"), ("ERROR", 1, "failed"),
            ("FAILURE", 1, "failed"), ("PENDING", 2, "pending"),
        ):
            with self.subTest(state=state):
                self.state = json.loads(json.dumps(initial))
                self.state["nodes"] = [{
                    "__typename": "StatusContext", "id": "status-1", "context": "external",
                    "state": state, "targetUrl": None, "isRequired": True,
                }]
                self.assert_axes(self.invoke(), code, status,
                                 "closed" if code == 0 else "open",
                                 "accepted" if code == 0 else "required")
                self.assert_no_reruns()

    def test_required_only_ignores_optional_failures_but_not_empty_set(self):
        self.state["nodes"] = [check(), check(name="optional", conclusion="FAILURE", required=False, check_id="run-2")]
        record = self.assert_axes(self.invoke("--required"), 0, "passed", "closed", "accepted")
        self.assertTrue(record["identity"]["required_only"])
        self.assertEqual(len(record["checks"]), 1)
        self.state["issues"]["gate-ci"]["status"] = "open"
        self.state["nodes"] = [check(required=False)]
        self.assert_axes(self.invoke("--required"), 2, "pending", "open", "required")

    def test_bad_external_data_cannot_become_success(self):
        cases = [
            {"malformed": True}, {"graphql_errors": True}, {"checks_exit": 1},
            {"nodes": [None]}, {"nodes": [{}]},
            {"nodes": [check(conclusion="UNKNOWN")]},
            {"nodes": [dict(check(), isRequired="true")]},
            {"truncated": True}, {"evidence_head": OTHER_HEAD},
            {"head_exit": 1}, {"pr_number": 9},
            {"pr_url": "https://github.com/other/project/pull/42"},
        ]
        for case in cases:
            with self.subTest(case=case):
                original = json.loads(json.dumps(self.state))
                self.state.update(case)
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.record(result)["status"], "unavailable")
                self.assertEqual(self.state.get("close_attempts", 0), 0)
                self.assertEqual(self.state.get("creates", 0), 0)
                self.state = original

    def test_head_drift_does_not_mutate_gate(self):
        self.state["drift"] = True
        self.assert_axes(self.invoke(), 2, "unavailable", "open", "blocked")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))

    def test_wrong_gate_is_not_mutated(self):
        self.state["gate_override"] = {"id": "other-gate"}
        self.assert_axes(self.invoke(), 2, "unavailable", "closure_unresolved", "blocked")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))

    def test_malformed_gate_status_is_unavailable(self):
        self.state["gate_override"] = {"status": []}
        self.assert_axes(self.invoke(), 2, "unavailable", "open", "blocked")
        self.assertEqual(self.state.get("close_attempts", 0), 0)

    def test_malformed_readback_status_is_unavailable(self):
        self.state.update(close_mode="committed-error", readback_override={"status": []})
        self.assert_axes(self.invoke(), 2, "unavailable", "closure_unresolved", "blocked")
        self.assertEqual(self.state["readbacks"], 1)

    def test_failure_reuses_closed_triage_across_invocations(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        first = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        triage = first["triage_bead"]
        self.state["issues"][triage]["status"] = "closed"
        self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        self.assertEqual(self.state["creates"], 1)
        self.assertEqual(self.state["issues"][triage]["status"], "closed")
        self.assertEqual(self.state.get("close_attempts", 0), 0)
        self.assert_no_reruns()
        self.state["head"] = OTHER_HEAD
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 2)
        self.state["head"] = HEAD
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 2)
        self.state["nodes"] = [check(conclusion="FAILURE", check_id="new-run")]
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 3)

    def test_explicit_creation_contract_routes_failure_once(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        first = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        create = next(call for call in self.state["calls"] if call[:2] == ["bd", "create"])
        self.assertNotIn("--parent", create)
        self.assertEqual(create[create.index("--id") + 1], first["triage_bead"])
        self.assertEqual(
            create[create.index("--deps") + 1].split(","),
            ["parent-child:gate-root", "blocks:gate-ci"],
        )
        triage = self.state["issues"][first["triage_bead"]]
        self.assertEqual(triage["parent"], "gate-root")
        triage["status"] = "closed"
        second = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        self.assertEqual(second["triage_bead"], first["triage_bead"])
        self.assertEqual(self.state["creates"], 1)
        self.assert_no_reruns()

    def test_create_echo_requires_persisted_verification(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        record = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        triage_id = record["triage_bead"]
        self.assertNotIn("parent", self.state["create_echo"])
        self.assertEqual(self.state["issues"][triage_id]["parent"], "gate-root")
        self.assertEqual(
            [call for call in self.state["calls"] if call[:3] == ["bd", "show", triage_id]],
            [["bd", "show", triage_id, "--json"]],
        )
        self.state["issues"][triage_id]["status"] = "closed"
        replay = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        self.assertEqual(replay["triage_bead"], triage_id)
        self.assertEqual(self.state["creates"], 1)
        self.assert_no_reruns()

    def test_create_echo_cannot_replace_unverifiable_persisted_record(self):
        initial = json.loads(json.dumps(self.state))
        for failure in ({"triage_override": {"parent": ""}}, {"triage_read_unavailable": True}):
            with self.subTest(failure=failure):
                self.state = json.loads(json.dumps(initial))
                self.state.update(nodes=[check(conclusion="FAILURE")], **failure)
                record = self.assert_axes(self.invoke(), 2, "failed", "open", "blocked")
                triage_id = record["triage_bead"]
                self.assertEqual(
                    [call for call in self.state["calls"] if call[:3] == ["bd", "show", triage_id]],
                    [["bd", "show", triage_id, "--json"]],
                )
                self.assertEqual(self.state["creates"], 1)
                self.assert_no_reruns()

    def test_explicit_creation_contract_preserves_dependencies(self):
        from agentflow import beads

        dependencies = ["blocks:gate-ci", "depends-on:gate-other"]
        response = subprocess.CompletedProcess([], 0, '{"id":"gate-triage"}', "")
        with mock.patch.object(beads, "run", return_value=response) as run:
            beads.create_issue(
                self.directory, title="Triage", description="Diagnose checks", acceptance="Failure explained",
                parent="gate-root", labels=["af:stage:ci-triage"], assignee="triager",
                dependencies=dependencies, issue_id="gate-triage",
            )
        run.assert_called_once()
        arguments = run.call_args.args[1:]
        self.assertNotIn("--parent", arguments)
        self.assertEqual(arguments[arguments.index("--id") + 1], "gate-triage")
        self.assertEqual(
            arguments[arguments.index("--deps") + 1].split(","),
            ["parent-child:gate-root", "blocks:gate-ci", "depends-on:gate-other"],
        )
        self.assertEqual(dependencies, ["blocks:gate-ci", "depends-on:gate-other"])

    def test_normal_creation_contract_retains_parent_flag(self):
        from agentflow import beads

        response = subprocess.CompletedProcess([], 0, '{"id":"gate-triage"}', "")
        with mock.patch.object(beads, "run", return_value=response) as run:
            beads.create_issue(
                self.directory, title="Triage", description="Diagnose checks", acceptance="Failure explained",
                parent="gate-root", labels=["af:stage:ci-triage"], assignee="triager",
                dependencies=["blocks:gate-ci"],
            )
        run.assert_called_once_with(
            self.directory, "create", "Triage", "--description", "Diagnose checks",
            "--acceptance", "Failure explained", "--parent", "gate-root",
            "--no-inherit-labels", "--labels", "af:stage:ci-triage", "--json",
            "--assignee", "triager", "--deps", "blocks:gate-ci",
        )

    def test_two_failed_invocations_do_not_create_again(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        self.assertEqual(self.invoke().returncode, 1)
        for key, issue in self.state["issues"].items():
            if key != "gate-ci":
                issue["status"] = "closed"
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 1)

    def test_unverifiable_existing_triage_is_not_duplicated(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        self.assertEqual(self.invoke().returncode, 1)
        self.state["triage_override"] = {"status": []}
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.state["creates"], 1)

    def test_failed_open_gate_releases_its_assignee(self):
        self.state["nodes"] = [check(conclusion="FAILURE")]
        self.state["issues"]["gate-ci"]["assignee"] = "ci-observer"
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["issues"]["gate-ci"]["assignee"], "")

    def test_same_failure_reuses_triage_when_other_checks_advance(self):
        self.state["nodes"] = [
            check(conclusion="FAILURE"),
            dict(check(name="optional", required=False, check_id="optional"), status="IN_PROGRESS", conclusion=None),
        ]
        self.assertEqual(self.invoke().returncode, 1)
        self.state["nodes"][1].update(status="COMPLETED", conclusion="SUCCESS")
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 1)
        self.assertEqual(self.invoke("--required").returncode, 1)
        self.assertEqual(self.state["creates"], 1)

    def test_invalid_rows_and_failed_queries_never_close(self):
        for case in ({"nodes": [None]}, {"checks_exit": 1}):
            with self.subTest(case=case):
                original = json.loads(json.dumps(self.state))
                self.state.update(case)
                self.assertEqual(self.invoke().returncode, 2)
                self.assertEqual(self.state.get("close_attempts", 0), 0)
                self.state = original

    def test_committed_create_error_reuses_exact_triage(self):
        self.state.update(nodes=[check(conclusion="FAILURE")], create_mode="committed-error")
        first = self.assert_axes(self.invoke(), 1, "failed", "open", "required")
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 1)
        triage = self.state["issues"][first["triage_bead"]]
        self.assertEqual(triage["parent"], "gate-root")
        self.assertEqual(triage["metadata"]["agentflow"]["ci_identity"], first["identity"])

    def test_unavailable_create_ack_never_duplicates_triage(self):
        self.state.update(nodes=[check(conclusion="FAILURE")], create_mode="committed-error",
                          triage_read_unavailable=True)
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.state["creates"], 1)
        self.state["triage_read_unavailable"] = False
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(self.state["creates"], 1)

    def test_uncommitted_create_and_metadata_errors_fail_closed(self):
        self.state.update(nodes=[check(conclusion="FAILURE")], create_mode="open-error")
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.state["creates"], 1)

    def test_ambiguous_reservation_write_never_duplicates_creation(self):
        self.state.update(nodes=[check(conclusion="FAILURE")], update_error_at=2,
                          update_commits=True)
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.state.get("creates", 0), 0)
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(self.state.get("creates", 0), 0)

    def test_failed_disposition_refresh_does_not_retry_a_verified_close(self):
        self.state.update(close_mode="committed-error", update_error_at=2)
        result = self.invoke()
        self.assert_axes(result, 0, "passed", "closed", "accepted")
        self.assertIn("metadata refresh unavailable", result.stderr)
        self.assertEqual(self.state["close_attempts"], 1)
        self.state["calls"] = []
        self.assert_axes(self.invoke(), 0, "passed", "closed", "accepted")
        self.assertFalse(any(c[0] == "bd" and c[1] != "show" for c in self.state["calls"]))


if __name__ == "__main__":
    unittest.main()
