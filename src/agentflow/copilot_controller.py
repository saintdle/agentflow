"""Controller-owned durable continuity for the optional Copilot proof route.

The authority key is supplied by the controller in memory. A distinct SQLite
database stores an authenticated expected head; SQLite coordinates changes to
that database and the ledger database in one rollback-journal transaction.
This is a local controller boundary, not a same-user isolation mechanism, and
never enables persistent worker admission.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentflow.copilot_sdk import (
    COPILOT_NEGATIVE_CONTROL_PROMPT,
    CopilotLaunchIdentity,
    CopilotLedgerHead,
    CopilotLedgerSnapshot,
    CopilotProofRun,
    CopilotSDKError,
    CopilotUsageReport,
    ProtectedCopilotLedger,
    derive_evidence_key,
)


_CHECKPOINT_DOMAIN = b"agentflow.copilot-controller.expected-head@1\0"
_AUTHORITY_KEY_DOMAIN = b"agentflow.copilot-controller.authority-key@1\0"
_APPROVAL_FIELDS = {
    "schema", "purpose", "decision", "approved_by", "workspace_root", "identity",
    "budget", "preflight",
}


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _scope(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentflow.copilot.{kind}\0{value}".encode("utf-8")).hexdigest()


def _identity_digest(identity: CopilotLaunchIdentity) -> str:
    return hashlib.sha256(_canonical(identity.to_dict())).hexdigest()


def _workspace_scope(workspace_root: Path) -> str:
    resolved = Path(workspace_root).expanduser().resolve(strict=False)
    return _scope("workspace-root", str(resolved))


def _checkpoint_key(authority_secret: str, identity: CopilotLaunchIdentity, workspace_scope: str) -> bytes:
    if not isinstance(authority_secret, str) or len(authority_secret) < 32:
        raise CopilotSDKError("controller Copilot authority key is unavailable")
    context = _AUTHORITY_KEY_DOMAIN + _canonical(identity.to_dict()) + b"\0" + workspace_scope.encode("ascii")
    return hmac.new(authority_secret.encode("utf-8"), context, hashlib.sha256).digest()


def _run_nonce(identity: CopilotLaunchIdentity) -> str:
    """Make the nonce deterministic for this controller-issued unique launch."""
    return hashlib.sha256(b"agentflow.copilot.launch-nonce\0" + _canonical(identity.to_dict())).hexdigest()


def load_approved_proof_manifest(
    path: Path,
    *,
    identity: CopilotLaunchIdentity,
    workspace_root: Path,
) -> Mapping[str, Any]:
    """Require an explicit private approval record before credential prompts or SDK startup."""
    try:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise CopilotSDKError("Copilot proof approval manifest path is invalid")
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
        if resolved == workspace or workspace in resolved.parents:
            raise CopilotSDKError("Copilot proof approval manifest must be outside the worker workspace")
        info = resolved.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or hasattr(os, "getuid") and info.st_uid != os.getuid()
            or info.st_size > 16_384
        ):
            raise CopilotSDKError("Copilot proof approval manifest is not a private controller file")
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except CopilotSDKError:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise CopilotSDKError("Copilot proof approval manifest is unavailable") from exc
    if not isinstance(document, dict) or set(document) != _APPROVAL_FIELDS:
        raise CopilotSDKError("Copilot proof approval manifest has an unsupported shape")
    budget = document.get("budget")
    preflight = document.get("preflight")
    if (
        document.get("schema") != 1
        or document.get("purpose") != "copilot-negative-control-proof"
        or document.get("decision") != "approved"
        or not isinstance(document.get("approved_by"), str)
        or not document["approved_by"].strip()
        or document.get("workspace_root") != str(workspace)
        or document.get("identity") != identity.to_dict()
        or not isinstance(budget, dict)
        or set(budget) != {"deadline_seconds", "retry_count"}
        or type(budget.get("deadline_seconds")) is not int
        or budget.get("deadline_seconds") != 180
        or type(budget.get("retry_count")) is not int
        or budget.get("retry_count") != 0
        or not isinstance(preflight, dict)
        or set(preflight) != {"controller_status", "independent_review_status", "proof_invocation_status"}
        or preflight.get("controller_status") != "passed"
        or preflight.get("independent_review_status") != "passed"
        or preflight.get("proof_invocation_status") != "not_started"
    ):
        raise CopilotSDKError("Copilot proof approval manifest is incomplete or mismatched")
    return document


@dataclass(frozen=True)
class CopilotControllerCheckpoint:
    """Authenticated high-water mark saved outside the ledger database."""

    head: CopilotLedgerHead
    identity_digest: str
    workspace_scope: str
    session_scope: str


class SQLiteCopilotControllerStore:
    """Atomically commit ledger rows and a separate authenticated head record.

    Both files must be private, controller-owned, and outside the worker
    workspace. The authority key is supplied by the caller and is never
    persisted. SQLite's rollback-journal super-journal coordinates writes to
    the two files in one transaction. This does not establish a same-UID
    security boundary; callers must keep admission disabled unless an
    independently enforced worker boundary protects both files and the key.
    """

    def __init__(
        self,
        ledger_path: Path,
        authority_path: Path,
        *,
        workspace_root: Path,
        identity: CopilotLaunchIdentity,
        authority_key: bytes,
    ) -> None:
        if not isinstance(identity, CopilotLaunchIdentity):
            raise CopilotSDKError("controller Copilot launch identity is unavailable")
        if not isinstance(authority_key, bytes) or len(authority_key) < 32:
            raise CopilotSDKError("controller Copilot checkpoint key is unavailable")
        try:
            self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
            self.identity = identity
            self.identity_digest = _identity_digest(identity)
            self.workspace_scope = _workspace_scope(self.workspace_root)
            self.anchor_id = _scope("protected-ledger", self.identity_digest)
            self._authority_key = authority_key
            self.ledger_path = self._private_database_path(ledger_path, "ledger")
            self.authority_path = self._private_database_path(authority_path, "authority")
            if self.ledger_path == self.authority_path:
                raise CopilotSDKError("Copilot ledger and authority stores must be separate files")
            self._initialize()
        except CopilotSDKError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise CopilotSDKError("Copilot controller continuity storage is unavailable") from exc

    def _private_database_path(self, path: Path, name: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise CopilotSDKError(f"Copilot controller {name} store path is invalid")
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
        if resolved == self.workspace_root or self.workspace_root in resolved.parents:
            raise CopilotSDKError("Copilot controller stores must be outside the worker workspace")
        parent_info = parent.stat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) & 0o077
            or hasattr(os, "getuid") and parent_info.st_uid != os.getuid()
        ):
            raise CopilotSDKError(f"Copilot controller {name} directory is not private and owned")
        if resolved.is_symlink():
            raise CopilotSDKError(f"Copilot controller {name} store cannot be a symlink")
        if resolved.exists():
            info = resolved.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                raise CopilotSDKError(f"Copilot controller {name} store is not private and owned")
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(resolved, flags, 0o600)
            os.close(descriptor)
        return resolved

    def _connect_ledger(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _connect_atomic(self) -> sqlite3.Connection:
        connection = self._connect_ledger()
        try:
            connection.execute("ATTACH DATABASE ? AS authority", (str(self.authority_path),))
            for schema in ("main", "authority"):
                mode = connection.execute(f"PRAGMA {schema}.journal_mode = DELETE").fetchone()
                if mode is None or mode[0].lower() != "delete":
                    raise CopilotSDKError("Copilot controller stores cannot enable atomic rollback journaling")
                connection.execute(f"PRAGMA {schema}.synchronous = FULL")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self) -> None:
        with closing(self._connect_atomic()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS copilot_ledger_head ("
                "anchor_id TEXT PRIMARY KEY, identity_digest TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, signature TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS copilot_ledger_row ("
                "anchor_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(anchor_id, sequence), "
                "FOREIGN KEY(anchor_id) REFERENCES copilot_ledger_head(anchor_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS authority.copilot_expected_head ("
                "anchor_id TEXT PRIMARY KEY, identity_digest TEXT NOT NULL, "
                "workspace_scope TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "signature TEXT NOT NULL, session_scope TEXT NOT NULL, mac TEXT NOT NULL)"
            )
        os.chmod(self.ledger_path, 0o600)
        os.chmod(self.authority_path, 0o600)

    def _read_checkpoint_row(self, connection: sqlite3.Connection, anchor_id: str) -> CopilotControllerCheckpoint | None:
        saved = connection.execute(
            "SELECT identity_digest, workspace_scope, sequence, signature, session_scope, mac "
            "FROM authority.copilot_expected_head WHERE anchor_id = ?",
            (anchor_id,),
        ).fetchone()
        if saved is None:
            return None
        identity_digest, workspace_scope, sequence, signature, session_scope, supplied_mac = saved
        try:
            head = CopilotLedgerHead(identity_digest, sequence, signature)
        except CopilotSDKError as exc:
            raise CopilotSDKError("Copilot controller expected head is malformed") from exc
        body = self._checkpoint_body(anchor_id, head, workspace_scope, session_scope)
        expected_mac = hmac.new(self._authority_key, _CHECKPOINT_DOMAIN + _canonical(body), hashlib.sha256).hexdigest()
        if (
            identity_digest != self.identity_digest
            or workspace_scope != self.workspace_scope
            or not self._valid_session_scope(session_scope, allow_empty=True)
            or not isinstance(supplied_mac, str)
            or not hmac.compare_digest(supplied_mac, expected_mac)
        ):
            raise CopilotSDKError("Copilot controller expected head authentication failed")
        return CopilotControllerCheckpoint(head, identity_digest, workspace_scope, session_scope)

    @staticmethod
    def _valid_session_scope(value: object, *, allow_empty: bool = False) -> bool:
        return (
            isinstance(value, str)
            and (allow_empty and value == "" or len(value) == 64)
            and all(character in "0123456789abcdef" for character in value)
        )

    def _checkpoint_body(
        self,
        anchor_id: str,
        head: CopilotLedgerHead,
        workspace_scope: str,
        session_scope: str,
    ) -> dict[str, Any]:
        return {
            "anchor_id": anchor_id,
            "identity_digest": head.identity_digest,
            "workspace_scope": workspace_scope,
            "sequence": head.sequence,
            "signature": head.signature,
            "session_scope": session_scope,
        }

    def read_expected_checkpoint(self) -> CopilotControllerCheckpoint | None:
        try:
            with closing(self._connect_atomic()) as connection:
                connection.execute("BEGIN")
                checkpoint = self._read_checkpoint_row(connection, self.anchor_id)
                connection.commit()
                return checkpoint
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CopilotSDKError("Copilot controller expected head could not be read") from exc

    def read_ledger(self, anchor_id: str) -> CopilotLedgerSnapshot | None:
        try:
            with closing(self._connect_ledger()) as connection:
                connection.execute("BEGIN")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature FROM copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                if saved is None:
                    stray = connection.execute(
                        "SELECT 1 FROM copilot_ledger_row WHERE anchor_id = ? LIMIT 1", (anchor_id,),
                    ).fetchone()
                    connection.commit()
                    if stray is not None:
                        raise CopilotSDKError("Copilot controller ledger has rows without a head")
                    return None
                rows = connection.execute(
                    "SELECT payload FROM copilot_ledger_row WHERE anchor_id = ? ORDER BY sequence", (anchor_id,),
                ).fetchall()
                connection.commit()
            parsed = tuple(json.loads(row[0]) for row in rows)
            if any(not isinstance(row, Mapping) for row in parsed):
                raise CopilotSDKError("Copilot controller ledger has malformed rows")
            return CopilotLedgerSnapshot(CopilotLedgerHead(*saved), parsed)
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CopilotSDKError("Copilot controller ledger could not be read") from exc

    def assert_unused(self) -> None:
        snapshot = self.read_ledger(self.anchor_id)
        checkpoint = self.read_expected_checkpoint()
        if snapshot is not None or checkpoint is not None:
            raise CopilotSDKError("Copilot proof launch was already used; resume is unsupported")

    def compare_and_append(
        self,
        anchor_id: str,
        expected: CopilotLedgerHead | None,
        signed_row: Mapping[str, Any],
        replacement: CopilotLedgerHead,
    ) -> bool:
        row = dict(signed_row)
        try:
            with closing(self._connect_atomic()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature FROM main.copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                current = None if saved is None else CopilotLedgerHead(*saved)
                checkpoint = self._read_checkpoint_row(connection, anchor_id)
                if current != expected:
                    connection.rollback()
                    return False
                if (current is None) != (checkpoint is None):
                    raise CopilotSDKError("Copilot controller expected head is missing or orphaned")
                if checkpoint is not None and checkpoint.head != current:
                    raise CopilotSDKError("Copilot controller expected head differs from the ledger")
                expected_sequence = 0 if current is None else current.sequence + 1
                expected_previous = "0" * 64 if current is None else current.signature
                if (
                    anchor_id != self.anchor_id
                    or row.get("sequence") != expected_sequence
                    or row.get("previous_signature") != expected_previous
                    or row.get("signature") != replacement.signature
                    or replacement.identity_digest != self.identity_digest
                    or replacement.sequence != expected_sequence
                    or current is not None and replacement.identity_digest != current.identity_digest
                ):
                    connection.rollback()
                    return False
                session_scope = "" if checkpoint is None else checkpoint.session_scope
                row_session_scope = row.get("session_scope")
                if row.get("kind") == "launch":
                    if not self._valid_session_scope(row_session_scope) or session_scope:
                        connection.rollback()
                        return False
                    session_scope = row_session_scope
                elif isinstance(row_session_scope, str) and row_session_scope:
                    if not self._valid_session_scope(row_session_scope) or session_scope != row_session_scope:
                        connection.rollback()
                        return False
                if current is None:
                    connection.execute(
                        "INSERT INTO main.copilot_ledger_head(anchor_id, identity_digest, sequence, signature) "
                        "VALUES (?, ?, ?, ?)",
                        (anchor_id, replacement.identity_digest, replacement.sequence, replacement.signature),
                    )
                else:
                    connection.execute(
                        "UPDATE main.copilot_ledger_head SET sequence = ?, signature = ? "
                        "WHERE anchor_id = ? AND identity_digest = ? AND sequence = ? AND signature = ?",
                        (replacement.sequence, replacement.signature, anchor_id,
                         current.identity_digest, current.sequence, current.signature),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        connection.rollback()
                        return False
                connection.execute(
                    "INSERT INTO main.copilot_ledger_row(anchor_id, sequence, payload) VALUES (?, ?, ?)",
                    (anchor_id, expected_sequence, json.dumps(row, sort_keys=True, separators=(",", ":"))),
                )
                body = self._checkpoint_body(anchor_id, replacement, self.workspace_scope, session_scope)
                mac = hmac.new(
                    self._authority_key, _CHECKPOINT_DOMAIN + _canonical(body), hashlib.sha256,
                ).hexdigest()
                connection.execute(
                    "INSERT INTO authority.copilot_expected_head "
                    "(anchor_id, identity_digest, workspace_scope, sequence, signature, session_scope, mac) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(anchor_id) DO UPDATE SET identity_digest=excluded.identity_digest, "
                    "workspace_scope=excluded.workspace_scope, sequence=excluded.sequence, "
                    "signature=excluded.signature, session_scope=excluded.session_scope, mac=excluded.mac",
                    (anchor_id, replacement.identity_digest, self.workspace_scope,
                     replacement.sequence, replacement.signature, session_scope, mac),
                )
                connection.commit()
            return True
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CopilotSDKError("Copilot controller ledger and expected head could not commit atomically") from exc


class CopilotControllerContinuity:
    """Bind a fresh proof and restart verification to explicit controller authority."""

    def __init__(
        self,
        *,
        identity: CopilotLaunchIdentity,
        authority_secret: str,
        workspace_root: Path,
        ledger_path: Path,
        authority_path: Path,
    ) -> None:
        self.identity = identity
        workspace = Path(workspace_root).expanduser().resolve(strict=False)
        workspace_scope = _workspace_scope(workspace)
        checkpoint_key = _checkpoint_key(authority_secret, identity, workspace_scope)
        self.evidence_key = derive_evidence_key(
            authority_secret, identity, run_nonce=_run_nonce(identity),
        )
        self.store = SQLiteCopilotControllerStore(
            ledger_path,
            authority_path,
            workspace_root=workspace,
            identity=identity,
            authority_key=checkpoint_key,
        )

    def new_ledger(self) -> ProtectedCopilotLedger:
        self.store.assert_unused()
        return ProtectedCopilotLedger(
            identity=self.identity,
            evidence_key=self.evidence_key,
            anchor_store=self.store,
        )

    def verify_existing(self) -> tuple[Mapping[str, Any], ...]:
        checkpoint = self.store.read_expected_checkpoint()
        if checkpoint is None:
            raise CopilotSDKError("Copilot controller expected head is missing")
        rows = ProtectedCopilotLedger.verify_existing(
            identity=self.identity,
            evidence_key=self.evidence_key,
            anchor_store=self.store,
            expected_head=checkpoint.head,
        )
        launch_rows = [row for row in rows if row.get("kind") == "launch"]
        if len(launch_rows) != 1 or launch_rows[0].get("session_scope") != checkpoint.session_scope:
            raise CopilotSDKError("Copilot controller checkpoint belongs to another native session")
        end_rows = [row for row in rows if row.get("kind") == "end"]
        if (
            not end_rows
            or rows[-1].get("kind") != "end"
            or rows[-1].get("request_completed") is not True
            or rows[-1].get("reason_codes") != []
            or rows[-1].get("usage_count", 0) < 1
        ):
            raise CopilotSDKError("Copilot proof is incomplete or did not verify")
        return rows

    async def run_proof(
        self,
        *,
        base_directory: Path,
        github_token: str,
    ) -> CopilotUsageReport:
        """Run exactly one approved no-tools turn and verify its durable checkpoint."""
        ledger = self.new_ledger()
        async with await CopilotProofRun.open(
            identity=self.identity,
            evidence_key=self.evidence_key,
            protected_ledger=ledger,
            base_directory=base_directory,
            workspace_root=self.store.workspace_root,
            github_token=github_token,
        ) as run:
            report = await run.send_once(COPILOT_NEGATIVE_CONTROL_PROMPT)
        if not report.verified or report.persistent_admission:
            raise CopilotSDKError("Copilot proof did not meet its no-tools verification checks")
        rows = self.verify_existing()
        if list(rows) != list(report.ledger):
            raise CopilotSDKError("Copilot proof report differs from its authenticated controller ledger")
        return report


__all__ = [
    "CopilotControllerCheckpoint",
    "CopilotControllerContinuity",
    "SQLiteCopilotControllerStore",
    "load_approved_proof_manifest",
]
