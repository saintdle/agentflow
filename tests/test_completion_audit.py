from __future__ import annotations

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

from agentflow import beads as beads_backend  # noqa: E402
from agentflow import checkpoint as checkpoint_backend  # noqa: E402
from agentflow import cli  # noqa: E402
from agentflow.session_control import PACKET_SCHEMA  # noqa: E402


OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
THIRD_HEAD = "c" * 40
AUDIT_SCHEMA = "agentflow.completion-audit@1"
SUPERSESSION_SCHEMA = "agentflow.ci-supersession@1"


FAKE = r'''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["AUDIT_FIXTURE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
state["calls"].append([tool, *args])

def finish(value=None, code=0, error="", raw=None):
    path.write_text(json.dumps(state))
    if raw is not None:
        sys.stdout.write(raw)
    elif value is not None:
        print(json.dumps(value))
    if error:
        print(error, file=sys.stderr)
    sys.exit(code)

def option(name):
    return args[args.index(name) + 1]

if tool != "bd":
    finish(code=90, error="unexpected external tool")
issues = state["issues"]

def after(event):
    # Deterministic interleaving: apply a concurrent write after the Nth event.
    counts = state.setdefault("event_counts", {})
    counts[event] = counts.get(event, 0) + 1
    for hook in state.get("hooks", []):
        if hook["on"] == event and hook["count"] == counts[event]:
            for keys, value in hook["set"]:
                target = issues
                for part in keys[:-1]:
                    target = target[part]
                target[keys[-1]] = value

if args[:1] == ["list"]:
    gates = "--include-gates" in args
    mode = state.get("list_mode", "") if gates else ""
    if mode == "fail":
        finish(code=1, error="list failed")
    if mode == "raw":
        finish(raw=state["list_raw"])
    rows = [json.loads(json.dumps(issue)) for issue in issues.values()
            if (gates or issue.get("issue_type") != "gate")
            and issue["id"] not in state.get("list_omit", [])]
    if mode == "dict":
        finish({"issues": rows, **state.get("list_extra", {})})
    finish(rows)
if args[:1] == ["show"]:
    key = args[1]
    if key in state.get("closed_attempts", []):
        state.setdefault("readbacks", {})[key] = state.setdefault("readbacks", {}).get(key, 0) + 1
    if key in state.get("show_fail", []) or key not in issues:
        finish(code=1, error="issue not found")
    shown = json.loads(json.dumps([issues[key]]))
    after("show:" + key)
    finish(shown)
if args[:1] == ["update"]:
    key = args[1]
    if key in state.get("update_fail", []):
        finish(code=1, error="update unavailable")
    if "--metadata" in args:
        issues[key]["metadata"] = json.loads(option("--metadata"))
    if "--status" in args:
        issues[key]["status"] = option("--status")
    after("update:" + key)
    finish()
if args[:1] == ["close"]:
    key = args[1]
    state.setdefault("closed_attempts", []).append(key)
    mode = state.get("close_mode", {}).get(key, "success")
    if mode != "open-error":
        issues[key]["status"] = "closed"
    finish(code=0 if mode == "success" else 1, error="" if mode == "success" else "close ambiguous")
if args[:1] == ["ready"]:
    finish(state.get("ready", []))
finish(code=90, error="unexpected bd command")
'''


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def ci_record(gate, parent, head, status, *, disposition, obligation, repo="example/project"):
    checks = [{
        "bucket": "fail" if status == "failed" else "pass", "id": f"run-{head[:4]}",
        "kind": "CheckRun", "link": "https://github.com/example/project/actions/runs/1",
        "name": "unit", "required": True, "state": "COMPLETED",
    }]
    record = {
        "status": status, "observed_result": status,
        "disposition": disposition, "obligation": obligation,
        "identity": {
            "gate": gate, "parent": parent, "repo": repo, "pr": 42, "head": head,
            "required_only": False, "check_set_sha256": digest(checks),
        },
        "checks": checks, "pr": "42", "repo": repo, "required_only": False,
        "check_counts": {checks[0]["bucket"]: 1},
        "failed_checks": ["unit"] if status == "failed" else [],
        "observed_at": "2026-10-30T00:00:00+00:00", "triage_receipts": {},
    }
    return record


def failure_identity(record):
    return {
        **{key: value for key, value in record["identity"].items() if key != "required_only"},
        "check_set_sha256": digest([
            {key: value for key, value in check.items() if key != "required"}
            for check in record["checks"] if check["bucket"] in {"fail", "cancel"}
        ]),
    }


def issue(identifier, status, parent="", *, labels=(), metadata=None, issue_type="task", deps=()):
    dependencies = [
        {"issue_id": identifier, "depends_on_id": parent, "type": "parent-child"}
    ] if parent else []
    dependencies.extend(
        {"issue_id": identifier, "depends_on_id": target, "type": kind} for kind, target in deps
    )
    value = {
        "id": identifier, "title": identifier, "status": status, "issue_type": issue_type,
        "labels": list(labels), "dependencies": dependencies,
        "metadata": metadata if metadata is not None else {},
    }
    if parent:
        value["parent"] = parent
    return value


