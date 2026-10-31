from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow import cli  # noqa: E402
from agentflow import reconciliation  # noqa: E402
from agentflow.provider_capabilities import (  # noqa: E402
    CapabilityState,
    ProviderCapability,
    capability_for,
    provider_capabilities,
)


WORKER = "native-worker"
OWNER = "project-owner"
BASE = "1" * 40
HEAD = "2" * 40
NEW = "wf-root.1"
OLD = "wf-root.2"
INELIGIBLE = "wf-root.3"
UNCLAIMED = "wf-root.4"
DONE = "wf-root.5"
OUTSIDE = "other-1"
VISIBILITY_SCHEMA = "agentflow.native-visibility@1"
OBSERVATION_SCHEMA = "agentflow.native-observation@1"
AUTHORITY_SCHEMA = "agentflow.native-return-authority@1"
RESULT_SCHEMA = "agentflow.native-return@1"
RECEIPT_SCHEMA = "agentflow.native-return-receipt@1"
ATTESTATION = (
    "controller-validated native return; not managed causal-result authentication "
    "or resolved-model attestation"
)
WRITE_COMMANDS = {"update", "close", "create", "comment", "claim", "reopen", "delete", "heartbeat"}


FAKE = r'''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["NATIVE_FIXTURE"])
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

if tool != "bd":
    finish(code=90, error="unexpected external tool")
issues = state["issues"]
if args[:1] == ["list"]:
    if state.get("list_mode") == "fail":
        finish(code=1, error="list failed")
    finish([json.loads(json.dumps(issue)) for issue in issues.values()])
if args[:1] == ["show"]:
    key = args[1]
    if key not in issues:
        finish(code=1, error="issue not found")
    finish(json.loads(json.dumps([issues[key]])))
if args[:1] == ["history"]:
    key = args[1]
    if key in state.get("history_fail", []):
        finish(code=1, error="history unavailable")
    rows = json.loads(json.dumps(state.get("history", {}).get(key, [])))
    reads = state.setdefault("history_reads", {})
    reads[key] = reads.get(key, 0) + 1
    if reads[key] == state.get("claim_change_after", {}).get(key):
        state.setdefault("history", {}).setdefault(key, []).append({
            "id": "ev-new-raced", "issue_id": key, "event_type": "claimed", "actor": "another-worker",
        })
    finish(rows)
if args[:1] == ["update"]:
    key = args[1]
    if "--metadata" in args:
        issues[key]["metadata"] = json.loads(option("--metadata"))
    if "--status" in args:
        issues[key]["status"] = option("--status")
    if "--assignee" in args:
        issues[key]["assignee"] = option("--assignee")
    finish()
if args[:1] == ["close"]:
    key = args[1]
    if "--force" in args:
        finish(code=1, error="force is not allowed in this fixture")
    actor = option("--actor") if "--actor" in args else os.environ.get("BD_ACTOR", "")
    holder = issues[key].get("assignee") or ""
    if holder and actor != holder:
        finish(code=1, error="cannot close: actor is not the claim holder")
    if state.get("close_mode", {}).get(key) == "open-error":
        finish(code=1, error="close ambiguous")
    issues[key]["status"] = "closed"
    finish()
finish(code=90, error="unexpected bd command")
'''


def sha256_json(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def generation(*claims):
    return hashlib.sha256(
        json.dumps(sorted(list(item) for item in claims), separators=(",", ":")).encode()
    ).hexdigest()


GEN_NEW = generation(("ev-new-2", WORKER))
GEN_OLD = generation(("ev-old-1", "old-worker"))


def matrix(task_id, status="passed"):
    rows = []
    for row_id in ("AC-1", "AC-2"):
        row = {
            "id": row_id, "outcome": f"Outcome {row_id}", "owner": "controller",
            "lane": "static", "planned_evidence": "unit tests", "status": status,
        }
        if status == "passed":
            row["actual_evidence"] = f"controller reran the unit tests for {row_id}"
        rows.append(row)
    return {"version": 1, "task_id": task_id, "rows": rows}


def issue(identifier, status, parent="", *, assignee="", labels=(), agentflow=None, issue_type="task"):
    value = {
        "id": identifier, "title": identifier, "status": status, "issue_type": issue_type,
        "assignee": assignee, "labels": list(labels), "dependencies": [],
        "metadata": {"unrelated": "kept", "agentflow": agentflow or {}},
    }
    if parent:
        value["parent"] = parent
    return value


def graph():
    rows = [
        issue("wf-root", "open", issue_type="epic"),
        issue(NEW, "in_progress", "wf-root", assignee=WORKER, agentflow={"acceptance": matrix(NEW)}),
        issue(OLD, "in_progress", "wf-root", assignee="old-worker", agentflow={
            "acceptance": matrix(OLD), "launch": {"provider": "codex", "lease_epoch": 1},
        }),
        issue(
            INELIGIBLE, "in_progress", "wf-root", assignee=WORKER,
            labels=["af:requires:credential-use"], agentflow={"acceptance": matrix(INELIGIBLE)},
        ),
        issue(UNCLAIMED, "open", "wf-root", agentflow={"acceptance": matrix(UNCLAIMED)}),
        issue(DONE, "closed", "wf-root", agentflow={"acceptance": matrix(DONE)}),
        issue("other-root", "open", issue_type="epic"),
        issue(OUTSIDE, "in_progress", "other-root", assignee=WORKER,
              agentflow={"acceptance": matrix(OUTSIDE)}),
    ]
    return {row["id"]: row for row in rows}


def history():
    def events(task, *rows):
        return [{"id": event_id, "issue_id": task, "event_type": kind, "actor": actor,
                 "created_at": "2026-10-30T00:00:00Z"} for event_id, kind, actor in rows]
    return {
        NEW: events(NEW, ("ev-new-1", "created", OWNER), ("ev-new-2", "claimed", WORKER)),
        OLD: events(OLD, ("ev-old-1", "claimed", "old-worker")),
        INELIGIBLE: events(INELIGIBLE, ("ev-in-1", "claimed", WORKER)),
        OUTSIDE: events(OUTSIDE, ("ev-out-1", "claimed", WORKER)),
    }


def tree_digest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        if item.is_file() else "<dir>"
        for item in sorted(path.rglob("*"))
    }


