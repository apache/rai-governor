"""Every Mango query the run registry and memory index make has an index.

CouchDB uses a json index only when the selector names every field in it
(docs.couchdb.org, /{db}/_find, "Index selection"); otherwise it reads every
document. /runs read every run document, traces included, on each 5-second
poll, and memory had no indexes made at all."""

from datetime import date

import pytest

from config import settings
from services import run_registry as rr
from services.memory import index as mi
from services.memory import service as ms

pytestmark = pytest.mark.unit


class Recorder:
    """A database that records indexes made and queries asked, and has no rows."""

    def __init__(self):
        self.indexes = {}       # db -> [fields]
        self.queries = []       # (db, selector, fields)

    def ensure_index(self, db_name, fields, index_name=None):
        self.indexes.setdefault(db_name, []).append(list(fields))

    def find(self, db_name, selector, fields=None, limit=10000):
        self.queries.append((db_name, dict(selector), fields))
        return []

    def list_all(self, db_name):
        return []


def _unindexed(rec, db_name):
    """Queries on db_name no index can serve."""
    usable = [set(f) for f in rec.indexes.get(db_name, [])]
    return [sel for db, sel, _ in rec.queries
            if db == db_name and not any(f <= set(sel) for f in usable)]


def test_every_run_registry_query_has_an_index(monkeypatch):
    monkeypatch.setattr(rr, "_indexes_ensured", False)
    rec = Recorder()
    rr._ensure_indexes(rec)
    reg = rr.RunRegistry(db=rec)
    reg.list_for_user("alice", projects=["steve"])
    reg.list_for_user("alice", agent_id="a1", projects=["steve"])
    reg.list_for_user("alice", workload_id="w1", projects=["steve"])
    reg.poll_owned_stops()
    reg.reconcile_orphans()
    reg.evict_old_completed()
    assert _unindexed(rec, rr.RUN_REGISTRY_DB) == []


def test_a_run_list_reads_the_summary_not_the_trace():
    rec = Recorder()
    rr.RunRegistry(db=rec).list_for_user("alice", projects=["steve"])
    fields = {f for db, _, fs in rec.queries if db == rr.RUN_REGISTRY_DB for f in fs}
    assert "events" not in fields and "result" not in fields and "ops_log" not in fields
    # A row built from the fields read is the row built from the whole doc.
    doc = {"run_id": "r", "user_id": "alice", "agent_name": "x", "agent_id": "a1",
           "project": "steve", "workload_id": "w1", "input_dict": {"q": 1},
           "started_at": "2026-11-11T00:00:00", "completed_at": "2026-11-11T00:05:00",
           "status": "error", "error": "boom", "worker_id": "w", "stop_requested": False,
           "events": [{"e": 1}], "result": {"big": "x" * 100}, "ops_log": [1]}
    projected = {k: v for k, v in doc.items() if k in fields}
    assert (rr.RunRecord.from_couch_doc(projected).to_summary()
            == rr.RunRecord.from_couch_doc(doc).to_summary())


def test_memory_makes_its_indexes_and_every_query_has_one(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("services.memory.blob_store.get_blob_store", lambda: None)
    service = ms.get_memory_service(rec)
    index = service.index
    index.search("dom-scan")
    index.search("dom-scan", filters={"type": "fact"})
    index.search("dom-scan", filters={"run": "r1"})
    index.counts("dom-scan", date(2026, 11, 11))
    index.runs("dom-scan")
    index.for_run("r1")
    assert rec.indexes.get(mi.INDEX_DB), "get_memory_service made no indexes"
    assert _unindexed(rec, mi.INDEX_DB) == []


def test_every_session_query_has_an_index(monkeypatch):
    from services import session_service as ss
    monkeypatch.setattr(ss, "_indexes_ensured", False)
    rec = Recorder()
    rec.delete_many = lambda db_name, ids: []
    monkeypatch.setattr(settings, "AUTH_CONFIG", {"session_idle_hours": 8})
    svc = ss.SessionService(rec)
    svc._delete_all_for_user_sync("asf:alice")
    svc.sweep_sync()
    assert any("lastUsedAt" in sel for db, sel, _ in rec.queries)
    assert rec.queries, "no session query was made"
    assert _unindexed(rec, ss._SESSIONS_COLLECTION) == []
