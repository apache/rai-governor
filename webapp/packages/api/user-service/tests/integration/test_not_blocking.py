"""The database client blocks. Routes and the per-request session lookup
must not run it on the event loop, or one slow read holds up every other
request: the home page's agent list made 64 of them in a row."""

import asyncio
import time
from datetime import timedelta

import httpx
import pytest

import services.database_service as dbs
from app_factory import create_app
from config import settings
from dependencies import get_db
from models.session import Session
from services.session_service import session_doc_id
from routes import get_agent_exec_user, get_author_user, get_current_user
from services.database_service.memory import MemoryDBService
from time_utils import naive_utc_now

SLOW = 0.3


class SlowDB(MemoryDBService):
    def get(self, *a, **k):
        time.sleep(SLOW)
        return super().get(*a, **k)

    def list_all(self, *a, **k):
        time.sleep(SLOW)
        return super().list_all(*a, **k)

    def save(self, *a, **k):
        time.sleep(SLOW)
        return super().save(*a, **k)

    def delete(self, *a, **k):
        time.sleep(SLOW)
        return super().delete(*a, **k)


ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}


@pytest.fixture
def db(monkeypatch):
    d = SlowDB()
    for i in range(4):
        MemoryDBService.save(d, "agents", f"a{i}", {"_id": f"a{i}", "name": f"n{i}", "description": "d",
                                                    "code": "", "friendlyName": f"f{i}",
                                                    "inputSchema": {"q": "string"}, "outputSchema": {"a": "string"}})
        MemoryDBService.save(d, "deployments", f"f{i}", {"_id": f"f{i}", "agentId": f"a{i}"})
    monkeypatch.setattr(dbs, "get_database_service", lambda *_a, **_k: d)
    return d


async def _together(app, requests):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        t = time.perf_counter()
        out = await asyncio.gather(*(c.request(m, u, cookies=ck) for m, u, ck in requests))
        return time.perf_counter() - t, out


