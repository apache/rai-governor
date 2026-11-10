"""Agents, pipelines, deployments and demo apps are for site admins.

Committers will use the workloads page; they don't see agent code or its
env vars, and can't change what an admin published. Before this, any
signed-in user could list, read, edit, delete, deploy or undeploy any agent
or demo app, and import over any pipeline copy.

The routes are taken from the app itself, so a new route under one of
these prefixes without the gate fails here.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_async_db, get_db
from routes import get_current_user
from services.database_service.async_shim import AsyncDatabaseService
from services.database_service.memory import MemoryDBService

PREFIXES = ("/agents", "/deployments", "/demos", "/pipelines", "/mcp", "/specs")
COMMITTER = {"uid": "asf:alice", "is_site_admin": False}
ADMIN = {"uid": "asf:root", "is_site_admin": True}


def _client(user):
    db = MemoryDBService()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_async_db] = lambda: AsyncDatabaseService(db)
    app.dependency_overrides[get_current_user] = lambda: user
    return app, TestClient(app)


def _authoring_routes(app):
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith(PREFIXES):
            for m in ops:
                out.append((m.upper(), re.sub(r"\{[^}]+\}", "x", path)))
    return out


def test_every_authoring_route_refuses_a_committer(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "site_admins")
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED_UIDS", frozenset())
    app, c = _client(COMMITTER)
    routes = _authoring_routes(app)
    assert len(routes) >= 25, routes          # the prefixes didn't go missing
    open_ = [(m, p) for m, p in routes if c.request(m, p, json={}).status_code != 403]
    assert open_ == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/agents"), ("GET", "/deployments"), ("GET", "/demos"),
    ("GET", "/agents/x"), ("GET", "/agents/x/export"),
])
def test_site_admin_gets_past_the_gate(monkeypatch, method, path):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "site_admins")
    _, c = _client(ADMIN)
    assert c.request(method, path).status_code != 403


def test_all_users_setting_opens_authoring_too(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    _, c = _client(COMMITTER)
    assert c.get("/agents").status_code == 200
