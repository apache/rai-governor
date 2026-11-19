"""Admission limits: runs over RUN_MAX_CONCURRENT / RUN_MAX_PER_USER queue.

The fake database behaves like CouchDB (copies, 409 on a stale _rev); see
test_run_doc_writes. Two RunRegistry objects on one database stand for two
worker processes.
"""

import asyncio
from datetime import timedelta

import pytest

from services import run_admission as ra
from services import run_registry as rr
from tests.unit.services.test_run_doc_writes import RevDB
from time_utils import naive_utc_now


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 0)
    monkeypatch.setattr(ra, "RUN_MAX_PER_USER", 0)
    monkeypatch.setattr(ra, "RUN_QUEUE_POLL_SECONDS", 0.01)
    ra.reset_for_tests()
    return RevDB()


def _start(reg, user="u"):
    """What the start routes do."""
    queue = ra.applies(reg)
    rec = reg.new_record(user_id=user, agent_name="a", queued=queue)
    if queue:
        ra.admit(reg, rec)
    return rec


def _status(db, rec):
    return db.stored(rec.run_id)["status"]


def _running(db):
    return sum(1 for (name, _), d in db.docs.items()
               if name == rr.RUN_REGISTRY_DB and d.get("status") == "running")


async def _until(cond, seconds=2.0):
    for _ in range(int(seconds / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_limits_unset_change_nothing(db):
    finds = []
    real_find = db.find
    db.find = lambda *a, **k: finds.append(a) or real_find(*a, **k)
    reg = rr.RunRegistry(db=db)
    assert not ra.applies(reg)
    recs = [_start(reg) for _ in range(5)]
    assert all(r.status == "running" for r in recs)
    assert all(_status(db, r) == "running" for r in recs)
    doc = db.stored(recs[0].run_id)
    assert "queued_at" not in doc
    assert "queueAhead" not in recs[0].to_summary() and "queuedAt" not in recs[0].to_summary()
    assert asyncio.run(ra.wait_turn(reg, recs[0])) is True
    assert finds == []


def test_site_wide_limit_queues_and_starts_when_one_finishes(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 2)
    w1, w2 = rr.RunRegistry(db=db), rr.RunRegistry(db=db)
    a = _start(w1, "alice")
    b = _start(w2, "bob")
    c = _start(w1, "carol")
    assert (a.status, b.status, c.status) == ("running", "running", "queued")
    assert _status(db, c) == "queued" and db.stored(c.run_id)["queued_at"]
    assert ra.positions(w2, [c.run_id]) == {c.run_id: 0}

    async def go():
        waiter = asyncio.create_task(ra.wait_turn(w1, c))
        await asyncio.sleep(0.05)
        assert not waiter.done() and _status(db, c) == "queued"
        # Another worker's run ends: its slot goes to the queued run.
        w2.mark_complete(b, status="success")
        assert await asyncio.wait_for(waiter, 2) is True

    asyncio.run(go())
    assert c.status == "running" and _status(db, c) == "running"
    assert _running(db) == 2


def test_per_user_limit_passes_over_that_users_queued_run(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_PER_USER", 1)
    reg = rr.RunRegistry(db=db)
    a1 = _start(reg, "alice")
    a2 = _start(reg, "alice")
    b1 = _start(reg, "bob")
    assert (a1.status, a2.status, b1.status) == ("running", "queued", "running")

    async def go():
        waiter = asyncio.create_task(ra.wait_turn(reg, a2))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        reg.mark_complete(a1, status="error", error="x")
        assert await asyncio.wait_for(waiter, 2) is True

    asyncio.run(go())
    assert _status(db, a2) == "running"


def test_stopping_a_queued_run_takes_it_off_the_queue(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    w1, w2 = rr.RunRegistry(db=db), rr.RunRegistry(db=db)
    first = _start(w1)
    q1 = _start(w1)
    q2 = _start(w1)
    assert ra.positions(w1, [q1.run_id, q2.run_id]) == {q1.run_id: 0, q2.run_id: 1}

    async def go():
        waiter = asyncio.create_task(ra.wait_turn(w1, q1))
        await asyncio.sleep(0.03)
        # The stop lands on another worker: the doc says stopped at once.
        assert w2.request_stop(q1.run_id, "u") == "persisted_remote"
        assert _status(db, q1) == "stopped"
        assert await asyncio.wait_for(waiter, 2) is False

    asyncio.run(go())
    assert q1.status == "stopped" and q1.error == ra.STOPPED_WHILE_QUEUED
    stored = db.stored(q1.run_id)
    assert stored["status"] == "stopped" and stored["completed_at"]
    # It no longer counts in front of anyone.
    assert ra.positions(w1, [q2.run_id]) == {q2.run_id: 0}
    w1.mark_complete(first, status="success")
    assert ra.try_start(w1, q2) is True


def test_a_stop_on_the_owning_worker_is_seen_before_the_next_read(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    reg = rr.RunRegistry(db=db)
    _start(reg)
    q = _start(reg)
    reg.request_stop(q.run_id, "u")
    assert asyncio.run(ra.wait_turn(reg, q)) is False
    assert _status(db, q) == "stopped"


def test_queue_is_oldest_first(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    regs = [rr.RunRegistry(db=db) for _ in range(3)]
    first = _start(regs[0], "x")
    queued = [_start(regs[i], f"user{i}") for i in range(3)]
    assert all(r.status == "queued" for r in queued)
    assert ra.positions(regs[0], [r.run_id for r in queued]) == {r.run_id: i for i, r in enumerate(queued)}
    order = []

    async def wait(reg, rec):
        assert await ra.wait_turn(reg, rec)
        order.append(rec.run_id)
        await asyncio.sleep(0.05)
        reg.mark_complete(rec, status="success")

    async def go():
        # Newest waiter first: polling order must not decide.
        tasks = [asyncio.create_task(wait(regs[i], queued[i])) for i in (2, 1, 0)]
        await asyncio.sleep(0.05)
        assert order == [] and _running(db) == 1
        regs[0].mark_complete(first, status="success")
        await asyncio.wait_for(asyncio.gather(*tasks), 5)

    asyncio.run(go())
    assert order == [r.run_id for r in queued]


def test_two_claims_for_the_last_slot_never_both_win(db, monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    # Both claimants believe it is their turn (stale reads on two workers).
    monkeypatch.setattr(ra, "_my_turn", lambda *a: True)
    w1, w2 = rr.RunRegistry(db=db), rr.RunRegistry(db=db)
    a = w1.new_record(user_id="u", agent_name="a", queued=True)
    b = w2.new_record(user_id="v", agent_name="a", queued=True)
    results = {}
    # b claims and re-counts completely between a's read and a's write.
    db.between.append(lambda: results.setdefault("b", ra.try_start(w2, b)))
    results["a"] = ra.try_start(w1, a)
    assert results == {"a": False, "b": True}
    assert _running(db) == 1 and _status(db, a) == "queued" and a.status == "queued"

    # Another claim lands between a's read and a's write (its re-count not
    # yet done): a counts two and backs off.
    db2 = RevDB()
    w1, w2 = rr.RunRegistry(db=db2), rr.RunRegistry(db=db2)
    a = w1.new_record(user_id="u", agent_name="a", queued=True)
    b = w2.new_record(user_id="v", agent_name="a", queued=True)

    def b_claims():
        doc = db2.get(rr.RUN_REGISTRY_DB, b.run_id)
        doc["status"] = "running"
        db2.raw_save(rr.RUN_REGISTRY_DB, b.run_id, doc)

    db2.between.append(b_claims)
    assert ra.try_start(w1, a) is False
    assert _status(db2, a) == "queued" and _running(db2) == 1


def test_queued_runs_of_a_dead_worker_are_closed(db, monkeypatch):
    monkeypatch.setattr(rr, "WORKER_DEAD_AFTER_SECONDS", 120.0)
    now = naive_utc_now()
    db.raw_save(rr.WORKERS_DB, "dead", {"type": "worker", "worker_id": "dead",
                                          "last_seen": (now - timedelta(seconds=600)).isoformat()})
    for rid, status in (("q", "queued"), ("r", "running")):
        db.raw_save(rr.RUN_REGISTRY_DB, rid, {
            "type": "run", "run_id": rid, "user_id": "u", "agent_name": "a",
            "status": status, "worker_id": "dead",
            "started_at": (now - timedelta(seconds=600)).isoformat()})
    reg = rr.RunRegistry(db=db)
    assert reg.reconcile_orphans(now=now) == 2
    assert db.stored("q")["status"] == "error"
    assert db.stored("q")["error"] == rr.ORPHANED_QUEUED_ERROR
    assert db.stored("r")["error"] == rr.ORPHANED_RUN_ERROR


def test_without_a_database_limits_do_not_apply(monkeypatch):
    monkeypatch.setattr(ra, "RUN_MAX_CONCURRENT", 1)
    rr.reset_run_registry_for_tests()
    reg = rr.RunRegistry()
    assert not ra.applies(reg)