def graph(*, root_status="closed", parent_status="closed"):
    old = ci_record("zz-ci-old", "wf-root.1", OLD_HEAD, "failed", disposition="open", obligation="required")
    key = digest(failure_identity(old))
    old["triage_receipts"] = {key: "zz-citriage"}
    old["triage_bead"] = "zz-citriage"
    new = ci_record("wf-ci-new", "wf-root.1", NEW_HEAD, "passed", disposition="closed", obligation="accepted")
    rows = [
        issue("wf-root", root_status, issue_type="epic"),
        issue("wf-root.1", parent_status, "wf-root", deps=[("blocks", "ext-1")]),
        issue(
            "zz-ci-old", "open", "wf-root.1", labels=["af:stage:ci"], issue_type="gate",
            metadata={"unrelated": "preserved", "agentflow": {"stage": "ci", "ci": old}},
        ),
        issue(
            "zz-citriage", "open", "wf-root.1",
            labels=["agentflow", "af:stage:ci-triage", "af:cap:ci", "af:role:ci"],
            deps=[("blocks", "zz-ci-old")],
            metadata={"agentflow": {
                "stage": "ci-triage", "capability": "ci", "ci_bead": "zz-ci-old",
                "original_writer": "", "pr": "42", "repo": "example/project",
                "ci_identity": old["identity"], "failure_identity": failure_identity(old),
            }},
        ),
        issue(
            "wf-ci-new", "closed", "wf-root.1", labels=["af:stage:ci"],
            metadata={"agentflow": {"stage": "ci", "ci": new}},
        ),
        issue("ext-1", "open"),
        issue("other-root", "open", issue_type="epic"),
        issue("other-1", "open", "other-root"),
    ]
    return {row["id"]: row for row in rows}


