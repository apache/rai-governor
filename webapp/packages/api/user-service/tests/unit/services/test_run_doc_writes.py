"""Writes to a run doc from different places must not undo each other.

The event checkpoint (ISSUE-017), completion, stop requests from another
worker, and the orphan sweep (ISSUE-022) all write the same CouchDB doc.
Each one reads, changes and saves; on a revision conflict it reads again and
decides again. The fake below behaves like CouchDB: it returns copies and
answers a save with a stale ``_rev`` with 409. ``between`` runs another
writer between a reader's get and its save, which is the race.
"""

import copy
from datetime import timedelta

import pytest
from fastapi import HTTPException

from services import run_registry as rr
from time_utils import naive_utc_now


class RevDB:
    def __init__(self):
        self.docs = {}
        self.between = []  # one-shot callbacks, run just before a save

    def get(self, db, doc_id):
        if (db, doc_id) not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return copy.deepcopy(self.docs[(db, doc_id)])

    def save(self, db, doc_id, doc):
        if self.between:
            self.between.pop(0)()
        return self.raw_save(db, doc_id, doc)

    def raw_save(self, db, doc_id, doc):
        current = self.docs.get((db, doc_id))
        if current is not None and doc.get("_rev") != current["_rev"]:
            raise HTTPException(status_code=409, detail="conflict")
        doc = copy.deepcopy(doc)
        doc["_id"] = doc_id
        doc["_rev"] = str(int(current["_rev"]) + 1 if current else 1)
        self.docs[(db, doc_id)] = doc
        return {"id": doc_id, "rev": doc["_rev"]}

    def find(self, db, selector=None, limit=None, **_):
        out = []
        for (name, _id), doc in self.docs.items():
            if name == db and all(doc.get(k) == v for k, v in (selector or {}).items()):
                out.append(copy.deepcopy(doc))
        return out

    def delete(self, db, doc_id):
        self.docs.pop((db, doc_id), None)

    def ensure_index(self, *a, **k):
        pass

    def stored(self, run_id):
        return self.docs[(rr.RUN_REGISTRY_DB, run_id)]


def _other_writer(db, run_id, **changes):
    """Another worker updating the doc on the current revision."""
    def write():
        doc = db.get(rr.RUN_REGISTRY_DB, run_id)
        doc.update(changes)
        db.raw_save(rr.RUN_REGISTRY_DB, run_id, doc)
    return write


@pytest.fixture
def setup():
    db = RevDB()
    reg = rr.RunRegistry(db=db)
    record = reg.new_record(user_id="u", agent_name="a")
    record.trace.append({"n": 1})
    return db, reg, record


def test_a_checkpoint_does_not_reopen_a_run_finished_meanwhile(setup):
    db, reg, record = setup
    db.between.append(_other_writer(db, record.run_id, status="success", result="ok"))

    assert reg.checkpoint_events() == 0
    assert db.stored(record.run_id)["status"] == "success"
    assert db.stored(record.run_id)["result"] == "ok"


def test_completion_lands_after_a_checkpoint_wrote_in_between(setup):
    db, reg, record = setup
    db.between.append(_other_writer(db, record.run_id, events=[{"n": 1}]))

    reg.mark_complete(record, status="success", result="ok")
    stored = db.stored(record.run_id)
    assert stored["status"] == "success"
    assert stored["result"] == "ok"


def test_a_stop_request_survives_a_checkpoint(setup):
    db, reg, record = setup
    other = rr.RunRegistry(db=db)
    # The stop request from another worker lands between the checkpoint's
    # read and its save.
    db.between.append(lambda: other.request_stop(record.run_id, "u"))

    assert reg.checkpoint_events() == 1
    stored = db.stored(record.run_id)
    assert stored["stop_requested"] is True
    assert stored["events"] == [{"n": 1, "seq": 1}]  # numbered by the trace


def test_completion_keeps_a_stop_request_made_elsewhere(setup):
    db, reg, record = setup
    db.between.append(_other_writer(db, record.run_id, stop_requested=True))

    reg.mark_complete(record, status="stopped")
    stored = db.stored(record.run_id)
    assert stored["status"] == "stopped"
    assert stored["stop_requested"] is True


def test_the_sweep_does_not_close_a_run_that_finished_meanwhile(setup, monkeypatch):
    db, reg, _ = setup
    monkeypatch.setattr(rr, "WORKER_DEAD_AFTER_SECONDS", 120.0)
    now = naive_utc_now()
    db.raw_save(rr.RUN_REGISTRY_DB, "r-x", {
        "type": "run", "run_id": "r-x", "user_id": "u", "agent_name": "a",
        "status": "running", "worker_id": "gone",
        "started_at": (now - timedelta(seconds=600)).isoformat(),
    })
    db.between.append(_other_writer(db, "r-x", status="success"))

    assert reg.reconcile_orphans(now=now) == 0
    assert db.stored("r-x")["status"] == "success"


def test_a_stored_copy_of_a_running_run_is_read_again(setup):
    db, _, record = setup
    reader = rr.RunRegistry(db=db)  # another worker
    assert reader.get(record.run_id).status == "running"
    _other_writer(db, record.run_id, status="success")()
    assert reader.get(record.run_id).status == "success"
    assert not reader.is_live_here(reader.get(record.run_id))
