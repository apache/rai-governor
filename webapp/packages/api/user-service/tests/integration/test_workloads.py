"""Workloads (B11): site admins publish deployed agents; project members run them.

A committer can launch a workload for their project even though only site
admins may run agent code (the code is the admin's). They set only the
listed inputs, repositories only from their own project, and never see or
override the admin's fixed inputs, which reach the run but not its record.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from config import settings
from dependencies import get_db
from routes import get_current_user
from services import run_registry as rr
from services.database_service.memory import MemoryDBService
from services.workload_service import FIXED_PLACEHOLDER


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


ALICE = {"uid": "asf:alice", "workspaces": _ws(("whimsy", "member"))}
BOB = {"uid": "asf:bob", "workspaces": _ws(("whimsy", "member"))}
PMC = {"uid": "asf:pmc", "workspaces": _ws(("whimsy", "admin"))}
CAROL = {"uid": "asf:carol", "workspaces": _ws(("tomcat", "member"))}
ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}

CODE = '''
async def run(input_dict, tools):
    print("token is " + input_dict.get("sourceToken", ""))
    if input_dict.get("level") == "L3":
        raise RuntimeError("push failed with " + input_dict["sourceToken"])
    return {"saw": input_dict}
'''

ASVS = {
    "name": "ASVS audit",
    "description": "Audits one repository against OWASP ASVS.",
    "deployment": "asvs_orchestrate",
    "projects": ["whimsy"],
    "inputs": [
        {"name": "sourceRepo", "label": "Repository", "kind": "repository"},
        {"name": "commit", "label": "Commit", "default": "latest"},
        {"name": "level", "label": "ASVS level", "kind": "choice", "choices": ["L1", "L2", "L3"], "default": "L1"},
    ],
    "fixedInputs": {"sourceToken": "github_pat_SECRET", "outputRepo": "apache/tooling-runbooks"},
    "produces": "Reports in apache/tooling-runbooks",
}


class _Who:
    user = ADMIN


def _as(u):
    _Who.user = u


@pytest.fixture
def env(monkeypatch):
    db = MemoryDBService()
    db.save("agents", "a1", {"_id": "a1", "name": "asvs_orchestrate", "description": "d",
                             "code": CODE, "tools": {}, "gofannon_agents": []})
    db.save("deployments", "asvs_orchestrate", {"_id": "asvs_orchestrate", "agentId": "a1"})
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "site_admins")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    _as(ADMIN)
    return db, reg, TestClient(app)


def _publish(c, **over):
    _as(ADMIN)
    r = c.post("/workloads/admin", json={**ASVS, **over})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait(reg, run_id):
    for _ in range(100):
        rec = reg.get(run_id)
        if rec and rec.status != "running":
            return rec
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_only_site_admins_publish(env):
    _, _, c = env
    _as(PMC)
    assert c.post("/workloads/admin", json=ASVS).status_code == 403
    assert c.get("/workloads/admin").status_code == 403
    _as(ADMIN)
    assert c.post("/workloads/admin", json={**ASVS, "deployment": "nope"}).status_code == 400
    bad = {**ASVS, "inputs": ASVS["inputs"] + [{"name": "sourceToken", "label": "x"}]}
    assert c.post("/workloads/admin", json=bad).status_code == 422


def test_members_see_their_projects_workloads_without_fixed_values(env):
    _, _, c = env
    wid = _publish(c)
    _as(ALICE)
    out = c.get("/workloads", params={"project": "whimsy"}).json()
    assert out["role"] == "member" and [w["id"] for w in out["workloads"]] == [wid]
    w = out["workloads"][0]
    assert w["fixedInputNames"] == ["outputRepo", "sourceToken"]
    assert "SECRET" not in str(out) and "deployment" not in w and "fixedInputs" not in w
    assert c.get("/workloads/projects").json()["projects"] == [{"project": "whimsy", "role": "member"}]
    _as(CAROL)
    assert c.get("/workloads", params={"project": "whimsy"}).status_code == 403
    assert c.get("/workloads", params={"project": "tomcat"}).json()["workloads"] == []
    assert c.get(f"/workloads/{wid}", params={"project": "tomcat"}).status_code == 404


def test_a_committer_runs_it_for_their_project(env):
    _, reg, c = env
    wid = _publish(c)
    _as(ALICE)
    r = c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy"}})
    assert r.status_code == 202, r.text
    rec = _wait(reg, r.json()["runId"])
    assert rec.status == "success", rec.error
    assert rec.project == "whimsy" and rec.workload_id == wid and rec.user_id == "asf:alice"
    # The agent got the admin's values; nothing the project sees has them.
    assert rec.result["saw"]["sourceToken"] == FIXED_PLACEHOLDER
    assert rec.result["saw"]["outputRepo"] == "apache/tooling-runbooks"   # not a secret: findable
    assert rec.input_dict["sourceToken"] == FIXED_PLACEHOLDER
    assert rec.input_dict["outputRepo"] == FIXED_PLACEHOLDER                # inputs: names only
    _as(BOB)
    seen = c.get(f"/runs/{rec.run_id}").json()
    assert "SECRET" not in json.dumps(seen)
    assert any("token is (set by a site admin)" in (e.get("message") or "") for e in seen["events"])

    _as(BOB)
    listed = c.get("/runs", params={"project": "whimsy", "workload_id": wid}).json()["runs"]
    assert [x["runId"] for x in listed] == [rec.run_id] and listed[0]["workloadId"] == wid


def test_a_failed_runs_error_keeps_the_token_out(env):
    _, reg, c = env
    wid = _publish(c)
    _as(ALICE)
    r = c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy", "level": "L3"}})
    rec = _wait(reg, r.json()["runId"])
    assert rec.status == "error" and "push failed with (set by a site admin)" in rec.error
    _as(BOB)
    assert "SECRET" not in json.dumps(c.get(f"/runs/{rec.run_id}").json())


@pytest.mark.parametrize("inputs,code", [
    ({"sourceRepo": "apache/tomcat"}, 403),                    # another project's repository
    ({"sourceRepo": "apache/whimsy-private/../x"}, 403),
    ({"sourceRepo": "apache/whimsy", "sourceToken": "mine"}, 400),   # can't override a fixed input
    ({"sourceRepo": "apache/whimsy", "level": "L9"}, 400),
    ({}, 400),                                                 # repository is required
    ({"sourceRepo": "apache/whimsy", "commit": "../../tooling-private/tarball/main"}, 400),  # path tricks
    ({"sourceRepo": "apache/whimsy", "commit": "main\nrepo: apache/other"}, 400),         # config lines
    ({"sourceRepo": "apache/whimsy", "commit": "https://evil.example/x"}, 400),
    ({"sourceRepo": "apache/whimsy", "commit": "release/2.1"}, 202),                      # a real branch
])
def test_inputs_are_checked(env, inputs, code):
    _, _, c = env
    wid = _publish(c)
    _as(ALICE)
    assert c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": inputs}).status_code == code


def test_an_admin_pattern_decides_a_text_input(env):
    _, _, c = env
    inputs = [ASVS["inputs"][0], {"name": "securityModelUrl", "label": "Security model", "required": False,
                                  "pattern": r"https://github\.com/apache/whimsy/blob/[A-Za-z0-9._/-]+\.md"}]
    wid = _publish(c, inputs=inputs)
    _as(ALICE)
    ok = "https://github.com/apache/whimsy/blob/main/SECURITY.md"
    assert c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy", "securityModelUrl": ok}}).status_code == 202
    bad = "https://github.com/apache/other/blob/main/SECURITY.md"
    assert c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy", "securityModelUrl": bad}}).status_code == 400


def test_a_listed_repository_needs_no_naming_rule(env):
    _, reg, c = env
    wid = _publish(c, repositories={"whimsy": ["apache/infrastructure-whimsy"]})
    _as(ALICE)
    r = c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {}})
    assert r.status_code == 202, r.text
    assert _wait(reg, r.json()["runId"]).input_dict["sourceRepo"] == "apache/infrastructure-whimsy"


def test_reserved_names_get_usable_ids(env):
    _, _, c = env
    wid = _publish(c, name="Admin")
    assert wid != "admin"
    _as(ALICE)
    assert c.get(f"/workloads/{wid}", params={"project": "whimsy"}).status_code == 200


def test_listed_repositories_are_the_only_choices(env):
    _, _, c = env
    wid = _publish(c, repositories={"whimsy": ["apache/whimsy", "apache/whimsy-private"]})
    _as(ALICE)
    w = c.get(f"/workloads/{wid}", params={"project": "whimsy"}).json()
    assert w["inputs"][0]["choices"] == ["apache/whimsy", "apache/whimsy-private"]
    assert w["inputs"][0]["default"] == "apache/whimsy"
    r = c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy-site"}})
    assert r.status_code == 403


def test_not_published_to_the_project_or_disabled(env):
    _, _, c = env
    wid = _publish(c)
    _as(CAROL)
    assert c.post(f"/workloads/{wid}/run", json={"project": "tomcat", "inputs": {"sourceRepo": "apache/tomcat"}}).status_code == 404
    _as(ADMIN)
    assert c.put(f"/workloads/admin/{wid}", json={**ASVS, "enabled": False}).status_code == 200
    _as(ALICE)
    assert c.get("/workloads", params={"project": "whimsy"}).json()["workloads"] == []
    assert c.post(f"/workloads/{wid}/run", json={"project": "whimsy", "inputs": {"sourceRepo": "apache/whimsy"}}).status_code == 404


def test_members_stop_their_own_runs_the_pmc_stops_any(env):
    _, reg, c = env
    rec = reg.new_record(user_id="asf:alice", agent_name="asvs_orchestrate", project="whimsy")
    _as(BOB)
    assert c.post(f"/runs/{rec.run_id}/stop").status_code == 403
    _as(CAROL)
    assert c.post(f"/runs/{rec.run_id}/stop").status_code == 404
    _as(BOB)
    assert c.post(f"/runs/{rec.run_id}/force-complete", json={"force": True}).status_code == 403
    _as(PMC)
    assert c.post(f"/runs/{rec.run_id}/stop").status_code == 202
    mine = reg.new_record(user_id="asf:alice", agent_name="asvs_orchestrate", project="whimsy")
    _as(ALICE)
    assert c.post(f"/runs/{mine.run_id}/stop").status_code == 202
    loose = reg.new_record(user_id="asf:alice", agent_name="sandbox")
    _as(ADMIN)
    assert c.post(f"/runs/{loose.run_id}/stop").status_code == 202


def test_admins_change_and_unpublish(env):
    _, _, c = env
    wid = _publish(c)
    _as(ADMIN)
    full = c.get("/workloads/admin").json()["workloads"]
    assert full[0]["fixedInputs"]["sourceToken"] == "github_pat_SECRET"
    assert c.delete(f"/workloads/admin/{wid}").status_code == 204
    assert c.get("/workloads/admin").json()["workloads"] == []
