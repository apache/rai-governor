"""ISSUE-017: a run stream served by a worker that is not running the run.

With several API workers, GET /runs/{id}/stream can land on a worker that
only has a stored copy of the run. That copy never receives live events or
the done signal, so the stream sent heartbeats forever. It now follows the
stored run, which the owning worker checkpoints as it goes.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import routes
from services import run_registry as rr
from services.database_service.memory import MemoryDBService

OTHER_WORKER = "another-worker"


def _store(db, run_id, status="running", events=(), worker=OTHER_WORKER, **extra):
    doc = {
        "_id": run_id, "type": "run", "run_id": run_id, "user_id": "u1",
        "agent_name": "a", "status": status, "worker_id": worker,
        "started_at": "2026-11-05T10:00:00", "events": list(events),
    }
    doc.update(extra)
    try:
        doc["_rev"] = db.get(rr.RUN_REGISTRY_DB, run_id).get("_rev")
    except Exception:
        pass
    db.save(rr.RUN_REGISTRY_DB, run_id, doc)


def _frames(chunks):
    out = []
    for chunk in chunks:
        for block in chunk.strip().split("\n\n"):
            if block.startswith("event: "):
                name, data = block.split("\n", 1)
                out.append((name[len("event: "):], json.loads(data[len("data: "):])))
    return out


async def _collect(response, limit_s=5.0):
    chunks = []

    async def drain():
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    await asyncio.wait_for(drain(), timeout=limit_s)
    return chunks


@pytest.fixture
def registry(monkeypatch):
    db = MemoryDBService()
    reg = rr.RunRegistry(db=db)
    monkeypatch.setattr(rr, "get_run_registry", lambda: reg)
    monkeypatch.setattr(routes, "FOLLOW_POLL_SECONDS", 0.01)
    return db, reg


@pytest.mark.asyncio
async def test_stream_on_a_non_owning_worker_follows_the_run_to_done(registry):
    db, reg = registry
    _store(db, "r1", events=[{"n": 1}])
    polls = {"n": 0}
    real_fetch = reg.fetch_persisted

    def fetch(run_id):
        # The owning worker makes progress between polls, then finishes.
        polls["n"] += 1
        if polls["n"] == 2:
            _store(db, run_id, events=[{"n": 1}, {"n": 2}])
        if polls["n"] == 3:
            _store(db, run_id, status="success", events=[{"n": 1}, {"n": 2}, {"n": 3}],
                   result={"outputText": "ok"})
        return real_fetch(run_id)

    reg.fetch_persisted = fetch
    response = await routes.stream_run("r1", user={"uid": "u1"})
    frames = _frames(await _collect(response))

    assert [name for name, _ in frames] == ["run_id", "trace", "trace", "trace", "done"]
    assert [d["n"] for name, d in frames if name == "trace"] == [1, 2, 3]
    assert frames[-1][1]["outcome"] == "success"
    assert frames[-1][1]["result"] == {"outputText": "ok"}


@pytest.mark.asyncio
async def test_the_follower_keeps_up_past_the_cap(registry):
    # Past the cap the kept list stays the same length while the run goes
    # on: the follower goes by event numbers, not by count.
    db, reg = registry
    marker = {"type": "trace_truncated", "message": "dropped"}
    _store(db, "r9", events=[{"n": 1, "seq": 1}, marker, {"n": 5, "seq": 5}])
    polls = {"n": 0}
    real_fetch = reg.fetch_persisted

    def fetch(run_id):
        polls["n"] += 1
        if polls["n"] == 2:
            _store(db, run_id, events=[{"n": 1, "seq": 1}, marker, {"n": 9, "seq": 9}])
        if polls["n"] == 3:
            _store(db, run_id, status="success", events=[{"n": 1, "seq": 1}, marker, {"n": 12, "seq": 12}])
        return real_fetch(run_id)

    reg.fetch_persisted = fetch
    response = await routes.stream_run("r9", user={"uid": "u1"})
    frames = _frames(await _collect(response))
    traced = [d.get("n", d.get("type")) for name, d in frames if name == "trace"]
    assert traced == [1, "trace_truncated", 5, 9, 12]


@pytest.mark.asyncio
async def test_a_run_closed_by_the_orphan_sweep_ends_the_stream(registry):
    db, reg = registry
    _store(db, "r2", events=[{"n": 1}])
    seen = {"n": 0}
    real_fetch = reg.fetch_persisted

    def fetch(run_id):
        seen["n"] += 1
        if seen["n"] == 2:
            _store(db, run_id, status="error", events=[{"n": 1}], error=rr.ORPHANED_RUN_ERROR)
        return real_fetch(run_id)

    reg.fetch_persisted = fetch
    frames = _frames(await _collect(await routes.stream_run("r2", user={"uid": "u1"})))
    assert frames[-1] == ("done", {"outcome": "error", "result": None, "error": rr.ORPHANED_RUN_ERROR,
                                   "schemaWarnings": None, "opsLog": None})


@pytest.mark.asyncio
async def test_owner_still_streams_live_events(registry):
    db, reg = registry
    record = reg.new_record(user_id="u1", agent_name="a")
    response = await routes.stream_run(record.run_id, user={"uid": "u1"})

    async def run():
        await asyncio.sleep(0.05)
        record.trace.append({"live": True})
        await asyncio.sleep(0.05)
        reg.mark_complete(record, status="success", result={"ok": 1})

    task = asyncio.create_task(run())
    frames = _frames(await _collect(response))
    await task
    assert [name for name, _ in frames] == ["run_id", "trace", "done"]


def test_checkpoint_writes_events_only_and_keeps_a_stop_request(registry):
    db, reg = registry
    record = reg.new_record(user_id="u1", agent_name="a")
    record.trace.append({"n": 1})
    # Another worker flags a stop on the stored doc.
    doc = dict(db.get(rr.RUN_REGISTRY_DB, record.run_id))
    doc["stop_requested"] = True
    db.save(rr.RUN_REGISTRY_DB, record.run_id, doc)

    assert reg.checkpoint_events() == 1
    stored = db.get(rr.RUN_REGISTRY_DB, record.run_id)
    assert stored["events"] == [{"n": 1, "seq": 1}]  # numbered by the trace
    assert stored["stop_requested"] is True
    assert reg.checkpoint_events() == 0  # nothing new


def test_checkpoint_never_reopens_a_finished_run(registry):
    db, reg = registry
    record = reg.new_record(user_id="u1", agent_name="a")
    record.trace.append({"n": 1})
    doc = dict(db.get(rr.RUN_REGISTRY_DB, record.run_id))
    doc["status"] = "success"  # finished as far as the store knows
    db.save(rr.RUN_REGISTRY_DB, record.run_id, doc)
    assert reg.checkpoint_events() == 0
    assert db.get(rr.RUN_REGISTRY_DB, record.run_id)["status"] == "success"


@pytest.mark.asyncio
async def test_a_database_error_does_not_end_the_followed_stream(registry):
    db, reg = registry
    _store(db, "r3", events=[{"n": 1}])
    calls = {"n": 0}
    real_fetch = reg.fetch_persisted

    def fetch(run_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("couchdb blip")
        if calls["n"] == 3:
            _store(db, run_id, status="success", events=[{"n": 1}])
        return real_fetch(run_id)

    reg.fetch_persisted = fetch
    frames = _frames(await _collect(await routes.stream_run("r3", user={"uid": "u1"})))
    assert [name for name, _ in frames] == ["run_id", "trace", "done"]
    assert frames[-1][1]["outcome"] == "success"
