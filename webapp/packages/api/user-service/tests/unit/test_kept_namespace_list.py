"""The data-store namespace list is kept in one CouchDB document all workers
share: reading the three views took 6-7 s on 400,000 records, and a cache
per worker missed on most loads. Only the first load waits; later loads
answer from the document and, once it is old and the owner's records have
changed, rebuild it in the background."""

import threading
from types import SimpleNamespace

import couchdb

from services.database_service import couchdb as cdb
from services.database_service.couchdb import KEPT_TOTALS_DB, CouchDBService

OWNER = "asf:akm"
KEPT_ID = f"namespaces:{OWNER}"


class FakeStore:
    """agent_data_store: the gofannon_ns views over a list of records."""

    def __init__(self):
        self.records = [("notes", 7, 100), ("files:x", 1200, 200)]  # (namespace, size, updated)
        self.views = []
        self.gate = None  # an Event a view read waits on

    def get(self, doc_id):
        return {"_id": doc_id, "_rev": "1-a", "version": cdb.STATS_DDOC_VERSION}

    def view(self, name, **opts):
        if self.gate:
            self.gate.wait(5)
        self.views.append((name, opts))
        view = name.split("/")[1]
        level = opts["group_level"]
        groups = {}
        for ns, size, updated in self.records:
            key = [OWNER, ns][:level]
            g = groups.setdefault(tuple(key), {"count": 0, "sum": 0, "max": 0})
            g["count"] += 1
            g["sum"] += size if view == "size" else 0
            g["max"] = max(g["max"], updated if view == "updated" else size)
        if view == "agents":
            return []
        return [SimpleNamespace(key=list(k), value=v) for k, v in sorted(groups.items())]


class FakeKept:
    """data_store_totals, with CouchDB's revision check."""

    def __init__(self):
        self.docs = {}
        self.saves = 0

    def get(self, doc_id):
        doc = self.docs.get(doc_id)
        return dict(doc) if doc else None

    def save(self, doc):
        current = self.docs.get(doc["_id"])
        if (current or {}).get("_rev") != doc.get("_rev"):
            raise couchdb.http.ResourceConflict("conflict")
        n = int((current or {"_rev": "0-"})["_rev"].split("-")[0]) + 1
        self.docs[doc["_id"]] = dict(doc, _rev=f"{n}-x")
        self.saves += 1


def _service(store, kept):
    s = CouchDBService.__new__(CouchDBService)
    s._get_or_create_db = lambda name: kept if name == KEPT_TOTALS_DB else store
    return s


def _wait_for_rebuild(s):
    lock = getattr(s, "_kept_refresh_lock", None)
    if lock:
        assert lock.acquire(timeout=5)
        lock.release()


def test_first_load_builds_and_keeps_the_list():
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    out = s.kept_namespace_stats(OWNER)
    assert out["files:x"]["recordCount"] == 1 and out["notes"]["sizeBytes"] == 7
    doc = kept.docs[KEPT_ID]
    assert doc["namespaces"] == out and doc["fingerprint"] == [2, 1207, 200]
    # The first load doesn't wait on indexing either.
    assert all(o.get("update") == "lazy" for _, o in store.views)


def test_later_loads_answer_from_the_kept_list_without_the_views(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cdb.time, "time", lambda: clock[0])
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    s.kept_namespace_stats(OWNER)
    store.views.clear()
    clock[0] += cdb.KEPT_TOTALS_REFRESH_SECONDS - 1
    other_worker = _service(store, kept)
    assert "notes" in other_worker.kept_namespace_stats(OWNER)
    assert store.views == []


def test_an_old_list_is_returned_at_once_and_rebuilt_in_the_background(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cdb.time, "time", lambda: clock[0])
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    s.kept_namespace_stats(OWNER)
    store.records.append(("new", 5, 300))
    clock[0] += cdb.KEPT_TOTALS_REFRESH_SECONDS + 1

    store.gate = threading.Event()          # the views are slow
    out = s.kept_namespace_stats(OWNER)
    assert "new" not in out                 # answered without waiting
    assert s.kept_namespace_stats(OWNER) == out
    store.gate.set()
    _wait_for_rebuild(s)
    assert "new" in kept.docs[KEPT_ID]["namespaces"]
    assert kept.docs[KEPT_ID]["fingerprint"] == [3, 1212, 300]
    # Only one rebuild for the two requests, and its fingerprint waited
    # for the index.
    fp = [o for n, o in store.views if o["group_level"] == 1]
    assert len(fp) == 2 + 2 and all("update" not in o for o in fp[2:])
    assert "new" in s.kept_namespace_stats(OWNER)


def test_an_unchanged_store_is_not_read_again(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cdb.time, "time", lambda: clock[0])
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    s.kept_namespace_stats(OWNER)
    store.views.clear()
    clock[0] += cdb.KEPT_TOTALS_REFRESH_SECONDS + 1
    s.kept_namespace_stats(OWNER)
    _wait_for_rebuild(s)
    assert [o["group_level"] for _, o in store.views] == [1, 1]    # the fingerprint only
    assert kept.saves == 1


def test_a_rebuild_that_loses_a_conflict_gives_up(monkeypatch):
    """Another worker's rebuild, or a dropped namespace, wrote the document
    while this one read the views: that write stands, no retry."""
    clock = [1000.0]
    monkeypatch.setattr(cdb.time, "time", lambda: clock[0])
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    s.kept_namespace_stats(OWNER)
    store.records.append(("new", 5, 300))
    clock[0] += cdb.KEPT_TOTALS_REFRESH_SECONDS + 1
    store.gate = threading.Event()
    s.kept_namespace_stats(OWNER)
    s.drop_kept_namespace(OWNER, "notes")   # written mid-rebuild
    store.gate.set()
    _wait_for_rebuild(s)
    assert list(kept.docs[KEPT_ID]["namespaces"]) == ["files:x"]
    assert kept.saves == 2


def test_dropping_a_namespace_retries_a_conflict_once():
    store, kept = FakeStore(), FakeKept()
    s = _service(store, kept)
    s.kept_namespace_stats(OWNER)
    real_save, calls = kept.save, []

    def save(doc):
        calls.append(1)
        if len(calls) <= 2:
            raise couchdb.http.ResourceConflict("conflict")
        real_save(doc)

    kept.save = save
    s.drop_kept_namespace(OWNER, "notes")
    assert len(calls) == 2 and "notes" in kept.docs[KEPT_ID]["namespaces"]
    calls.clear()
    calls.append(1)                         # the next save goes through after one conflict
    s.drop_kept_namespace(OWNER, "notes")
    assert "notes" not in kept.docs[KEPT_ID]["namespaces"]
    s.drop_kept_namespace(OWNER, "never-there")


def test_falls_back_to_finding_records_when_the_views_fail(monkeypatch):
    monkeypatch.setattr(cdb.DatabaseService, "namespace_stats",
                        lambda self, u, n=None: {"x": {"recordCount": 1}})
    store, kept = FakeStore(), FakeKept()
    store.view = lambda name, **o: (_ for _ in ()).throw(RuntimeError("view broke"))
    assert _service(store, kept).kept_namespace_stats(OWNER) == {"x": {"recordCount": 1}}
    assert kept.docs == {}
