"""Durable, single-writer control for one approved Agentflow root.

The controller is intentionally provider-neutral.  It persists the lease and
the pre-launch claim before invoking a scheduler callback, so a process crash
cannot make a later resume silently launch the same task twice.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping
import uuid

from agentflow import checkpoint
from agentflow import session_control


TERMINAL_STATES = frozenset({"completed", "failed", "blocked", "halted", "terminal"})


class ControllerError(RuntimeError):
    """A controller operation cannot safely proceed."""


class LeaseConflict(ControllerError):
    """Another live controller owns this root, or the caller is fenced."""


class DuplicateController(LeaseConflict):
    """A second live controller attempted to acquire the same root."""


class DuplicateSupervisor(ControllerError):
    """A supervisor process already owns this root's OS lock."""


class StaleLease(LeaseConflict):
    """A stale lease exists and takeover was not explicitly requested."""


class FencedLease(LeaseConflict):
    """The lease epoch/token is no longer current."""


class RootViolation(ControllerError):
    """A scheduler input is not an exact descendant of the approved root."""


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _authority_mac(secret: str, value: Mapping[str, Any], *, domain: str) -> str:
    if not secret:
        raise ControllerError("controller authority secret is required")
    payload = dict(value)
    payload.pop("authority_hmac", None)
    encoded = (
        f"agentflow:{domain}:".encode("utf-8")
        + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Lease:
    root: str
    controller: str
    epoch: int
    token: str
    acquired_at: float
    heartbeat_at: float
    owner_id: str = ""
    # Private reattach credential. Unlike ``token`` (derived, guessable
    # root/controller/epoch), this is a random secret generated fresh on
    # every new epoch and rotated on every reattach and takeover; it never
    # appears in to_dict()'s public projection or in the on-disk state.
    # ``resume_secret`` is populated only in-memory, on the instance that
    # just (re)acquired the lease, so a caller can hand it to the operator
    # once (e.g. write it to a protected 0600 key file). Only its SHA-256
    # digest is ever persisted (``resume_secret_hash``, in to_storage_dict()),
    # so reading the state file alone -- accidentally shared, backed up, or
    # attached to a support bundle -- never yields a usable credential.
    resume_secret: str = ""
    resume_secret_hash: str = ""
    # Continuity identity for one controller incarnation. Unlike ``controller``
    # (a reusable operator-chosen name) or ``epoch``/``token`` (which advance on
    # every legitimate reattach as well as a takeover), ``continuity_id`` is
    # minted once per incarnation, PRESERVED across an authenticated
    # resume/reattach, and ROTATED only when a different owner takes the lease
    # over. It is the unambiguous "same controller incarnation" identity a
    # return contract binds to (AFREL): a result issued by one incarnation must
    # not be consumable after a different owner has taken the reusable name.
    continuity_id: str = ""

    @property
    def root_id(self) -> str:
        return self.root

    @property
    def controller_id(self) -> str:
        return self.controller

    def to_dict(self) -> dict[str, Any]:
        """Public-safe projection: never includes the private resume secret or its hash."""
        return {
            "root": self.root,
            "controller": self.controller,
            "epoch": self.epoch,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "owner_id": self.owner_id,
            "continuity_id": self.continuity_id,
        }

    def to_storage_dict(self) -> dict[str, Any]:
        """Persisted projection: only the secret's hash, never the plaintext."""
        value = self.to_dict()
        value["resume_secret_hash"] = self.resume_secret_hash
        return value

    def verify_resume_proof(self, proof: str) -> bool:
        if not proof or not self.resume_secret_hash:
            return False
        return hmac.compare_digest(_hash_secret(proof), self.resume_secret_hash)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Lease":
        try:
            return cls(
                root=str(value["root"]),
                controller=str(value["controller"]),
                epoch=int(value["epoch"]),
                token=str(value["token"]),
                acquired_at=float(value["acquired_at"]),
                heartbeat_at=float(value["heartbeat_at"]),
                owner_id=str(value.get("owner_id") or ""),
                # The plaintext secret is never stored; only its hash round-trips.
                resume_secret="",
                resume_secret_hash=str(value.get("resume_secret_hash") or ""),
                continuity_id=str(value.get("continuity_id") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerError("controller state contains an invalid lease") from exc


@dataclass(frozen=True)
class ResumeResult:
    state: str
    task: str = ""
    session_id: str = ""
    dispatched: bool = False
    halted: bool = False
    resumed: bool = False
    checkpoint: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "task": self.task,
            "session_id": self.session_id,
            "dispatched": self.dispatched,
            "halted": self.halted,
            "resumed": self.resumed,
            "terminal": self.terminal,
            "checkpoint": self.checkpoint,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _active_tasks_from_checkpoint(document: Mapping[str, Any], root: str) -> list[dict[str, str]]:
    """Read the bounded task collection, upgrading a live v1/v2 slot in memory.

    Old checkpoints stored one task in the top-level pointer. A live pointer is
    never discarded: on the next fenced write it is copied into ``active_tasks``
    before another claim can be reserved.
    """
    rows = document.get("active_tasks")
    if isinstance(rows, list) and rows:
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    task_id = str(document.get("task") or "")
    state = checkpoint.resume_state(dict(document))
    if (
        task_id and task_id != root
        and state in {"claimed_no_session", "running", "launched", "identity_pending"}
    ):
        return [{
            "task": task_id,
            "phase": str(document.get("phase") or ""),
            "actor": str(document.get("actor") or ""),
            "claim_id": str(document.get("claim_id") or ""),
            "session_id": str(document.get("session_id") or ""),
            "state": state,
        }]
    return []


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = -1
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot read controller state: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError("controller state must be an object")
    return value


def _root_key(root: str) -> str:
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:20]


class RootController:
    """Own and resume one approved root.

    ``state_path`` is shared by controller instances for a root.  The lock is
    held across each lease read/modify/write transaction; the epoch in the
    durable record fences an old process after an explicit stale takeover.
    """

    def __init__(
        self,
        root: str | Path,
        controller: str,
        *,
        state_path: Path | None = None,
        checkpoint_path: Path | None = None,
        supervisor_lock_path: Path | None = None,
        stale_after: float = 300.0,
        clock: Callable[[], float] = time.time,
        owner_id: str | None = None,
    ) -> None:
        if not str(root):
            raise ControllerError("root is required")
        if not controller:
            raise ControllerError("controller is required")
        if stale_after <= 0:
            raise ControllerError("stale_after must be positive")
        self.root = str(root)
        self.controller = controller
        self.stale_after = float(stale_after)
        self.clock = clock
        self.owner_id = owner_id or uuid.uuid4().hex
        if state_path is None:
            base = Path.cwd() / ".agentflow" / "controller"
            state_path = base / f"{_root_key(self.root)}.json"
        self.state_path = Path(state_path)
        self.checkpoint_path = Path(checkpoint_path or self.state_path.with_suffix(".checkpoint.json"))
        self.lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")
        self.supervisor_lock_path = (
            Path(supervisor_lock_path)
            if supervisor_lock_path
            else self.state_path.with_name(f".{self.state_path.name}.supervisor.lock")
        )
        self._lease: Lease | None = None

    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @contextmanager
    def supervisor_lock(self):
        """Hold the one process-level supervisor lock for this root.

        The file is deliberately separate from the lease transaction lock:
        short status/credential operations remain available while one
        supervised controller polls.  The kernel releases this lock if its
        process exits, so restart requires an explicit command and protected
        lease credential rather than a time-based takeover.
        """
        self.supervisor_lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self.supervisor_lock_path.open("a+", encoding="utf-8")
        try:
            os.fchmod(handle.fileno(), 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise DuplicateSupervisor(
                        f"a supervisor is already running for root {self.root}"
                    ) from exc
                raise
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _read_lease(self, state: Mapping[str, Any]) -> Lease | None:
        value = state.get("lease")
        return Lease.from_dict(value) if isinstance(value, Mapping) else None

    def _write_state(self, state: dict[str, Any]) -> None:
        state["schema"] = "agentflow.controller"
        state["version"] = 1
        _atomic_json(self.state_path, state)

    def _current_lease(self) -> Lease:
        with self._locked():
            lease = self._read_lease(_read_json(self.state_path))
        if lease is None:
            raise FencedLease("controller has no active lease")
        return lease

    def acquire(self, *, takeover: bool = False, resume_proof: str = "") -> Lease:
        now = float(self.clock())
        with self._locked():
            state = _read_json(self.state_path)
            previous = self._read_lease(state)
            if previous is not None:
                age = max(0.0, now - previous.heartbeat_at)
                stale = age > self.stale_after
                reattach_proof_valid = bool(
                    previous.controller == self.controller
                    and previous.verify_resume_proof(resume_proof)
                )
                if reattach_proof_valid:
                    # The private resume secret -- never the guessable public
                    # token -- is the sole proof that authorizes reattaching
                    # to a live lease. AFREL-028/AFREL-032: both the resume
                    # secret AND the public owner-bound fencing identity
                    # (epoch + token) rotate on every successful reattach,
                    # not just an explicit takeover. The public token is a
                    # bare string wherever it crosses a process boundary
                    # (herdr_launch's --lease, _controller_fence) with no
                    # way to carry owner_id alongside it; leaving it
                    # unchanged across a reattach let a displaced owner's
                    # remembered token string keep authorizing
                    # heartbeat/release/fence/launch forever. Bumping the
                    # epoch and minting a fresh token here means a stale
                    # token immediately stops matching current.token.
                    new_secret = secrets.token_urlsafe(32)
                    epoch = previous.epoch + 1
                    token = f"{self.root}/{self.controller}/{epoch}"
                    lease = Lease(
                        previous.root,
                        previous.controller,
                        epoch,
                        token,
                        previous.acquired_at,
                        now,
                        self.owner_id,
                        new_secret,
                        _hash_secret(new_secret),
                        # An authenticated reattach is the SAME incarnation:
                        # the continuity identity is preserved (never a fresh
                        # one) even though epoch/token/secret all rotate, so a
                        # return contract bound to it before the reattach is
                        # still consumable by the resumed controller.
                        continuity_id=previous.continuity_id or uuid.uuid4().hex,
                    )
                    state["epoch"] = epoch
                    state["lease"] = lease.to_storage_dict()
                    self._write_state(state)
                    self._lease = lease
                    return lease
                if previous.controller == self.controller and not stale:
                    if previous.owner_id == self.owner_id:
                        self._lease = previous
                        return previous
                    raise DuplicateController(
                        f"root {self.root} is already owned by controller {previous.controller}"
                    )
                if not stale:
                    raise DuplicateController(
                        f"root {self.root} is leased by controller {previous.controller}"
                    )
                if not takeover:
                    raise StaleLease(
                        f"root {self.root} has a stale lease; pass takeover=True explicitly"
                    )
                epoch = previous.epoch + 1
            else:
                epoch = int(state.get("epoch", 0)) + 1
            token = f"{self.root}/{self.controller}/{epoch}"
            # A fresh, cryptographically random resume secret is minted for
            # every new epoch (first acquire or takeover) -- it is never
            # derived from root/controller/epoch, so it cannot be guessed
            # the way the public token can.
            resume_secret = secrets.token_urlsafe(32)
            lease = Lease(
                self.root, self.controller, epoch, token, now, now, self.owner_id,
                resume_secret, _hash_secret(resume_secret),
                # A first acquire or a different-owner takeover is a NEW
                # incarnation: mint a fresh continuity identity so any result
                # issued by a previous incarnation of the same reusable
                # controller name no longer matches and cannot be consumed.
                continuity_id=uuid.uuid4().hex,
            )
            state["epoch"] = epoch
            state["lease"] = lease.to_storage_dict()
            self._write_state(state)
            self._lease = lease
            return lease

    def _lease_matches(self, current: Lease, expected: Lease | str) -> bool:
        if isinstance(expected, str):
            return current.token == expected
        # AFREL-032: owner_id is always required to match, never bypassed
        # for an "expected" Lease that happens to carry an empty owner_id --
        # every legitimate caller's expected Lease comes from a real
        # acquire()/reattach and always has one.
        return (
            current.root == expected.root
            and current.controller == expected.controller
            and current.epoch == expected.epoch
            and current.token == expected.token
            and current.owner_id == expected.owner_id
        )

    def assert_lease(self, lease: Lease | str | None = None) -> Lease:
        expected = lease or self._lease
        if expected is None:
            raise FencedLease("controller lease is required")
        current = self._current_lease()
        if not self._lease_matches(current, expected) or current.root != self.root:
            raise FencedLease("controller lease is no longer current")
        return current

    def authorize(self, resume_proof: str) -> Lease:
        """Authenticate a sideband command or a lock-protected restart.

        Progress recording and rotation-packet generation are side-band
        controller operations.  They may share the current controller lease
        when the caller proves possession of its private resume secret; unlike
        ``acquire(resume_proof=...)`` this does not rotate epoch, token, owner,
        secret, or continuity identity and therefore does not interrupt a live
        autonomous controller heartbeat.  A supervised process restart also
        uses this path, but only after acquiring ``supervisor_lock()``; every
        current root runner takes that same lock before lease authentication.
        """

        if not resume_proof:
            raise LeaseConflict("controller resume proof is required")
        with self._locked():
            current = self._read_lease(_read_json(self.state_path))
        if (
            current is None
            or current.root != self.root
            or current.controller != self.controller
            or not current.verify_resume_proof(resume_proof)
        ):
            raise LeaseConflict("controller resume proof is invalid")
        self._lease = current
        return current

    def heartbeat(self, lease: Lease | str | None = None) -> Lease:
        """Renew the heartbeat as one lease-check-and-write transaction.

        AFREL-028: a prior implementation called ``assert_lease`` (which
        locks, reads, and unlocks) and only then re-locked to write the
        renewal -- a displaced owner could win a race in that gap and
        resurrect itself over a legitimate takeover. The comparison and the
        write now share one held lock.
        """
        expected = lease or self._lease
        if expected is None:
            raise FencedLease("controller lease is required")
        now = float(self.clock())
        with self._locked():
            current = self._read_lease(_read_json(self.state_path))
            if current is None or not self._lease_matches(current, expected) or current.root != self.root:
                raise FencedLease("controller lease is no longer current")
            state = _read_json(self.state_path)
            renewed = Lease(
                current.root,
                current.controller,
                current.epoch,
                current.token,
                current.acquired_at,
                now,
                current.owner_id,
                "",
                current.resume_secret_hash,
                continuity_id=current.continuity_id,
            )
            state["lease"] = renewed.to_storage_dict()
            self._write_state(state)
            self._lease = renewed
            return renewed

    def release(self, lease: Lease | str | None = None) -> None:
        """Release the lease as one lease-check-and-write transaction (AFREL-028)."""
        expected = lease or self._lease
        if expected is None:
            raise FencedLease("controller lease is required")
        with self._locked():
            current = self._read_lease(_read_json(self.state_path))
            if current is None or not self._lease_matches(current, expected) or current.root != self.root:
                raise FencedLease("controller lease is no longer current")
            state = _read_json(self.state_path)
            state["epoch"] = current.epoch
            state["lease"] = None
            self._write_state(state)
            self._lease = None

    @contextmanager
    def fence(self, lease: Lease | str | None = None):
        """Hold the controller lock across an external critical section.

        Re-verifies ``lease`` (or the retained lease) at entry, exactly like
        ``assert_lease``, but never releases the lock until the ``with``
        block exits. A concurrent ``acquire(takeover=True)``, ``heartbeat``,
        ``release``, or ``_save_checkpoint`` from another process shares the
        same lock file and therefore blocks for the duration -- closing the
        AFREL-027 gap where authority was proven, the lock released, and
        only then the actual spawn/commit happened, leaving a window a
        takeover could win. Lock order is always controller-then-Herdr:
        callers acquire this fence before taking any Herdr-side lock, never
        the reverse.
        """
        expected = lease or self._lease
        if expected is None:
            raise FencedLease("controller lease is required")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            current = self._read_lease(_read_json(self.state_path))
            if current is None or not self._lease_matches(current, expected) or current.root != self.root:
                raise FencedLease("controller lease is no longer current")
            self._lease = current
            yield current
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def approve_waiver(
        self,
        *,
        workflow_root: str,
        task: str,
        acceptance_id: str,
        approval_ref: str,
        approved_by: str,
        approved_at: str,
        reason: str = "",
        authority_secret: str,
        lease: Lease | str | None = None,
    ) -> dict[str, Any]:
        """Record a waiver only through the currently fenced controller.

        Beads metadata names an approver but cannot authenticate that identity
        to Agentflow. This controller-owned record is the independent
        authorization. It is bound to the exact workflow/task/row/reference
        and to the controller incarnation that wrote it.
        """
        for name, value in (
            ("workflow_root", workflow_root), ("task", task),
            ("acceptance_id", acceptance_id), ("approval_ref", approval_ref),
            ("approved_by", approved_by), ("approved_at", approved_at),
        ):
            if not str(value).strip():
                raise ControllerError(f"waiver approval {name} is required")
        with self.fence(lease) as current:
            state = _read_json(self.state_path)
            record = {
                "schema": "agentflow.controller-waiver@1",
                "workflow_root": workflow_root,
                "task": task,
                "acceptance_id": acceptance_id,
                "approval_ref": approval_ref,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "reason": reason,
                "controller_id": current.controller,
                "continuity_id": current.continuity_id,
                "epoch": current.epoch,
            }
            record["authority_hmac"] = _authority_mac(
                authority_secret, record, domain="waiver-approval-v1"
            )
            approvals = state.get("waiver_approvals")
            if not isinstance(approvals, list):
                approvals = []
            approvals = [
                item for item in approvals
                if not isinstance(item, Mapping)
                or not (
                    str(item.get("workflow_root") or "") == workflow_root
                    and str(item.get("task") or "") == task
                    and str(item.get("acceptance_id") or "") == acceptance_id
                    and str(item.get("approval_ref") or "") == approval_ref
                )
            ]
            approvals.append(record)
            state["waiver_approvals"] = approvals
            self._write_state(state)
            return dict(record)

    def _assert_task_root(self, task: Mapping[str, Any]) -> str:
        task_id = str(task.get("task") or task.get("task_id") or task.get("id") or "")
        task_root = task.get("root", task.get("root_id", task.get("workflow_root")))
        if not task_id or str(task_root or "") != self.root:
            raise RootViolation(
                f"task {task_id or '<unknown>'} is outside approved root {self.root}"
            )
        if task.get("ready") is False or task.get("blocked") is True:
            raise ControllerError(f"task {task_id} is not ready")
        return task_id

    def _base_checkpoint(self) -> dict[str, Any]:
        return checkpoint.build_checkpoint(
            {
                "task": self.root,
                "phase": "controller",
                "next_action": "schedule",
                "root": self.root,
                "controller": self.controller,
                "state": "idle",
                "status": "idle",
            }
        )

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return self._base_checkpoint()
        return checkpoint.load_checkpoint(self.checkpoint_path)

    def _save_checkpoint(
        self, document: dict[str, Any], *, lease: Lease | str | None = None
    ) -> dict[str, Any]:
        """Write ``document`` as one lease-fenced transaction.

        The lease re-verification and the checkpoint replacement happen
        under the same held controller-state lock, closing the TOCTOU gap
        where a prior implementation checked the lease, released the lock,
        and only then wrote the checkpoint -- leaving a window in which a
        takeover could land between the check and the write. Holding the
        lock across both means a takeover during that window blocks on the
        same lock instead of racing it.
        """
        if lease is None:
            return checkpoint.write_checkpoint(self.checkpoint_path, document)
        with self.fence(lease):
            return checkpoint.write_checkpoint(self.checkpoint_path, document)

    def active_tasks(self) -> list[dict[str, str]]:
        """Return current workers, preserving a legacy single-task checkpoint."""
        return _active_tasks_from_checkpoint(self._load_checkpoint(), self.root)

    def _checkpoint_active_tasks(
        self,
        document: dict[str, Any],
        rows: list[dict[str, str]],
        current: Lease,
    ) -> dict[str, Any]:
        draining = document.get("state") == "draining" or document.get("status") == "draining"
        next_action = (
            "drain active task results" if draining and rows
            else "finalize blocked root after draining" if draining
            else "await active task results" if rows
            else "select next ready task"
        )
        document.update(
            {
                "task": self.root,
                "phase": "controller",
                "next_action": next_action,
                "root": self.root,
                "controller": self.controller,
                "actor": "",
                "claim_id": "",
                "session_id": "",
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": "draining" if draining else "running" if rows else "advancing",
                "status": "draining" if draining else "running" if rows else "advancing",
                "terminal": False,
                "active_tasks": rows,
            }
        )
        return checkpoint.write_checkpoint(self.checkpoint_path, document)

    def migrate_active_tasks(
        self, *, lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Fenced migration of a live v1/v2 single slot into the v3 collection."""
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            already_current = isinstance(document.get("active_tasks"), list) and bool(document.get("active_tasks"))
            legacy_live = bool(rows) and not already_current
            if legacy_live:
                return self._result(self._checkpoint_active_tasks(document, rows, current), resumed=True)
            return self._result(document, resumed=True)

    def reserve_active_task(
        self, task: Mapping[str, Any], *, lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Durably reserve one exact task before any provider launch attempt."""
        task_id = self._assert_task_root(task)
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            if any(item["task"] == task_id for item in rows):
                raise ControllerError(f"task {task_id} is already present in the active task set")
            if len(rows) >= checkpoint.MAX_ACTIVE_TASKS:
                raise ControllerError("active task set reached its safe checkpoint limit")
            row = {
                "task": task_id,
                "phase": str(task.get("phase") or "dispatch"),
                "actor": str(task.get("actor") or task.get("assignee") or current.controller),
                "claim_id": str(task.get("claim_id") or task.get("claim") or ""),
                "session_id": "",
                "state": "claimed_no_session",
            }
            rows.append(row)
            return self._result(self._checkpoint_active_tasks(document, rows, current), resumed=False)

    def bind_active_task(
        self,
        task_id: str,
        *,
        session_id: str,
        state: str = "running",
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Bind an already-reserved task to the session returned by Herdr."""
        if state not in {"running", "launched", "identity_pending"}:
            raise ControllerError(f"unsupported active task launch state {state!r}")
        if state != "identity_pending" and not session_id:
            raise ControllerError("a launched active task requires a provider session id")
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            matches = [item for item in rows if item["task"] == task_id]
            if len(matches) != 1 or matches[0]["state"] != "claimed_no_session":
                raise ControllerError(f"task {task_id} has no unique pending launch reservation")
            matches[0]["session_id"] = session_id
            matches[0]["state"] = state
            return self._result(self._checkpoint_active_tasks(document, rows, current), dispatched=True)

    def complete_active_task(
        self, task_id: str, *, lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Remove one task only after its authenticated result is dispositioned."""
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            remaining = [item for item in rows if item["task"] != task_id]
            if len(remaining) == len(rows):
                raise ControllerError(f"task {task_id} is not present in the active task set")
            return self._result(self._checkpoint_active_tasks(document, remaining, current), resumed=True)

    def begin_draining(
        self,
        reason: str,
        *,
        failed_task: str = "",
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Persist a failure and stop new launches while active siblings drain."""
        reason = str(reason).strip()
        if not reason:
            raise ControllerError("drain reason is required")
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            if failed_task:
                rows = [item for item in rows if item["task"] != failed_task]
            existing = str(document.get("terminal_reason") or "")
            if existing and reason not in existing:
                terminal_reason = f"{existing}; {reason}"[:500]
            else:
                terminal_reason = existing or reason
            document.update({
                "root": self.root,
                "controller": self.controller,
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": "draining",
                "status": "draining",
                "terminal": False,
                "terminal_reason": terminal_reason,
                "next_action": "drain active task results" if rows else "finalize blocked root after draining",
                "active_tasks": rows,
            })
            saved = checkpoint.write_checkpoint(self.checkpoint_path, document)
            return self._result(saved, resumed=True)

    def _result(self, document: Mapping[str, Any], *, dispatched: bool = False, resumed: bool = False) -> ResumeResult:
        state = checkpoint.resume_state(dict(document))
        return ResumeResult(
            state=state,
            task=str(document.get("task") or ""),
            session_id=str(document.get("session_id") or ""),
            dispatched=dispatched,
            halted=state in TERMINAL_STATES,
            resumed=resumed,
            checkpoint=dict(document),
        )

    def resume(
        self,
        tasks: Iterable[Mapping[str, Any]] = (),
        *,
        dispatch: Callable[[Mapping[str, Any]], Any] | None = None,
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Resume once; never relaunch a claimed task without a session.

        The selected task is written as ``claimed_no_session`` before
        ``dispatch`` runs.  A crash in or before the provider launch therefore
        leaves an auditable halt for an operator instead of an implicit retry.
        """

        current_lease = self.assert_lease(lease)
        document = self._load_checkpoint()
        state = checkpoint.resume_state(document)
        if state in TERMINAL_STATES:
            return self._result(document, resumed=True)
        if state in {"claimed", "claimed_no_session", "running", "launched", "identity_pending"}:
            return self._result(document, resumed=True)

        candidate_rows = [dict(task) for task in tasks]
        for candidate in candidate_rows:
            self._assert_task_root(candidate)
        candidates = sorted(
            candidate_rows,
            key=lambda item: str(item.get("task") or item.get("task_id") or item.get("id") or ""),
        )
        if not candidates:
            return self._result(document, resumed=True)
        selected = candidates[0]
        task_id = self._assert_task_root(selected)
        claim = selected.get("claim_id") or selected.get("claim") or ""
        actor = str(selected.get("actor") or selected.get("assignee") or "")
        before_launch = dict(document)
        before_launch.update(
            {
                "task": task_id,
                "phase": str(selected.get("phase") or "dispatch"),
                "next_action": "resume claimed task",
                "root": self.root,
                "controller": self.controller,
                "actor": actor,
                "claim_id": str(claim),
                "epoch": current_lease.epoch,
                "lease_token": current_lease.token,
                "session_id": "",
                "state": "claimed_no_session",
                "status": "claimed_no_session",
                "terminal": False,
            }
        )
        document = self._save_checkpoint(before_launch, lease=current_lease)
        if dispatch is None:
            return self._result(document, resumed=False)

        try:
            outcome = dispatch(dict(selected))
        except BaseException:
            # The durable pre-launch marker is intentionally retained.
            raise

        if isinstance(outcome, Mapping):
            session_id = str(outcome.get("session_id") or outcome.get("session") or "")
            outcome_state = str(outcome.get("state") or outcome.get("status") or "")
        else:
            session_id = str(outcome or "")
            outcome_state = ""
        # Dispatch may have taken long enough for an explicit takeover.  The
        # old epoch must never be allowed to write a post-dispatch checkpoint.
        current_lease = self.assert_lease(current_lease)
        after_launch = dict(document)
        after_launch["session_id"] = session_id
        after_launch["state"] = outcome_state or ("running" if session_id else "claimed_no_session")
        after_launch["status"] = after_launch["state"]
        after_launch["next_action"] = "await session" if session_id else "inspect claimed task"
        after_launch["terminal"] = after_launch["state"] in TERMINAL_STATES
        saved = self._save_checkpoint(after_launch, lease=current_lease)
        return self._result(saved, dispatched=True)

    schedule = resume

    def halt(
        self,
        state: str,
        reason: str,
        *,
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        if state not in TERMINAL_STATES:
            raise ControllerError(f"not a terminal halt state: {state}")
        current = self.assert_lease(lease)
        document = self._load_checkpoint()
        document.update(
            {
                "root": self.root,
                "controller": self.controller,
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": state,
                "status": state,
                "terminal": True,
                "terminal_reason": reason,
                "next_action": "no further scheduling",
            }
        )
        return self._result(self._save_checkpoint(document, lease=current))

    def complete(self, *, reason: str = "", lease: Lease | str | None = None) -> ResumeResult:
        return self.halt("completed", reason, lease=lease)

    def mark_incomplete(
        self,
        reason: str = "DEADLINE_EXCEEDED",
        *,
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Persist a resumable deadline result without discarding live work."""
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            rows = _active_tasks_from_checkpoint(document, self.root)
            previous_state = checkpoint.resume_state(document)
            if previous_state in TERMINAL_STATES and not rows:
                # An immediate deadline must not reopen a completed/blocked
                # checkpoint and make future resumes schedule work again.
                return self._result(document, resumed=True)
            was_draining = (
                document.get("state") == "draining"
                or document.get("status") == "draining"
                or previous_state in TERMINAL_STATES
            )
            if rows:
                # Convert the legacy single-task pointer to the durable active
                # collection before replacing its visible status.  On the
                # next resume this routes through the same Herdr reconciliation
                # path and cannot authorize a second launch.
                document.update({
                    "task": self.root,
                    "phase": "controller",
                    "next_action": "resume controller and reconcile active tasks",
                    "root": self.root,
                    "controller": self.controller,
                    "actor": "",
                    "claim_id": "",
                    "session_id": "",
                    "epoch": current.epoch,
                    "lease_token": current.token,
                    "active_tasks": rows,
                })
            if was_draining:
                # Keep the original failure evidence as the reason for the
                # drain. The separate status carries the deadline outcome.
                document.update({
                    "state": "draining",
                    "status": "incomplete",
                    "terminal": False,
                    "next_action": "deadline exceeded; resume controller and drain active tasks",
                })
            else:
                document.update({
                    "state": "incomplete",
                    "status": "incomplete",
                    "terminal": False,
                    "terminal_reason": reason or "DEADLINE_EXCEEDED",
                    "next_action": "resume controller",
                })
            saved = checkpoint.write_checkpoint(self.checkpoint_path, document)
            return self._result(saved, resumed=True)

    def advance(self, *, lease: Lease | str | None = None) -> ResumeResult:
        """Clear the current in-flight task pointer; a non-terminal transition.

        Call this once a caller has independently confirmed (via a real
        Herdr result) that the previously dispatched task is finished and
        has performed the durable Beads transition for it. Unlike
        ``halt()``, the checkpoint state after ``advance()`` is not in
        ``TERMINAL_STATES``, so the next ``resume()`` call is free to select
        and claim a new candidate instead of being stuck re-returning the
        finished task's state forever (AFREL-010).
        """
        current = self.assert_lease(lease)
        document = self._load_checkpoint()
        document.update(
            {
                # ``task`` is a required, non-empty checkpoint field; the
                # root itself is the "no current task selected" placeholder,
                # matching _base_checkpoint()'s initial idle document.
                "task": self.root,
                "root": self.root,
                "controller": self.controller,
                "epoch": current.epoch,
                "lease_token": current.token,
                "session_id": "",
                "state": "advancing",
                "status": "advancing",
                "next_action": "select next ready descendant",
                "terminal": False,
            }
        )
        return self._result(self._save_checkpoint(document, lease=current))

    def session_ledger(self) -> dict[str, Any]:
        """Return the public, non-secret controller-context budget ledger."""

        with self._locked():
            state = _read_json(self.state_path)
        value = state.get("session_control")
        if isinstance(value, Mapping) and value.get("schema") == session_control.SCHEMA:
            return dict(value)
        return session_control.new_ledger()

    def record_session_event(
        self,
        *,
        event: str,
        task_class: str,
        task: str = "",
        phase: str = "",
        approach: str = "",
        evidence: str = "",
        budget: session_control.SessionBudget | None = None,
        lease: Lease | str | None = None,
    ) -> dict[str, Any]:
        """Persist a task-aware progress or failure event under the lease."""

        with self.fence(lease):
            state = _read_json(self.state_path)
            value = state.get("session_control")
            ledger = session_control.record_event(
                value if isinstance(value, Mapping) else None,
                event=event,
                task_class=task_class,
                task=task,
                phase=phase,
                approach=approach,
                evidence=evidence,
                budget=budget,
            )
            state["session_control"] = ledger
            self._write_state(state)
            return ledger

    def rotate_session_budget(
        self, *, lease: Lease | str | None = None
    ) -> dict[str, Any]:
        """Begin a fresh context generation while preserving workflow authority."""

        with self.fence(lease):
            state = _read_json(self.state_path)
            value = state.get("session_control")
            ledger = session_control.next_generation(
                value if isinstance(value, Mapping) else None
            )
            state["session_control"] = ledger
            self._write_state(state)
            return ledger


Controller = RootController


def acquire_lease(
    state_path: Path,
    *,
    root: str,
    controller: str,
    stale_after: float = 300.0,
    takeover: bool = False,
    resume_proof: str = "",
    clock: Callable[[], float] = time.time,
) -> Lease:
    """Functional convenience wrapper for callers that do not retain a controller."""

    return RootController(
        root,
        controller,
        state_path=state_path,
        stale_after=stale_after,
        clock=clock,
    ).acquire(takeover=takeover, resume_proof=resume_proof)


__all__ = [
    "Controller",
    "ControllerError",
    "DuplicateController",
    "DuplicateSupervisor",
    "FencedLease",
    "Lease",
    "LeaseConflict",
    "ResumeResult",
    "RootController",
    "RootViolation",
    "StaleLease",
    "TERMINAL_STATES",
    "acquire_lease",
]
