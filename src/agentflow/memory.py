"""Governed, bounded recall over :class:`agentflow.search.KnowledgeIndex`.

Recall is deliberately find-first: it performs deterministic FTS lookup, then
renders only explicitly approved, unexpired metadata. No approval is inferred
from authority, provenance, or a caller's requested scope.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Iterable, Mapping, Sequence

from agentflow.privacy import require_safe_text
from agentflow.search import KnowledgeIndex, SearchError, SearchResult


@dataclasses.dataclass(frozen=True)
class RecallItem:
    """A short approved memory and the source needed to verify it."""

    document_id: str
    title: str
    summary: str
    source: str
    source_digest: str
    scope: str
    scope_id: str
    authority: int
    provenance: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RecallPlan:
    query: str
    items: tuple[RecallItem, ...]
    text: str
    character_budget: int
    item_budget: int

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(item.source for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "items": [item.to_dict() for item in self.items],
            "text": self.text,
            "sources": list(self.sources),
            "character_budget": self.character_budget,
            "character_count": self.character_count,
            "item_budget": self.item_budget,
            "item_count": self.item_count,
        }


def _parse_time(value: str) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _fresh(result: SearchResult, now: dt.datetime, max_age_days: float | None) -> bool:
    expiry = _parse_time(result.expires_at)
    if expiry and expiry <= now:
        return False
    if max_age_days is not None:
        verified = _parse_time(result.last_verified_at)
        if not verified or verified < now - dt.timedelta(days=max_age_days):
            return False
    return True


def _render(item: RecallItem) -> str:
    # Source and digest are intentionally always present: recall is not a
    # substitute for reading the source when a fact matters.
    return f"- {item.title}: {item.summary} [{item.source}#{item.source_digest[:12]}]"


def find_first(
    index: KnowledgeIndex,
    query: str,
    *,
    scopes: str | Sequence[str] | None = None,
    scope: str | Sequence[str] | None = None,
    allowed_scopes: str | Sequence[str] | None = None,
    scope_id: str = "",
    min_authority: int = 0,
    max_items: int = 5,
    item_budget: int | None = None,
    max_chars: int = 2_000,
    character_budget: int | None = None,
    max_age_days: float | None = 30,
    max_summary_chars: int = 480,
    now: dt.datetime | None = None,
) -> RecallPlan:
    """Return a deterministic, bounded plan of approved fresh memories.

    ``scope`` and ``scopes`` are aliases.  ``item_budget`` and
    ``character_budget`` are aliases for the corresponding limits.  Providing
    both aliases with different values is rejected to avoid silent policy
    changes.
    """
    query = require_safe_text(query, "query", limit=400)
    if allowed_scopes is not None:
        if selected_scope := (scope if scope is not None else scopes):
            if selected_scope != allowed_scopes:
                raise SearchError("scope and allowed_scopes disagree")
        scopes = allowed_scopes
    if scope is not None and scopes is not None and scope != scopes:
        raise SearchError("scope and scopes disagree")
    selected_scope = scope if scope is not None else scopes
    if item_budget is not None:
        if max_items != 5 and max_items != item_budget:
            raise SearchError("max_items and item_budget disagree")
        max_items = item_budget
    if character_budget is not None:
        if max_chars != 2_000 and max_chars != character_budget:
            raise SearchError("max_chars and character_budget disagree")
        max_chars = character_budget
    if max_items < 1 or max_items > 100:
        raise SearchError("item budget must be between 1 and 100")
    if max_chars < 1 or max_chars > 100_000:
        raise SearchError("character budget must be between 1 and 100000")
    if max_age_days is not None and max_age_days < 0:
        raise SearchError("max_age_days cannot be negative")
    if max_summary_chars < 1 or max_summary_chars > 4_096:
        raise SearchError("max_summary_chars must be between 1 and 4096")
    instant = now or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    results = index.search(query, limit=min(100, max_items * 4), min_authority=min_authority, scope=selected_scope, scope_id=scope_id, statuses=("approved",))
    items: list[RecallItem] = []
    rendered: list[str] = []
    seen_digests: set[str] = set()
    used = 0
    for result in results:
        if not _fresh(result, instant, max_age_days):
            continue
        if result.source_digest in seen_digests:
            continue
        # Keep room for the deterministic separator and permit a shortened
        # summary when a source reference itself consumes much of the budget.
        item = RecallItem(result.document_id, result.title, result.summary[:max_summary_chars], result.source, result.source_digest, result.scope, result.scope_id, result.authority, result.provenance)
        line = _render(item)
        separator = 1 if rendered else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        if len(line) > remaining:
            prefix = f"- {item.title}: "
            suffix = f" [{item.source}#{item.source_digest[:12]}]"
            available = remaining - len(prefix) - len(suffix)
            if available <= 0:
                break
            item = dataclasses.replace(item, summary=item.summary[:available].rstrip())
            line = _render(item)
        if len(line) > remaining:
            break
        rendered.append(line)
        items.append(item)
        seen_digests.add(item.source_digest)
        used += separator + len(line)
        if len(items) >= max_items:
            break
    # Injection counts represent inclusion in a recall plan, not merely a
    # search hit. Updates happen after selection, in deterministic order.
    for item in items:
        index.mark_used(item.document_id, injected=True)
    return RecallPlan(query, tuple(items), "\n".join(rendered), max_chars, max_items)


recall = find_first


class MemoryStore:
    """Convenience facade combining governance and bounded recall."""

    def __init__(self, index: KnowledgeIndex | None = None, path: str = ":memory:") -> None:
        self.index = index or KnowledgeIndex(path)

    def close(self) -> None:
        self.index.close()

    def candidate(self, document: Any) -> Any:
        return self.index.candidate(document)

    def approve(self, document_id: str) -> Any:
        return self.index.approve(document_id)

    def reject(self, document_id: str) -> Any:
        return self.index.reject(document_id)

    def supersede(self, document_id: str, replacement_id: str) -> Any:
        return self.index.supersede(document_id, replacement_id)

    def forget(self, document_id: str) -> None:
        self.index.forget(document_id)

    def fetch(self, document_id: str) -> Any:
        return self.index.fetch(document_id)

    def find_first(self, query: str, **kwargs: Any) -> RecallPlan:
        return find_first(self.index, query, **kwargs)

    def recall(self, query: str, **kwargs: Any) -> RecallPlan:
        return self.find_first(query, **kwargs)

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
