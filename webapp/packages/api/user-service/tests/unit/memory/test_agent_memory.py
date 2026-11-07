"""``memory`` in agent code (ISSUE-033), run through _execute_agent_code."""

import pytest

import dependencies
from services.database_service.memory import MemoryDBService
from services.memory import blob_store

AGENT = '''
async def run(input_dict, tools):
    if not memory.enabled:
        return {"outputText": "off"}
    eid = await memory.write(
        summary="String-built SQL\\nin a.py", body="# SQLi\\n\\nline 10\\n",
        source=["https://example.org/a.py#L10"], origin="run-1", model="qwen3-8b",
    )
    hits = await memory.search("sql", origin="run-1")
    got = await memory.get(eid)
    return {"outputText": eid, "hits": [h["id"] for h in hits],
            "author": got["frontmatter"]["author"], "tier": got["frontmatter"]["trust_tier"],
            "summary": got["frontmatter"]["summary"], "written": memory.written}
'''


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DOMAINS", "dom-scan")
    monkeypatch.setenv("MEMORY_WRITERS", "asf:akm")
    monkeypatch.setattr(blob_store, "_default_in_memory", blob_store.InMemoryBlobStore())
    return MemoryDBService()


async def _run(db, code=AGENT, user_id="asf:akm", admin=False):
    out, _ = await dependencies._execute_agent_code(
        code=code, input_dict={}, tools={}, gofannon_agents=[], db=db,
        user_id=user_id, user_basic_info={"is_site_admin": admin}, agent_name="asvs_audit",
    )
    return out


@pytest.mark.asyncio
async def test_an_agent_writes_at_raw_under_its_own_name(env, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    out = await _run(env)
    assert out["hits"] == [out["outputText"]] == out["written"]
    assert out["author"] == "agent:asvs_audit (model: qwen3-8b)"
    assert out["tier"] == "raw"
    assert out["summary"] == "String-built SQL in a.py"


@pytest.mark.asyncio
async def test_memory_is_there_but_off_unless_enabled(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    out = await _run(MemoryDBService())
    assert out == {"outputText": "off"}


@pytest.mark.asyncio
async def test_only_a_memory_writer_runs_agents_that_write(env, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    with pytest.raises(PermissionError, match="may not write memory"):
        await _run(env, user_id="asf:someone")
    out = await _run(env, user_id="asf:someone", admin=True)
    assert out["tier"] == "raw"


@pytest.mark.asyncio
async def test_reads_stay_inside_the_configured_domains(env, monkeypatch):
    monkeypatch.setattr(dependencies, "get_user_service", lambda db: None)
    code = '''
async def run(input_dict, tools):
    try:
        await memory.search("x", domain="dom-secret")
    except ValueError as e:
        return {"outputText": str(e)}
'''
    out = await _run(env, code=code)
    assert "no memory domain 'dom-secret'" in out["outputText"]
