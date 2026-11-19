"""Workload runs over RUN_MAX_CONCURRENT queue, show their place, and start
when a slot frees. With the limits unset a run starts as before. A detached
run that is going stops on POST /runs/{id}/stop."""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

import routes
from app_factory import create_app
from config import settings
from dependencies import get_db
from routes import get_current_user
from services import run_admission as ra
from services import run_registry as rr
from services.database_service.memory import MemoryDBService

ALICE = {"uid": "asf:alice", "workspaces": [
    {"workspaceId": "project:whimsy", "role": "admin", "displayName": "whimsy", "source": "ldap_committer"}]}

WORKLOAD = {
    "name": "Scan", "description": "d", "deployment": "scan", "projects": ["whimsy"],
    "inputs": [{"name": "commit", "label": "Commit", "default": "latest"}],
}


# Who the requests are from.
WHO = {}


@pytest.fixture
def env(monkeypatch):
    db = MemoryDBService()
    db.save("agents", "a1", {"_id": "a1", "name": "scan", "description": "d",
                             "code": "", "tools": {}, "gofannon_agents": []})
    db.save("deployments", "scan", {"_id": "scan", "agentId": "a1"})
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "site_admins")
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 0)
    monkeypatch.setattr(ra, "RUN_MAX_PER_USER", 0)
    monkeypatch.setattr(ra, "RUN_QUEUE_POLL_SECONDS", 0.02)
    ra.reset_for_tests()

    # Each run holds until the test lets it finish.
    release = {}

    async def fake_execute(**kw):
        gate = release.setdefault(kw["input_dict"]["commit"], threading.Event())
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return {"done": kw["input_dict"]["commit"]}, []

    monkeypatch.setattr(routes, "_execute_agent_code", fake_execute)
    user = WHO
    user["u"] = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user["u"]
    with TestClient(app) as c:
        wid = c.post("/workloads/admin", json=WORKLOAD).json()["id"]
        user["u"] = ALICE
        assert c.post(f"/workloads/{wid}/add", json={"project": "whimsy"}).status_code == 201

        def run(commit):
            release.setdefault(commit, threading.Event())
            r = c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"commit": commit}})
            assert r.status_code == 202, r.text
            return r.json()

        yield c, reg, run, release


def _wait(c, run_id, status):
    for _ in range(200):
        got = c.get(f"/runs/{run_id}").json()
        if got["status"] == status:
            return got
        time.sleep(0.02)
    raise AssertionError(f"{run_id} never reached {status}: {got['status']}")


def test_limits_unset_start_every_run_at_once(env):
    c, _, run, release = env
    outs = [run(f"c{i}") for i in range(3)]
    assert [o["status"] for o in outs] == ["running"] * 3
    got = c.get(f"/runs/{outs[0]['runId']}").json()
    assert "queueAhead" not in got and "queuedAt" not in got
    for e in release.values():
        e.set()
    for o in outs:
        _wait(c, o["runId"], "success")


def test_a_run_over_the_limit_waits_its_turn(env, monkeypatch):
    c, _, run, release = env
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    first = run("one")
    second = run("two")
    third = run("three")
    assert (first["status"], second["status"], third["status"]) == ("running", "queued", "queued")
    assert c.get(f"/runs/{second['runId']}").json()["queueAhead"] == 0
    listed = {r["runId"]: r for r in c.get("/runs").json()["runs"]}
    assert listed[third["runId"]]["queueAhead"] == 1

    # Stopping the second takes it off the queue; the third moves up.
    assert c.post(f"/runs/{second['runId']}/stop").status_code == 202
    assert _wait(c, second["runId"], "stopped")["error"] == ra.STOPPED_WHILE_QUEUED
    assert c.get(f"/runs/{third['runId']}").json()["queueAhead"] == 0

    release["one"].set()
    _wait(c, first["runId"], "success")
    _wait(c, third["runId"], "running")
    release["three"].set()
    assert _wait(c, third["runId"], "success")


def test_stop_ends_a_running_workload_run(env):
    c, _, run, release = env
    out = run("held")
    _wait(c, out["runId"], "running")
    assert c.post(f"/runs/{out['runId']}/stop").status_code == 202
    got = _wait(c, out["runId"], "stopped")
    assert "AgentStopped" in got["error"]
    assert not release["held"].is_set()   # it ended without being let go


def test_stop_ends_a_running_run_code_start_run(env):
    c, _, _, release = env
    WHO["u"] = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}
    release["sandbox"] = threading.Event()
    r = c.post("/agents/run-code/start", json={
        "code": "x", "inputDict": {"commit": "sandbox"}, "tools": {}, "gofannonAgents": []})
    assert r.status_code == 202, r.text
    run_id = r.json()["runId"]
    _wait(c, run_id, "running")
    assert c.post(f"/runs/{run_id}/stop").status_code == 202
    assert "AgentStopped" in _wait(c, run_id, "stopped")["error"]


def test_admission_runs_off_the_event_loop(env, monkeypatch):
    # Admitting a queued run reads and writes run records in CouchDB; on the
    # event loop that would stall every request on the worker.
    c, reg, run, release = env
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    on_loop = []
    real = ra.admit

    def admit(registry, record):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(registry, record)

    monkeypatch.setattr(ra, "admit", admit)
    first = run("a")["runId"]
    _wait(c, first, "running")
    release["a"].set()
    _wait(c, first, "success")
    assert on_loop == [False]
