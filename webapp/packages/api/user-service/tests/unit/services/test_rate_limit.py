"""The in-process rate limiter: fixed windows per (limit, key), bounded
memory, 429 with Retry-After, and the client IP it keys anonymous callers on."""

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from services import rate_limit as rl
from services.rate_limit import Limit, RateLimiter, parse_limit

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rl, "limiter", RateLimiter(clock=c))
    monkeypatch.setattr(rl, "ENABLED", True)
    monkeypatch.setitem(rl.LIMITS, "t", Limit(3, 60))
    return c


@pytest.mark.parametrize("raw,want", [
    ("30/minute", Limit(30, 60)), ("60/hour", Limit(60, 3600)),
    ("5/second", Limit(5, 1)), (" 2 / days ", Limit(2, 86400)),
])
def test_parse_limit(raw, want):
    assert parse_limit(raw) == want


@pytest.mark.parametrize("raw", ["30", "0/minute", "x/minute", "30/fortnight", "-1/hour", ""])
def test_parse_limit_refuses_nonsense(raw):
    with pytest.raises(ValueError):
        parse_limit(raw)


def test_over_the_limit_is_429_with_retry_after(clock):
    for _ in range(3):
        rl.check("t", "user:a")
    clock.t += 15
    with pytest.raises(HTTPException) as e:
        rl.check("t", "user:a")
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"] == "45"


def test_keys_and_limits_are_counted_apart(clock, monkeypatch):
    monkeypatch.setitem(rl.LIMITS, "u", Limit(3, 60))
    for _ in range(3):
        rl.check("t", "user:a")
    rl.check("t", "user:b")
    rl.check("u", "user:a")
    with pytest.raises(HTTPException):
        rl.check("t", "user:a")


def test_the_window_resets(clock):
    for _ in range(3):
        rl.check("t", "user:a")
    with pytest.raises(HTTPException):
        rl.check("t", "user:a")
    clock.t += 60
    for _ in range(3):
        rl.check("t", "user:a")


def test_disabled_limits_nothing(clock, monkeypatch):
    monkeypatch.setattr(rl, "ENABLED", False)
    for _ in range(100):
        rl.check("t", "user:a")


def test_refusal_is_logged_once_per_window(clock, monkeypatch):
    logged = []
    monkeypatch.setattr(rl, "get_observability_service",
                        lambda: type("L", (), {"log": lambda self, **kw: logged.append(kw)})())
    for _ in range(3):
        rl.check("t", "ip:1.2.3.4")
    for _ in range(5):
        with pytest.raises(HTTPException):
            rl.check("t", "ip:1.2.3.4")
    assert [e["event_type"] for e in logged] == ["rate_limited"]


def test_memory_is_bounded():
    lim = RateLimiter(max_keys=100, clock=Clock())
    for i in range(1000):
        lim.hit("t", f"ip:{i}", Limit(3, 60))
    assert len(lim) == 100
    # The most recently used keys are the ones kept.
    assert lim.hit("t", "ip:999", Limit(1, 60))[0] is False


def _ip_app():
    app = FastAPI()

    @app.get("/ip")
    def ip(request: Request):
        return rl.client_ip(request)
    return TestClient(app)


@pytest.mark.parametrize("xff,want", [
    # Apache appends the browser, nginx appends Apache; the rest is the client's.
    ("6.6.6.6, 1.2.3.4, 172.17.0.1", "1.2.3.4"),
    ("1.2.3.4, 172.17.0.1", "1.2.3.4"),
    # Only nginx in front (docker dev).
    ("1.2.3.4", "1.2.3.4"),
    (None, "testclient"),
])
def test_client_ip_ignores_what_the_client_put_in_x_forwarded_for(monkeypatch, xff, want):
    monkeypatch.setattr(rl, "PROXY_HOPS", 2)
    headers = {"X-Forwarded-For": xff} if xff else {}
    assert _ip_app().get("/ip", headers=headers).json() == want


def test_dependencies_answer_429(clock):
    app = FastAPI()

    def user():
        return {"uid": "asf:a"}

    @app.post("/u", dependencies=[rl.per_user("t", user)])
    def u():
        return "ok"

    @app.post("/i", dependencies=[rl.per_ip("t")])
    def i():
        return "ok"

    c = TestClient(app)
    for path in ("/u", "/i"):
        assert [c.post(path).status_code for _ in range(4)] == [200, 200, 200, 429]
    r = c.post("/u")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
