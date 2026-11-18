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
import contextvars
import hashlib
import secrets
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
from .index import INDEX_DB as INDEX_DB_NAME, MemoryIndex

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
        "supersedes": row.get("supersedes"),
        "run": row.get("run"),
        "origin": row.get("origin"),
        "source": row.get("source") or [],
        # Governor's own row fields (index.ROW_ONLY): a person's disposition
        # of a finding, and what the scan found about it.
        "disposition": row.get("disposition"),
        "why": (row.get("decision") or {}).get("why"),
        "decided_by": (row.get("decision") or {}).get("by"),
        "decided_at": (row.get("decision") or {}).get("at"),
        "decision": row.get("decision"),
        "evidence": row.get("evidence"),
        "seen_in_runs": row.get("seen_in_runs") or [],
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


# The registry run whose agent code is executing, bound by
# dependencies._execute_agent_code. write() stamps it on the entry: not a
# field of WriteRequest or of the agent's memory object, so agent code has no
# argument or attribute that sets it. (Agent code still runs in-process, with
# full builtins, until ISSUE-023: this removes the obvious routes, not all.)
_CURRENT_RUN: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("memory_run", default=None)


def bind_run(run_id: Optional[str]):
    return _CURRENT_RUN.set(run_id)


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
        review_queue: bool = False,
        origins: Optional[List[str]] = None,
        disposition: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Summaries only, never bodies."""
        rows = await asyncio.to_thread(
            self.index.search,
            domain, query, filters, due_by, include_inactive, limit, oldest_first, review_queue,
            origins, disposition,
        )
        return [_summary(r) for r in rows]

    async def search_page(
        self,
        domains: List[str],
        query: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        due_by: Optional[date] = None,
        limit: int = 20,
        offset: int = 0,
        include_inactive: bool = False,
        oldest_first: bool = False,
        review_queue: bool = False,
        origins: Optional[List[str]] = None,
        disposition: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One page of summaries over one or more domains, each with its
        domain, and how many match in all."""
        rows, total = await asyncio.to_thread(
            self.index.search_page,
            domains, query, filters, due_by, include_inactive, limit, oldest_first, review_queue,
            origins, disposition, offset,
        )
        return {"hits": [{**_summary(r), "domain": r.get("domain")} for r in rows], "total": total}

    async def domains(self) -> List[str]:
        return await asyncio.to_thread(self.index.domains)

    async def runs(self, domain: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.index.runs, domain)

    async def run_overview(self, domains: List[str]) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.index.run_overview, domains)

    async def for_run(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        return await asyncio.to_thread(self.index.for_run, run_id)

    async def counts(self, domain: str, today: Optional[date] = None) -> Dict[str, int]:
        today = today or datetime.now(timezone.utc).date()
        return await asyncio.to_thread(self.index.counts, domain, today)

    async def counts_by_domain(self, today: Optional[date] = None) -> Dict[str, Dict[str, int]]:
        today = today or datetime.now(timezone.utc).date()
        return await asyncio.to_thread(self.index.counts_by_domain, today)

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
        batch: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        row: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a person's judgement of an entry, then write the version it makes.

        ``extra`` goes into the review record (a disposition), ``row`` onto
        the index row (index.ROW_ONLY fields).

        ``batch`` ties the record to a batch review, which can be undone as
        one. The record keeps the frontmatter the review replaced, for undo.

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
        before_front = front.to_dict()
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
            "before": before_front,
        }
        if batch:
            record["batch"] = batch
        record.update(extra or {})
        doc_id = f"{entry_id}:{now.strftime('%Y%m%dT%H%M%S.%fZ')}"
        await asyncio.to_thread(self.index.db.save, REVIEWS_DB, doc_id, record)
        await asyncio.to_thread(self.blobs.put, entry)
        await asyncio.to_thread(self.index.upsert, entry, row)
        return {"frontmatter": front.to_dict(), "review": record}

    async def curate_written(self, entry: Entry, by: str, why: str) -> Dict[str, Any]:
        """Curate an entry as its writer writes it. Only for a writer who
        reviews the entry's domain, writing in a review step (triage
        guidance): a second review would only repeat the first. The record
        says so, and has no batch: undoing the triage withdraws the entry."""
        before_front, before_text = entry.front.to_dict(), entry.render()
        entry.front.trust_tier = TrustTier.CURATED
        return await self._record(entry, ReviewAction.CURATE.value, by, why,
                                  before_front, before_text, None)

    async def _record(self, entry: Entry, action: str, by: str, why: str,
                      before_front: Dict[str, Any], before_text: str, batch: Optional[str],
                      extra: Optional[Dict[str, Any]] = None,
                      row: Optional[Dict[str, Any]] = None, clear: tuple = ()) -> Dict[str, Any]:
        """Write a review record for a change made outside review(): a
        superseded entry brought back, or an undo."""
        front = entry.front
        now = datetime.now(timezone.utc)
        record = {
            "domain": front.domain, "entry": front.id, "action": action,
            "tier": front.trust_tier.value, "by": by, "author": front.author, "why": why,
            "classification": front.classification.value,
            "digest": _digest(before_text), "new_digest": _digest(entry.render()),
            "review_by": front.review_by.isoformat() if front.review_by else None,
            "at": now.isoformat().replace("+00:00", "Z"),
            "before": before_front, "batch": batch, **(extra or {}),
        }
        await asyncio.to_thread(
            self.index.db.save, REVIEWS_DB, f"{front.id}:{now.strftime('%Y%m%dT%H%M%S.%fZ')}", record)
        await asyncio.to_thread(self.blobs.put, entry)
        await asyncio.to_thread(self.index.upsert, entry, row, clear)
        return record

    async def batch_review(
        self,
        ids: List[str],
        action: ReviewAction,
        reviewer: str,
        why: str = "",
        review_by: Optional[date] = None,
        restore_superseded: bool = False,
        may_review=None,
        may_read=None,
    ) -> Dict[str, Any]:
        """One decision over many entries, each recorded on its own with the
        batch id. An entry that can't take it (the reviewer wrote it, it's
        not live, the tier is wrong, the caller may not review its domain)
        is skipped and listed, not the whole batch refused.

        ``restore_superseded`` with a demote brings back, live, what each
        demoted entry had superseded, if nothing has replaced it since.
        """
        why = (why or "").strip()
        if action is not ReviewAction.KEEP and not why:
            raise Refused("say why: what was checked, and against what")
        batch = "b-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + secrets.token_hex(4)
        applied, skipped, restored = [], [], []
        for entry_id in dict.fromkeys(ids):
            try:
                entry = await self.get(entry_id)
                if may_read is not None and not may_read(entry.front.domain):
                    raise NotFound(entry_id)
                if may_review is not None and not may_review(entry.front.domain):
                    raise Refused(f"You can read {entry.front.domain} but not review it")
                await self.review(entry_id, action, reviewer, why, review_by, batch=batch)
                applied.append(entry_id)
            except NotFound:
                skipped.append({"id": entry_id, "reason": "No such entry"})
                continue
            except OwnEntry:
                skipped.append({"id": entry_id, "reason": "You wrote it; someone else reviews it"})
                continue
            except (Refused, EntryError) as exc:
                skipped.append({"id": entry_id, "reason": str(exc)})
                continue
            old_id = entry.front.supersedes
            if restore_superseded and action is ReviewAction.DEMOTE and old_id:
                try:
                    old = await self.get(old_id)
                except NotFound:
                    continue
                # Not one the reviewer wrote (the approver is never the
                # proposer), and not if a live entry other than this one
                # supersedes it: that would leave two versions live.
                others = await asyncio.to_thread(
                    self.index.db.find, INDEX_DB_NAME, {"supersedes": old_id}, None, None)
                replaced_elsewhere = any(
                    r.get("id") != entry_id and r.get("status") not in ("demoted", "superseded", "erased")
                    for r in others)
                if (old.front.status is Status.SUPERSEDED and old.front.domain == entry.front.domain
                        and old.front.author != reviewer and not replaced_elsewhere):
                    before_text, before_front = old.render(), old.front.to_dict()
                    old.front.status = None
                    await self._record(old, "restore", reviewer,
                                       f"{entry_id} demoted in batch {batch}: {why}",
                                       before_front, before_text, batch)
                    restored.append(old_id)
        return {"batch": batch, "action": action.value, "applied": applied,
                "skipped": skipped, "restored": restored}

    async def undo_batch(self, batch: str, reviewer: str, may_review=None) -> Dict[str, Any]:
        """Put back what a batch changed, where nothing has changed it since.
        Each entry put back gets an undo record of its own."""
        records = await asyncio.to_thread(self.index.db.find, REVIEWS_DB, {"batch": batch}, None, None)
        # An undo isn't undone, and nor is guidance withdrawn because no
        # finding took its disposition.
        records = [r for r in records if r.get("action") not in ("undo", "withdraw")]
        # Records in domains the caller can't review don't exist for them:
        # not undone, not listed, and a batch with none left is missing.
        if may_review is not None:
            records = [r for r in records if may_review(r.get("domain") or "")]
        if not records:
            raise NotFound(batch)
        undo = "u-" + batch
        undone, skipped, withdrawn = [], [], []
        # Newest first: a restore after its demote is undone before it.
        for rec in sorted(records, key=lambda r: r.get("at") or "", reverse=True):
            entry_id = rec.get("entry")
            try:
                entry = await self.get(entry_id)
            except NotFound:
                skipped.append({"id": entry_id, "reason": "No such entry"})
                continue
            if entry.front.author == reviewer:
                # Undoing a demote is a review too: not of your own entry.
                skipped.append({"id": entry_id, "reason": "You wrote it; someone else reviews it"})
                continue
            if _digest(entry.render()) != rec.get("new_digest") or not rec.get("before"):
                skipped.append({"id": entry_id, "reason": "Changed since the batch; left as it is"})
                continue
            before_text, before_front = entry.render(), entry.front.to_dict()
            entry.front = Frontmatter.from_dict(rec["before"])
            # A disposition comes off the row with the review it came with.
            clear = ("disposition", "decision") if rec.get("disposition") else ()
            await self._record(entry, "undo", reviewer, f"undo of batch {batch}",
                               before_front, before_text, undo, {"undoes": batch}, clear=clear)
            undone.append(entry_id)
            gid = (rec.get("disposition") or {}).get("guidance_id")
            if gid and gid not in withdrawn:
                if await self._withdraw(gid, reviewer, f"undo of batch {batch}", undo):
                    withdrawn.append(gid)
        return {"batch": batch, "undo": undo, "undone": undone, "skipped": skipped,
                "withdrawn": withdrawn}

    async def _withdraw(self, entry_id: str, by: str, why: str, batch: str) -> bool:
        """Take out of use guidance written with a disposition that was undone.
        Its author is the person undoing, so this is not a review."""
        try:
            entry = await self.get(entry_id)
        except NotFound:
            return False
        if entry.front.status is not None:
            return False
        before_text, before_front = entry.render(), entry.front.to_dict()
        entry.front.status = Status.DEMOTED
        await self._record(entry, "withdraw", by, why, before_front, before_text, batch)
        return True

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
            run=_CURRENT_RUN.get(),
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
    # Without this the index has none of its Mango indexes, and every
    # search and count reads every row. Cheap after the first call: the
    # database service remembers which indexes it has made.
    index.ensure()
    return MemoryService(get_blob_store(), index)
