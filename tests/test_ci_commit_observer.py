from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


GH_BOUNDARY = r'''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["GH_FIXTURE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
path.write_text(json.dumps(state))
if state.get("error"):
    print(state["error"], file=sys.stderr)
    sys.exit(1)
if state.get("invalid_json"):
    print("{invalid")
    sys.exit(0)
print(json.dumps(state.get("pages", [])))
'''

BD_BOUNDARY = r'''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["GH_FIXTURE"])
state = json.loads(path.read_text())
state.setdefault("bd_calls", []).append(sys.argv[1:])
path.write_text(json.dumps(state))
sys.exit(90)
'''


def run(workflow_id: int, *, status="completed", conclusion="success",
        head=HEAD, run_id=1):
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "name": f"Workflow {workflow_id}",
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
    }


def page(runs, *, total_count=None):
    return {
        "total_count": len(runs) if total_count is None else total_count,
        "workflow_runs": runs,
    }


class CICommitObserverTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="agentflow-ci-commit-"))
        self.addCleanup(shutil.rmtree, self.directory)
        self.state_path = self.directory / "fixture.json"
        gh = self.directory / "gh"
        gh.write_text(GH_BOUNDARY.replace("__PYTHON__", sys.executable))
        gh.chmod(0o755)
        bd = self.directory / "bd"
        bd.write_text(BD_BOUNDARY.replace("__PYTHON__", sys.executable))
        bd.chmod(0o755)
        self.state = {"pages": [page([])]}

    def invoke(self, *extra):
        self.state["calls"] = []
        self.state.pop("bd_calls", None)
        self.state_path.write_text(json.dumps(self.state))
        environment = {
            **os.environ,
            "PATH": str(self.directory) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "GH_FIXTURE": str(self.state_path),
        }
        result = subprocess.run(
            [
                sys.executable, "-m", "agentflow.cli", "ci", "watch-commit",
                "--repo", "Example/Project", "--sha", HEAD,
                "--workflow-id", "101", "--workflow-id", "202",
                "--cwd", str(self.directory), *extra,
            ],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.state = json.loads(self.state_path.read_text())
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertEqual(self.state.get("bd_calls", []), [])
        self.assertEqual(len(self.state["calls"]), 1)
        self.assertEqual(
            self.state["calls"][0][:5],
            ["api", "--paginate", "--slurp", "--method", "GET"],
        )
        self.assertFalse(any(
            "dispatch" in arg or "rerun" in arg
            for arg in self.state["calls"][0]
        ))
        return result

    def report(self, result):
        records = [
            line.removeprefix("CI_COMMIT_STATE ")
            for line in result.stdout.splitlines()
            if line.startswith("CI_COMMIT_STATE ")
        ]
        self.assertEqual(len(records), 1, result.stdout)
        return json.loads(records[0])

    def assert_read_only_query(self):
        self.assertEqual(len(self.state["calls"]), 1)
        call = self.state["calls"][0]
        self.assertEqual(call[:5], [
            "api", "--paginate", "--slurp", "--method", "GET",
        ])
        self.assertIn("repos/example/project/actions/runs", call)
        self.assertIn(f"head_sha={HEAD}", call)
        self.assertNotIn("POST", call)
        self.assertFalse(any("dispatch" in arg or "rerun" in arg for arg in call))
        self.assertEqual(self.state.get("bd_calls", []), [])

    def test_pass_requires_one_successful_run_for_each_expected_workflow(self):
        self.state["pages"] = [
            page([run(101, run_id=1)], total_count=2),
            page([run(202, run_id=2)], total_count=2),
        ]

        result = self.invoke("--once", "--timeout-seconds", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report(result)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["observed_result"], "passed")
        self.assertEqual(report["disposition"], "observed")
        self.assertEqual(report["obligation"], "record_result")
        self.assertFalse(report["rerun_authorized"])
        self.assertEqual(report["repo"], "example/project")
        self.assertEqual(report["head"], HEAD)
        self.assertEqual(report["expected_workflow_ids"], [101, 202])
        self.assertEqual([item["status"] for item in report["workflows"]], ["passed", "passed"])
        self.assertEqual(report["observed_run_count"], 2)
        self.assert_read_only_query()

    def test_missing_and_pending_are_distinct(self):
        missing = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(self.report(missing)["status"], "missing")

        self.state["pages"] = [page([
            run(101, status="queued", conclusion=None),
            run(202, run_id=2),
        ])]
        pending = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(pending.returncode, 2)
        pending_report = self.report(pending)
        self.assertEqual(pending_report["status"], "pending")
        self.assertEqual(
            [item["status"] for item in pending_report["workflows"]],
            ["pending", "passed"],
        )

    def test_failed_or_cancelled_run_is_never_passed(self):
        for conclusion in ("failure", "cancelled"):
            self.state["pages"] = [page([
                run(101, conclusion=conclusion),
                run(202, run_id=2),
            ])]

            result = self.invoke("--once", "--timeout-seconds", "0")

            self.assertEqual(result.returncode, 1)
            report = self.report(result)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["workflows"][0]["status"], "failed")

    def test_wrong_sha_duplicate_runs_and_incomplete_pages_fail_closed(self):
        self.state["pages"] = [page([
            run(101, head=OTHER_HEAD), run(202, run_id=2),
        ])]
        drift = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(drift.returncode, 2)
        self.assertEqual(self.report(drift)["status"], "drift")

        self.state["pages"] = [page([
            run(101), run(101, run_id=3), run(202, run_id=2),
        ])]
        duplicate = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(self.report(duplicate)["status"], "duplicate")

        self.state["pages"] = [page([run(101)], total_count=2)]
        incomplete = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(incomplete.returncode, 2)
        self.assertEqual(self.report(incomplete)["status"], "unavailable")

        self.state["pages"] = [page([
            run(101), run(202, run_id=2), run(303, head=OTHER_HEAD, run_id=3),
        ])]
        unrelated_drift = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(unrelated_drift.returncode, 2)
        self.assertEqual(self.report(unrelated_drift)["status"], "drift")

        malformed_run = run(101)
        malformed_run["status"] = "unexpected"
        self.state["pages"] = [page([malformed_run, run(202, run_id=2)])]
        malformed = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(self.report(malformed)["status"], "unavailable")

    def test_malformed_response_and_deadline_are_reported(self):
        self.state["invalid_json"] = True
        malformed = self.invoke("--once", "--timeout-seconds", "0")
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(self.report(malformed)["status"], "unavailable")

        self.state = {"pages": [page([])]}
        deadline = self.invoke("--timeout-seconds", "0")
        self.assertEqual(deadline.returncode, 2)
        deadline_report = self.report(deadline)
        self.assertEqual(deadline_report["status"], "missing")
        self.assertEqual(deadline_report["observed_result"], "missing")
        self.assertEqual(deadline_report["disposition"], "deadline")
        self.assertEqual(deadline_report["last_status"], "missing")


if __name__ == "__main__":
    unittest.main()
