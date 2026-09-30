#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow import assets as assets_backend
from agentflow import model_policy
from agentflow import project_config
from sync_resources import check_resources


ROOT = Path(__file__).resolve().parents[1]
ADAPTED_SKILLS = ("code-review", "diagnosing-bugs", "to-tickets", "wayfinder")
MIT_NOTICE_MARKERS = (
    "MIT License",
    "Copyright (c) 2026 Matt Pocock",
    "Permission is hereby granted, free of charge",
    "THE SOFTWARE IS PROVIDED \"AS IS\"",
)
PROVENANCE_MARKERS = (
    "https://github.com/mattpocock/skills",
    "Upstream license: MIT",
    "Relationship: adapted",
    "Apache-2.0",
)


def _looks_like_asset_lock(data: object) -> bool:
    return isinstance(data, dict) and "assets" in data and isinstance(data.get("assets"), list)


def _looks_like_model_policy(data: object) -> bool:
    return isinstance(data, dict) and data.get("schema") == model_policy.SCHEMA


def main() -> int:
    errors: list[str] = []
    errors.extend(check_resources(ROOT))

    packaged_skills = ROOT / "src/agentflow/resources/skills"
    for name in ADAPTED_SKILLS:
        skill = packaged_skills / name
        notice = skill / "THIRD_PARTY_NOTICES.md"
        provenance = skill / "PROVENANCE.md"
        for path, markers in (
            (notice, MIT_NOTICE_MARKERS),
            (provenance, PROVENANCE_MARKERS),
        ):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{path.relative_to(ROOT)}: {exc}")
                continue
            missing = [marker for marker in markers if marker not in text]
            if missing:
                errors.append(
                    f"{path.relative_to(ROOT)}: incomplete attribution; missing "
                    f"{', '.join(repr(marker) for marker in missing)}"
                )

    for path in sorted(ROOT.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
            continue
        if _looks_like_asset_lock(data):
            for lock_error in assets_backend.validate_lock_data(data):
                errors.append(f"{path.relative_to(ROOT)} (asset lock): {lock_error}")
        if _looks_like_model_policy(data):
            try:
                model_policy.parse_policy(data)
            except model_policy.ModelPolicyError as exc:
                errors.append(f"{path.relative_to(ROOT)} (model policy): {exc}")

    config_template = ROOT / "src/agentflow/resources/templates/project/agentflow.json"
    try:
        config_data = json.loads(config_template.read_text(encoding="utf-8"))
        for config_error in project_config.validate(config_data, ROOT):
            errors.append(f"{config_template.relative_to(ROOT)}: {config_error}")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{config_template.relative_to(ROOT)}: {exc}")

    for path in sorted((ROOT / ".codex/agents").glob("*.toml")):
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")

    for path in sorted((ROOT / "templates/beads/formulas").glob("*.toml")):
        try:
            formula = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
            continue
        if not formula.get("formula") or not formula.get("steps"):
            errors.append(f"{path.relative_to(ROOT)}: formula and steps are required")
        steps = formula.get("steps", [])
        step_ids = {
            str(step.get("id"))
            for step in steps
            if isinstance(step, dict) and step.get("id")
        }
        for step in steps:
            if "## Acceptance criteria" not in str(step.get("description") or ""):
                errors.append(
                    f"{path.relative_to(ROOT)}: step {step.get('id') or '?'} "
                    "requires embedded acceptance criteria"
                )
            labels = step.get("labels", [])
            stage_labels = [
                label for label in labels if str(label).startswith("af:stage:")
            ]
            role_labels = [
                label for label in labels if str(label).startswith("af:role:")
            ]
            if len(stage_labels) != 1:
                errors.append(
                    f"{path.relative_to(ROOT)}: step {step.get('id') or '?'} "
                    "requires exactly one af:stage label"
                )
            if len(role_labels) != 1:
                errors.append(
                    f"{path.relative_to(ROOT)}: step {step.get('id') or '?'} "
                    "requires exactly one af:role label"
                )
            for dependency in step.get("needs", []):
                if dependency not in step_ids:
                    errors.append(
                        f"{path.relative_to(ROOT)}: step {step.get('id') or '?'} "
                        f"depends on unknown step {dependency}"
                    )

    for skill in sorted((ROOT / ".agents/skills").iterdir()):
        if not skill.is_dir():
            continue
        entrypoint = skill / "SKILL.md"
        if not entrypoint.is_file():
            errors.append(f"{skill.relative_to(ROOT)}: missing SKILL.md")
            continue
        text = entrypoint.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\nname:" not in text or "\ndescription:" not in text:
            errors.append(f"{skill.relative_to(ROOT)}: invalid SKILL.md frontmatter")

    for text_file in ROOT.rglob("*.md"):
        if "TODO" in text_file.read_text(encoding="utf-8"):
            errors.append(f"{text_file.relative_to(ROOT)}: unresolved TODO")

    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validated resource exports, JSON, TOML, project configuration, and shared skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
