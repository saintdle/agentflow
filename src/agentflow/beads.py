from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable, Mapping


MINIMUM_VERSION = (1, 3, 1)


class BeadsError(RuntimeError):
    """A safe, user-facing Beads integration error."""


class ClaimConflict(BeadsError):
    """A named task could not be claimed without changing the requested scope."""

    def __init__(
        self,
        reason: str,
        *,
        task: str = "",
        root: str = "",
        actor: str = "",
        observed: Any = None,
    ) -> None:
        self.reason = reason
        self.code = reason
        self.task = task
        self.root = root
        self.actor = actor
        self.observed = observed
        detail = f"exact claim conflict ({reason})"
        if task:
            detail += f" for {task}"
        super().__init__(detail)


@dataclass(frozen=True)
class ClaimIdentity:
    """The immutable identity bound by an exact claim."""

    root: str
    task: str
    actor: str
    claim_id: str
    claim_token: str = ""

    def __post_init__(self) -> None:
        if not self.root or not self.task or not self.actor or not self.claim_id:
            raise ClaimConflict("invalid_identity", task=self.task, root=self.root, actor=self.actor)
        if not self.claim_token:
            object.__setattr__(self, "claim_token", secrets.token_urlsafe(32))

    @property
    def token(self) -> str:
        return self.claim_token

    def to_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "task": self.task,
            "actor": self.actor,
            "claim_id": self.claim_id,
            "claim_token": self.claim_token,
            "token": self.token,
        }


@dataclass(frozen=True)
class ExactClaim:
    """The verified result of one exact Beads claim."""

    identity: ClaimIdentity
    issue: dict[str, Any]

    @property
    def root(self) -> str:
        return self.identity.root

    @property
    def task(self) -> str:
        return self.identity.task

    @property
    def actor(self) -> str:
        return self.identity.actor

    @property
    def claim_id(self) -> str:
        return self.identity.claim_id

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "issue": self.issue}


