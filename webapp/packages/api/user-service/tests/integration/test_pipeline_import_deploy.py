"""Importing a pipeline deploys every agent it wrote, unless asked not to.

Each agent is deployed at /rest/{its friendly name}, which carries the
import's label. A deployment that can't be made (the name taken by another
agent) is reported in deployProblems, and the import still stands. A dry
run deploys nothing.
"""

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService

MANIFEST = """pipeline: demo
entry: demo_main
agents:
  - name: demo_main
    code: main.py
    calls: [demo_step]
  - name: demo_step
    code: step.py
"""
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


def _upload(client, label="v1", **form):
    files = [("files", ("p/pipeline.yaml", MANIFEST.encode())),
             ("files", ("p/main.py", CODE.encode())),
             ("files", ("p/step.py", CODE.encode()))]
    return client.post("/pipelines/import/upload", files=files,
                       data={"label": label, "dryRun": "false", **form})


def _deployments(client):
    # The in-memory store keeps each document under its id.
    return {k: v["agentId"] for k, v in client.db.dbs.get("deployments", {}).items()}


def test_an_import_deploys_every_agent_it_wrote(client):
    r = _upload(client)
    assert r.status_code == 200, r.text
    out = r.json()
    ids = {name: w["id"] for name, w in out["written"].items()}
    assert _deployments(client) == {"demo_main-v1": ids["demo_main"], "demo_step-v1": ids["demo_step"]}
    assert sorted(d["endpoint"] for d in out["deployed"]) == ["/rest/demo_main-v1", "/rest/demo_step-v1"]
    assert out["deployProblems"] == []

    # The same label again updates the copy; the deployments stay as they are.
    again = _upload(client).json()
    assert again["deployProblems"] == [] and len(again["deployed"]) == 2
    assert len(_deployments(client)) == 2


def test_deploy_false_and_a_dry_run_deploy_nothing(client):
    assert _upload(client, deploy="false").status_code == 200
    assert _deployments(client) == {}
    files = [("files", ("p/pipeline.yaml", MANIFEST.encode())),
             ("files", ("p/main.py", CODE.encode())), ("files", ("p/step.py", CODE.encode()))]
    r = client.post("/pipelines/import/upload", files=files, data={"label": "v2", "dryRun": "true"})
    assert r.status_code == 200 and "deployed" not in r.json()
    assert _deployments(client) == {}


def test_a_name_taken_by_another_agent_is_reported_not_raised(client):
    client.db.save("deployments", "demo_step-v1", {"agentId": "someone-else"})
    r = _upload(client)
    assert r.status_code == 200
    out = r.json()
    assert [d["agent"] for d in out["deployed"]] == ["demo_main"]
    assert out["deployProblems"][0]["agent"] == "demo_step"
    assert "already exists" in out["deployProblems"][0]["reason"]
    assert len(out["written"]) == 2