class NativeFixture(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".agentflow" / ("native-fixture-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.workspace = self.directory / "repo"
        self.workspace.mkdir()
        self.state_home = self.directory / "state"
        self.state_path = self.directory / "fixture.json"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("bd", "gh", "herdr", "codex", "claude", "copilot", "tmux"):
            executable = self.bin / name
            executable.write_text(FAKE.replace("__PYTHON__", sys.executable))
            executable.chmod(0o755)
        self.state = {"calls": [], "issues": graph(), "history": history()}
        self.all_calls: list[list[str]] = []

    def environment(self):
        environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "NATIVE_FIXTURE": str(self.state_path),
            "AGENTFLOW_STATE_HOME": str(self.state_home),
        }
        for key in ("BEADS_DIR", "BEADS_DB", "BEADS_DATABASE", "BD_ACTOR"):
            environment.pop(key, None)
        return environment

    def run_cli(self, *arguments):
        self.state["calls"] = []
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(
            [sys.executable, "-m", "agentflow.cli", *arguments],
            cwd=self.workspace, env=self.environment(), capture_output=True, text=True,
            timeout=60,
        )
        self.state = json.loads(self.state_path.read_text())
        self.all_calls.extend(self.state["calls"])
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result

    def native(self, command, task, *extra, workflow_root="wf-root", actor="controller-a"):
        arguments = [
            "native", command, "--workflow-root", workflow_root, "--task", task,
            "--cwd", str(self.workspace), "--json", *extra,
        ]
        if actor:
            arguments.extend(["--actor", actor])
        result = self.run_cli(*arguments)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(result.stdout + result.stderr)
        return result, payload

    def approve(self, task=NEW, *, provider="copilot", base=BASE, approval_ref="owner-approval-59",
                approved_by=OWNER, **kwargs):
        return self.native(
            "approve", task, "--provider", provider, "--base", base,
            "--approval-ref", approval_ref, "--approved-by", approved_by, **kwargs,
        )

    def observe(self, task=NEW, *, provider="copilot", session_id="sess-new", state="present",
                observer="controller-a", **kwargs):
        return self.native(
            "observe", task, "--provider", provider, "--session-id", session_id,
            "--state", state, "--observer", observer, **kwargs,
        )

    def result_file(self, task=NEW, **overrides):
        value = {
            "schema": RESULT_SCHEMA, "workflow_root": "wf-root", "task_id": task,
            "provider": "copilot", "session_id": "sess-new", "claim_generation": GEN_NEW,
            "base": BASE, "head": HEAD,
            "acceptance_results": [
                {"acceptance_id": "AC-1", "status": "passed", "evidence": "unit tests passed"},
                {"acceptance_id": "AC-2", "status": "passed", "evidence": "docs check passed"},
            ],
            "verdict": "usable",
        }
        value.update(overrides)
        path = self.directory / f"result-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(value))
        return path, value

    def accept(self, path, task=NEW, **kwargs):
        return self.native("accept", task, "--result", str(path), **kwargs)

    def writes(self):
        return [call for call in self.state["calls"] if call[0] == "bd" and call[1] in WRITE_COMMANDS]

    def agentflow(self, identifier):
        return self.state["issues"][identifier]["metadata"]["agentflow"]

    def write_herdr(self, sessions):
        path = self.workspace / ".agentflow/herdr/sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "agentflow.herdr", "version": 1, "sessions": sessions}))
        return path

    def assert_rejected(self, result, payload, text=""):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["disposition"], "rejected")
        if text:
            self.assertIn(text, payload["error"])
        self.assertEqual(self.writes(), [])

    def assert_no_foreign_tools(self):
        self.assertEqual([call for call in self.all_calls if call[0] != "bd"], [])
        for call in self.all_calls:
            self.assertNotIn("--force", call)
            self.assertNotIn(call[1], {"claim", "reopen", "heartbeat"})

    def prepared(self):
        result, payload = self.approve()
        self.assertEqual(result.returncode, 0, payload)
        result, payload = self.observe()
        self.assertEqual(result.returncode, 0, payload)
        return copy.deepcopy(self.state)