def normalize(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.endswith("_at") and isinstance(item, str):
                result[key] = "<ts>"
            elif key in {"continuity_id", "workspace_root"}:
                result[key] = f"<{key}>"
            else:
                result[key] = normalize(item)
        return result
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


class CompletionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".agentflow" / ("audit-fixture-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.workspace = self.directory / "repo"
        self.workspace.mkdir()
        self.state_home = self.directory / "state"
        self.state_path = self.directory / "fixture.json"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("bd", "gh"):
            executable = self.bin / name
            executable.write_text(FAKE.replace("__PYTHON__", sys.executable))
            executable.chmod(0o755)
        self.state = {"calls": [], "issues": graph()}

    def environment(self):
        environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "AUDIT_FIXTURE": str(self.state_path),
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
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result

    def status(self, workflow_root="wf-root"):
        result = self.run_cli(
            "controller", "status", "--root", str(self.workspace),
            "--workflow-root", workflow_root, "--json",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def supersede(self, *, bead="zz-ci-old", successor="wf-ci-new", reason="New head passed",
                  decision_ref="issue-57", actor="auditor", workflow_root="wf-root"):
        arguments = [
            "ci", "supersede", "--bead", bead, "--successor", successor,
            "--workflow-root", workflow_root, "--reason", reason,
            "--decision-ref", decision_ref, "--cwd", str(self.workspace), "--json",
        ]
        if actor:
            arguments.extend(["--actor", actor])
        result = self.run_cli(*arguments)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(result.stdout + result.stderr)
        return result, payload

    def writes(self):
        return [call for call in self.state["calls"]
                if call[0] == "bd" and call[1] in {"update", "close", "create", "comment"}]

    def gh_calls(self):
        return [call for call in self.state["calls"] if call[0] == "gh"]

    def metadata(self, identifier):
        return self.state["issues"][identifier]["metadata"]

    def hook(self, event, count, *changes):
        self.state.setdefault("hooks", []).append(
            {"on": event, "count": count, "set": [[list(keys), value] for keys, value in changes]}
        )


class CompletionAuditStatusTests(CompletionFixture):
    """V2a: complete enumeration under a terminal root."""

    def test_open_failed_gate_under_closed_ancestors_is_reported(self):
        payload = self.status()
        audit = payload["completion_audit"]
        self.assertEqual(audit["schema"], AUDIT_SCHEMA)
        self.assertTrue(audit["available"])
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["root"], {"id": "wf-root", "status": "closed"})
        self.assertEqual(
            sorted(item["id"] for item in audit["open_descendants"]), ["zz-ci-old", "zz-citriage"]
        )
        self.assertEqual(audit["counts"]["descendants"], 4)
        self.assertEqual(audit["counts"]["open"], 2)
        findings = {(item["id"], item["task"]): item for item in audit["findings"]}
        self.assertEqual(findings[("open-under-terminal-ancestor", "zz-ci-old")]["severity"], "high")
        self.assertEqual(findings[("open-under-terminal-ancestor", "zz-ci-old")]["ancestor"], "wf-root.1")
        self.assertEqual(findings[("open-under-terminal-ancestor", "zz-citriage")]["severity"], "high")
        unaccepted = findings[("ci-result-unaccepted", "zz-ci-old")]
        self.assertEqual(unaccepted["severity"], "high")
        self.assertEqual(unaccepted["observed_result"], "failed")
        outside = findings[("outside-root-dependency", "wf-root.1")]
        self.assertEqual((outside["severity"], outside["depends_on"]), ("info", "ext-1"))
        self.assertNotIn(("ci-result-unaccepted", "wf-ci-new"), findings)
        gates = {item["id"]: item for item in audit["ci"]}
        self.assertEqual(
            (gates["zz-ci-old"]["observed_result"], gates["zz-ci-old"]["disposition"],
             gates["zz-ci-old"]["obligation"], gates["zz-ci-old"]["head"]),
            ("failed", "open", "required", OLD_HEAD),
        )
        self.assertEqual(
            (gates["wf-ci-new"]["observed_result"], gates["wf-ci-new"]["disposition"],
             gates["wf-ci-new"]["obligation"]),
            ("passed", "closed", "accepted"),
        )
        self.assertNotIn("ext-1", json.dumps(audit["open_descendants"]))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.gh_calls(), [])
        listing = [call for call in self.state["calls"] if call[1] == "list"]
        self.assertIn(["bd", "list", "--all", "--include-gates", "--brief", "--limit", "0", "--json"], listing)

    def test_lifecycle_reconciliation_and_herdr_reconcile_are_unchanged(self):
        payload = self.status()
        expected = {
            "schema": "agentflow.lifecycle-reconciliation@1", "ok": True,
            "descendants": 3, "sessions": 0, "findings": [],
        }
        self.assertEqual(payload["lifecycle_reconciliation"], expected)
        self.assertTrue(payload["ok"])
        result = self.run_cli(
            "herdr", "reconcile", "--root", str(self.workspace), "--workflow-root", "wf-root", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_in_progress_root_keeps_lifecycle_meaning(self):
        self.state["issues"] = graph(root_status="open", parent_status="in_progress")
        payload = self.status()
        lifecycle = payload["lifecycle_reconciliation"]
        self.assertFalse(lifecycle["ok"])
        self.assertEqual([item["id"] for item in lifecycle["findings"]], ["claim-without-session"])
        audit = payload["completion_audit"]
        self.assertTrue(audit["available"])
        self.assertFalse(audit["complete"])
        self.assertFalse(any(item["id"] == "open-under-terminal-ancestor" for item in audit["findings"]))
        result = self.run_cli(
            "herdr", "reconcile", "--root", str(self.workspace), "--workflow-root", "wf-root", "--json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), lifecycle)

    def test_closed_failed_gate_without_supersession_is_unaccepted(self):
        self.state["issues"]["zz-ci-old"]["status"] = "closed"
        self.state["issues"]["zz-citriage"]["status"] = "closed"
        audit = self.status()["completion_audit"]
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["counts"]["open"], 0)
        self.assertIn(
            ("ci-result-unaccepted", "zz-ci-old"),
            {(item["id"], item["task"]) for item in audit["findings"] if item["severity"] == "high"},
        )

    def test_legacy_unbound_ci_record_is_reported(self):
        legacy = {"status": "passed", "pr": "42"}
        self.state["issues"]["wf-ci-new"]["metadata"]["agentflow"]["ci"] = legacy
        audit = self.status()["completion_audit"]
        self.assertIn(
            ("legacy-unbound-ci", "wf-ci-new", "info"),
            {(item["id"], item["task"], item["severity"]) for item in audit["findings"]},
        )


class CompletionAuditUnavailableTests(CompletionFixture):
    """V2b: incomplete enumeration is never a zero-open success."""

    def assert_unavailable(self, audit):
        self.assertEqual(audit["schema"], AUDIT_SCHEMA)
        self.assertFalse(audit["available"])
        self.assertFalse(audit["complete"])
        self.assertIsNone(audit["counts"])
        self.assertIsNone(audit["open_descendants"])
        self.assertTrue(audit["error"])

    def all_closed(self):
        for row in self.state["issues"].values():
            row["status"] = "closed"

    def test_listing_failure_modes(self):
        cases = {
            "nonzero": {"list_mode": "fail"},
            "non-json": {"list_mode": "raw", "list_raw": "warning: not json\n"},
            "truncated-json": {"list_mode": "raw", "list_raw": '[{"id": "wf-root", "status": "closed"},'},
            "unexpected-shape": {"list_mode": "raw", "list_raw": '{"rows": []}'},
            "non-object-row": {"list_mode": "raw", "list_raw": '[{"id": "wf-root"}, 7]'},
            "has-more": {"list_mode": "dict", "list_extra": {"has_more": True}},
            "truncated": {"list_mode": "dict", "list_extra": {"truncated": True}},
            "next-cursor": {"list_mode": "dict", "list_extra": {"next_cursor": "page-2"}},
            "empty": {"list_mode": "raw", "list_raw": "[]"},
            "ancestor-missing": {"list_omit": ["wf-root.1"]},
            "root-missing": {"list_omit": ["wf-root"]},
        }
        for name, settings in cases.items():
            with self.subTest(name):
                self.state = {"calls": [], "issues": graph()}
                self.all_closed()
                self.state.update(settings)
                self.assert_unavailable(self.status()["completion_audit"])
                self.assertEqual(self.writes(), [])

    def test_malformed_nested_fields_are_unavailable(self):
        cases = {
            "dependencies-not-list": ("zz-citriage", "dependencies", 7),
            "dependency-entry-not-object": ("wf-root.1", "dependencies", [7]),
            "root-dependencies-object": ("wf-root", "dependencies", {"depends_on_id": "ext-1"}),
            "metadata-not-object": ("wf-ci-new", "metadata", "agentflow"),
            "status-not-text": ("wf-root.1", "status", 5),
            "status-missing": ("wf-ci-new", "status", None),
            "parent-not-text": ("zz-ci-old", "parent", ["wf-root.1"]),
            "parent-id-not-text": ("wf-ci-new", "parent_id", {"id": "wf-root.1"}),
        }
        for name, (identifier, field, value) in cases.items():
            with self.subTest(name):
                self.state = {"calls": [], "issues": graph()}
                self.all_closed()
                self.state["issues"][identifier][field] = value
                self.assert_unavailable(self.status()["completion_audit"])
                self.assertEqual(self.writes(), [])

    def test_parent_cycle_is_unavailable(self):
        self.all_closed()
        self.state["issues"]["cyc-a"] = issue("cyc-a", "closed", "cyc-b")
        self.state["issues"]["cyc-b"] = issue("cyc-b", "closed", "cyc-a")
        self.assert_unavailable(self.status()["completion_audit"])

    def test_unprovable_root_is_unavailable(self):
        self.all_closed()
        self.assert_unavailable(self.status("missing-root")["completion_audit"])

    def test_dict_listing_without_markers_is_available(self):
        self.all_closed()
        self.state.update(list_mode="dict", list_extra={"has_more": False})
        audit = self.status()["completion_audit"]
        self.assertTrue(audit["available"])

    def test_timeout_is_unavailable(self):
        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[1:2] == ["list"] and "--include-gates" in command:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))
            return real_run(command, *args, **kwargs)

        self.all_closed()
        self.state_path.write_text(json.dumps(self.state))
        output = io.StringIO()
        environment = self.environment()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(beads_backend.subprocess, "run", side_effect=fake_run),
            redirect_stdout(output),
        ):
            code = cli.main([
                "controller", "status", "--root", str(self.workspace),
                "--workflow-root", "wf-root", "--json",
            ])
        self.assertEqual(code, 0)
        self.assert_unavailable(json.loads(output.getvalue())["completion_audit"])

    def test_root_descendants_scheduling_behaviour_is_unchanged(self):
        self.state.update(list_omit=["wf-root.1"])
        self.state_path.write_text(json.dumps(self.state))
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            rows = beads_backend.root_descendants(self.workspace, "wf-root")
        self.assertEqual([row["id"] for row in rows], [])
        self.state = {"calls": [], "issues": graph()}
        self.state_path.write_text(json.dumps(self.state))
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            rows = beads_backend.root_descendants(self.workspace, "wf-root")
        self.assertEqual([row["id"] for row in rows], ["wf-ci-new", "wf-root.1", "zz-citriage"])
        self.assertEqual(
            json.loads(self.state_path.read_text())["calls"][-1],
            ["bd", "list", "--all", "--brief", "--limit", "0", "--json"],
        )


