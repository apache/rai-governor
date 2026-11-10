"""Pipeline import and export (ISSUE-011)."""

import io
import uuid
import zipfile

import httpx
import pytest
import yaml

import dependencies
from services import pipeline_io as pio
from services.database_service.memory import MemoryDBService

PARENT = '''
async def run(input_dict, tools):
    child = await gofannon_client.call("child", {"inputText": input_dict.get("inputText", "")})
    return {"outputText": "parent saw: " + child["outputText"]}
'''
CHILD = '''
async def run(input_dict, tools):
    import os
    return {"outputText": input_dict["inputText"].upper() + " " + os.environ.get("GREETING", "")}
'''


def _folder(**extra_agents):
    manifest = {
        "pipeline": "demo",
        "entry": "parent",
        "agents": [
            {"name": "parent", "code": "agents/parent.py", "description": "top",
             "calls": ["child"], "input_schema": {"inputText": "string"}},
            {"name": "child", "code": "agents/child.py", "env_file": "agents/env/child.env",
             "env": {"EXTRA": "inline"}},
        ],
    }
    files = {
        "pipeline.yaml": yaml.safe_dump(manifest),
        "agents/parent.py": PARENT,
        "agents/child.py": CHILD,
        "agents/env/child.env": "# greeting\nGREETING=hello   # trailing note\nEMPTY=\n",
    }
    files.update(extra_agents)
    return files


def _store():
    db = MemoryDBService()

    def save(doc):
        prev = None
        try:
            prev = db.get("agents", doc["_id"])
        except Exception:
            pass
        if prev and prev.get("_rev"):
            doc = {**doc, "_rev": prev["_rev"]}
        return db.save("agents", doc["_id"], doc).get("rev")

    return db, save


def _import(db, save, files, label, source=None):
    m = pio.load_manifest(files)
    return pio.apply_import(m, label, source or {"kind": "test"}, db.list_all("agents"),
                            save, lambda: str(uuid.uuid4()))


# --- env files --------------------------------------------------------------

def test_parse_env_handles_comments_quotes_and_empty_values():
    text = ('# header\n\nA=1\nB = two words   # note\nexport C="quoted # kept"\n'
            "D=\nE=a#b\n")
    assert pio.parse_env(text) == [
        ("A", "1"), ("B", "two words"), ("C", "quoted # kept"), ("D", ""), ("E", "a#b")]


def test_parse_env_rejects_lines_it_cannot_read():
    with pytest.raises(pio.PipelineError, match=":2: expected KEY=VALUE"):
        pio.parse_env("A=1\nnot a line\n", "x.env")
    with pytest.raises(pio.PipelineError, match="not a valid variable name"):
        pio.parse_env("1BAD=x\n")


# --- manifest ---------------------------------------------------------------

def test_manifest_merges_env_file_and_inline_env():
    m = pio.load_manifest(_folder())
    child = m.agents[1]
    assert child.env == [("GREETING", "hello"), ("EMPTY", ""), ("EXTRA", "inline")]
    assert m.entry == "parent" and m.agents[0].calls == ["child"]


def test_a_single_agent_is_a_pipeline():
    files = {"pipeline.yaml": "pipeline: solo\nagents:\n  - name: solo\n    code: solo.py\n",
             "solo.py": CHILD}
    m = pio.load_manifest(files)
    assert [a.name for a in m.agents] == ["solo"]
    plan = pio.plan_import(m, "v1", [])
    assert plan["entry"] == "solo"


def test_manifest_is_found_in_a_subfolder():
    files = {f"ASVS/{p}": t for p, t in _folder().items()}
    assert pio.load_manifest(files).pipeline == "demo"


