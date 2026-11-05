"""The index: one row per entry, so a search costs a row read rather than a blob read.

Retrieval in the pilot is keyed and filtered, not ranked (services/memory.md
section 4): equality on indexed fields, then a substring match on title and
summary. Ranked full-text is deferred until the zero-result rate says it is
needed, and the index is derived, so it can be rebuilt into another store
(AD-19, work item B13) without moving a single entry.
"""

from __future__ import annotations

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
    ) -> List[Dict[str, Any]]:
        selector: Dict[str, Any] = {"domain": domain}
        for key, value in (filters or {}).items():
            if key not in FILTER_FIELDS:
                raise ValueError(f"cannot filter on {key!r}")
            selector[key] = value
        rows = self.db.find(INDEX_DB, selector, limit=None)

        needle = (query or "").strip().lower()
        hits = []
        for row in rows:
            if not include_inactive and row.get("status") in (
                Status.SUPERSEDED.value,
                Status.ERASED.value,
            ):
                continue
            if due_by is not None:
                if not row.get("review_by") or row["review_by"] > due_by.isoformat():
                    continue
            if needle:
                text = f"{row.get('title') or ''} {row.get('summary') or ''}".lower()
                if needle not in text:
                    continue
            hits.append(row)
        hits.sort(key=lambda r: r.get("created") or "", reverse=True)
        return hits[: max(1, min(limit, 200))]
