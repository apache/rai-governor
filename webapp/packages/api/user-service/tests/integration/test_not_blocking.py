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
from routes import get_author_user, get_current_user
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