@pytest.mark.parametrize("change, message", [
    ({"agents/child.py": None}, "code file"),
    ({"pipeline.yaml": "pipeline: Demo!\nagents: [{name: a, code: a.py}]\n"}, "'pipeline' must be"),
    ({"pipeline.yaml": "pipeline: d\nagents: [{name: a, code: ../a.py}]\n"}, "outside the pipeline"),
    ({"pipeline.yaml": "pipeline: d\nagents: [{name: a, code: agents/parent.py}, "
                       "{name: a, code: agents/parent.py}]\n"}, "duplicate agent"),
    ({"pipeline.yaml": "pipeline: d\nentry: nope\nagents: [{name: a, code: agents/parent.py}]\n"},
     "entry 'nope'"),
])
def test_manifest_errors_say_what_is_wrong(change, message):
    files = _folder()
    for k, v in change.items():
        if v is None:
            files.pop(k)
        else:
            files[k] = v
    with pytest.raises(pio.PipelineError, match=message):
        pio.load_manifest(files)


# --- import -----------------------------------------------------------------

def test_first_import_creates_a_labelled_copy():
    db, save = _store()
    result = _import(db, save, _folder(), "abc1234")
    agents = {a["baseName"]: a for a in db.list_all("agents")}
    assert set(agents) == {"parent", "child"}
    parent, child = agents["parent"], agents["child"]
    assert parent["name"] == "parent@abc1234" and child["name"] == "child@abc1234"
    assert parent["gofannonAgents"] == [child["_id"]]
    assert parent["friendlyName"] == "parent-abc1234"
    assert {e["key"]: e["value"] for e in child["envVars"]} == {
        "GREETING": "hello", "EMPTY": "", "EXTRA": "inline"}
    assert parent["pipeline"] == "demo" and parent["pipelineLabel"] == "abc1234"
    assert result["entryId"] == parent["_id"]
    assert [r["action"] for r in result["agents"]] == ["create", "create"]


def test_same_label_updates_in_place_and_reports_what_changed():
    db, save = _store()
    first = _import(db, save, _folder(), "v1")
    m = pio.load_manifest(_folder())
    assert [r["action"] for r in pio.plan_import(m, "v1", db.list_all("agents"))["agents"]] == [
        "unchanged", "unchanged"]

    files = _folder()
    files["agents/child.py"] = CHILD + "\n# edited\n"
    files["agents/env/child.env"] = "GREETING=hi\n"
    plan = pio.plan_import(pio.load_manifest(files), "v1", db.list_all("agents"))
    rows = {r["agent"]: r for r in plan["agents"]}
    assert rows["child"]["action"] == "update"
    assert rows["child"]["changes"] == ["code", "env"]
    assert rows["parent"]["action"] == "unchanged"

    second = _import(db, save, files, "v1")
    assert second["written"]["child"]["id"] == first["written"]["child"]["id"]
    assert len(db.list_all("agents")) == 2


def test_a_new_label_makes_a_second_copy_alongside():
    db, save = _store()
    _import(db, save, _folder(), "v1")
    _import(db, save, _folder(), "v2")
    names = sorted(a["name"] for a in db.list_all("agents"))
    assert names == ["child@v1", "child@v2", "parent@v1", "parent@v2"]
    v2 = {a["baseName"]: a for a in db.list_all("agents") if a["pipelineLabel"] == "v2"}
    assert v2["parent"]["gofannonAgents"] == [v2["child"]["_id"]]


def test_problems_block_the_import():
    db, save = _store()
    db.save("agents", "x", {"_id": "x", "name": "parent@v1", "code": ""})
    files = _folder()
    manifest = yaml.safe_load(files["pipeline.yaml"])
    manifest["agents"][0]["calls"] = ["child", "missing_helper"]
    files["pipeline.yaml"] = yaml.safe_dump(manifest)
    plan = pio.plan_import(pio.load_manifest(files), "v1", db.list_all("agents"))
    assert any("parent@v1: an agent with this name exists" in p for p in plan["problems"])
    assert any("calls 'missing_helper'" in p for p in plan["problems"])
    with pytest.raises(pio.PipelineError):
        _import(db, save, files, "v1")


def test_a_call_to_an_existing_shared_agent_is_linked_by_name():
    db, save = _store()
    db.save("agents", "helper-id", {"_id": "helper-id", "name": "shared_helper", "code": ""})
    files = _folder()
    manifest = yaml.safe_load(files["pipeline.yaml"])
    manifest["agents"][1]["calls"] = ["shared_helper"]
    files["pipeline.yaml"] = yaml.safe_dump(manifest)
    result = _import(db, save, files, "v1")
    child = db.get("agents", result["written"]["child"]["id"])
    assert child["gofannonAgents"] == ["helper-id"]
    assert result["agents"][1]["externalCalls"] == ["shared_helper"]