class CISupersedeTests(CompletionFixture):
    """V2c: recorded supersession without rewriting CI evidence."""

    def immutable_digests(self):
        old = self.metadata("zz-ci-old")["agentflow"]
        triage = self.metadata("zz-citriage")["agentflow"]
        return {
            "ci": digest(old["ci"]),
            "head": old["ci"]["identity"]["head"],
            "checks": digest(old["ci"]["checks"]),
            "failure_identity": digest(triage["failure_identity"]),
            "ci_identity": digest(triage["ci_identity"]),
            "successor_ci": digest(self.metadata("wf-ci-new")["agentflow"]["ci"]),
        }

    def test_supersede_records_successor_and_closes_triage_then_gate(self):
        before = self.immutable_digests()
        new_identity = self.metadata("wf-ci-new")["agentflow"]["ci"]["identity"]
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disposition"], "superseded")
        self.assertEqual(payload["observed_result"], "failed")
        self.assertEqual(payload["unresolved"], [])
        self.assertEqual(self.immutable_digests(), before)
        self.assertEqual(self.metadata("zz-ci-old")["unrelated"], "preserved")
        record = self.metadata("zz-ci-old")["agentflow"]["supersession"]
        self.assertEqual(record["schema"], SUPERSESSION_SCHEMA)
        self.assertEqual(record["successor"], "wf-ci-new")
        self.assertEqual(record["successor_head"], NEW_HEAD)
        self.assertEqual(record["successor_identity_sha256"], digest(new_identity))
        self.assertEqual(record["reason"], "New head passed")
        self.assertEqual(record["decision_ref"], "issue-57")
        self.assertEqual(record["actor"], "auditor")
        self.assertTrue(record["recorded_at"])
        self.assertEqual(record["original_ci_sha256"], before["ci"])
        self.assertEqual(self.metadata("zz-citriage")["agentflow"]["supersession"], record)
        self.assertEqual(self.state["issues"]["zz-ci-old"]["status"], "closed")
        self.assertEqual(self.state["issues"]["zz-citriage"]["status"], "closed")
        closes = [call[2] for call in self.state["calls"] if call[:2] == ["bd", "close"]]
        self.assertEqual(closes, ["zz-citriage", "zz-ci-old"])
        self.assertEqual(self.state["readbacks"], {"zz-citriage": 1, "zz-ci-old": 1})
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(any(call[1] == "create" for call in self.state["calls"]))
        updated = sorted(call[2] for call in self.state["calls"] if call[:2] == ["bd", "update"])
        self.assertEqual(updated, ["zz-ci-old", "zz-citriage"])

        audit = self.status()["completion_audit"]
        self.assertTrue(audit["available"])
        self.assertTrue(audit["complete"], audit["findings"])
        gate = next(item for item in audit["ci"] if item["id"] == "zz-ci-old")
        self.assertEqual(
            (gate["observed_result"], gate["disposition"], gate["head"]),
            ("failed", "superseded", OLD_HEAD),
        )
        self.assertEqual(gate["supersession"]["successor"], "wf-ci-new")
        superseded = [item for item in audit["findings"] if item["id"] == "superseded"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(
            (superseded[0]["severity"], superseded[0]["task"], superseded[0]["observed_result"],
             superseded[0]["head"]),
            ("info", "zz-ci-old", "failed", OLD_HEAD),
        )

    def test_identical_replay_makes_zero_writes(self):
        first, _ = self.supersede()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        snapshot = json.dumps(self.state["issues"], sort_keys=True)
        replay, payload = self.supersede(actor="")
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["disposition"], "superseded")
        self.assertEqual(self.writes(), [])
        self.assertEqual(json.dumps(self.state["issues"], sort_keys=True), snapshot)
        self.assertEqual(self.gh_calls(), [])

    def test_conflicting_resupersession_is_rejected_without_writes(self):
        first, _ = self.supersede()
        self.assertEqual(first.returncode, 0)
        third = ci_record("wf-ci-third", "wf-root.1", THIRD_HEAD, "passed", disposition="closed", obligation="accepted")
        self.state["issues"]["wf-ci-third"] = issue(
            "wf-ci-third", "closed", "wf-root.1", labels=["af:stage:ci"],
            metadata={"agentflow": {"stage": "ci", "ci": third}},
        )
        snapshot = json.dumps(self.state["issues"], sort_keys=True)
        for overrides in ({"reason": "A different reason"}, {"successor": "wf-ci-third"},
                          {"decision_ref": "other-decision"}):
            with self.subTest(overrides):
                result, payload = self.supersede(**overrides)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(self.writes(), [])
                self.assertEqual(json.dumps(self.state["issues"], sort_keys=True), snapshot)

    def test_rejection_matrix_makes_zero_writes(self):
        def mutate_new(**fields):
            def apply(state):
                state["issues"]["wf-ci-new"]["metadata"]["agentflow"]["ci"].update(fields)
            return apply

        def mutate_new_identity(**fields):
            def apply(state):
                state["issues"]["wf-ci-new"]["metadata"]["agentflow"]["ci"]["identity"].update(fields)
            return apply

        def mutate_old(**fields):
            def apply(state):
                state["issues"]["zz-ci-old"]["metadata"]["agentflow"]["ci"].update(fields)
            return apply

        def successor_checks(*buckets):
            # Self-consistent digest, so only the bucket evidence is wrong.
            def apply(state):
                ci = state["issues"]["wf-ci-new"]["metadata"]["agentflow"]["ci"]
                checks = [
                    dict(ci["checks"][0], bucket=bucket, id=f"run-{index}")
                    for index, bucket in enumerate(buckets)
                ]
                ci["checks"] = checks
                ci["identity"]["check_set_sha256"] = digest(checks)
            return apply

        def drop_new_identity(field):
            def apply(state):
                state["issues"]["wf-ci-new"]["metadata"]["agentflow"]["ci"]["identity"].pop(field)
            return apply

        def set_status(identifier, status):
            def apply(state):
                state["issues"][identifier]["status"] = status
            return apply

        def outside_successor(state):
            state["issues"]["wf-ci-new"].pop("parent")
            state["issues"]["wf-ci-new"]["dependencies"] = []

        def outside_old(state):
            state["issues"]["zz-ci-old"]["parent"] = "other-root"
            state["issues"]["zz-ci-old"]["metadata"]["agentflow"]["ci"]["identity"]["parent"] = "other-root"

        def no_ci(state):
            state["issues"]["zz-ci-old"]["metadata"]["agentflow"].pop("ci")

        def wrong_triage(state):
            state["issues"]["zz-citriage"]["metadata"]["agentflow"]["ci_bead"] = "other-gate"

        def list_failure(state):
            state["list_mode"] = "fail"

        cases = {
            "successor-outside-root": (outside_successor, {}),
            "old-outside-root": (outside_old, {}),
            "old-not-ci": (no_ci, {}),
            "old-passed": (mutate_old(status="passed", observed_result="passed"), {}),
            "successor-open": (set_status("wf-ci-new", "open"), {}),
            "successor-failed": (mutate_new(status="failed", observed_result="failed"), {}),
            "successor-status-mismatch": (mutate_new(status="failed"), {}),
            "successor-disposition-open": (mutate_new(disposition="open"), {}),
            "successor-obligation-required": (mutate_new(obligation="required"), {}),
            "successor-gate-mismatch": (mutate_new_identity(gate="zz-ci-old"), {}),
            "successor-parent-mismatch": (mutate_new_identity(parent="wf-root"), {}),
            "different-repo": (mutate_new_identity(repo="other/project"), {}),
            "same-head": (mutate_new_identity(head=OLD_HEAD), {}),
            "successor-empty-checks": (successor_checks(), {}),
            "successor-failing-check": (successor_checks("pass", "fail"), {}),
            "successor-cancelled-check": (successor_checks("cancel"), {}),
            "successor-pending-check": (successor_checks("pass", "pending"), {}),
            "successor-check-digest-mismatch": (mutate_new_identity(check_set_sha256="0" * 64), {}),
            "successor-missing-check-set": (drop_new_identity("check_set_sha256"), {}),
            "successor-missing-pr": (drop_new_identity("pr"), {}),
            "successor-required-only-not-bool": (mutate_new_identity(required_only="false"), {}),
            "empty-reason": (None, {"reason": "  "}),
            "empty-decision-ref": (None, {"decision_ref": ""}),
            "self-successor": (None, {"successor": "zz-ci-old"}),
            "unknown-successor": (None, {"successor": "nope-1"}),
            "triage-identity-mismatch": (wrong_triage, {}),
            "enumeration-unavailable": (list_failure, {}),
        }
        for name, (prepare, overrides) in cases.items():
            with self.subTest(name):
                self.state = {"calls": [], "issues": graph()}
                if prepare:
                    prepare(self.state)
                before = json.dumps(self.state["issues"], sort_keys=True)
                result, payload = self.supersede(**overrides)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(payload["ok"])
                self.assertTrue(payload["error"])
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.gh_calls(), [])
                self.assertEqual(json.dumps(self.state["issues"], sort_keys=True), before)

    def test_ambiguous_gate_close_committed_is_confirmed_by_readback(self):
        self.state["close_mode"] = {"zz-ci-old": "committed-error"}
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["disposition"], "superseded")
        self.assertEqual(self.state["readbacks"]["zz-ci-old"], 1)
        self.assertEqual(self.state["closed_attempts"].count("zz-ci-old"), 1)

    def test_ambiguous_gate_close_left_open_is_unresolved_then_resumes(self):
        self.state["close_mode"] = {"zz-ci-old": "open-error"}
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["disposition"], "closure_unresolved")
        self.assertEqual(payload["unresolved"], ["zz-ci-old"])
        self.assertEqual(self.state["readbacks"]["zz-ci-old"], 1)
        self.assertEqual(self.state["closed_attempts"].count("zz-ci-old"), 1)
        self.assertEqual(self.state["issues"]["zz-ci-old"]["status"], "open")
        self.assertEqual(self.state["issues"]["zz-citriage"]["status"], "closed")
        audit = self.status()["completion_audit"]
        self.assertFalse(audit["complete"])
        gate = next(item for item in audit["ci"] if item["id"] == "zz-ci-old")
        self.assertEqual(gate["disposition"], "closure_unresolved")
        self.assertIn("zz-ci-old", [item["id"] for item in audit["open_descendants"]])
        self.assertEqual(
            [item for item in audit["findings"]
             if item["id"] == "superseded" and item["task"] == "zz-ci-old"],
            [],
        )
        self.assertEqual(audit["counts"]["superseded"], 0)

        self.state["close_mode"] = {}
        self.state["closed_attempts"] = []
        self.state["readbacks"] = {}
        resumed, payload = self.supersede()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(payload["disposition"], "superseded")
        self.assertEqual(
            [call[2] for call in self.writes()], ["zz-ci-old"],
        )
        self.assertEqual(self.writes()[0][1], "close")

    def test_unconfirmed_triage_close_stops_before_gate(self):
        self.state["close_mode"] = {"zz-citriage": "open-error"}
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(payload["disposition"], "closure_unresolved")
        self.assertIn("zz-citriage", payload["unresolved"])
        self.assertEqual(self.state["closed_attempts"], ["zz-citriage"])
        self.assertEqual(self.state["readbacks"], {"zz-citriage": 1})

    def test_unreadable_readback_is_unresolved(self):
        # Reads before the close succeed; only the post-close read-back fails.
        patched = FAKE.replace(
            'if key in state.get("show_fail", []) or key not in issues:',
            'if key in state.get("closed_attempts", []) or key not in issues:',
        )
        (self.bin / "bd").write_text(patched.replace("__PYTHON__", sys.executable))
        self.state["close_mode"] = {"zz-citriage": "committed-error"}
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(payload["disposition"], "closure_unresolved")
        self.assertIn("zz-citriage", payload["unresolved"])
        self.assertEqual(self.state["closed_attempts"], ["zz-citriage"])
        self.assertEqual(self.state["readbacks"], {"zz-citriage": 1})

    def test_audit_rejects_supersession_whose_successor_receipt_lost_passing_evidence(self):
        first, _ = self.supersede()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        ci = self.metadata("wf-ci-new")["agentflow"]["ci"]
        # Identity (and so the recorded identity digest) is unchanged; only evidence fails.
        ci["checks"] = [dict(ci["checks"][0], bucket="fail")]
        audit = self.status()["completion_audit"]
        self.assertFalse(audit["complete"])
        gate = next(item for item in audit["ci"] if item["id"] == "zz-ci-old")
        self.assertEqual(gate["disposition"], "closed")
        self.assertFalse(gate["supersession"]["valid"])
        unaccepted = [item for item in audit["findings"]
                      if item["id"] == "ci-result-unaccepted" and item["task"] == "zz-ci-old"]
        self.assertEqual(len(unaccepted), 1)
        self.assertEqual(unaccepted[0]["severity"], "high")
        self.assertEqual(audit["counts"]["superseded"], 0)

    def assert_rejected_without_writes(self, result, payload):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.gh_calls(), [])

    def test_old_ci_written_after_validation_is_not_overwritten(self):
        newer = "2026-10-30T01:00:00+00:00"
        self.hook("show:zz-ci-old", 1, (("zz-ci-old", "metadata", "agentflow", "ci", "observed_at"), newer))
        result, payload = self.supersede()
        self.assert_rejected_without_writes(result, payload)
        self.assertEqual(self.metadata("zz-ci-old")["agentflow"]["ci"]["observed_at"], newer)
        self.assertNotIn("supersession", self.metadata("zz-ci-old")["agentflow"])
        self.assertEqual(self.state["issues"]["zz-ci-old"]["status"], "open")

    def test_old_gate_reparented_after_validation_is_rejected(self):
        self.hook("show:zz-ci-old", 1, (("zz-ci-old", "parent"), "other-root"))
        result, payload = self.supersede()
        self.assert_rejected_without_writes(result, payload)
        self.assertEqual(self.state["issues"]["zz-ci-old"]["parent"], "other-root")
        self.assertNotIn("supersession", self.metadata("zz-ci-old")["agentflow"])

    def test_successor_failing_after_validation_is_rejected(self):
        ci = ("wf-ci-new", "metadata", "agentflow", "ci")
        self.hook("show:wf-ci-new", 1, ((*ci, "status"), "failed"), ((*ci, "observed_result"), "failed"))
        result, payload = self.supersede()
        self.assert_rejected_without_writes(result, payload)
        self.assertEqual(self.metadata("wf-ci-new")["agentflow"]["ci"]["observed_result"], "failed")
        self.assertNotIn("supersession", self.metadata("zz-ci-old")["agentflow"])

    def test_triage_identity_changed_after_validation_is_not_overwritten(self):
        changed = {"gate": "zz-ci-old", "check_set_sha256": "1" * 64}
        self.hook("show:zz-citriage", 1, (("zz-citriage", "metadata", "agentflow", "failure_identity"), changed))
        result, payload = self.supersede()
        self.assert_rejected_without_writes(result, payload)
        self.assertEqual(self.metadata("zz-citriage")["agentflow"]["failure_identity"], changed)
        self.assertEqual(self.state["issues"]["zz-citriage"]["status"], "open")

    def test_old_ci_drift_after_first_write_leaves_gate_open_and_unresolved(self):
        newer = "2026-10-30T02:00:00+00:00"
        self.hook("update:zz-ci-old", 1, (("zz-ci-old", "metadata", "agentflow", "ci", "observed_at"), newer))
        result, payload = self.supersede()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["disposition"], "closure_unresolved")
        self.assertIn("zz-ci-old", payload["unresolved"])
        self.assertNotIn("zz-ci-old", self.state.get("closed_attempts", []))
        self.assertEqual(self.state["issues"]["zz-ci-old"]["status"], "open")
        self.assertEqual(self.metadata("zz-ci-old")["agentflow"]["ci"]["observed_at"], newer)
        self.assertEqual(
            [call[2] for call in self.state["calls"] if call[:2] == ["bd", "update"]].count("zz-ci-old"), 1,
        )
        self.assertEqual(self.gh_calls(), [])


