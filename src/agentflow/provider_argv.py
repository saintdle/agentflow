"""Provider-specific launch argv, built from each installed CLI's real flags.

Captured from installed CLI ``--help`` output (Codex, Claude Code, GitHub
Copilot CLI), not invented: Codex has no ``--effort`` flag at all -- reasoning
effort is a config override (``-c model_reasoning_effort=<level>``) -- while
Claude and Copilot both expose ``--model``/``--effort`` directly. Building one
shared ``--model --effort --json`` template for all three breaks Codex
launches (AFREL-013); each provider gets its own exact argv.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

PROVIDERS = ("codex", "claude", "copilot")


class ProviderArgvError(ValueError):
    """Raised when a provider/model/effort combination cannot produce a launch argv."""


@dataclasses.dataclass(frozen=True)
class ConfinedHandoff:
    """A handoff artifact that has passed the provider launch boundary."""

    path: Path
    manifest: dict[str, object]
    content_sha256: str
    manifest_sha256: str
    preflight_sha256: str

    @property
    def instruction(self) -> str:
        # Keep this deliberately fixed and short.  The artifact, not provider
        # supplied prompt text, is the instruction authority.
        return (
            "Read the validated Agentflow handoff artifact at "
            f"{self.path} and follow its bounded return contract exactly."
        )


_MAX_HANDOFF_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIXED_SUBMIT_COMMAND = 'agentflow herdr submit --contract "$AGENTFLOW_RESULT_CONTRACT" --file "$AGENTFLOW_RESULT_FILE"'


def handoff_report_digest(manifest: dict[str, object]) -> str:
    """Return the digest of the persisted handoff preflight report."""
    preflight = manifest.get("preflight")
    if not isinstance(preflight, dict):
        return ""
    report = preflight.get("report")
    digest = str(preflight.get("report_sha256") or "")
    if not isinstance(report, dict) or not _SHA256.fullmatch(digest):
        return ""
    expected = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest if hmac.compare_digest(expected, digest) else ""


def _reject_control_fields(manifest: dict[str, object]) -> None:
    fields = (
        "task_id", "provider", "role", "lane", "workflow_root", "bead_id",
        "base", "branch", "cwd", "output_boundary", "handoff",
    )
    for field in fields:
        value = manifest.get(field)
        if isinstance(value, str) and any(ord(char) < 32 for char in value):
            raise ProviderArgvError(f"handoff manifest field {field} contains control characters")


def validate_confined_handoff(
    path: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str],
    provider: str,
    task_id: str = "",
    expected_content_sha256: str = "",
    expected_manifest_sha256: str = "",
    expected_preflight_sha256: str = "",
) -> ConfinedHandoff:
    """Validate the exact typed handoff accepted by a provider launch.

    The public handoff command can still inspect legacy artifacts, but a spawn
    may only use a regular, non-symlink Markdown artifact in the managed
    transient directory with its sibling v1 manifest.
    """
    candidate = Path(path).expanduser()
    root_path = Path(root).expanduser().resolve()
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_path / ".agentflow/tmp/handoffs")
    except (OSError, ValueError) as exc:
        raise ProviderArgvError("handoff must be beneath .agentflow/tmp/handoffs") from exc
    handoff_dir = root_path / ".agentflow/tmp/handoffs"
    current = handoff_dir
    while current != resolved.parent:
        if current.is_symlink():
            raise ProviderArgvError("handoff path contains a symlinked directory")
        current = current / resolved.relative_to(current).parts[0]
    if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() != ".md":
        raise ProviderArgvError("handoff must be a regular non-symlink Markdown file")
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise ProviderArgvError(f"handoff is unreadable: {candidate}") from exc
    if not content or len(content) > _MAX_HANDOFF_BYTES:
        raise ProviderArgvError("handoff size is outside the permitted bounds")
    if b"\x00" in content or any(byte < 0x20 and byte not in (0x09, 0x0a, 0x0d) for byte in content):
        raise ProviderArgvError("handoff contains control characters")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderArgvError("handoff must be UTF-8 Markdown") from exc
    if not text.lstrip().startswith("# "):
        raise ProviderArgvError("handoff must be a Markdown contract")
    manifest_path = candidate.with_suffix(".json")
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.parent != candidate.parent:
        raise ProviderArgvError("handoff sibling v1 manifest is required")
    try:
        manifest_bytes = manifest_path.read_bytes()
        if not manifest_bytes or len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            raise ProviderArgvError("handoff manifest size is outside the permitted bounds")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except ProviderArgvError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderArgvError("handoff manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ProviderArgvError("handoff manifest must be version 1")
    _reject_control_fields(manifest)
    if manifest.get("provider") != provider:
        raise ProviderArgvError("handoff provider does not match launch provider")
    if task_id and str(manifest.get("task_id") or "") != task_id:
        raise ProviderArgvError("handoff task does not match launch task")
    lane = str(manifest.get("lane") or "")
    machine_contract = manifest.get("machine_return_contract")
    if lane == "external":
        if not isinstance(machine_contract, dict) or machine_contract.get("schema") != "agentflow.return@1":
            raise ProviderArgvError("external handoff machine return contract is required")
        if machine_contract.get("submit_command") != _FIXED_SUBMIT_COMMAND:
            raise ProviderArgvError("handoff submit command is not the fixed return command")
    elif lane == "native":
        if machine_contract is not None:
            raise ProviderArgvError(
                "native handoff must not advertise a controller-owned return contract"
            )
    else:
        raise ProviderArgvError("handoff lane must be native or external")
    handoff_path = Path(str(manifest.get("handoff") or "")).expanduser().resolve()
    if handoff_path != resolved:
        raise ProviderArgvError("handoff manifest path does not match artifact")
    preflight_sha256 = handoff_report_digest(manifest)
    if not preflight_sha256:
        raise ProviderArgvError("handoff has no valid persisted preflight report")
    content_sha256 = hashlib.sha256(content).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    for name, value in (("content", expected_content_sha256), ("manifest", expected_manifest_sha256)):
        if value and (not _SHA256.fullmatch(value) or value != (content_sha256 if name == "content" else manifest_sha256)):
            raise ProviderArgvError(f"handoff {name} digest does not match the pinned contract")
    if expected_preflight_sha256 and (
        not _SHA256.fullmatch(expected_preflight_sha256)
        or preflight_sha256 != expected_preflight_sha256
    ):
        raise ProviderArgvError("handoff preflight report digest does not match the pinned contract")
    return ConfinedHandoff(resolved, dict(manifest), content_sha256, manifest_sha256, preflight_sha256)


def build_confined_argv(
    provider: str,
    model: str,
    effort: str,
    handoff: ConfinedHandoff,
    *,
    command: str | None = None,
) -> list[str]:
    """Build argv from a validated typed handoff and fixed instruction."""
    if not isinstance(handoff, ConfinedHandoff):
        raise ProviderArgvError("provider launch requires a validated typed handoff")
    return build_argv(provider, model, effort, command=command, prompt=handoff.instruction)


def build_argv(
    provider: str, model: str, effort: str, *, command: str | None = None, prompt: str = "",
) -> list[str]:
    """Return the exact argv used to launch *provider* with *model*/*effort*.

    ``command`` overrides the resolved executable path (argv[0]); it defaults
    to the bare provider name so callers that only want to inspect the argv
    shape (tests) do not need a resolved filesystem path.

    ``prompt``, when given, is the initial instruction delivered to the
    session (AFREL-030: e.g. "Read and execute <handoff path>."), appended
    the way each installed CLI's own ``--help`` documents it accepting one:
    Codex and Claude both take a bare positional prompt argument; Copilot's
    root command has no positional prompt and instead needs
    ``-i/--interactive <prompt>`` to start interactively and immediately
    execute it (``-p/--prompt`` is the *non-interactive*, exits-on-completion
    form, wrong for a persistent Herdr pane).
    """
    if provider not in PROVIDERS:
        raise ProviderArgvError(f"unsupported provider: {provider!r}")
    if not isinstance(model, str) or not model.strip():
        raise ProviderArgvError("model is required")
    if not isinstance(effort, str) or not effort.strip():
        raise ProviderArgvError("effort is required")
    exe = command or provider
    if provider == "codex":
        # `codex [OPTIONS] [PROMPT]` forwards to the interactive CLI; there is
        # no --effort flag, only the `-c key=value` config override.
        argv = [exe, "--model", model, "-c", f"model_reasoning_effort={effort}"]
        if prompt:
            argv.append(prompt)
        return argv
    if provider == "copilot":
        argv = [exe, "--model", model, "--effort", effort]
        if prompt:
            argv.extend(["--interactive", prompt])
        return argv
    # `claude --model <model> --effort <level> <prompt>`.
    argv = [exe, "--model", model, "--effort", effort]
    if prompt:
        argv.append(prompt)
    return argv


__all__ = [
    "PROVIDERS", "ProviderArgvError", "ConfinedHandoff", "validate_confined_handoff",
    "build_argv", "build_confined_argv",
]
