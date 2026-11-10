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
