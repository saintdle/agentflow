#!/usr/bin/env python3
"""Install and exercise a wheel in an isolated environment outside its checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


def run(wheel: Path, expected_version: str) -> None:
    wheel = wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="agentflow-wheel-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        executable_dir = "Scripts" if os.name == "nt" else "bin"
        python = environment / executable_dir / ("python.exe" if os.name == "nt" else "python")
        agentflow = environment / executable_dir / ("agentflow.exe" if os.name == "nt" else "agentflow")
        clean_env = os.environ.copy()
        clean_env.pop("PYTHONPATH", None)
        clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
        isolated_home = root / "home"
        isolated_home.mkdir()
        clean_env["HOME"] = str(isolated_home)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", str(wheel)],
            cwd=root,
            env=clean_env,
            check=True,
        )
        version = subprocess.run(
            [str(agentflow), "--version"], cwd=root, env=clean_env, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        expected_output = f"agentflow {expected_version}"
        if version != expected_output:
            raise RuntimeError(f"unexpected CLI version output: {version!r}")
        explicit_state = root / "explicit-state"
        xdg_state = root / "xdg-state"
        artifact_env = clean_env.copy()
        artifact_env["AGENTFLOW_STATE_HOME"] = str(explicit_state)
        artifact_env["XDG_STATE_HOME"] = str(xdg_state)
        privacy_probe = subprocess.run(
            [
                str(python), "-c",
                "import json; from agentflow.events import normalize_event; "
                "from agentflow.memory_runtime import state_home; "
                "e=normalize_event('codex', {'event':'PostToolUseFailure', "
                "'tool_id':'/tmp/top-id', 'tool_name':'/tmp/top-name', "
                "'data':{'sessionId':'nested', 'toolId':'/tmp/nested-id', "
                "'toolName':'/tmp/nested-name', 'error':'permission denied'}}); "
                "print(json.dumps({'home':str(state_home()), 'metadata':dict(e.metadata)}))",
            ],
            cwd=root,
            env=artifact_env,
            text=True,
            capture_output=True,
            check=True,
        )
        privacy_result = json.loads(privacy_probe.stdout)
        if privacy_result["home"] != str(explicit_state):
            raise RuntimeError("installed state-home override did not beat XDG_STATE_HOME")
        metadata_text = json.dumps(privacy_result["metadata"], sort_keys=True)
        if any(raw in metadata_text for raw in ("/tmp/top-id", "/tmp/top-name", "/tmp/nested-id", "/tmp/nested-name", "toolid", "toolname")):
            raise RuntimeError("installed provider alias privacy probe retained raw tool metadata")
        subprocess.run([str(agentflow), "--help"], cwd=root, env=clean_env, check=True)
        manifest_result = subprocess.run(
            [
                str(python), "-c",
                "import json; from agentflow import resources; "
                "print(json.dumps({'skills': resources.names('skills'), "
                "'profiles': {p: resources.names('agents', p) "
                "for p in ('codex', 'claude', 'copilot')}}))",
            ],
            cwd=root,
            env=clean_env,
            text=True,
            capture_output=True,
            check=True,
        )
        manifest = json.loads(manifest_result.stdout)
        expected_skills = {
            "code-review",
            "diagnosing-bugs",
            "gatekeep-prs",
            "orchestrate-agents",
            "shape-goal",
            "to-tickets",
            "wayfinder",
        }
        if set(manifest["skills"]) != expected_skills:
            raise RuntimeError(f"unexpected bundled skills: {manifest['skills']!r}")
        profiles = manifest["profiles"]
        if set(profiles) != {"codex", "claude", "copilot"}:
            raise RuntimeError(f"unexpected bundled profile providers: {sorted(profiles)!r}")
        if sum(len(names) for names in profiles.values()) != 15:
            raise RuntimeError(f"expected 15 bundled profiles, got {profiles!r}")
        expected_roles = {
            "agentflow-controller",
            "agentflow-explorer",
            "agentflow-pr-gatekeeper",
            "agentflow-reviewer",
            "agentflow-worker",
        }
        for provider, names in profiles.items():
            roles = {name.split(".", 1)[0] for name in names}
            if roles != expected_roles:
                raise RuntimeError(f"unexpected {provider} profiles: {names!r}")

        home_before_dry_run = {
            path.relative_to(isolated_home) for path in isolated_home.rglob("*")
        }
        dry_run = subprocess.run(
            [str(agentflow), "install", "--dry-run"],
            cwd=root,
            env=clean_env,
            text=True,
            capture_output=True,
            check=True,
        )
        planned = [line for line in dry_run.stdout.splitlines() if line.startswith("would-install")]
        if len(planned) != 37:
            raise RuntimeError(
                f"expected 37 bundled install assets, got {len(planned)}:\n{dry_run.stdout}"
            )
        for skill in expected_skills:
            if sum(skill in line for line in planned) != 3:
                raise RuntimeError(f"dry run did not plan {skill!r} for all three providers")
        home_after_dry_run = {
            path.relative_to(isolated_home) for path in isolated_home.rglob("*")
        }
        if home_after_dry_run != home_before_dry_run:
            added = sorted(str(path) for path in home_after_dry_run - home_before_dry_run)
            removed = sorted(str(path) for path in home_before_dry_run - home_after_dry_run)
            raise RuntimeError(
                "install --dry-run changed the isolated home: "
                f"added={added!r}, removed={removed!r}"
            )

        # Exercise the packaged migration itself against a synthetic legacy
        # layout before installing the ordinary bundle. The real user home and
        # any project state remain outside this temporary environment.
        legacy = root / "legacy-agentflow"
        legacy_command = legacy / "bin/agentflow"
        legacy_skill = legacy / ".agents/skills/to-tickets"
        legacy_profile = legacy / ".codex/agents/agentflow-controller.toml"
        legacy_hook = legacy / "templates/user/codex-hooks.json"
        for path, payload in (
            (legacy_command, "#!/bin/sh\n"),
            (legacy_skill / "SKILL.md", "legacy\n"),
            (legacy_profile, "legacy\n"),
            (legacy_hook, "{}\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        legacy_command.chmod(0o755)
        legacy_links = {
            isolated_home / ".local/bin/agentflow": legacy_command,
            isolated_home / ".agents/skills/to-tickets": legacy_skill,
            isolated_home / ".codex/agents/agentflow-controller.toml": legacy_profile,
            isolated_home / ".codex/hooks.json": legacy_hook,
        }
        for destination, source in legacy_links.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=source.is_dir())
        migration_base = [
            str(agentflow), "migrate", "legacy", "--from", str(legacy),
            "--new-command", str(agentflow),
        ]
        migration_dry_run = subprocess.run(
            [*migration_base, "--dry-run"], cwd=root, env=clean_env,
            text=True, capture_output=True, check=True,
        )
        if "DRY_RUN: 4 exact legacy-owned integration link(s)" not in migration_dry_run.stdout:
            raise RuntimeError(f"unexpected packaged migration plan: {migration_dry_run.stdout!r}")
        migration_apply = subprocess.run(
            [*migration_base, "--apply"], cwd=root, env=clean_env,
            text=True, capture_output=True, check=True,
        )
        migration_id = next(
            (line.partition(":")[2].strip() for line in migration_apply.stdout.splitlines()
             if line.startswith("Migration ID:")),
            "",
        )
        if not migration_id:
            raise RuntimeError(f"packaged migration did not return an ID: {migration_apply.stdout!r}")
        if (isolated_home / ".agents/skills/to-tickets").is_symlink():
            raise RuntimeError("packaged migration did not replace the legacy skill link")
        subprocess.run(
            [str(agentflow), "migrate", "legacy", "--rollback", migration_id],
            cwd=root, env=clean_env, text=True, capture_output=True, check=True,
        )
        for destination, source in legacy_links.items():
            if not destination.is_symlink() or destination.resolve() != source.resolve():
                raise RuntimeError(f"packaged migration did not restore {destination}")
            destination.unlink()
        shutil.rmtree(legacy)

        subprocess.run(
            [str(agentflow), "install"],
            cwd=root,
            env=clean_env,
            text=True,
            capture_output=True,
            check=True,
        )
        adapted_skills = {"code-review", "diagnosing-bugs", "to-tickets", "wayfinder"}
        for provider_root in (".agents", ".claude", ".copilot"):
            for skill in adapted_skills:
                installed = isolated_home / provider_root / "skills" / skill
                notice = (installed / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
                provenance = (installed / "PROVENANCE.md").read_text(encoding="utf-8")
                if "Copyright (c) 2026 Matt Pocock" not in notice:
                    raise RuntimeError(f"incomplete installed MIT notice: {installed}")
                if "Permission is hereby granted" not in notice:
                    raise RuntimeError(f"incomplete installed MIT permission: {installed}")
                if "https://github.com/mattpocock/skills" not in provenance:
                    raise RuntimeError(f"missing installed upstream provenance: {installed}")
                if "Upstream license: MIT" not in provenance:
                    raise RuntimeError(f"missing installed license provenance: {installed}")
    print(f"Clean-wheel smoke passed for {wheel.name}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--expected-version", default="0.0.7")
    args = parser.parse_args()
    run(args.wheel, args.expected_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
