"""POST /log/client takes events from anyone, signed in or not, so it is
bounded and marks them as the client's (FINDING-099)."""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from dependencies import get_logger


class Recorder:
    def __init__(self):
        self.logged = []

    def log(self, **kw):
        self.logged.append(kw)


@pytest.fixture
def client():
    app = create_app()
    rec = Recorder()
    app.dependency_overrides[get_logger] = lambda: rec
    c = TestClient(app)
    c.rec = rec
    return c


def test_an_event_is_logged_as_the_clients(client):
    r = client.post("/log/client", json={"eventType": "user-action", "message": "clicked", "level": "WARN",
                                         "metadata": {"page": "/run/x"}})
    assert r.status_code == 202
    [e] = client.rec.logged
    assert e["event_type"] == "client:user-action"
    assert e["service"] == "webui"


@pytest.mark.parametrize("body", [
    {"eventType": "auth_success", "message": "x", "level": "CRITICAL"},
    {"eventType": "has space", "message": "x"},
    {"eventType": "x" * 65, "message": "x"},
    {"eventType": "e", "message": "x" * 2001},
    {"eventType": "e", "message": "x", "metadata": {"blob": "x" * 9000}},
])
def test_out_of_bounds_events_are_refused(client, body):
    assert client.post("/log/client", json=body).status_code == 422
    assert client.rec.logged == []
