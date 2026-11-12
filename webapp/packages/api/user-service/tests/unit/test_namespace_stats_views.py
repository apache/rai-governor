"""CouchDB keeps data-store totals per namespace in views (ISSUE-034):
listing namespaces reads a row per namespace, not one per record, totalled
by CouchDB's built-in reduces, and never waits on indexing (update=lazy)."""

from types import SimpleNamespace

from services.database_service import couchdb as cdb
from services.database_service.couchdb import STATS_DDOC_ID, STATS_DDOC_VERSION, CouchDBService


class FakeDB:
    """The parts of a couchdb.Database the views code uses."""

    def __init__(self, ddoc=None, fail=False):
        self.docs = {STATS_DDOC_ID: ddoc} if ddoc else {}
        self.views = []
        self.fail = fail

    def get(self, doc_id):
        return self.docs.get(doc_id)

    def save(self, doc):
        self.docs[doc["_id"]] = dict(doc, _rev="2-x")

    def view(self, name, **opts):
        self.views.append((name, opts))
        if self.fail:
            raise RuntimeError("view broke")
        rows = {
            "gofannon_ns/size": [
                (["asf:akm", "files:apache/steve"], {"sum": 1200, "count": 3, "min": 0, "max": 900, "sumsqr": 1}),
                (["asf:akm", "notes"], {"sum": 7, "count": 1, "min": 7, "max": 7, "sumsqr": 49}),
            ],
            "gofannon_ns/updated": [
                (["asf:akm", "files:apache/steve"], {"sum": 0, "count": 3, "min": 0, "max": 1762855200.0, "sumsqr": 0}),
                (["asf:akm", "notes"], {"sum": 0, "count": 1, "min": 0, "max": 0, "sumsqr": 0}),
            ],
            "gofannon_ns/agents": [
                (["asf:akm", "files:apache/steve", "glasswing_discover"], 2),
                (["asf:akm", "files:apache/steve", "asvs_download_repo"], 3),
            ],
        }[name]
        lo, hi = opts["startkey"], opts["endkey"]
        return [SimpleNamespace(key=k, value=v) for k, v in rows if k[:len(lo)] == lo]


def _service(db):
    s = CouchDBService.__new__(CouchDBService)
    s._get_or_create_db = lambda name: db
    return s


def test_reads_one_row_per_namespace():
    db = FakeDB()
    out = _service(db).namespace_stats("asf:akm")
    assert out == {
        "files:apache/steve": {"recordCount": 3, "sizeBytes": 1200,
                               "agents": ["asvs_download_repo", "glasswing_discover"],
                               "updatedAt": "2025-11-11T10:00:00"},
        "notes": {"recordCount": 1, "sizeBytes": 7, "agents": [], "updatedAt": None},
    }
    assert sorted((n, o["group_level"], o["startkey"], o["endkey"]) for n, o in db.views) == [
        ("gofannon_ns/agents", 3, ["asf:akm"], ["asf:akm", {}]),
        ("gofannon_ns/size", 2, ["asf:akm"], ["asf:akm", {}]),
        ("gofannon_ns/updated", 2, ["asf:akm"], ["asf:akm", {}]),
    ]
    # A page must never wait on indexing.
    assert all(o.get("update") == "lazy" for _, o in db.views)


def test_one_namespace_reads_only_its_rows():
    db = FakeDB()
    out = _service(db).namespace_stats("asf:akm", "notes")
    assert list(out) == ["notes"]
    assert db.views[0][1]["startkey"] == ["asf:akm", "notes"]
    assert db.views[0][1]["endkey"] == ["asf:akm", "notes", {}]


def test_creates_the_design_doc_once_and_rewrites_an_old_one():
    db = FakeDB()
    s = _service(db)
    s.namespace_stats("asf:akm")
    assert db.docs[STATS_DDOC_ID]["version"] == STATS_DDOC_VERSION
    db.docs[STATS_DDOC_ID] = None
    s.namespace_stats("asf:akm")             # this process already ensured it
    assert db.docs[STATS_DDOC_ID] is None
    old = FakeDB(ddoc={"_id": STATS_DDOC_ID, "_rev": "1-a", "version": STATS_DDOC_VERSION - 1})
    _service(old).namespace_stats("asf:akm")
    assert old.docs[STATS_DDOC_ID]["version"] == STATS_DDOC_VERSION


