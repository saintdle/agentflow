"""Deterministic local SQLite FTS5 knowledge search.

Only caller-curated metadata is accepted. This module never calls a model and
never stores provider prompts, transcripts, credentials, or raw tool output.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from agentflow.privacy import PrivacyError, require_safe_mapping, require_safe_text


class SearchError(ValueError):
    """Raised for unsafe or invalid local-search input."""


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

    def __post_init__(self) -> None:
        for field in ("document_id", "title", "summary", "source", "source_kind"):
            object.__setattr__(self, field, require_safe_text(getattr(self, field), field, limit=4_096 if field == "summary" else 240))
        if self.privacy != "metadata-only":
            raise SearchError("only metadata-only documents can be indexed")
        if not isinstance(self.authority, int) or self.authority < 0 or self.authority > 100:
            raise SearchError("authority must be an integer from 0 to 100")
        safe = require_safe_mapping(dict(self.provenance), "provenance")
        object.__setattr__(self, "provenance", safe)
        if self.freshness:
            try:
                dt.datetime.fromisoformat(self.freshness.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SearchError("freshness must be an ISO-8601 timestamp") from exc


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

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _query(value: str) -> str:
    value = require_safe_text(value, "query", limit=400)
    # FTS syntax is intentionally reduced to quoted tokens. This makes ranking
    # deterministic and prevents a caller from injecting MATCH operators.
    tokens = re.findall(r"[\w-]+", value, flags=re.UNICODE)
    if not tokens:
        raise SearchError("query has no searchable terms")
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class KnowledgeIndex:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(document_id UNINDEXED, title, summary)")
        except sqlite3.OperationalError as exc:
            raise SearchError("SQLite FTS5 is required for knowledge search") from exc
        self.connection.execute("""CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
            source TEXT NOT NULL, source_kind TEXT NOT NULL, freshness TEXT NOT NULL,
            authority INTEGER NOT NULL, provenance TEXT NOT NULL, privacy TEXT NOT NULL
        )""")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def add(self, document: KnowledgeDocument | Mapping[str, Any]) -> KnowledgeDocument:
        if not isinstance(document, KnowledgeDocument):
            document = KnowledgeDocument(**dict(document))
        existing = self.connection.execute("SELECT 1 FROM documents WHERE document_id = ?", (document.document_id,)).fetchone()
        if existing:
            raise SearchError(f"document already exists: {document.document_id}")
        self.connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            document.document_id, document.title, document.summary, document.source, document.source_kind,
            document.freshness, document.authority, json.dumps(document.provenance, sort_keys=True), document.privacy,
        ))
        self.connection.execute("INSERT INTO documents_fts(document_id, title, summary) VALUES (?, ?, ?)", (document.document_id, document.title, document.summary))
        self.connection.commit()
        return document

    index = add

    def search(self, query: str, *, limit: int = 20, min_authority: int = 0, max_age_days: float | None = None, source_kind: str = "") -> list[SearchResult]:
        if limit < 1 or limit > 100:
            raise SearchError("limit must be between 1 and 100")
        if min_authority < 0 or min_authority > 100:
            raise SearchError("min_authority must be between 0 and 100")
        params: list[Any] = [_query(query), min_authority]
        clauses = ["d.authority >= ?", "d.privacy = 'metadata-only'"]
        if source_kind:
            clauses.append("d.source_kind = ?")
            params.append(require_safe_text(source_kind, "source_kind", limit=80))
        if max_age_days is not None:
            if max_age_days < 0:
                raise SearchError("max_age_days cannot be negative")
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)).isoformat()
            clauses.append("(d.freshness = '' OR d.freshness >= ?)")
            params.append(cutoff)
        params.extend([limit])
        rows = self.connection.execute(
            "SELECT d.*, bm25(documents_fts) AS rank FROM documents_fts "
            "JOIN documents d ON d.document_id = documents_fts.document_id WHERE documents_fts MATCH ? AND "
            + " AND ".join(clauses) + " ORDER BY rank ASC, d.authority DESC, d.document_id ASC LIMIT ?",
            params,
        ).fetchall()
        return [SearchResult(row["document_id"], row["title"], row["summary"], row["source"], row["source_kind"], row["freshness"], row["authority"], json.loads(row["provenance"]), row["privacy"], float(row["rank"])) for row in rows]

    query = search

    def __enter__(self) -> "KnowledgeIndex":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