class CapabilityDeclarationTests(unittest.TestCase):
    """V3a: a distinct conditional lane that leaves persistent Herdr unchanged."""

    def test_controller_observed_return_is_conditional_for_every_provider(self):
        for capability in provider_capabilities():
            with self.subTest(provider=capability.provider):
                self.assertEqual(capability.controller_observed_return, CapabilityState.CONDITIONAL)
                self.assertEqual(
                    capability.lane_state("controller-observed-return"), CapabilityState.CONDITIONAL
                )
                self.assertIn("not managed", capability.observed_return_note)
        self.assertEqual(capability_for("copilot").persistent_herdr, CapabilityState.DISABLED)
        self.assertEqual(capability_for("codex").persistent_herdr, CapabilityState.SUPPORTED)
        self.assertEqual(capability_for("claude").persistent_herdr, CapabilityState.CONDITIONAL)


NOW = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)


def observation(*, state="present", seconds_ago=60, claim_generation=GEN_NEW, **overrides):
    value = {
        "schema": OBSERVATION_SCHEMA, "provider": "copilot", "session_id": "sess-1",
        "state": state, "source": "exact-session-query", "observer": "controller-a",
        "observed_at": (NOW - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds"),
        "claim_generation": claim_generation,
    }
    value.update(overrides)
    return value


class NativeVisibilityClassifierTests(unittest.TestCase):
    """V3b: the pure classifier distinguishes the four visibility states."""

    def enumeration(self, rows):
        return {
            "root_id": "wf-root", "available": True, "error": "",
            "root": {"id": "wf-root", "status": "open"},
            "descendants": [
                {"id": task, "status": status, "parent": "wf-root",
                 "metadata": {"agentflow": {"native_observation": value} if value is not None else {}}}
                for task, status, value in rows
            ],
        }

    def classify(self, rows, sessions=None, generations=None, **kwargs):
        return reconciliation.classify_native_visibility(
            self.enumeration(rows), {} if sessions is None else sessions,
            {task: GEN_NEW for task, _, _ in rows} if generations is None else generations,
            now=NOW, **kwargs,
        )

    def test_four_states_are_distinct(self):
        rows = [
            ("t-confirmed", "in_progress", observation()),
            ("t-boundary", "in_progress", observation(seconds_ago=300)),
            ("t-stale", "in_progress", observation(seconds_ago=301)),
            ("t-absent", "in_progress", observation(state="absent", seconds_ago=5000)),
            ("t-missing", "in_progress", None),
            ("t-query-unavailable", "in_progress", observation(state="unavailable", session_id="")),
            ("t-mismatch", "in_progress", observation(claim_generation="0" * 64)),
            ("t-source", "in_progress", observation(source="pane-scrape")),
            ("t-time", "in_progress", observation(observed_at="yesterday")),
            ("t-future", "in_progress", observation(seconds_ago=-600)),
            ("t-no-session", "in_progress", observation(session_id="")),
            ("t-malformed", "in_progress", "present"),
            ("t-open", "open", observation()),
            ("t-assigned", "assigned", observation()),
            ("t-closed", "closed", observation()),
            ("t-managed", "in_progress", observation()),
        ]
        generations = {task: GEN_NEW for task, _, _ in rows}
        generations["t-gen-unavailable"] = None
        rows.append(("t-gen-unavailable", "in_progress", observation()))
        report = self.classify(rows, sessions={"t-managed": {"status": "running"}}, generations=generations)
        self.assertEqual(report["schema"], VISIBILITY_SCHEMA)
        self.assertTrue(report["available"])
        self.assertEqual(report["stale_after_seconds"], 300)
        by_task = {item["task_id"]: item for item in report["tasks"]}
        expected = {
            "t-confirmed": ("confirmed_native", "present"),
            "t-boundary": ("confirmed_native", "present"),
            "t-stale": ("stale", "present"),
            "t-absent": ("absent", "absent"),
            "t-missing": ("unobservable", "missing"),
            "t-query-unavailable": ("unobservable", "unavailable"),
            "t-mismatch": ("unobservable", "mismatched"),
            "t-gen-unavailable": ("unobservable", "mismatched"),
            "t-source": ("unobservable", "invalid"),
            "t-time": ("unobservable", "invalid"),
            "t-future": ("unobservable", "invalid"),
            "t-no-session": ("unobservable", "invalid"),
            "t-malformed": ("unobservable", "invalid"),
        }
        self.assertEqual(
            {task: (item["state"], item["observation"]) for task, item in by_task.items()}, expected
        )
        for item in report["tasks"]:
            self.assertEqual(item["claim"], "claimed")
            self.assertTrue(item["reason"])
            self.assertEqual(
                set(item),
                {"task_id", "claim", "observation", "state", "reason", "provider", "session_id", "observed_at"},
            )
        self.assertEqual(by_task["t-confirmed"]["provider"], "copilot")
        self.assertEqual(by_task["t-confirmed"]["session_id"], "sess-1")
        self.assertNotIn("t-open", by_task)
        self.assertNotIn("t-assigned", by_task)
        self.assertEqual(report["managed_excluded"], ["t-managed"])
        self.assertEqual(
            report["counts"], {"confirmed_native": 2, "stale": 1, "unobservable": 9, "absent": 1}
        )

    def test_freshness_window_is_fixed_and_unavailable_inputs_are_explicit(self):
        report = self.classify([
            ("t-boundary", "in_progress", observation(seconds_ago=300)),
            ("t-stale", "in_progress", observation(seconds_ago=301)),
        ])
        self.assertEqual(report["stale_after_seconds"], 300)
        states = {item["task_id"]: item["state"] for item in report["tasks"]}
        self.assertEqual(states, {"t-boundary": "confirmed_native", "t-stale": "stale"})
        unavailable = reconciliation.classify_native_visibility(
            {"root_id": "wf-root", "available": False, "root": None, "descendants": [], "error": "list failed"},
            {}, {}, now=NOW,
        )
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["tasks"], [])
        self.assertIsNone(unavailable["counts"])
        self.assertIn("list failed", unavailable["error"])
        no_herdr = reconciliation.classify_native_visibility(
            self.enumeration([("t", "in_progress", observation())]), None, {"t": GEN_NEW},
            now=NOW, sessions_error="Herdr state is malformed",
        )
        self.assertFalse(no_herdr["available"])
        self.assertEqual(no_herdr["tasks"], [])
        self.assertIn("Herdr state is malformed", no_herdr["error"])