def _local_beads_dir(cwd: Path) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / ".beads"
        if (candidate / "metadata.json").is_file() or (candidate / "config.yaml").is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _is_git_repository(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def executable() -> str | None:
    selected = shutil.which("bd")
    if selected:
        return selected
    candidates = (
        Path("/opt/homebrew/bin/bd"),
        Path.home() / ".local/bin/bd",
        Path("/usr/local/bin/bd"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("bd")


def run(
    cwd: Path,
    *arguments: str,
    timeout: float = 30,
    input_text: str | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = executable()
    if not command:
        raise BeadsError("Beads CLI is unavailable; install stable beads 1.3.1 or later.")
    environment = os.environ.copy()
    environment.setdefault("BD_NON_INTERACTIVE", "1")
    environment.setdefault("DOLT_DISABLE_EVENT_FLUSH", "1")
    local_beads = _local_beads_dir(cwd)
    if local_beads is not None and not _is_git_repository(cwd):
        environment.setdefault("BEADS_DIR", str(local_beads))
    if environment_overrides:
        environment.update(environment_overrides)
    if arguments and arguments[0] in {"ready", "list"}:
        preflight_topology(cwd, environment=environment, arguments=arguments)
    try:
        return subprocess.run(
            [command, *arguments],
            cwd=cwd,
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BeadsError(f"Beads command failed to run: {exc}") from exc


def _json_output(result: subprocess.CompletedProcess[str], operation: str) -> Any:
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit {result.returncode}"
        raise BeadsError(f"{operation} failed: {message}")
    text = result.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise BeadsError(f"{operation} returned invalid JSON")


def version() -> tuple[tuple[int, int, int] | None, str]:
    command = executable()
    if not command:
        return None, "missing"
    result = run(Path.cwd(), "version")
    text = (result.stdout or result.stderr).strip().splitlines()
    display = text[0] if text else command
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", display)
    parsed = tuple(int(part) for part in match.groups()) if match else None
    return parsed, display


def require_supported_version() -> str:
    parsed, display = version()
    if parsed is None:
        raise BeadsError(f"Cannot determine Beads version from: {display}")
    if parsed < MINIMUM_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_VERSION)
        raise BeadsError(f"Beads {minimum} or later is required; found {display}")
    return display


def workspace(cwd: Path) -> dict[str, Any] | None:
    if not executable():
        return None
    result = run(cwd, "where", "--json", timeout=2)
    if result.returncode:
        return None
    value = _json_output(result, "bd where")
    if not isinstance(value, dict):
        return None
    beads_path = Path(str(value.get("path") or ""))
    mode = _configured_mode(beads_path)
    if mode:
        value["_agentflow_mode"] = mode
    if not _is_git_repository(cwd):
        value["_agentflow_gitless"] = True
    return value


def workspace_mode(data: dict[str, Any]) -> str:
    mode = data.get("_agentflow_mode") or data.get("dolt_mode")
    if mode in {"embedded", "server", "shared-server", "proxied-server"}:
        return str(mode)
    if data.get("path"):
        mode = _configured_mode(Path(str(data["path"])))
        if mode:
            return mode
    database_path = str(data.get("database_path") or "")
    if "shared-server" in database_path:
        return "shared-server"
    if "/dolt/" in database_path or database_path.endswith("/dolt"):
        return "server"
    if "embeddeddolt" in database_path:
        return "embedded"
    return "unknown"


def _configured_mode(beads_dir: Path) -> str:
    """Read topology without opening the database or starting a server."""
    try:
        metadata = json.loads((beads_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = {}
    mode = metadata.get("dolt_mode", "") if isinstance(metadata, dict) else ""
    if mode == "proxied-server":
        return mode
    try:
        lines = (beads_dir / "config.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    # Beads emits both dotted top-level keys and a nested dolt mapping. Only
    # inspect this boolean; unrelated YAML sections must not select topology.
    in_dolt = False
    child_indent: int | None = None
    for line in lines:
        content = line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        if not content[0].isspace():
            in_dolt = bool(re.fullmatch(r"dolt\s*:\s*", content))
            child_indent = None
            if re.fullmatch(r"dolt\.shared-server\s*:\s*true\s*", content, re.IGNORECASE):
                return "shared-server"
            continue
        indent = len(content) - len(content.lstrip())
        if in_dolt and child_indent is None:
            child_indent = indent
        if in_dolt and indent == child_indent and re.fullmatch(r"\s*shared-server\s*:\s*true\s*", content, re.IGNORECASE):
            return "shared-server"
    return str(mode) if mode in {"embedded", "server", "shared-server"} else ""


def preflight_topology(
    cwd: Path, *, environment: Mapping[str, str] | None = None,
    arguments: Iterable[str] = (),
) -> None:
    """Reject unsupported row caps before a proxied discovery invocation."""
    environment = os.environ if environment is None else environment
    selected = environment.get("BEADS_DIR")
    beads_dir = Path(selected) if selected else _local_beads_dir(cwd)
    if beads_dir is not None and not beads_dir.is_absolute():
        beads_dir = cwd / beads_dir
    if beads_dir is None:
        return
    # Worktrees may redirect to the repository's actual store.
    seen: set[Path] = set()
    while (beads_dir / "redirect").is_file():
        if beads_dir.resolve() in seen:
            raise BeadsError("Beads workspace redirect cycle; repair .beads/redirect before discovery")
        seen.add(beads_dir.resolve())
        target = Path((beads_dir / "redirect").read_text(encoding="utf-8").strip())
        beads_dir = target if target.is_absolute() else beads_dir / target
    if _configured_mode(beads_dir) != "proxied-server":
        return
    arguments = tuple(arguments)
    capped = bool(environment.get("BEADS_MAX_ROWS")) or any(
        value == "--max-rows" or value.startswith("--max-rows=") for value in arguments
    )
    if capped:
        raise BeadsError(
            "Proxied-server discovery does not support BEADS_MAX_ROWS/--max-rows. "
            "Use an embedded/server workspace to enforce the cap, or explicitly "
            "remove the cap after reviewing the query. Agentflow will not discard it."
        )


def initialize(
    cwd: Path,
    *,
    mode: str,
    stealth: bool,
    prefix: str,
    gitless: bool = False,
) -> tuple[str, dict[str, Any]]:
    require_supported_version()
    existing = workspace(cwd)
    if existing is not None:
        return "existing", existing

    arguments = [
        "init",
        "--non-interactive",
        "--skip-agents",
        "--skip-hooks",
        "--prefix",
        prefix,
    ]
    if stealth:
        arguments.append("--stealth")
    if mode == "shared-server":
        arguments.append("--shared-server")
    elif mode != "embedded":
        raise BeadsError(f"Unsupported Beads mode: {mode}")

    if gitless:
        (cwd / ".beads").mkdir(parents=True, exist_ok=True)
    environment = {"BEADS_DIR": str(cwd / ".beads")} if gitless else None
    result = run(cwd, *arguments, timeout=90, environment_overrides=environment)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BeadsError(f"bd init failed: {detail[-1] if detail else result.returncode}")
    created = workspace(cwd)
    if created is None:
        raise BeadsError("bd init completed but the workspace cannot be discovered")
    return "created", created


def get_issue(
    cwd: Path, issue_id: str, *, brief_dependencies: bool = False, timeout: float = 30,
) -> dict[str, Any]:
    projection = ("--brief-deps",) if brief_dependencies else ()
    value = _json_output(
        run(cwd, "show", issue_id, *projection, "--json", timeout=timeout),
        f"bd show {issue_id}",
    )
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BeadsError(f"Bead not found or ambiguous: {issue_id}")
    issue = value[0]
    if brief_dependencies:
        issue["_agentflow_projection"] = "brief-deps"
    return issue


def claim_generation(cwd: Path, task: str) -> str:
    """Fingerprint native claim events, independently of actor/timestamp reuse.

    Audit history is available without the optional event journal. Include all
    claim IDs: native timestamps have second precision and are not a total order.
    Heartbeats, idempotent claims and unrelated edits do not add claim events.
    Missing/truncated history must never bootstrap a new execution generation.
    """
    result = run(cwd, "history", task, "--events", "--limit", "0", "--json", timeout=10)
    if result.returncode:
        raise BeadsError("native claim generation history is unavailable")
    try:
        events = json.loads(result.stdout)
        if not isinstance(events, list):
            raise ValueError("expected event list")
        claims = []
        seen = set()
        for event in events:
            if not isinstance(event, dict) or event.get("issue_id") != task:
                raise ValueError("invalid task event")
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id or event_id in seen:
                raise ValueError("invalid event identity")
            seen.add(event_id)
            if event.get("event_type") == "claimed":
                actor = event.get("actor")
                if not isinstance(actor, str) or not actor:
                    raise ValueError("invalid claim actor")
                claims.append((event_id, actor))
        if not claims:
            raise ValueError("missing claim events")
    except (ValueError, TypeError) as exc:
        raise BeadsError("native claim generation history is invalid") from exc
    return hashlib.sha256(json.dumps(sorted(claims), separators=(",", ":")).encode()).hexdigest()


def heartbeat_issue(cwd: Path, task: str, *, actor: str) -> None:
    """Renew one native claim, checking the exact identity in the CLI receipt."""
    if not task or not actor:
        raise BeadsError("Native task heartbeat requires an exact task and actor")
    value = _json_output(
        run(cwd, "heartbeat", task, "--actor", actor, "--json", timeout=10),
        "bd heartbeat",
    )
    if not isinstance(value, dict) or any(
        value.get(field) != expected
        for field, expected in (("id", task), ("status", "heartbeat"), ("owner", actor))
    ):
        raise BeadsError("Native task heartbeat did not confirm the exact task and actor")


def root_descendants(cwd: Path, root: str) -> list[dict[str, Any]]:
    """Return the exact durable Beads descendants of *root*.

    Controller traversal is deliberately based on one Beads graph snapshot;
    callers cannot replace it with caller-supplied task rows or outcomes.
    Parent links are checked locally so an accidentally broad ``bd list``
    result cannot escape the approved root.
    """

    if not root:
        raise BeadsError("workflow root is required")
    value = _json_output(
        run(cwd, "list", "--all", "--brief", "--limit", "0", "--json"), "bd list --all"
    )
    if isinstance(value, dict):
        raw = value.get("issues", [])
    else:
        raw = value
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise BeadsError("bd list --all returned an unexpected JSON shape")
    rows = [dict(item) for item in raw]
    by_id = {str(item.get("id") or ""): item for item in rows}
    if root not in by_id:
        # A root may be omitted by a provider-specific list response.  Prove it
        # exists separately; the descendants themselves still come only from
        # the list snapshot.
        get_issue(cwd, root)

    def parent_of(item: Mapping[str, Any]) -> str:
        for field in ("parent", "parent_id", "root_id", "workflow_root"):
            if field in item and item[field] not in (None, ""):
                return str(item[field])
        return ""

    descendants: list[dict[str, Any]] = []
    for item in rows:
        item_id = str(item.get("id") or "")
        if not item_id or item_id == root:
            continue
        current = parent_of(item)
        seen: set[str] = set()
        reached = False
        while current and current not in seen:
            if current == root:
                reached = True
                break
            seen.add(current)
            ancestor = by_id.get(current)
            if ancestor is None:
                # The snapshot is incomplete; do not guess ancestry.
                break
            current = parent_of(ancestor)
        if reached:
            item["workflow_root"] = root
            item["_agentflow_projection"] = "brief"
            descendants.append(item)
    return sorted(descendants, key=lambda item: str(item.get("id") or ""))


def claim_ready(
    cwd: Path,
    *,
    parent: str,
    labels: list[str],
    actor: str,
) -> dict[str, Any] | None:
    common = ["ready", "--parent", parent, "--limit", "1", "--sort", "priority", "--brief"]
    for label in labels:
        common.extend(["--label", label])

    def issues_from(value: Any, operation: str) -> list[Any]:
        if isinstance(value, dict):
            if isinstance(value.get("issues"), list):
                return value["issues"]
            elif value.get("id"):
                return [value]
            return []
        elif isinstance(value, list):
            return value
        raise BeadsError(f"{operation} returned an unexpected JSON shape")

    # Brief discovery is never execution authority. Fetch the selected task in
    # full and exact-claim it; a race must fail rather than select another task.
    assigned_value = _json_output(
        run(cwd, *common, "--assignee", actor, "--json"),
        "bd ready --assignee",
    )
    assigned = issues_from(assigned_value, "bd ready --assignee")
    if assigned:
        if len(assigned) != 1 or not isinstance(assigned[0], dict):
            raise BeadsError("bd ready --assignee returned an ambiguous result")
        issue_id = str(assigned[0].get("id") or "")
        if not issue_id:
            raise BeadsError("bd ready --assignee returned an issue without an ID")
        return claim_issue_exact(cwd, issue_id, root=parent, actor=actor, labels=labels).issue

    shared_value = _json_output(
        run(
            cwd,
            *common,
            "--unassigned",
            "--json",
        ),
        "bd ready --unassigned",
    )
    shared = issues_from(shared_value, "bd ready --unassigned")
    if shared:
        if len(shared) != 1 or not isinstance(shared[0], dict):
            raise BeadsError("bd ready --unassigned returned an ambiguous result")
        issue_id = str(shared[0].get("id") or "")
        return claim_issue_exact(cwd, issue_id, root=parent, actor=actor, labels=labels).issue
    return None


def _issue_value(issue: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in issue:
            return issue[name]
    return None


def _claim_conflict(
    reason: str,
    *,
    task: str,
    root: str,
    actor: str,
    observed: Any = None,
) -> ClaimConflict:
    return ClaimConflict(reason, task=task, root=root, actor=actor, observed=observed)


def _validate_exact_issue(
    cwd: Path,
    issue: Mapping[str, Any],
    *,
    task: str,
    root: str,
    actor: str,
    labels: set[str],
) -> None:
    observed_id = str(issue.get("id") or "")
    if observed_id != task:
        raise _claim_conflict(
            "task_identity_mismatch", task=task, root=root, actor=actor, observed=observed_id
        )

    # A direct parent is the normal Beads representation.  The root_id/root
    # aliases are accepted for imported graph snapshots, but never inferred.
    parent = _issue_value(issue, "parent", "parent_id", "root_id", "workflow_root")
    if str(parent or "") != root:
        # Beads normally supplies the immediate parent.  Walk only when the
        # snapshot proves a different parent; an absent parent is never
        # treated as an implicit root.
        seen: set[str] = set()
        current = str(parent or "")
        reached_root = False
        while current and current not in seen and current != root:
            seen.add(current)
            try:
                ancestor = get_issue(cwd, current)
            except BeadsError as exc:
                raise _claim_conflict(
                    "ancestry_unavailable", task=task, root=root, actor=actor, observed=current
                ) from exc
            next_parent = _issue_value(
                ancestor, "parent", "parent_id", "root_id", "workflow_root"
            )
            current = str(next_parent or "")
        reached_root = current == root
        if reached_root:
            parent = root
    if str(parent or "") != root:
        raise _claim_conflict(
            "wrong_root", task=task, root=root, actor=actor, observed=parent
        )

    explicit_ready = _issue_value(issue, "ready", "is_ready", "claimable")
    if explicit_ready is False:
        raise _claim_conflict("not_ready", task=task, root=root, actor=actor, observed=issue)
    status = str(issue.get("status") or "open").lower()
    if status in {"blocked", "closed", "done", "deferred", "cancelled", "canceled"}:
        raise _claim_conflict("not_ready", task=task, root=root, actor=actor, observed=status)
    if status == "in_progress" and str(issue.get("assignee") or "") != actor:
        raise _claim_conflict("already_claimed", task=task, root=root, actor=actor, observed=issue)

    assignee = str(issue.get("assignee") or "")
    if assignee and assignee != actor:
        raise _claim_conflict("wrong_assignee", task=task, root=root, actor=actor, observed=assignee)

    actual_labels = {str(label) for label in (issue.get("labels") or ())}
    missing = sorted(labels - actual_labels)
    if missing:
        raise _claim_conflict("labels_mismatch", task=task, root=root, actor=actor, observed=missing)

    blocked = _issue_value(issue, "blocked", "is_blocked")
    if blocked is True:
        raise _claim_conflict("not_ready", task=task, root=root, actor=actor, observed=issue)

    # If a snapshot carries dependency status, only an explicitly open blocker
    # makes it unclaimable.  Parent-child metadata is not itself a blocker.
    dependencies = issue.get("dependencies")
    if isinstance(dependencies, list):
        for dependency in dependencies:
            if not isinstance(dependency, Mapping):
                continue
            dependency_status = str(dependency.get("status") or "").lower()
            dependency_type = str(dependency.get("dependency_type") or "").lower()
            if dependency_type == "parent-child":
                continue
            if dependency_status and dependency_status not in {"closed", "done", "resolved"}:
                raise _claim_conflict(
                    "not_ready", task=task, root=root, actor=actor, observed=dependency
                )


def verify_task_ancestry_and_ownership(
    cwd: Path,
    issue: Mapping[str, Any],
    *,
    task: str,
    root: str,
    actor: str,
) -> None:
    """Prove ``issue`` really descends from ``root`` and is owned by ``actor``.

    Reuses the same ancestry-walking, status, and assignee validation
    ``claim_issue_exact`` applies at claim time (AFREL-029): launch
    authority must never trust copied ``metadata.agentflow`` fields alone,
    since those can be stale or forged for an unrelated task. Raises
    ``ClaimConflict`` (a ``BeadsError``) on any mismatch. ``labels`` are a
    claim-time concern, not a launch-time one, so none are required here.
    """
    _validate_exact_issue(cwd, issue, task=task, root=root, actor=actor, labels=set())


def claim_issue_exact(
    cwd: Path,
    task: str | None = None,
    *,
    root: str | None = None,
    actor: str | None = None,
    labels: Iterable[str] = (),
    claim_id: str = "",
    task_id: str | None = None,
    issue_id: str | None = None,
    root_id: str | None = None,
    actor_id: str | None = None,
    expected_labels: Iterable[str] | None = None,
    persist: bool = False,
) -> ExactClaim:
    """Atomically claim exactly one already-selected task.

    This function intentionally does not call ``bd ready``.  Selection and
    claiming are separate operations here: a controller has already selected
    the task, so falling back to another ready task after a mismatch would
    violate the root/task/actor claim identity.
    """

    selected = task or task_id or issue_id or ""
    root = root or root_id or ""
    actor = actor or actor_id or ""
    if expected_labels is not None:
        labels = expected_labels
    if task and task_id and task != task_id:
        raise _claim_conflict("task_identity_mismatch", task=task, root=root, actor=actor)
    if task and issue_id and task != issue_id:
        raise _claim_conflict("task_identity_mismatch", task=task, root=root, actor=actor)
    if not selected or not root or not actor:
        raise _claim_conflict("invalid_identity", task=selected, root=root, actor=actor)
    requested_labels = {str(label) for label in labels if str(label)}
    try:
        initial = get_issue(cwd, selected)
    except BeadsError as exc:
        raise _claim_conflict(
            "task_not_found", task=selected, root=root, actor=actor
        ) from exc
    _validate_exact_issue(
        cwd, initial, task=selected, root=root, actor=actor, labels=requested_labels
    )

    identity = ClaimIdentity(root, selected, actor, claim_id or f"{root}/{selected}/{actor}")
    metadata = initial.get("metadata")
    if isinstance(metadata, Mapping):
        agentflow = metadata.get("agentflow")
        if isinstance(agentflow, Mapping):
            for field, expected in (("root", root), ("task", selected), ("actor", actor)):
                stored = agentflow.get(field)
                if stored not in (None, "", expected):
                    raise _claim_conflict(
                        "claim_identity_mismatch",
                        task=selected,
                        root=root,
                        actor=actor,
                        observed=stored,
                    )
            stored_claim = agentflow.get("claim_id")
            if stored_claim not in (None, "", identity.claim_id):
                raise _claim_conflict(
                    "claim_identity_mismatch",
                    task=selected,
                    root=root,
                    actor=actor,
                    observed=stored_claim,
                    )
            stored_token = str(agentflow.get("claim_token") or "")
            if stored_token:
                if stored_token == f"{root}/{selected}/{actor}" or len(stored_token) < 32:
                    raise _claim_conflict(
                        "claim_identity_mismatch", task=selected, root=root,
                        actor=actor, observed=stored_token,
                    )
                identity = ClaimIdentity(root, selected, actor, identity.claim_id, stored_token)

    result = run(cwd, "update", selected, "--claim", "--actor", actor)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise _claim_conflict(
            "claim_rejected",
            task=selected,
            root=root,
            actor=actor,
            observed=detail[-1] if detail else result.returncode,
        )

    try:
        final = get_issue(cwd, selected)
    except BeadsError as exc:
        raise _claim_conflict(
            "ownership_not_confirmed", task=selected, root=root, actor=actor
        ) from exc
    _validate_exact_issue(
        cwd, final, task=selected, root=root, actor=actor, labels=requested_labels
    )
    final_assignee = str(final.get("assignee") or "")
    if final_assignee != actor:
        raise _claim_conflict(
            "ownership_not_confirmed",
            task=selected,
            root=root,
            actor=actor,
            observed=final_assignee,
        )
    if persist:
        update_agentflow_metadata(
            cwd,
            selected,
            {
                "root": root,
                "task": selected,
                "actor": actor,
                "claim_id": identity.claim_id,
                "claim_token": identity.token,
            },
        )
    return ExactClaim(identity, dict(final))


def update_issue(
    cwd: Path,
    issue_id: str,
    *,
    status: str = "",
    assignee: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> None:
    arguments = ["update", issue_id]
    if status:
        arguments.extend(["--status", status])
    if assignee is not None:
        arguments.extend(["--assignee", assignee])
    for label in add_labels or []:
        arguments.extend(["--add-label", label])
    for label in remove_labels or []:
        arguments.extend(["--remove-label", label])
    result = run(cwd, *arguments)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BeadsError(
            f"Cannot update {issue_id}: {detail[-1] if detail else result.returncode}"
        )


def create_issue(
    cwd: Path,
    *,
    title: str,
    description: str,
    acceptance: str,
    parent: str,
    labels: list[str],
    assignee: str = "",
    dependencies: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    issue_id: str = "",
) -> dict[str, Any]:
    arguments = [
        "create",
        title,
        "--description",
        description,
        "--acceptance",
        acceptance,
        "--parent",
        parent,
        "--no-inherit-labels",
        "--labels",
        ",".join(labels),
        "--json",
    ]
    if assignee:
        arguments.extend(["--assignee", assignee])
    if issue_id:
        arguments.extend(["--id", issue_id])
    if dependencies:
        arguments.extend(["--deps", ",".join(dependencies)])
    if metadata:
        arguments.extend(
            [
                "--metadata",
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            ]
        )
    value = _json_output(run(cwd, *arguments), "bd create")
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict) or not value.get("id"):
        raise BeadsError("bd create returned an unexpected JSON shape")
    return value


def close_issue(cwd: Path, issue_id: str, reason: str, *, actor: str = "") -> None:
    arguments = ["close", issue_id, "--reason", reason]
    if actor:
        arguments.extend(["--actor", actor])
    result = run(cwd, *arguments)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BeadsError(
            f"Cannot close {issue_id}: {detail[-1] if detail else result.returncode}"
        )


def update_agentflow_metadata(
    cwd: Path, issue_id: str, update: dict[str, Any], *,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Merge metadata using an optional, already validated exact-issue snapshot."""
    issue = snapshot if snapshot is not None else get_issue(cwd, issue_id)
    if snapshot is not None and issue.get("id") != issue_id:
        raise BeadsError("Metadata snapshot does not match the requested issue")
    metadata = issue.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    agentflow = metadata.get("agentflow")
    agentflow = dict(agentflow) if isinstance(agentflow, dict) else {}
    agentflow.update(update)
    metadata["agentflow"] = agentflow
    result = run(
        cwd,
        "update",
        issue_id,
        "--metadata",
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BeadsError(
            f"Cannot update Agentflow metadata on {issue_id}: "
            f"{detail[-1] if detail else result.returncode}"
        )


def add_comment(cwd: Path, issue_id: str, text: str) -> None:
    result = run(cwd, "comment", issue_id, text)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BeadsError(
            f"Cannot comment on {issue_id}: {detail[-1] if detail else result.returncode}"
        )


def prime(cwd: Path, *, maximum_characters: int = 12_000, truncate: bool = True) -> str:
    workspace_data = workspace(cwd)
    if workspace_data is None:
        return ""
    result = run(cwd, "prime", "--stealth", timeout=2)
    if result.returncode:
        return ""
    text = result.stdout.strip()
    if workspace_data.get("_agentflow_gitless"):
        beads_dir = str(workspace_data.get("path") or "")
        text = (
            "Gitless Beads workspace. Workers may execute concurrently on disjoint "
            "outputs, but the controller is the sole Beads graph writer. "
            "For direct commands outside this folder use "
            f"`BEADS_DIR={shlex.quote(beads_dir)} bd <command>`; do not create Git "
            f"state.\n\n{text}"
        )
    return text[:maximum_characters] if truncate else text