@pytest.mark.parametrize("bad", ["", "has space", "a/b", "x" * 41])
def test_labels_are_checked(bad):
    with pytest.raises(pio.PipelineError, match="label"):
        pio.check_label(bad)


def test_default_labels():
    from datetime import datetime, timezone
    assert pio.default_label(commit_sha="3194087abcdef") == "3194087"
    assert pio.default_label(now=datetime(2026, 11, 6, 18, 5, 9, tzinfo=timezone.utc)) == "20261106-180509"


# --- running an imported copy -------------------------------------------------

@pytest.mark.asyncio
async def test_imported_code_calls_its_sibling_by_plain_name():
    """The parent's code calls "child", but the stored agent is "child@v1"."""
    db, save = _store()
    result = _import(db, save, _folder(), "v1")
    parent = db.get("agents", result["written"]["parent"]["id"])
    child = db.get("agents", result["written"]["child"]["id"])
    from models.agent import AgentEnvVar
    from services.environ_proxy import install_environ_proxy
    install_environ_proxy()   # the app does this at startup; env overlays need it
    out, _ops = await dependencies._execute_agent_code(
        code=parent["code"], input_dict={"inputText": "hi"}, tools={},
        gofannon_agents=parent["gofannonAgents"], db=db,
        env_vars=[AgentEnvVar(**e) for e in parent["envVars"]],
    )
    assert out == {"outputText": "parent saw: HI hello"}
    assert child["name"] == "child@v1"


# --- export -----------------------------------------------------------------

def test_export_round_trips_through_import():
    db, save = _store()
    result = _import(db, save, _folder(), "v1")
    docs = pio.collect_pipeline(result["entryId"], lambda i: db.get("agents", i))
    pipeline, files = pio.build_export(docs)
    assert pipeline == "demo"
    assert set(files) == {"pipeline.yaml", "agents/parent.py", "agents/child.py",
                          "agents/env/child.env"}
    m = pio.load_manifest(files)
    by = {a.name: a for a in m.agents}
    assert m.entry == "parent" and by["parent"].calls == ["child"]
    assert by["parent"].friendly_name == "parent"
    assert dict(by["child"].env) == {"GREETING": "hello", "EMPTY": "", "EXTRA": "inline"}
    assert by["child"].code == CHILD
    # Re-importing the export under the same label changes nothing.
    plan = pio.plan_import(m, "v1", db.list_all("agents"))
    assert [r["action"] for r in plan["agents"]] == ["unchanged", "unchanged"]


def test_export_of_an_agent_made_in_the_ui():
    docs = [{"_id": "a1", "name": "My Agent", "code": "async def run(i, t): pass",
             "gofannonAgents": [], "envVars": []}]
    pipeline, files = pio.build_export(docs)
    assert pipeline == "my-agent"
    assert set(files) == {"pipeline.yaml", "agents/My Agent.py"}
    m = pio.load_manifest(files)
    assert [a.name for a in m.agents] == ["My Agent"]


def test_zip_puts_everything_under_one_folder():
    data = pio.zip_files({"pipeline.yaml": "x", "agents/a.py": "y"}, "demo")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert names == ["demo/agents/a.py", "demo/pipeline.yaml"]


# --- sources ----------------------------------------------------------------

def test_upload_drops_the_picked_folders_own_name_and_skips_binaries():
    files = pio.files_from_upload([
        ("ASVS/pipeline.yaml", b"pipeline: x"),
        ("ASVS/agents/a.py", b"code"),
        ("ASVS/logo.png", b"\x89PNG\x00\xff"),
    ])
    assert files == {"pipeline.yaml": "pipeline: x", "agents/a.py": "code"}


