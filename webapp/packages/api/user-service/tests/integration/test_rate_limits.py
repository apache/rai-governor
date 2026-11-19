"""Which routes are rate limited, on what key, and that the defaults leave
a person using the UI alone."""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services import rate_limit as rl
from services.database_service.memory import MemoryDBService
from services.memory import blob_store
from services.rate_limit import Limit

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
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    monkeypatch.setattr(rl, "ENABLED", True)
    monkeypatch.setattr(rl, "PROXY_HOPS", 2)
    app = create_app()
    db = MemoryDBService()
    import services.database_service as database_service
    monkeypatch.setattr(database_service, "_db_instance", db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_service_dep] = lambda: None
    c = TestClient(app, follow_redirects=False)
    c.user = {"uid": "asf:a", "is_site_admin": True}
    app.dependency_overrides[get_current_user] = lambda: c.user
    return c


def _only(monkeypatch, name, count=1):
    """Every limit high but ``name``, which allows ``count``."""
    for n in rl.LIMITS:
        monkeypatch.setitem(rl.LIMITS, n, Limit(count if n == name else 10_000, 3600))


NS = "/data-store/namespaces/ns"
LIMITED = [
    ("login", "get", "/auth/login/dev_stub"),
    ("login", "get", "/auth/callback/dev_stub?code=alice&state=x"),
    ("client_log", "post", "/log/client"),
    ("run_start", "post", "/agents/run-code"),
    ("run_start", "post", "/agents/run-code/stream"),
    ("run_start", "post", "/agents/run-code/start"),
    ("run_start", "post", "/rest/some-agent"),
    ("run_start", "post", "/rest/some-agent/start"),
    ("run_start", "post", "/workloads/w/run"),
    ("memory_search", "get", "/memory/dom-scan/search"),
    ("memory_recall", "post", "/memory/dom-scan/recall"),
    ("memory_write", "post", "/memory/dom-scan/entries"),
    ("memory_write", "post", "/memory/entries/e/review"),
    ("memory_write", "post", "/memory/reviews/batch"),
    ("memory_write", "post", "/memory/reviews/batch/b/undo"),
    ("memory_write", "post", "/memory/entries/e/disposition"),
    ("memory_write", "post", "/memory/dispositions/batch"),
    ("memory_write", "post", "/memory/proposals/accept"),
    ("memory_write", "post", "/memory/proposals/reject"),
    ("memory_write", "post", "/memory/digests/e/accept"),
    ("data_store_write", "put", NS + "/records/k"),
    ("data_store_write", "delete", NS + "/records/k"),
    ("data_store_write", "delete", NS),
    ("pipeline_import", "post", "/pipelines/import/github"),
    ("pipeline_import", "post", "/pipelines/import/upload"),
]


def _call(c, method, path, **kw):
    if method in ("post", "put"):
        kw.setdefault("json", {})
    return getattr(c, method)(path, **kw)


@pytest.mark.parametrize("name,method,path", LIMITED)
def test_route_is_limited_with_retry_after(client, monkeypatch, name, method, path):
    _only(monkeypatch, name)
    assert _call(client, method, path).status_code != 429
    r = _call(client, method, path)
    assert r.status_code == 429, r.text
    assert 0 < int(r.headers["Retry-After"]) <= 3600


def test_users_are_counted_apart(client, monkeypatch):
    _only(monkeypatch, "data_store_write", 2)
    for _ in range(2):
        assert client.put(NS + "/records/k", json={"value": "v"}).status_code == 200
    assert client.put(NS + "/records/k", json={"value": "v"}).status_code == 429
    client.user = {"uid": "asf:b", "is_site_admin": True}
    assert client.put(NS + "/records/k", json={"value": "v"}).status_code == 200


def _xff(*hops):
    return {"X-Forwarded-For": ", ".join(hops)}


def test_login_is_per_client_ip_and_a_forged_x_forwarded_for_does_not_escape(client, monkeypatch):
    _only(monkeypatch, "login", 2)
    get = lambda h: client.get("/auth/login/dev_stub", headers=h).status_code
    assert [get(_xff("1.1.1.1", "10.0.0.1")) for _ in range(2)] == [302, 302]
    assert get(_xff("1.1.1.1", "10.0.0.1")) == 429
    # What the client puts left of Apache's entry is ignored.
    assert get(_xff("7.7.7.7", "1.1.1.1", "10.0.0.1")) == 429
    assert get(_xff("2.2.2.2", "10.0.0.1")) == 302


def test_client_logs_are_per_session_user_else_per_ip(client, monkeypatch):
    _only(monkeypatch, "client_log", 2)
    event = {"eventType": "e", "message": "m"}
    # Signed in through dev_stub: the observability middleware finds the session.
    start = client.get("/auth/login/dev_stub")
    state = start.cookies.get("gofannon_auth_state")
    client.cookies.clear()
    done = client.get("/auth/callback/dev_stub", params={"code": "alice", "state": state},
                      cookies={"gofannon_auth_state": state})
    sid = done.cookies.get("gofannon_sid")
    assert sid
    client.cookies.clear()
    anon = [client.post("/log/client", json=event).status_code for _ in range(3)]
    assert anon == [202, 202, 429]
    signed_in = [client.post("/log/client", json=event, cookies={"gofannon_sid": sid}).status_code
                 for _ in range(3)]
    assert signed_in == [202, 202, 429]


def test_rate_limit_enabled_false_turns_it_off(client, monkeypatch):
    _only(monkeypatch, "client_log", 1)
    monkeypatch.setattr(rl, "ENABLED", False)
    codes = {client.post("/log/client", json={"eventType": "e", "message": "m"}).status_code
             for _ in range(5)}
    assert codes == {202}


def test_the_defaults_leave_a_busy_ui_session_alone(client):
    """More than a person does in a busy hour, at the default limits: a
    couple of sign-ins, a burst of client errors, a triage session, browsing
    and asking memory, editing data-store records, iterating on an agent."""
    rl.limiter.clear()
    burst = (
        [("get", "/auth/login/dev_stub")] * 6 + [("get", "/auth/callback/dev_stub?code=alice&state=x")] * 6
        + [("post", "/log/client")] * 60
        + [("get", "/memory/dom-scan/search")] * 60
        + [("post", "/memory/dom-scan/recall")] * 10
        + [("post", "/memory/entries/e/disposition")] * 40
        + [("put", NS + "/records/k")] * 40
        + [("post", "/agents/run-code")] * 30
        + [("post", "/workloads/w/run")] * 10
        + [("post", "/pipelines/import/github")] * 20
    )
    refused = [p for m, p in burst if _call(client, m, p).status_code == 429]
    assert refused == []
