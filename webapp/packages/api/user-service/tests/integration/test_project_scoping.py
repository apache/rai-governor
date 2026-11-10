"""A project's runs and data belong to the project (ISSUE-002).

A run started for a project (``project`` on /agents/run-code/start,
``?project=`` on /rest/<name>[/start]) is visible to, and stoppable by, the
project's members. Its data-store records are the project's, under
``project:<name>``: members read them, the PMC changes them. A run for no
project is its starter's alone, as before.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routes
from app_factory import create_app
from auth import project_access as pa
from config import settings
from dependencies import _execute_agent_code, get_async_db, get_db
from routes import get_current_user
from services import run_registry as rr
from services.data_store_service import get_data_store_service
from services.database_service.async_shim import AsyncDatabaseService
from services.database_service.memory import MemoryDBService


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


ALICE = {"uid": "asf:alice", "workspaces": _ws(("whimsy", "member"))}        # committer
PMC = {"uid": "asf:pmc", "workspaces": _ws(("whimsy", "admin"))}             # PMC member
CAROL = {"uid": "asf:carol", "workspaces": _ws(("tomcat", "member"))}        # another project
ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}


# --- the rules ---------------------------------------------------------------

def test_membership_rules_are_llmaos():
    assert pa.is_member(ALICE, "whimsy") and not pa.is_admin(ALICE, "whimsy")
    assert pa.is_member(PMC, "whimsy") and pa.is_admin(PMC, "whimsy")
    assert not pa.is_member(CAROL, "whimsy")
    assert pa.is_member(ADMIN, "whimsy") and pa.is_admin(ADMIN, "anything")
    assert pa.member_projects(ADMIN) == set()            # lists don't fill with everything
    assert not pa.is_member(ALICE, None) and not pa.is_member(ALICE, "")


def test_personal_workspaces_are_not_projects():
    u = {"uid": "asf:alice", "workspaces": [{"workspaceId": "personal:asf:alice", "role": "admin"}]}
    assert pa.member_projects(u) == set()


@pytest.mark.parametrize("name", ["Whimsy", "../x", "project:whimsy", "a b", "-x", "whimsy\n"])
def test_malformed_project_names_are_refused(name):
    with pytest.raises(Exception) as e:
        pa.check_project(ADMIN, name)
    assert getattr(e.value, "status_code", None) == 400


# --- runs --------------------------------------------------------------------

class _Who:
    user = ALICE


@pytest.fixture
def client(monkeypatch):
    db = MemoryDBService()
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    return db, reg, TestClient(app)


def _as(user):
    _Who.user = user


def _ids(c):
    return {r["runId"] for r in c.get("/runs").json()["runs"]}


def test_project_members_see_and_stop_its_runs(client):
    _, reg, c = client
    shared = reg.new_record(user_id="asf:alice", agent_name="asvs", project="whimsy")
    private = reg.new_record(user_id="asf:alice", agent_name="mine")
    reg.mark_complete(private, status="success")

    _as(ALICE)
    assert _ids(c) == {shared.run_id, private.run_id}
    _as(PMC)
    assert _ids(c) == {shared.run_id}
    assert c.get(f"/runs/{shared.run_id}").status_code == 200
    assert c.get(f"/runs/{private.run_id}").status_code == 404
    _as(CAROL)
    assert _ids(c) == set()
    assert c.get(f"/runs/{shared.run_id}").status_code == 404
    assert c.post(f"/runs/{shared.run_id}/stop").status_code == 404
    _as(ADMIN)
    assert c.get(f"/runs/{shared.run_id}").status_code == 200
    _as(PMC)
    assert c.post(f"/runs/{shared.run_id}/stop").status_code == 202


def test_a_run_starts_only_for_a_project_the_caller_is_in(client):
    _, reg, c = client
    body = {"code": "async def run(input_dict, tools):\n    return {'outputText': 'ok'}\n",
            "inputDict": {}, "tools": {}, "project": "whimsy"}
    _as(CAROL)
    assert c.post("/agents/run-code/start", json=body).status_code == 403
    assert c.post("/agents/run-code/start", json={**body, "project": "Not A Project"}).status_code == 400
    _as(ALICE)
    r = c.post("/agents/run-code/start", json=body)
    assert r.status_code == 202
    assert reg.get(r.json()["runId"]).project == "whimsy"
    r = c.post("/agents/run-code/start", json={**body, "project": None})
    assert reg.get(r.json()["runId"]).project is None


@pytest.mark.parametrize("path", ["/agents/run-code", "/agents/run-code/stream"])
def test_the_other_run_code_routes_honour_the_project(client, path):
    db, _, c = client
    body = {"code": WRITER, "inputDict": {}, "tools": {}, "project": "whimsy"}
    _as(CAROL)
    assert c.post(path, json=body).status_code == 403
    _as(ALICE)
    r = c.post(path, json=body)
    assert r.status_code == 200, r.text
    r.read()                                     # the stream runs to its end
    store = get_data_store_service(db)
    assert store.get("project:whimsy", "default", "k") is not None
    assert store.get("asf:alice", "default", "k") is None


def test_a_long_run_list_keeps_the_newest(client):
    # find() returns in storage order; the list must still lead with the
    # newest, running ones included, past the page size.
    db, reg, c = client
    t0 = datetime(2026, 11, 1)
    first = None
    for i in range(130):
        rec = reg.new_record(user_id="asf:pmc", agent_name=f"a{i}", project="whimsy")
        rec.started_at = t0 + timedelta(minutes=i)
        if i < 129:
            reg.mark_complete(rec, status="success")
        db.save(rr.RUN_REGISTRY_DB, rec.run_id, rec.to_couch_doc())
        first = first or rec
    newest = rec
    _as(ALICE)
    runs = c.get("/runs").json()["runs"]
    assert runs[0]["runId"] == newest.run_id
    assert first.run_id not in {r["runId"] for r in runs}


def test_the_project_survives_a_round_trip_through_the_store(client):
    db, reg, _ = client
    rec = reg.new_record(user_id="asf:alice", agent_name="asvs", project="whimsy")
    reg.mark_complete(rec, status="success")
    stored = rr.RunRecord.from_couch_doc(db.get(rr.RUN_REGISTRY_DB, rec.run_id))
    assert stored.project == "whimsy" and stored.to_summary()["project"] == "whimsy"


# --- data store --------------------------------------------------------------

WRITER = (
    "async def run(input_dict, tools):\n"
    "    data_store.set('k', {'v': 1})\n"
    "    return {'outputText': 'ok'}\n"
)


def test_a_project_run_writes_the_projects_records(client):
    db, _, c = client
    asyncio.run(_execute_agent_code(code=WRITER, input_dict={}, tools={}, gofannon_agents=[],
                                    db=db, user_id="asf:alice", project="whimsy"))
    store = get_data_store_service(db)
    assert store.get("project:whimsy", "default", "k") is not None
    assert store.get("asf:alice", "default", "k") is None


def test_a_run_for_no_project_writes_its_starters_records(client):
    db, _, _ = client
    asyncio.run(_execute_agent_code(code=WRITER, input_dict={}, tools={}, gofannon_agents=[],
                                    db=db, user_id="asf:alice"))
    assert get_data_store_service(db).get("asf:alice", "default", "k") is not None


def test_members_read_project_records_and_the_pmc_changes_them(client):
    db, _, c = client
    get_data_store_service(db).set("project:whimsy", "reports", "r1", "# report")
    url = "/data-store/namespaces/reports/records/r1?project=whimsy"

    _as(ALICE)
    assert c.get(url).status_code == 200
    assert c.get("/data-store/namespaces?project=whimsy").status_code == 200
    assert c.put(url, json={"value": "edited"}).status_code == 403
    assert c.delete(url).status_code == 403
    assert c.delete("/data-store/namespaces/reports?project=whimsy").status_code == 403
    _as(CAROL)
    assert c.get(url).status_code == 403
    _as(PMC)
    assert c.put(url, json={"value": "edited"}).status_code == 200
    _as(ALICE)
    assert c.get("/data-store/namespaces/reports/records/r1").status_code == 404   # her own, not the project's