def _github_transport(tree, blobs, sha="3194087abcdef0000000000000000000000000000", status=200):
    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/commits/main"):
            if status != 200:
                return httpx.Response(status, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": sha})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [
                {"path": p, "type": "blob", "sha": f"blob-{p}"} for p in tree]})
        if "/git/blobs/" in path:
            p = path.split("/git/blobs/blob-", 1)[1]
            import base64
            return httpx.Response(200, json={"content": base64.b64encode(blobs[p].encode()).decode()})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_github_import_reads_only_what_the_manifest_names():
    folder = {f"ASVS/{p}": t for p, t in _folder().items()}
    tree = list(folder) + ["ASVS/README.md", "other/pipeline.yaml"]
    async with httpx.AsyncClient(transport=_github_transport(tree, folder)) as client:
        files, sha = await pio.files_from_github("apache/tooling-agents", "main", "ASVS",
                                                 None, client=client)
    assert sha.startswith("3194087")
    assert set(files) == set(_folder())
    assert pio.load_manifest(files).pipeline == "demo"


@pytest.mark.asyncio
async def test_github_errors_are_explained():
    async with httpx.AsyncClient(transport=_github_transport([], {}, status=404)) as client:
        with pytest.raises(pio.PipelineError, match="private and needs a token"):
            await pio.files_from_github("apache/private", "main", "", None, client=client)
    limited = httpx.MockTransport(lambda req: httpx.Response(
        403, json={"message": "API rate limit exceeded for 1.2.3.4."}))
    async with httpx.AsyncClient(transport=limited) as client:
        with pytest.raises(pio.PipelineError, match="rate limit .* add one"):
            await pio.files_from_github("apache/tooling-agents", "main", "", None, client=client)
    with pytest.raises(pio.PipelineError, match="owner/name"):
        await pio.files_from_github("not-a-repo", "main", "", None)


# --- HTTP -------------------------------------------------------------------

def _app_client(db):
    from fastapi import Request
    from fastapi.testclient import TestClient
    from unittest.mock import Mock
    from app_factory import create_app
    from dependencies import get_db, get_logger
    from routes import get_current_user

    app = create_app()

    def current(request: Request):
        return {"uid": "asf:dev", "email": "dev@example.com", "is_site_admin": True}

    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_logger] = lambda: Mock()
    return TestClient(app)


def test_folder_upload_previews_then_imports_then_exports():
    db = MemoryDBService()
    c = _app_client(db)
    upload = [("files", (f"demo/{p}", t.encode(), "text/plain")) for p, t in _folder().items()]

    r = c.post("/pipelines/import/upload", files=upload, data={"label": "v1"})
    assert r.status_code == 200, r.text
    assert [a["action"] for a in r.json()["agents"]] == ["create", "create"]
    assert db.list_all("agents") == []          # a dry run writes nothing

    r = c.post("/pipelines/import/upload", files=upload, data={"label": "v1", "dryRun": "false"})
    assert r.status_code == 200, r.text
    entry_id = r.json()["entryId"]
    assert sorted(a["name"] for a in db.list_all("agents")) == ["child@v1", "parent@v1"]

    r = c.get(f"/agents/{entry_id}/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="demo.zip"'
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert "demo/pipeline.yaml" in names and "demo/agents/child.py" in names

    r = c.get(f"/agents/{entry_id}")
    assert r.json()["pipelineLabel"] == "v1" and r.json()["baseName"] == "parent"


def test_a_bad_manifest_is_a_400_with_the_reason():
    c = _app_client(MemoryDBService())
    upload = [("files", ("demo/pipeline.yaml", b"pipeline: x\nagents: []\n", "text/plain"))]
    r = c.post("/pipelines/import/upload", files=upload)
    assert r.status_code == 400
    assert "'agents' must be a non-empty list" in r.json()["detail"]


def test_editing_an_imported_agent_in_the_ui_keeps_its_pipeline_fields():
    db, save = _store()
    result = _import(db, save, _folder(), "v1")
    c = _app_client(db)
    aid = result["written"]["child"]["id"]
    r = c.put(f"/agents/{aid}", json={"description": "edited in the UI"})
    assert r.status_code == 200, r.text
    doc = db.get("agents", aid)
    assert doc["description"] == "edited in the UI"
    assert (doc["pipeline"], doc["pipelineLabel"], doc["baseName"]) == ("demo", "v1", "child")
