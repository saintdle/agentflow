#!/usr/bin/env python3
"""Install and exercise a wheel in an isolated environment outside its checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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
        if sum(len(names) for names in profiles.values()) != 12:
            raise RuntimeError(f"expected 12 bundled profiles, got {profiles!r}")
        expected_roles = {
            "agentflow-controller",
            "agentflow-explorer",
            "agentflow-pr-gatekeeper",
            "agentflow-reviewer",
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
        if len(planned) != 34:
            raise RuntimeError(
                f"expected 34 bundled install assets, got {len(planned)}:\n{dry_run.stdout}"
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
    print(f"Clean-wheel smoke passed for {wheel.name}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--expected-version", default="0.0.1")
    args = parser.parse_args()
    run(args.wheel, args.expected_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
