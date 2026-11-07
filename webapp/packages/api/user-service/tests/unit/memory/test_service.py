import hashlib
import io
import re
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from services.database_service.memory import MemoryDBService
from services.memory.blob_store import InMemoryBlobStore, S3BlobStore, entry_key
from services.memory.entry import Entry, EntryError, EntryType, Status, TrustTier
from services.memory.index import MemoryIndex
from services.memory.service import MemoryService, NotFound, WriteRequest, human_author


@pytest.fixture
def svc():
    return MemoryService(InMemoryBlobStore(), MemoryIndex(MemoryDBService()))


def _req(**over):
    base = dict(
        domain="dom-scan",
        type=EntryType.FACT,
        body="# SQLi in a.py\n\nString-built query at line 10.\n",
        summary="String-built SQL in a.py",
        source=["https://github.com/apache/example/blob/main/a.py#L10"],
    )
    base.update(over)
    return WriteRequest(**base)


@pytest.mark.asyncio
async def test_write_lands_at_raw_with_provenance(svc):
    e = await svc.write(_req(), author=human_author("alice"))
    assert e.front.trust_tier is TrustTier.RAW
    assert e.front.author == "human:gofannon:alice"
    assert e.front.id.startswith("mem-scan-")
    text = svc.blobs.get_text("dom-scan", e.front.id)
    assert Entry.parse(text).front == e.front


@pytest.mark.asyncio
async def test_search_returns_summaries_never_bodies(svc):
    await svc.write(_req(), author="human:gofannon:alice")
    await svc.write(_req(summary="Hard-coded key in b.py", body="# Key\n"), author="x")
    hits = await svc.search("dom-scan", "sql")
    assert [h["summary"] for h in hits] == ["String-built SQL in a.py"]
    assert "body" not in hits[0]
    assert await svc.search("dom-other", "sql") == []


@pytest.mark.asyncio
async def test_a_whole_question_finds_entries_by_its_words(svc):
    sql = await svc.write(_req(), author="x")
    key = await svc.write(_req(summary="Hard-coded key in a.py", body="# Key\n"), author="x")
    hits = await svc.search("dom-scan", "What SQL problems are in a.py?")
    # Both mention a.py; the SQL entry matches two words, the key entry one.
    assert [h["id"] for h in hits] == [sql.front.id, key.front.id]
    assert await svc.search("dom-scan", "what is in there?") == await svc.search("dom-scan")


def test_human_author_uses_the_session_provider():
    assert human_author("asf:akm") == "human:asf:akm"
    assert human_author("dev_stub:site_admin_1") == "human:dev_stub:site_admin_1"
    assert human_author("firebase-uid") == "human:gofannon:firebase-uid"


@pytest.mark.asyncio
async def test_filters_and_due_by(svc):
    await svc.write(_req(review_by=date(2026, 12, 1)), author="x")
    await svc.write(_req(type=EntryType.CONVENTION, summary="Use parameterised queries"), author="x")
    assert len(await svc.search("dom-scan", filters={"type": "convention"})) == 1
    assert len(await svc.search("dom-scan", due_by=date(2026, 12, 31))) == 1
    with pytest.raises(ValueError):
        await svc.search("dom-scan", filters={"body": "x"})


@pytest.mark.asyncio
async def test_get_round_trips_and_misses(svc):
    e = await svc.write(_req(), author="x")
    got = await svc.get(e.front.id)
    assert got.body == e.body
    with pytest.raises(NotFound):
        await svc.get("mem-scan-nothere1")


@pytest.mark.asyncio
async def test_supersede_marks_old_and_archives_its_previous_version(svc):
    old = await svc.write(_req(), author="x")
    new = await svc.write(_req(summary="Fixed in 1.2", supersedes=old.front.id), author="x")
    assert (await svc.get(old.front.id)).front.status is Status.SUPERSEDED
    assert [h["id"] for h in await svc.search("dom-scan")] == [new.front.id]
    (version,) = svc.blobs.history("dom-scan", old.front.id)
    # tooling-memory's name: <stamp>-<first 8 hex of sha256 of the old text>.md
    archived_text = svc.blobs.objects[version]
    expected = hashlib.sha256(archived_text.encode()).hexdigest()[:8]
    assert re.fullmatch(rf".*/\d{{8}}T\d{{6}}\.\d{{6}}Z-{expected}\.md", version)


@pytest.mark.asyncio
async def test_cannot_supersede_across_domains(svc):
    old = await svc.write(_req(), author="x")
    with pytest.raises(EntryError):
        await svc.write(_req(domain="dom-other", supersedes=old.front.id), author="x")


@pytest.mark.asyncio
async def test_recall_goes_through_call_llm_and_cites(svc):
    e = await svc.write(_req(), author="x")
    with patch("services.llm_service.call_llm", new_callable=AsyncMock) as llm:
        llm.return_value = (f"String-built SQL [{e.front.id}].", None)
        out = await svc.recall("sql", "dom-scan", user_id="alice")
    assert out["cited"] == [e.front.id]
    assert [x["summary"] for x in out["entries"]] == [e.front.summary]
    assert out["matched"] == 1
    assert llm.await_args.kwargs["user_id"] == "alice"
    sent = llm.await_args.kwargs["messages"]
    assert e.front.id in sent[1]["content"]
    assert "Question or topic: sql" in sent[1]["content"]
    assert "cite the entry" in sent[0]["content"]


@pytest.mark.asyncio
async def test_recall_with_no_match_makes_no_model_call(svc):
    with patch("services.llm_service.call_llm", new_callable=AsyncMock) as llm:
        out = await svc.recall("anything", "dom-scan")
    assert out == {"brief": "", "cited": [], "entries": [], "matched": 0}
    llm.assert_not_awaited()


class _FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body

    def get_paginator(self, name):
        store = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for (b, k) in store.objects if b == Bucket and k.startswith(Prefix)]}

        return P()


@pytest.mark.asyncio
async def test_s3_layout_matches_tooling_memory():
    s3 = _FakeS3()
    svc = MemoryService(S3BlobStore(s3, "bucket", "memory"), MemoryIndex(MemoryDBService()))
    e = await svc.write(_req(), author="x")
    assert ("bucket", f"memory/record/dom-scan/{e.front.id}.md") in s3.objects
    assert entry_key("dom-scan", e.front.id) == f"record/dom-scan/{e.front.id}.md"


class _RevDB(MemoryDBService):
    """Refuses a save over an existing document without its _rev, as CouchDB does."""

    def save(self, db_name, doc_id, doc):
        from fastapi import HTTPException

        current = self.dbs.get(db_name, {}).get(doc_id)
        if current is not None and doc.get("_rev") != current["_rev"]:
            raise HTTPException(status_code=409, detail="Document update conflict")
        rev = int((current or {}).get("_rev", "0-x").split("-")[0]) + 1
        doc = {**doc, "_rev": f"{rev}-x"}
        return super().save(db_name, doc_id, doc)


@pytest.mark.asyncio
async def test_superseding_and_reviewing_replace_index_rows_on_couchdb():
    from services.memory.service import ReviewAction

    svc = MemoryService(InMemoryBlobStore(), MemoryIndex(_RevDB()))
    old = await svc.write(_req(), author="human:gofannon:alice")
    new = await svc.write(_req(supersedes=old.front.id), author="human:gofannon:alice")
    assert svc.index.get(old.front.id)["status"] == "superseded"
    await svc.review(new.front.id, ReviewAction.CURATE, "human:gofannon:bob", "read it")
    assert svc.index.get(new.front.id)["trust_tier"] == "curated"