def test_falls_back_to_totalling_records_when_a_view_fails(monkeypatch):
    called = {}

    def by_find(self, user_id, namespace=None):
        called["args"] = (user_id, namespace)
        return {"x": {"recordCount": 1, "sizeBytes": 1, "agents": [], "updatedAt": None}}

    monkeypatch.setattr(cdb.DatabaseService, "namespace_stats", by_find)
    out = _service(FakeDB(fail=True)).namespace_stats("asf:akm", "x")
    assert called["args"] == ("asf:akm", "x") and list(out) == ["x"]


def test_only_built_in_reduces():
    """A JavaScript reduce took 100 s for 4,000 namespaces in production."""
    from services.database_service.couchdb import STATS_DDOC
    assert {v["reduce"] for v in STATS_DDOC["views"].values()} <= {"_stats", "_count", "_sum"}


def test_reads_the_three_views_at_once():
    """In turn they took about 7 s on 400,000 records: three views, ~2 s each.
    Each view here waits for the other two; read in turn, they never meet."""
    import threading
    meet = threading.Barrier(3, timeout=2)

    class MeetingDB(FakeDB):
        def view(self, name, **opts):
            meet.wait()
            return super().view(name, **opts)

    out = _service(MeetingDB()).namespace_stats("asf:akm")
    assert out["notes"]["recordCount"] == 1


def test_keeps_an_answer_until_a_write_or_it_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cdb.time, "monotonic", lambda: clock[0])
    db = FakeDB()
    s = _service(db)
    first = s.namespace_stats("asf:akm")
    first["notes"]["recordCount"] = 99          # a caller's copy, not the kept one
    assert s.namespace_stats("asf:akm")["notes"]["recordCount"] == 1
    assert len(db.views) == 3                   # the second came from memory

    s.forget_namespace_stats("someone-else")
    s.namespace_stats("asf:akm")
    assert len(db.views) == 3
    s.forget_namespace_stats("asf:akm")
    s.namespace_stats("asf:akm")
    assert len(db.views) == 6

    clock[0] += cdb.STATS_CACHE_SECONDS + 1
    s.namespace_stats("asf:akm")
    assert len(db.views) == 9


def test_a_failed_read_is_not_kept(monkeypatch):
    monkeypatch.setattr(cdb.DatabaseService, "namespace_stats",
                        lambda self, u, n=None: {"x": {}})
    db = FakeDB(fail=True)
    s = _service(db)
    s.namespace_stats("asf:akm")
    db.fail = False
    assert "notes" in s.namespace_stats("asf:akm")


def test_data_store_writes_forget_the_owners_totals():
    from services.data_store_service import DataStoreService
    from services.database_service.memory import MemoryDBService

    class Recording(MemoryDBService):
        def __init__(self):
            super().__init__()
            self.forgot = []

        def forget_namespace_stats(self, user_id):
            self.forgot.append(user_id)

    db = Recording()
    store = DataStoreService(db)
    store.set("u", "ns", "a", 1)
    store.set("u", "ns", "a", 2)                # the conflict path
    store.set_many("u", [("ns", "b", 3, None)])
    store.delete("u", "ns", "a")
    store.delete_many("u", "ns", ["b"])
    store.set("u", "ns", "c", 4)
    store.clear_namespace("u", "ns")
    assert db.forgot == ["u"] * 7


def test_lists_namespaces_from_the_view_and_up_to_date():
    """{"userId": u} alone has no index: listing namespaces read every
    record in the store. A run lists them to clear them, so not lazy."""
    db = FakeDB()
    names = _service(db).data_store_namespaces("asf:akm")
    assert names == ["files:apache/steve", "notes"]
    (name, opts), = db.views
    assert (name, opts["group_level"], opts["startkey"]) == ("gofannon_ns/size", 2, ["asf:akm"])
    assert "update" not in opts


def test_lists_namespaces_by_finding_records_when_the_view_fails(monkeypatch):
    monkeypatch.setattr(cdb.DatabaseService, "data_store_namespaces", lambda self, u: ["x"])
    assert _service(FakeDB(fail=True)).data_store_namespaces("asf:akm") == ["x"]
