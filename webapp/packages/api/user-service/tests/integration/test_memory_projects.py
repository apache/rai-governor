"""A project's memory domain is its own (ISSUE-002).

The domain is named for the project (``whimsy``). Its members read it, its
PMC writes and reviews it, and an agent running for the project writes into
it at raw. Shared domains (MEMORY_DOMAINS) are for site admins and
MEMORY_WRITERS unless listed in MEMORY_OPEN_DOMAINS, and a project's run
reads only the open ones, whoever launched it.
"""

import pytest
from fastapi.testclient import TestClient

import dependencies
from app_factory import create_app
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.database_service.memory import MemoryDBService
from services.memory import access, blob_store


def _ws(*pairs):
    return [{"workspaceId": f"project:{p}", "role": r, "displayName": p, "source": "ldap_committer"}
            for p, r in pairs]


COMMITTER = {"uid": "asf:alice", "workspaces": _ws(("whimsy", "member"))}
PMC = {"uid": "asf:pmc", "workspaces": _ws(("whimsy", "admin"))}
OTHER = {"uid": "asf:carol", "workspaces": _ws(("tomcat", "member"))}
ADMIN = {"uid": "asf:root", "is_site_admin": True, "workspaces": []}

BODY = {"type": "fact", "summary": "String-built SQL in board.rb",
        "body": "# SQLi\n\nline 10\n", "review_by": "2027-02-01"}


class _Who:
    user = ADMIN


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.delenv("MEMORY_OPEN_DOMAINS", raising=False)
    monkeypatch.delenv("MEMORY_WRITERS", raising=False)
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _Who.user
    app.dependency_overrides[get_user_service_dep] = lambda: None
    c = TestClient(app)
    c.db = db
    return c


def _as(user):
    _Who.user = user


# --- which names are domains -------------------------------------------------

def test_domain_names(client):
    assert access.is_shared("dom-scan") and access.project_of("dom-scan") is None
    assert access.project_of("whimsy") == "whimsy"
    assert access.project_of("dom-secret") is None          # dom- is for shared domains
    assert access.project_of("Whimsy") is None and access.project_of("") is None
    assert not access.exists("dom-secret")


# --- routes ------------------------------------------------------------------

def test_pmc_writes_members_read_others_get_404(client):
    _as(PMC)
    r = client.post("/memory/whimsy/entries", json=BODY)
    assert r.status_code == 201, r.text
    eid = r.json()["frontmatter"]["id"]
    assert eid.startswith("mem-whimsy-")

    _as(COMMITTER)
    assert [h["id"] for h in client.get("/memory/whimsy/search").json()["hits"]] == [eid]
    assert client.get(f"/memory/entries/{eid}").status_code == 200
    assert client.get(f"/memory/entries/{eid}/history").status_code == 200
    assert client.post("/memory/whimsy/entries", json=BODY).status_code == 403

    _as(OTHER)
    assert client.get("/memory/whimsy/search").status_code == 404
    assert client.get(f"/memory/entries/{eid}").status_code == 404
    assert client.get(f"/memory/entries/{eid}/history").status_code == 404
    assert client.post("/memory/whimsy/recall", json={"question": "sql"}).status_code == 404


def test_the_pmc_reviews_committers_do_not(client):
    _as(PMC)
    eid = client.post("/memory/whimsy/entries", json=BODY).json()["frontmatter"]["id"]
    _as(COMMITTER)
    r = client.post(f"/memory/entries/{eid}/review", json={"action": "keep"})
    assert r.status_code == 403
    _as(ADMIN)
    r = client.post(f"/memory/entries/{eid}/review", json={"action": "curate", "why": "read board.rb"})
    assert r.status_code == 200, r.text


def test_an_unreadable_entry_looks_missing(client):
    _as(PMC)
    eid = client.post("/memory/whimsy/entries", json=BODY).json()["frontmatter"]["id"]
    _as(OTHER)
    for path in (f"/memory/entries/{eid}", f"/memory/entries/{eid}/history"):
        hidden, missing = client.get(path), client.get(path.replace(eid, "mem-whimsy-nothere1"))
        assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    hidden = client.post(f"/memory/entries/{eid}/review", json={"action": "keep"})
    missing = client.post("/memory/entries/mem-whimsy-nothere1/review", json={"action": "keep"})
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()) == (404, {"detail": "No such entry"})
    # tomcat's PMC can't learn the id exists by superseding it from tomcat
    _as({"uid": "asf:tpmc", "workspaces": _ws(("tomcat", "admin"))})
    hidden = client.post("/memory/tomcat/entries", json={**BODY, "supersedes": eid})
    missing = client.post("/memory/tomcat/entries", json={**BODY, "supersedes": "mem-whimsy-nothere1"})
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    assert hidden.status_code == 404


def test_shared_domains_are_closed_to_non_writers(client, monkeypatch):
    _as(ADMIN)
    eid = client.post("/memory/dom-scan/entries", json=BODY).json()["frontmatter"]["id"]
    _as(PMC)
    assert client.get("/memory/dom-scan/search").status_code == 404
    assert client.get(f"/memory/entries/{eid}").status_code == 404
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    assert client.get(f"/memory/entries/{eid}").status_code == 200
    assert client.post("/memory/dom-scan/entries", json=BODY).status_code == 403


