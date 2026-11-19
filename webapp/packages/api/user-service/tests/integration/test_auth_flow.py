"""The login flow through the dev_stub provider: where it sends the browser
back to after login, and what it keeps in cookies."""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db
from services.database_service.memory import MemoryDBService

AUTH_CONFIG = {
    "providers": [{"type": "dev_stub", "enabled": True, "config": {"users": [
        {"uid": "alice", "display_name": "Alice", "email": "a@example.org",
         "workspaces": [{"id": "project:demo", "role": "member", "display_name": "Demo"}]}]}}],
    "site_admins": [],
    "session_ttl_hours": 24,
    "workspace_refresh_minutes": 15,
    "legacy_firebase_enabled": False,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local")
    monkeypatch.setattr(settings, "AUTH_CONFIG", AUTH_CONFIG)
    monkeypatch.setenv("FRONTEND_URL", "https://governor.example.org")
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    # The middleware and get_current_user find sessions through the
    # process-wide database, not get_db: make it the same one.
    import services.database_service as database_service
    monkeypatch.setattr(database_service, "_db_instance", db)
    c = TestClient(app, follow_redirects=False)
    c.db = db
    return c


def login(client, return_to=None, cookies=None):
    """Start a login and finish it; the callback's response."""
    params = {"return_to": return_to} if return_to is not None else {}
    start = client.get("/auth/login/dev_stub", params=params)
    assert start.status_code == 302, start.text
    state = start.cookies.get("gofannon_auth_state")
    jar = {"gofannon_auth_state": state}
    if start.cookies.get("gofannon_return_to"):
        jar["gofannon_return_to"] = start.cookies.get("gofannon_return_to")
    jar.update(cookies or {})
    client.cookies.clear()
    return start, client.get("/auth/callback/dev_stub", params={"code": "alice", "state": state}, cookies=jar)


def test_a_path_on_this_site_is_where_login_returns(client):
    _, done = login(client, "/run/abc?tab=1")
    assert done.status_code == 302
    assert done.headers["location"] == "https://governor.example.org/run/abc?tab=1"


@pytest.mark.parametrize("bad", [
    "https://evil.example", "//evil.example", "/\\evil.example", "evil", "/a\nb", "/" + "a" * 2048,
])
def test_anything_else_is_not_kept_and_login_lands_on_the_home_page(client, bad):
    start, done = login(client, bad)
    assert "gofannon_return_to" not in start.cookies
    assert done.headers["location"] == "https://governor.example.org/"


@pytest.mark.parametrize("cookie", ["https://evil.example", "//evil.example", "/\\evil.example"])
def test_a_return_to_cookie_set_by_hand_is_checked_again(client, cookie):
    _, done = login(client, cookies={"gofannon_return_to": cookie})
    assert done.headers["location"] == "https://governor.example.org/"


class _Events:
    def __init__(self):
        self.logged = []

    def log(self, **kw):
        self.logged.append(kw)

    def of(self, event_type):
        return [e for e in self.logged if e["event_type"] == event_type]


@pytest.fixture
def events(monkeypatch):
    import routes_auth
    rec = _Events()
    monkeypatch.setattr(routes_auth, "get_observability_service", lambda: rec)
    return rec


def test_login_and_logout_are_recorded_without_code_state_or_return_to(client, events):
    start, done = login(client, "/secret-path")
    state = start.cookies.get("gofannon_auth_state")
    [ok] = events.of("auth_success")
    assert ok["user_id"] == "dev_stub:alice"
    assert ok["metadata"] == {"provider": "dev_stub"}
    sid = done.cookies.get("gofannon_sid")
    assert client.post("/auth/logout", cookies={"gofannon_sid": sid}).status_code == 204
    [out] = events.of("auth_logout")
    assert out["user_id"] == "dev_stub:alice"
    text = repr(events.logged)
    for secret in (state, "alice&", "/secret-path", sid):
        assert secret not in text


def test_a_refused_login_is_recorded(client, events):
    start = client.get("/auth/login/dev_stub")
    r = client.get("/auth/callback/dev_stub", params={"code": "alice", "state": "wrong"},
                   cookies={"gofannon_auth_state": start.cookies.get("gofannon_auth_state")})
    assert r.status_code == 400
    [bad] = events.of("auth_failure")
    assert bad["metadata"] == {"provider": "dev_stub", "reason": "state mismatch"}
    assert bad["level"] == "WARNING"


# --- Memberships are re-read during a session (FINDING-018) ---------------

from datetime import timedelta  # noqa: E402

from auth.base import Membership, MembershipRefresh  # noqa: E402
from services.session_service import session_doc_id  # noqa: E402
from time_utils import naive_utc_now  # noqa: E402


def _age_session(client, sid, minutes=60):
    doc = client.db.get("user_sessions", session_doc_id(sid))
    doc["lastRefreshAt"] = (naive_utc_now() - timedelta(minutes=minutes)).isoformat()
    client.db.save("user_sessions", session_doc_id(sid), doc)


def _me(client, sid):
    return client.get("/auth/me", cookies={"gofannon_sid": sid})


def _workspaces(client, sid):
    client.get("/users/me", cookies={"gofannon_sid": sid})  # any authenticated request
    return [w["workspaceId"] for w in _me(client, sid).json()["workspaces"]]


@pytest.fixture
def session(client):
    _, done = login(client)
    sid = done.cookies.get("gofannon_sid")
    client.cookies.clear()
    return sid


def _provider_returns(monkeypatch, result):
    from auth import get_registry
    provider = get_registry().get("dev_stub")

    async def refresh(user_info):
        return result

    monkeypatch.setattr(provider, "refresh_memberships", refresh)


def test_stale_memberships_are_re_read(client, session, monkeypatch):
    assert "project:demo" in _workspaces(client, session)
    _provider_returns(monkeypatch, MembershipRefresh(memberships=[
        Membership(workspace_id="project:other", role="member", display_name="Other", source="dev_stub")]))
    assert "project:demo" in _workspaces(client, session)  # still fresh: not re-read
    _age_session(client, session)
    ws = _workspaces(client, session)
    assert "project:other" in ws and "project:demo" not in ws


def test_a_failed_lookup_keeps_the_memberships(client, session, monkeypatch):
    _provider_returns(monkeypatch, None)
    _age_session(client, session)
    assert "project:demo" in _workspaces(client, session)
    doc = client.db.get("user_sessions", session_doc_id(session))
    assert naive_utc_now() - timedelta(minutes=1) < \
        __import__("datetime").datetime.fromisoformat(doc["lastRefreshAt"])  # next try is an interval later


def test_a_user_banned_since_login_is_logged_out(client, session, monkeypatch):
    _provider_returns(monkeypatch, MembershipRefresh(memberships=[], banned=True))
    _age_session(client, session)
    assert client.get("/users/me", cookies={"gofannon_sid": session}).status_code == 401
    assert _me(client, session).status_code == 401


def test_a_provider_that_cannot_refresh_leaves_the_session_alone(client, session):
    from auth.base import AuthProvider
    from auth import get_registry
    provider = get_registry().get("dev_stub")
    # The base class's answer: no background lookup (GitHub, Google, Microsoft).
    assert __import__("asyncio").run(AuthProvider.refresh_memberships(provider, None)) is None


# --- Sessions are stored under a hash of the cookie ------------------------

def test_the_cookie_value_is_not_stored_and_the_stored_id_is_not_a_cookie(client, session):
    stored = client.db.dbs["user_sessions"]
    assert session not in stored and session not in repr(stored)
    assert list(stored) == [session_doc_id(session)]
    assert _me(client, session).status_code == 200
    # What someone reading the database sees doesn't log them in.
    assert _me(client, session_doc_id(session)).status_code == 401


def test_logout_deletes_the_stored_session(client, session):
    assert client.post("/auth/logout", cookies={"gofannon_sid": session}).status_code == 204
    assert client.db.dbs["user_sessions"] == {}


# --- Idle timeout -----------------------------------------------------------

from datetime import datetime  # noqa: E402


def _unused_for(client, sid, hours, refreshed_minutes_ago=None):
    """Last used `hours` ago; last refreshed then too, unless said otherwise."""
    doc = client.db.get("user_sessions", session_doc_id(sid))
    doc["lastUsedAt"] = (naive_utc_now() - timedelta(hours=hours)).isoformat()
    ago = timedelta(minutes=refreshed_minutes_ago) if refreshed_minutes_ago is not None else timedelta(hours=hours)
    doc["lastRefreshAt"] = (naive_utc_now() - ago).isoformat()
    client.db.save("user_sessions", session_doc_id(sid), doc)


def _stored(client, sid):
    return client.db.dbs["user_sessions"].get(session_doc_id(sid))


def test_a_session_unused_past_the_idle_limit_is_refused_and_deleted(client, session):
    _unused_for(client, session, hours=8.5)
    assert client.get("/users/me", cookies={"gofannon_sid": session}).status_code == 401
    assert _stored(client, session) is None


def test_a_session_used_within_the_idle_limit_goes_on(client, session):
    _unused_for(client, session, hours=7.5)
    assert client.get("/users/me", cookies={"gofannon_sid": session}).status_code == 200
    # The refresh that request made recorded the use.
    used = datetime.fromisoformat(_stored(client, session)["lastUsedAt"])
    assert naive_utc_now() - used < timedelta(minutes=1)


def test_use_is_recorded_at_each_refresh_even_when_nothing_changed(client, session):
    # dev_stub returns the same memberships; last use is still written.
    _unused_for(client, session, hours=1, refreshed_minutes_ago=20)
    client.get("/users/me", cookies={"gofannon_sid": session})
    used = datetime.fromisoformat(_stored(client, session)["lastUsedAt"])
    assert naive_utc_now() - used < timedelta(minutes=1)


def test_use_is_not_written_between_refreshes(client, session):
    _unused_for(client, session, hours=1, refreshed_minutes_ago=5)
    before = _stored(client, session)["lastUsedAt"]
    client.get("/users/me", cookies={"gofannon_sid": session})
    assert _stored(client, session)["lastUsedAt"] == before


def test_an_idle_limit_of_zero_turns_it_off(client, session, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_CONFIG", {**AUTH_CONFIG, "session_idle_hours": 0})
    _unused_for(client, session, hours=20)
    assert client.get("/users/me", cookies={"gofannon_sid": session}).status_code == 200


def test_a_session_from_before_last_use_was_recorded_goes_by_its_last_refresh(client, session):
    doc = client.db.get("user_sessions", session_doc_id(session))
    doc.pop("lastUsedAt", None)
    doc["lastRefreshAt"] = (naive_utc_now() - timedelta(hours=9)).isoformat()
    client.db.save("user_sessions", session_doc_id(session), doc)
    assert _me(client, session).status_code == 401


# --- The OAuth redirect_uri ------------------------------------------------

def test_the_callback_url_comes_from_the_request_by_default(client, monkeypatch):
    monkeypatch.delenv("AUTH_CALLBACK_BASE_URL", raising=False)
    r = client.get("/auth/login/dev_stub", headers={"Host": "other.example"})
    assert "redirect_uri=http%3A%2F%2Fother.example%2Fauth%2Fcallback%2Fdev_stub" in r.headers["location"]


def test_a_configured_callback_url_ignores_the_host_header(client, monkeypatch):
    monkeypatch.setenv("AUTH_CALLBACK_BASE_URL", "https://governor.example.org/")
    r = client.get("/auth/login/dev_stub", headers={"Host": "other.example"})
    loc = r.headers["location"]
    assert "redirect_uri=https%3A%2F%2Fgovernor.example.org%2Fauth%2Fcallback%2Fdev_stub" in loc
    assert "other.example" not in loc
