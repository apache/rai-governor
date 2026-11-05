"""The four operations: search, get, recall, write (services/memory.md section 5).

The blob is the entry; the index row is derived from it. A write puts the blob
first and the row second, so a failure between the two leaves an entry the index
can be rebuilt to find, never a row pointing at nothing.

Blob-store and database calls are blocking, so every one runs in a thread:
a blocking call on the event loop stalls every request in the process.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


class NotFound(LookupError):
    pass


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
    """tooling-memory's shape for a person: ``human:<system>:<user-id>``."""
    return f"human:gofannon:{uid}"


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
    ) -> List[Dict[str, Any]]:
        """Summaries only, never bodies."""
        rows = await asyncio.to_thread(
            self.index.search, domain, query, filters, due_by, False, limit
        )
        return [
            {
                "id": r["id"],
                "title": r.get("title"),
                "summary": r.get("summary"),
                "type": r.get("type"),
                "trust_tier": r.get("trust_tier"),
                "created": r.get("created"),
                "review_by": r.get("review_by"),
            }
            for r in rows
        ]

    async def get(self, entry_id: str) -> Entry:
        row = await asyncio.to_thread(self.index.get, entry_id)
        if not row:
            raise NotFound(entry_id)
        text = await asyncio.to_thread(self.blobs.get_text, row["domain"], entry_id)
        if text is None:
            raise NotFound(entry_id)
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
            return {"brief": "", "cited": [], "matched": 0}

        parts, cited, used = [], [], 0
        for hit in hits:
            entry = await self.get(hit["id"])
            chunk = entry.body[: max(0, RECALL_MAX_CHARS - used)]
            if not chunk:
                break
            used += len(chunk)
            cited.append(hit["id"])
            parts.append(
                f"<entry id=\"{hit['id']}\" trust=\"{hit['trust_tier']}\">\n{chunk}\n</entry>"
            )

        from services.llm_service import call_llm

        messages = [
            {
                "role": "system",
                "content": (
                    "Answer from the entries given and nothing else. Cite each claim "
                    "with the entry id in square brackets. Say which entries are at "
                    "raw trust. If the entries do not answer the question, say so."
                ),
            },
            {"role": "user", "content": "\n\n".join(parts) + f"\n\nQuestion: {question}"},
        ]
        brief, _ = await call_llm(
            provider=RECALL_PROVIDER,
            model=RECALL_MODEL,
            messages=messages,
            parameters={"max_tokens": 1024},
            user_service=user_service,
            user_id=user_id,
        )
        return {"brief": brief, "cited": cited, "matched": len(hits)}


def get_memory_service(db) -> MemoryService:
    from .blob_store import get_blob_store

    index = MemoryIndex(db)
    return MemoryService(get_blob_store(), index)
