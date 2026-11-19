"""Ending all of a user's sessions: the user signing out everywhere, and a
site admin ending someone's sessions."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import services.database_service as dbs
from app_factory import create_app
from dependencies import get_db
from models.session import Session
from services.database_service.memory import MemoryDBService
from services.session_service import session_doc_id
from time_utils import naive_utc_now


def _session(db, sid, uid, site_admin=False):
    s = Session(
        id=session_doc_id(sid), user_uid=uid, provider_type="asf", display_name=uid, email=None,
        is_site_admin=site_admin, expires_at=naive_utc_now() + timedelta(hours=1),
    )
    db.save("user_sessions", session_doc_id(sid), s.model_dump(by_alias=True, mode="json"))


class _Events:
    def __init__(self):
        self.logged = []

    def log(self, **kw):
        self.logged.append(kw)

    def of(self, event_type):
        return [e for e in self.logged if e["event_type"] == event_type]


@pytest.fixture
def env(monkeypatch):
    db = MemoryDBService()
    monkeypatch.setattr(dbs, "get_database_service", lambda *_a, **_k: db)
    app = create_app()
    from routes_auth import router as auth_router
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = lambda: db
    import routes_auth
    events = _Events()
    monkeypatch.setattr(routes_auth, "get_observability_service", lambda: events)
    # alice signed in from three browsers, bob from one; akm is a site admin.
    for sid in ("a1", "a2", "a3"):
        _session(db, sid, "asf:alice")
    _session(db, "b1", "asf:bob")
    _session(db, "s-akm", "asf:akm", site_admin=True)
    return db, TestClient(app), events


def _live(db):
    return sorted(d["userUid"] for d in db.list_all("user_sessions"))


def _me(c, sid):
    return c.get("/auth/me", cookies={"gofannon_sid": sid}).status_code


def test_signing_out_everywhere_ends_every_session_of_the_user_only(env):
    db, c, events = env
    r = c.post("/auth/logout-everywhere", cookies={"gofannon_sid": "a2"})
    assert r.status_code == 204
    # The cookie is cleared as logout clears it.
    assert 'gofannon_sid=""' in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert r.headers["Clear-Site-Data"] == '"cookies", "storage", "cache"'
    assert [_me(c, s) for s in ("a1", "a2", "a3")] == [401, 401, 401]
    assert _me(c, "b1") == 200
    assert _live(db) == ["asf:akm", "asf:bob"]
    [e] = events.of("auth_sessions_ended")
    assert e["user_id"] == "asf:alice"
    assert e["metadata"] == {"provider": "asf", "target_uid": "asf:alice", "count": 3, "by": "self"}


def test_signing_out_everywhere_needs_a_session(env):
    db, c, events = env
    assert c.post("/auth/logout-everywhere").status_code == 401
    assert c.post("/auth/logout-everywhere", cookies={"gofannon_sid": "nope"}).status_code == 401
    assert len(_live(db)) == 5 and events.of("auth_sessions_ended") == []


def test_a_site_admin_ends_all_sessions_of_a_user(env):
    db, c, events = env
    r = c.post("/auth/admin/users/asf:alice/end-sessions", cookies={"gofannon_sid": "s-akm"})
    assert r.status_code == 200
    assert r.json() == {"uid": "asf:alice", "ended": 3}
    assert _live(db) == ["asf:akm", "asf:bob"]
    assert _me(c, "s-akm") == 200
    [e] = events.of("auth_sessions_ended")
    assert e["user_id"] == "asf:akm"
    assert e["metadata"] == {"provider": "asf", "target_uid": "asf:alice", "count": 3, "by": "site_admin"}


def test_a_user_with_no_sessions_ends_none(env):
    _, c, _ = env
    r = c.post("/auth/admin/users/asf:carol/end-sessions", cookies={"gofannon_sid": "s-akm"})
    assert r.json() == {"uid": "asf:carol", "ended": 0}


def test_anyone_else_is_refused(env):
    db, c, events = env
    r = c.post("/auth/admin/users/asf:alice/end-sessions", cookies={"gofannon_sid": "b1"})
    assert r.status_code == 403
    assert c.post("/auth/admin/users/asf:alice/end-sessions").status_code == 401
    # A site admin viewing as a regular user is a regular user here.
    assert c.post("/auth/view-as", json={"on": True}, cookies={"gofannon_sid": "s-akm"}).status_code == 200
    r = c.post("/auth/admin/users/asf:alice/end-sessions", cookies={"gofannon_sid": "s-akm"})
    assert r.status_code == 403
    assert len(_live(db)) == 5 and events.of("auth_sessions_ended") == []
