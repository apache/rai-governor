"""The index: one row per entry, so a search costs a row read rather than a blob read.

Retrieval in the pilot is keyed and filtered (services/memory.md section 4):
equality on indexed fields, then the query's words matched against title and
summary, ordered by how many matched. Ranked full-text is deferred until the zero-result rate says it is
needed, and the index is derived, so it can be rebuilt into another store
(AD-19, work item B13) without moving a single entry.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from services.database_service import DatabaseService

from .blob_store import entry_key
from .entry import Entry, Status

INDEX_DB = "memory_index"

# Fields a caller may filter on with equality.
FILTER_FIELDS = ("type", "trust_tier", "author", "status", "classification", "origin")

_INDEXES = (
    ["domain", "created"],
    ["domain", "type"],
    ["domain", "trust_tier"],
    ["domain", "review_by"],
)


def row_for(entry: Entry) -> Dict[str, Any]:
    f = entry.front
    return {
        "_id": f.id,
        "id": f.id,
        "domain": f.domain,
        "type": f.type.value,
        "trust_tier": f.trust_tier.value,
        "author": f.author,
        "source": list(f.source),
        "created": f.created.isoformat(),
        "review_by": f.review_by.isoformat() if f.review_by else None,
        "supersedes": f.supersedes,
        "status": f.status.value if f.status else None,
        "classification": f.classification.value,
        "origin": f.origin,
        "title": entry.display_title,
        "summary": f.summary,
        "blob": entry_key(f.domain, f.id),
    }


class MemoryIndex:
    def __init__(self, db: DatabaseService) -> None:
        self.db = db

    def ensure(self) -> None:
        for fields in _INDEXES:
            self.db.ensure_index(INDEX_DB, fields, index_name="memory_" + "_".join(fields))

    def upsert(self, entry: Entry) -> Dict[str, Any]:
        row = row_for(entry)
        self.db.save(INDEX_DB, row["_id"], row)
        return row

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.db.get(INDEX_DB, entry_id)
        except Exception:
            return None

    def search(
        self,
        domain: str,
        query: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        due_by: Optional[date] = None,
        include_inactive: bool = False,
        limit: int = 20,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        selector: Dict[str, Any] = {"domain": domain}
        for key, value in (filters or {}).items():
            if key not in FILTER_FIELDS:
                raise ValueError(f"cannot filter on {key!r}")
            selector[key] = value
        rows = self.db.find(INDEX_DB, selector, limit=None)

        terms = query_terms(query)
        hits = []
        for row in rows:
            if not include_inactive and row.get("status") in _INACTIVE:
                continue
            if due_by is not None:
                if not row.get("review_by") or row["review_by"] > due_by.isoformat():
                    continue
            score = 0
            if terms:
                words = set(_WORD_RE.findall(
                    f"{row.get('title') or ''} {row.get('summary') or ''}".lower()
                ))
                score = sum(1 for t in terms if t in words)
                if score == 0:
                    continue
            hits.append((score, row.get("created") or "", row))
        # Most query words matched first; among equals newest first, or oldest
        # first for a review queue. Two stable sorts: date, then score.
        hits.sort(key=lambda h: h[1], reverse=not oldest_first)
        hits.sort(key=lambda h: h[0], reverse=True)
        return [row for _, _, row in hits[: max(1, min(limit, 200))]]

    def counts(self, domain: str, today: date) -> Dict[str, int]:
        """Live entries in a domain: how many, how many raw, due, and either (the review queue)."""
        live = raw = due = queue = 0
        for row in self.db.find(INDEX_DB, {"domain": domain}, limit=None):
            if row.get("status") in _INACTIVE:
                continue
            live += 1
            is_raw = row.get("trust_tier") == "raw"
            is_due = bool(row.get("review_by")) and row["review_by"] <= today.isoformat()
            raw += is_raw
            due += is_due
            queue += is_raw or is_due
        return {"entries": live, "raw": raw, "due": due, "review": queue}


# Not live: tooling-memory serves none of these (core Entry::is_active).
_INACTIVE = (Status.DEMOTED.value, Status.SUPERSEDED.value, Status.ERASED.value)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]")

# Words that carry no meaning for matching a question to an entry.
_STOPWORDS = frozenset(
    "a an and any are as at be by do does for from has have how i in is it its "
    "of on or our that the their there these this to was we were what when where "
    "which who why will with you".split()
)


def query_terms(query: Optional[str]) -> List[str]:
    """The words of a query worth matching on: lowercased, no stopwords, unique."""
    seen: List[str] = []
    for w in _WORD_RE.findall((query or "").lower()):
        w = w.rstrip(".")
        if w and w not in _STOPWORDS and w not in seen:
            seen.append(w)
    return seen
