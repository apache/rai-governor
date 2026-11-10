"""Reviews, history and the domain summary (ISSUE-026's API).

The rule that matters most: whoever wrote an entry cannot review it.
"""

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes_memory
from dependencies import get_db
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store
from services.memory.service import REVIEWS_DB


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.setenv("MEMORY_WRITERS", "alice,bob")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    db = MemoryDBService()
    app = FastAPI()
    app.include_router(routes_memory.router)
    app.dependency_overrides[get_db] = lambda: db
    user = {"uid": "alice", "is_site_admin": False}
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app)
    c.user = user
    c.db = db
    return c


def _as(client, uid):
    client.user["uid"] = uid
    return client


def _write(client, **over):
    body = {"body": "# SQLi\n\nline 10\n", "summary": "String-built SQL in a.py",
            "review_by": "2026-01-01"}
    body.update(over)
    r = _as(client, "alice").post("/memory/dom-scan/entries", json=body)
    assert r.status_code == 201, r.text
    return r.json()["frontmatter"]["id"]


def _review(client, uid, entry_id, **body):
    return _as(client, uid).post(f"/memory/entries/{entry_id}/review", json=body)


def test_the_author_cannot_review_their_own_entry(client):
    eid = _write(client)
    r = _review(client, "alice", eid, action="verify", why="checked line 10")
    assert r.status_code == 403
    assert "someone else" in r.json()["detail"]
    assert client.db.list_all(REVIEWS_DB) == []


def test_a_reader_cannot_review(client, monkeypatch):
    eid = _write(client)
    # A shared domain is unreadable to non-writers unless opened (ISSUE-002).
    assert _review(client, "carol", eid, action="keep").status_code == 404
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    assert _review(client, "carol", eid, action="keep").status_code == 403


def test_curate_then_verify_records_each_review_and_version(client):
    eid = _write(client)
    r = _review(client, "bob", eid, action="curate", why="read a.py at HEAD")
    assert r.status_code == 200, r.text
    assert r.json()["frontmatter"]["trust_tier"] == "curated"
    r = _review(client, "bob", eid, action="verify", why="reproduced with a test",
                review_by="2027-06-01")
    front = r.json()["frontmatter"]
    assert (front["trust_tier"], front["review_by"]) == ("verified", "2027-06-01")

    h = client.get(f"/memory/entries/{eid}/history").json()
    assert [x["action"] for x in h["reviews"]] == ["curate", "verify"]
    first = h["reviews"][0]
    assert (first["by"], first["author"], first["tier"]) == (
        "human:gofannon:bob", "human:gofannon:alice", "curated")
    # Each review replaced a version, and the record names the one it judged.
    assert len(h["versions"]) == 2
    old = client.get(f"/memory/entries/{eid}/versions/{h['versions'][0]['version']}").json()
    assert old["frontmatter"]["trust_tier"] == "raw"
    assert h["versions"][0]["digest"] == first["digest"][:8]


def test_promotions_need_a_why_and_never_go_down(client):
    eid = _write(client)
    assert _review(client, "bob", eid, action="curate", why="  ").status_code == 409
    assert _review(client, "bob", eid, action="verify", why="ok").status_code == 200
    r = _review(client, "bob", eid, action="curate", why="again")
    assert r.status_code == 409
    assert "verified already" in r.json()["detail"]


def test_keep_moves_the_review_date_on(client):
    eid = _write(client)
    r = _review(client, "bob", eid, action="keep")
    assert r.status_code == 200
    expected = (date.today() + timedelta(days=90)).isoformat()
    assert r.json()["frontmatter"]["review_by"] == expected
    assert r.json()["frontmatter"]["trust_tier"] == "raw"


def test_demote_takes_the_entry_out_of_search_and_review(client):
    eid = _write(client)
    assert _review(client, "bob", eid, action="demote", why="").status_code == 409
    assert _review(client, "bob", eid, action="demote", why="a.py was rewritten").status_code == 200
    assert client.get("/memory/dom-scan/search", params={"q": "sql"}).json()["hits"] == []
    hits = client.get("/memory/dom-scan/search",
                      params={"q": "sql", "include_inactive": True}).json()["hits"]
    assert [(h["id"], h["status"]) for h in hits] == [(eid, "demoted")]
    assert _review(client, "bob", eid, action="keep").status_code == 409


def test_domains_counts_and_who_you_are(client):
    a = _write(client)
    _write(client, review_by="2999-01-01")
    _write(client)  # raw and overdue: one place in the queue, not two
    _review(client, "bob", a, action="curate", why="fine", review_by="2999-01-01")
    out = _as(client, "bob").get("/memory/domains").json()
    assert out["domains"] == [
        {"domain": "dom-scan", "kind": "shared", "canWrite": True, "canReview": True,
         "entries": 3, "raw": 2, "due": 1, "review": 2}]
    assert out["you"] == {"author": "human:gofannon:bob", "canWrite": True, "canReview": True}
    carol = _as(client, "carol").get("/memory/domains").json()
    assert carol["domains"] == [] and carol["you"]["canReview"] is False


def test_search_returns_author_and_orders_oldest_first(client):
    a = _write(client, summary="first sql note")
    b = _write(client, summary="second sql note")
    newest = client.get("/memory/dom-scan/search").json()["hits"]
    assert [h["id"] for h in newest] == [b, a]
    assert newest[0]["author"] == "human:gofannon:alice"
    oldest = client.get("/memory/dom-scan/search", params={"order": "oldest"}).json()["hits"]
    assert [h["id"] for h in oldest] == [a, b]


@pytest.mark.parametrize("path", [
    "/memory/entries/mem-scan-nothere1/history",
    "/memory/entries/mem-scan-nothere1/versions/x",
])
def test_missing_entry_or_version_is_404(client, path):
    assert client.get(path).status_code == 404


def test_unsafe_version_name_is_404(client):
    eid = _write(client)
    assert client.get(f"/memory/entries/{eid}/versions/..%2F..%2Fx").status_code == 404
