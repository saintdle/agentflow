"""Read-only capability probes for the CLI interfaces Agentflow actually uses."""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any


class CliCompatibilityError(ValueError):
    """The selected executable cannot implement the required launch contract."""


PROVIDER_FLAGS = {
    "codex": ("--model", "--config"),
    "claude": ("--model", "--effort", "--fallback-model"),
    "copilot": ("--model", "--reasoning-effort", "--interactive"),
}
HERDR_COMMANDS = (
    (("workspace", "create"), ("--cwd", "--env", "--no-focus", "--label")),
    (("pane", "run"), ("PANE_ID", "COMMAND")),
    (("agent", "get"), ("target",)),
)


def _read(executable: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliCompatibilityError(
            f"cannot probe {executable} {' '.join(arguments)}: {type(exc).__name__}"
        ) from exc
    if result.returncode:
        raise CliCompatibilityError(
            f"CLI probe failed: {executable} {' '.join(arguments)} (exit {result.returncode})"
        )
    return result.stdout


def _require_tokens(text: str, tokens: tuple[str, ...], command: str) -> None:
    missing = [token for token in tokens if not re.search(
        r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text,
    )]
    if missing:
        raise CliCompatibilityError(f"{command} lacks required capabilities: {', '.join(missing)}")


def probe_provider(provider: str, executable: str) -> dict[str, Any]:
    if provider not in PROVIDER_FLAGS:
        raise CliCompatibilityError(f"unsupported provider: {provider}")
    version = _read(executable, "--version").strip().splitlines()
    _require_tokens(_read(executable, "--help"), PROVIDER_FLAGS[provider], provider)
    return {"executable": executable, "version": version[0] if version else "", "compatible": True}


def probe_herdr(executable: str) -> dict[str, Any]:
    version = _read(executable, "--version").strip()
    for command, tokens in HERDR_COMMANDS:
        _require_tokens(_read(executable, *command, "--help"), tokens, "herdr " + " ".join(command))
    try:
        schema = json.loads(_read(executable, "api", "schema", "--json"))
        protocol = schema["protocol"]
        definitions = schema["schemas"]["request"]["$defs"]
        for name, fields in (
            ("WorkspaceCreateParams", {"cwd", "env", "focus", "label"}),
            ("PaneSendInputParams", {"pane_id", "text", "keys"}),
        ):
            if not fields <= definitions[name]["properties"].keys():
                raise CliCompatibilityError(f"Herdr schema lacks required {name} fields")
        if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 22:
            raise CliCompatibilityError("Herdr protocol 22 or later is required")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, CliCompatibilityError):
            raise
        raise CliCompatibilityError("Herdr returned an invalid capability schema") from exc
    return {"executable": executable, "version": version, "protocol": protocol, "compatible": True}
