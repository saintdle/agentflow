"""Deterministic, metadata-only SQLite knowledge search and governance.

The original ``KnowledgeIndex`` API is intentionally kept small and compatible.
Additional columns are additive migrations, so older Agentflow indexes continue
to open without a data rewrite. Memory recall lives in :mod:`agentflow.memory`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agentflow.privacy import require_safe_mapping, require_safe_text


class SearchError(ValueError):
    """Raised for unsafe or invalid local-search input."""


SCOPES = frozenset({"user", "project", "root", "task"})
STATUSES = frozenset({"candidate", "approved", "superseded", "rejected"})


def _timestamp(value: str, field: str) -> str:
    if not value:
        return ""
    value = require_safe_text(value, field, limit=80)
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SearchError(f"{field} must be an ISO-8601 timestamp") from exc
    return value


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    title: str
    summary: str
    source: str
    source_kind: str = "curated"
    freshness: str = ""
    authority: int = 0
    provenance: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    privacy: str = "metadata-only"
    scope: str = "project"
    scope_id: str = ""
    status: str = "candidate"
    expires_at: str = ""
    last_verified_at: str = ""
    source_digest: str = ""
    # Common vocabulary aliases are accepted for callers migrating from
    # stores that called these fields ``expiry`` and ``verified_at``.
    expiry: str = ""
    verified_at: str = ""
    superseded_by: str = ""
    conflicts_with: tuple[str, ...] = ()
    use_count: int = 0
    injection_count: int = 0

    def __post_init__(self) -> None:
        for field in ("document_id", "title", "summary", "source", "source_kind"):
            object.__setattr__(self, field, require_safe_text(getattr(self, field), field, limit=4_096 if field == "summary" else 240))
        if self.privacy != "metadata-only":
            raise SearchError("only metadata-only documents can be indexed")
        if not isinstance(self.authority, int) or isinstance(self.authority, bool) or not 0 <= self.authority <= 100:
            raise SearchError("authority must be an integer from 0 to 100")
        scope = require_safe_text(self.scope, "scope", limit=20).lower()
        if scope not in SCOPES:
            raise SearchError("scope must be one of user, project, root, or task")
        object.__setattr__(self, "scope", scope)
        if self.scope_id:
            object.__setattr__(self, "scope_id", require_safe_text(self.scope_id, "scope_id", limit=240))
        status = require_safe_text(self.status, "status", limit=20).lower()
        if status not in STATUSES:
            raise SearchError("status must be candidate, approved, superseded, or rejected")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "freshness", _timestamp(self.freshness, "freshness"))
        expiry = self.expires_at or self.expiry
        object.__setattr__(self, "expires_at", _timestamp(expiry, "expires_at"))
        object.__setattr__(self, "expiry", self.expires_at)
        verified = self.last_verified_at or self.verified_at or self.freshness
        object.__setattr__(self, "last_verified_at", _timestamp(verified, "last_verified_at"))
        object.__setattr__(self, "verified_at", self.last_verified_at)
        if self.source_digest:
            digest = require_safe_text(self.source_digest, "source_digest", limit=128).lower()
            if not re.fullmatch(r"[0-9a-f]{32,128}", digest):
                raise SearchError("source_digest must be hexadecimal")
            object.__setattr__(self, "source_digest", digest)
        else:
            object.__setattr__(self, "source_digest", _source_digest(self.source))
        if self.superseded_by:
            object.__setattr__(self, "superseded_by", require_safe_text(self.superseded_by, "superseded_by", limit=240))
        relations: list[str] = []
        for relation in self.conflicts_with:
            relations.append(require_safe_text(relation, "conflicts_with", limit=240))
        object.__setattr__(self, "conflicts_with", tuple(sorted(set(relations))))
        for field in ("use_count", "injection_count"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SearchError(f"{field} must be a non-negative integer")
        object.__setattr__(self, "provenance", require_safe_mapping(dict(self.provenance), "provenance"))


@dataclasses.dataclass(frozen=True)
class SearchResult:
    document_id: str
    title: str
    summary: str
    source: str
    source_kind: str
    freshness: str
    authority: int
    provenance: Mapping[str, Any]
    privacy: str
    rank: float
    scope: str = "project"
    scope_id: str = ""
    status: str = "candidate"
    expires_at: str = ""
    last_verified_at: str = ""
    source_digest: str = ""
    superseded_by: str = ""
    conflicts_with: tuple[str, ...] = ()
    use_count: int = 0
    injection_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def expiry(self) -> str:
        return self.expires_at

    @property
    def verified_at(self) -> str:
        return self.last_verified_at


def _query(value: str) -> str:
    value = require_safe_text(value, "query", limit=400)
    tokens = re.findall(r"[\w-]+", value, flags=re.UNICODE)
    if not tokens:
        raise SearchError("query has no searchable terms")
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _row_document(row: sqlite3.Row) -> KnowledgeDocument:
    keys = set(row.keys())
    return KnowledgeDocument(
        document_id=row["document_id"], title=row["title"], summary=row["summary"], source=row["source"], source_kind=row["source_kind"],
        freshness=row["freshness"], authority=row["authority"], provenance=json.loads(row["provenance"]), privacy=row["privacy"],
        scope=row["scope"] if "scope" in keys else "project", scope_id=row["scope_id"] if "scope_id" in keys else "",
        status=row["status"] if "status" in keys else "candidate", expires_at=row["expires_at"] if "expires_at" in keys else "",
        last_verified_at=row["last_verified_at"] if "last_verified_at" in keys else "", source_digest=row["source_digest"] if "source_digest" in keys else "",
        superseded_by=row["superseded_by"] if "superseded_by" in keys else "",
        conflicts_with=tuple(json.loads(row["conflicts_with"])) if "conflicts_with" in keys else (),
        use_count=row["use_count"] if "use_count" in keys else 0, injection_count=row["injection_count"] if "injection_count" in keys else 0,
    )


class KnowledgeIndex:
    """A local FTS index with explicit, caller-controlled governance."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        try:
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(document_id UNINDEXED, title, summary)")
        except sqlite3.OperationalError as exc:
            raise SearchError("SQLite FTS5 is required for knowledge search") from exc
        self.connection.execute("""CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
            source TEXT NOT NULL, source_kind TEXT NOT NULL, freshness TEXT NOT NULL,
            authority INTEGER NOT NULL, provenance TEXT NOT NULL, privacy TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'project', scope_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate', expires_at TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '', source_digest TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '', conflicts_with TEXT NOT NULL DEFAULT '[]',
            use_count INTEGER NOT NULL DEFAULT 0, injection_count INTEGER NOT NULL DEFAULT 0
        )""")
        self._migrate_columns()
        self.connection.execute("""CREATE TABLE IF NOT EXISTS document_relations (
            document_id TEXT NOT NULL, relation TEXT NOT NULL, target_id TEXT NOT NULL,
            PRIMARY KEY (document_id, relation, target_id)
        )""")
        self.connection.commit()

    def _migrate_columns(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(documents)")}
        additions = {
            "scope": "TEXT NOT NULL DEFAULT 'project'", "scope_id": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'candidate'", "expires_at": "TEXT NOT NULL DEFAULT ''",
            "last_verified_at": "TEXT NOT NULL DEFAULT ''", "source_digest": "TEXT NOT NULL DEFAULT ''",
            "superseded_by": "TEXT NOT NULL DEFAULT ''", "conflicts_with": "TEXT NOT NULL DEFAULT '[]'",
            "use_count": "INTEGER NOT NULL DEFAULT 0", "injection_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {declaration}")
        self.connection.execute("UPDATE documents SET last_verified_at = freshness WHERE last_verified_at = '' AND freshness <> ''")
        for row in self.connection.execute("SELECT document_id, source FROM documents WHERE source_digest = ''").fetchall():
            self.connection.execute("UPDATE documents SET source_digest = ? WHERE document_id = ?", (_source_digest(row["source"]), row["document_id"]))

    def close(self) -> None:
        self.connection.close()

    def add(self, document: KnowledgeDocument | Mapping[str, Any]) -> KnowledgeDocument:
        if not isinstance(document, KnowledgeDocument):
            document = KnowledgeDocument(**dict(document))
        existing = self.connection.execute("SELECT 1 FROM documents WHERE document_id = ?", (document.document_id,)).fetchone()
        if existing:
            raise SearchError(f"document already exists: {document.document_id}")
        try:
            self.connection.execute("""INSERT INTO documents
            (document_id,title,summary,source,source_kind,freshness,authority,provenance,privacy,
             scope,scope_id,status,expires_at,last_verified_at,source_digest,superseded_by,conflicts_with,use_count,injection_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            document.document_id, document.title, document.summary, document.source, document.source_kind,
            document.freshness, document.authority, json.dumps(document.provenance, sort_keys=True), document.privacy,
            document.scope, document.scope_id, document.status, document.expires_at, document.last_verified_at,
            document.source_digest, document.superseded_by, json.dumps(list(document.conflicts_with)), document.use_count, document.injection_count,
            ))
            self.connection.execute("INSERT INTO documents_fts(document_id, title, summary) VALUES (?, ?, ?)", (document.document_id, document.title, document.summary))
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise SearchError(f"document already exists: {document.document_id}") from exc
        return document

    index = add

    def get(self, document_id: str) -> KnowledgeDocument | None:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        row = self.connection.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        return _row_document(row) if row else None

    fetch = get

    def search(self, query: str, *, limit: int = 20, min_authority: int = 0, max_age_days: float | None = None,
               source_kind: str = "", scope: str | Sequence[str] | None = None, scope_id: str = "",
               statuses: Iterable[str] | None = None) -> list[SearchResult]:
        if limit < 1 or limit > 100:
            raise SearchError("limit must be between 1 and 100")
        if min_authority < 0 or min_authority > 100:
            raise SearchError("min_authority must be between 0 and 100")
        params: list[Any] = [_query(query), min_authority]
        clauses = ["d.authority >= ?", "d.privacy = 'metadata-only'", "d.status != 'rejected'"]
        if source_kind:
            clauses.append("d.source_kind = ?")
            params.append(require_safe_text(source_kind, "source_kind", limit=80))
        if scope is not None:
            scopes = [scope] if isinstance(scope, str) else list(scope)
            scopes = [require_safe_text(item, "scope", limit=20).lower() for item in scopes]
            if not scopes or any(item not in SCOPES for item in scopes):
                raise SearchError("scope must be one of user, project, root, or task")
            clauses.append("d.scope IN (" + ",".join("?" for _ in scopes) + ")")
            params.extend(scopes)
        if scope_id:
            clauses.append("d.scope_id = ?")
            params.append(require_safe_text(scope_id, "scope_id", limit=240))
        if statuses is not None:
            values = [require_safe_text(item, "status", limit=20).lower() for item in statuses]
            if not values or any(item not in STATUSES for item in values):
                raise SearchError("invalid status")
            clauses.append("d.status IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        if max_age_days is not None:
            if max_age_days < 0:
                raise SearchError("max_age_days cannot be negative")
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)).isoformat()
            clauses.append("(d.last_verified_at = '' OR d.last_verified_at >= ?)")
            params.append(cutoff)
        params.append(limit)
        rows = self.connection.execute(
            "SELECT d.*, bm25(documents_fts) AS rank FROM documents_fts JOIN documents d ON d.document_id = documents_fts.document_id "
            "WHERE documents_fts MATCH ? AND " + " AND ".join(clauses) + " ORDER BY rank ASC, d.authority DESC, d.document_id ASC LIMIT ?", params,
        ).fetchall()
        return [SearchResult(
            row["document_id"], row["title"], row["summary"], row["source"], row["source_kind"], row["freshness"], row["authority"], json.loads(row["provenance"]), row["privacy"], float(row["rank"]),
            row["scope"], row["scope_id"], row["status"], row["expires_at"], row["last_verified_at"], row["source_digest"], row["superseded_by"], tuple(json.loads(row["conflicts_with"])), row["use_count"], row["injection_count"],
        ) for row in rows]

    query = search

    def _set_status(self, document_id: str, status: str) -> KnowledgeDocument:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        if status not in STATUSES:
            raise SearchError("invalid status")
        current = self.get(document_id)
        if not current:
            raise SearchError(f"document not found: {document_id}")
        allowed = {
            "approved": {"candidate", "approved"},
            "rejected": {"candidate", "approved", "rejected"},
            "candidate": {"candidate"},
            "superseded": {"superseded"},
        }
        if current.status not in allowed[status]:
            raise SearchError(f"cannot transition {current.status} to {status}")
        self.connection.execute("UPDATE documents SET status = ? WHERE document_id = ?", (status, document_id))
        self.connection.commit()
        return self.get(document_id)  # type: ignore[return-value]

    def candidate(self, document: KnowledgeDocument | Mapping[str, Any]) -> KnowledgeDocument:
        if isinstance(document, KnowledgeDocument):
            document = dataclasses.replace(document, status="candidate")
        else:
            values = dict(document)
            values["status"] = "candidate"
            document = KnowledgeDocument(**values)
        existing = self.get(document.document_id)
        if existing:
            if existing == document:
                return existing
            raise SearchError(f"document already exists: {document.document_id}")
        return self.add(document)

    add_candidate = candidate

    def approve(self, document_id: str) -> KnowledgeDocument:
        return self._set_status(document_id, "approved")

    mark_approved = approve

    def reject(self, document_id: str) -> KnowledgeDocument:
        return self._set_status(document_id, "rejected")

    mark_rejected = reject

    def supersede(self, document_id: str, replacement_id: str) -> KnowledgeDocument:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        replacement_id = require_safe_text(replacement_id, "replacement_id", limit=240)
        if document_id == replacement_id:
            raise SearchError("a document cannot supersede itself")
        current = self.get(document_id)
        if not current or not self.get(replacement_id):
            raise SearchError("both documents must exist before superseding")
        if current.status == "superseded" and current.superseded_by == replacement_id:
            return current
        if current.status not in {"candidate", "approved"}:
            raise SearchError(f"cannot supersede {current.status} document")
        self.connection.execute("UPDATE documents SET status = 'superseded', superseded_by = ? WHERE document_id = ?", (replacement_id, document_id))
        self.connection.execute("INSERT OR IGNORE INTO document_relations VALUES (?, 'supersedes', ?)", (replacement_id, document_id))
        self.connection.commit()
        return self.get(document_id)  # type: ignore[return-value]

    def conflict(self, document_id: str, other_id: str) -> None:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        other_id = require_safe_text(other_id, "other_id", limit=240)
        if document_id == other_id or not self.get(document_id) or not self.get(other_id):
            raise SearchError("both distinct documents must exist before recording a conflict")
        for left, right in ((document_id, other_id), (other_id, document_id)):
            current = self.get(left)
            relations = sorted(set(current.conflicts_with if current else ()) | {right})
            self.connection.execute("UPDATE documents SET conflicts_with = ? WHERE document_id = ?", (json.dumps(relations), left))
            self.connection.execute("INSERT OR IGNORE INTO document_relations VALUES (?, 'conflicts', ?)", (left, right))
        self.connection.commit()

    add_conflict = conflict

    def mark_used(self, document_id: str, *, injected: bool = False) -> KnowledgeDocument:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        if not self.get(document_id):
            raise SearchError(f"document not found: {document_id}")
        self.connection.execute("UPDATE documents SET use_count = use_count + 1, injection_count = injection_count + ? WHERE document_id = ?", (1 if injected else 0, document_id))
        self.connection.commit()
        return self.get(document_id)  # type: ignore[return-value]

    record_use = mark_used

    def forget(self, document_id: str) -> None:
        document_id = require_safe_text(document_id, "document_id", limit=240)
        self.connection.execute("DELETE FROM documents_fts WHERE document_id = ?", (document_id,))
        self.connection.execute("DELETE FROM document_relations WHERE document_id = ? OR target_id = ?", (document_id, document_id))
        self.connection.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        self.connection.commit()

    def __enter__(self) -> "KnowledgeIndex":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