def test_domains_lists_what_each_person_may_read(client):
    _as(PMC)
    client.post("/memory/whimsy/entries", json=BODY)
    _as(ADMIN)
    client.post("/memory/tomcat/entries", json=BODY)

    def listed(user):
        _as(user)
        return {d["domain"]: (d["kind"], d["canWrite"]) for d in client.get("/memory/domains").json()["domains"]}

    assert listed(COMMITTER) == {"whimsy": ("project", False)}
    assert listed(PMC) == {"whimsy": ("project", True)}
    assert listed(OTHER) == {"tomcat": ("project", False)}
    assert listed(ADMIN) == {"dom-scan": ("shared", True), "tomcat": ("project", True),
                             "whimsy": ("project", True)}


def test_domain_counts_are_the_same_read_whole_or_one_at_a_time(client, monkeypatch):
    """A site admin's list counts every domain from one read of the index,
    not one query per domain; a member's matches it for their domain."""
    _as(PMC)
    client.post("/memory/whimsy/entries", json=BODY)
    client.post("/memory/whimsy/entries", json={**BODY, "summary": "another"})
    _as(ADMIN)
    client.post("/memory/dom-scan/entries", json=BODY)
    finds = []
    real = client.db.find
    monkeypatch.setattr(client.db, "find", lambda db, sel, *a, **k: finds.append(db) or real(db, sel, *a, **k))

    def counts(user):
        _as(user)
        return {d["domain"]: {k: d[k] for k in ("entries", "raw", "due", "review")}
                for d in client.get("/memory/domains").json()["domains"]}

    admin = counts(ADMIN)
    assert "memory_index" not in finds
    assert admin["whimsy"]["entries"] == 2 and admin["dom-scan"]["entries"] == 1
    assert counts(COMMITTER) == {"whimsy": admin["whimsy"]}


def test_status_says_memory_is_on_without_counting(client, monkeypatch):
    _as(COMMITTER)
    monkeypatch.setattr(client.db, "find", lambda *a, **k: pytest.fail("counted"))
    monkeypatch.setattr(client.db, "list_all", lambda *a, **k: pytest.fail("counted"))
    assert client.get("/memory/status").json() == {"enabled": True}


def test_mistyped_shared_domain_is_404_even_for_a_site_admin(client):
    _as(ADMIN)
    assert client.post("/memory/dom-secret/entries", json=BODY).status_code == 404


# --- agent code --------------------------------------------------------------

AGENT = '''
async def run(input_dict, tools):
    out = {"domains": memory.domains}
    eid = await memory.write(summary="SQLi in board.rb", body="# SQLi\\n", origin="asvs:apache/whimsy")
    out["wrote"] = (await memory.get(eid))["frontmatter"]["domain"]
    for name in ("dom-scan",):
        try:
            await memory.write(summary="x", body="# x\\n", domain=name)
            out["shared_write"] = "ok"
        except PermissionError:
            out["shared_write"] = "refused"
        try:
            await memory.search("x", domain=name)
            out["shared_read"] = "ok"
        except PermissionError:
            out["shared_read"] = "refused"
    return out
'''


async def _run(db, user, project):
    out, _ = await dependencies._execute_agent_code(
        code=AGENT, input_dict={}, tools={}, gofannon_agents=[], db=db,
        user_id=user["uid"], user_basic_info={"is_site_admin": bool(user.get("is_site_admin"))},
        agent_name="asvs_consolidate", project=project,
    )
    return out


@pytest.mark.asyncio
async def test_a_project_run_writes_its_projects_domain_and_no_shared_one(client, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    out = await _run(client.db, COMMITTER, "whimsy")
    assert out["domains"] == ["whimsy"]
    assert out["wrote"] == "whimsy"
    assert out["shared_write"] == "refused" and out["shared_read"] == "refused"


@pytest.mark.asyncio
async def test_a_site_admins_project_run_reaches_no_closed_shared_domain(client, monkeypatch):
    # Every run is admin-launched today: the launcher's reach would put
    # dom-scan, other projects' findings included, into whimsy's output.
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    out = await _run(client.db, ADMIN, "whimsy")
    assert out["domains"] == ["whimsy"]
    assert out["wrote"] == "whimsy"
    assert out["shared_write"] == "refused" and out["shared_read"] == "refused"


@pytest.mark.asyncio
async def test_a_project_run_reads_open_shared_domains_only(client, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    monkeypatch.setenv("MEMORY_OPEN_DOMAINS", "dom-scan")
    out = await _run(client.db, ADMIN, "whimsy")
    assert out["domains"] == ["whimsy", "dom-scan"]
    assert out["shared_read"] == "ok" and out["shared_write"] == "refused"


@pytest.mark.asyncio
async def test_a_run_for_no_project_is_as_before(client, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    out = await _run(client.db, ADMIN, None)
    assert out["domains"] == ["dom-scan"] and out["wrote"] == "dom-scan"
