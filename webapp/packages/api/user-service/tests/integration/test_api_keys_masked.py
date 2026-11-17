"""A saved LLM key goes back to the browser only masked; the stored key is
untouched and still used."""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.user_service import UserService

KEY = "sk-proj-abcdefghijklmnop1234"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PANEL_ENABLED", True)
    monkeypatch.setattr(settings, "ADMIN_PANEL_PASSWORD", "s3cret-admin")
    app = create_app()
    svc = UserService(MemoryDBService())
    app.dependency_overrides[get_user_service_dep] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: {"uid": "asf:akm"}
    c = TestClient(app)
    c.svc = svc
    return c


def test_keys_are_masked_everywhere_the_api_returns_them(client):
    r = client.put("/users/me/api-keys", json={"provider": "openai", "api_key": KEY})
    assert r.status_code == 200
    responses = [r, client.get("/users/me"), client.get("/users/me/api-keys"),
                 client.get("/admin/users", headers={"X-Admin-Password": "s3cret-admin"})]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert KEY not in resp.text
    assert client.get("/users/me/api-keys").json()["openaiApiKey"] == "****1234"
    assert client.get("/users/me").json()["apiKeys"]["anthropicApiKey"] is None
    # Stored as given, and still the key that is used.
    assert client.svc.get_api_keys("asf:akm").openai_api_key == KEY
    assert client.svc.get_effective_api_key("asf:akm", "openai") == KEY
