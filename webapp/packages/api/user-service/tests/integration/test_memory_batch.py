"""Batch review and undo (B11).

One decision over many entries, each recorded on its own with the batch id.
What can't take it is skipped and listed, not the whole batch refused. An
undo puts back every entry nothing has changed since. Entries in domains
the caller can't read are reported as missing, by batch and by undo alike.
"""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


PMC = {"uid": "asf:pmc", "workspaces": _ws(("whimsy", "admin"))}
PMC2 = {"uid": "asf:pmc2", "workspaces": _ws(("whimsy", "admin"))}
COMMITTER = {"uid": "asf:alice", "workspaces": _ws(("whimsy", "member"))}
TOMCAT_PMC = {"uid": "asf:tpmc", "workspaces": _ws(("tomcat", "admin"))}

BODY = {"type": "fact", "summary": "a finding", "body": "# x\n", "review_by": "2027-02-01"}


class _Who:
    user = PMC


def _as(u):
    _Who.user = u


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.delenv("MEMORY_OPEN_DOMAINS", raising=False)
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    app.dependency_overrides[get_user_service_dep] = lambda: None
    return TestClient(app)


def _write(c, who=PMC2, domain="whimsy", **over):
    _as(who)
    r = c.post(f"/memory/{domain}/entries", json={**BODY, **over})
    assert r.status_code == 201, r.text
    return r.json()["frontmatter"]["id"]


def _fm(c, eid):
    _as(PMC)
    return c.get(f"/memory/entries/{eid}").json()["frontmatter"]


def test_demote_a_batch_and_undo_it(c):
    a, b = _write(c), _write(c)
    mine = _write(c, who=PMC)
    _as(PMC)
    r = c.post("/memory/reviews/batch", json={"ids": [a, b, mine, "mem-whimsy-nothere1"],
                                              "action": "demote", "why": "run lost its sections"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] == [a, b]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {
        mine: "You wrote it; someone else reviews it", "mem-whimsy-nothere1": "No such entry"}
    assert _fm(c, a)["status"] == "demoted"
    hist = c.get(f"/memory/entries/{a}/history").json()["reviews"]
    assert hist[-1]["batch"] == out["batch"] and hist[-1]["why"] == "run lost its sections"
    assert c.get("/memory/whimsy/search").json()["hits"][0]["id"] == mine   # demoted are out of search

    u = c.post(f"/memory/reviews/batch/{out['batch']}/undo").json()
    assert sorted(u["undone"]) == sorted([a, b]) and u["skipped"] == []
    assert "status" not in _fm(c, a) and _fm(c, a)["trust_tier"] == "raw"
    assert c.post(f"/memory/reviews/batch/{out['batch']}/undo").json()["undone"] == []


def test_undo_leaves_what_changed_since(c):
    a, b = _write(c), _write(c)
    _as(PMC)
    batch = c.post("/memory/reviews/batch", json={"ids": [a, b], "action": "curate", "why": "read"}).json()["batch"]
    _as(PMC2)
    # PMC2 wrote them, so another PMC member moves one on.
    _as({"uid": "asf:pmc3", "workspaces": _ws(("whimsy", "admin"))})
    assert c.post(f"/memory/entries/{a}/review", json={"action": "verify", "why": "checked"}).status_code == 200
    _as(PMC)
    u = c.post(f"/memory/reviews/batch/{batch}/undo").json()
    assert u["undone"] == [b]
    assert u["skipped"] == [{"id": a, "reason": "Changed since the batch; left as it is"}]
    assert _fm(c, a)["trust_tier"] == "verified" and _fm(c, b)["trust_tier"] == "raw"


def test_demoting_a_superseding_entry_can_bring_back_the_old_one(c):
    old = _write(c)
    new = _write(c, supersedes=old)
    assert _fm(c, old)["status"] == "superseded"
    _as(PMC)
    out = c.post("/memory/reviews/batch", json={"ids": [new], "action": "demote", "why": "wrong",
                                                "restore_superseded": True}).json()
    assert out["restored"] == [old]
    assert "status" not in _fm(c, old) and _fm(c, new)["status"] == "demoted"
    c.post(f"/memory/reviews/batch/{out['batch']}/undo")
    assert _fm(c, old)["status"] == "superseded" and "status" not in _fm(c, new)


def test_an_author_cannot_undo_a_review_of_their_own_entry(c):
    a = _write(c)                       # PMC2 wrote it
    _as(PMC)
    batch = c.post("/memory/reviews/batch", json={"ids": [a], "action": "demote", "why": "wrong"}).json()["batch"]
    _as(PMC2)
    u = c.post(f"/memory/reviews/batch/{batch}/undo").json()
    assert u["undone"] == [] and u["skipped"] == [{"id": a, "reason": "You wrote it; someone else reviews it"}]
    assert _fm(c, a)["status"] == "demoted"


def test_restoring_skips_what_the_reviewer_wrote_or_another_entry_replaced(c):
    mine = _write(c, who=PMC)
    newer = _write(c, supersedes=mine)
    _as(PMC)
    out = c.post("/memory/reviews/batch", json={"ids": [newer], "action": "demote", "why": "x",
                                                "restore_superseded": True}).json()
    assert out["applied"] == [newer] and out["restored"] == []
    assert _fm(c, mine)["status"] == "superseded"


def test_readers_and_strangers(c):
    a = _write(c)
    t = _write(c, who=TOMCAT_PMC, domain="tomcat")
    _as(COMMITTER)
    out = c.post("/memory/reviews/batch", json={"ids": [a, t], "action": "keep"}).json()
    assert out["applied"] == []
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {
        a: "You can read whimsy but not review it", t: "No such entry"}

    _as(TOMCAT_PMC)
    batch = c.post("/memory/reviews/batch", json={"ids": [t], "action": "demote", "why": "x"}).json()["batch"]
    _as(PMC)
    assert c.post(f"/memory/reviews/batch/{batch}/undo").status_code == 404


def test_a_reason_is_required_except_to_keep(c):
    a = _write(c)
    _as(PMC)
    assert c.post("/memory/reviews/batch", json={"ids": [a], "action": "demote"}).status_code == 400
    assert c.post("/memory/reviews/batch", json={"ids": [a], "action": "keep"}).json()["applied"] == [a]
