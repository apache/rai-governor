"""Chat tickets and config sessions answer only their owner; the admin panel
refuses its default password; agent code gets no database handle.

Before this, any signed-in user could read any chat ticket (request and
answer) and read, overwrite or delete any config session by id, and
gofannon_client.db handed agent code every collection.
"""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import _execute_agent_code, get_async_db, get_db, require_admin_access
from routes import get_current_user
from services.database_service.async_shim import AsyncDatabaseService
from services.database_service.memory import MemoryDBService

ALICE = {"uid": "asf:alice", "is_site_admin": False}
BOB = {"uid": "asf:bob", "is_site_admin": False}
ADMIN = {"uid": "asf:root", "is_site_admin": True}


class _Who:
    user = ALICE


def _client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    return TestClient(app)


def _as(user):
    _Who.user = user


def test_ticket_answers_only_its_owner():
    db = MemoryDBService()
    db.save("tickets", "t1", {"status": "completed", "user_id": "asf:alice",
                              "result": {"content": "secret"}})
    c = _client(db)
    _as(ALICE)
    assert c.get("/chat/t1").status_code == 200
    _as(BOB)
    assert c.get("/chat/t1").status_code == 404
    _as(ADMIN)
    assert c.get("/chat/t1").status_code == 404


def test_ticket_without_owner_is_site_admin_only():
    db = MemoryDBService()
    db.save("tickets", "old", {"status": "completed", "result": {"content": "x"}})
    c = _client(db)
    _as(ALICE)
    assert c.get("/chat/old").status_code == 404
    _as(ADMIN)
    assert c.get("/chat/old").status_code == 200


def test_session_config_answers_only_its_owner():
    db = MemoryDBService()
    c = _client(db)
    cfg = {"provider": "openai", "model": "gpt", "parameters": {}}
    _as(ALICE)
    assert c.post("/sessions/s1/config", json=cfg).status_code == 200
    assert c.get("/sessions/s1/config").status_code == 200
    _as(BOB)
    assert c.get("/sessions/s1/config").status_code == 404
    assert c.post("/sessions/s1/config", json={**cfg, "model": "other"}).status_code == 404
    assert c.delete("/sessions/s1").status_code == 404
    _as(ALICE)
    assert c.get("/sessions/s1/config").json()["model"] == "gpt"
    assert c.delete("/sessions/s1").status_code == 200


def test_admin_panel_refuses_default_password(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PANEL_ENABLED", True)
    for pw in ("password", "", "Password", " PASSWORD "):
        monkeypatch.setattr(settings, "ADMIN_PANEL_PASSWORD", pw)
        with pytest.raises(HTTPException) as e:
            require_admin_access(pw)
        assert e.value.status_code == 403
    monkeypatch.setattr(settings, "ADMIN_PANEL_PASSWORD", "long-random-value")
    assert require_admin_access("long-random-value") is None
    for wrong in (None, "", "long-random-valu", "long-random-value "):
        with pytest.raises(HTTPException) as e:
            require_admin_access(wrong)
        assert e.value.status_code == 401


def test_agent_code_gets_no_database_handle():
    db = MemoryDBService()
    code = (
        "async def run(input_dict, tools):\n"
        "    return {'has_db': hasattr(gofannon_client, 'db')}\n"
    )
    result, _ = asyncio.run(_execute_agent_code(
        code=code, input_dict={}, tools={}, gofannon_agents=[], db=db, user_id="asf:alice"))
    assert result == {"has_db": False}
