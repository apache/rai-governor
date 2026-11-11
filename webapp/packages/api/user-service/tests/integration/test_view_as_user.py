"""A site admin views the site as a regular user: through a real session
cookie, every route sees no site admin and no uid allowlists, only the
LDAP project roles, until it's switched off."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import services.database_service as dbs
from app_factory import create_app
from config import settings
from dependencies import get_db
from models.session import Session
from services.database_service.memory import MemoryDBService
from services.memory import access
from services.memory.agent_proxy import AgentMemory
from time_utils import naive_utc_now


def _session(db, sid, uid, site_admin, projects=()):
    s = Session(
        id=sid, user_uid=uid, provider_type="asf", display_name=uid, email=None,
        workspaces=[{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
                    for p, r in projects],
        is_site_admin=site_admin, expires_at=naive_utc_now() + timedelta(hours=1),
    )
    db.save("user_sessions", sid, s.model_dump(by_alias=True, mode="json"))


@pytest.fixture
def env(monkeypatch):
    db = MemoryDBService()
    monkeypatch.setattr(dbs, "get_database_service", lambda *_a, **_k: db)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "site_admins")
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED_UIDS", frozenset())
    app = create_app()
    # Mounted only when a login provider is configured; mount it here.
    from routes_auth import router as auth_router
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = lambda: db
    # akm: site admin, a committer on whimsy in LDAP. alice: a committer.
    _session(db, "s-akm", "asf:akm", True, [("whimsy", "member")])
    _session(db, "s-alice", "asf:alice", False, [("whimsy", "member")])
    return db, TestClient(app)


def _me(c, sid):
    return c.get("/auth/me", cookies={"gofannon_sid": sid}).json()


def _view(c, sid, on):
    return c.post("/auth/view-as", json={"on": on}, cookies={"gofannon_sid": sid})


def test_viewing_as_a_regular_user_drops_site_admin_everywhere(env):
    _, c = env
    ck = {"gofannon_sid": "s-akm"}
    me = _me(c, "s-akm")
    assert (me["isSiteAdmin"], me["canAuthor"], me["viewingAsUser"], me["canViewAsUser"]) == (True, True, False, True)
    assert c.get("/workloads/admin", cookies=ck).status_code == 200
    assert c.get("/agents", cookies=ck).status_code == 200

    r = _view(c, "s-akm", True)
    assert r.status_code == 200
    assert (r.json()["isSiteAdmin"], r.json()["canAuthor"], r.json()["viewingAsUser"]) == (False, False, True)
    # It stays on: a later request, a reload, still sees a regular user.
    me = _me(c, "s-akm")
    assert (me["isSiteAdmin"], me["canAuthor"], me["viewingAsUser"], me["canViewAsUser"]) == (False, False, True, True)
    assert c.get("/workloads/admin", cookies=ck).status_code == 403
    assert c.get("/agents", cookies=ck).status_code == 403
    # The LDAP role still counts: a committer on whimsy, not its PMC.
    out = c.get("/workloads", params={"project": "whimsy"}, cookies=ck)
    assert out.status_code == 200 and out.json()["canManage"] is False
    assert c.get("/workloads", params={"project": "tomcat"}, cookies=ck).status_code in (403, 404)
    projects = c.get("/workloads/projects", cookies=ck).json()
    assert projects["isSiteAdmin"] is False and [p["project"] for p in projects["projects"]] == ["whimsy"]

    assert _view(c, "s-akm", False).status_code == 200
    assert _me(c, "s-akm")["isSiteAdmin"] is True
    assert c.get("/workloads/admin", cookies=ck).status_code == 200


def test_uid_allowlists_stop_applying_too(env, monkeypatch):
    _, c = env
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED_UIDS", frozenset({"asf:akm"}))
    _view(c, "s-akm", True)
    assert _me(c, "s-akm")["canAuthor"] is False
    assert c.get("/agents", cookies={"gofannon_sid": "s-akm"}).status_code == 403
    # all_users means everyone, a regular user included.
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    assert _me(c, "s-akm")["canAuthor"] is True


def test_only_a_site_admin_can_switch_it_on(env):
    _, c = env
    assert _view(c, "s-alice", True).status_code == 403
    me = _me(c, "s-alice")
    assert (me["viewingAsUser"], me["canViewAsUser"]) == (False, False)
    assert _view(c, "s-alice", False).status_code == 200
    assert c.post("/auth/view-as", json={"on": True}).status_code == 401


def test_memory_writers_stop_applying(monkeypatch):
    monkeypatch.setenv("MEMORY_WRITERS", "asf:akm")
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    akm = {"uid": "asf:akm", "workspaces": []}
    assert access.can_write(akm, "dom-scan") is True
    assert access.can_write({**akm, "viewing_as_user": True}, "dom-scan") is False
    plain = AgentMemory(None, agent_name="a", user_id="asf:akm")
    viewing = AgentMemory(None, agent_name="a", user_id="asf:akm", viewing_as_user=True)
    assert "dom-scan" in plain._writable and viewing._writable == set()
