"""A streamed run's events land in its registry record.

/agents/run-code/stream registers a run so the page can stop it and, after
a dropped connection or a refresh, reattach through /runs/{id}/stream,
which replays the record's trace. The run used to emit into a separate
trace, so the record stayed empty and a reattached page showed nothing.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db
from routes import get_current_user
from services import run_registry as rr
from services.database_service.memory import MemoryDBService

ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}

CODE = '''
async def run(input_dict, tools):
    for i in range(3):
        print(f"step {i}")
    return {"outputText": "done"}
'''


@pytest.fixture
def env(monkeypatch):
    db = MemoryDBService()
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return reg, TestClient(app)


def test_the_record_holds_the_streamed_events(env):
    reg, c = env
    r = c.post("/agents/run-code/stream", json={"code": CODE, "inputDict": {}, "tools": {}})
    assert r.status_code == 200, r.text
    run_id = None
    for line in r.text.splitlines():
        if line.startswith("data:") and "runId" in line:
            run_id = json.loads(line[5:])["runId"]
            break
    assert run_id
    rec = reg.get(run_id)
    printed = [e.get("message") for e in rec.trace.events if e.get("type") == "stdout"]
    assert printed == ["step 0", "step 1", "step 2"]
    assert rec.trace.run_id == run_id


def test_the_record_keeps_tokens_out_of_its_inputs(env):
    reg, c = env
    r = c.post("/agents/run-code/stream", json={"code": CODE, "tools": {},
                                                "inputDict": {"sourceToken": "github_pat_11" + "A" * 20 + "_" + "b" * 59}})
    run_id = next(json.loads(l[5:])["runId"] for l in r.text.splitlines() if l.startswith("data:") and "runId" in l)
    assert "github_pat_11AAAA" not in json.dumps(reg.get(run_id).input_dict)


def test_a_checkpoint_follows_a_trace_past_the_cap(monkeypatch):
    from services import agent_trace
    monkeypatch.setattr(agent_trace, "MAX_EVENTS_PER_TRACE", 5)
    monkeypatch.setattr(agent_trace, "HEAD_EVENTS_KEPT", 2)
    db = MemoryDBService()
    reg = rr.RunRegistry(db=db)
    rec = reg.new_record(user_id="asf:root", agent_name="long")
    for i in range(10):
        rec.trace.stdout(f"line {i}")
    reg.checkpoint_events()
    for i in range(10, 20):
        rec.trace.stdout(f"line {i}")
    reg.checkpoint_events()
    stored = db.get(rr.RUN_REGISTRY_DB, rec.run_id)["events"]
    assert stored[-1]["message"] == "line 19"
