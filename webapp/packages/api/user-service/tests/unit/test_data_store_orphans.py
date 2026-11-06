"""ISSUE-018: cleared data store records must stay cleared.

Two ways records survived a clear:

1. CouchDB ``find(limit=None)`` read only the first page. python-couchdb's
   ``Database.find`` drops the bookmark, so ``clear_namespace`` deleted the
   first 10,000 records and left the rest.
2. The access-count flush wrote back docs that were deleted between its
   read and its write, on backends without revision checks.
"""

from typing import Any, Dict, List

import pytest

from services.access_tracking import get_accumulator
from services.data_store_service import DATA_STORE_DB, DataStoreService
from services.database_service.couchdb import CouchDBService
from services.database_service.memory import MemoryDBService


# --- 1. CouchDB paging ------------------------------------------------------


class _FakeResource:
    """Answers _find in pages, the way CouchDB does, with a bookmark each time."""

    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = docs
        self.queries: List[Dict[str, Any]] = []

    def post_json(self, path, body):
        assert path == "_find"
        self.queries.append(body)
        start = int(body.get("bookmark") or 0)
        page = self.docs[start : start + body["limit"]]
        return 200, {}, {"docs": page, "bookmark": str(start + len(page))}


class _FakeDB:
    def __init__(self, docs):
        self.resource = _FakeResource(docs)

    def find(self, query):  # what the old code used: docs only, no bookmark
        _, _, data = self.resource.post_json("_find", body=query)
        return iter(data["docs"])


def _couch_with(docs, page_size):
    svc = CouchDBService.__new__(CouchDBService)
    svc.FIND_PAGE_SIZE = page_size
    fake = _FakeDB(docs)
    svc._get_or_create_db = lambda name: fake
    return svc, fake


def test_find_without_limit_reads_every_page():
    docs = [{"_id": f"d{i}", "namespace": "ns"} for i in range(25)]
    svc, fake = _couch_with(docs, page_size=10)
    out = svc.find("agent_data_store", {"namespace": "ns"}, limit=None)
    assert [d["_id"] for d in out] == [d["_id"] for d in docs]
    assert [q.get("bookmark") for q in fake.resource.queries] == [None, "10", "20"]


def test_find_without_limit_stops_on_an_exact_multiple():
    docs = [{"_id": f"d{i}"} for i in range(20)]
    svc, fake = _couch_with(docs, page_size=10)
    assert len(svc.find("db", {}, limit=None)) == 20
    # 10, 10, then an empty page ends it.
    assert len(fake.resource.queries) == 3


def test_find_with_a_limit_is_one_request():
    docs = [{"_id": f"d{i}"} for i in range(25)]
    svc, fake = _couch_with(docs, page_size=10)
    svc._get_or_create_db = lambda name: fake
    assert len(svc.find("db", {}, limit=5)) == 5


# --- 2. The access flush must not bring deleted docs back -------------------


class _DeleteDuringFlushDB(MemoryDBService):
    """Runs a callback inside get_many: a delete that lands mid-flush."""

    def __init__(self):
        super().__init__()
        self.during_get_many = None

    def get_many(self, db_name, doc_ids):
        result = super().get_many(db_name, doc_ids)
        if self.during_get_many:
            hook, self.during_get_many = self.during_get_many, None
            hook()
        return result


@pytest.mark.asyncio
async def test_delete_during_flush_is_not_undone():
    db = _DeleteDuringFlushDB()
    store = DataStoreService(db)
    store.set("u1", "ns", "k1", "v1")
    store.get_all("u1", "ns", agent_name="agent_x")  # queues an access update
    doc_id = store._make_doc_id("u1", "ns", "k1")

    db.during_get_many = lambda: store.delete("u1", "ns", "k1")
    await store._access_accumulator.flush()

    with pytest.raises(Exception):
        db.get(DATA_STORE_DB, doc_id)


@pytest.mark.asyncio
async def test_clear_drops_updates_another_request_queued():
    db = MemoryDBService()
    reader = DataStoreService(db)  # one request reads ...
    clearer = DataStoreService(db)  # ... another clears the namespace
    reader.set("u1", "ns", "k1", "v1")
    reader.get_all("u1", "ns", agent_name="agent_x")

    assert clearer.clear_namespace("u1", "ns") == 1
    assert await reader._access_accumulator.flush() == 0
    assert reader.list_keys("u1", "ns") == []


def test_one_accumulator_per_database():
    db = MemoryDBService()
    assert DataStoreService(db)._access_accumulator is DataStoreService(db)._access_accumulator
    assert get_accumulator(MemoryDBService(), DATA_STORE_DB) is not get_accumulator(db, DATA_STORE_DB)


def test_deletes_outside_a_flush_are_not_remembered():
    # Ids are only kept for a flush under way; without one, a busy store
    # would otherwise collect every deleted id for the life of the process.
    db = MemoryDBService()
    store = DataStoreService(db)
    for i in range(50):
        store.set("u1", "ns", f"k{i}", "v")
        store.delete("u1", "ns", f"k{i}")
    store.clear_namespace("u1", "ns")
    assert store._access_accumulator._deleted_during_flush == set()


@pytest.mark.asyncio
async def test_the_mid_flush_set_is_emptied_when_the_flush_ends():
    db = _DeleteDuringFlushDB()
    store = DataStoreService(db)
    store.set("u1", "ns", "k1", "v1")
    store.set("u1", "ns", "k2", "v2")
    store.get_all("u1", "ns", agent_name="agent_x")

    db.during_get_many = lambda: store.delete("u1", "ns", "k1")
    assert await store._access_accumulator.flush() == 1  # only k2
    assert store._access_accumulator._deleted_during_flush == set()
