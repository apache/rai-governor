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
