"""Protected, exact-scope action grants.

This module deliberately does not discover controller credentials or validate a
caller identity.  The controller must first authenticate its canonical,
external credential and hold its own lease fence, then pass that trusted
authority context here while it issues or checks a grant.  In particular, a
workspace, Beads record, imported feedback, or a caller-supplied key is never
an authority source for this module.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePath
import re
import secrets
import stat
import tempfile
import time
from typing import Any, Iterable, Iterator, Mapping


SCHEMA = "agentflow.action-grants@2"
_LEGACY_SCHEMA = "agentflow.action-grants@1"
GRANT_SCHEMA = "agentflow.action-grant@1"
_DOMAIN = b"agentflow:action-grant-v1:\0"
_STORE_DOMAIN = b"agentflow:action-grant-store-v2:\0"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_WILDCARD = re.compile(r"[*?\[\]{}]")


class ActionGrantError(RuntimeError):
    """A protected action grant cannot be issued or checked safely."""


class ActionGrantUnavailable(ActionGrantError):
    """The protected grant store is unavailable; no legacy fallback exists."""


class ActionGrantDenied(ActionGrantError):
    """No valid signed grant authorizes the requested exact action."""


@dataclass(frozen=True)
class AuthenticatedGrantAuthority:
    """Authority already authenticated by the leased root controller.

    ``authority_secret`` is intentionally accepted only as an opaque signing
    secret.  Constructing this value proves nothing by itself: callers must
    authenticate it against the canonical external controller credential
    and derive ``bound_worktree_roots`` from the protected controller registry
    before calling this module. The registry roots are frozen here so grant
    documents cannot expand the storage boundary. Callers must retain their
    controller fence across the later dispatch/claim operation.
    """

    workspace_root: Path | str
    workflow_root: str
    controller_id: str
    continuity_id: str
    authority_secret: str | bytes
    bound_worktree_roots: tuple[Path | str, ...] = ()

    def __post_init__(self) -> None:
        roots = self.bound_worktree_roots
        if isinstance(roots, (str, Path)):
            roots = (roots,)
        else:
            try:
                roots = tuple(roots)
            except TypeError as exc:
                raise ActionGrantError("trusted worktree roots must be an iterable") from exc
        if not all(isinstance(root, (str, Path)) for root in roots):
            raise ActionGrantError("trusted worktree roots must be path strings or Path values")
        object.__setattr__(self, "bound_worktree_roots", roots)


@dataclass(frozen=True)
class ActionGrantRequest:
    """The exact operation that must match a grant."""

    action: str
    task_id: str
    declared_paths: tuple[str | Path, ...]
    expires_at: float | None = None
    worktree_root: Path | str | None = None

    def __init__(
        self,
        *,
        action: str,
        task_id: str,
        declared_paths: Iterable[str | Path],
        expires_at: float | None = None,
        worktree_root: Path | str | None = None,
    ) -> None:
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "declared_paths", tuple(declared_paths))
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "worktree_root", worktree_root)


@dataclass(frozen=True)
class ActionGrantSummary:
    """Safe projection suitable for status output; it contains no secret."""

    workspace_root: str
    workflow_root: str
    controller_id: str
    continuity_id: str
    worktree_root: str
    action: str
    task_id: str
    declared_paths: tuple[str, ...]
    expires_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "workflow_root": self.workflow_root,
            "controller_id": self.controller_id,
            "continuity_id": self.continuity_id,
            "worktree_root": self.worktree_root,
            "action": self.action,
            "task_id": self.task_id,
            "declared_paths": list(self.declared_paths),
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ActionGrantStoreStatus:
    """Safe availability result; ``legacy`` is informational, never approval."""

    mode: str
    available: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "available": self.available, "reason": self.reason}


def legacy_action_status() -> ActionGrantStoreStatus:
    """Describe a pre-grant attempt without treating it as authorized.

    The caller owns launch history.  This helper is deliberately unable to
    turn that history into a grant or to activate protected mode retroactively.
    """

    return ActionGrantStoreStatus(
        mode="legacy", available=False,
        reason="legacy attempt has no protected action-grant authority",
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mac(secret: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(secret, _DOMAIN + _canonical_json(payload), hashlib.sha256).hexdigest()


def _store_mac(secret: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(secret, _STORE_DOMAIN + _canonical_json(payload), hashlib.sha256).hexdigest()


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or not _IDENTITY.fullmatch(value):
        raise ActionGrantError(f"{label} is malformed")
    return value


def _safe_expiry(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ActionGrantError("grant expiry must be an absolute finite epoch timestamp")
    return float(value)


def _existing_ancestor(path: Path) -> Path:
    current = path
    while True:
        try:
            current.lstat()
            return current
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ActionGrantError(
                "could not determine protected grant storage filesystem identity"
            ) from exc
        parent = current.parent
        if parent == current:
            raise ActionGrantError("protected grant storage has no existing ancestor")
        current = parent


def _reject_symlink_ancestors(path: Path) -> None:
    current = _existing_ancestor(path)
    while True:
        if current.is_symlink():
            raise ActionGrantError("protected grant storage must not use a symlink")
        if current.parent == current:
            return
        current = current.parent


def _is_within_filesystem_root(path: Path, root: Path) -> bool:
    """Check lexical and existing filesystem ancestry for a bound root.

    ``Path.relative_to`` alone misses a differently-cased path on a
    case-insensitive filesystem. Walk existing ancestors and compare their
    filesystem identities with the canonical root before creating anything.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass

    current = _existing_ancestor(path)
    while True:
        try:
            if current.samefile(root):
                return True
        except OSError as exc:
            raise ActionGrantError(
                "could not determine protected grant storage filesystem identity"
            ) from exc
        if current.parent == current:
            return False
        current = current.parent


