"""Memory entries know the run that wrote them (B11).

An agent's ``memory.write`` stamps the run id, which agent code can't set or
change. A domain's readers can list its entries by run and see which runs
wrote into it; the run page tallies a run's entries per domain. What the run
itself was (its agent, status, project) shows only to someone who may see
the run.
"""

import pytest
from fastapi.testclient import TestClient

import dependencies
from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services import run_registry as rr
from services.database_service.memory import MemoryDBService
from services.memory import blob_store
from services.memory.entry import Entry, Frontmatter


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


COMMITTER = {"uid": "asf:alice", "workspaces": _ws(("whimsy", "member"))}
OTHER = {"uid": "asf:carol", "workspaces": _ws(("tomcat", "member"))}
ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}

AGENT = '''
async def run(input_dict, tools):
    a = await memory.write(summary="SQLi in board.rb", body="# SQLi\\n", origin="asvs:apache/whimsy")
    b = await memory.write(summary="XSS in roster", body="# XSS\\n", type="fact")
    return {"ids": [a, b]}
'''


class _Who:
    user = ADMIN


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.delenv("MEMORY_OPEN_DOMAINS", raising=False)
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    db = MemoryDBService()
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    app.dependency_overrides[get_user_service_dep] = lambda: None
    return db, reg, TestClient(app)


async def _run(db, reg, project="whimsy", user=ADMIN):
    rec = reg.new_record(user_id=user["uid"], agent_name="asvs_consolidate", project=project)
    out, _ = await dependencies._execute_agent_code(
        code=AGENT, input_dict={}, tools={}, gofannon_agents=[], db=db,
        user_id=user["uid"], user_basic_info={"is_site_admin": bool(user.get("is_site_admin"))},
        agent_name="asvs_consolidate", trace=rec.trace, project=project,
    )
    return rec, out["ids"]


@pytest.mark.asyncio
async def test_agent_entries_carry_their_run(env):
    db, reg, c = env
    rec, ids = await _run(db, reg)
    _Who.user = COMMITTER
    for eid in ids:
        assert c.get(f"/memory/entries/{eid}").json()["frontmatter"]["run"] == rec.run_id
    hits = c.get("/memory/whimsy/search", params={"run": rec.run_id, "limit": 500}).json()["hits"]
    assert sorted(h["id"] for h in hits) == sorted(ids)
    assert all(h["run"] == rec.run_id for h in hits)
    assert c.get("/memory/whimsy/search", params={"run": "someone-else"}).json()["hits"] == []


@pytest.mark.asyncio
async def test_a_run_without_a_registry_record_writes_no_run(env):
    db, reg, c = env
    out, _ = await dependencies._execute_agent_code(
        code=AGENT, input_dict={}, tools={}, gofannon_agents=[], db=db,
        user_id="asf:root", user_basic_info={"is_site_admin": True},
        agent_name="x", project="whimsy",
    )
    _Who.user = ADMIN
    assert "run" not in c.get(f"/memory/entries/{out['ids'][0]}").json()["frontmatter"]


@pytest.mark.asyncio
async def test_runs_per_domain_and_entries_per_run(env):
    db, reg, c = env
    first, _ = await _run(db, reg)
    second, _ = await _run(db, reg)
    _Who.user = COMMITTER
    runs = c.get("/memory/whimsy/runs").json()["runs"]
    assert [r["run"] for r in runs] == [second.run_id, first.run_id]
    assert runs[0]["entries"] == 2 and runs[0]["by_type"] == {"fact": 2} and runs[0]["by_tier"] == {"raw": 2}
    assert runs[0]["info"]["visible"] and runs[0]["info"]["agentName"] == "asvs_consolidate"
    assert "inputDict" not in runs[0]["info"]

    by_run = c.get(f"/memory/by-run/{first.run_id}").json()
    assert [(d["domain"], d["entries"], d["canReview"]) for d in by_run["domains"]] == [("whimsy", 2, False)]

    _Who.user = OTHER
    assert c.get("/memory/whimsy/runs").status_code == 404
    assert c.get(f"/memory/by-run/{first.run_id}").json() == {
        "run": {"runId": first.run_id, "visible": False}, "domains": []}


SEER = '''
async def run(input_dict, tools):
    await memory.annotate(input_dict["id"], {"seen_again": [{"finding": "FINDING-003"}]})
    return {}
'''


@pytest.mark.asyncio
async def test_a_run_that_finds_an_entry_again_shows_it(env):
    # A later scan matched its finding to an earlier one and annotated that
    # instead of writing a new entry: the later run's triage shows it.
    db, reg, c = env
    first, ids = await _run(db, reg)
    for _ in range(2):          # two later runs, each annotating twice: each recorded once
        later = reg.new_record(user_id="asf:root", agent_name="asvs_compare", project="whimsy")
        await dependencies._execute_agent_code(
            code=SEER, input_dict={"id": ids[0]}, tools={}, gofannon_agents=[], db=db,
            user_id="asf:root", user_basic_info={"is_site_admin": True},
            agent_name="asvs_compare", trace=later.trace, project="whimsy",
        )
        await dependencies._execute_agent_code(
            code=SEER, input_dict={"id": ids[0]}, tools={}, gofannon_agents=[], db=db,
            user_id="asf:root", user_basic_info={"is_site_admin": True},
            agent_name="asvs_compare", trace=later.trace, project="whimsy",
        )
    _Who.user = COMMITTER
    hits = c.get("/memory/whimsy/search", params={"run": later.run_id}).json()["hits"]
    assert [h["id"] for h in hits] == [ids[0]]
    assert hits[0]["run"] == first.run_id and hits[0]["seen_in_runs"][-1] == later.run_id
    assert len(hits[0]["seen_in_runs"]) == 2
    assert len(c.get("/memory/whimsy/search", params={"run": first.run_id}).json()["hits"]) == 2
    by_run = c.get(f"/memory/by-run/{later.run_id}").json()["domains"]
    assert [(d["domain"], d["entries"], d["seen"]) for d in by_run] == [("whimsy", 0, 1)]


FORGER = '''
async def run(input_dict, tools):
    memory.run_id = "victim-run"
    a = await memory.write(summary="one", body="# x\\n")
    from services.memory.service import WriteRequest
    from services.memory.entry import EntryType
    try:
        WriteRequest(domain="whimsy", type=EntryType.FACT, body="b", summary="s", run="victim-run")
        made = "accepted"
    except TypeError:
        made = "refused"
    return {"ids": [a], "made": made}
'''


@pytest.mark.asyncio
async def test_agent_code_cannot_choose_the_run(env):
    db, reg, c = env
    rec = reg.new_record(user_id="asf:root", agent_name="forger", project="whimsy")
    out, _ = await dependencies._execute_agent_code(
        code=FORGER, input_dict={}, tools={}, gofannon_agents=[], db=db,
        user_id="asf:root", user_basic_info={"is_site_admin": True},
        agent_name="forger", trace=rec.trace, project="whimsy",
    )
    _Who.user = ADMIN
    assert out["made"] == "refused"
    assert c.get(f"/memory/entries/{out['ids'][0]}").json()["frontmatter"]["run"] == rec.run_id


def test_frontmatter_round_trips_run():
    fm = Frontmatter.from_dict({"id": "mem-whimsy-a1b2c3d4", "type": "fact", "domain": "whimsy",
                                "author": "agent:x (model: m)", "created": "2026-11-10T00:00:00Z",
                                "summary": "s", "run": "r-1"})
    assert fm.run == "r-1" and Frontmatter.from_dict(fm.to_dict()).run == "r-1"
