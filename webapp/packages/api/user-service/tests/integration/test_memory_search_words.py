"""Searching memory finds what people type: a part of a compound word
("governor" in "rai-governor"), the start of a word, the repository, the
finding id and the run, and, with domain "*", every domain the caller reads
and no other."""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store
from services.memory.index import _row_words, query_terms, _matches

ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}
PMC = {"uid": "asf:pmc", "workspaces": [{"workspaceId": "project:whimsy", "role": "admin",
                                         "displayName": "whimsy", "source": "ldap_committer"}]}


class _Who:
    user = ADMIN


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.delenv("MEMORY_OPEN_DOMAINS", raising=False)
    monkeypatch.delenv("MEMORY_WRITERS", raising=False)
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    app.dependency_overrides[get_user_service_dep] = lambda: None
    _Who.user = ADMIN
    return TestClient(app)


def _write(client, domain, summary, origin=None):
    r = client.post(f"/memory/{domain}/entries", json={
        "type": "fact", "summary": summary, "body": "x\n", "origin": origin})
    assert r.status_code == 201, r.text
    return r.json()["frontmatter"]["id"]


def _ids(client, domain, q):
    r = client.get(f"/memory/{domain}/search", params={"q": q, "limit": 100})
    assert r.status_code == 200, r.text
    return {h["id"] for h in r.json()["hits"]}


def test_a_part_of_a_compound_word_and_the_start_of_a_word_match(client):
    gov = _write(client, "dom-scan", "Medium in apache/rai-governor: open redirect",
                 origin="asvs:finding:apache/rai-governor")
    other = _write(client, "dom-scan", "Low in apache/whimsy: stale cache")
    assert _ids(client, "dom-scan", "governor") == {gov}
    assert _ids(client, "dom-scan", "rai-governor") == {gov}
    assert _ids(client, "dom-scan", "gov") == {gov}
    assert _ids(client, "dom-scan", "redir") == {gov}
    assert _ids(client, "dom-scan", "whimsy") == {other}


def test_the_repository_is_searched_through_the_origin(client):
    eid = _write(client, "dom-scan", "Unbounded upload size", origin="asvs:finding:apache/rai-governor")
    assert _ids(client, "dom-scan", "governor") == {eid}


def test_finding_ids_and_runs_are_words_too():
    row = {"title": "Open redirect", "summary": "Medium in apache/rai-governor: open redirect",
           "origin": "asvs:finding:apache/rai-governor", "run": "f6c9b9a0-7fe8-49b9-88ff-24120f86ce1f",
           "evidence": {"finding": {"global_id": "FINDING-006"}}}
    words = _row_words(row)
    for q in ("FINDING-006", "finding-006", "006", "f6c9b9a0", "governor", "apache/rai-governor"):
        assert all(_matches(t, words) for t in query_terms(q)), q
    assert not any(_matches(t, words) for t in query_terms("tomcat"))


def test_all_domains_searches_every_readable_domain_and_no_other(client):
    in_scan = _write(client, "dom-scan", "Governor finding in the shared domain")
    in_whimsy = _write(client, "whimsy", "Governor finding in whimsy")
    in_tomcat = _write(client, "tomcat", "Governor finding in tomcat")
    out = client.get("/memory/*/search", params={"q": "governor", "limit": 100}).json()
    assert {h["id"] for h in out["hits"]} == {in_scan, in_whimsy, in_tomcat}
    assert {h["domain"] for h in out["hits"]} == {"dom-scan", "whimsy", "tomcat"}

    _Who.user = PMC   # whimsy's PMC: whimsy, and no shared or other project domain
    out = client.get("/memory/*/search", params={"q": "governor", "limit": 100}).json()
    assert {h["id"] for h in out["hits"]} == {in_whimsy}