def _canonical_workspace(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ActionGrantError("canonical workspace is unavailable") from exc
    if not resolved.is_dir():
        raise ActionGrantError("canonical workspace must be a directory")
    return resolved


def _reject_lexical_alias(raw: str, *, label: str) -> None:
    """Reject dot components before ``Path`` or ``PurePath`` normalizes them."""
    if (
        raw.endswith(os.sep)
        or f"{os.sep}{os.sep}" in raw
        or any(part in {".", ".."} for part in raw.split(os.sep))
    ):
        raise ActionGrantError(f"{label} must not contain traversal or aliases")


def _canonical_worktree(workspace: Path, value: Path | str | None) -> Path:
    if value is None:
        return workspace
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw or _WILDCARD.search(raw):
        raise ActionGrantError("worktree root is malformed or contains a wildcard")
    _reject_lexical_alias(raw, label="worktree root")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ActionGrantError("explicit worktree root must be an absolute canonical path")
    _reject_symlink_ancestors(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ActionGrantError("explicit worktree root is unavailable") from exc
    if not resolved.is_dir() or candidate.absolute() != resolved:
        raise ActionGrantError("explicit worktree root must be a canonical directory")
    return resolved


def _canonical_paths(bound_root: Path, values: Iterable[str | Path]) -> tuple[str, ...]:
    result: list[str] = []
    for raw_value in values:
        raw = os.fspath(raw_value)
        if not isinstance(raw, str) or not raw or "\x00" in raw or _WILDCARD.search(raw):
            raise ActionGrantError("declared path is malformed or contains a wildcard")
        _reject_lexical_alias(raw, label="declared path")
        lexical = PurePath(raw)
        candidate = Path(raw) if lexical.is_absolute() else bound_root / lexical
        try:
            relative = candidate.relative_to(bound_root)
        except ValueError as exc:
            raise ActionGrantError("declared path is outside the exact bound worktree root") from exc
        if not relative.parts:
            raise ActionGrantError("declared path must name a descendant of the bound worktree root")
        cursor = bound_root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ActionGrantError("declared path must not traverse a symlink")
        # ``absolute`` is lexical and therefore cannot hide a symlink already
        # rejected above. It also preserves the signed bound-root prefix.
        result.append(str(candidate.absolute()))
    if not result:
        raise ActionGrantError("at least one declared path is required")
    if len(set(result)) != len(result):
        raise ActionGrantError("declared paths must be unique")
    return tuple(sorted(result))


class ActionGrantStore:
    """A private, external store of HMAC-authenticated exact-scope grants."""

    def __init__(self, path: Path | str, authority: AuthenticatedGrantAuthority) -> None:
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ActionGrantError("protected grant store path is malformed")
        _reject_lexical_alias(raw_path, label="protected grant store path")
        self.path = Path(raw_path).expanduser()
        if not self.path.is_absolute():
            raise ActionGrantError("protected grant store path must be absolute")
        if any(part in {".", ".."} for part in self.path.parts):
            raise ActionGrantError("protected grant store path must not contain aliases")
        self.workspace = _canonical_workspace(authority.workspace_root)
        trusted_roots = [self.workspace]
        for raw_root in authority.bound_worktree_roots:
            try:
                root = _canonical_worktree(self.workspace, raw_root)
            except (TypeError, ValueError, ActionGrantError) as exc:
                raise ActionGrantError("trusted worktree root is unavailable or noncanonical") from exc
            if root not in trusted_roots:
                trusted_roots.append(root)
        self._trusted_roots = tuple(trusted_roots)
        self.workflow_root = _safe_identity(authority.workflow_root, "workflow root")
        self.controller_id = _safe_identity(authority.controller_id, "controller identity")
        self.continuity_id = _safe_identity(authority.continuity_id, "controller continuity")
        secret = authority.authority_secret
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or not secret:
            raise ActionGrantError("authenticated controller authority is required")
        self._secret = secret
        self._validate_store_location(create_directory=False)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def _validate_store_location(
        self,
        *,
        create_directory: bool,
    ) -> None:
        _reject_symlink_ancestors(self.path.parent)
        for root in self._trusted_roots:
            for protected_path in (self.path, self.lock_path):
                if _is_within_filesystem_root(protected_path, root):
                    raise ActionGrantError(
                        "protected grant store and lock must be outside the worker workspace "
                        "and every bound worktree root"
                    )
        if self.path.is_symlink():
            raise ActionGrantError("protected grant store must not use a symlink")
        try:
            lock_info = self.lock_path.lstat()
        except FileNotFoundError:
            lock_info = None
        except OSError as exc:
            raise ActionGrantError("protected grant store lock cannot be inspected") from exc
        if lock_info is not None:
            if stat.S_ISLNK(lock_info.st_mode):
                raise ActionGrantError("protected grant store lock must not use a symlink")
            if not stat.S_ISREG(lock_info.st_mode):
                raise ActionGrantError("protected grant store lock is not a regular file")
            if os.name == "posix":
                if lock_info.st_uid != os.geteuid():
                    raise ActionGrantError("protected grant store lock must be owned by this user")
                if (lock_info.st_mode & 0o777) != 0o600:
                    raise ActionGrantError("protected grant store lock permissions must be 0600")
        if create_directory:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _reject_symlink_ancestors(self.path.parent)
            os.chmod(self.path.parent, 0o700)
        if self.path.parent.exists():
            info = self.path.parent.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise ActionGrantError("protected grant store parent is not a directory")
            if os.name == "posix":
                if info.st_uid != os.geteuid():
                    raise ActionGrantError("protected grant store directory must be owned by this user")
                if (info.st_mode & 0o777) != 0o700:
                    raise ActionGrantError("protected grant store directory permissions must be 0700")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ActionGrantError("protected grant store is not a regular file")
            if os.name == "posix":
                if info.st_uid != os.geteuid():
                    raise ActionGrantError("protected grant store file must be owned by this user")
                if (info.st_mode & 0o777) != 0o600:
                    raise ActionGrantError("protected grant store file permissions must be 0600")

    @contextmanager
    def _locked(self, *, create_directory: bool) -> Iterator[None]:
        self._validate_store_location(create_directory=create_directory)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ActionGrantUnavailable("protected grant store lock is unavailable") from exc
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=True) as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._validate_store_location(create_directory=False)
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ActionGrantUnavailable("protected grant store lock is unavailable") from exc

    def _read(self, *, required: bool) -> dict[str, Any]:
        if not self.path.exists():
            if required:
                raise ActionGrantUnavailable("protected action-grant store is missing")
            return {"schema": SCHEMA, "version": 2, "grants": {}}
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (os.name == "posix" and info.st_uid != os.geteuid())
                    or (os.name == "posix" and (info.st_mode & 0o777) != 0o600)
                ):
                    raise OSError("protected grant store is not a regular file")
                with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                    value = json.load(handle, object_pairs_hook=_json_object)
            finally:
                os.close(descriptor)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ActionGrantUnavailable("protected action-grant store cannot be read") from exc
        if not isinstance(value, dict) or not isinstance(value.get("grants"), dict):
            raise ActionGrantUnavailable("protected action-grant store is malformed")
        if (
            set(value) == {"schema", "version", "grants", "hmac"}
            and value.get("schema") == SCHEMA
            and value.get("version") == 2
        ):
            payload = dict(value)
            supplied = payload.pop("hmac")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, _store_mac(self._secret, payload)):
                raise ActionGrantDenied("protected action-grant store authentication failed")
            return payload
        # Older stores authenticate each nonempty grant individually. Accept
        # those records for verification and let the next issue upgrade the
        # envelope. An unsigned empty document carries no authenticated
        # history and cannot stand in for an activated store.
        if (
            set(value) == {"schema", "version", "grants"}
            and value.get("schema") == _LEGACY_SCHEMA
            and value.get("version") == 1
            and value["grants"]
        ):
            return value
        raise ActionGrantUnavailable("protected action-grant store is malformed or unauthenticated")

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = {
            "schema": SCHEMA,
            "version": 2,
            "grants": dict(value.get("grants", {})),
        }
        signed = {**payload, "hmac": _store_mac(self._secret, payload)}
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_canonical_json(signed).decode("utf-8"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def initialize_empty(self) -> None:
        """Create the authenticated empty store exactly once during activation."""

        with self._locked(create_directory=True):
            if self.path.exists() or self.path.is_symlink():
                raise ActionGrantUnavailable("protected action-grant store already exists")
            self._write({"grants": {}})

    def _scope(self, request: ActionGrantRequest) -> dict[str, Any]:
        worktree_root = _canonical_worktree(self.workspace, request.worktree_root)
        if worktree_root not in self._trusted_roots:
            raise ActionGrantDenied(
                "worktree root is not registered in authenticated controller authority"
            )
        return {
            "workspace_root": str(self.workspace),
            "workflow_root": self.workflow_root,
            "controller_id": self.controller_id,
            "continuity_id": self.continuity_id,
            "worktree_root": str(worktree_root),
            "action": _safe_identity(request.action, "action category"),
            "task_id": _safe_identity(request.task_id, "task identity"),
            "declared_paths": list(_canonical_paths(worktree_root, request.declared_paths)),
            "expires_at": _safe_expiry(request.expires_at),
        }

    @staticmethod
    def _summary(payload: Mapping[str, Any]) -> ActionGrantSummary:
        return ActionGrantSummary(
            workspace_root=str(payload["workspace_root"]),
            workflow_root=str(payload["workflow_root"]), controller_id=str(payload["controller_id"]),
            continuity_id=str(payload["continuity_id"]), worktree_root=str(payload["worktree_root"]),
            action=str(payload["action"]),
            task_id=str(payload["task_id"]), declared_paths=tuple(payload["declared_paths"]),
            expires_at=payload["expires_at"],
        )

    def _validated_payload(self, grant_id: str, raw: Any) -> dict[str, Any]:
        fields = {
            "schema", "grant_id", "workspace_root", "workflow_root", "controller_id",
            "continuity_id", "worktree_root", "action", "task_id", "declared_paths", "expires_at", "hmac",
        }
        if not isinstance(raw, Mapping) or set(raw) != fields or not isinstance(grant_id, str):
            raise ActionGrantDenied("protected action grant is malformed")
        payload = dict(raw)
        supplied = payload.pop("hmac")
        if (
            not all(isinstance(payload.get(field), str) for field in (
                "schema", "grant_id", "workspace_root", "workflow_root", "controller_id",
                "continuity_id", "worktree_root", "action", "task_id",
            ))
            or not isinstance(supplied, str)
        ):
            raise ActionGrantDenied("protected action grant is malformed")
        if (
            payload.get("schema") != GRANT_SCHEMA
            or payload.get("grant_id") != grant_id
            or not secrets.compare_digest(supplied, _mac(self._secret, payload))
        ):
            raise ActionGrantDenied("protected action grant authentication failed")
        if not isinstance(payload.get("declared_paths"), list):
            raise ActionGrantDenied("protected action grant is malformed")
        try:
            canonical = self._scope(ActionGrantRequest(
                action=str(payload["action"]), task_id=str(payload["task_id"]),
                declared_paths=tuple(payload["declared_paths"]), expires_at=payload["expires_at"],
                worktree_root=str(payload["worktree_root"]),
            ))
        except (KeyError, TypeError, ActionGrantError) as exc:
            raise ActionGrantDenied("protected action grant is malformed") from exc
        if (
            canonical["declared_paths"] != payload.get("declared_paths")
            or canonical["expires_at"] != payload.get("expires_at")
            or canonical["worktree_root"] != payload.get("worktree_root")
        ):
            raise ActionGrantDenied("protected action grant is not canonical")
        if (
            payload.get("workspace_root") != str(self.workspace)
            or payload.get("workflow_root") != self.workflow_root
            or payload.get("controller_id") != self.controller_id
            or payload.get("continuity_id") != self.continuity_id
        ):
            raise ActionGrantDenied("protected action grant belongs to a different controller scope")
        self._validate_store_location(create_directory=False)
        return payload

    def issue(
        self, request: ActionGrantRequest, *, initialize_if_missing: bool = True,
    ) -> ActionGrantSummary:
        """Sign and atomically retain one exact scope grant.

        The controller must hold its fence around this call and its later
        dispatch.  This store lock only serializes store updates; it cannot
        authenticate a caller or substitute for the controller lease fence.
        """

        scope = self._scope(request)
        payload = {"schema": GRANT_SCHEMA, "grant_id": secrets.token_hex(24), **scope}
        payload["hmac"] = _mac(self._secret, payload)
        with self._locked(create_directory=True):
            document = self._read(required=not initialize_if_missing)
            for grant_id, raw in document["grants"].items():
                self._validated_payload(grant_id, raw)
            document["grants"][payload["grant_id"]] = payload
            self._write(document)
        return self._summary(scope)

    def require(self, request: ActionGrantRequest, *, now: float | None = None) -> ActionGrantSummary:
        """Require a currently valid grant with precisely the requested scope.

        Absence, unreadability, a bad MAC, a foreign authority context, and an
        expired grant all deny the action.  There is intentionally no legacy
        or metadata-controlled fallback. ``request.expires_at`` is used when
        issuing a grant; dispatch verification deliberately ignores a
        caller's expiry and enforces the signed expiry retained in the store.
        """

        scope = self._scope(request)
        with self._locked(create_directory=False):
            document = self._read(required=True)
            matches: list[dict[str, Any]] = []
            for grant_id, raw in document["grants"].items():
                payload = self._validated_payload(grant_id, raw)
                if all(
                    payload.get(key) == value
                    for key, value in scope.items()
                    if key != "expires_at"
                ):
                    matches.append(payload)
            # Time is deliberately sampled only after the protected store has
            # been read and matched, while its lock remains held. Sampling
            # before a contended lock can otherwise accept a grant that expires
            # while the caller waits to verify it.
            instant = time.time() if now is None else float(now)
            if not math.isfinite(instant):
                raise ActionGrantError("verification time must be finite")
            if not matches:
                raise ActionGrantDenied("no protected action grant matches this exact scope")
            valid = [item for item in matches if item["expires_at"] is None or instant < item["expires_at"]]
            if not valid:
                raise ActionGrantDenied("matching protected action grant is expired")
            # Multiple independently issued grants for the same scope are harmless;
            # selecting a stable one makes safe status output deterministic.
            return self._summary(sorted(valid, key=lambda item: item["grant_id"])[0])

    def status(self) -> ActionGrantStoreStatus:
        """Return availability without granting authority or exposing secrets."""

        try:
            self._validate_store_location(create_directory=False)
            document = self._read(required=True)
            for grant_id, raw in document["grants"].items():
                self._validated_payload(grant_id, raw)
        except ActionGrantError as exc:
            return ActionGrantStoreStatus(mode="unavailable", available=False, reason=str(exc))
        return ActionGrantStoreStatus(mode="protected", available=True)


__all__ = [
    "ActionGrantDenied", "ActionGrantError", "ActionGrantRequest", "ActionGrantStore",
    "ActionGrantStoreStatus", "ActionGrantSummary", "ActionGrantUnavailable",
    "AuthenticatedGrantAuthority", "legacy_action_status",
]
