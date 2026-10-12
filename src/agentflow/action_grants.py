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


SCHEMA = "agentflow.action-grants@1"
GRANT_SCHEMA = "agentflow.action-grant@1"
_DOMAIN = b"agentflow:action-grant-v1:\0"
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
    before calling this module, and must retain their controller fence across
    the later dispatch/claim operation.
    """

    workspace_root: Path | str
    workflow_root: str
    controller_id: str
    continuity_id: str
    authority_secret: str | bytes


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
    while not current.exists() and not current.is_symlink():
        parent = current.parent
        if parent == current:
            raise ActionGrantError("protected grant storage has no existing ancestor")
        current = parent
    return current


def _reject_symlink_ancestors(path: Path) -> None:
    current = _existing_ancestor(path)
    while True:
        if current.is_symlink():
            raise ActionGrantError("protected grant storage must not use a symlink")
        if current.parent == current:
            return
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
    if any(part in {".", ".."} for part in raw.split(os.sep)):
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
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ActionGrantError("protected grant store path must be absolute")
        if any(part in {".", ".."} for part in self.path.parts):
            raise ActionGrantError("protected grant store path must not contain aliases")
        self.workspace = _canonical_workspace(authority.workspace_root)
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

    def _validate_store_location(self, *, create_directory: bool) -> None:
        _reject_symlink_ancestors(self.path.parent)
        try:
            self.path.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise ActionGrantError("protected grant store must be outside the worker workspace")
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ActionGrantError("protected grant store must not use a symlink")
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
            return {"schema": SCHEMA, "version": 1, "grants": {}}
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
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "version", "grants"}
            or value.get("schema") != SCHEMA
            or value.get("version") != 1
            or not isinstance(value.get("grants"), dict)
        ):
            raise ActionGrantUnavailable("protected action-grant store is malformed")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_canonical_json(value).decode("utf-8"))
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

    def _scope(self, request: ActionGrantRequest) -> dict[str, Any]:
        worktree_root = _canonical_worktree(self.workspace, request.worktree_root)
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
        return payload

    def issue(self, request: ActionGrantRequest) -> ActionGrantSummary:
        """Sign and atomically retain one exact scope grant.

        The controller must hold its fence around this call and its later
        dispatch.  This store lock only serializes store updates; it cannot
        authenticate a caller or substitute for the controller lease fence.
        """

        scope = self._scope(request)
        payload = {"schema": GRANT_SCHEMA, "grant_id": secrets.token_hex(24), **scope}
        payload["hmac"] = _mac(self._secret, payload)
        with self._locked(create_directory=True):
            document = self._read(required=False)
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
            with self._locked(create_directory=False):
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
