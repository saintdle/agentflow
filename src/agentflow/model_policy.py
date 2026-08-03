"""Versioned exact provider/model/role/effort policy, audit, and migration planning.

The policy is a closed, versioned document (see ``policies/models-v1.json``)
listing the only approved (provider, model, role, effort) routes. Anything not
an exact match fails closed: a generic alias (``opus``), a renamed or
deprecated tier (``gpt-5.6-terra``), an unresolved placeholder, or a lookalike
string (wrong case, stray whitespace, homoglyph) is rejected the same way as
an explicitly forbidden model. This module never writes to disk; the audit
and migration-planning primitives are read-only.
"""

from __future__ import annotations

import dataclasses
import json
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib
from pathlib import Path
from typing import Any, Iterable, Mapping

from agentflow import resources


class ModelPolicyError(ValueError):
    """Raised when a policy document or a lookup input is malformed or unsafe."""


SCHEMA = "agentflow.model_policy"
SUPPORTED_VERSIONS = (1,)
DEFAULT_POLICY_PATH = resources.item("policies", "models-v1.json")

_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_EFFORT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")

_TOP_LEVEL_FIELDS = frozenset({"schema", "version", "id", "description", "roles", "routes", "forbidden_models"})
_ROUTE_FIELDS = frozenset({"provider", "model", "roles", "efforts"})

# Every shipped agentflow profile agentflow itself ships and knows how to
# safely reason about. A migration plan never proposes a change for a file
# outside this identity set, even if it happens to sit in the same directory.
MANAGED_PROFILE_STEMS = frozenset(
    {
        "agentflow-controller",
        "agentflow-explorer",
        "agentflow-pr-gatekeeper",
        "agentflow-reviewer",
    }
)
_CODEX_PROFILE_GLOB = ".codex/agents/*.toml"
_CLAUDE_PROFILE_GLOB = ".claude/agents/*.md"
_COPILOT_PROFILE_GLOB = ".github/agents/*.agent.md"

# The role each shipped profile plays, used to plan and audit role-aware
# (not just provider-aware) migrations: a profile using an otherwise-approved
# model for the wrong role (e.g. a coding-tier model on the controller
# profile) is still a violation.
PROFILE_ROLES: Mapping[str, str] = {
    "agentflow-controller": "controller",
    "agentflow-explorer": "exploration",
    "agentflow-pr-gatekeeper": "judgment",
    "agentflow-reviewer": "review",
}


def _require_str(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ModelPolicyError(f"{field} must be a non-empty exact string")
    if pattern is not None and not pattern.match(value):
        raise ModelPolicyError(f"{field} {value!r} has an invalid shape")
    return value


@dataclasses.dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    roles: tuple[str, ...]
    efforts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "roles": list(self.roles),
            "efforts": list(self.efforts),
        }


@dataclasses.dataclass(frozen=True)
class RouteResult:
    ok: bool
    reason: str
    route: ModelRoute | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "route": self.route.to_dict() if self.route else None}


