"""Triage: dispositions, guidance, proposals and the digest (scans that remember).

A disposition applies the trust action it implies and keeps the kind and
reason on the finding's index row. "Needs context" and "by design" write a
guidance entry as the person, one per repo however many findings it covers.
Undo puts the findings back and withdraws the guidance. Scans load a
person's guidance at any tier, an agent's only once curated, and a digest
only once a person has accepted it.
"""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import blob_store
from services.memory.agent_proxy import AgentMemory
from services.memory.index import INDEX_DB


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


PMC = {"uid": "asf:pmc", "workspaces": _ws(("tooling", "admin"))}
COMMITTER = {"uid": "asf:alice", "workspaces": _ws(("tooling", "member"))}
OTHER_PMC = {"uid": "asf:tpmc", "workspaces": _ws(("tomcat", "admin"))}
REPO = "apache/tooling-trusted-releases"


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
    client = TestClient(app)
    client.db = db
    client.mem = AgentMemory(db, "asvs_consolidate", user_id="asf:root", is_site_admin=True,
                             project="tooling")
    return client


async def _finding(c, title="PubSub URL not validated", repo=REPO + "/atr"):
    return await c.mem.write(summary=f"Low in {repo}: {title}", body=f"# {title}\n",
                             origin=f"asvs:finding:{repo}", model="qwen3.8-27b", title=title)


def _fm(c, eid):
    _as(PMC)
    return c.get(f"/memory/entries/{eid}").json()["frontmatter"]


def _row(c, eid):
    return c.db.get(INDEX_DB, eid)


