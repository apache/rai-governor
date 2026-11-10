"""Memory pilot through the real app: create_app mounts /memory only when enabled.

Runs on the in-memory database and blob store, so it needs no services.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store

ADMIN = {"uid": "asf:admin", "is_site_admin": True}
READER = {"uid": "asf:reader", "is_site_admin": False}


def _client(monkeypatch, user, enabled=True):
    monkeypatch.setenv("MEMORY_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_user_service_dep] = lambda: None
    client = TestClient(app)
    client.user = user
    return client


def _write(client, **over):
    body = {
        "type": "fact",
        "summary": "String-built SQL in the order report query",
        "body": "# SQLi in OrderReport\n\nThe sort parameter is concatenated.\n",
        "source": ["https://github.com/apache/example/blob/main/OrderReport.java#L88"],
        "review_by": "2027-02-01",
    }
    body.update(over)
    return client.post("/memory/dom-scan/entries", json=body)


def test_not_mounted_unless_enabled(monkeypatch):
    client = _client(monkeypatch, ADMIN, enabled=False)
    assert client.get("/memory/dom-scan/search").status_code == 404
    assert _write(client).status_code == 404


def test_write_search_get_supersede_recall(monkeypatch):
    client = _client(monkeypatch, ADMIN)

    r = _write(client)
    assert r.status_code == 201, r.text
    first = r.json()["frontmatter"]
    assert first["trust_tier"] == "raw"
    assert first["author"] == "human:asf:admin"

    hits = client.get(
        "/memory/dom-scan/search", params={"q": "what SQL problems are in the report?"}
    ).json()["hits"]
    assert [h["id"] for h in hits] == [first["id"]]

    due = client.get("/memory/dom-scan/search", params={"due_by": "2027-03-01"}).json()
    assert [h["id"] for h in due["hits"]] == [first["id"]]
    assert client.get("/memory/dom-scan/search", params={"type": "convention"}).json()["hits"] == []

    got = client.get(f"/memory/entries/{first['id']}").json()
    assert "concatenated" in got["body"]

    r = _write(client, summary="Order report now uses the sort allowlist", supersedes=first["id"])
    assert r.status_code == 201, r.text
    second = r.json()["frontmatter"]
    old = client.get(f"/memory/entries/{first['id']}").json()["frontmatter"]
    assert old["status"] == "superseded"
    ids = [h["id"] for h in client.get("/memory/dom-scan/search", params={"q": "order report"}).json()["hits"]]
    assert ids == [second["id"]]

    with patch("services.llm_service.call_llm", new_callable=AsyncMock) as llm:
        llm.return_value = (f"Fixed by the allowlist [{second['id']}].", None)
        out = client.post("/memory/dom-scan/recall", json={"question": "order report sort"}).json()
    assert out["cited"] == [second["id"]]
    assert llm.await_args.kwargs["user_id"] == "asf:admin"


def test_reader_reads_but_cannot_write(monkeypatch):
    client = _client(monkeypatch, ADMIN)
    entry = _write(client).json()["frontmatter"]
    client.app.dependency_overrides[get_current_user] = lambda: READER
    assert client.get(f"/memory/entries/{entry['id']}").status_code == 404
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    assert client.get(f"/memory/entries/{entry['id']}").status_code == 200
    assert _write(client).status_code == 403


@pytest.mark.parametrize(
    "path",
    ["/memory/dom-secret/search", "/memory/entries/mem-scan-nothere1"],
)
def test_unknown_domain_or_entry_is_404(monkeypatch, path):
    client = _client(monkeypatch, READER)
    assert client.get(path).status_code == 404


def test_superseding_a_missing_entry_is_404(monkeypatch):
    client = _client(monkeypatch, ADMIN)
    assert _write(client, supersedes="mem-scan-nothere1").status_code == 404
