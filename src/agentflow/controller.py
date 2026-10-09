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

    @staticmethod
    def _reject_pending_continuation(state: Mapping[str, Any]) -> None:
        if "continuation_reattach" in state:
            raise LeaseConflict(
                "an authenticated explicit continuation is pending; retry that exact continuation"
            )
        if "abandoned_epoch_repair" in state:
            raise LeaseConflict(
                "an authenticated abandoned-epoch repair is pending; retry that exact repair"
            )

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
            self._reject_pending_continuation(state)
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
                    state.pop("dormant_lease", None)
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
                dormant_value = state.get("dormant_lease")
                dormant = self._read_lease({"lease": dormant_value}) if isinstance(dormant_value, Mapping) else None
                if dormant_value is not None and dormant is None:
                    raise ControllerError("released controller incarnation record is malformed")
                if dormant is not None:
                    if (
                        dormant.root != self.root
                        or dormant.epoch != int(state.get("epoch", 0))
                        or not dormant.continuity_id
                    ):
                        raise ControllerError("released controller incarnation does not match root state")
                    if (
                        dormant.controller == self.controller
                        and dormant.verify_resume_proof(resume_proof)
                    ):
                        epoch = dormant.epoch + 1
                        new_secret = secrets.token_urlsafe(32)
                        lease = Lease(
                            dormant.root, dormant.controller, epoch,
                            f"{self.root}/{self.controller}/{epoch}",
                            dormant.acquired_at, now, self.owner_id,
                            new_secret, _hash_secret(new_secret),
                            continuity_id=dormant.continuity_id,
                        )
                        state["epoch"] = epoch
                        state["lease"] = lease.to_storage_dict()
                        state.pop("dormant_lease", None)
                        self._write_state(state)
                        self._lease = lease
                        return lease
                    if not takeover:
                        raise LeaseConflict(
                            "released controller incarnation requires its protected resume proof"
                        )
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
            state.pop("dormant_lease", None)
            self._write_state(state)
            self._lease = lease
            return lease

    def recover_released_incarnation(
        self,
        *,
        workflow_root: str,
        contract: Mapping[str, Any],
        authority_secret: str,
        resume_proof: str,
        persist_credentials: Callable[[Lease], Any] | None = None,
        superseded_legacy_scope: bool = False,
    ) -> Lease:
        """Restore a released lease only from its exact signed launch.

        Older released state discarded the resume-proof hash. This narrow
        migration accepts no caller-supplied continuity value: it derives the
        incarnation from a controller-signed return contract and requires the
        matching protected controller credential. The default path still
        requires an expired structured deadline. The explicit legacy-scope
        path accepts only a signed contract with no structured-budget fields;
        callers must additionally authenticate the missing ledger, task
        claim, checkpoint, and absent provider pane before invoking it.
        """

        if not resume_proof:
            raise LeaseConflict("protected controller resume proof is required")
        if (
            not isinstance(contract, Mapping)
            or contract.get("schema") != "agentflow.return@1"
            or contract.get("workspace_root") != self.root
            or contract.get("workflow_root") != workflow_root
            or contract.get("controller_id") != self.controller
            or not str(contract.get("task_id") or "")
            or not str(contract.get("claim_id") or "")
            or not str(contract.get("launch_id") or "")
            or not str(contract.get("continuity_id") or "")
            or not authority_secret
            or not hmac.compare_digest(
                str(contract.get("authority_key_id") or ""),
                hashlib.sha256(authority_secret.encode("utf-8")).hexdigest()[:24],
            )
            or not hmac.compare_digest(
                str(contract.get("authority_hmac") or ""),
                _authority_mac(authority_secret, contract, domain="return-contract-v1"),
            )
        ):
            raise LeaseConflict("signed launch does not authenticate this released controller incarnation")
        deadline = contract.get("deadline_epoch")
        epoch = contract.get("lease_epoch")
        budget_fields = {
            "execution_limits", "deadline_epoch", "deadline_seconds", "expires_at", "issued_at",
            "attempt", "max_attempts", "max_retries", "execution_limit_capabilities", "budget",
        }
        if superseded_legacy_scope:
            if any(field in contract for field in budget_fields):
                raise LeaseConflict("legacy-scope recovery requires a signed contract without budget metadata")
        elif (
            isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or float(self.clock()) < float(deadline)
        ):
            raise LeaseConflict("signed launch is not an expired, epoch-bound recovery contract")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise LeaseConflict("signed launch does not carry a valid controller epoch")

        with self._locked():
            state = _read_json(self.state_path)
            self._reject_pending_continuation(state)
            active = self._read_lease(state)
            dormant_value = state.get("dormant_lease")
            dormant = (
                self._read_lease({"lease": dormant_value})
                if isinstance(dormant_value, Mapping) else None
            )
            if dormant_value is not None and dormant is None:
                raise LeaseConflict("released controller incarnation record is malformed")
            old_epoch = int(state.get("epoch", 0))
            now = float(self.clock())
            contract_digest = hashlib.sha256(
                json.dumps(dict(contract), sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            # A legacy release discarded the resume-proof hash. Its exact
            # signed, expired launch and canonical external credential are
            # the remaining authority, so persist the credential first. A
            # crash leaves the null-lease state retryable with that credential.
            if active is None and dormant is None:
                if old_epoch != epoch:
                    raise LeaseConflict("signed launch epoch no longer matches released controller state")
                new_epoch = old_epoch + 1
                secret = secrets.token_urlsafe(32)
                lease = Lease(
                    self.root, self.controller, new_epoch,
                    f"{self.root}/{self.controller}/{new_epoch}", now, now,
                    self.owner_id, secret, _hash_secret(secret),
                    continuity_id=str(contract["continuity_id"]),
                )
                if persist_credentials is not None:
                    persist_credentials(lease)
                state["epoch"] = new_epoch
                state["lease"] = lease.to_storage_dict()
                self._write_state(state)
                self._lease = lease
                return lease

            if active is not None or dormant is None:
                raise LeaseConflict("released controller state is no longer eligible for recovery")
            if (
                dormant.root != self.root or dormant.controller != self.controller
                or dormant.continuity_id != contract.get("continuity_id")
                or dormant.epoch != old_epoch or dormant.epoch < epoch
            ):
                raise LeaseConflict("released controller incarnation does not match signed launch")

            pending = state.get("recovery_reattach")
            if isinstance(pending, Mapping):
                pending_payload = dict(pending)
                pending_mac = str(pending_payload.pop("authority_hmac", ""))
                expected_fields = {
                    "schema": "agentflow.recovery_reattach@1",
                    "root": self.root,
                    "controller": self.controller,
                    "workflow_root": workflow_root,
                    "task_id": str(contract.get("task_id") or ""),
                    "claim_id": str(contract.get("claim_id") or ""),
                    "launch_id": str(contract.get("launch_id") or ""),
                    "continuity_id": str(contract.get("continuity_id") or ""),
                    "contract_sha256": contract_digest,
                    "previous_epoch": old_epoch,
                }
                if (
                    any(pending_payload.get(key) != value for key, value in expected_fields.items())
                    or not hmac.compare_digest(
                        pending_mac,
                        _authority_mac(authority_secret, pending_payload, domain="recovery-reattach-v1"),
                    )
                ):
                    raise LeaseConflict("pending released-controller reattach proof is invalid")
                pending_hash = str(pending_payload.get("resume_secret_hash") or "")
                if pending_hash and hmac.compare_digest(_hash_secret(resume_proof), pending_hash):
                    new_epoch = int(pending_payload.get("epoch", 0))
                    if new_epoch != old_epoch + 1:
                        raise LeaseConflict("pending released-controller epoch is invalid")
                    lease = Lease(
                        self.root, self.controller, new_epoch,
                        str(pending_payload.get("token") or ""),
                        float(pending_payload.get("acquired_at", 0)),
                        float(pending_payload.get("heartbeat_at", 0)),
                        str(pending_payload.get("owner_id") or ""),
                        resume_proof, pending_hash,
                        continuity_id=dormant.continuity_id,
                    )
                    state["epoch"] = new_epoch
                    state["lease"] = lease.to_storage_dict()
                    state.pop("dormant_lease", None)
                    state.pop("recovery_reattach", None)
                    self._write_state(state)
                    self._lease = lease
                    return lease

            if not dormant.verify_resume_proof(resume_proof):
                raise LeaseConflict("released controller incarnation requires its protected resume proof")
            new_epoch = old_epoch + 1
            secret = secrets.token_urlsafe(32)
            lease = Lease(
                self.root, self.controller, new_epoch,
                f"{self.root}/{self.controller}/{new_epoch}",
                dormant.acquired_at, now, self.owner_id, secret, _hash_secret(secret),
                continuity_id=dormant.continuity_id,
            )
            if persist_credentials is not None:
                payload = {
                    "schema": "agentflow.recovery_reattach@1",
                    "root": self.root,
                    "controller": self.controller,
                    "workflow_root": workflow_root,
                    "task_id": str(contract.get("task_id") or ""),
                    "claim_id": str(contract.get("claim_id") or ""),
                    "launch_id": str(contract.get("launch_id") or ""),
                    "continuity_id": dormant.continuity_id,
                    "contract_sha256": contract_digest,
                    "previous_epoch": old_epoch,
                    "epoch": new_epoch,
                    "token": lease.token,
                    "acquired_at": lease.acquired_at,
                    "heartbeat_at": lease.heartbeat_at,
                    "owner_id": lease.owner_id,
                    "resume_secret_hash": lease.resume_secret_hash,
                }
                payload["authority_hmac"] = _authority_mac(
                    authority_secret, payload, domain="recovery-reattach-v1",
                )
                # Keep the prior dormant proof until the new canonical
                # credential is durable. The signed intent lets a retry
                # finish this exact epoch if the process stops after writing
                # the credential but before committing the active lease.
                state["recovery_reattach"] = payload
                self._write_state(state)
                persist_credentials(lease)
            state["epoch"] = new_epoch
            state["lease"] = lease.to_storage_dict()
            state.pop("dormant_lease", None)
            state.pop("recovery_reattach", None)
            self._write_state(state)
            self._lease = lease
            return lease

    def repair_abandoned_epoch(
        self,
        *,
        workflow_root: str,
        contract: Mapping[str, Any],
        authority_secret: str,
        resume_proof: str,
        abandoned_epoch: int,
        acknowledge_abandoned_epoch: bool,
        state_sha256: str,
        checkpoint_sha256: str,
        contract_sha256: str,
        persist_credentials: Callable[[Lease], Any],
    ) -> Lease:
        """Repair one explicitly acknowledged, unowned legacy epoch gap.

        This is deliberately separate from ordinary released-incarnation
        recovery.  Its signed intent is bound to one exact state/checkpoint/
        contract snapshot and remains in controller state until the matching
        preidentity cancellation has revoked the old return channel and
        retired the checkpoint pointer.
        """
        if not acknowledge_abandoned_epoch:
            raise LeaseConflict("explicit abandoned-epoch acknowledgement is required")
        if not callable(persist_credentials) or not resume_proof:
            raise LeaseConflict("canonical controller credential and protected writer are required")
        for name, digest in (
            ("state", state_sha256), ("checkpoint", checkpoint_sha256),
            ("contract", contract_sha256),
        ):
            if not isinstance(digest, str) or len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise LeaseConflict(f"expected {name} snapshot SHA-256 is malformed")
        if (
            not isinstance(contract, Mapping)
            or contract.get("schema") != "agentflow.return@1"
            or contract.get("workspace_root") != self.root
            or contract.get("workflow_root") != workflow_root
            or contract.get("controller_id") != self.controller
            or not str(contract.get("task_id") or "")
            or not str(contract.get("claim_id") or "")
            or not str(contract.get("launch_id") or "")
            or not str(contract.get("continuity_id") or "")
            or not authority_secret
            or not hmac.compare_digest(
                str(contract.get("authority_key_id") or ""),
                hashlib.sha256(authority_secret.encode("utf-8")).hexdigest()[:24],
            )
            or not hmac.compare_digest(
                str(contract.get("authority_hmac") or ""),
                _authority_mac(authority_secret, contract, domain="return-contract-v1"),
            )
        ):
            raise LeaseConflict("signed launch does not authenticate this abandoned controller incarnation")
        budget_fields = {
            "execution_limits", "deadline_epoch", "deadline_seconds", "expires_at", "issued_at",
            "attempt", "max_attempts", "max_retries", "execution_limit_capabilities", "budget",
        }
        if any(field in contract for field in budget_fields):
            raise LeaseConflict("abandoned legacy repair cannot invent or replace execution-budget evidence")
        previous_epoch = contract.get("lease_epoch")
        if (
            isinstance(previous_epoch, bool) or not isinstance(previous_epoch, int)
            or previous_epoch < 1 or isinstance(abandoned_epoch, bool)
            or not isinstance(abandoned_epoch, int) or abandoned_epoch != previous_epoch + 1
        ):
            raise LeaseConflict("abandoned epoch must be the exact single gap after the signed launch")
        canonical_contract_digest = hashlib.sha256(
            json.dumps(dict(contract), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        expected = {
            "schema": "agentflow.abandoned_epoch_repair@1",
            "root": self.root,
            "controller": self.controller,
            "workflow_root": workflow_root,
            "task_id": str(contract["task_id"]),
            "claim_id": str(contract["claim_id"]),
            "launch_id": str(contract["launch_id"]),
            "continuity_id": str(contract["continuity_id"]),
            "previous_epoch": previous_epoch,
            "abandoned_epoch": abandoned_epoch,
            "new_epoch": abandoned_epoch + 1,
            "state_sha256": state_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "contract_sha256": contract_sha256,
            "contract_json_sha256": canonical_contract_digest,
        }
        now = float(self.clock())
        with self._locked():
            state = _read_json(self.state_path)
            pending_value = state.get("abandoned_epoch_repair")
            receipt_value = state.get("abandoned_epoch_repair_receipt")
            completed_receipt = pending_value is None and receipt_value is not None
            if pending_value is None and receipt_value is None:
                self._reject_pending_continuation(state)
                if not self.state_path.is_file() or not self.checkpoint_path.is_file():
                    raise LeaseConflict("exact protected state and checkpoint snapshots are required")
                if hashlib.sha256(self.state_path.read_bytes()).hexdigest() != state_sha256:
                    raise LeaseConflict("controller state changed from the explicitly acknowledged snapshot")
                if hashlib.sha256(self.checkpoint_path.read_bytes()).hexdigest() != checkpoint_sha256:
                    raise LeaseConflict("controller checkpoint changed from the explicitly acknowledged snapshot")
                current_epoch = state.get("epoch")
                if (
                    isinstance(current_epoch, bool) or not isinstance(current_epoch, int)
                    or current_epoch != abandoned_epoch
                    or state.get("lease") is not None
                    or state.get("dormant_lease") is not None
                    or "recovery_reattach" in state
                ):
                    raise LeaseConflict("only the exact unowned abandoned epoch with no dormant lease can be repaired")
                document = self._load_checkpoint()
                rows = _active_tasks_from_checkpoint(document, self.root)
                if (
                    checkpoint.admission_phase(document) != "draining"
                    or checkpoint.resume_state(document) != "draining"
                    or document.get("terminal") is not False
                    or document.get("task") != self.root
                    or document.get("root") != self.root
                    or document.get("controller") != self.controller
                    or document.get("epoch") != previous_epoch
                    or document.get("actor") or document.get("claim_id")
                    or document.get("session_id") or len(rows) != 1
                    or rows[0].get("task") != contract["task_id"]
                    or rows[0].get("claim_id") != contract["claim_id"]
                    or rows[0].get("state") != "identity_pending"
                    or rows[0].get("session_id")
                ):
                    raise LeaseConflict("checkpoint is not the exact retained unbound legacy launch")
                nonce = secrets.token_urlsafe(24)
                candidate_secret = hmac.new(
                    authority_secret.encode("utf-8"),
                    f"agentflow:abandoned-epoch-candidate:{nonce}:{resume_proof}".encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                candidate = Lease(
                    self.root, self.controller, abandoned_epoch + 1,
                    f"{self.root}/{self.controller}/{abandoned_epoch + 1}",
                    now, now, self.owner_id, candidate_secret, _hash_secret(candidate_secret),
                    continuity_id=str(contract["continuity_id"]),
                )
                payload = {
                    **expected,
                    "nonce": nonce,
                    "previous_resume_secret_hash": _hash_secret(resume_proof),
                    "candidate_resume_secret_hash": candidate.resume_secret_hash,
                    "token": candidate.token,
                    "acquired_at": candidate.acquired_at,
                    "heartbeat_at": candidate.heartbeat_at,
                    "owner_id": candidate.owner_id,
                }
                payload["authority_hmac"] = _authority_mac(
                    authority_secret, payload, domain="abandoned-epoch-repair-v1",
                )
                state["abandoned_epoch_repair"] = payload
                self._write_state(state)
            else:
                if pending_value is not None and receipt_value is not None:
                    raise LeaseConflict("abandoned-epoch repair has conflicting intent and completion records")
                if completed_receipt:
                    pending_value = receipt_value
                if not isinstance(pending_value, Mapping):
                    raise LeaseConflict("pending abandoned-epoch repair intent is malformed")
                payload = dict(pending_value)
                supplied = str(payload.pop("authority_hmac", ""))
                if (
                    any(payload.get(key) != value for key, value in expected.items())
                    or not hmac.compare_digest(
                        supplied,
                        _authority_mac(authority_secret, payload, domain="abandoned-epoch-repair-v1"),
                    )
                ):
                    raise LeaseConflict("pending abandoned-epoch repair does not match this exact owner request")
                if not isinstance(payload.get("nonce"), str) or not payload.get("nonce"):
                    raise LeaseConflict("pending abandoned-epoch repair nonce is malformed")
                stored_epoch = state.get("epoch")
                active = self._read_lease(state)
                if stored_epoch == abandoned_epoch:
                    if active is not None or state.get("dormant_lease") is not None:
                        raise LeaseConflict("pending abandoned-epoch repair found a changed lease")
                elif stored_epoch == abandoned_epoch + 1:
                    if (
                        active is None or state.get("dormant_lease") is not None
                        or active.controller != self.controller or active.root != self.root
                        or active.epoch != abandoned_epoch + 1
                        or active.continuity_id != contract.get("continuity_id")
                        or active.resume_secret_hash != payload.get("candidate_resume_secret_hash")
                    ):
                        raise LeaseConflict("pending abandoned-epoch repair lease changed")
                else:
                    raise LeaseConflict("pending abandoned-epoch repair state epoch changed")
                old_hash = str(payload.get("previous_resume_secret_hash") or "")
                candidate_hash = str(payload.get("candidate_resume_secret_hash") or "")
                if hmac.compare_digest(_hash_secret(resume_proof), candidate_hash):
                    candidate_secret = resume_proof
                elif hmac.compare_digest(_hash_secret(resume_proof), old_hash):
                    candidate_secret = hmac.new(
                        authority_secret.encode("utf-8"),
                        f"agentflow:abandoned-epoch-candidate:{payload['nonce']}:{resume_proof}".encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                else:
                    raise LeaseConflict("canonical controller credential does not match the staged repair")
                if not hmac.compare_digest(_hash_secret(candidate_secret), candidate_hash):
                    raise LeaseConflict("pending abandoned-epoch candidate credential is invalid")
                candidate = Lease(
                    self.root, self.controller, abandoned_epoch + 1,
                    str(payload.get("token") or ""),
                    float(payload.get("acquired_at", 0)),
                    float(payload.get("heartbeat_at", 0)),
                    str(payload.get("owner_id") or ""), candidate_secret,
                    candidate_hash, continuity_id=str(contract["continuity_id"]),
                )
                if not candidate.token or not candidate.owner_id:
                    raise LeaseConflict("pending abandoned-epoch candidate lease is malformed")
                if completed_receipt:
                    current = self._read_lease(state)
                    document = self._load_checkpoint()
                    retired_check = (
                        f"cancelled owner_abandoned_epoch preidentity launch "
                        f"{contract['launch_id']} for {contract['task_id']}"
                    )
                    if (
                        current is None or state.get("epoch") != candidate.epoch
                        or state.get("dormant_lease") is not None
                        or current.epoch != candidate.epoch or current.token != candidate.token
                        or current.owner_id != candidate.owner_id
                        or current.continuity_id != candidate.continuity_id
                        or current.resume_secret_hash != candidate.resume_secret_hash
                        or not current.verify_resume_proof(resume_proof)
                        or checkpoint.admission_phase(document) != "terminal"
                        or checkpoint.resume_state(document) != "blocked"
                        or document.get("terminal") is not True
                        or document.get("root") != self.root
                        or document.get("controller") != self.controller
                        or document.get("epoch") != candidate.epoch
                        or document.get("task") != self.root
                        or document.get("last_check") != retired_check
                        or _active_tasks_from_checkpoint(document, self.root)
                    ):
                        raise LeaseConflict("completed abandoned-epoch repair receipt no longer matches its retired halt")
                    self._lease = candidate
                    return candidate

            # Persisting the protected credential follows the durable intent.
            # If either write fails, the exact command can reconstruct and
            # finish this candidate without accepting a different snapshot.
            persist_credentials(candidate)
            state = _read_json(self.state_path)
            if state.get("abandoned_epoch_repair") != pending_value and pending_value is not None:
                raise LeaseConflict("pending abandoned-epoch repair changed during credential persistence")
            pending = state.get("abandoned_epoch_repair")
            if not isinstance(pending, Mapping):
                raise LeaseConflict("abandoned-epoch repair intent disappeared before lease commit")
            current = self._read_lease(state)
            if current is None:
                state["epoch"] = candidate.epoch
                state["lease"] = candidate.to_storage_dict()
                state.pop("dormant_lease", None)
                self._write_state(state)
            elif (
                current.epoch != candidate.epoch or current.token != candidate.token
                or current.owner_id != candidate.owner_id
                or current.resume_secret_hash != candidate.resume_secret_hash
                or current.continuity_id != candidate.continuity_id
            ):
                raise LeaseConflict("abandoned-epoch repair lease changed before commit")
            self._lease = candidate
            return candidate

    @staticmethod
    def _verify_abandoned_epoch_fence_authority(
        state: Mapping[str, Any], expected: Lease, authority: Mapping[str, Any],
        *, root: str, controller: str,
    ) -> None:
        pending = state.get("abandoned_epoch_repair")
        if pending is None:
            pending = state.get("abandoned_epoch_repair_receipt")
        if not isinstance(pending, Mapping):
            raise LeaseConflict("abandoned-epoch repair authority has no matching durable intent")
        payload = dict(pending)
        supplied = str(payload.pop("authority_hmac", ""))
        secret = str(authority.get("authority_secret") or "")
        if (
            payload.get("root") != root or payload.get("controller") != controller
            or payload.get("workflow_root") != authority.get("workflow_root")
            or payload.get("task_id") != authority.get("task_id")
            or payload.get("claim_id") != authority.get("claim_id")
            or payload.get("launch_id") != authority.get("launch_id")
            or payload.get("contract_sha256") != authority.get("contract_sha256")
            or payload.get("new_epoch") != expected.epoch
            or payload.get("continuity_id") != expected.continuity_id
            or payload.get("candidate_resume_secret_hash") != expected.resume_secret_hash
            or not secret
            or not hmac.compare_digest(
                supplied, _authority_mac(secret, payload, domain="abandoned-epoch-repair-v1"),
            )
        ):
            raise LeaseConflict("abandoned-epoch repair authority does not match the staged exact launch")

    def pending_explicit_continuation(
        self,
        *,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        authority_secret: str,
        resume_proof: str,
    ) -> Mapping[str, Any] | None:
        """Authenticate a staged exact-target continuation, if one exists."""
        with self._locked():
            state = _read_json(self.state_path)
            if "continuation_reattach" not in state:
                return None
            pending = state["continuation_reattach"]
            if not isinstance(pending, Mapping):
                raise LeaseConflict("staged continuation reattach record is malformed")
            payload = dict(pending)
            supplied = str(payload.pop("authority_hmac", ""))
            expected = {
                "schema": "agentflow.continuation-reattach@1",
                "root": self.root,
                "controller": self.controller,
                "workflow_root": workflow_root,
                "cancelled_task": cancelled_task,
                "ready_task": ready_task,
            }
            if (
                any(payload.get(key) != value for key, value in expected.items())
                or not hmac.compare_digest(
                    supplied,
                    _authority_mac(authority_secret, payload, domain="continuation-reattach-v1"),
                )
            ):
                raise LeaseConflict("staged continuation does not authenticate this exact target")
            try:
                previous_epoch = int(payload["previous_epoch"])
                candidate_epoch = int(payload["epoch"])
                state_epoch = int(state.get("epoch", -1))
            except (KeyError, TypeError, ValueError) as exc:
                raise LeaseConflict("staged continuation epochs are malformed") from exc
            if (
                candidate_epoch != previous_epoch + 1
                or state_epoch not in {previous_epoch, candidate_epoch}
                or payload.get("continuity_id") != self._continuity_for_staged_state(state, previous_epoch, candidate_epoch)
                or payload.get("token") != f"{self.root}/{self.controller}/{candidate_epoch}"
            ):
                raise LeaseConflict("staged continuation no longer matches its protected incarnation")
            active = self._read_lease(state)
            dormant_value = state.get("dormant_lease")
            dormant = self._read_lease({"lease": dormant_value}) if isinstance(dormant_value, Mapping) else None
            candidate_hash = str(payload.get("resume_secret_hash") or "")
            candidate_proof = bool(
                resume_proof and candidate_hash
                and hmac.compare_digest(_hash_secret(resume_proof), candidate_hash)
            )
            if state_epoch == previous_epoch:
                previous = active or dormant
                if (
                    previous is None or previous.epoch != previous_epoch
                    or previous.root != self.root or previous.controller != self.controller
                    or previous.continuity_id != payload.get("continuity_id")
                    or (not candidate_proof and not previous.verify_resume_proof(resume_proof))
                ):
                    raise LeaseConflict("staged continuation requires its exact current or rotated credential")
            else:
                candidate = active
                if (
                    candidate is None or candidate.epoch != candidate_epoch
                    or candidate.root != self.root or candidate.controller != self.controller
                    or candidate.continuity_id != payload.get("continuity_id")
                    or candidate.token != payload.get("token")
                    or candidate.resume_secret_hash != candidate_hash
                    or not candidate_proof or not candidate.verify_resume_proof(resume_proof)
                ):
                    raise LeaseConflict("staged continuation requires its exact committed credential")
            return dict(pending)

    def _continuity_for_staged_state(
        self, state: Mapping[str, Any], previous_epoch: int, candidate_epoch: int,
    ) -> str:
        active = self._read_lease(state)
        dormant_value = state.get("dormant_lease")
        dormant = self._read_lease({"lease": dormant_value}) if isinstance(dormant_value, Mapping) else None
        expected_epoch = int(state.get("epoch", -1))
        expected = candidate_epoch if expected_epoch == candidate_epoch else previous_epoch
        lease = active if active is not None and active.epoch == expected else dormant
        if lease is None or lease.epoch != expected or lease.root != self.root or lease.controller != self.controller:
            return ""
        return lease.continuity_id

    def _authenticated_pending_candidate(
        self,
        state: Mapping[str, Any],
        *,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        authority_secret: str,
        resume_proof: str,
    ) -> Lease:
        """Verify the only lease allowed to mutate a staged continuation."""
        pending = state.get("continuation_reattach")
        if not isinstance(pending, Mapping):
            raise LeaseConflict("staged continuation record is missing or malformed")
        payload = dict(pending)
        supplied = str(payload.pop("authority_hmac", ""))
        expected = {
            "schema": "agentflow.continuation-reattach@1",
            "root": self.root,
            "controller": self.controller,
            "workflow_root": workflow_root,
            "cancelled_task": cancelled_task,
            "ready_task": ready_task,
        }
        if (
            not authority_secret
            or any(payload.get(key) != value for key, value in expected.items())
            or not hmac.compare_digest(
                supplied, _authority_mac(authority_secret, payload, domain="continuation-reattach-v1"),
            )
        ):
            raise LeaseConflict("staged continuation does not authenticate this exact target")
        try:
            previous_epoch = int(payload["previous_epoch"])
            candidate_epoch = int(payload["epoch"])
            state_epoch = int(state.get("epoch", -1))
        except (KeyError, TypeError, ValueError) as exc:
            raise LeaseConflict("staged continuation epochs are malformed") from exc
        active = self._read_lease(state)
        candidate_hash = str(payload.get("resume_secret_hash") or "")
        if (
            candidate_epoch != previous_epoch + 1
            or state_epoch != candidate_epoch
            or active is None
            or active.root != self.root
            or active.controller != self.controller
            or active.epoch != candidate_epoch
            or active.token != payload.get("token")
            or active.continuity_id != payload.get("continuity_id")
            or active.resume_secret_hash != candidate_hash
            or not hmac.compare_digest(_hash_secret(resume_proof), candidate_hash)
        ):
            raise LeaseConflict("staged continuation requires its exact committed credential")
        candidate = self._continuation_lease_from_record(pending, resume_proof)
        if not self._lease_matches(active, candidate):
            raise LeaseConflict("staged continuation lease no longer matches protected state")
        return candidate

    def authorize_explicit_continuation(
        self,
        *,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        authority_secret: str,
        resume_proof: str,
    ) -> Lease:
        """Authorize only the exact candidate lease saved in a continuation intent."""
        with self._locked():
            state = _read_json(self.state_path)
            candidate = self._authenticated_pending_candidate(
                state, workflow_root=workflow_root, cancelled_task=cancelled_task,
                ready_task=ready_task, authority_secret=authority_secret,
                resume_proof=resume_proof,
            )
            self._lease = candidate
            return candidate

    def reattach_for_explicit_continuation(
        self,
        *,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        checkpoint_epoch: int,
        authority_secret: str,
        resume_proof: str,
        persist_credentials: Callable[[Lease], Any],
    ) -> Lease:
        """Rotate credentials under a signed, exact-target retry intent."""
        if not all((workflow_root, cancelled_task, ready_task, authority_secret, resume_proof)):
            raise LeaseConflict("explicit continuation reattach is missing an authenticated identity")
        if cancelled_task == ready_task:
            raise LeaseConflict("continuation target must differ from the cancelled task")
        with self._locked():
            state = _read_json(self.state_path)
            active = self._read_lease(state)
            dormant_value = state.get("dormant_lease")
            dormant = self._read_lease({"lease": dormant_value}) if isinstance(dormant_value, Mapping) else None
            if dormant_value is not None and dormant is None:
                raise LeaseConflict("dormant controller lease is malformed")
            pending_value = state.get("continuation_reattach")
            if "continuation_reattach" in state and not isinstance(pending_value, Mapping):
                raise LeaseConflict("staged continuation reattach record is malformed")
            pending: dict[str, Any] | None = None
            if pending_value is not None:
                if not isinstance(pending_value, Mapping):
                    raise LeaseConflict("staged continuation reattach record is malformed")
                pending = dict(pending_value)
                supplied = str(pending.pop("authority_hmac", ""))
                expected = {
                    "schema": "agentflow.continuation-reattach@1",
                    "root": self.root, "controller": self.controller,
                    "workflow_root": workflow_root,
                    "cancelled_task": cancelled_task, "ready_task": ready_task,
                }
                if (
                    any(pending.get(key) != value for key, value in expected.items())
                    or not hmac.compare_digest(
                        supplied,
                        _authority_mac(authority_secret, pending, domain="continuation-reattach-v1"),
                    )
                ):
                    raise LeaseConflict("staged continuation does not authenticate this exact target")
                if checkpoint_epoch not in {pending.get("previous_epoch"), pending.get("epoch")}:
                    raise LeaseConflict("staged continuation checkpoint epoch changed")
                candidate_hash = str(pending.get("resume_secret_hash") or "")
                try:
                    previous_epoch = int(pending["previous_epoch"])
                    candidate_epoch = int(pending["epoch"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise LeaseConflict("staged continuation epochs are malformed") from exc
                if candidate_epoch != previous_epoch + 1 or int(state.get("epoch", -1)) not in {
                    previous_epoch, candidate_epoch,
                }:
                    raise LeaseConflict("staged continuation epochs no longer match the protected lease")
                if (
                    active is not None and active.epoch == candidate_epoch
                    and active.root == self.root and active.controller == self.controller
                    and active.continuity_id == pending.get("continuity_id")
                    and active.token == pending.get("token")
                    and active.resume_secret_hash == candidate_hash
                    and int(state.get("epoch", -1)) == candidate_epoch
                    and active.verify_resume_proof(resume_proof)
                ):
                    candidate = self._continuation_lease_from_record(pending_value, resume_proof)
                    if not self._lease_matches(active, candidate):
                        raise LeaseConflict("staged continuation lease no longer matches protected state")
                    self._lease = candidate
                    return candidate
                if candidate_hash and hmac.compare_digest(
                    hashlib.sha256(resume_proof.encode("utf-8")).hexdigest(), candidate_hash,
                ):
                    previous = active or dormant
                    if (
                        previous is None or previous.root != self.root
                        or previous.controller != self.controller
                        or previous.epoch != previous_epoch
                        or previous.continuity_id != pending.get("continuity_id")
                    ):
                        raise LeaseConflict("staged continuation no longer matches its original incarnation")
                    candidate = self._continuation_lease_from_record(pending, resume_proof)
                    state["epoch"] = candidate.epoch
                    state["lease"] = candidate.to_storage_dict()
                    state.pop("dormant_lease", None)
                    self._write_state(state)
                    self._lease = candidate
                    return candidate
            previous = active or dormant
            if (
                previous is None or previous.root != self.root
                or previous.controller != self.controller
                or previous.epoch != int(state.get("epoch", -1))
                or previous.epoch != checkpoint_epoch
                or not previous.verify_resume_proof(resume_proof)
                or not previous.continuity_id
            ):
                raise LeaseConflict("explicit continuation requires its exact current protected credential")
            now = float(self.clock())
            epoch = previous.epoch + 1
            secret = secrets.token_urlsafe(32)
            candidate = Lease(
                self.root, self.controller, epoch, f"{self.root}/{self.controller}/{epoch}",
                previous.acquired_at, now, self.owner_id, secret, _hash_secret(secret),
                continuity_id=previous.continuity_id,
            )
            payload = {
                "schema": "agentflow.continuation-reattach@1",
                "root": self.root, "controller": self.controller,
                "workflow_root": workflow_root,
                "cancelled_task": cancelled_task, "ready_task": ready_task,
                "continuity_id": previous.continuity_id,
                "previous_epoch": previous.epoch, "epoch": epoch,
                "token": candidate.token, "acquired_at": candidate.acquired_at,
                "heartbeat_at": candidate.heartbeat_at, "owner_id": candidate.owner_id,
                "resume_secret_hash": candidate.resume_secret_hash,
            }
            payload["authority_hmac"] = _authority_mac(
                authority_secret, payload, domain="continuation-reattach-v1",
            )
            state["continuation_reattach"] = payload
            self._write_state(state)
            persist_credentials(candidate)
            state["epoch"] = candidate.epoch
            state["lease"] = candidate.to_storage_dict()
            state.pop("dormant_lease", None)
            self._write_state(state)
            self._lease = candidate
            return candidate

    def clear_explicit_continuation(
        self,
        *,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        authority_secret: str,
        lease: Lease | str,
    ) -> None:
        """Clear a staged continuation only after its target is checkpointed."""
        with self.fence(
            lease,
            _continuation_authority=(workflow_root, cancelled_task, ready_task, authority_secret),
        ) as current:
            state = _read_json(self.state_path)
            pending = state.get("continuation_reattach")
            if "continuation_reattach" not in state:
                return
            if not isinstance(pending, Mapping):
                raise LeaseConflict("staged continuation reattach record is malformed")
            payload = dict(pending)
            supplied = str(payload.pop("authority_hmac", ""))
            expected = {
                "schema": "agentflow.continuation-reattach@1",
                "root": self.root, "controller": self.controller,
                "workflow_root": workflow_root,
                "cancelled_task": cancelled_task, "ready_task": ready_task,
                "epoch": current.epoch, "continuity_id": current.continuity_id,
            }
            checkpoint = self._load_checkpoint()
            expected_check = (
                f"authenticated cancelled-preidentity continuation for {workflow_root}; "
                f"ready descendant {ready_task} after {cancelled_task}"
            )
            if (
                any(payload.get(key) != value for key, value in expected.items())
                or not hmac.compare_digest(
                    supplied,
                    _authority_mac(authority_secret, payload, domain="continuation-reattach-v1"),
                )
                or int(state.get("epoch", -1)) != current.epoch
                or payload.get("previous_epoch") != current.epoch - 1
                or payload.get("token") != current.token
                or payload.get("resume_secret_hash") != current.resume_secret_hash
                or checkpoint.get("root") != self.root
                or checkpoint.get("controller") != self.controller
                or checkpoint.get("task") != self.root
                or checkpoint.get("phase") != "controller"
                or checkpoint.get("state") != "advancing"
                or checkpoint.get("terminal") is not False
                or checkpoint.get("active_tasks")
                or checkpoint.get("actor") or checkpoint.get("claim_id") or checkpoint.get("session_id")
                or checkpoint.get("last_check") != expected_check
                or checkpoint.get("pending_continuation_task") != ready_task
                or checkpoint.get("epoch") != current.epoch
                or checkpoint.get("lease_token") != current.token
            ):
                raise LeaseConflict("staged continuation cannot be cleared before its exact target is durable")
            state.pop("continuation_reattach", None)
            self._write_state(state)

    @staticmethod
    def _continuation_lease_from_record(value: Mapping[str, Any], resume_proof: str) -> Lease:
        try:
            epoch = int(value["epoch"])
            resume_hash = str(value["resume_secret_hash"])
            if not hmac.compare_digest(_hash_secret(resume_proof), resume_hash):
                raise ValueError("staged credential does not match")
            return Lease(
                root=str(value["root"]), controller=str(value["controller"]),
                epoch=epoch, token=str(value["token"]),
                acquired_at=float(value["acquired_at"]), heartbeat_at=float(value["heartbeat_at"]),
                owner_id=str(value.get("owner_id") or ""), resume_secret=resume_proof,
                resume_secret_hash=resume_hash, continuity_id=str(value["continuity_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LeaseConflict("staged continuation lease is malformed") from exc

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
            state = _read_json(self.state_path)
            self._reject_pending_continuation(state)
            current = self._read_lease(state)
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
            state = _read_json(self.state_path)
            self._reject_pending_continuation(state)
            current = self._read_lease(state)
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
            state = _read_json(self.state_path)
            self._reject_pending_continuation(state)
            current = self._read_lease(state)
            if current is None or not self._lease_matches(current, expected) or current.root != self.root:
                raise FencedLease("controller lease is no longer current")
            state = _read_json(self.state_path)
            state["epoch"] = current.epoch
            # Keep the private proof hash and incarnation identity across a
            # clean release so an authenticated later resume does not become
            # a different result-owning controller. The next acquisition
            # consumes this record and rotates the proof as usual.
            state["dormant_lease"] = current.to_storage_dict()
            state["lease"] = None
            self._write_state(state)
            self._lease = None

    @contextmanager
    def fence(
        self,
        lease: Lease | str | None = None,
        *,
        _continuation_authority: tuple[str, str, str, str] | None = None,
        _abandoned_epoch_repair_authority: Mapping[str, Any] | None = None,
    ):
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
            state = _read_json(self.state_path)
            current = self._read_lease(state)
            if current is None or not self._lease_matches(current, expected) or current.root != self.root:
                raise FencedLease("controller lease is no longer current")
            if "continuation_reattach" in state:
                if _continuation_authority is None or not isinstance(expected, Lease):
                    self._reject_pending_continuation(state)
                workflow_root, cancelled_task, ready_task, authority_secret = _continuation_authority
                self._authenticated_pending_candidate(
                    state, workflow_root=workflow_root, cancelled_task=cancelled_task,
                    ready_task=ready_task, authority_secret=authority_secret,
                    resume_proof=expected.resume_secret,
                )
            if "abandoned_epoch_repair" in state:
                if _abandoned_epoch_repair_authority is None or not isinstance(expected, Lease):
                    self._reject_pending_continuation(state)
                self._verify_abandoned_epoch_fence_authority(
                    state, expected, _abandoned_epoch_repair_authority,
                    root=self.root, controller=self.controller,
                )
            elif _abandoned_epoch_repair_authority is not None:
                if not isinstance(expected, Lease):
                    raise FencedLease("owner repair requires the exact candidate lease")
                self._verify_abandoned_epoch_fence_authority(
                    state, expected, _abandoned_epoch_repair_authority,
                    root=self.root, controller=self.controller,
                )
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

    @staticmethod
    def _require_pending_continuation_target(document: Mapping[str, Any], task_id: str) -> str:
        pending = document.get("pending_continuation_task")
        if pending in (None, ""):
            return ""
        if not isinstance(pending, str) or not pending.strip():
            raise ControllerError("durable continuation target is malformed")
        if task_id != pending:
            raise ControllerError(
                f"task {task_id} does not match durable continuation target {pending}"
            )
        return pending

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
            with self._locked():
                state = _read_json(self.state_path)
                self._reject_pending_continuation(state)
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
        draining = checkpoint.admission_phase(document) == "draining"
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
            self._require_pending_continuation_target(document, task_id)
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
            if document.get("pending_continuation_task") == task_id:
                # The active pointer replaces the pending selection in the
                # same atomic checkpoint write. Before it, retries retain the
                # exact target; after it, claimed_no_session fences dispatch.
                document["pending_continuation_task"] = ""
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

    def _result(
        self,
        document: Mapping[str, Any],
        *,
        dispatched: bool = False,
        resumed: bool = False,
        task: str | None = None,
        session_id: str | None = None,
    ) -> ResumeResult:
        state = checkpoint.resume_state(dict(document))
        return ResumeResult(
            state=state,
            task=str(document.get("task") or "") if task is None else task,
            session_id=str(document.get("session_id") or "") if session_id is None else session_id,
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

        # Keep selection and the pending-target check in the same lease-fenced
        # transaction as the durable pre-launch reservation. Once the exact
        # target is reserved, claimed_no_session replaces the pending pointer
        # atomically; a competing scheduler then observes that reservation.
        with self.fence(lease) as current_lease:
            document = self._load_checkpoint()
            phase = checkpoint.admission_phase(document)
            state = checkpoint.resume_state(document)
            if phase == "terminal":
                return self._result(document, resumed=True)
            if phase == "draining":
                # A resumable deadline status can coexist with a durable drain.
                # Reconciliation may continue elsewhere, but this admission API
                # must never turn it into permission to select a fresh candidate.
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

            pending_target = str(document.get("pending_continuation_task") or "")
            if pending_target:
                matching = [candidate for candidate in candidates if self._assert_task_root(candidate) == pending_target]
                if len(matching) != 1:
                    raise ControllerError(
                        f"resume candidates must contain exactly one durable continuation target {pending_target}"
                    )
                selected = matching[0]
            else:
                selected = candidates[0]
            task_id = self._assert_task_root(selected)
            self._require_pending_continuation_target(document, task_id)
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
            if pending_target:
                before_launch["pending_continuation_task"] = ""
            document = checkpoint.write_checkpoint(self.checkpoint_path, before_launch)
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
        launch_state = outcome_state or ("running" if session_id else "claimed_no_session")
        selected_claim_id = str(claim)
        # The callback runs without the controller lock. Another worker may
        # reserve a sibling while it is in progress, so bind only our exact
        # reservation against the latest checkpoint instead of replacing it
        # with the stale pre-dispatch snapshot.
        with self.fence(current_lease) as current_lease:
            latest = self._load_checkpoint()
            if checkpoint.admission_phase(latest) == "terminal":
                raise ControllerError("controller checkpoint became terminal during task dispatch")
            rows = _active_tasks_from_checkpoint(latest, self.root)
            matches = [row for row in rows if row.get("task") == task_id]
            if len(matches) != 1 or matches[0].get("claim_id") != selected_claim_id:
                raise ControllerError(
                    f"task {task_id} no longer has its exact pending dispatch reservation"
                )
            selected_row = matches[0]
            if selected_row.get("state") != "claimed_no_session" or selected_row.get("session_id"):
                if (
                    selected_row.get("state") != "claimed_no_session"
                    and selected_row.get("state") == launch_state
                    and selected_row.get("session_id") == session_id
                ):
                    return self._result(
                        latest, dispatched=True, task=task_id, session_id=session_id,
                    )
                raise ControllerError(f"task {task_id} dispatch reservation was already changed")

            after_launch = dict(latest)
            after_launch.update({
                "task": task_id,
                "phase": str(selected.get("phase") or "dispatch"),
                "root": self.root,
                "controller": self.controller,
                "actor": actor,
                "claim_id": selected_claim_id,
                "epoch": current_lease.epoch,
                "lease_token": current_lease.token,
                "session_id": session_id,
                "state": launch_state,
                "status": launch_state,
                "next_action": "await session" if session_id else "inspect claimed task",
                "terminal": launch_state in TERMINAL_STATES,
            })
            has_active_rows = bool(latest.get("active_tasks"))
            if has_active_rows and launch_state not in TERMINAL_STATES:
                if launch_state not in checkpoint.ACTIVE_TASK_STATES:
                    raise ControllerError(
                        f"dispatch returned unsupported active task state {launch_state!r}"
                    )
                selected_row["session_id"] = session_id
                selected_row["state"] = launch_state
                saved = self._checkpoint_active_tasks(latest, rows, current_lease)
                return self._result(
                    saved, dispatched=True, task=task_id, session_id=session_id,
                )

            if has_active_rows and len(rows) > 1:
                raise ControllerError(
                    "cannot commit a terminal dispatch outcome while sibling tasks are active"
                )
            if has_active_rows:
                after_launch["active_tasks"] = []
            saved = checkpoint.write_checkpoint(self.checkpoint_path, after_launch)
            return self._result(saved, dispatched=True, task=task_id, session_id=session_id)

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

    def acknowledge_no_ready_halt(
        self, workflow_root: str, ready_task: str, *, lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Reopen only the known no-ready-work halt after a caller proves readiness.

        This deliberately does not dispatch or claim. The normal scheduler owns
        those operations after this fenced, durable acknowledgement transition.
        """
        if not workflow_root or not ready_task:
            raise ControllerError("workflow root and ready descendant are required")
        with self.fence(lease) as current:
            document = self._load_checkpoint()
            reason = str(document.get("terminal_reason") or "")
            recognized = reason.startswith(
                "USER_ACTION_REQUIRED: NO_READY_WORK: nonterminal descendant(s) remain with no ready work: "
            ) or reason.startswith(
                "USER_ACTION_REQUIRED: nonterminal descendant(s) remain with no ready work: "
            )
            if (
                checkpoint.admission_phase(document) != "terminal"
                or checkpoint.resume_state(document) not in {"blocked", "halted"}
                or not document.get("terminal")
                or not recognized
                or str(document.get("root") or "") != self.root
                or str(document.get("controller") or "") != self.controller
                or str(document.get("task") or "") != self.root
                or document.get("active_tasks")
                or document.get("actor")
                or document.get("claim_id")
                or document.get("session_id")
            ):
                raise ControllerError("checkpoint is not an eligible no-ready-work halt")
            acknowledgement = (
                f"authenticated NO_READY_WORK acknowledgement for {workflow_root}; "
                f"ready descendant {ready_task}"
            )
            if len(acknowledgement) > checkpoint.FIELD_MAX:
                raise ControllerError("workflow and ready task IDs are too long to acknowledge safely")
            prior_check = str(document.get("last_check") or "")
            prior_budget = checkpoint.FIELD_MAX - len(acknowledgement) - 2
            last_check = (
                f"{prior_check[:prior_budget]}; {acknowledgement}"
                if prior_check and prior_budget > 0 else acknowledgement
            )
            document.update({
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": "advancing",
                "status": "advancing",
                "terminal": False,
                "next_action": "select next ready descendant",
                "last_check": last_check,
            })
            return self._result(checkpoint.write_checkpoint(self.checkpoint_path, document), resumed=True)

    def cancel_expired_preidentity_task(
        self,
        task_id: str,
        claim_id: str,
        launch_id: str,
        *,
        commit_cancellation: Callable[[], None],
        lease: Lease | str | None = None,
        retirement_reason: str = "expired",
        _abandoned_epoch_repair_authority: Mapping[str, Any] | None = None,
    ) -> ResumeResult:
        """Fence a proved-expired identity-pending launch before clearing it.

        ``commit_cancellation`` must atomically persist the signed Herdr audit
        disposition and revoke its result channel. If checkpoint persistence
        then fails, a retry can finish clearing the pointer from that durable
        disposition without accepting a late result.
        """

        if retirement_reason not in {"expired", "superseded_legacy_scope", "owner_abandoned_epoch"}:
            raise ControllerError("unsupported preidentity retirement reason")
        if (retirement_reason == "owner_abandoned_epoch") != (
            _abandoned_epoch_repair_authority is not None
        ):
            raise ControllerError("owner-abandoned retirement requires its exact staged repair authority")
        for name, value in (("task", task_id), ("claim", claim_id), ("launch", launch_id)):
            if not value:
                raise ControllerError(f"preidentity cancellation {name} is required")
        with self.fence(
            lease, _abandoned_epoch_repair_authority=_abandoned_epoch_repair_authority,
        ) as current:
            document = self._load_checkpoint()
            expected_reason = f"USER_ACTION_REQUIRED: task {task_id} provider identity never resolved within "
            reason = str(document.get("terminal_reason") or "")
            rows = _active_tasks_from_checkpoint(document, self.root)
            retired_check = f"cancelled {retirement_reason} preidentity launch {launch_id} for {task_id}"
            terminal_halt = (
                checkpoint.admission_phase(document) == "terminal"
                and checkpoint.resume_state(document) == "blocked"
                and document.get("terminal") is True
            )
            retained_draining = (
                checkpoint.admission_phase(document) == "draining"
                and checkpoint.resume_state(document) == "draining"
                and document.get("terminal") is False
                and str(document.get("task") or "") == self.root
                and not document.get("actor") and not document.get("claim_id")
                and not document.get("session_id")
                and len(rows) == 1
                and rows[0].get("task") == task_id
                and rows[0].get("state") == "identity_pending"
                and not rows[0].get("session_id")
            )
            if (
                terminal_halt
                and str(document.get("root") or "") == self.root
                and str(document.get("controller") or "") == self.controller
                and not rows and document.get("task") == self.root
                and not document.get("active_tasks")
                and document.get("last_check") == retired_check
            ):
                commit_cancellation()
                if retirement_reason == "owner_abandoned_epoch":
                    state = _read_json(self.state_path)
                    pending = state.pop("abandoned_epoch_repair", None)
                    if isinstance(pending, Mapping):
                        state["abandoned_epoch_repair_receipt"] = dict(pending)
                        self._write_state(state)
                    elif not isinstance(state.get("abandoned_epoch_repair_receipt"), Mapping):
                        raise LeaseConflict("completed abandoned-epoch repair lost its signed intent")
                return self._result(document, resumed=True)
            eligible = (
                ((terminal_halt and reason.startswith(expected_reason)) or retained_draining)
                and (retirement_reason == "expired" or retained_draining)
                and str(document.get("root") or "") == self.root
                and str(document.get("controller") or "") == self.controller
                and len(rows) == 1
                and rows[0].get("task") == task_id
                and rows[0].get("claim_id") == claim_id
                and rows[0].get("state") == "identity_pending"
                and not rows[0].get("session_id")
            )
            if not eligible:
                raise ControllerError("checkpoint is not the exact eligible identity-pending task halt")
            # Cross-file crash semantics are deliberate: Herdr is atomically
            # fenced first, and only then is the checkpoint pointer cleared.
            commit_cancellation()
            document.update({
                "task": self.root,
                "phase": "controller",
                "actor": "",
                "claim_id": "",
                "session_id": "",
                "root": self.root,
                "controller": self.controller,
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": "blocked",
                "status": "blocked",
                "terminal": True,
                "active_tasks": [],
                "next_action": "explicitly continue with a distinct ready task",
                "last_check": retired_check,
            })
            result = self._result(checkpoint.write_checkpoint(self.checkpoint_path, document), resumed=True)
            if retirement_reason == "owner_abandoned_epoch":
                state = _read_json(self.state_path)
                pending = state.pop("abandoned_epoch_repair", None)
                if isinstance(pending, Mapping):
                    state["abandoned_epoch_repair_receipt"] = dict(pending)
                    self._write_state(state)
                elif not isinstance(state.get("abandoned_epoch_repair_receipt"), Mapping):
                    raise LeaseConflict("completed abandoned-epoch repair lost its signed intent")
            return result

    def cancel_superseded_legacy_preidentity_task(
        self,
        task_id: str,
        claim_id: str,
        launch_id: str,
        *,
        commit_cancellation: Callable[[], None],
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Retire an authenticated legacy launch after its owning scope ends."""
        return self.cancel_expired_preidentity_task(
            task_id, claim_id, launch_id,
            commit_cancellation=commit_cancellation, lease=lease,
            retirement_reason="superseded_legacy_scope",
        )

    def acknowledge_cancelled_preidentity_halt(
        self,
        workflow_root: str,
        cancelled_task: str,
        ready_task: str,
        *,
        authority_secret: str = "",
        lease: Lease | str | None = None,
    ) -> ResumeResult:
        """Reopen only a signed cancelled-preidentity halt for distinct ready work."""

        if not workflow_root or not cancelled_task or not ready_task:
            raise ControllerError("workflow root, cancelled task, and ready descendant are required")
        if ready_task == cancelled_task:
            raise ControllerError("continuation must select a task distinct from the cancelled launch")
        with self.fence(
            lease,
            _continuation_authority=(workflow_root, cancelled_task, ready_task, authority_secret),
        ) as current:
            document = self._load_checkpoint()
            reason = str(document.get("terminal_reason") or "")
            retired_check = str(document.get("last_check") or "")
            recognized = (
                retired_check.startswith("cancelled expired preidentity launch ")
                or retired_check.startswith("cancelled superseded_legacy_scope preidentity launch ")
            ) and retired_check.endswith(f" for {cancelled_task}")
            if (
                checkpoint.admission_phase(document) != "terminal"
                or checkpoint.resume_state(document) != "blocked"
                or not document.get("terminal")
                or not recognized
                or str(document.get("root") or "") != self.root
                or str(document.get("controller") or "") != self.controller
                or document.get("active_tasks")
                or document.get("actor")
                or document.get("claim_id")
                or document.get("session_id")
            ):
                raise ControllerError("checkpoint is not an eligible cancelled preidentity halt")
            acknowledgement = (
                f"authenticated cancelled-preidentity continuation for {workflow_root}; "
                f"ready descendant {ready_task} after {cancelled_task}"
            )
            if len(acknowledgement) > checkpoint.FIELD_MAX:
                raise ControllerError("workflow and task IDs are too long to acknowledge safely")
            document.update({
                "task": self.root,
                "phase": "controller",
                "root": self.root,
                "controller": self.controller,
                "epoch": current.epoch,
                "lease_token": current.token,
                "state": "advancing",
                "status": "advancing",
                "terminal": False,
                "next_action": "select explicitly verified ready descendant",
                "last_check": acknowledgement,
                "pending_continuation_task": ready_task,
            })
            return self._result(checkpoint.write_checkpoint(self.checkpoint_path, document), resumed=True)

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
            phase = checkpoint.admission_phase(document)
            if phase == "terminal" and not rows:
                # An immediate deadline must not reopen a completed/blocked
                # checkpoint and make future resumes schedule work again.
                return self._result(document, resumed=True)
            was_draining = phase == "draining" or (
                phase == "terminal" and bool(rows)
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
