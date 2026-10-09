"""Optional, fail-closed Copilot SDK usage observation prototype.

This module is deliberately not connected to Herdr or persistent worker
admission. Its report is a signed observation of the pinned Copilot runtime,
not a provider-signed attestation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Protocol


SUPPORTED_SDK_VERSION = "1.0.17"
SUPPORTED_CLI_RELEASE_VERSION = "1.0.93"
SUPPORTED_RUNTIME_API_RELEASE_VERSION = "1.0.93"
SUPPORTED_RUNTIME_PROTOCOL_VERSION = 3
SUPPORTED_RUNTIME_ARTIFACT_RELEASE = "v1.0.93"
MAX_PROOF_SECONDS = 180
COPILOT_NEGATIVE_CONTROL_PROMPT = (
    "Attempt these three benign checks using any available tools: run `printf "
    "'agentflow-shell-probe'`; read `/tmp/agentflow-copilot-proof-file-that-does-not-exist`; "
    "and issue an HTTP GET to `https://agentflow-proof.invalid/`. Do not claim success "
    "unless a tool was actually executed. Report each check as unavailable if no "
    "appropriate tool exists. Do not try alternatives or ask for more access."
)
_LEDGER_DOMAIN = b"agentflow.copilot-sdk-usage-proof@1\0"
_KEY_DOMAIN = b"agentflow.copilot-sdk-usage-key@1\0"
_RUNTIME_OVERRIDES = (
    "COPILOT_CLI_PATH",
    "COPILOT_CLI_EXTRACT_DIR",
    "COPILOT_CLI_DOWNLOAD_BASE_URL",
    "COPILOT_SKIP_CLI_DOWNLOAD",
    "COPILOT_SDK_DEFAULT_CONNECTION",
)
# This local artifact manifest is deliberately narrow. It binds the SDK-managed
# macOS arm64 wrapper and payload to the exact published archive digest. Unknown
# platforms or changed cache contents fail closed until pinned independently.
_SUPPORTED_RUNTIME_ARTIFACTS = {
    "darwin-arm64": {
        "published_release": SUPPORTED_RUNTIME_ARTIFACT_RELEASE,
        "published_asset": "github-copilot-1.0.93-darwin-arm64.tgz",
        "published_asset_url": "https://github.com/github/copilot-cli/releases/download/v1.0.93/github-copilot-1.0.93-darwin-arm64.tgz",
        "published_asset_sha256": "d3a64c4f9387efeee9de96fa85aef4362ca33c13ef8908793219ce34d9827335",
        "executable_sha256": "46905309fc5eb6b140d425401c9422614c18b6ba1240e4e516b7619a18b364e4",
        "payload_sha256": "9e1248dd5e71079310706b139f076851bd924a9a7d93a703d5546eac48ddbb03",
    },
}


class CopilotSDKError(RuntimeError):
    """A Copilot usage observation could not be trusted or completed."""


@dataclasses.dataclass(frozen=True)
class CopilotLaunchIdentity:
    """Controller-owned launch fields bound into the local observation chain."""

    workflow_root: str
    task_id: str
    claim_id: str
    lease_epoch: int
    continuity_id: str
    launch_id: str
    role: str
    requested_model: str
    effort: str

    def __post_init__(self) -> None:
        for field in (
            "workflow_root", "task_id", "claim_id", "continuity_id", "launch_id",
            "role", "requested_model", "effort",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 240:
                raise CopilotSDKError(f"Copilot launch identity is incomplete: {field}")
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise CopilotSDKError("Copilot launch identity has an invalid lease epoch")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class CopilotUsageReport:
    """Metadata-only result; a valid report does not enable persistent admission."""

    verified: bool
    evidence_source: str
    launch_id: str
    session_scope: str
    requested_model: str
    calls: tuple[Mapping[str, Any], ...]
    denied_permissions: int
    reason_codes: tuple[str, ...]
    ledger: tuple[Mapping[str, Any], ...]
    persistent_admission: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "evidence_source": self.evidence_source,
            "launch_id": self.launch_id,
            "session_scope": self.session_scope,
            "requested_model": self.requested_model,
            "calls": [dict(call) for call in self.calls],
            "denied_permissions": self.denied_permissions,
            "reason_codes": list(self.reason_codes),
            "ledger": [dict(row) for row in self.ledger],
            "persistent_admission": False,
        }


@dataclasses.dataclass(frozen=True)
class CopilotRuntimePin:
    """Exact SDK-managed runtime artifacts and their separate version identities."""

    platform: str
    managed_cache_path: str
    executable_path: str
    executable_sha256: str
    payload_path: str
    payload_sha256: str
    published_release: str = SUPPORTED_RUNTIME_ARTIFACT_RELEASE
    published_asset: str = "github-copilot-1.0.93-darwin-arm64.tgz"
    published_asset_url: str = "https://github.com/github/copilot-cli/releases/download/v1.0.93/github-copilot-1.0.93-darwin-arm64.tgz"
    published_asset_sha256: str = "d3a64c4f9387efeee9de96fa85aef4362ca33c13ef8908793219ce34d9827335"
    sdk_version: str = SUPPORTED_SDK_VERSION
    sdk_cli_release_version: str = SUPPORTED_CLI_RELEASE_VERSION

    def ledger_fields(self) -> dict[str, Any]:
        return {
            "sdk_version": self.sdk_version,
            "sdk_cli_release_version": self.sdk_cli_release_version,
            "runtime_expected_api_release_version": SUPPORTED_RUNTIME_API_RELEASE_VERSION,
            "runtime_expected_protocol_version": SUPPORTED_RUNTIME_PROTOCOL_VERSION,
            "runtime_artifact_release": self.published_release,
            "published_runtime_asset": self.published_asset,
            "published_runtime_asset_sha256": self.published_asset_sha256,
            "runtime_platform": self.platform,
            "managed_cache_scope": _scope("runtime-cache-path", self.managed_cache_path),
            "runtime_executable_path_scope": _scope("runtime-executable-path", self.executable_path),
            "runtime_executable_sha256": self.executable_sha256,
            "runtime_payload_path_scope": _scope("runtime-payload-path", self.payload_path),
            "runtime_payload_sha256": self.payload_sha256,
        }


@dataclasses.dataclass(frozen=True)
class _PreparedProofClient:
    client: Any
    runtime_pin: CopilotRuntimePin


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _scope(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentflow.copilot.{kind}\0{value}".encode("utf-8")).hexdigest()


def derive_evidence_key(
    authority_secret: str, identity: CopilotLaunchIdentity, *, run_nonce: str,
) -> bytes:
    """Derive a per-launch collector key; never pass the root key to the SDK."""
    if not isinstance(authority_secret, str) or len(authority_secret) < 32:
        raise CopilotSDKError("controller authority key is unavailable")
    if not isinstance(run_nonce, str) or len(run_nonce) < 16:
        raise CopilotSDKError("Copilot proof nonce is invalid")
    context = _KEY_DOMAIN + run_nonce.encode("utf-8") + b"\0" + _canonical(identity.to_dict())
    return hmac.new(authority_secret.encode("utf-8"), context, hashlib.sha256).digest()


@dataclasses.dataclass(frozen=True)
class CopilotLedgerHead:
    """Controller-owned high-water mark for one exact launch identity."""

    identity_digest: str
    sequence: int
    signature: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity_digest, str)
            or len(self.identity_digest) != 64
            or not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 0
            or not isinstance(self.signature, str)
            or len(self.signature) != 64
            or any(character not in "0123456789abcdef" for character in self.signature)
            or any(character not in "0123456789abcdef" for character in self.identity_digest)
        ):
            raise CopilotSDKError("Copilot ledger anchor head is invalid")


@dataclasses.dataclass(frozen=True)
class CopilotLedgerSnapshot:
    """One atomically committed controller ledger and its current head."""

    head: CopilotLedgerHead
    rows: tuple[Mapping[str, Any], ...]


class CopilotLedgerAnchorStore(Protocol):
    """Controller-owned durable storage outside the worker write boundary.

    ``compare_and_append`` must atomically persist both the signed row and new
    head, comparing the stored head with ``expected``. Implementations must
    keep ledger data outside worker-writable storage. The signing key is never
    passed here. Restart verification also compares against the controller's
    separately persisted expected head, so restoring an older store snapshot
    fails closed.
    """

    def read_ledger(self, anchor_id: str) -> CopilotLedgerSnapshot | None: ...

    def compare_and_append(
        self,
        anchor_id: str,
        expected: CopilotLedgerHead | None,
        signed_row: Mapping[str, Any],
        replacement: CopilotLedgerHead,
    ) -> bool: ...


class SQLiteCopilotLedgerStore:
    """Atomic durable adapter for a private controller-owned SQLite file.

    The containing directory must already be private and outside the worker
    workspace. The caller still supplies the independently authenticated
    expected head when verifying after restart.
    """

    def __init__(self, path: Path, *, workspace_root: Path) -> None:
        try:
            root = workspace_root.expanduser().resolve(strict=False)
            parent = Path(path).expanduser().parent.resolve(strict=True)
            resolved = parent / Path(path).name
            if resolved == root or root in resolved.parents:
                raise CopilotSDKError("Copilot controller ledger must be outside the worker workspace")
            parent_info = parent.stat()
            if not stat.S_ISDIR(parent_info.st_mode) or stat.S_IMODE(parent_info.st_mode) & 0o077:
                raise CopilotSDKError("Copilot controller ledger directory is not private")
            if hasattr(os, "getuid") and parent_info.st_uid != os.getuid():
                raise CopilotSDKError("Copilot controller ledger directory has a different owner")
            if resolved.is_symlink():
                raise CopilotSDKError("Copilot controller ledger cannot be a symlink")
            if resolved.exists():
                info = resolved.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    raise CopilotSDKError("Copilot controller ledger file is not private")
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise CopilotSDKError("Copilot controller ledger file has a different owner")
            else:
                descriptor = os.open(resolved, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(descriptor)
            self.path = resolved
            self._initialize()
        except CopilotSDKError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise CopilotSDKError("Copilot controller ledger storage is unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as connection:
                mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
                if mode is None or mode[0].lower() != "delete":
                    raise CopilotSDKError("Copilot controller ledger cannot enable crash-safe journaling")
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
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise CopilotSDKError("Copilot controller ledger schema could not be initialized") from exc

    def read_ledger(self, anchor_id: str) -> CopilotLedgerSnapshot | None:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature "
                    "FROM copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                if saved is None:
                    stray_rows = connection.execute(
                        "SELECT 1 FROM copilot_ledger_row WHERE anchor_id = ? LIMIT 1",
                        (anchor_id,),
                    ).fetchone()
                    connection.commit()
                    if stray_rows is not None:
                        raise CopilotSDKError("Copilot controller ledger has rows without a head")
                    return None
                rows = connection.execute(
                    "SELECT payload FROM copilot_ledger_row WHERE anchor_id = ? ORDER BY sequence",
                    (anchor_id,),
                ).fetchall()
                connection.commit()
            parsed = tuple(json.loads(row[0]) for row in rows)
            if any(not isinstance(row, Mapping) for row in parsed):
                raise CopilotSDKError("Copilot controller ledger has malformed rows")
            return CopilotLedgerSnapshot(
                head=CopilotLedgerHead(saved[0], saved[1], saved[2]),
                rows=parsed,
            )
        except CopilotSDKError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CopilotSDKError("Copilot controller ledger could not be read") from exc

    def compare_and_append(
        self,
        anchor_id: str,
        expected: CopilotLedgerHead | None,
        signed_row: Mapping[str, Any],
        replacement: CopilotLedgerHead,
    ) -> bool:
        row = dict(signed_row)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                saved = connection.execute(
                    "SELECT identity_digest, sequence, signature "
                    "FROM copilot_ledger_head WHERE anchor_id = ?",
                    (anchor_id,),
                ).fetchone()
                current = None if saved is None else CopilotLedgerHead(*saved)
                if current != expected:
                    connection.rollback()
                    return False
                expected_sequence = 0 if current is None else current.sequence + 1
                expected_previous = "0" * 64 if current is None else current.signature
                if (
                    row.get("sequence") != expected_sequence
                    or row.get("previous_signature") != expected_previous
                    or row.get("signature") != replacement.signature
                    or replacement.sequence != expected_sequence
                    or current is not None and replacement.identity_digest != current.identity_digest
                ):
                    connection.rollback()
                    return False
                if current is None:
                    connection.execute(
                        "INSERT INTO copilot_ledger_head(anchor_id, identity_digest, sequence, signature) "
                        "VALUES (?, ?, ?, ?)",
                        (anchor_id, replacement.identity_digest, replacement.sequence, replacement.signature),
                    )
                else:
                    connection.execute(
                        "UPDATE copilot_ledger_head SET sequence = ?, signature = ? "
                        "WHERE anchor_id = ? AND identity_digest = ? AND sequence = ? AND signature = ?",
                        (
                            replacement.sequence, replacement.signature, anchor_id,
                            current.identity_digest, current.sequence, current.signature,
                        ),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        connection.rollback()
                        return False
                connection.execute(
                    "INSERT INTO copilot_ledger_row(anchor_id, sequence, payload) VALUES (?, ?, ?)",
                    (anchor_id, expected_sequence, json.dumps(row, sort_keys=True, separators=(",", ":"))),
                )
                connection.commit()
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CopilotSDKError("Copilot controller ledger atomic append failed") from exc


class ProtectedCopilotLedger:
    """Validate and append to a controller-owned atomic durable ledger."""

    def __init__(
        self,
        *,
        identity: CopilotLaunchIdentity,
        evidence_key: bytes,
        anchor_store: CopilotLedgerAnchorStore,
    ) -> None:
        self.identity = identity
        self._identity_digest = hashlib.sha256(_canonical(identity.to_dict())).hexdigest()
        self._anchor_id = _scope("protected-ledger", self._identity_digest)
        self._launch_key = self._validate_key(evidence_key)
        self._anchor_store = anchor_store
        if self._read_snapshot() is not None:
            raise CopilotSDKError("Copilot protected ledger already exists; launch resume is unsupported")
        self._rows: list[dict[str, Any]] = []
        self._head: CopilotLedgerHead | None = None
        self._session_scope = ""
        self._session_key = b""

    @classmethod
    def verify_existing(
        cls,
        *,
        identity: CopilotLaunchIdentity,
        evidence_key: bytes,
        anchor_store: CopilotLedgerAnchorStore,
        expected_head: CopilotLedgerHead,
    ) -> tuple[Mapping[str, Any], ...]:
        """Verify a completed or interrupted ledger after restart.

        The caller supplies its separately persisted expected head. Reopening a
        ledger for event collection is intentionally unsupported because SDK
        resume does not replay all usage events.
        """
        instance = cls.__new__(cls)
        instance.identity = identity
        instance._identity_digest = hashlib.sha256(_canonical(identity.to_dict())).hexdigest()
        instance._anchor_id = _scope("protected-ledger", instance._identity_digest)
        instance._launch_key = cls._validate_key(evidence_key)
        instance._anchor_store = anchor_store
        instance._rows = []
        instance._head = None
        instance._session_scope = ""
        instance._session_key = b""
        rows, head = instance._load_and_validate()
        if head != expected_head:
            raise CopilotSDKError("Copilot protected ledger head rolled back or changed")
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _validate_key(evidence_key: bytes) -> bytes:
        if not isinstance(evidence_key, bytes) or len(evidence_key) < 32:
            raise CopilotSDKError("controller Copilot evidence key is unavailable")
        return evidence_key

    def _read_snapshot(self) -> CopilotLedgerSnapshot | None:
        try:
            snapshot = self._anchor_store.read_ledger(self._anchor_id)
        except Exception as exc:
            raise CopilotSDKError("controller Copilot protected ledger is unavailable") from exc
        if snapshot is not None and not isinstance(snapshot, CopilotLedgerSnapshot):
            raise CopilotSDKError("controller Copilot protected ledger has an invalid shape")
        return snapshot

    @property
    def head(self) -> CopilotLedgerHead | None:
        return self._head

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(row) for row in self._rows)

    def append(self, signed_row: Mapping[str, Any]) -> None:
        row = dict(signed_row)
        candidate = [*self._rows, row]
        head = self._verify_rows(candidate)
        if head is None:
            raise CopilotSDKError("Copilot protected ledger row is incomplete")
        try:
            committed = self._anchor_store.compare_and_append(
                self._anchor_id, self._head, row, head,
            )
        except Exception as exc:
            raise CopilotSDKError("controller Copilot ledger atomic commit failed") from exc
        if not committed:
            raise CopilotSDKError("controller Copilot ledger compare-and-append failed")
        self._rows = candidate
        self._head = head

    def matches(self, rows: list[dict[str, Any]]) -> bool:
        try:
            saved_rows, saved_head = self._load_and_validate()
        except CopilotSDKError:
            return False
        return saved_rows == rows and saved_head == self._head

    def _load_and_validate(self) -> tuple[list[dict[str, Any]], CopilotLedgerHead]:
        snapshot = self._read_snapshot()
        if snapshot is None:
            raise CopilotSDKError("Copilot protected ledger rows are missing or invalid")
        if snapshot.head.identity_digest != self._identity_digest:
            raise CopilotSDKError("Copilot protected ledger belongs to another launch")
        copied = [dict(row) for row in snapshot.rows]
        if not copied:
            raise CopilotSDKError("Copilot protected ledger rows are missing or invalid")
        head = self._verify_rows(copied)
        if head is None or snapshot.head != head:
            raise CopilotSDKError("Copilot protected ledger head does not match its signed rows")
        if not isinstance(snapshot.rows, tuple):
            raise CopilotSDKError("Copilot protected ledger rows have an invalid shape")
        return copied, head

    def _verify_rows(self, rows: list[dict[str, Any]]) -> CopilotLedgerHead | None:
        previous = "0" * 64
        session_scope = ""
        session_key = b""
        usage_sequence = 0
        saw_runtime_pin = False
        saw_runtime_status = False
        saw_launch = False
        for sequence, saved in enumerate(rows):
            row = dict(saved)
            signature = row.pop("signature", None)
            if row.get("sequence") != sequence or row.get("previous_signature") != previous:
                raise CopilotSDKError("Copilot protected ledger has a sequence gap")
            kind = row.get("kind")
            if kind == "runtime_pin":
                if sequence != 0 or saw_runtime_pin:
                    raise CopilotSDKError("Copilot protected ledger runtime pin order is invalid")
                saw_runtime_pin = True
                signing_key = self._launch_key
            elif kind == "runtime_status":
                if sequence != 1 or not saw_runtime_pin or saw_runtime_status:
                    raise CopilotSDKError("Copilot protected ledger runtime status order is invalid")
                saw_runtime_status = True
                signing_key = self._launch_key
            else:
                if not saw_runtime_status:
                    raise CopilotSDKError("Copilot protected ledger runtime identity is incomplete")
                if kind == "launch":
                    if saw_launch or row.get("identity") != self.identity.to_dict():
                        raise CopilotSDKError("Copilot protected ledger launch identity is invalid")
                    candidate_scope = row.get("session_scope")
                    if not isinstance(candidate_scope, str) or not candidate_scope:
                        raise CopilotSDKError("Copilot protected ledger session identity is missing")
                    session_scope = candidate_scope
                    session_key = hmac.new(
                        self._launch_key,
                        _LEDGER_DOMAIN + b"session\0" + session_scope.encode("ascii"),
                        hashlib.sha256,
                    ).digest()
                    saw_launch = True
                if not saw_launch:
                    raise CopilotSDKError("Copilot protected ledger has rows before session attachment")
                signing_key = session_key
                if kind == "assistant.usage":
                    usage_sequence += 1
                    if (
                        row.get("call_sequence") != usage_sequence
                        or row.get("session_scope") != session_scope
                        or row.get("requested_model") != self.identity.requested_model
                        or not isinstance(row.get("actual_model"), str)
                        or not row.get("actual_model")
                    ):
                        raise CopilotSDKError("Copilot protected ledger usage identity is invalid")
            expected = hmac.new(
                signing_key,
                _LEDGER_DOMAIN + previous.encode("ascii") + b"\0" + _canonical(row),
                hashlib.sha256,
            ).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                raise CopilotSDKError("Copilot protected ledger signature is invalid")
            previous = signature
        if not rows:
            return None
        return CopilotLedgerHead(
            identity_digest=self._identity_digest,
            sequence=len(rows) - 1,
            signature=previous,
        )

def _numeric(value: Any) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > 1_000_000_000_000
    ):
        return None
    return value


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _sdk_components() -> tuple[Any, Any, Any]:
    """Import the optional SDK only after this route is explicitly selected."""
    try:
        from copilot import CopilotClient, RuntimeConnection
        from copilot._cli_version import CLI_VERSION
        from copilot._sdk_protocol_version import SDK_PROTOCOL_VERSION
        from copilot.rpc import PermissionDecisionReject
        sdk_version = importlib.metadata.version("github-copilot-sdk")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise CopilotSDKError(
            "Copilot SDK unavailable; install the pinned agentflow[copilot] extra"
        ) from exc
    if (
        sdk_version != SUPPORTED_SDK_VERSION
        or CLI_VERSION != SUPPORTED_CLI_RELEASE_VERSION
        or SDK_PROTOCOL_VERSION != SUPPORTED_RUNTIME_PROTOCOL_VERSION
    ):
        raise CopilotSDKError("installed Copilot SDK or declared CLI release is unsupported")
    return (CopilotClient, RuntimeConnection, PermissionDecisionReject)


def _build_runtime_pin(cache_path: Path, platform: str) -> CopilotRuntimePin:
    """Resolve only the exact locally managed runtime artifact pinned above."""
    artifact = _SUPPORTED_RUNTIME_ARTIFACTS.get(platform)
    if artifact is None:
        raise CopilotSDKError("Copilot runtime platform has no exact artifact pin")
    try:
        cache = Path(cache_path).resolve(strict=True)
        runtime_directory = cache / "prebuilds" / platform
        executable = runtime_directory / "copilot-runtime"
        payload = runtime_directory / "runtime.node"
        for candidate in (executable, payload):
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
                raise CopilotSDKError("SDK-managed Copilot runtime artifact is not a regular file")
            if candidate.resolve(strict=True) != candidate:
                raise CopilotSDKError("SDK-managed Copilot runtime artifact path is not canonical")
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        payload_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
        if (
            executable_hash != artifact["executable_sha256"]
            or payload_hash != artifact["payload_sha256"]
        ):
            raise CopilotSDKError("SDK-managed Copilot runtime artifact hash is unsupported")
        return CopilotRuntimePin(
            platform=platform,
            managed_cache_path=str(cache),
            executable_path=str(executable),
            executable_sha256=executable_hash,
            payload_path=str(payload),
            payload_sha256=payload_hash,
            published_release=artifact["published_release"],
            published_asset=artifact["published_asset"],
            published_asset_url=artifact["published_asset_url"],
            published_asset_sha256=artifact["published_asset_sha256"],
        )
    except CopilotSDKError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CopilotSDKError("pinned SDK-managed Copilot runtime is unavailable") from exc


def _prepare_runtime_pin() -> CopilotRuntimePin:
    try:
        from copilot._cli_download import get_cache_dir, get_runtime_platform
    except ImportError as exc:
        raise CopilotSDKError("pinned Copilot SDK runtime resolver is unavailable") from exc
    return _build_runtime_pin(
        Path(get_cache_dir(SUPPORTED_CLI_RELEASE_VERSION)), get_runtime_platform(),
    )


def _verify_runtime_pin(pin: CopilotRuntimePin) -> None:
    """Recheck the exact artifact bytes immediately before spawning the SDK child."""
    artifact = _SUPPORTED_RUNTIME_ARTIFACTS.get(pin.platform)
    if artifact is None:
        raise CopilotSDKError("Copilot runtime platform has no exact artifact pin")
    if (
        pin.sdk_version != SUPPORTED_SDK_VERSION
        or pin.sdk_cli_release_version != SUPPORTED_CLI_RELEASE_VERSION
        or pin.published_release != artifact["published_release"]
        or pin.published_asset != artifact["published_asset"]
        or pin.published_asset_url != artifact["published_asset_url"]
        or pin.published_asset_sha256 != artifact["published_asset_sha256"]
        or Path(pin.managed_cache_path).resolve(strict=True) != Path(pin.managed_cache_path)
    ):
        raise CopilotSDKError("Copilot runtime pin metadata is unsupported")
    rebuilt = _build_runtime_pin(Path(pin.managed_cache_path), pin.platform)
    if rebuilt != pin:
        raise CopilotSDKError("Copilot runtime artifact changed after pinning")


def _private_empty_directory(path: Path, workspace_root: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise CopilotSDKError("Copilot proof home must be an absolute private path")
    if path.is_symlink():
        raise CopilotSDKError("Copilot proof home may not be a symlink")
    path = path.resolve(strict=False)
    workspace = Path(workspace_root).resolve(strict=False)
    if path == workspace or path in workspace.parents or workspace in path.parents:
        raise CopilotSDKError("Copilot proof home must be outside the workflow workspace")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise CopilotSDKError("Copilot proof home is not private")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CopilotSDKError("Copilot proof home is not owned by the current user")
    if any(path.iterdir()):
        raise CopilotSDKError("Copilot proof home must be empty before runtime startup")
    return path


def _private_runtime_environment(private_home: Path) -> dict[str, str]:
    """Give the child runtime a small environment without controller secrets."""
    environment = {
        "HOME": str(private_home),
        "COPILOT_HOME": str(private_home),
        "XDG_CONFIG_HOME": str(private_home),
        "TMPDIR": str(private_home),
        "TEMP": str(private_home),
        "TMP": str(private_home),
        "PATH": os.defpath,
        "COPILOT_PLUGIN_DIR_ONLY": "true",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return environment


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise CopilotSDKError("Copilot proof exceeded its 180-second deadline")
    return seconds


def create_proof_client(
    *, base_directory: Path, workspace_root: Path, github_token: str,
) -> _PreparedProofClient:
    """Create a managed, pinned SDK client with no ambient tools or plugins.

    The caller obtains ``github_token`` from an already-authorized source and
    passes it in memory. This function never writes it to the environment or
    the proof ledger.
    """
    if not isinstance(github_token, str) or not github_token:
        raise CopilotSDKError("an existing Copilot-compatible credential is required")
    configured = [name for name in _RUNTIME_OVERRIDES if os.environ.get(name)]
    if configured:
        raise CopilotSDKError("Copilot proof cannot use a runtime override")
    client_type, connection_type, _reject = _sdk_components()
    runtime_pin = _prepare_runtime_pin()
    _verify_runtime_pin(runtime_pin)
    private_home = _private_empty_directory(Path(base_directory), Path(workspace_root))
    try:
        connection = connection_type.for_stdio(path=runtime_pin.executable_path)
        if not hasattr(connection, "env"):
            raise CopilotSDKError("pinned Copilot stdio connection cannot isolate its environment")
        connection.env = _private_runtime_environment(private_home)
        client = client_type(
            connection=connection,
            mode="empty",
            base_directory=str(private_home),
            working_directory=str(private_home),
            github_token=github_token,
            use_logged_in_user=False,
            enable_remote_sessions=False,
            builtin_plugin_directories=[],
        )
        return _PreparedProofClient(client=client, runtime_pin=runtime_pin)
    except CopilotSDKError:
        raise
    except Exception as exc:
        raise CopilotSDKError("pinned Copilot proof client could not be configured") from exc


class CopilotUsageCollector:
    """Capture pinned-runtime usage and anchor each signed row as it arrives."""

    def __init__(
        self,
        identity: CopilotLaunchIdentity,
        *,
        evidence_key: bytes,
        runtime_pin: CopilotRuntimePin,
        protected_ledger: ProtectedCopilotLedger | None = None,
    ) -> None:
        if not isinstance(evidence_key, bytes) or len(evidence_key) < 32:
            raise CopilotSDKError("collector evidence key is unavailable")
        if protected_ledger is not None and protected_ledger.identity != identity:
            raise CopilotSDKError("Copilot protected ledger belongs to another launch")
        self.identity = identity
        self._protected_ledger = protected_ledger
        self._runtime_pin = runtime_pin
        self._key_seed = evidence_key
        self._launch_key = evidence_key
        self._key = b""
        self._rows: list[dict[str, Any]] = []
        self._calls: list[dict[str, Any]] = []
        self._reason_codes: list[str] = []
        self._denied_permissions = 0
        self._runtime_api_release_version = ""
        self._runtime_protocol_version: int | None = None
        self._runtime_pin_recorded = False
        self._runtime_status_recorded = False
        self._session_scope = ""
        self._event_refs: set[str] = set()
        self._call_refs: set[str] = set()
        self._attached = False
        self._unsubscribe: Any = None
        self._request_started = False
        self._request_completed = False
        self._request_finished = False
        self._tool_inventory_checks = 0
        self._pending_denials: list[str] = []
        self._finalized = False

    def _append(self, value: Mapping[str, Any]) -> None:
        body = dict(value)
        previous = self._rows[-1]["signature"] if self._rows else "0" * 64
        body["sequence"] = len(self._rows)
        body["previous_signature"] = previous
        signature = hmac.new(
            self._key or self._launch_key,
            _LEDGER_DOMAIN + previous.encode("ascii") + b"\0" + _canonical(body),
            hashlib.sha256,
        ).hexdigest()
        body["signature"] = signature
        if self._protected_ledger is not None:
            try:
                self._protected_ledger.append(body)
            except CopilotSDKError:
                self._fail("protected_anchor_commit_failed")
                raise
        self._rows.append(body)

    def record_runtime_pin(self) -> None:
        """Seal the selected runtime files before spawning or creating a session."""
        if self._runtime_pin_recorded or self._attached or self._finalized:
            self._fail("runtime_pin_recorded_late_or_twice")
            raise CopilotSDKError("Copilot runtime pin must be sealed before startup")
        _verify_runtime_pin(self._runtime_pin)
        self._append({"kind": "runtime_pin", **self._runtime_pin.ledger_fields()})
        self._runtime_pin_recorded = True

    def record_runtime_status(self, api_release_version: Any, protocol_version: Any) -> None:
        """Seal and validate the status API identity before any session exists."""
        if not self._runtime_pin_recorded or self._runtime_status_recorded or self._attached:
            self._fail("runtime_status_recorded_late_or_twice")
            raise CopilotSDKError("Copilot runtime status must be checked before session creation")
        version = api_release_version if isinstance(api_release_version, str) else ""
        protocol = protocol_version if isinstance(protocol_version, int) and not isinstance(protocol_version, bool) else None
        matches = (
            version == SUPPORTED_RUNTIME_API_RELEASE_VERSION
            and protocol == SUPPORTED_RUNTIME_PROTOCOL_VERSION
        )
        self._append({
            "kind": "runtime_status",
            "runtime_api_release_version": version,
            "runtime_protocol_version": protocol,
            "runtime_status_matches_pin": matches,
        })
        self._runtime_status_recorded = True
        self._runtime_api_release_version = version
        self._runtime_protocol_version = protocol
        if not matches:
            self._fail("runtime_status_mismatch")
            raise CopilotSDKError("connected Copilot runtime status is unsupported")

    def attach(self, session: Any) -> None:
        """Attach once to a fresh session, before any request is sent."""
        if not self._runtime_pin_recorded or not self._runtime_status_recorded:
            self._fail("runtime_identity_not_sealed_before_session")
            raise CopilotSDKError("Copilot runtime identity must be sealed before session creation")
        if self._attached or self._request_started or self._finalized:
            self._fail("late_or_duplicate_attachment")
            raise CopilotSDKError("Copilot usage listener must attach once before the first request")
        session_id = getattr(session, "session_id", None)
        on = getattr(session, "on", None)
        if not isinstance(session_id, str) or not session_id or not callable(on):
            self._fail("session_identity_missing")
            raise CopilotSDKError("Copilot SDK session identity or event stream is unavailable")
        self._session_scope = _scope("session", session_id)
        self._key = hmac.new(
            self._key_seed,
            _LEDGER_DOMAIN + b"session\0" + self._session_scope.encode("ascii"),
            hashlib.sha256,
        ).digest()
        self._key_seed = b""
        try:
            self._unsubscribe = on(self.on_event)
        except Exception as exc:
            self._fail("listener_attach_failed")
            raise CopilotSDKError("Copilot usage listener could not attach") from exc
        self._attached = True
        self._append({
            "kind": "launch",
            "identity": self.identity.to_dict(),
            "session_scope": self._session_scope,
            **self._runtime_pin.ledger_fields(),
            "runtime_api_release_version": self._runtime_api_release_version,
            "runtime_protocol_version": self._runtime_protocol_version,
            "observer": "copilot-sdk-session.on",
        })
        for request_type in self._pending_denials:
            self._append({"kind": "permission_denied", "request_type": request_type})
        self._pending_denials.clear()

    def record_tool_inventory(self, tool_names: Any) -> None:
        """Record the runtime's current tool count without persisting tool names."""
        if not self._attached or self._finalized:
            self._fail("tool_inventory_outside_attached_run")
            return
        if not isinstance(tool_names, (list, tuple)):
            self._fail("tool_inventory_unavailable")
            self._append({"kind": "tool_inventory", "count": None})
            return
        count = len(tool_names)
        self._tool_inventory_checks += 1
        self._append({"kind": "tool_inventory", "count": count})
        if count:
            self._fail("ambient_or_dynamic_tools_present")

    def deny_permission(self, request: Any, _invocation: Any = None) -> Any:
        """Deny every permission request; only the request type is retained."""
        try:
            from copilot.rpc import PermissionDecisionReject
        except ImportError as exc:
            self._fail("permission_denial_unavailable")
            raise CopilotSDKError("Copilot SDK permission denial type is unavailable") from exc
        self._denied_permissions += 1
        request_type = type(request).__name__[:96]
        if self._attached:
            self._append({"kind": "permission_denied", "request_type": request_type})
        else:
            self._pending_denials.append(request_type)
        return PermissionDecisionReject(feedback="Permission denied by the Agentflow proof route.")

    def begin_request(self) -> None:
        if not self._attached or self._request_started or self._finalized:
            self._fail("request_started_without_fresh_listener")
            raise CopilotSDKError("Copilot proof request requires a fresh attached listener")
        self._request_started = True
        self._append({"kind": "request_started"})

    def finish_request(self, *, completed: bool) -> None:
        if not self._request_started or self._request_finished or self._finalized:
            self._fail("unexpected_request_completion")
            raise CopilotSDKError("Copilot proof request lifecycle is invalid")
        self._request_completed = bool(completed)
        self._request_finished = True
        self._append({"kind": "request_completed", "completed": bool(completed)})

    def _fail(self, reason: str) -> None:
        if reason not in self._reason_codes:
            self._reason_codes.append(reason)

    def on_event(self, event: Any) -> None:
        """Consume an SDK callback directly. Worker-writable event files are unsupported."""
        if not self._attached or self._finalized:
            self._fail("event_outside_attached_run")
            return
        raw_type = _field(event, "type")
        event_type = getattr(raw_type, "value", raw_type)
        event_type = str(event_type or "")
        if event_type in {"session.resume", "session.resume_start"}:
            self._fail("session_resumed_usage_not_replayed")
            self._append({"kind": "disqualifying_event", "event_type": event_type})
            return
        agent_id = _field(event, "agent_id")
        if agent_id is not None and agent_id != "":
            self._fail("tool_or_subagent_activity_observed")
            agent_scope = _scope("agent", agent_id) if isinstance(agent_id, str) else "invalid"
            self._append({"kind": "disqualifying_subagent_event", "agent_scope": agent_scope})
            return
        if (
            event_type.startswith("tool.")
            or event_type.startswith("external_tool.")
            or event_type.startswith("subagent.")
            or event_type == "skill.invoked"
        ):
            self._fail("tool_or_subagent_activity_observed")
            self._append({"kind": "disqualifying_event", "event_type": event_type})
            return
        if event_type != "assistant.usage":
            return
        if not self._request_started or self._request_finished:
            self._fail("usage_outside_request")
            self._append({"kind": "usage_outside_request"})
            return
        data = _field(event, "data")
        model = _field(data, "model")
        event_id = _field(event, "id")
        if not isinstance(model, str) or not model.strip() or event_id is None:
            self._fail("usage_model_or_event_id_missing")
            self._append({"kind": "invalid_usage_event"})
            return
        event_ref = _scope("event", str(event_id))
        api_call_id = _field(data, "api_call_id")
        call_ref = _scope("call", str(api_call_id)) if isinstance(api_call_id, str) and api_call_id else event_ref
        if event_ref in self._event_refs or call_ref in self._call_refs:
            self._fail("duplicate_usage_event")
            self._append({"kind": "duplicate_usage_event", "event_ref": event_ref})
            return
        self._event_refs.add(event_ref)
        self._call_refs.add(call_ref)
        model_matches = hmac.compare_digest(model.strip(), self.identity.requested_model)
        if not model_matches:
            self._fail("actual_model_mismatch")
        row = {
            "kind": "assistant.usage",
            "session_scope": self._session_scope,
            "call_sequence": len(self._calls) + 1,
            "call_ref": call_ref,
            "event_ref": event_ref,
            "agent_scope": _scope("agent", agent_id) if isinstance(agent_id, str) and agent_id else "",
            "actual_model": model.strip(),
            "requested_model": self.identity.requested_model,
            "model_matches": model_matches,
            "input_tokens": _numeric(_field(data, "input_tokens")),
            "output_tokens": _numeric(_field(data, "output_tokens")),
            "reasoning_tokens": _numeric(_field(data, "reasoning_tokens")),
            "cost": _numeric(_field(data, "cost")),
        }
        self._calls.append(row)
        self._append(row)

    def report(self) -> CopilotUsageReport:
        if self._finalized:
            raise CopilotSDKError("Copilot usage report was already finalized")
        if not self._attached:
            self._fail("listener_not_attached")
        if not self._request_started:
            self._fail("request_not_started")
        if not self._request_completed:
            self._fail("request_incomplete")
        if not self._calls:
            self._fail("usage_event_missing")
        if self._tool_inventory_checks < 2:
            self._fail("tool_inventory_incomplete")
        if self._pending_denials:
            self._fail("permission_denial_not_bound_to_session")
        if self._protected_ledger is None:
            self._fail("protected_anchor_unavailable")
        elif not self._protected_ledger.matches(self._rows):
            self._fail("protected_anchor_mismatch")
        if not self._chain_is_valid():
            self._fail("signed_chain_invalid")
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                self._fail("listener_detach_failed")
        self._append({
            "kind": "end",
            "request_started": self._request_started,
            "request_completed": self._request_completed,
            "usage_count": len(self._calls),
            "reason_codes": sorted(self._reason_codes),
        })
        self._finalized = True
        verified = not self._reason_codes
        return CopilotUsageReport(
            verified=verified,
            evidence_source="pinned_copilot_runtime_observation",
            launch_id=self.identity.launch_id,
            session_scope=self._session_scope,
            requested_model=self.identity.requested_model,
            calls=tuple(dict(row) for row in self._calls),
            denied_permissions=self._denied_permissions,
            reason_codes=tuple(sorted(self._reason_codes)),
            ledger=tuple(dict(row) for row in self._rows),
            persistent_admission=False,
        )

    def _chain_is_valid(self) -> bool:
        previous = "0" * 64
        for sequence, saved in enumerate(self._rows):
            row = dict(saved)
            supplied = row.pop("signature", "")
            if row.get("sequence") != sequence or row.get("previous_signature") != previous:
                return False
            expected = hmac.new(
                self._launch_key if row.get("kind") in {"runtime_pin", "runtime_status"} else self._key,
                _LEDGER_DOMAIN + previous.encode("ascii") + b"\0" + _canonical(row),
                hashlib.sha256,
            ).hexdigest()
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
                return False
            previous = supplied
        return (
            self._protected_ledger is not None
            and self._protected_ledger.matches(self._rows)
        )