class NativeVisibilityStatusTests(NativeFixture):
    """V3b: controller status adds native_visibility without changing anything else."""

    def status(self):
        result = self.run_cli(
            "controller", "status", "--root", str(self.workspace), "--workflow-root", "wf-root", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_missing_herdr_record_alone_is_unobservable_and_status_writes_nothing(self):
        self.write_herdr({OLD: {"status": "running", "binding": {"task_id": OLD}}})
        payload = self.status()
        self.assertTrue(payload["ok"])
        visibility = payload["native_visibility"]
        self.assertEqual(visibility["schema"], VISIBILITY_SCHEMA)
        self.assertTrue(visibility["available"])
        by_task = {item["task_id"]: item for item in visibility["tasks"]}
        self.assertEqual(sorted(by_task), [NEW, INELIGIBLE])
        self.assertEqual(by_task[NEW]["state"], "unobservable")
        self.assertEqual(by_task[NEW]["observation"], "missing")
        self.assertEqual(visibility["managed_excluded"], [OLD])
        self.assertEqual(visibility["counts"]["unobservable"], 2)
        self.assertEqual(self.writes(), [])
        self.assertEqual([call for call in self.state["calls"] if call[1] == "history"], [])
        lifecycle = payload["lifecycle_reconciliation"]
        self.assertEqual(
            sorted(item["task"] for item in lifecycle["findings"] if item["id"] == "claim-without-session"),
            [NEW, INELIGIBLE],
        )
        self.assertEqual(payload["completion_audit"]["schema"], "agentflow.completion-audit@1")
        self.assertTrue(payload["completion_audit"]["available"])
        reconcile = self.run_cli(
            "herdr", "reconcile", "--root", str(self.workspace), "--workflow-root", "wf-root", "--json",
        )
        self.assertEqual(json.loads(reconcile.stdout), lifecycle)
        self.assert_no_foreign_tools()

    def test_observed_states_reach_status(self):
        result, payload = self.observe()
        self.assertEqual(result.returncode, 0, payload)
        visibility = self.status()["native_visibility"]
        entry = next(item for item in visibility["tasks"] if item["task_id"] == NEW)
        self.assertEqual((entry["state"], entry["observation"]), ("confirmed_native", "present"))
        self.assertEqual(self.writes(), [])
        self.assertIn(["bd", "history", NEW, "--events", "--limit", "0", "--json"], self.state["calls"])

        self.agentflow(NEW)["native_observation"]["observed_at"] = "2020-01-01T00:00:00+00:00"
        entry = next(item for item in self.status()["native_visibility"]["tasks"] if item["task_id"] == NEW)
        self.assertEqual(entry["state"], "stale")

        result, payload = self.observe(state="absent")
        self.assertEqual(result.returncode, 0, payload)
        entry = next(item for item in self.status()["native_visibility"]["tasks"] if item["task_id"] == NEW)
        self.assertEqual(entry["state"], "absent")

        self.state["history"][NEW].append(
            {"id": "ev-new-3", "issue_id": NEW, "event_type": "claimed", "actor": "someone-else"}
        )
        entry = next(item for item in self.status()["native_visibility"]["tasks"] if item["task_id"] == NEW)
        self.assertEqual((entry["state"], entry["observation"]), ("unobservable", "mismatched"))

        self.state["history_fail"] = [NEW]
        entry = next(item for item in self.status()["native_visibility"]["tasks"] if item["task_id"] == NEW)
        self.assertEqual((entry["state"], entry["observation"]), ("unobservable", "mismatched"))
        self.assertEqual(self.writes(), [])
        self.assert_no_foreign_tools()

    def test_unavailable_enumeration_or_herdr_state_yields_no_task_states(self):
        self.state["list_mode"] = "fail"
        visibility = self.status()["native_visibility"]
        self.assertFalse(visibility["available"])
        self.assertEqual(visibility["tasks"], [])
        self.assertIsNone(visibility["counts"])
        self.state["list_mode"] = ""
        (self.workspace / ".agentflow/herdr").mkdir(parents=True)
        (self.workspace / ".agentflow/herdr/sessions.json").write_text("not json")
        payload = self.status()
        self.assertFalse(payload["native_visibility"]["available"])
        self.assertEqual(payload["native_visibility"]["tasks"], [])
        self.assertEqual(self.writes(), [])


class NativeApproveTests(NativeFixture):
    """V3c: approval is owner-supplied, bound, and validated before its one write."""

    def test_approval_records_bound_authority_once_and_replays_without_writes(self):
        result, payload = self.approve()
        self.assertEqual(result.returncode, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disposition"], "approved")
        authority = self.agentflow(NEW)["native_return_authority"]
        self.assertEqual(authority["schema"], AUTHORITY_SCHEMA)
        self.assertEqual(
            {key: authority[key] for key in (
                "workflow_root", "task_id", "provider", "base", "acceptance_ids",
                "claim_generation", "approval_ref", "approved_by", "actor",
            )},
            {
                "workflow_root": "wf-root", "task_id": NEW, "provider": "copilot", "base": BASE,
                "acceptance_ids": ["AC-1", "AC-2"], "claim_generation": GEN_NEW,
                "approval_ref": "owner-approval-59",
                "approved_by": OWNER, "actor": "controller-a",
            },
        )
        self.assertTrue(authority["recorded_at"])
        self.assertEqual(self.state["issues"][NEW]["metadata"]["unrelated"], "kept")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][:3], ["bd", "update", NEW])
        self.assertEqual(self.writes()[0][3], "--metadata")
        self.assertEqual(self.state["issues"][NEW]["status"], "in_progress")
        self.assertEqual(self.state["issues"][NEW]["assignee"], WORKER)

        result, payload = self.approve(actor="controller-b")
        self.assertEqual(result.returncode, 0, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.agentflow(NEW)["native_return_authority"], authority)

        result, payload = self.approve(approval_ref="another-approval")
        self.assert_rejected(result, payload, "different native return approval")
        self.assert_no_foreign_tools()

    def test_invalid_approvals_are_rejected_with_zero_writes(self):
        cases = [
            ("unknown provider", {"provider": "unknown"}, None, "unsupported provider"),
            ("short base", {"base": "abc123"}, None, "--base"),
            ("upper base", {"base": "A" * 40}, None, "--base"),
            ("empty ref", {"approval_ref": "  "}, None, "--approval-ref"),
            ("empty approver", {"approved_by": ""}, None, "--approved-by"),
            ("self approval", {"approved_by": WORKER}, None, "self-approval"),
            ("ineligible label", {"task": INELIGIBLE}, None, "credential-use"),
            ("terminal", {"task": DONE}, None, "closed"),
            ("unclaimed", {}, lambda s: s["issues"][NEW].update(status="open", assignee=""),
             "claim holder"),
            ("outside root", {"task": OUTSIDE}, None, "not a descendant"),
            ("unbound matrix", {}, lambda s: s["issues"][NEW]["metadata"]["agentflow"]["acceptance"]
             .update(task_id=OLD), "acceptance matrix"),
            ("no matrix", {}, lambda s: s["issues"][NEW]["metadata"]["agentflow"].pop("acceptance"),
             "acceptance matrix"),
            ("ineligible requirement", {}, lambda s: s["issues"][NEW]["metadata"]["agentflow"]
             .update(requirements=["publication"]), "publication"),
            ("requirements not list", {}, lambda s: s["issues"][NEW]["metadata"]["agentflow"]
             .update(requirements="publication"), "requirements"),
            ("requirement not string", {}, lambda s: s["issues"][NEW]["metadata"]["agentflow"]
             .update(requirements=[1]), "requirements"),
            ("enumeration unavailable", {}, lambda s: s.update(list_mode="fail"), "enumeration"),
        ]
        original = copy.deepcopy(self.state)
        for name, overrides, mutate, text in cases:
            with self.subTest(case=name):
                self.state = copy.deepcopy(original)
                if mutate:
                    mutate(self.state)
                result, payload = self.approve(**overrides)
                self.assert_rejected(result, payload, text)
        self.assert_no_foreign_tools()

    def test_claim_generation_change_after_validation_rejects_without_write(self):
        self.state["claim_change_after"] = {NEW: 1}
        result, payload = self.approve()
        self.assert_rejected(result, payload, "claim generation changed")
        self.assert_no_foreign_tools()

    def test_herdr_bound_or_unreadable_herdr_state_is_rejected(self):
        self.write_herdr({NEW: {"status": "running"}})
        result, payload = self.approve()
        self.assert_rejected(result, payload, "Herdr session record")
        (self.workspace / ".agentflow/herdr/sessions.json").write_text("{broken")
        result, payload = self.approve()
        self.assert_rejected(result, payload, "Herdr")

    def test_disabled_lane_is_rejected_before_any_beads_call(self):
        disabled = ProviderCapability(
            provider="copilot", chat_controller=CapabilityState.SUPPORTED,
            native_subagents=CapabilityState.SUPPORTED, direct_cli=CapabilityState.SUPPORTED,
            persistent_herdr=CapabilityState.DISABLED,
            controller_observed_return=CapabilityState.DISABLED,
        )
        output = io.StringIO()
        with mock.patch.object(cli.provider_capabilities_backend, "capability_for", return_value=disabled), \
             mock.patch.object(cli.beads_backend, "run") as run, redirect_stdout(output):
            code = cli.main([
                "native", "approve", "--workflow-root", "wf-root", "--task", NEW,
                "--provider", "copilot", "--base", BASE, "--approval-ref", "ref",
                "--approved-by", OWNER, "--cwd", str(self.workspace), "--json",
            ])
        self.assertEqual(code, 2)
        self.assertIn("disabled", json.loads(output.getvalue())["error"])
        run.assert_not_called()


