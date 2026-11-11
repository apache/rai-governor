import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes_memory
from dependencies import get_db
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.setenv("MEMORY_WRITERS", "writer")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    db = MemoryDBService()
    app = FastAPI()
    app.include_router(routes_memory.router)
    app.dependency_overrides[get_db] = lambda: db
    user = {"uid": "reader", "is_site_admin": False}
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app)
    c.user = user
    return c


BODY = {"body": "# SQLi\n\nline 10\n", "summary": "String-built SQL in a.py"}


def test_reader_cannot_write(client, monkeypatch):
    assert client.post("/memory/dom-scan/entries", json=BODY).status_code == 404
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    assert client.post("/memory/dom-scan/entries", json=BODY).status_code == 403


def test_writer_writes_then_readers_of_an_open_domain_read(client, monkeypatch):
    client.user["uid"] = "writer"
    r = client.post("/memory/dom-scan/entries", json={**BODY, "trust_tier": "verified"})
    assert r.status_code == 201
    front = r.json()["frontmatter"]
    assert front["trust_tier"] == "raw"
    assert front["author"] == "human:gofannon:writer"

    client.user["uid"] = "reader"
    assert client.get("/memory/dom-scan/search", params={"q": "sql"}).status_code == 404
    assert client.get(f"/memory/entries/{front['id']}").status_code == 404
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    hits = client.get("/memory/dom-scan/search", params={"q": "sql"}).json()["hits"]
    assert [h["id"] for h in hits] == [front["id"]]
    got = client.get(f"/memory/entries/{front['id']}").json()
    assert got["body"] == BODY["body"]


def test_unknown_domain_is_404(client):
    assert client.get("/memory/dom-secret/search").status_code == 404
    client.user["is_site_admin"] = True
    assert client.post("/memory/dom-secret/entries", json=BODY).status_code == 404


def test_multiline_summary_is_rejected(client):
    client.user["is_site_admin"] = True
    r = client.post("/memory/dom-scan/entries", json={**BODY, "summary": "a\nb"})
    assert r.status_code == 422


def test_the_review_queue_is_chosen_before_the_limit(client):
    """250 older reviewed entries and one newer raw one: the oldest 200 of
    everything holds only reviewed ones, so the Review tab showed nothing.
    The queue keeps raw and overdue entries before it counts."""
    from datetime import date, timedelta
    from services.memory.index import INDEX_DB
    db = client.app.dependency_overrides[get_db]()
    later = (date.today() + timedelta(days=60)).isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()
    for i in range(250):
        db.save(INDEX_DB, f"kept-{i:03d}", {"_id": f"kept-{i:03d}", "id": f"kept-{i:03d}", "domain": "dom-scan",
                                             "trust_tier": "curated", "review_by": later,
                                             "created": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"})
    db.save(INDEX_DB, "overdue", {"_id": "overdue", "id": "overdue", "domain": "dom-scan", "trust_tier": "verified",
                                  "review_by": past, "created": "2026-02-01T00:00:00"})
    db.save(INDEX_DB, "new-raw", {"_id": "new-raw", "id": "new-raw", "domain": "dom-scan", "trust_tier": "raw",
                                  "created": "2026-03-01T00:00:00"})
    client.user["uid"] = "writer"
    old_way = client.get("/memory/dom-scan/search", params={"order": "oldest", "limit": 200}).json()["hits"]
    assert {h["id"] for h in old_way}.isdisjoint({"overdue", "new-raw"})
    queue = client.get("/memory/dom-scan/search", params={"order": "oldest", "queue": "true", "limit": 1000}).json()["hits"]
    assert [h["id"] for h in queue] == ["overdue", "new-raw"]
