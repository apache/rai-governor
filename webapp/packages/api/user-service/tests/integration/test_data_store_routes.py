"""Data-store routes take namespaces with slashes in them.

Every ASVS namespace has one (audit-reports:ASVS/llmao-experiments/...,
files:apache/whimsy-private). The webui encodes the namespace with
encodeURIComponent, the server decodes %2F back to "/", and a plain
{namespace} parameter does not match across a slash: the Data Stores page
returned 404 for every one of those namespaces.
"""

from urllib.parse import quote

from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_async_db, get_db
from routes import get_current_user
from services.database_service.async_shim import AsyncDatabaseService
from services.database_service.memory import MemoryDBService
from services.data_store_service import get_data_store_service

USER = {"uid": "asf:akm", "is_site_admin": True}
NS = "audit-reports:ASVS/llmao-experiments/whimsy-private/whimsy-private-2026-11-07-3234b76"
KEY = "observability_and_resilience/15.3.3.md"


def _client():
    db = MemoryDBService()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: USER
    get_data_store_service(db).set("asf:akm", NS, KEY, "# ASVS 15.3.3\n\nreport body\n")
    get_data_store_service(db).set("asf:akm", "plain", "k", "v")
    return TestClient(app)


def _url(ns, *rest):
    return "/data-store/namespaces/" + "/".join([quote(ns, safe="")] + [quote(r, safe="") for r in rest])


def test_stats_list_and_get_with_slashes():
    c = _client()
    r = c.get(_url(NS))
    assert r.status_code == 200 and r.json()["recordCount"] == 1
    r = c.get(_url(NS, "records"))
    assert r.status_code == 200 and [x["key"] for x in r.json()] == [KEY]
    r = c.get(_url(NS, "records", KEY))
    assert r.status_code == 200 and r.json()["value"].startswith("# ASVS 15.3.3")


def test_put_and_delete_with_slashes():
    c = _client()
    r = c.put(_url(NS, "records", KEY), json={"value": "# ASVS 15.3.3\n\nedited\n"})
    assert r.status_code == 200 and r.json()["value"].endswith("edited\n")
    assert c.delete(_url(NS, "records", KEY)).status_code == 204
    assert c.get(_url(NS, "records", KEY)).status_code == 404


def test_namespaces_without_slashes_still_work():
    c = _client()
    assert c.get(_url("plain")).json()["recordCount"] == 1
    assert c.get(_url("plain", "records", "k")).json()["value"] == "v"


class _ProjectingDB(MemoryDBService):
    """Returns only the fields asked for, as CouchDB does, and records
    every find, so a test can see whether values were read."""

    def __init__(self):
        super().__init__()
        self.finds = []

    def find(self, db_name, selector, fields=None, limit=10000, **kw):
        self.finds.append((db_name, fields))
        docs = super().find(db_name, selector, fields=None, limit=limit, **kw)
        if fields:
            keep = set(fields) | {"_id"}
            docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
        return docs


def test_namespace_stats_never_read_values():
    """Listing namespaces read every record's value to total sizes: for a
    downloaded repository, every file. The home page did it on each load."""
    import json
    def value_size(v):
        return len(json.dumps(v))
    db = _ProjectingDB()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: USER
    store = get_data_store_service(db)
    big = "x" * 50_000
    store.set("asf:akm", "files:apache/steve", "a.py", big, agent_name="asvs_download_repo")
    store.set_many("asf:akm", [("files:apache/steve", "b.py", "print(1)", None),
                               ("files:apache/steve", "c.py", {"n": 1}, None)],
                   agent_name="asvs_download_repo")
    c = TestClient(app)
    db.finds.clear()

    listing = c.get("/data-store/namespaces").json()
    one = c.get(_url("files:apache/steve")).json()

    want = value_size(big) + value_size("print(1)") + value_size({"n": 1})
    assert [n["sizeBytes"] for n in listing["namespaces"]] == [want] and one["sizeBytes"] == want
    assert listing["namespaces"][0]["recordCount"] == 3 and one["recordCount"] == 3
    assert one["agents"] == ["asvs_download_repo"]
    stats_finds = [f for d, f in db.finds if d == "agent_data_store"]
    assert stats_finds and all(f and "value" not in f for f in stats_finds), db.finds


def test_a_rewrite_stores_the_new_size():
    db = MemoryDBService()
    store = get_data_store_service(db)
    store.set("asf:akm", "n", "k", "short")
    store.set("asf:akm", "n", "k", "a much longer value")       # conflict path: merge
    store.set_many("asf:akm", [("n", "k", "x" * 10, None), ("n", "k2", "y", None)])  # existing and new
    docs = {d["key"]: d for d in db.list_all("agent_data_store")}
    assert docs["k"]["sizeBytes"] == len('"' + "x" * 10 + '"') and docs["k2"]["sizeBytes"] == 3


def test_the_list_uses_kept_totals_and_clearing_drops_the_namespace():
    """The namespace list may come from totals kept across workers; clearing
    a namespace on the Data Stores page takes it out of them at once."""
    class Keeping(MemoryDBService):
        def __init__(self):
            super().__init__()
            self.calls = []

        def kept_namespace_stats(self, user_id):
            self.calls.append(("kept", user_id))
            return super().kept_namespace_stats(user_id)

        def drop_kept_namespace(self, user_id, namespace):
            self.calls.append(("drop", user_id, namespace))

    db = Keeping()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: USER
    get_data_store_service(db).set("asf:akm", NS, KEY, "x")
    c = TestClient(app)
    assert [n["namespace"] for n in c.get("/data-store/namespaces").json()["namespaces"]] == [NS]
    assert c.delete(_url(NS)).json()["deletedCount"] == 1
    assert db.calls == [("kept", "asf:akm"), ("drop", "asf:akm", NS)]