class TerminalRotationTests(CompletionFixture):
    """V2d: terminal and active rotation packets."""

    def setUp(self):
        super().setUp()
        with mock.patch.dict(os.environ, {"AGENTFLOW_STATE_HOME": str(self.state_home)}):
            args = cli.build_parser().parse_args([
                "controller", "status", "--root", str(self.workspace),
                "--workflow-root", "wf-root", "--json",
            ])
            controller, _ = cli._controller_instance(args)
            checkpoint_backend.write_checkpoint(controller.checkpoint_path, {
                "task": "zz-ci-old", "phase": "ci", "next_action": "Observe CI for PR 42",
            })

    def rotate(self):
        result = self.run_cli(
            "controller", "rotate", "--root", str(self.workspace),
            "--workflow-root", "wf-root", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        packet = json.loads(Path(payload["packet"]).read_text(encoding="utf-8"))
        return payload, packet

    def close_everything(self):
        _, payload = self.supersede()
        self.assertTrue(payload["ok"], payload)

    def common(self, packet):
        self.assertEqual(packet["session"]["generation"], 1)
        return {"generation": 1, "rotation": packet["session"]["rotation"]}

    def test_terminal_root_with_complete_audit_writes_terminal_packet(self):
        self.close_everything()
        self.state["ready"] = [{"id": "ext-1", "status": "open"}]
        payload, packet = self.rotate()
        expected = {
            "schema": PACKET_SCHEMA,
            "created_at": "<ts>",
            "workspace_root": "<workspace_root>",
            "workflow_root": "wf-root",
            "controller": "agentflow-controller",
            "continuity_id": "<continuity_id>",
            "terminal": True,
            "checkpoint": {"task": "", "phase": "", "state": "complete", "next_action": ""},
            "historical_checkpoint": {
                "task": "zz-ci-old", "phase": "ci", "state": "idle",
                "next_action": "Observe CI for PR 42", "historical": True,
            },
            "next_actions": [],
            "obligations": [],
            "ready_tasks": [],
            "completion": {
                "schema": AUDIT_SCHEMA, "available": True, "complete": True,
                "root": {"id": "wf-root", "status": "closed"},
                "counts": {
                    "descendants": 4, "open": 0, "terminal": 4, "ci_gates": 2,
                    "superseded": 1, "high_findings": 0, "info_findings": 2,
                },
                "error": "",
            },
            "session": self.common(packet),
            "transcript_included": False,
            "instructions": [
                "This root is complete; do not resume a controller, relaunch workers, or create tasks for it.",
                "Treat the historical checkpoint as context only, not as a next action.",
                "Start a new approved goal for any further work.",
            ],
        }
        self.assertEqual(normalize(packet), expected)
        prompt = payload["resume_prompt"]
        self.assertIn("wf-root", prompt)
        self.assertIn("complete", prompt)
        self.assertIn("nothing to continue", prompt)
        self.assertNotIn("continue the persistent controller", prompt)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.writes(), [])

    def test_open_obligations_keep_an_active_packet(self):
        self.state["ready"] = [
            {"id": "zz-citriage", "status": "open", "labels": ["af:stage:ci-triage"]},
            {"id": "ext-1", "status": "open"},
        ]
        payload, packet = self.rotate()
        expected = {
            "schema": PACKET_SCHEMA,
            "created_at": "<ts>",
            "workspace_root": "<workspace_root>",
            "workflow_root": "wf-root",
            "controller": "agentflow-controller",
            "continuity_id": "<continuity_id>",
            "terminal": False,
            "checkpoint": {
                "task": "zz-ci-old", "phase": "ci", "state": "idle",
                "next_action": "Observe CI for PR 42",
            },
            "next_actions": ["Observe CI for PR 42"],
            "obligations": [
                {"kind": "ci-result-unaccepted", "task": "zz-ci-old"},
                {"kind": "open-under-terminal-ancestor", "task": "zz-ci-old"},
                {"kind": "open-under-terminal-ancestor", "task": "zz-citriage"},
            ],
            "ready_tasks": [{"id": "zz-citriage", "status": "open", "stage": "ci-triage"}],
            "completion": {
                "schema": AUDIT_SCHEMA, "available": True, "complete": False,
                "root": {"id": "wf-root", "status": "closed"},
                "counts": {
                    "descendants": 4, "open": 2, "terminal": 2, "ci_gates": 2,
                    "superseded": 0, "high_findings": 3, "info_findings": 1,
                },
                "error": "",
            },
            "session": self.common(packet),
            "transcript_included": False,
            "instructions": [
                "Resume this exact root; do not create a replacement root or duplicate tasks.",
                "Read Beads, controller state, and linked files instead of prior chat transcripts.",
                "Preserve the root's model policy, permissions, acceptance, and halt conditions.",
            ],
        }
        self.assertEqual(normalize(packet), expected)
        self.assertIn("continue the persistent controller", payload["resume_prompt"])
        self.assertIn("3 open obligation", payload["resume_prompt"])

    def test_unavailable_audit_keeps_an_explicit_active_packet(self):
        for row in self.state["issues"].values():
            row["status"] = "closed"
        self.state["list_mode"] = "fail"
        payload, packet = self.rotate()
        self.assertFalse(packet["terminal"])
        self.assertEqual(packet["checkpoint"]["next_action"], "Observe CI for PR 42")
        self.assertNotIn("historical_checkpoint", packet)
        self.assertFalse(packet["completion"]["available"])
        self.assertFalse(packet["completion"]["complete"])
        self.assertIsNone(packet["completion"]["counts"])
        self.assertEqual(packet["obligations"][0]["kind"], "completion-audit-unavailable")
        self.assertEqual(packet["obligations"][0]["task"], "wf-root")
        self.assertIn("continue the persistent controller", payload["resume_prompt"])
        self.assertIn("completion audit is unavailable", payload["resume_prompt"])

    def test_open_root_with_closed_work_stays_active(self):
        self.close_everything()
        self.state["issues"]["wf-root"]["status"] = "open"
        payload, packet = self.rotate()
        self.assertFalse(packet["terminal"])
        self.assertTrue(packet["completion"]["complete"])
        self.assertEqual(packet["obligations"], [
            {"kind": "root-not-terminal", "task": "wf-root", "status": "open"},
        ])
        self.assertEqual(packet["checkpoint"]["next_action"], "Observe CI for PR 42")
        self.assertIn("continue the persistent controller", payload["resume_prompt"])


if __name__ == "__main__":
    unittest.main()
