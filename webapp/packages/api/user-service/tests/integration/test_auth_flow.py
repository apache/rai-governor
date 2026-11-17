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
