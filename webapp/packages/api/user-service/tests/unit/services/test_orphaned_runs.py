"""ISSUE-022: runs whose worker is gone are closed, not left running forever."""

from datetime import timedelta

import pytest

from services import run_registry as rr
from services.database_service.memory import MemoryDBService
from time_utils import naive_utc_now

NOW = naive_utc_now()


def _run(db, run_id, worker_id, started_ago=600, status="running"):
    db.save(rr.RUN_REGISTRY_DB, run_id, {
        "_id": run_id, "type": "run", "run_id": run_id, "user_id": "u",
        "agent_name": "a", "status": status, "worker_id": worker_id,
        "started_at": (NOW - timedelta(seconds=started_ago)).isoformat(),
    })


def _worker(db, worker_id, seen_ago):
    db.save(rr.WORKERS_DB, worker_id, {
        "_id": worker_id, "type": "worker", "worker_id": worker_id,
        "last_seen": (NOW - timedelta(seconds=seen_ago)).isoformat(),
    })


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(rr, "WORKER_DEAD_AFTER_SECONDS", 120.0)
    db = MemoryDBService()
    return db, rr.RunRegistry(db=db)


def _status(db, run_id):
    return db.get(rr.RUN_REGISTRY_DB, run_id)["status"]


def test_runs_of_a_dead_worker_are_closed_others_are_not(setup):
    db, reg = setup
    _worker(db, "alive", seen_ago=30)
    _worker(db, "dead", seen_ago=600)
    _run(db, "r-alive", "alive")
    _run(db, "r-dead", "dead")
    _run(db, "r-unknown", "never-heartbeated")
    _run(db, "r-mine", rr.WORKER_ID)
    _run(db, "r-done", "dead", status="success")

    assert reg.reconcile_orphans(now=NOW) == 2

    assert _status(db, "r-alive") == "running"
    assert _status(db, "r-mine") == "running"
    assert _status(db, "r-done") == "success"
    for run_id, worker in (("r-dead", "dead"), ("r-unknown", "never-heartbeated")):
        doc = db.get(rr.RUN_REGISTRY_DB, run_id)
        assert doc["status"] == "error"
        assert doc["error"] == rr.ORPHANED_RUN_ERROR
        assert doc["orphaned_from_worker"] == worker
        assert doc["completed_at"] == NOW.isoformat()


def test_a_run_with_no_worker_gets_the_same_grace(setup):
    db, reg = setup
    _run(db, "fresh", "", started_ago=30)
    _run(db, "stale", "", started_ago=600)
    assert reg.reconcile_orphans(now=NOW) == 1
    assert _status(db, "fresh") == "running"
    assert _status(db, "stale") == "error"


def test_sweep_is_idempotent(setup):
    db, reg = setup
    _run(db, "r", "gone")
    assert reg.reconcile_orphans(now=NOW) == 1
    assert reg.reconcile_orphans(now=NOW) == 0


def test_heartbeat_keeps_this_workers_peers_alive(setup):
    db, reg = setup
    reg.heartbeat()
    beat = db.get(rr.WORKERS_DB, rr.WORKER_ID)
    assert beat["worker_id"] == rr.WORKER_ID
    reg.heartbeat()  # updating an existing beat must not fail
    other = rr.RunRegistry(db=db)
    _run(db, "r", rr.WORKER_ID)
    assert other.reconcile_orphans(now=naive_utc_now()) == 0


def test_long_silent_worker_docs_are_pruned(setup):
    db, reg = setup
    _worker(db, "ancient", seen_ago=rr.WORKER_DOC_RETENTION_SECONDS + 60)
    reg.reconcile_orphans(now=NOW)
    with pytest.raises(Exception):
        db.get(rr.WORKERS_DB, "ancient")


def test_cached_view_copy_is_updated(setup):
    db, reg = setup
    _run(db, "r", "gone")
    cached = reg.get("r")
    assert cached.status == "running"
    reg.reconcile_orphans(now=NOW)
    assert reg.get("r").status == "error"