class NativeObserveTests(NativeFixture):
    """V3c: observation is bound to the current claim generation and never claims."""

    def test_observation_binds_claim_generation_and_latest_wins(self):
        result, payload = self.observe()
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["disposition"], "observed")
        record = self.agentflow(NEW)["native_observation"]
        self.assertEqual(
            {key: value for key, value in record.items() if key != "observed_at"},
            {
                "schema": OBSERVATION_SCHEMA, "provider": "copilot", "session_id": "sess-new",
                "state": "present", "source": "exact-session-query", "observer": "controller-a",
                "claim_generation": GEN_NEW,
            },
        )
        dt.datetime.fromisoformat(record["observed_at"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][:4], ["bd", "update", NEW, "--metadata"])
        self.assertEqual(self.state["calls"][-1], ["bd", "show", NEW, "--json"])
        self.assertEqual(self.state["issues"][NEW]["status"], "in_progress")
        self.assertEqual(self.state["issues"][NEW]["assignee"], WORKER)

        result, payload = self.observe(state="unavailable", session_id="")
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(self.agentflow(NEW)["native_observation"]["state"], "unavailable")
        self.assert_no_foreign_tools()

    def test_invalid_observations_are_rejected_with_zero_writes(self):
        cases = [
            ("unclaimed", {"task": UNCLAIMED}, None, "in_progress claim holder"),
            ("assigned", {}, lambda s: s["issues"][NEW].update(status="assigned"),
             "in_progress claim holder"),
            ("terminal", {"task": DONE}, None, "closed"),
            ("outside", {"task": OUTSIDE}, None, "not a descendant"),
            ("history unavailable", {}, lambda s: s.update(history_fail=[NEW]), "claim generation"),
            ("no claim events", {}, lambda s: s["history"].update({NEW: []}), "claim generation"),
            ("present without session", {"session_id": ""}, None, "--session-id"),
            ("absent without session", {"state": "absent", "session_id": ""}, None, "--session-id"),
            ("bad state", {"state": "gone"}, None, "--state"),
            ("unknown provider", {"provider": "other"}, None, "unsupported provider"),
            ("empty observer", {"observer": " "}, None, "--observer"),
        ]
        original = copy.deepcopy(self.state)
        for name, overrides, mutate, text in cases:
            with self.subTest(case=name):
                self.state = copy.deepcopy(original)
                if mutate:
                    mutate(self.state)
                result, payload = self.observe(**overrides)
                self.assert_rejected(result, payload, text)
        self.write_herdr({NEW: {"status": "running"}})
        self.state = copy.deepcopy(original)
        result, payload = self.observe()
        self.assert_rejected(result, payload, "Herdr session record")
        self.assert_no_foreign_tools()