@pytest.mark.asyncio
async def test_needs_context_demotes_and_writes_guidance_scans_load(c):
    f = await _finding(c)
    _as(PMC)
    r = c.post(f"/memory/entries/{f}/disposition", json={
        "kind": "fp_context", "why": "fixed in #1683",
        "guidance": {"text": "PubSub transport is enforced in atr/pubsub.py.\n\nDon't report it."}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] == [f] and len(out["guidance"]) == 1
    gid = out["guidance"][0]
    assert _fm(c, f)["status"] == "demoted"
    assert _row(c, f)["disposition"] == "fp_context"
    assert _row(c, f)["decision"]["guidance_id"] == gid
    g = _fm(c, gid)
    assert g["origin"] == f"guidance:{REPO}" and g["type"] == "convention"
    assert g["author"] == "human:asf:pmc" and g["trust_tier"] == "raw"
    assert g["source"] == [f] and g["review_by"]
    assert g["summary"] == "PubSub transport is enforced in atr/pubsub.py."

    # The finding is out of plain search, and found again by disposition.
    hits = c.get("/memory/tooling/search", params={"include_inactive": "true",
                                                    "disposition": "fp_context"}).json()["hits"]
    assert [h["id"] for h in hits] == [f]
    assert hits[0]["why"] == "fixed in #1683" and hits[0]["decided_by"] == "human:asf:pmc"

    # A scan of the repo loads it, raw as it is, because a person wrote it.
    loaded = await c.mem.guidance(REPO, "asvs")
    assert [e["id"] for e in loaded["entries"]] == [gid]
    assert loaded["entries"][0]["body"].startswith("PubSub transport")
    # And can see the earlier decision on the finding.
    seen = await c.mem.search(None, origin=f"asvs:finding:{REPO}/atr", include_inactive=True)
    assert seen[0]["disposition"] == "fp_context"


@pytest.mark.asyncio
async def test_a_batch_writes_one_guidance_entry_and_undo_takes_it_all_back(c):
    fs = [await _finding(c, t, repo=REPO) for t in ("OIDC nonce", "state cookie", "pending states")]
    _as(PMC)
    out = c.post("/memory/dispositions/batch", json={
        "ids": fs, "kind": "fp_context", "why": "belongs to asfquart",
        "guidance": {"text": "Code under src/asfquart/ is audited in its own scans.",
                     "scope": "asvs", "always": True}}).json()
    assert sorted(out["applied"]) == sorted(fs) and len(out["guidance"]) == 1
    gid = out["guidance"][0]
    g = _fm(c, gid)
    assert g["origin"] == f"guidance:asvs:{REPO}" and g["type"] == "policy"
    assert sorted(g["source"]) == sorted(fs)
    assert (await c.mem.guidance(REPO, "glasswing"))["entries"] == []   # ASVS only

    u = c.post(f"/memory/reviews/batch/{out['batch']}/undo").json()
    assert sorted(u["undone"]) == sorted(fs) and u["withdrawn"] == [gid]
    for f in fs:
        assert "status" not in _fm(c, f)
        assert "disposition" not in _row(c, f) and "decision" not in _row(c, f)
    assert _fm(c, gid)["status"] == "demoted"
    assert (await c.mem.guidance(REPO, "asvs"))["entries"] == []


@pytest.mark.asyncio
async def test_kinds_and_their_rules(c):
    a, b = await _finding(c, "a"), await _finding(c, "b")
    _as(PMC)
    r = c.post(f"/memory/entries/{a}/disposition", json={"kind": "valid", "why": "real",
                                                          "filed_as": "https://x/advisory"})
    assert r.status_code == 200
    assert _fm(c, a)["trust_tier"] == "verified" and _row(c, a)["decision"]["filed_as"] == "https://x/advisory"
    # No guidance for valid; a duplicate says what it duplicates.
    assert r.json()["guidance"] == []
    assert c.post(f"/memory/entries/{b}/disposition",
                  json={"kind": "duplicate", "why": "same"}).status_code == 400
    r = c.post(f"/memory/entries/{b}/disposition",
               json={"kind": "duplicate", "why": "same flag", "duplicate_of": a})
    assert r.status_code == 200 and _fm(c, b)["status"] == "demoted"
    # A decided finding is decided: undo first.
    r = c.post(f"/memory/entries/{b}/disposition", json={"kind": "fp_wrong", "why": "x"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_the_reason_and_the_guidance_are_optional(c):
    a, b, d = await _finding(c, "a"), await _finding(c, "b"), await _finding(c, "d")
    _as(PMC)
    for eid, kind in ((a, "valid"), (b, "fp_wrong"), (d, "by_design")):
        r = c.post(f"/memory/entries/{eid}/disposition", json={"kind": kind})
        assert r.status_code == 200 and r.json()["applied"] == [eid] and r.json()["guidance"] == []
        assert _row(c, eid)["disposition"] == kind and _row(c, eid)["decision"]["why"] == ""
    assert _fm(c, a)["trust_tier"] == "verified" and _fm(c, b)["status"] == "demoted"
    assert _fm(c, d)["trust_tier"] == "verified"
    # The review record still says what was decided.
    reviews = c.get(f"/memory/entries/{b}/history").json()["reviews"]
    assert reviews[-1]["why"] == "Triaged as a false positive: wrong"
    # Blank guidance text writes no guidance.
    e = await _finding(c, "e")
    r = c.post(f"/memory/entries/{e}/disposition", json={"kind": "fp_context", "guidance": {"text": "  "}})
    assert r.status_code == 200 and r.json()["guidance"] == []


@pytest.mark.asyncio
async def test_who_may_decide(c):
    f = await _finding(c)
    _as(COMMITTER)
    assert c.post(f"/memory/entries/{f}/disposition",
                  json={"kind": "fp_wrong", "why": "x"}).status_code == 409   # reads, can't decide
    _as(OTHER_PMC)
    assert c.post(f"/memory/entries/{f}/disposition",
                  json={"kind": "fp_wrong", "why": "x"}).status_code == 404   # can't read it at all
    _as(PMC)
    mine = c.post("/memory/tooling/entries", json={"summary": "mine", "body": "# m\n",
                                                   "origin": f"asvs:finding:{REPO}"}).json()["frontmatter"]["id"]
    assert c.post(f"/memory/entries/{mine}/disposition",
                  json={"kind": "fp_wrong", "why": "x"}).status_code == 403


@pytest.mark.asyncio
async def test_agent_guidance_counts_only_once_curated(c):
    gid = await c.mem.write(summary="Forms are validated by Pydantic", body="Don't report it.\n",
                            origin=f"guidance:{REPO}", type="convention", model="m")
    assert (await c.mem.guidance(REPO))["entries"] == []
    _as(PMC)
    c.post(f"/memory/entries/{gid}/review", json={"action": "curate", "why": "checked"})
    assert [e["id"] for e in (await c.mem.guidance(REPO))["entries"]] == [gid]


@pytest.mark.asyncio
async def test_proposals_become_the_persons_guidance_or_are_rejected(c):
    p1 = await c.mem.write(summary="PAT reminders were declined in #1657", body="Declined in #1657.\n",
                           origin=f"proposal:guidance:{REPO}", type="decision", model="m",
                           source=["https://github.com/apache/tooling-trusted-releases/issues/1657"])
    p2 = await c.mem.write(summary="listen() reconnects", body="Back-off exists.\n",
                           origin=f"proposal:guidance:{REPO}", type="convention", model="m")
    assert (await c.mem.guidance(REPO))["entries"] == []              # proposals never load
    _as(PMC)
    out = c.post("/memory/proposals/accept", json={
        "ids": [p1], "edits": {p1: {"text": "PAT expiry reminders were declined (#1657).", "scope": "all"}}}).json()
    assert out["accepted"][0]["proposal"] == p1
    gid = out["accepted"][0]["guidance"]
    g = _fm(c, gid)
    assert g["author"] == "human:asf:pmc" and g["type"] == "decision" and g["supersedes"] == p1
    assert g["source"][0] == p1 and "issues/1657" in g["source"][1]
    assert _fm(c, p1)["status"] == "superseded"
    assert [e["id"] for e in (await c.mem.guidance(REPO))["entries"]] == [gid]

    assert c.post("/memory/proposals/reject", json={"ids": [p2], "why": ""}).status_code == 422
    out = c.post("/memory/proposals/reject", json={"ids": [p2], "why": "no back-off exists"}).json()
    assert out["rejected"] == [p2] and _fm(c, p2)["status"] == "demoted"

    listed = c.get("/memory/tooling/guidance", params={"repo": REPO}).json()
    assert [e["id"] for e in listed["entries"]] == [gid] and listed["proposals"] == []
    assert c.get("/memory/tooling/guidance-repos").json()["repos"] == [REPO]


@pytest.mark.asyncio
async def test_a_digest_loads_once_accepted_and_replaces_the_last(c):
    d1 = await c.mem.write(summary="Digest v1", body="## Rules\n- A\n- B\n",
                           origin=f"guidance:digest:{REPO}", type="convention", model="m")
    assert (await c.mem.guidance(REPO))["digest"] is None
    _as(PMC)
    view = c.get("/memory/tooling/digest", params={"repo": REPO}).json()
    assert view["current"] is None and view["draft"]["id"] == d1
    assert c.post(f"/memory/digests/{d1}/accept", json={}).status_code == 200
    assert (await c.mem.guidance(REPO))["digest"]["id"] == d1

    d2 = await c.mem.write(summary="Digest v2", body="## Rules\n- A\n- C\n",
                           origin=f"guidance:digest:{REPO}", type="convention", model="m")
    # Until v2 is accepted, scans keep v1.
    assert (await c.mem.guidance(REPO))["digest"]["id"] == d1
    view = c.get("/memory/tooling/digest", params={"repo": REPO}).json()
    assert view["draft"]["id"] == d2
    assert view["diff"] == {"added": ["- C"], "removed": ["- B"], "kept": ["- A"]}
    out = c.post(f"/memory/digests/{d2}/accept", json={}).json()
    assert out["superseded"] == d1
    assert (await c.mem.guidance(REPO))["digest"]["id"] == d2
    assert _fm(c, d1)["status"] == "superseded" and _fm(c, d2)["supersedes"] == d1
    assert c.post(f"/memory/digests/{d2}/accept", json={}).status_code == 409


@pytest.mark.asyncio
async def test_evidence_survives_a_review_and_use_is_counted(c):
    f = await _finding(c)
    await c.mem.annotate(f, {"matches": [{"kind": "issue", "number": 1683, "state": "closed"}],
                             "suggested": {"kind": "fp_context", "confidence": "high"}})
    _as(PMC)
    c.post(f"/memory/entries/{f}/review", json={"action": "curate", "why": "read it"})
    hit = c.get("/memory/tooling/search").json()["hits"][0]
    assert hit["evidence"]["suggested"]["kind"] == "fp_context" and hit["trust_tier"] == "curated"

    g = c.post(f"/memory/entries/{f}/disposition", json={
        "kind": "by_design", "why": "declined", "guidance": {"text": "Declined."}}).json()["guidance"][0]
    assert _row(c, f)["evidence"]["matches"][0]["number"] == 1683     # still there after the disposition
    assert await c.mem.used([g, g], "loaded") == 1
    assert await c.mem.used([g], "cited") == 1
    listed = c.get("/memory/tooling/guidance", params={"repo": REPO}).json()
    assert listed["usage"][g] == {"loaded": 1, "cited": 1}
    with pytest.raises(ValueError):
        await c.mem.annotate(f, {"x": "y" * 20000})
