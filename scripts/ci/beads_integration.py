#!/usr/bin/env python3
"""Real Beads adapter/supervision checks in disposable, explicitly selected stores."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

from agentflow import beads, cli, controller as controllers


def command(cwd: Path, *args: str, binary: str = "bd", check: bool = True, select_workspace: bool = True):
    env = os.environ.copy()
    # Do not inherit a caller's workspace, server, remote, cap or schema bypass.
    for name in tuple(env):
        if name.startswith(("BEADS_", "BD_")):
            env.pop(name)
    env.update(BD_NON_INTERACTIVE="1", DOLT_DISABLE_EVENT_FLUSH="1")
    if (cwd / ".integration-server-port").is_file():
        env.update(BEADS_DOLT_SERVER_HOST="127.0.0.1",
                   BEADS_DOLT_SERVER_PORT=(cwd / ".integration-server-port").read_text(),
                   BEADS_DOLT_AUTO_START="0")
    if select_workspace:
        env["BEADS_DIR"] = str(cwd / ".beads")
    result = subprocess.run([binary, *args], cwd=cwd, env=env, text=True, capture_output=True, timeout=180, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{Path(binary).name} {args[0]} failed: {result.stderr or result.stdout}")
    return result


def create_root(cwd: Path, title: str) -> str:
    return json.loads(command(cwd, "create", title, "--type", "epic", "--json").stdout)["id"]


@contextmanager
def selected_workspace(cwd: Path):
    names = ("BEADS_DIR", "BEADS_DOLT_SERVER_HOST", "BEADS_DOLT_SERVER_PORT", "BEADS_DOLT_AUTO_START")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["BEADS_DIR"] = str(cwd / ".beads")
    if (cwd / ".integration-server-port").is_file():
        os.environ.update(BEADS_DOLT_SERVER_HOST="127.0.0.1",
                          BEADS_DOLT_SERVER_PORT=(cwd / ".integration-server-port").read_text(),
                          BEADS_DOLT_AUTO_START="0")
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def exercise(cwd: Path, *, git: bool, port: int = 0) -> None:
    cwd.mkdir()
    (cwd / ".beads").mkdir()
    if git:
        subprocess.run(["git", "init", "--quiet", str(cwd)], check=True)
    if port:
        (cwd / ".integration-server-port").write_text(str(port))
        initialized = command(cwd, "init", "--server", "--shared-server", "--external", "--server-host", "127.0.0.1", "--server-port", str(port), "--non-interactive", "--skip-hooks", "--skip-agents", "--prefix", cwd.name, select_workspace=False)
        if not (cwd / ".beads/metadata.json").is_file():
            raise RuntimeError(f"shared init did not persist workspace metadata: {initialized.stdout} {initialized.stderr}")
    else:
        beads.initialize(cwd, mode="embedded", stealth=False, prefix=cwd.name, gitless=not git)
    with selected_workspace(cwd):
        root = create_root(cwd, "Controlled workflow")
        other = create_root(cwd, "Unrelated workflow")
        def child(title, parent=root, dependencies=None):
            return beads.create_issue(cwd, title=title, description="Full execution contract", acceptance="Integration evidence recorded", parent=parent, labels=["af:stage:implement"], dependencies=dependencies)
        task = child("Implement")
        review = child("Review", dependencies=[task["id"]])
        unrelated = child("Unrelated", parent=other)
        assert unrelated["id"] not in {row["id"] for row in beads.root_descendants(cwd, root)}
        selected = beads.claim_ready(cwd, parent=root, labels=["af:stage:implement"], actor="integration")
        assert selected and selected["id"] == task["id"] and selected["description"] == "Full execution contract"
        claim = beads.claim_issue_exact(cwd, task["id"], root=root, actor="integration", persist=True)
        beads.heartbeat_issue(cwd, task["id"], actor="integration")
        try:
            beads.heartbeat_issue(cwd, task["id"], actor="intruder")
        except beads.BeadsError:
            pass
        else:
            raise AssertionError("wrong-actor heartbeat succeeded")
        assert beads.claim_ready(cwd, parent=root, labels=[], actor="integration") is None, "review escaped dependency blocking"
        cli._persist_claim_identity(cwd, claim)
        controller = controllers.RootController(str(cwd), "integration", state_path=cli._controller_state_dir(cwd, root) / "state.json")
        lease = controller.acquire()
        controller.reserve_active_task({"task": task["id"], "root": str(cwd), "actor": "integration", "claim_id": claim.claim_id}, lease=lease)
        assert cli._renew_native_task_leases(controller, cwd, root) == []
        # Restart reads the durable task/actor rather than assuming a new owner.
        restarted = controllers.RootController(str(cwd), "integration", state_path=controller.state_path)
        assert cli._renew_native_task_leases(restarted, cwd, root) == []
        # This explicit reclaimer is confined to a disposable test store. It is
        # deliberately absent from Agentflow runtime/supervision code.
        if port:
            command(cwd, "sql", f"UPDATE leases SET lease_expires_at = '2000-01-01 00:00:00' WHERE issue_id = '{task['id']}'")
            command(cwd, "reclaim", "--id", task["id"], "--older-than", "0s", "--json")
        else:
            # bd sql deliberately rejects embedded mode. Exercise native
            # ownership loss here; the server lane exercises actual expiry.
            command(cwd, "update", task["id"], "--status", "open", "--actor", "integration")
        assert cli._renew_native_task_leases(restarted, cwd, root) == [task["id"]]
        try:
            cli._assert_native_claim_unfenced(cwd, root, task["id"])
        except ValueError:
            pass
        else:
            raise AssertionError("reclaimed task retained result authority")
        beads.close_issue(cwd, task["id"], "Integration implementation accepted", actor="integration")
        selected = beads.claim_ready(cwd, parent=root, labels=[], actor="reviewer")
        assert selected and selected["id"] == review["id"]
        beads.close_issue(cwd, review["id"], "Integration review accepted", actor="reviewer")
        assert all(row["status"] == "closed" for row in beads.root_descendants(cwd, root))
        # A new native claim by the same actor must not revive an old opaque
        # capability, even when both claims share a second-resolution timestamp.
        generation_task = child("Same actor generation")
        generation_claim = beads.claim_issue_exact(cwd, generation_task["id"], root=root, actor="integration", persist=True)
        cli._persist_claim_identity(cwd, generation_claim)
        first_generation = beads.claim_generation(cwd, generation_task["id"])
        command(cwd, "comments", "add", generation_task["id"], "Unrelated metadata remains safe")
        beads.claim_issue_exact(cwd, generation_task["id"], root=root, actor="integration")
        beads.heartbeat_issue(cwd, generation_task["id"], actor="integration")
        assert beads.claim_generation(cwd, generation_task["id"]) == first_generation
        cli._verify_launch_authority(cwd, generation_task["id"], generation_claim.identity.token,
                                     lease.token, workflow_root=root, actor="integration")
        if port:
            command(cwd, "sql", f"UPDATE leases SET lease_expires_at = '2000-01-01 00:00:00' WHERE issue_id = '{generation_task['id']}'")
            command(cwd, "reclaim", "--id", generation_task["id"], "--older-than", "0s", "--json")
        else:
            command(cwd, "update", generation_task["id"], "--status", "open", "--actor", "integration")
        beads.claim_issue_exact(cwd, generation_task["id"], root=root, actor="integration")
        assert beads.claim_generation(cwd, generation_task["id"]) != first_generation
        # Native actor-only heartbeat succeeds; Agentflow must still reject.
        beads.heartbeat_issue(cwd, generation_task["id"], actor="integration")
        try:
            cli._verify_launch_authority(cwd, generation_task["id"], generation_claim.identity.token,
                                         lease.token, workflow_root=root, actor="integration")
        except ValueError as exc:
            assert "generation" in str(exc)
        else:
            raise AssertionError("same-actor regrant revived old authority")
        assert cli._native_claim_fence_path(cwd, root, generation_task["id"]).is_file()
        beads.close_issue(cwd, generation_task["id"], "Generation regression completed", actor="integration")
        assert beads.get_issue(cwd, unrelated["id"])["status"] == "open"
        # Two independent exact claimers must not both own the same task.
        race = child("Claim race")
        def contender(actor):
            try:
                return beads.claim_issue_exact(cwd, race["id"], root=root, actor=actor).actor
            except beads.ClaimConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [value for value in pool.map(contender, ("one", "two")) if value]
        assert len(winners) == 1, winners
        controller.release(lease)
        print(f"PASS {'shared-server' if port else 'embedded'} {'Git' if git else 'gitless'}: discovery, claim race, renewal, {'reclaim' if port else 'claim-loss'}/fence, same-actor generation, restart, review graph")


def upgrade_fixture(base: Path, old_binary: str) -> None:
    source = base / "legacy-source"
    source.mkdir()
    (source / ".beads").mkdir()
    command(source, "init", "--non-interactive", "--skip-hooks", "--skip-agents", "--prefix", "legacy", binary=old_binary)
    root = json.loads(command(source, "create", "Legacy root", "--type", "epic", "--json", binary=old_binary).stdout)["id"]
    task = json.loads(command(source, "create", "Legacy task", "--parent", root, "--description", "Preserve this contract", "--json", binary=old_binary).stdout)["id"]
    dependent = json.loads(command(source, "create", "Legacy review", "--parent", root, "--deps", task, "--json", binary=old_binary).stdout)["id"]
    command(source, "comments", "add", task, "Preserve this comment", binary=old_binary)
    before = json.loads(command(source, "show", task, "--json", binary=old_binary).stdout)
    before_ids = {row["id"] for row in json.loads(command(source, "list", "--all", "--limit", "0", "--json", binary=old_binary).stdout)}
    before_ready = {row["id"] for row in json.loads(command(source, "ready", "--parent", root, "--limit", "0", "--json", binary=old_binary).stdout)}
    def manifest(directory):
        return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()}
    # No current binary opens the source. Seal a cold copy after old-binary
    # processes exit, restore it, and migrate only that restored copy.
    sealed = base / "sealed-legacy"
    restored = base / "restored-legacy"
    shutil.copytree(source, sealed)
    sealed_manifest = manifest(sealed)
    assert manifest(source) == sealed_manifest
    shutil.copytree(sealed, restored)
    assert manifest(restored) == sealed_manifest
    command(restored, "migrate", "schema")
    after = json.loads(command(restored, "show", task, "--json").stdout)
    assert before[0]["description"] == after[0]["description"]
    assert before[0]["parent"] == after[0]["parent"] == root
    assert "Preserve this comment" in command(restored, "comments", task, "--json").stdout
    assert {row["id"] for row in json.loads(command(restored, "list", "--all", "--limit", "0", "--json").stdout)} == before_ids == {root, task, dependent}
    assert {row["id"] for row in json.loads(command(restored, "ready", "--parent", root, "--limit", "0", "--json").stdout)} == before_ready == {task}
    assert task in command(restored, "show", dependent, "--json").stdout
    assert (sealed / ".beads/.local_version").read_text().strip() == "1.1.0"
    assert manifest(sealed) == sealed_manifest == manifest(source)
    print("PASS sealed 1.1.0 store: verified cold restore, 1.3.1 schema migration, ID/count/contract/parent/dependency/readiness/comment preservation")


def main() -> None:
    if os.environ.get("AGENTFLOW_INTEGRATION") != "1":
        raise SystemExit("Set AGENTFLOW_INTEGRATION=1 to run disposable real integration")
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared", action="store_true")
    parser.add_argument("--shared-only", action="store_true", help="Run only the isolated server lane while diagnosing it")
    parser.add_argument("--old-bd", default="")
    args = parser.parse_args()
    beads.require_supported_version()
    with tempfile.TemporaryDirectory(prefix="agentflow-beads-131-") as tmp:
        base = Path(tmp).resolve()
        # Authority fences and ledgers must also be disposable.
        os.environ["AGENTFLOW_STATE_HOME"] = str(base / "controller-state")
        for name in tuple(os.environ):
            if name.startswith(("BEADS_", "BD_")):
                os.environ.pop(name)
        if not args.shared_only:
            for git in (True, False):
                exercise(base / ("git" if git else "gitless"), git=git)
            if args.old_bd:
                upgrade_fixture(base, str(Path(args.old_bd).resolve()))
        if args.shared or args.shared_only:
            data = base / "server"
            data.mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            with (base / "dolt.log").open("w") as log:
                server = subprocess.Popen(["dolt", "sql-server", "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(data)], cwd=data, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError((base / "dolt.log").read_text())
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=.1):
                                break
                        except OSError:
                            time.sleep(.1)
                    else:
                        raise RuntimeError("isolated Dolt server did not become ready")
                    exercise(base / "sharedgit", git=True, port=port)
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)


if __name__ == "__main__":
    main()
