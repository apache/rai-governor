"""The four operations: search, get, recall, write (services/memory.md section 5),
and the review a person gives an entry: keep, curate, verify or demote.

The blob is the entry; the index row is derived from it. A write puts the blob
first and the row second, so a failure between the two leaves an entry the index
can be rebuilt to find, never a row pointing at nothing.

Blob-store and database calls are blocking, so every one runs in a thread:
a blocking call on the event loop stalls every request in the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .blob_store import BlobStore
from .entry import (
    Confidence,
    Entry,
    EntryError,
    EntryType,
    Frontmatter,
    Status,
    TrustTier,
    new_entry_id,
)
from .index import MemoryIndex

# Recall is a cheap model through the gateway, never a provider key held here.
RECALL_PROVIDER = os.getenv("MEMORY_RECALL_PROVIDER", "llmao")
RECALL_MODEL = os.getenv("MEMORY_RECALL_MODEL", "qwen3.8-27b")
RECALL_MAX_ENTRIES = int(os.getenv("MEMORY_RECALL_MAX_ENTRIES", "5"))
RECALL_MAX_CHARS = int(os.getenv("MEMORY_RECALL_MAX_CHARS", "6000"))


RECALL_INSTRUCTIONS = (
    "You write briefs from a memory store for another agent to act on. You are given "
    "entries, each tagged with its id and trust tier, and a question or a topic.\n"
    "Write 2 to 6 sentences that answer the question, or, if it is only a topic, say "
    "what the entries record about it: what was found, where, and anything that "
    "supersedes or qualifies it. Use only what the entries say.\n"
    "After each claim, cite the entry it came from as [entry id]. If any cited entry "
    "is at raw trust, end with: 'Unreviewed: ' and those ids.\n"
    "If the entries do not address the question, reply exactly: "
    "'The entries do not answer this.'"
)


# One row per review, shaped like tooling-memory's PromotionSpec (crates/ledger)
# plus the action and the review date it set, so the record can move there.
REVIEWS_DB = "memory_reviews"

# A kept entry's next review, when the reviewer names no date. tooling-memory
# gives a fact written without one the same ninety days.
REVIEW_INTERVAL_DAYS = 90


class NotFound(LookupError):
    pass


class Refused(ValueError):
    """A review the rules do not allow: the entry is not live, or the tier is wrong."""


class OwnEntry(PermissionError):
    """The reviewer wrote the entry. The approver is never the proposer."""


class ReviewAction(str, Enum):
    KEEP = "keep"  # still holds: tier unchanged, review date moved on
    CURATE = "curate"  # raw to curated
    VERIFY = "verify"  # raw or curated to verified
    DEMOTE = "demote"  # no longer relied on: status demoted, out of search


_TIER_FOR = {ReviewAction.CURATE: TrustTier.CURATED, ReviewAction.VERIFY: TrustTier.VERIFIED}
_TIER_RANK = {TrustTier.RAW: 0, TrustTier.CURATED: 1, TrustTier.VERIFIED: 2}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summary(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title"),
        "summary": row.get("summary"),
        "type": row.get("type"),
        "trust_tier": row.get("trust_tier"),
        "author": row.get("author"),
        "status": row.get("status"),
        "created": row.get("created"),
        "review_by": row.get("review_by"),
    }


@dataclass
class WriteRequest:
    domain: str
    type: EntryType
    body: str
    summary: str
    title: Optional[str] = None
    source: List[str] = field(default_factory=list)
    confidence: Optional[Confidence] = None
    review_by: Optional[date] = None
    supersedes: Optional[str] = None
    origin: Optional[str] = None


def human_author(uid: str) -> str:
    """tooling-memory's shape for a person: ``human:<system>:<user-id>``.

    Session uids are already ``<provider>:<id>`` (``asf:akm``), so the provider
    is the system. A bare uid, from the legacy Firebase path, gets ``gofannon``.
    """
    return f"human:{uid}" if ":" in uid else f"human:gofannon:{uid}"


def agent_author(agent_name: str, model: Optional[str]) -> str:
    """tooling-memory's shape for an agent: ``agent:<name> (model: <id>)``."""
    return f"agent:{agent_name} (model: {model or 'unknown'})"