class NativeAcceptTests(NativeFixture):
    """V3c: acceptance validates everything, writes a receipt, closes once as the holder."""

    def test_accept_records_receipt_closes_as_holder_and_replays(self):
        prepared = self.prepared()
        observation_record = prepared["issues"][NEW]["metadata"]["agentflow"]["native_observation"]
        path, value = self.result_file()
        result, payload = self.accept(path)
        self.assertEqual(result.returncode, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disposition"], "accepted")
        self.assertEqual(self.state["issues"][NEW]["status"], "closed")
        receipt = self.agentflow(NEW)["native_return_receipt"]
        self.assertEqual(
            {key: value for key, value in receipt.items() if key != "accepted_at"},
            {
                "schema": RECEIPT_SCHEMA, "workflow_root": "wf-root", "task_id": NEW,
                "provider": "copilot", "session_id": "sess-new", "claim_generation": GEN_NEW,
                "base": BASE, "head": HEAD, "acceptance_ids": ["AC-1", "AC-2"],
                "verdict": "usable", "result_sha256": sha256_json(value),
                "approval_ref": "owner-approval-59",
                "observation_sha256": sha256_json(observation_record),
                "accepted_by": "controller-a", "attestation": ATTESTATION,
            },
        )
        self.assertEqual(payload["receipt"], receipt)
        writes = self.writes()
        self.assertEqual([call[1] for call in writes], ["update", "close"])
        self.assertEqual(writes[0][3], "--metadata")
        close = writes[1]
        self.assertEqual(close[2], NEW)
        self.assertEqual(close[close.index("--actor") + 1], WORKER)
        self.assertEqual(self.state["calls"][-1], ["bd", "show", NEW, "--json"])
        agentflow = self.agentflow(NEW)
        self.assertEqual(agentflow["native_observation"], observation_record)
        self.assertIn("native_return_authority", agentflow)
        self.assertEqual(self.state["issues"][NEW]["metadata"]["unrelated"], "kept")

        result, payload = self.accept(path, actor="controller-b")
        self.assertEqual(result.returncode, 0, payload)
        self.assertTrue(payload["replayed"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.agentflow(NEW)["native_return_receipt"], receipt)

        other, _ = self.result_file(head="3" * 40)
        result, payload = self.accept(other)
        self.assert_rejected(result, payload)
        self.assert_no_foreign_tools()

    def test_unconfirmed_close_is_unresolved_and_retry_closes_without_rewriting(self):
        self.prepared()
        self.state["close_mode"] = {NEW: "open-error"}
        path, _ = self.result_file()
        result, payload = self.accept(path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["disposition"], "closure_unresolved")
        self.assertEqual([call[1] for call in self.writes()], ["update", "close"])
        self.assertEqual(self.state["issues"][NEW]["status"], "in_progress")
        receipt = self.agentflow(NEW)["native_return_receipt"]

        self.state["close_mode"] = {}
        result, payload = self.accept(path)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual([call[1] for call in self.writes()], ["close"])
        self.assertEqual(self.state["issues"][NEW]["status"], "closed")
        self.assertEqual(self.agentflow(NEW)["native_return_receipt"], receipt)

    def test_claim_change_after_receipt_keeps_close_unresolved(self):
        self.prepared()
        self.state["close_mode"] = {NEW: "open-error"}
        path, _ = self.result_file()
        result, payload = self.accept(path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["disposition"], "closure_unresolved")
        receipt = copy.deepcopy(self.agentflow(NEW)["native_return_receipt"])

        self.state["history"][NEW].append({
            "id": "ev-new-raced", "issue_id": NEW, "event_type": "claimed", "actor": "another-worker",
        })
        self.state["issues"][NEW]["assignee"] = "another-worker"
        result, payload = self.accept(path)
        self.assert_rejected(result, payload, "claim generation")
        self.assertEqual(self.state["issues"][NEW]["status"], "in_progress")
        self.assertEqual(self.agentflow(NEW)["native_return_receipt"], receipt)

    def test_invalid_results_are_rejected_with_zero_writes(self):
        prepared = self.prepared()

        def results(*rows):
            return [{"acceptance_id": row, "status": "passed", "evidence": "ok"} for row in rows]

        def agentflow(state):
            return state["issues"][NEW]["metadata"]["agentflow"]

        cases = [
            ("schema", {"schema": "agentflow.native-return@2"}, None, "schema"),
            ("workflow root", {"workflow_root": "other-root"}, None, "workflow_root"),
            ("task", {"task_id": OLD}, None, "task_id"),
            ("base", {"base": "3" * 40}, None, "base"),
            ("head", {"head": "not-a-sha"}, None, "head"),
            ("generation", {"claim_generation": "0" * 64}, None, "claim generation"),
            ("missing row", {"acceptance_results": results("AC-1")}, None, "acceptance"),
            ("extra row", {"acceptance_results": results("AC-1", "AC-2", "AC-3")}, None, "acceptance"),
            ("duplicate row", {"acceptance_results": results("AC-1", "AC-1", "AC-2")}, None, "duplicate"),
            ("empty evidence", {"acceptance_results": [
                {"acceptance_id": "AC-1", "status": "passed", "evidence": ""},
                {"acceptance_id": "AC-2", "status": "passed", "evidence": "ok"},
            ]}, None, "evidence"),
            ("waiver", {"acceptance_results": [
                {"acceptance_id": "AC-1", "status": "waived", "evidence": "skip"},
                {"acceptance_id": "AC-2", "status": "passed", "evidence": "ok"},
            ]}, None, "passed"),
            ("verdict", {"verdict": "needs-work"}, None, "verdict"),
            ("provider", {"provider": "codex"}, None, "provider"),
            ("observed session", {"session_id": "other-session"}, None, "session"),
            ("matrix not passed", {}, lambda s: agentflow(s).update(acceptance=matrix(NEW, "planned")),
             "controller validation"),
            ("no approval", {}, lambda s: agentflow(s).pop("native_return_authority"), "approval"),
            ("approval for another task", {}, lambda s: agentflow(s)["native_return_authority"]
             .update(task_id=OLD), "approval"),
            ("ineligible after approval", {}, lambda s: s["issues"][NEW]["labels"]
             .append("af:requires:publication"), "publication"),
            ("observation generation", {}, lambda s: agentflow(s)["native_observation"]
             .update(claim_generation="0" * 64), "observation"),
            ("self approval", {}, lambda s: agentflow(s)["native_return_authority"]
             .update(approved_by=WORKER), "self-approval"),
            ("history unavailable", {}, lambda s: s.update(history_fail=[NEW]), "claim generation"),
            ("not claimed", {}, lambda s: s["issues"][NEW].update(status="open", assignee=""),
             "claim holder"),
            ("different receipt", {}, lambda s: agentflow(s).update(native_return_receipt={
                "schema": RECEIPT_SCHEMA, "workflow_root": "wf-root", "task_id": NEW,
                "result_sha256": "0" * 64,
            }), "different"),
        ]
        for name, overrides, mutate, text in cases:
            with self.subTest(case=name):
                self.state = copy.deepcopy(prepared)
                if mutate:
                    mutate(self.state)
                path, _ = self.result_file(**overrides)
                result, payload = self.accept(path)
                self.assert_rejected(result, payload, text)
                self.assertNotIn("native_return_receipt", {} if name == "different receipt"
                                 else self.agentflow(NEW))
        self.state = copy.deepcopy(prepared)
        missing = self.directory / "missing.json"
        result, payload = self.accept(missing)
        self.assert_rejected(result, payload, "result")
        self.write_herdr({NEW: {"status": "running"}})
        path, _ = self.result_file()
        result, payload = self.accept(path)
        self.assert_rejected(result, payload, "Herdr session record")
        self.write_herdr({})
        path, _ = self.result_file()
        link = self.directory / "result-link.json"
        link.symlink_to(path)
        result, payload = self.accept(link)
        self.assert_rejected(result, payload, "non-symlink")
        oversized = self.directory / "oversized-result.json"
        oversized.write_text(" " * (64 * 1024 + 1))
        result, payload = self.accept(oversized)
        self.assert_rejected(result, payload, "too large")
        self.assert_no_foreign_tools()


class NegativeAuthorityTests(NativeFixture):
    """V3d: accepting a distinct task never revives or rewrites old managed state."""

    def old_record(self):
        result = {"task_id": OLD, "launch_id": "launch-old", "provider": "codex",
                  "session_id": "sess-old", "status": "completed"}
        return {
            "status": "exited", "task_id": OLD, "lease_epoch": 1, "launch_id": "launch-old",
            "binding": {"task_id": OLD, "launch_id": "launch-old", "provider": "codex",
                        "session_id": "sess-old", "lease_epoch": 1},
            "result": result,
            "return_channel": {"state": "consumed", "result_sha256": "f" * 64},
        }

    def seed_agentflow_state(self):
        files = {
            "controller/wf-root.json": {"lease": {"epoch": 2, "owner_id": "controller-now"}},
            "controller/wf-root.checkpoint.json": {"epoch": 2, "active_tasks": [{"task": OLD, "epoch": 1}]},
            f"claims/{hashlib.sha256(OLD.encode()).hexdigest()[:24]}.json": {"task": OLD, "epoch": 1},
            "controller/wf-root-task-claim-fences/old.json": {
                "schema": "agentflow.task-claim-fence@1", "task": OLD, "reason": "failed-auth",
            },
            "controller/wf-root-task-claim-fences-generations/old.json": {
                "schema": "agentflow.native-claim-generation@1", "task": OLD,
                "native_generation": GEN_OLD,
            },
        }
        for relative, value in files.items():
            path = self.workspace / ".agentflow" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2))
        self.state_home.mkdir(parents=True, exist_ok=True)
        (self.state_home / "ledger.json").write_text(json.dumps({"epoch": 2}))
        return self.write_herdr({OLD: self.old_record()})

    def snapshot(self):
        return tree_digest(self.workspace / ".agentflow"), tree_digest(self.state_home)

    def test_distinct_acceptance_leaves_old_managed_state_untouched(self):
        herdr_path = self.seed_agentflow_state()
        record = json.loads(herdr_path.read_text())["sessions"][OLD]
        self.assertFalse(cli._has_authenticated_herdr_result(record))
        before = self.snapshot()
        self.assertGreaterEqual(len(before[0]), 6)
        old_before = copy.deepcopy(self.state["issues"][OLD])

        self.prepared()
        path, _ = self.result_file()
        result, payload = self.accept(path)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(self.state["issues"][NEW]["status"], "closed")
        self.assertIn("native_return_receipt", self.agentflow(NEW))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state["issues"][OLD], old_before)
        record = json.loads(herdr_path.read_text())["sessions"][OLD]
        self.assertFalse(cli._has_authenticated_herdr_result(record))

        old_result, _ = self.result_file(task=OLD, provider="codex", session_id="sess-old",
                                         claim_generation=GEN_OLD)
        for name, run in (
            ("approve", lambda: self.approve(OLD, provider="codex")),
            ("observe", lambda: self.observe(OLD, provider="codex", session_id="sess-old")),
            ("accept", lambda: self.accept(old_result, OLD)),
        ):
            with self.subTest(command=name):
                result, payload = run()
                self.assert_rejected(result, payload, "Herdr session record")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state["issues"][OLD], old_before)
        self.assertFalse(cli._has_authenticated_herdr_result(
            json.loads(herdr_path.read_text())["sessions"][OLD]
        ))
        self.assert_no_foreign_tools()


if __name__ == "__main__":
    unittest.main()