async def test_slow_reads_in_routes_overlap(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    app.dependency_overrides[get_author_user] = lambda: ADMIN
    took, out = await _together(app, [("GET", f"/agents/a{i}/deployment", None) for i in range(4)])
    assert [r.json() for r in out] == [{"is_deployed": True, "friendly_name": f"f{i}"} for i in range(4)]
    # Two reads each: 4 x 2 x 0.3 = 2.4 s in a row; side by side about 0.6 s.
    assert took < 4 * 2 * SLOW / 2, took


async def test_listing_deployments_reads_the_agents_once(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_author_user] = lambda: ADMIN
    took, (r,) = await _together(app, [("GET", "/deployments", None)])
    assert sorted(d["friendlyName"] for d in r.json()) == ["f0", "f1", "f2", "f3"]
    # Deployments, then agents: two reads, not one per deployment.
    assert took < 3 * SLOW, took


async def test_session_lookups_overlap(db, monkeypatch):
    for i in range(4):
        s = Session(id=session_doc_id(f"s{i}"), user_uid=f"asf:u{i}", provider_type="asf", display_name="u", email=None,
                    workspaces=[], is_site_admin=False, expires_at=naive_utc_now() + timedelta(hours=1))
        MemoryDBService.save(db, "user_sessions", session_doc_id(f"s{i}"), s.model_dump(by_alias=True, mode="json"))
    app = create_app()
    from routes_auth import router as auth_router
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = lambda: db
    took, out = await _together(app, [("GET", "/auth/me", {"gofannon_sid": f"s{i}"}) for i in range(4)])
    assert [r.json()["uid"] for r in out] == [f"asf:u{i}" for i in range(4)]
    # Each request reads its session twice (middleware, then /auth/me):
    # 2.4 s in a row.
    assert took < 4 * 2 * SLOW / 2, took


def _app(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    for dep in (get_current_user, get_author_user, get_agent_exec_user):
        app.dependency_overrides[dep] = lambda: ADMIN
    return app


async def test_chain_view_deploy_and_undeploy_overlap(db):
    """These awaited helpers that read the database on the event loop."""
    app = _app(db)
    took, out = await _together(app, [("GET", f"/agents/a{i}/chain", None) for i in range(4)])
    assert [r.json()["root"] for r in out] == [f"a{i}" for i in range(4)]
    assert took < 4 * SLOW / 2, took                      # one read each

    took, out = await _together(app, [("POST", f"/agents/a{i}/deploy", None) for i in range(4)])
    assert all(r.json()["message"] == "Agent is already deployed" for r in out)
    assert took < 4 * 2 * SLOW / 2, took                  # agent, deployment

    took, out = await _together(app, [("DELETE", f"/agents/a{i}/undeploy", None) for i in range(4)])
    assert [r.status_code for r in out] == [204] * 4
    assert took < 4 * 2 * SLOW / 2, took                  # find, delete


TICK = 0.01


async def _stalled(work):
    """Run ``work`` while ticking the event loop; how long, in all, the
    ticks came late. Each database call on the loop stalls it for SLOW.
    A total rather than the longest stall: a busy machine can pause the
    process once for about as long."""
    stalls, done = [0.0], asyncio.Event()

    async def tick():
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            stalls[0] += max(0.0, now - last - TICK)
            last = now

    ticker = asyncio.create_task(tick())
    try:
        out = await work
    finally:
        done.set()
        await ticker
    return stalls[0], out


async def test_starting_and_finishing_runs_leave_the_loop_free(db, monkeypatch):
    """Starting a deployed agent read it and wrote the run record on the
    event loop, and so did finishing the run. (Run record writes still go
    one at a time: the registry holds a lock around each.)"""
    from services import run_registry as rr
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    app = _app(db)

    async def work():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            out = await asyncio.gather(*(c.post(f"/rest/f{i}/start", json={}) for i in range(4)))
        runs = [r.json()["runId"] for r in out]
        await asyncio.gather(*(reg.get(r).task for r in runs))
        return runs

    stall, runs = await _stalled(work())
    assert all(reg.get(r).status == "error" for r in runs)   # empty code: no run()
    # On the loop: deployment, agent, the record's read and write, then
    # the same two at the end: 4 runs x 6 x 0.3 = 7.2 s.
    assert stall < 2 * SLOW, stall


async def test_logout_and_view_as_leave_the_loop_free(db):
    for i in range(4):
        s = Session(id=session_doc_id(f"s{i}"), user_uid=f"asf:u{i}", provider_type="asf", display_name="u", email=None,
                    workspaces=[], is_site_admin=False, expires_at=naive_utc_now() + timedelta(hours=1))
        MemoryDBService.save(db, "user_sessions", session_doc_id(f"s{i}"), s.model_dump(by_alias=True, mode="json"))
    app = create_app()
    from routes_auth import router as auth_router
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = lambda: db

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        async def work():
            return await asyncio.gather(
                *(c.post("/auth/view-as", json={"on": False}, cookies={"gofannon_sid": f"s{i}"}) for i in range(2)),
                *(c.post("/auth/logout", cookies={"gofannon_sid": f"s{i}"}) for i in range(2, 4)))

        await c.post("/auth/logout")        # the app's first request sets things up
        stall, out = await _stalled(work())
    assert [r.status_code for r in out] == [200] * 2 + [204] * 2
    # The session write or delete each made on the loop: 4 x 0.3 = 1.2 s.
    assert stall < 2 * SLOW, stall


async def test_memory_run_pages_overlap(db, monkeypatch):
    """The memory routes looked up each run's summary on the event loop."""
    import dependencies
    from services import run_registry as rr
    from services.memory import blob_store
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    app = _app(db)
    took, out = await _together(app, [("GET", f"/memory/by-run/r{i}", None) for i in range(4)])
    assert [r.json()["run"] for r in out] == [{"runId": f"r{i}", "visible": False} for i in range(4)]
    # The run's entries (threaded already), then the run itself.
    assert took < 4 * 2 * SLOW / 2, took
