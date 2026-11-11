"""CouchDB keeps data-store totals per namespace in views (ISSUE-034):
listing namespaces reads a row per namespace, not one per record."""

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
            "gofannon_stats/by_namespace": [
                (["asf:akm", "files:apache/steve"], [3, 1200, "2026-11-11T10:00:00"]),
                (["asf:akm", "notes"], [1, 7, ""]),
            ],
            "gofannon_stats/agents": [
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
                               "updatedAt": "2026-11-11T10:00:00"},
        "notes": {"recordCount": 1, "sizeBytes": 7, "agents": [], "updatedAt": None},
    }
    assert [(n, o["group_level"], o["startkey"], o["endkey"]) for n, o in db.views] == [
        ("gofannon_stats/by_namespace", 2, ["asf:akm"], ["asf:akm", {}]),
        ("gofannon_stats/agents", 3, ["asf:akm"], ["asf:akm", {}]),
    ]


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