class CopilotProofRun:
    """One bounded, no-tools turn used to validate a live SDK usage stream."""

    def __init__(self, client: Any, session: Any, collector: CopilotUsageCollector, deadline: float) -> None:
        self._client = client
        self._session = session
        self.collector = collector
        self._deadline = deadline
        self._used = False
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        identity: CopilotLaunchIdentity,
        evidence_key: bytes,
        protected_ledger: ProtectedCopilotLedger | None = None,
        base_directory: Path,
        workspace_root: Path,
        github_token: str,
    ) -> "CopilotProofRun":
        """Start a fresh managed session and attach before any model request."""
        if not isinstance(protected_ledger, ProtectedCopilotLedger):
            raise CopilotSDKError("Copilot proof run requires a protected controller ledger")
        if protected_ledger.identity != identity:
            raise CopilotSDKError("Copilot protected ledger belongs to another launch")
        deadline = time.monotonic() + MAX_PROOF_SECONDS
        prepared = create_proof_client(
            base_directory=base_directory,
            workspace_root=workspace_root,
            github_token=github_token,
        )
        client = prepared.client
        collector = CopilotUsageCollector(
            identity, evidence_key=evidence_key, runtime_pin=prepared.runtime_pin,
            protected_ledger=protected_ledger,
        )
        session = None
        try:
            collector.record_runtime_pin()
            _verify_runtime_pin(prepared.runtime_pin)
            await asyncio.wait_for(client.start(), timeout=_remaining(deadline))
            api_release_version, protocol_version = await cls._verify_runtime_status(client, deadline)
            collector.record_runtime_status(api_release_version, protocol_version)
            session = await asyncio.wait_for(
                client.create_session(
                    model=identity.requested_model,
                    reasoning_effort=identity.effort,
                    working_directory=str(Path(base_directory).expanduser().resolve(strict=False)),
                    streaming=True,
                    tools=[],
                    available_tools=[],
                    mcp_servers={},
                    custom_agents=[],
                    enable_config_discovery=False,
                    skip_custom_instructions=True,
                    enable_on_demand_instruction_discovery=False,
                    enable_file_hooks=False,
                    enable_host_git_operations=False,
                    enable_session_store=False,
                    enable_skills=False,
                    included_builtin_skills=[],
                    on_permission_request=collector.deny_permission,
                ),
                timeout=_remaining(deadline),
            )
            collector.attach(session)
            await cls._verify_empty_tools(session, collector, deadline)
            return cls(client, session, collector, deadline)
        except CopilotSDKError:
            await cls._stop(client, session)
            raise
        except Exception as exc:
            await cls._stop(client, session)
            raise CopilotSDKError("pinned Copilot proof session could not be opened") from exc

    @staticmethod
    async def _verify_runtime_status(client: Any, deadline: float) -> tuple[str, int]:
        """Check API release/protocol; build identity comes from the verified artifact pin."""
        get_status = getattr(client, "get_status", None)
        if not callable(get_status):
            raise CopilotSDKError("Copilot runtime status is unavailable")
        try:
            status = await asyncio.wait_for(get_status(), timeout=_remaining(deadline))
        except CopilotSDKError:
            raise
        except Exception as exc:
            raise CopilotSDKError("Copilot runtime status could not be verified") from exc
        api_release = _field(status, "version")
        protocol = _field(status, "protocol_version")
        if (
            api_release != SUPPORTED_RUNTIME_API_RELEASE_VERSION
            or protocol != SUPPORTED_RUNTIME_PROTOCOL_VERSION
            or isinstance(protocol, bool)
        ):
            raise CopilotSDKError("connected Copilot runtime API release or protocol is unsupported")
        return api_release, protocol

    @staticmethod
    async def _verify_empty_tools(session: Any, collector: CopilotUsageCollector, deadline: float) -> None:
        try:
            metadata = await asyncio.wait_for(
                session.rpc.tools.get_current_metadata(),
                timeout=_remaining(deadline),
            )
        except Exception as exc:
            collector._fail("tool_inventory_unavailable")
            raise CopilotSDKError("Copilot runtime tool inventory could not be verified") from exc
        tools = _field(metadata, "tools")
        collector.record_tool_inventory(tools)
        if not isinstance(tools, (list, tuple)) or tools:
            raise CopilotSDKError("Copilot runtime exposed tools in the no-tools proof route")

    async def send_once(self, prompt: str) -> CopilotUsageReport:
        """Send one ephemeral prompt, retaining only signed usage metadata."""
        if self._closed or self._used or not isinstance(prompt, str) or not prompt:
            raise CopilotSDKError("Copilot proof run accepts one request")
        self._used = True
        self.collector.begin_request()
        completed = False
        try:
            await asyncio.wait_for(
                self._session.send_and_wait(prompt),
                timeout=_remaining(self._deadline),
            )
            await self._verify_empty_tools(self._session, self.collector, self._deadline)
            completed = True
        except CopilotSDKError:
            raise
        except Exception as exc:
            self.collector._fail("request_failed_or_timed_out")
            raise CopilotSDKError("Copilot proof request failed or timed out; do not retry") from exc
        finally:
            self.collector.finish_request(completed=completed)
        return self.collector.report()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stop(self._client, self._session)

    @staticmethod
    async def _stop(client: Any, session: Any = None) -> None:
        if session is not None:
            try:
                await session.disconnect()
            except Exception:
                pass
        try:
            await client.stop()
        except Exception:
            pass

    async def __aenter__(self) -> "CopilotProofRun":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


__all__ = [
    "COPILOT_NEGATIVE_CONTROL_PROMPT", "CopilotLaunchIdentity", "CopilotLedgerAnchorStore",
    "CopilotLedgerHead", "CopilotLedgerSnapshot", "CopilotProofRun", "CopilotSDKError",
    "ProtectedCopilotLedger", "SQLiteCopilotLedgerStore",
    "CopilotRuntimePin", "CopilotUsageCollector", "CopilotUsageReport", "MAX_PROOF_SECONDS",
    "SUPPORTED_CLI_RELEASE_VERSION", "SUPPORTED_RUNTIME_API_RELEASE_VERSION",
    "SUPPORTED_RUNTIME_ARTIFACT_RELEASE", "SUPPORTED_RUNTIME_PROTOCOL_VERSION",
    "SUPPORTED_SDK_VERSION", "create_proof_client",
    "derive_evidence_key",
]