class MemoryService:
    def __init__(self, blobs: BlobStore, index: MemoryIndex) -> None:
        self.blobs = blobs
        self.index = index

    async def search(
        self,
        domain: str,
        query: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        due_by: Optional[date] = None,
        limit: int = 20,
        include_inactive: bool = False,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """Summaries only, never bodies."""
        rows = await asyncio.to_thread(
            self.index.search,
            domain, query, filters, due_by, include_inactive, limit, oldest_first,
        )
        return [_summary(r) for r in rows]

    async def counts(self, domain: str, today: Optional[date] = None) -> Dict[str, int]:
        today = today or datetime.now(timezone.utc).date()
        return await asyncio.to_thread(self.index.counts, domain, today)

    async def get(self, entry_id: str) -> Entry:
        row = await asyncio.to_thread(self.index.get, entry_id)
        if not row:
            raise NotFound(entry_id)
        text = await asyncio.to_thread(self.blobs.get_text, row["domain"], entry_id)
        if text is None:
            raise NotFound(entry_id)
        return Entry.parse(text)

    async def review(
        self,
        entry_id: str,
        action: ReviewAction,
        reviewer: str,
        why: str = "",
        review_by: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Record a person's judgement of an entry, then write the version it makes.

        The record goes first, as tooling-memory's promote does: a review that
        cannot be recorded is not made, and a version that then fails to write
        leaves a record a retry finds. Any writer may review, never the author.
        """
        entry = await self.get(entry_id)
        front = entry.front
        if front.status is not None:
            raise Refused(f"{entry_id} is {front.status.value} and cannot be reviewed")
        if front.author == reviewer:
            raise OwnEntry("You wrote this entry; someone else has to review it")
        why = (why or "").strip()
        if action is not ReviewAction.KEEP and not why:
            raise Refused("say why: what was checked, and against what")
        target = _TIER_FOR.get(action)
        if target is not None and _TIER_RANK[front.trust_tier] >= _TIER_RANK[target]:
            raise Refused(f"{entry_id} is {front.trust_tier.value} already")

        before = entry.render()
        if target is not None:
            front.trust_tier = target
        if action is ReviewAction.DEMOTE:
            front.status = Status.DEMOTED
        if review_by is not None:
            front.review_by = review_by
        elif action is ReviewAction.KEEP:
            front.review_by = datetime.now(timezone.utc).date() + timedelta(
                days=REVIEW_INTERVAL_DAYS
            )
        after = entry.render()

        now = datetime.now(timezone.utc)
        record = {
            "domain": front.domain,
            "entry": entry_id,
            "action": action.value,
            "tier": front.trust_tier.value,
            "by": reviewer,
            "author": front.author,
            "why": why,
            "classification": front.classification.value,
            "digest": _digest(before),
            "new_digest": _digest(after),
            "review_by": front.review_by.isoformat() if front.review_by else None,
            "at": now.isoformat().replace("+00:00", "Z"),
        }
        doc_id = f"{entry_id}:{now.strftime('%Y%m%dT%H%M%S.%fZ')}"
        await asyncio.to_thread(self.index.db.save, REVIEWS_DB, doc_id, record)
        await asyncio.to_thread(self.blobs.put, entry)
        await asyncio.to_thread(self.index.upsert, entry)
        return {"frontmatter": front.to_dict(), "review": record}

    async def history(self, entry_id: str) -> Dict[str, Any]:
        """The versions an entry replaced and the reviews it had, oldest first."""
        row = await asyncio.to_thread(self.index.get, entry_id)
        if not row:
            raise NotFound(entry_id)
        keys = await asyncio.to_thread(self.blobs.history, row["domain"], entry_id)
        versions = []
        for key in keys:
            name = key.rsplit("/", 1)[-1][: -len(".md")]
            stamp, _, digest = name.rpartition("-")
            try:
                at = datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
                replaced = at.isoformat().replace("+00:00", "Z")
            except ValueError:
                replaced = None
            versions.append({"version": name, "replaced": replaced, "digest": digest})
        reviews = await asyncio.to_thread(
            self.index.db.find, REVIEWS_DB, {"entry": entry_id}, None, None
        )
        reviews = sorted(
            ({k: v for k, v in r.items() if not k.startswith("_")} for r in reviews),
            key=lambda r: r.get("at") or "",
        )
        return {"id": entry_id, "domain": row["domain"], "versions": versions, "reviews": reviews}

    async def get_version(self, entry_id: str, version: str) -> Entry:
        row = await asyncio.to_thread(self.index.get, entry_id)
        if not row:
            raise NotFound(entry_id)
        try:
            text = await asyncio.to_thread(
                self.blobs.get_version_text, row["domain"], entry_id, version
            )
        except EntryError:
            raise NotFound(version)
        if text is None:
            raise NotFound(version)
        return Entry.parse(text)

    async def write(self, req: WriteRequest, author: str) -> Entry:
        """Append an entry at raw trust. Callers cannot choose its tier."""
        if not req.summary or "\n" in req.summary:
            raise EntryError("summary is required and is one line")
        if req.supersedes:
            old = await self.get(req.supersedes)
            if old.front.domain != req.domain:
                raise EntryError("an entry can only supersede one in its own domain")
        front = Frontmatter(
            id=new_entry_id(req.domain),
            type=req.type,
            domain=req.domain,
            author=author,
            created=datetime.now(timezone.utc),
            source=list(req.source),
            confidence=req.confidence,
            trust_tier=TrustTier.RAW,
            review_by=req.review_by,
            supersedes=req.supersedes,
            origin=req.origin,
            summary=req.summary,
            title=req.title,
        )
        entry = Entry(front=front, body=req.body)
        front.validate()
        await asyncio.to_thread(self.blobs.put, entry)
        await asyncio.to_thread(self.index.upsert, entry)
        if req.supersedes:
            old.front.status = Status.SUPERSEDED
            await asyncio.to_thread(self.blobs.put, old)
            await asyncio.to_thread(self.index.upsert, old)
        return entry

    async def recall(
        self,
        question: str,
        domain: str,
        user_service=None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """A cited brief from the entries that match, so the caller spends one turn."""
        hits = await self.search(domain, question, limit=RECALL_MAX_ENTRIES)
        if not hits:
            return {"brief": "", "cited": [], "entries": [], "matched": 0}

        parts, cited, entries, used = [], [], [], 0
        for hit in hits:
            entry = await self.get(hit["id"])
            chunk = entry.body[: max(0, RECALL_MAX_CHARS - used)]
            if not chunk:
                break
            used += len(chunk)
            cited.append(hit["id"])
            entries.append(hit)
            parts.append(
                f"<entry id=\"{hit['id']}\" trust=\"{hit['trust_tier']}\">\n{chunk}\n</entry>"
            )

        from services.llm_service import call_llm

        messages = [
            {"role": "system", "content": RECALL_INSTRUCTIONS},
            {
                "role": "user",
                "content": "\n\n".join(parts)
                + f"\n\nQuestion or topic: {question}\n\nWrite the brief.",
            },
        ]
        brief, _ = await call_llm(
            provider=RECALL_PROVIDER,
            model=RECALL_MODEL,
            messages=messages,
            parameters={"max_tokens": 1024},
            user_service=user_service,
            user_id=user_id,
        )
        # entries: the summaries of what was given to the model, in order, so a
        # page can show them beside the brief without a search per id.
        return {"brief": brief, "cited": cited, "entries": entries, "matched": len(hits)}


def get_memory_service(db) -> MemoryService:
    from .blob_store import get_blob_store

    index = MemoryIndex(db)
    return MemoryService(get_blob_store(), index)
