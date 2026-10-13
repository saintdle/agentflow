"""Controller-owned activation and dispatch for protected action grants.

This module is the trust boundary above :mod:`agentflow.action_grants`.
It loads the canonical external controller credential itself, proves that its
resume secret still authenticates the lease held under ``RootController``'s
fence, and only then exposes the separately signed worktree registry and
grant store. Beads records, imported feedback, and caller-provided metadata
are never consulted for authority.

The registry is installed once as a complete root set. Protected mode has no
downgrade operation. ``execute`` keeps the root-controller fence held until
the supplied operation returns; its callback must perform work that assumes
that fence is already held, rather than calling a controller method that
tries to acquire the same non-reentrant lock again.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar

from agentflow import action_grants
from agentflow.action_grants import (
    ActionGrantDenied,
    ActionGrantError,
    ActionGrantRequest,
    ActionGrantStore,
    ActionGrantSummary,
    AuthenticatedGrantAuthority,
)
from agentflow.controller import FencedLease, Lease, RootController


_MODE_SCHEMA = "agentflow.controller-action-mode@1"
_REGISTRY_SCHEMA = "agentflow.controller-worktree-registry@1"
_ACTIVATION_SCHEMA = "agentflow.controller-action-activation@1"
_ANCHOR_SCHEMA = "agentflow.controller-action-anchor@1"
_AUTH_DOMAIN = b"agentflow:controller-action-state-v1:\0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


class ControllerActionError(RuntimeError):
    """Protected controller action authority cannot proceed safely."""


class ControllerActionDenied(ControllerActionError):
    """The requested controller action is outside current protected authority."""


class ControllerActionUnavailable(ControllerActionError):
    """The canonical controller authority or protected state is unavailable."""


@dataclass(frozen=True)
class GrantedActionStatus:
    """Secret-free projection of one stored exact-scope grant."""

    state: str
    summary: ActionGrantSummary

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "scope": self.summary.to_dict()}


@dataclass(frozen=True)
class ControllerActionStatus:
    """Safe status projection; it contains no credential or lease material."""

    mode: str
    available: bool
    reason: str = ""
    registered_worktree_roots: tuple[str, ...] = ()
    grants: tuple[GrantedActionStatus, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "available": self.available,
            "reason": self.reason,
            "registered_worktree_roots": list(self.registered_worktree_roots),
            "grants": [grant.to_dict() for grant in self.grants],
        }


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _mac(secret: str, value: Mapping[str, Any]) -> str:
    return hmac.new(secret.encode("utf-8"), _AUTH_DOMAIN + _canonical_json(value), hashlib.sha256).hexdigest()


def _signed(secret: str, value: Mapping[str, Any]) -> dict[str, Any]:
    signed = dict(value)
    signed["authority_hmac"] = _mac(secret, value)
    return signed


def _verify_signed(secret: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerActionUnavailable("protected controller action state is malformed")
    supplied = value.get("authority_hmac")
    payload = dict(value)
    payload.pop("authority_hmac", None)
    try:
        expected = _mac(secret, payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControllerActionUnavailable("protected controller action state is malformed") from exc
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise ControllerActionUnavailable("protected controller action state authentication failed")
    return payload


def validate_credential_action_anchor(
    anchor: Any,
    *,
    authority_secret: str,
    workspace_root: str,
    workflow_root: str,
    controller_id: str,
    continuity_id: str,
) -> dict[str, Any] | None:
    """Validate the credential-resident proof of protected activation.

    ``None`` means the credential predates protected activation. Any present
    value must be a complete authenticated commitment for this exact
    controller incarnation.
    """

    if anchor is None:
        return None
    payload = _verify_signed(authority_secret, anchor)
    expected = {
        "schema", "version", "state", "workspace_root", "workflow_root",
        "controller_id", "continuity_id", "registry_sha256", "mode_sha256",
    }
    if (
        set(payload) != expected
        or payload.get("schema") != _ANCHOR_SCHEMA
        or payload.get("version") != 1
        or payload.get("state") not in {"pending", "active"}
        or payload.get("workspace_root") != workspace_root
        or payload.get("workflow_root") != workflow_root
        or payload.get("controller_id") != controller_id
        or payload.get("continuity_id") != continuity_id
        or not isinstance(payload.get("registry_sha256"), str)
        or not _SHA256.fullmatch(payload["registry_sha256"])
        or not isinstance(payload.get("mode_sha256"), str)
        or not _SHA256.fullmatch(payload["mode_sha256"])
    ):
        raise ControllerActionUnavailable("protected controller activation anchor is malformed or foreign")
    return dict(anchor)


def _canonical_external_file(value: Path | str, *, label: str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ControllerActionUnavailable(f"canonical {label} path is malformed")
    try:
        action_grants._reject_lexical_alias(raw, label=label)
    except ActionGrantError as exc:
        raise ControllerActionUnavailable(f"canonical {label} path is not normalized") from exc
    candidate = action_grants._canonical_system_alias(Path(raw).expanduser())
    if not candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
        raise ControllerActionUnavailable(f"canonical {label} path must be absolute and normalized")
    try:
        action_grants._reject_symlink_ancestors(candidate.parent)
        resolved = candidate.resolve(strict=True)
    except (OSError, ActionGrantError) as exc:
        raise ControllerActionUnavailable(f"canonical {label} is unavailable") from exc
    if candidate.absolute() != resolved or not resolved.is_file():
        raise ControllerActionUnavailable(f"canonical {label} must be a regular file without path aliases")
    return resolved


def _read_private_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        action_grants._reject_symlink_ancestors(path.parent)
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode):
            raise OSError("parent is not a directory")
        if os.name == "posix" and (
            parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise OSError("parent permissions are not private")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise OSError("file ownership or permissions are not private")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                value = json.load(handle, object_pairs_hook=_json_object)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ControllerActionUnavailable(f"canonical {label} cannot be read safely") from exc
    if not isinstance(value, dict):
        raise ControllerActionUnavailable(f"canonical {label} is malformed")
    return value


def _read_json_record(path: Path, *, label: str) -> dict[str, Any] | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControllerActionUnavailable(f"{label} cannot be inspected") from exc
    if path.is_symlink():
        raise ControllerActionUnavailable(f"{label} must not be a symlink")
    return _read_private_json(path, label=label)


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(value).decode("utf-8"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def credential_write_lock(path: Path | str) -> Iterator[None]:
    """Serialize canonical credential read/validate/write transactions."""

    credential = action_grants._canonical_system_alias(Path(path).expanduser())
    parent = credential.parent
    try:
        action_grants._reject_symlink_ancestors(parent)
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        info = parent.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (os.name == "posix" and info.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise OSError("credential directory is not private")
        lock_path = credential.with_name(f".{credential.name}.credential.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        lock_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or (os.name == "posix" and lock_info.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(lock_info.st_mode) != 0o600)
        ):
            os.close(descriptor)
            raise OSError("credential lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (OSError, ActionGrantError) as exc:
        raise ControllerActionUnavailable("canonical controller credential lock is unavailable") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ControllerActionAuthority:
    """Authenticate an owned root controller before protected action access.

    ``credential_path`` is the exact external credential path selected by
    the controller CLI. The helper reads it itself and verifies its current
    workspace, workflow, continuity and resume proof under the controller's
    lease fence. Protected state is deterministically kept beside that
    credential in a private ``action-authority`` directory.
    """

    def __init__(
        self,
        controller: RootController,
        lease: Lease,
        *,
        workflow_root: str,
        credential_path: Path | str,
        canonical_credential_path: Path | str | None = None,
    ) -> None:
        if not isinstance(controller, RootController) or not isinstance(lease, Lease):
            raise TypeError("a RootController and its current Lease are required")
        self.controller = controller
        self.lease = lease
        self.workflow_root = workflow_root
        self.selected_credential_path = action_grants._canonical_system_alias(
            Path(credential_path).expanduser()
        )
        self.credential_path = action_grants._canonical_system_alias(
            Path(canonical_credential_path or credential_path).expanduser()
        )
        self._workspace = action_grants._canonical_workspace(controller.root)
        if not isinstance(workflow_root, str) or not workflow_root or workflow_root.strip() != workflow_root:
            raise ControllerActionUnavailable("workflow root identity is malformed")
        try:
            action_grants._safe_identity(workflow_root, "workflow root")
        except ActionGrantError as exc:
            raise ControllerActionUnavailable("workflow root identity is malformed") from exc
        scope_hash = hashlib.sha256(
            f"{self._workspace}\0{workflow_root}".encode("utf-8")
        ).hexdigest()[:32]
        self.state_directory = self.credential_path.parent / "action-authority" / scope_hash
        # Keep the sticky marker separate from the registry/grant directory:
        # removing one protected-state subtree must still leave evidence that
        # this workflow was activated and therefore cannot fall back to legacy.
        self.mode_path = self.credential_path.with_name(
            f".{self.credential_path.name}.{scope_hash}.protected-mode.json"
        )
        self.activation_path = self.credential_path.with_name(
            f".{self.credential_path.name}.{scope_hash}.protected-activation.json"
        )
        self.registry_path = self.state_directory / "worktree-registry.json"
        self.lock_path = self.state_directory / ".controller-action-authority.lock"
        self.grants_path = self.state_directory / "action-grants.json"

    def _canonical_credentials_path(self) -> Path:
        path = _canonical_external_file(self.credential_path, label="controller credential")
        if action_grants._is_within_filesystem_root(path, self._workspace):
            raise ControllerActionUnavailable("canonical controller credential must be outside the worker workspace")
        return path

    def _canonical_selected_credentials_path(self) -> Path:
        return _canonical_external_file(
            self.selected_credential_path, label="selected controller credential"
        )

    def _validate_storage_location(self, roots: Iterable[Path | str] = ()) -> tuple[Path, ...]:
        canonical_roots = [self._workspace]
        try:
            for raw_root in roots:
                root = action_grants._canonical_worktree(self._workspace, raw_root)
                if root not in canonical_roots:
                    canonical_roots.append(root)
        except (TypeError, ValueError, ActionGrantError) as exc:
            raise ControllerActionUnavailable("trusted worktree registry is unavailable or noncanonical") from exc

        try:
            action_grants._reject_symlink_ancestors(self.state_directory.parent)
        except ActionGrantError as exc:
            raise ControllerActionUnavailable("protected action storage cannot use a symlink") from exc
        for root in canonical_roots:
            for protected_path in (
                self.credential_path,
                self.credential_path.with_name(f".{self.credential_path.name}.credential.lock"),
                self.selected_credential_path,
                self.selected_credential_path.with_name(
                    f".{self.selected_credential_path.name}.credential.lock"
                ),
                self.state_directory,
                self.mode_path,
                self.activation_path,
                self.registry_path,
                self.lock_path,
                self.grants_path,
                self.grants_path.with_name(f".{self.grants_path.name}.lock"),
            ):
                try:
                    if action_grants._is_within_filesystem_root(protected_path, root):
                        raise ControllerActionUnavailable(
                            "canonical credential, protected state, grant store, and locks must be outside "
                            "the workspace and every registered worktree"
                        )
                except ActionGrantError as exc:
                    raise ControllerActionUnavailable("protected storage filesystem identity is unavailable") from exc

        self._canonical_selected_credentials_path()
        if self.credential_path.exists() and not self.credential_path.is_symlink():
            self._canonical_credentials_path()
        else:
            try:
                if self.credential_path.is_symlink():
                    raise ControllerActionUnavailable("canonical controller credential must not be a symlink")
                action_grants._reject_symlink_ancestors(self.credential_path.parent)
            except ActionGrantError as exc:
                raise ControllerActionUnavailable("canonical controller credential path is unsafe") from exc
        if self.state_directory.exists():
            try:
                info = self.state_directory.lstat()
            except OSError as exc:
                raise ControllerActionUnavailable("protected action state directory cannot be inspected") from exc
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700)
            ):
                raise ControllerActionUnavailable("protected action state directory ownership or permissions are unsafe")
        storage_parent = self.state_directory.parent
        if storage_parent.exists():
            try:
                info = storage_parent.lstat()
            except OSError as exc:
                raise ControllerActionUnavailable("protected action storage parent cannot be inspected") from exc
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700)
            ):
                raise ControllerActionUnavailable("protected action storage parent ownership or permissions are unsafe")
        return tuple(canonical_roots)

    def _credentials(
        self,
        current: Lease,
        *,
        allow_missing_canonical: bool = False,
    ) -> tuple[str, Path, dict[str, Any] | None, bool]:
        selected_path = self._canonical_selected_credentials_path()
        selected = _read_private_json(selected_path, label="selected controller credential")

        def validate_record(record: Mapping[str, Any], *, selected_record: bool) -> dict[str, Any] | None:
            if (
                record.get("schema") != "agentflow.controller_credentials"
                or record.get("version") != 2
                or record.get("workspace_root") != str(self._workspace)
                or record.get("workflow_root") != self.workflow_root
                or record.get("continuity_id") != current.continuity_id
            ):
                raise ControllerActionUnavailable("controller credential does not match the current workflow incarnation")
            if not isinstance(record.get("authority_secret"), str) or not record.get("authority_secret"):
                raise ControllerActionUnavailable("controller authority credential is unavailable")
            anchor = None
            if "protected_action_anchor" in record:
                if record["protected_action_anchor"] is None:
                    raise ControllerActionUnavailable("protected controller activation anchor is malformed")
                anchor = validate_credential_action_anchor(
                    record["protected_action_anchor"],
                    authority_secret=str(record["authority_secret"]),
                    workspace_root=str(self._workspace),
                    workflow_root=self.workflow_root,
                    controller_id=current.controller,
                    continuity_id=current.continuity_id,
                )
            if selected_record:
                resume_secret = record.get("resume_secret")
                proof_is_current = (
                    isinstance(resume_secret, str)
                    and bool(resume_secret)
                    and current.verify_resume_proof(resume_secret)
                )
                authority_only = (
                    resume_secret == ""
                    and anchor is not None
                    and anchor.get("state") == "active"
                )
                if not proof_is_current and not authority_only:
                    raise ControllerActionUnavailable("selected controller credential does not authenticate the current lease")
            return anchor

        selected_anchor = validate_record(selected, selected_record=True)
        if selected_path == self.credential_path:
            return str(selected["authority_secret"]), selected_path, selected_anchor, False

        try:
            self.credential_path.lstat()
        except FileNotFoundError:
            if not allow_missing_canonical or selected_anchor is not None:
                raise ControllerActionUnavailable("canonical controller credential is missing")
            if any(
                path.exists() or path.is_symlink()
                for path in (self.mode_path, self.activation_path, self.grants_path)
            ):
                raise ControllerActionUnavailable("canonical credential is missing after protected state exists")
            if self.registry_path.exists() or self.registry_path.is_symlink():
                # Registration writes the signed immutable root set before
                # creating a custom-key mirror. Only that exact pre-activation
                # state can be resumed if the mirror write was interrupted.
                self._read_registry(str(selected["authority_secret"]), current)
            elif self.state_directory.exists() or self.state_directory.is_symlink():
                raise ControllerActionUnavailable("partial protected action state prevents canonical credential recovery")
            return str(selected["authority_secret"]), self.credential_path, None, True
        except OSError as exc:
            raise ControllerActionUnavailable("canonical controller credential cannot be inspected") from exc

        canonical_path = self._canonical_credentials_path()
        canonical = _read_private_json(canonical_path, label="canonical controller credential")
        canonical_anchor = validate_record(canonical, selected_record=False)
        if (
            not hmac.compare_digest(
                str(selected["authority_secret"]), str(canonical["authority_secret"]),
            )
            or selected_anchor != canonical_anchor
            or ("protected_action_anchor" in selected) != ("protected_action_anchor" in canonical)
        ):
            raise ControllerActionUnavailable("selected and canonical controller credentials disagree")
        return str(canonical["authority_secret"]), canonical_path, canonical_anchor, False

    @contextmanager
    def _authenticated_fence(
        self,
        *,
        allow_missing_canonical: bool = False,
    ) -> Iterator[tuple[Lease, str, dict[str, Any] | None, bool]]:
        try:
            with self.controller.fence(self.lease) as current:
                if (
                    current.root != str(self._workspace)
                    or current.controller != self.controller.controller
                    or current.controller != self.lease.controller
                    or not current.continuity_id
                    or current.continuity_id != self.lease.continuity_id
                ):
                    raise ControllerActionUnavailable("controller lease identity is not current")
                secret, _path, anchor, missing_canonical = self._credentials(
                    current, allow_missing_canonical=allow_missing_canonical,
                )
                yield current, secret, anchor, missing_canonical
        except FencedLease as exc:
            raise ControllerActionUnavailable("controller lease is no longer current") from exc

    def _persist_initial_canonical_credential(self, current: Lease, secret: str) -> None:
        """Create the authority-only default copy after roots passed preflight."""

        canonical = self.credential_path
        selected = self._canonical_selected_credentials_path()
        if canonical == selected:
            return
        with ExitStack() as locks:
            for path in sorted((canonical, selected), key=str):
                locks.enter_context(credential_write_lock(path))
            try:
                canonical.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ControllerActionUnavailable("canonical controller credential appeared during registration")
            credentials = _read_private_json(selected, label="selected controller credential")
            if (
                credentials.get("schema") != "agentflow.controller_credentials"
                or credentials.get("version") != 2
                or credentials.get("workspace_root") != str(self._workspace)
                or credentials.get("workflow_root") != self.workflow_root
                or credentials.get("continuity_id") != current.continuity_id
                or credentials.get("authority_secret") != secret
                or "protected_action_anchor" in credentials
                or not isinstance(credentials.get("resume_secret"), str)
                or not current.verify_resume_proof(credentials["resume_secret"])
            ):
                raise ControllerActionUnavailable("selected controller credential changed during registration")
            try:
                _atomic_private_json(canonical, {
                    "schema": "agentflow.controller_credentials",
                    "version": 2,
                    "resume_secret": "",
                    "authority_secret": secret,
                    "continuity_id": current.continuity_id,
                    "workspace_root": str(self._workspace),
                    "workflow_root": self.workflow_root,
                })
            except OSError as exc:
                raise ControllerActionUnavailable("canonical controller credential could not be committed") from exc

    def _ensure_directory(self, roots: Iterable[Path | str]) -> None:
        self._validate_storage_location(roots)
        storage_parent = self.state_directory.parent
        if not storage_parent.exists():
            try:
                storage_parent.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ControllerActionUnavailable("protected action storage parent cannot be created") from exc
        if not self.state_directory.exists():
            try:
                self.state_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ControllerActionUnavailable("protected action state directory cannot be created") from exc
        self._validate_storage_location(roots)

    @contextmanager
    def _metadata_lock(self, roots: Iterable[Path | str], *, create: bool) -> Iterator[None]:
        if create:
            self._ensure_directory(roots)
        else:
            self._validate_storage_location(roots)
        try:
            self.lock_path.lstat()
        except FileNotFoundError:
            if not create:
                raise ControllerActionUnavailable("protected controller action lock is missing")
        except OSError as exc:
            raise ControllerActionUnavailable("protected controller action lock cannot be inspected") from exc
        else:
            info = self.lock_path.lstat()
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_uid != os.geteuid())
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise ControllerActionUnavailable("protected controller action lock ownership or permissions are unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ControllerActionUnavailable("protected controller action lock is unavailable") from exc
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=True) as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (os.name == "posix" and info.st_uid != os.geteuid())
                    or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
                ):
                    raise ControllerActionUnavailable(
                        "protected controller action lock ownership or permissions are unsafe"
                    )
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._validate_storage_location(roots)
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ControllerActionUnavailable("protected controller action lock is unavailable") from exc

    def _scope(self, current: Lease) -> dict[str, str]:
        return {
            "workspace_root": str(self._workspace),
            "workflow_root": self.workflow_root,
            "controller_id": current.controller,
            "continuity_id": current.continuity_id,
        }

    def _activation_anchor(
        self,
        secret: str,
        current: Lease,
        *,
        state: str,
        registry_digest: str,
        mode_digest: str,
    ) -> dict[str, Any]:
        return _signed(secret, {
            "schema": _ANCHOR_SCHEMA,
            "version": 1,
            "state": state,
            **self._scope(current),
            "registry_sha256": registry_digest,
            "mode_sha256": mode_digest,
        })

    def _persist_activation_anchor(
        self,
        secret: str,
        current: Lease,
        anchor: dict[str, Any],
    ) -> None:
        canonical_path = self._canonical_credentials_path()
        selected_path = self._canonical_selected_credentials_path()
        paths = tuple(sorted(set((canonical_path, selected_path)), key=str))
        with ExitStack() as locks:
            for path in paths:
                locks.enter_context(credential_write_lock(path))
            canonical = _read_private_json(canonical_path, label="canonical controller credential")
            selected = (
                canonical
                if selected_path == canonical_path
                else _read_private_json(selected_path, label="selected controller credential")
            )

            def validate(record: Mapping[str, Any], *, selected_record: bool) -> dict[str, Any] | None:
                if (
                    record.get("schema") != "agentflow.controller_credentials"
                    or record.get("version") != 2
                    or record.get("workspace_root") != str(self._workspace)
                    or record.get("workflow_root") != self.workflow_root
                    or record.get("continuity_id") != current.continuity_id
                    or record.get("authority_secret") != secret
                ):
                    raise ControllerActionUnavailable("controller credential changed during activation")
                if selected_record and (
                    not isinstance(record.get("resume_secret"), str)
                    or not current.verify_resume_proof(record["resume_secret"])
                ):
                    raise ControllerActionUnavailable("selected controller credential changed during activation")
                if "protected_action_anchor" not in record:
                    return None
                if record["protected_action_anchor"] is None:
                    raise ControllerActionUnavailable("protected controller activation anchor is malformed")
                return validate_credential_action_anchor(
                    record["protected_action_anchor"],
                    authority_secret=secret,
                    workspace_root=str(self._workspace),
                    workflow_root=self.workflow_root,
                    controller_id=current.controller,
                    continuity_id=current.continuity_id,
                )

            canonical_anchor = validate(canonical, selected_record=canonical_path == selected_path)
            selected_anchor = canonical_anchor if canonical_path == selected_path else validate(
                selected, selected_record=True,
            )
            if (
                canonical_anchor != selected_anchor
                or ("protected_action_anchor" in canonical)
                != ("protected_action_anchor" in selected)
            ):
                raise ControllerActionUnavailable("selected and canonical activation anchors disagree")
            target_payload = _verify_signed(secret, anchor)
            if target_payload.get("state") == "pending":
                if canonical_anchor is not None or selected_anchor is not None:
                    raise ControllerActionUnavailable("protected controller activation is already committed")
            elif target_payload.get("state") == "active":
                if canonical_anchor is None or selected_anchor is None:
                    raise ControllerActionUnavailable("pending protected controller activation anchor is missing")
                existing_payload = _verify_signed(secret, canonical_anchor)
                if (
                    existing_payload.get("state") != "pending"
                    or {
                        key: value for key, value in existing_payload.items() if key != "state"
                    } != {
                        key: value for key, value in target_payload.items() if key != "state"
                    }
                ):
                    raise ControllerActionUnavailable("pending protected controller activation anchor changed")
            else:
                raise ControllerActionUnavailable("protected controller activation state is malformed")

            canonical["protected_action_anchor"] = anchor
            _atomic_private_json(canonical_path, canonical)
            if selected_path != canonical_path:
                selected["protected_action_anchor"] = anchor
                _atomic_private_json(selected_path, selected)

    def _read_registry(self, secret: str, current: Lease) -> tuple[dict[str, Any], tuple[str, ...]]:
        value = _read_json_record(self.registry_path, label="protected worktree registry")
        if value is None:
            raise ControllerActionUnavailable("complete protected worktree registry is missing")
        payload = _verify_signed(secret, value)
        expected = {"schema", "version", "complete", "workspace_root", "workflow_root", "controller_id", "continuity_id", "roots"}
        if (
            set(payload) != expected
            or payload.get("schema") != _REGISTRY_SCHEMA
            or payload.get("version") != 1
            or payload.get("complete") is not True
            or any(payload.get(key) != val for key, val in self._scope(current).items())
            or not isinstance(payload.get("roots"), list)
            or not payload["roots"]
            or not all(isinstance(root, str) for root in payload["roots"])
        ):
            raise ControllerActionUnavailable("complete protected worktree registry is malformed or foreign")
        canonical: list[str] = []
        try:
            for raw_root in payload["roots"]:
                root = action_grants._canonical_worktree(self._workspace, raw_root)
                if str(root) != raw_root or str(root) in canonical:
                    raise ActionGrantError("registry roots are noncanonical or duplicated")
                canonical.append(str(root))
        except ActionGrantError as exc:
            raise ControllerActionUnavailable("complete protected worktree registry is noncanonical") from exc
        for index, first in enumerate(canonical):
            first_path = Path(first)
            for second in canonical[index + 1:]:
                second_path = Path(second)
                if first_path in second_path.parents or second_path in first_path.parents:
                    raise ControllerActionUnavailable("complete protected worktree registry contains overlapping roots")
        roots = tuple(sorted(canonical))
        if list(roots) != payload["roots"]:
            raise ControllerActionUnavailable("complete protected worktree registry is not canonical")
        return payload, roots

    def _require_mode(
        self,
        secret: str,
        current: Lease,
        anchor: dict[str, Any] | None,
        *,
        allow_pending: bool = False,
    ) -> tuple[tuple[str, ...], ActionGrantStore]:
        if anchor is None:
            raise ControllerActionUnavailable("protected controller activation anchor is missing")
        registry_payload, roots = self._read_registry(secret, current)
        mode_value = _read_json_record(self.mode_path, label="protected controller mode marker")
        activation_value = _read_json_record(self.activation_path, label="protected controller activation receipt")
        if mode_value is None or activation_value is None:
            raise ControllerActionUnavailable("protected controller mode has not been activated")
        mode_payload = _verify_signed(secret, mode_value)
        expected = {"schema", "version", "mode", "workspace_root", "workflow_root", "controller_id", "continuity_id", "registry_sha256", "activated_at"}
        registry_digest = hashlib.sha256(_canonical_json(_signed(secret, registry_payload))).hexdigest()
        activated_at = mode_payload.get("activated_at")
        if (
            set(mode_payload) != expected
            or mode_payload.get("schema") != _MODE_SCHEMA
            or mode_payload.get("version") != 1
            or mode_payload.get("mode") != "protected"
            or any(mode_payload.get(key) != val for key, val in self._scope(current).items())
            or mode_payload.get("registry_sha256") != registry_digest
            or isinstance(activated_at, bool)
            or not isinstance(activated_at, (int, float))
            or not math.isfinite(float(activated_at))
        ):
            raise ControllerActionUnavailable("protected controller mode marker is malformed, foreign, or downgraded")
        activation_payload = _verify_signed(secret, activation_value)
        activation_expected = {
            "schema", "version", "workspace_root", "workflow_root", "controller_id",
            "continuity_id", "mode_sha256",
        }
        mode_digest = hashlib.sha256(_canonical_json(mode_value)).hexdigest()
        if (
            set(activation_payload) != activation_expected
            or activation_payload.get("schema") != _ACTIVATION_SCHEMA
            or activation_payload.get("version") != 1
            or any(activation_payload.get(key) != val for key, val in self._scope(current).items())
            or activation_payload.get("mode_sha256") != mode_digest
        ):
            raise ControllerActionUnavailable("protected controller activation receipt is malformed or downgraded")
        anchor_payload = _verify_signed(secret, anchor)
        allowed_states = {"active", "pending"} if allow_pending else {"active"}
        if (
            anchor_payload.get("state") not in allowed_states
            or anchor_payload.get("registry_sha256") != registry_digest
            or anchor_payload.get("mode_sha256") != mode_digest
        ):
            raise ControllerActionUnavailable("protected controller activation anchor does not match its metadata")
        self._validate_storage_location(roots)
        authority = AuthenticatedGrantAuthority(
            workspace_root=self._workspace,
            workflow_root=self.workflow_root,
            controller_id=current.controller,
            continuity_id=current.continuity_id,
            authority_secret=secret,
            bound_worktree_roots=roots,
        )
        try:
            store = ActionGrantStore(
                self.grants_path,
                authority,
                require_authenticated_envelope=True,
            )
        except ActionGrantError as exc:
            raise ControllerActionUnavailable("protected action grant storage is unavailable") from exc
        store_status = store.status()
        if not store_status.available:
            raise ControllerActionUnavailable("protected action grant storage is unavailable")
        return roots, store

    @staticmethod
    def _registered_request_root(request: ActionGrantRequest, workspace: Path, roots: tuple[str, ...]) -> str:
        try:
            root = action_grants._canonical_worktree(workspace, request.worktree_root)
        except ActionGrantError as exc:
            raise ControllerActionDenied("requested worktree root is not canonical") from exc
        if str(root) not in roots:
            raise ControllerActionDenied("requested worktree root is not registered in protected controller authority")
        return str(root)

    def register_worktrees(self, worktree_roots: Iterable[Path | str]) -> tuple[str, ...]:
        """Persist the complete immutable worktree registry under the current lease.

        Repeating the exact registration is idempotent. Adding, removing, or
        replacing roots requires a new controller incarnation and therefore
        cannot silently broaden an existing protected registry.
        """

        if isinstance(worktree_roots, (str, Path)):
            raise ControllerActionDenied("worktree registration must provide the complete root iterable")
        try:
            raw_roots = tuple(worktree_roots)
        except TypeError as exc:
            raise ControllerActionDenied("worktree registration must provide the complete root iterable") from exc
        if not raw_roots:
            raise ControllerActionDenied("complete worktree registration must contain at least one root")
        with self._authenticated_fence(allow_missing_canonical=True) as (current, secret, anchor, missing_canonical):
            protected_roots = None
            if anchor is not None:
                anchor_payload = _verify_signed(secret, anchor)
                if anchor_payload.get("state") != "active":
                    raise ControllerActionUnavailable("pending protected controller activation cannot be reinitialized")
                protected_roots, _store = self._require_mode(secret, current, anchor)
            canonical: list[str] = []
            try:
                for raw_root in raw_roots:
                    root = action_grants._canonical_worktree(self._workspace, raw_root)
                    if str(root) in canonical:
                        raise ActionGrantError("duplicate worktree root")
                    canonical.append(str(root))
            except (TypeError, ValueError, ActionGrantError) as exc:
                raise ControllerActionDenied("worktree registration contains an alias, duplicate, or unavailable root") from exc
            for index, first in enumerate(canonical):
                first_path = Path(first)
                for second in canonical[index + 1:]:
                    second_path = Path(second)
                    if first_path in second_path.parents or second_path in first_path.parents:
                        raise ControllerActionDenied("complete worktree registration must not contain overlapping roots")
            roots = tuple(sorted(canonical))
            if protected_roots is not None:
                if protected_roots != roots:
                    raise ControllerActionDenied("protected worktree registry is immutable")
                return protected_roots
            if self.registry_path.exists() or self.registry_path.is_symlink():
                _payload, registered_roots = self._read_registry(secret, current)
                if registered_roots != roots:
                    raise ControllerActionDenied("protected worktree registry is immutable")
            self._validate_storage_location(roots)
            if missing_canonical:
                # Create only the already-validated canonical credential
                # directory/lock before the metadata directory; the signed
                # registry still precedes the first secret mirror write.
                with credential_write_lock(self.credential_path):
                    pass
            had_state_directory = self.state_directory.exists()
            with self._metadata_lock(roots, create=True):
                registry_value = _read_json_record(self.registry_path, label="protected worktree registry")
                mode_value = _read_json_record(self.mode_path, label="protected controller mode marker")
                activation_value = _read_json_record(
                    self.activation_path, label="protected controller activation receipt"
                )
                grants_exists = self.grants_path.exists() or self.grants_path.is_symlink()
                if registry_value is None and mode_value is None and grants_exists:
                    raise ControllerActionUnavailable("orphaned action grants prevent registry registration")
                if registry_value is not None:
                    _payload, existing = self._read_registry(secret, current)
                    if existing != roots:
                        raise ControllerActionDenied("protected worktree registry is immutable")
                    if mode_value is not None or activation_value is not None:
                        self._require_mode(secret, current, anchor)
                    if missing_canonical:
                        if grants_exists or mode_value is not None or activation_value is not None:
                            raise ControllerActionUnavailable("canonical credential cannot be recovered after activation state exists")
                        self._persist_initial_canonical_credential(current, secret)
                    return existing
                if mode_value is not None or activation_value is not None or grants_exists:
                    raise ControllerActionUnavailable("orphaned protected state prevents registry registration")
                # A directory/lock that predates this registration without a
                # registry is evidence of interrupted or removed state. Do not
                # silently reconstruct a supposedly immutable root set.
                if had_state_directory:
                    raise ControllerActionUnavailable("partial protected action state prevents registry registration")
                payload = {
                    "schema": _REGISTRY_SCHEMA,
                    "version": 1,
                    "complete": True,
                    **self._scope(current),
                    "roots": list(roots),
                }
                _atomic_private_json(self.registry_path, _signed(secret, payload))
                if missing_canonical:
                    self._persist_initial_canonical_credential(current, secret)
                return roots

    def _fresh_root_before_activation(self) -> None:
        # A pre-existing checkpoint may carry a claim, a completed attempt,
        # or history that this component cannot safely retroactively classify.
        # Requiring its absence is deliberately stricter than inferring from
        # mutable checkpoint fields.
        if self.lease.epoch != 1:
            raise ControllerActionDenied(
                "protected controller mode can only be activated in the first lease epoch"
            )
        try:
            action_grants._reject_symlink_ancestors(self.controller.checkpoint_path.parent)
            self.controller.checkpoint_path.lstat()
        except FileNotFoundError:
            return
        except (OSError, ActionGrantError) as exc:
            raise ControllerActionUnavailable("controller checkpoint cannot be inspected before activation") from exc
        raise ControllerActionDenied(
            "protected controller mode can only be activated before a checkpoint or task attempt exists"
        )

    def activate(self) -> None:
        """Make protected mode sticky for this controller incarnation."""

        with self._authenticated_fence() as (current, secret, anchor, _missing_canonical):
            if anchor is not None:
                anchor_payload = _verify_signed(secret, anchor)
                if anchor_payload.get("state") == "active":
                    self._require_mode(secret, current, anchor)
                    return
                raise ControllerActionUnavailable("pending protected controller activation cannot be resumed")
            registry_payload, roots = self._read_registry(secret, current)
            self._validate_storage_location(roots)
            with self._metadata_lock(roots, create=False):
                existing = _read_json_record(self.mode_path, label="protected controller mode marker")
                activation_receipt = _read_json_record(
                    self.activation_path, label="protected controller activation receipt"
                )
                if existing is not None or activation_receipt is not None:
                    self._require_mode(secret, current, anchor)
                self._fresh_root_before_activation()
                if self.grants_path.exists() or self.grants_path.is_symlink():
                    raise ControllerActionUnavailable("orphaned action grants prevent protected mode activation")
                registry_digest = hashlib.sha256(
                    _canonical_json(_signed(secret, registry_payload))
                ).hexdigest()
                payload = {
                    "schema": _MODE_SCHEMA,
                    "version": 1,
                    "mode": "protected",
                    **self._scope(current),
                    "registry_sha256": registry_digest,
                    "activated_at": float(time.time()),
                }
                signed_mode = _signed(secret, payload)
                mode_digest = hashlib.sha256(_canonical_json(signed_mode)).hexdigest()
                pending_anchor = self._activation_anchor(
                    secret, current, state="pending",
                    registry_digest=registry_digest, mode_digest=mode_digest,
                )
                self._persist_activation_anchor(secret, current, pending_anchor)
                _atomic_private_json(self.mode_path, signed_mode)
                receipt = {
                    "schema": _ACTIVATION_SCHEMA,
                    "version": 1,
                    **self._scope(current),
                    "mode_sha256": mode_digest,
                }
                _atomic_private_json(self.activation_path, _signed(secret, receipt))
                grant_authority = AuthenticatedGrantAuthority(
                    workspace_root=self._workspace,
                    workflow_root=self.workflow_root,
                    controller_id=current.controller,
                    continuity_id=current.continuity_id,
                    authority_secret=secret,
                    bound_worktree_roots=roots,
                )
                store = ActionGrantStore(self.grants_path, grant_authority)
                store.initialize_empty()
                self._require_mode(secret, current, pending_anchor, allow_pending=True)
                active_anchor = self._activation_anchor(
                    secret, current, state="active",
                    registry_digest=registry_digest, mode_digest=mode_digest,
                )
                self._persist_activation_anchor(secret, current, active_anchor)
                self._require_mode(secret, current, active_anchor)

    def issue(self, request: ActionGrantRequest) -> ActionGrantSummary:
        """Explicitly issue one exact action grant under the current root fence."""

        with self._authenticated_fence() as (current, secret, anchor, _missing_canonical):
            roots, store = self._require_mode(secret, current, anchor)
            self._registered_request_root(request, self._workspace, roots)
            self._validate_storage_location(roots)
            try:
                return store.issue(request, initialize_if_missing=False)
            except ActionGrantError as exc:
                raise ControllerActionDenied(str(exc)) from exc

    def require(self, request: ActionGrantRequest) -> ActionGrantSummary:
        """Require an existing exact action grant while holding the root fence."""

        with self._authenticated_fence() as (current, secret, anchor, _missing_canonical):
            roots, store = self._require_mode(secret, current, anchor)
            self._registered_request_root(request, self._workspace, roots)
            try:
                return store.require(request)
            except ActionGrantError as exc:
                raise ControllerActionDenied(str(exc)) from exc

    def execute(self, request: ActionGrantRequest, operation: Callable[[], _T]) -> _T:
        """Run ``operation`` only while its exact grant and current lease hold.

        The callback runs with the controller fence still held. A denied or
        unavailable request never calls it.
        """

        if not callable(operation):
            raise TypeError("protected action operation must be callable")
        with self._authenticated_fence() as (current, secret, anchor, _missing_canonical):
            roots, store = self._require_mode(secret, current, anchor)
            self._registered_request_root(request, self._workspace, roots)
            try:
                store.require(request)
            except ActionGrantError as exc:
                raise ControllerActionDenied(str(exc)) from exc
            return operation()

    def status(self) -> ControllerActionStatus:
        """Return a read-only, secret-free projection of mode and stored scopes."""

        try:
            with self._authenticated_fence() as (current, secret, anchor, _missing_canonical):
                self._validate_storage_location()
                registry_value = _read_json_record(self.registry_path, label="protected worktree registry")
                mode_value = _read_json_record(self.mode_path, label="protected controller mode marker")
                activation_value = _read_json_record(
                    self.activation_path, label="protected controller activation receipt"
                )
                grants_exists = self.grants_path.exists() or self.grants_path.is_symlink()
                lock_exists = self.lock_path.exists() or self.lock_path.is_symlink()
                if (
                    registry_value is None and mode_value is None and activation_value is None
                    and not grants_exists and not lock_exists and not self.state_directory.exists()
                    and anchor is None
                ):
                    return ControllerActionStatus(
                        mode="legacy", available=False,
                        reason="protected controller action mode has not been activated",
                    )
                if registry_value is None or mode_value is None:
                    raise ControllerActionUnavailable("protected controller action state is incomplete")
                roots, store = self._require_mode(secret, current, anchor)
                try:
                    document = store._read(required=True)
                    summaries: list[GrantedActionStatus] = []
                    now = float(time.time())
                    if not math.isfinite(now):
                        raise ControllerActionUnavailable("system time is unavailable")
                    for grant_id, raw in document["grants"].items():
                        payload = store._validated_payload(grant_id, raw)
                        summary = store._summary(payload)
                        expires_at = payload["expires_at"]
                        state = "expired" if expires_at is not None and now >= expires_at else "valid"
                        summaries.append(GrantedActionStatus(state=state, summary=summary))
                except ActionGrantError as exc:
                    raise ControllerActionUnavailable("protected action grant store is unavailable") from exc
                return ControllerActionStatus(
                    mode="protected", available=True,
                    registered_worktree_roots=roots,
                    grants=tuple(sorted(
                        summaries,
                        key=lambda item: (
                            item.summary.worktree_root,
                            item.summary.action,
                            item.summary.task_id,
                            item.summary.declared_paths,
                            item.summary.expires_at is None,
                            item.summary.expires_at or 0.0,
                        ),
                    )),
                )
        except (ControllerActionError, FencedLease, ActionGrantError, OSError, ValueError):
            return ControllerActionStatus(
                mode="unavailable", available=False,
                reason="canonical controller authority or protected action state is unavailable",
            )


__all__ = [
    "ControllerActionAuthority", "ControllerActionDenied", "ControllerActionError",
    "ControllerActionStatus", "ControllerActionUnavailable", "GrantedActionStatus",
    "validate_credential_action_anchor",
]