@dataclasses.dataclass(frozen=True)
class ModelPolicy:
    schema: str
    version: int
    id: str
    roles: tuple[str, ...]
    routes: tuple[ModelRoute, ...]
    forbidden_models: tuple[str, ...]

    def approved_models(self, provider: str) -> frozenset[str]:
        return frozenset(route.model for route in self.routes if route.provider == provider)

    def validate_route(self, *, provider: Any, role: Any, model: Any, effort: Any = None) -> RouteResult:
        """Validate an exact (provider, role, model[, effort]) combination.

        Returns a :class:`RouteResult` rather than raising: callers (tests,
        preflight, audits) need a pass/fail verdict plus a reason for every
        input, including malformed ones, not an exception path.
        """

        for field, value in (("provider", provider), ("role", role), ("model", model)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                return RouteResult(False, f"{field} must be a non-empty exact string")
        if effort is not None and (not isinstance(effort, str) or not effort.strip() or effort != effort.strip()):
            return RouteResult(False, "effort must be a non-empty exact string")

        if model in self.forbidden_models:
            return RouteResult(False, f"model {model!r} is forbidden under {self.id}")
        if role not in self.roles:
            return RouteResult(False, f"role {role!r} is not defined by {self.id}")

        candidates = [route for route in self.routes if route.provider == provider and route.model == model]
        if not candidates:
            return RouteResult(
                False, f"no exact route approves provider={provider!r} model={model!r} under {self.id}"
            )
        role_candidates = [route for route in candidates if role in route.roles]
        if not role_candidates:
            return RouteResult(
                False, f"model {model!r} is not approved for role {role!r} under {self.id}"
            )
        if effort is None:
            return RouteResult(True, "approved", role_candidates[0])
        effort_candidates = [route for route in role_candidates if effort in route.efforts]
        if not effort_candidates:
            return RouteResult(
                False,
                f"effort {effort!r} is not approved for provider={provider!r} model={model!r} "
                f"role={role!r} under {self.id}",
            )
        return RouteResult(True, "approved", effort_candidates[0])


def _load_route(raw: Any, *, known_roles: frozenset[str]) -> ModelRoute:
    if not isinstance(raw, Mapping):
        raise ModelPolicyError("each route must be an object")
    unknown = sorted(set(raw) - _ROUTE_FIELDS)
    if unknown:
        raise ModelPolicyError(f"route has unknown field(s): {', '.join(unknown)}")
    provider = _require_str(raw.get("provider"), "route.provider", _PROVIDER_PATTERN)
    model = _require_str(raw.get("model"), "route.model", _MODEL_PATTERN)
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise ModelPolicyError(f"route for {model!r} must declare a non-empty roles list")
    roles = tuple(_require_str(role, "route.roles[]", _ROLE_PATTERN) for role in roles_raw)
    if len(set(roles)) != len(roles):
        raise ModelPolicyError(f"route for {model!r} has duplicate roles")
    unknown_roles = sorted(set(roles) - known_roles)
    if unknown_roles:
        raise ModelPolicyError(f"route for {model!r} references undefined role(s): {', '.join(unknown_roles)}")
    efforts_raw = raw.get("efforts")
    if not isinstance(efforts_raw, list) or not efforts_raw:
        raise ModelPolicyError(f"route for {model!r} must declare a non-empty efforts list")
    efforts = tuple(_require_str(effort, "route.efforts[]", _EFFORT_PATTERN) for effort in efforts_raw)
    if len(set(efforts)) != len(efforts):
        raise ModelPolicyError(f"route for {model!r} has duplicate efforts")
    return ModelRoute(provider=provider, model=model, roles=roles, efforts=efforts)


def parse_policy(data: Any) -> ModelPolicy:
    """Validate a raw policy document against the closed ``agentflow.model_policy`` schema."""

    if not isinstance(data, Mapping):
        raise ModelPolicyError("policy document must be an object")
    unknown = sorted(set(data) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ModelPolicyError(f"policy has unknown top-level field(s): {', '.join(unknown)}")

    schema = data.get("schema")
    if schema != SCHEMA:
        raise ModelPolicyError(f"unexpected schema {schema!r}; expected {SCHEMA!r}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        raise ModelPolicyError(f"unsupported policy version {version!r}")
    policy_id = _require_str(data.get("id"), "id")
    if "description" in data and not isinstance(data["description"], str):
        raise ModelPolicyError("description must be a string")

    roles_raw = data.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise ModelPolicyError("roles must be a non-empty list")
    roles = tuple(_require_str(role, "roles[]", _ROLE_PATTERN) for role in roles_raw)
    if len(set(roles)) != len(roles):
        raise ModelPolicyError("roles must not contain duplicates")

    routes_raw = data.get("routes")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ModelPolicyError("routes must be a non-empty list")
    routes = tuple(_load_route(route, known_roles=frozenset(roles)) for route in routes_raw)
    seen_provider_models = set()
    for route in routes:
        key = (route.provider, route.model)
        if key in seen_provider_models:
            raise ModelPolicyError(f"duplicate route for provider={route.provider!r} model={route.model!r}")
        seen_provider_models.add(key)

    forbidden_raw = data.get("forbidden_models", [])
    if not isinstance(forbidden_raw, list):
        raise ModelPolicyError("forbidden_models must be a list")
    forbidden_models = tuple(_require_str(model, "forbidden_models[]") for model in forbidden_raw)
    if len(set(forbidden_models)) != len(forbidden_models):
        raise ModelPolicyError("forbidden_models must not contain duplicates")

    approved_models = {route.model for route in routes}
    overlap = approved_models & set(forbidden_models)
    if overlap:
        raise ModelPolicyError(f"model(s) both approved and forbidden: {', '.join(sorted(overlap))}")

    return ModelPolicy(
        schema=schema, version=version, id=policy_id, roles=roles, routes=routes, forbidden_models=forbidden_models
    )


def load_policy(path: Any = DEFAULT_POLICY_PATH) -> ModelPolicy:
    try:
        source = path if hasattr(path, "read_text") else Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelPolicyError(f"cannot read policy {path}: {exc}") from exc
    return parse_policy(raw)


# --- Profile audit -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProfileViolation:
    path: str
    provider: str
    model: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _profile_identity(path: Path) -> str:
    """The profile name a path claims, e.g. ``agentflow-controller``.

    ``Path.stem`` only strips the last suffix, which under-strips GitHub
    Copilot's ``*.agent.md`` double extension; handle it explicitly rather
    than mis-identifying every Copilot profile as unmanaged.
    """

    if path.name.endswith(".agent.md"):
        return path.name[: -len(".agent.md")]
    return path.stem


def is_managed_profile(path: Path) -> bool:
    """Whether ``path`` is one of the profiles agentflow itself ships and knows.

    Anything else -- a user's own custom agent, an unrelated file that
    happens to match the directory glob -- is out of scope for both the audit
    and migration planning below.
    """

    return _profile_identity(path) in MANAGED_PROFILE_STEMS and path.suffix in (".toml", ".md")


def _role_for_profile(path: Path) -> str | None:
    return PROFILE_ROLES.get(_profile_identity(path))


def _read_codex_model(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ModelPolicyError(f"cannot parse {path}: {exc}") from exc
    model = data.get("model")
    return model if isinstance(model, str) and model.strip() else None


def _read_frontmatter_model(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        key, sep, value = stripped.partition(":")
        if sep and key.strip() == "model":
            value = value.strip()
            return value or None
    return None


def discover_profiles(root: Path) -> list[tuple[Path, str]]:
    """Return ``(path, provider)`` for every managed profile shipped under ``root``."""

    root = Path(root)
    profiles: list[tuple[Path, str]] = []
    for path in sorted(root.glob(_CODEX_PROFILE_GLOB)):
        if is_managed_profile(path):
            profiles.append((path, "codex"))
    for path in sorted(root.glob(_CLAUDE_PROFILE_GLOB)):
        if is_managed_profile(path):
            profiles.append((path, "claude"))
    for path in sorted(root.glob(_COPILOT_PROFILE_GLOB)):
        if is_managed_profile(path):
            profiles.append((path, "copilot"))
    return profiles


def _profile_model(path: Path, provider: str) -> str | None:
    if provider == "codex":
        return _read_codex_model(path)
    return _read_frontmatter_model(path)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def audit_profiles(root: Path, policy: ModelPolicy) -> list[ProfileViolation]:
    """Read-only scan of every shipped agent profile under ``root`` against ``policy``.

    Performs no writes. A profile violates the policy if it declares no
    model, has no known role mapping, or declares a (provider, role, model)
    combination :meth:`ModelPolicy.validate_route` rejects -- this covers
    every forbidden model, generic alias, unresolved placeholder, lookalike
    string, and role/provider mismatch (e.g. a coding-tier model pinned to
    the controller profile), since none of those are exact approved routes.
    """

    root = Path(root)
    violations: list[ProfileViolation] = []
    for path, provider in discover_profiles(root):
        rel = _relative(path, root)
        model = _profile_model(path, provider)
        if not model:
            violations.append(ProfileViolation(rel, provider, "", f"{rel} declares no model"))
            continue
        role = _role_for_profile(path)
        if role is None:
            violations.append(ProfileViolation(rel, provider, model, f"{rel} has no role mapping under {policy.id}"))
            continue
        result = policy.validate_route(provider=provider, role=role, model=model, effort=None)
        if not result.ok:
            violations.append(
                ProfileViolation(rel, provider, model, f"{rel} uses model {model!r} for role {role!r}: {result.reason}")
            )
    return violations


# --- Migration planning -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MigrationAction:
    path: str
    provider: str
    current_model: str
    proposed_model: str | None
    action: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _candidates_for_role(policy: ModelPolicy, provider: str, role: str) -> list[str]:
    return sorted({route.model for route in policy.routes if route.provider == provider and role in route.roles})


def _plan_one(path: Path, provider: str | None, root: Path, policy: ModelPolicy) -> MigrationAction:
    rel = _relative(path, root)
    if provider is None or not is_managed_profile(path):
        return MigrationAction(
            rel, provider or "unknown", "", None, "skip_unmanaged",
            f"{rel} is not a tracked agentflow profile; migration never rewrites unmanaged user files",
        )
    model = _profile_model(path, provider) or ""
    role = _role_for_profile(path)
    if role is None:
        return MigrationAction(
            rel, provider, model, None, "skip_unresolvable",
            f"{rel} has no role mapping under {policy.id}; cannot plan a role-aware migration",
        )
    result = policy.validate_route(provider=provider, role=role, model=model, effort=None) if model else None
    if result is not None and result.ok:
        return MigrationAction(
            rel, provider, model, model, "skip_compliant",
            f"{rel} already uses an approved exact model for role {role!r}",
        )
    candidates = _candidates_for_role(policy, provider, role)
    if not candidates:
        return MigrationAction(
            rel, provider, model, None, "skip_unresolvable",
            f"{policy.id} defines no approved model for provider={provider!r} role={role!r}",
        )
    return MigrationAction(
        rel, provider, model, candidates[0], "propose_update",
        f"{rel} uses {model!r}, which is not approved for role {role!r}; "
        f"candidates under {policy.id}: {', '.join(candidates)}",
    )


def plan_migration(root: Path, policy: ModelPolicy, extra_paths: Iterable[Path] = ()) -> list[MigrationAction]:
    """Plan (never apply) model updates for shipped profiles under ``root``.

    Every managed, non-compliant profile gets a ``propose_update`` action
    naming a candidate approved model; the caller decides whether and how to
    write it. Anything outside the managed identity set -- including any
    path passed in ``extra_paths`` that is not a recognized agentflow
    profile -- is returned with action ``skip_unmanaged`` and
    ``proposed_model=None``, and this function never calls a filesystem
    write for any path.
    """

    root = Path(root)
    actions: list[MigrationAction] = []
    seen: set[Path] = set()

    for path, provider in discover_profiles(root):
        seen.add(path)
        actions.append(_plan_one(path, provider, root, policy))

    for raw_path in extra_paths:
        path = Path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if path.suffix == ".toml":
            provider: str | None = "codex"
        elif path.name.endswith(".agent.md"):
            provider = "copilot"
        elif path.suffix == ".md":
            provider = "claude"
        else:
            provider = None
        actions.append(_plan_one(path, provider, root, policy))

    return actions


def _managed_path(root: Path, relative: str) -> Path:
    """Resolve one planned path without allowing a migration escape."""

    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ModelPolicyError(f"managed profile escapes migration root: {relative}") from exc
    if not candidate.is_file():
        raise ModelPolicyError(f"managed profile is not a regular file: {relative}")
    return candidate


def _replace_profile_model(path: Path, provider: str, model: str) -> None:
    """Replace only the managed model declaration, preserving profile content."""

    text = path.read_text(encoding="utf-8")
    if provider == "codex":
        pattern = re.compile(r"(?m)^(model\s*=\s*)([\"']).*?\2\s*$")
        updated, count = pattern.subn(lambda match: f'{match.group(1)}"{model}"', text, count=1)
        if count != 1:
            raise ModelPolicyError(f"managed Codex profile has no model declaration: {path}")
    else:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ModelPolicyError(f"managed profile has no front matter: {path}")
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ModelPolicyError(f"managed profile front matter is unterminated: {path}")
        model_index = next(
            (index for index in range(1, end) if lines[index].lstrip().startswith("model:")),
            None,
        )
        replacement = f"model: {model}\n"
        if model_index is None:
            lines.insert(end, replacement)
        else:
            newline = "\n" if lines[model_index].endswith("\n") else ""
            lines[model_index] = replacement.rstrip("\n") + newline
        updated = "".join(lines)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def apply_migration(
    root: Path,
    policy: ModelPolicy,
    extra_paths: Iterable[Path] = (),
) -> list[MigrationAction]:
    """Apply the managed migration plan idempotently.

    Only shipped profiles identified by :func:`is_managed_profile` are ever
    written.  The model line is the sole mutation, so local instructions,
    permissions, and unrelated edits remain intact.
    """

    root = Path(root)
    actions = plan_migration(root, policy, extra_paths)
    for action in actions:
        if action.action != "propose_update" or action.proposed_model is None:
            continue
        path = _managed_path(root, action.path)
        provider = action.provider
        _replace_profile_model(path, provider, action.proposed_model)
    return plan_migration(root, policy, extra_paths)


# The noun is useful to callers exposing a policy ``migrate`` command.
migrate_profiles = apply_migration
