"""The index: one row per entry, so a search costs a row read rather than a blob read.

Retrieval in the pilot is keyed and filtered (services/memory.md section 4):
equality on indexed fields, then the query's words matched against title and
summary, ordered by how many matched. Ranked full-text is deferred until the zero-result rate says it is
needed, and the index is derived, so it can be rebuilt into another store
(AD-19, work item B13) without moving a single entry.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from services.database_service import DatabaseService

from .blob_store import entry_key
from .entry import Entry, Status

INDEX_DB = "memory_index"

# Most hits one search returns: a whole run's entries, for batch review.
MAX_HITS = 1000

# Fields a caller may filter on with equality.
FILTER_FIELDS = ("type", "trust_tier", "author", "status", "classification", "origin", "run")

# Row fields that are Governor's own, not derived from the entry: a person's
# disposition of a finding, and what a scan found about it. The frontmatter
# is shared with tooling-memory and stays as it is, so these live on the row
# only, and a rewrite of the row from the entry (a review, a supersede) keeps
# them.
ROW_ONLY = ("disposition", "decision", "evidence")

# CouchDB uses an index only when the selector names every one of its
# fields, so a domain's plain {"domain": d} needs ["domain"] alone.
_INDEXES = (
    ["domain"],
    ["domain", "created"],
    ["domain", "type"],
    ["domain", "trust_tier"],
    ["domain", "review_by"],
    ["domain", "run"],
    ["run"],
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
        "run": f.run,
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

    def upsert(self, entry: Entry, extra: Optional[Dict[str, Any]] = None,
               clear: tuple = ()) -> Dict[str, Any]:
        """Write the entry's row, replacing the one there.

        The ROW_ONLY fields carry over from the row replaced, then ``extra``
        sets fields and ``clear`` removes them. That needs the row first, on
        every backend: the in-memory one overwrites without a conflict. It
        also gives the save its _rev, which CouchDB wants for a replacement
        (409 without), and a conflict from a concurrent write is retried once.
        """
        bad = set(extra or {}) - set(ROW_ONLY)
        if bad:
            raise ValueError(f"not a row-only field: {sorted(bad)}")
        for attempt in (1, 2):
            row = row_for(entry)
            current = self.get(row["_id"])
            if current:
                row.update({k: current[k] for k in ROW_ONLY if k in current})
            row.update(extra or {})
            for k in clear:
                row.pop(k, None)
            doc = dict(row)
            if current and current.get("_rev"):
                doc["_rev"] = current["_rev"]
            try:
                self.db.save(INDEX_DB, row["_id"], doc)
                return row
            except HTTPException as exc:
                if exc.status_code != 409 or attempt == 2:
                    raise
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
        review_queue: bool = False,
        origins: Optional[List[str]] = None,
        disposition: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """``origins``: any of these. ``disposition``: a kind, "any" for
        decided, "none" for not decided."""
        selector: Dict[str, Any] = {"domain": domain}
        # The review queue: raw entries, and others past their review date.
        # Chosen before the limit, or a domain's older reviewed entries fill
        # the window and newer raw ones never reach the Review tab.
        today = datetime.now(timezone.utc).date().isoformat()
        for key, value in (filters or {}).items():
            if key not in FILTER_FIELDS:
                raise ValueError(f"cannot filter on {key!r}")
            selector[key] = value
        rows = self.db.find(INDEX_DB, selector, limit=None)

        terms = query_terms(query)
        want_origins = set(origins or ())
        hits = []
        for row in rows:
            if not include_inactive and row.get("status") in _INACTIVE:
                continue
            if want_origins and row.get("origin") not in want_origins:
                continue
            if disposition:
                have = row.get("disposition")
                if disposition == "none" and have:
                    continue
                if disposition == "any" and not have:
                    continue
                if disposition not in ("none", "any") and have != disposition:
                    continue
            if due_by is not None:
                if not row.get("review_by") or row["review_by"] > due_by.isoformat():
                    continue
            if review_queue and not (
                (row.get("trust_tier") or "raw") == "raw"
                or (row.get("review_by") and row["review_by"] <= today)
            ):
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
        return [row for _, _, row in hits[: max(1, min(limit, MAX_HITS))]]

    def domains(self) -> List[str]:
        """Every domain that has an entry. list_all, because find() takes
        equality selectors only on some backends."""
        return sorted({row["domain"] for row in self.db.list_all(INDEX_DB) if row.get("domain")})

    @staticmethod
    def _tally(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"entries": 0, "live": 0, "by_type": {}, "by_tier": {},
                               "first": None, "last": None}
        for row in rows:
            out["entries"] += 1
            inactive = row.get("status") in _INACTIVE
            if not inactive:
                out["live"] += 1
            t = row.get("type") or "?"
            out["by_type"][t] = out["by_type"].get(t, 0) + 1
            tier = "demoted" if row.get("status") == "demoted" else (row.get("trust_tier") or "raw")
            out["by_tier"][tier] = out["by_tier"].get(tier, 0) + 1
            c = row.get("created")
            if c and (out["first"] is None or c < out["first"]):
                out["first"] = c
            if c and (out["last"] is None or c > out["last"]):
                out["last"] = c
        return out

    def runs(self, domain: str) -> List[Dict[str, Any]]:
        """Runs that wrote into a domain, newest first, each with its
        entries counted by type and tier."""
        by_run: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.db.find(INDEX_DB, {"domain": domain}, limit=None):
            if row.get("run"):
                by_run.setdefault(row["run"], []).append(row)
        out = [{"run": r, **self._tally(rows)} for r, rows in by_run.items()]
        out.sort(key=lambda d: d["last"] or "", reverse=True)
        return out

    def for_run(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        """One run's entries, tallied per domain."""
        by_domain: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.db.find(INDEX_DB, {"run": run_id}, limit=None):
            by_domain.setdefault(row.get("domain") or "?", []).append(row)
        return {d: self._tally(rows) for d, rows in by_domain.items()}

    def counts(self, domain: str, today: date) -> Dict[str, int]:
        """Live entries in a domain: how many, how many raw, due, and either (the review queue)."""
        return self._count(self.db.find(INDEX_DB, {"domain": domain}, limit=None), today)

    def counts_by_domain(self, today: date) -> Dict[str, Dict[str, int]]:
        """counts() for every domain that has an entry, from one read of the index."""
        by_domain: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.db.list_all(INDEX_DB):
            if row.get("domain"):
                by_domain.setdefault(row["domain"], []).append(row)
        return {d: self._count(rows, today) for d, rows in by_domain.items()}

    @staticmethod
    def _count(rows, today: date) -> Dict[str, int]:
        live = raw = due = queue = 0
        for row in rows:
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
