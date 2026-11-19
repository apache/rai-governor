"""An agent other agents call (their gofannonAgents list it).

Deleting it is refused while they call it, naming them; exporting a
pipeline whose agent is missing names the agent that calls it.
"""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService

CODE = "async def run(input_dict, tools):\n    return {'outputText': 'ok'}\n"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"uid": "asf:akm", "isSiteAdmin": True}
    app.dependency_overrides[get_user_service_dep] = lambda: None
    c = TestClient(app)
    c.db = db
    return c


def _agent(client, aid, name, calls=()):
    client.db.save("agents", aid, {"_id": aid, "name": name, "description": name,
                                   "code": CODE, "gofannonAgents": list(calls)})


def test_an_agent_others_call_is_not_deleted(client):
    _agent(client, "step", "demo_step")
    _agent(client, "main", "demo_main", calls=["step"])
    _agent(client, "other", "other_main", calls=["step"])
    r = client.delete("/agents/step")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "demo_main (main)" in detail and "other_main (other)" in detail
    assert client.db.get("agents", "step")["name"] == "demo_step"

    # Once nothing calls it, it goes; the caller first.
    assert client.delete("/agents/main").status_code == 204
    assert client.delete("/agents/other").status_code == 204
    assert client.delete("/agents/step").status_code == 204
    assert client.db.list_all("agents") == []


def test_an_agent_that_calls_itself_may_be_deleted(client):
    _agent(client, "loop", "loop", calls=["loop"])
    assert client.delete("/agents/loop").status_code == 204


def test_exporting_a_pipeline_names_a_missing_agent_and_its_caller(client):
    # Saved before deleting was refused, or written straight to the store.
    _agent(client, "main", "demo_main", calls=["gone"])
    r = client.get("/agents/main/export")
    assert r.status_code == 404
    assert r.json()["detail"] == "agent gone not found: demo_main (main) calls it"
    assert client.get("/agents/nothing/export").json()["detail"] == "agent nothing not found"
