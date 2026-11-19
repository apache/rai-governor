"""Admission limits for agent runs: queue a run instead of overloading the box.

RUN_MAX_CONCURRENT caps the runs going at once across every worker process,
RUN_MAX_PER_USER the runs one person has going. 0 (the default) is no limit,
and with both at 0 nothing here does anything.

Only runs with a run record count: the ones a person starts (workloads,
/rest/{name}/start, /agents/run-code/start and /stream). A pipeline's calls to
its sub-agents run inside the top-level run and have no record of their own.

A run over a limit is recorded as "queued". The worker that took the request
keeps it and polls until it is the run's turn, oldest first; a run whose
owner is at RUN_MAX_PER_USER is passed over, not made to block the others.

Claiming a slot is safe across processes without a lock: the claimant moves
its own doc from queued to running (CouchDB's _rev check makes that a
compare-and-set, so a stop and a claim cannot both win), then counts the
running runs again. Over a limit, it moves its doc back and waits. The last
of any set of claimants to write sees all the others' writes, so the limits
are never exceeded; at worst two claimants both back off and the oldest goes
first on the next poll.

A worker that dies leaves its queued and running docs behind; the orphan
sweep (ISSUE-022) closes both once its heartbeat is stale, which frees their
slots.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional

from time_utils import naive_utc_now

RUN_MAX_CONCURRENT = int(os.getenv("RUN_MAX_CONCURRENT", "0"))
RUN_MAX_PER_USER = int(os.getenv("RUN_MAX_PER_USER", "0"))
# How often a queued run checks for a free slot.
RUN_QUEUE_POLL_SECONDS = float(os.getenv("RUN_QUEUE_POLL_SECONDS", "3"))

STOPPED_WHILE_QUEUED = "Stopped while queued; it never started."

_QUEUE_FIELDS = ["run_id", "user_id", "queued_at", "started_at", "stop_requested"]
_RUNNING_FIELDS = ["run_id", "user_id"]


def enabled() -> bool:
    return RUN_MAX_CONCURRENT > 0 or RUN_MAX_PER_USER > 0


def applies(registry) -> bool:
    """Limits are set and there is a shared database to count in."""
    return enabled() and registry._get_db() is not None


def _queue_key(doc) -> tuple:
    return (doc.get("queued_at") or doc.get("started_at") or "", doc.get("run_id") or "")


def _find(registry, status: str, fields: List[str]) -> List[dict]:
    from services.run_registry import RUN_REGISTRY_DB
    return registry._get_db().find(RUN_REGISTRY_DB, selector={"status": status},
                                   fields=fields, limit=None)


# The queue and running docs as one worker last read them. Every queued run
# on a worker polls; they share one read per poll instead of one each.
_snapshot_lock = threading.Lock()
_snapshot: Dict[str, object] = {"at": 0.0, "queued": [], "running": []}


def _read_state(registry, fresh: bool):
    with _snapshot_lock:
        if not fresh and time.monotonic() - _snapshot["at"] < RUN_QUEUE_POLL_SECONDS / 2:
            return _snapshot["queued"], _snapshot["running"]
        queued = _find(registry, "queued", _QUEUE_FIELDS)
        running = _find(registry, "running", _RUNNING_FIELDS)
        _snapshot.update(at=time.monotonic(), queued=queued, running=running)
        return queued, running


def reset_for_tests() -> None:
    with _snapshot_lock:
        _snapshot.update(at=0.0, queued=[], running=[])


def _over(running: Iterable[dict], user_id: str) -> bool:
    running = list(running)
    if RUN_MAX_CONCURRENT and len(running) > RUN_MAX_CONCURRENT:
        return True
    if RUN_MAX_PER_USER and sum(1 for d in running if d.get("user_id") == user_id) > RUN_MAX_PER_USER:
        return True
    return False


def _my_turn(record, queued: List[dict], running: List[dict]) -> bool:
    """Would the free slots, handed out oldest first, reach this run?"""
    if not any(d.get("run_id") == record.run_id for d in queued):
        # Not in what was read yet: it is the newest.
        queued = queued + [{"run_id": record.run_id, "user_id": record.user_id,
                            "queued_at": record.queued_at.isoformat() if record.queued_at else ""}]
    free = RUN_MAX_CONCURRENT - len(running) if RUN_MAX_CONCURRENT else len(queued)
    per_user = Counter(d.get("user_id") for d in running)
    for d in sorted(queued, key=_queue_key):
        if free <= 0:
            return False
        if d.get("stop_requested"):
            continue
        owner = d.get("user_id")
        if RUN_MAX_PER_USER and per_user[owner] >= RUN_MAX_PER_USER:
            continue
        if d.get("run_id") == record.run_id:
            return True
        free -= 1
        per_user[owner] += 1
    return False


def try_start(registry, record, fresh: bool = True) -> bool:
    """Move a queued run to running if a slot is free and it is its turn.

    Blocking (database calls). True when the run may start now.
    """
    from services.run_registry import WORKER_ID
    if record.status != "queued":
        return record.status == "running"
    queued, running = _read_state(registry, fresh)
    if not _my_turn(record, queued, running):
        return False

    started = naive_utc_now().isoformat()

    def claim(stored):
        if not stored or stored.get("status") != "queued" or stored.get("stop_requested"):
            return None
        stored["status"] = "running"
        stored["started_at"] = started
        stored["worker_id"] = WORKER_ID
        return stored

    if not registry._update_run_doc(record.run_id, claim):
        return False
    # Count again now that this run's own write is in: whoever writes last
    # sees everyone else.
    try:
        over = _over(_find(registry, "running", _RUNNING_FIELDS), record.user_id)
    except Exception:
        over = True
    if over:
        def back(stored):
            if not stored or stored.get("status") != "running" or stored.get("started_at") != started:
                return None
            stored["status"] = "queued"
            stored["started_at"] = stored.get("queued_at") or started
            return stored
        try:
            if registry._update_run_doc(record.run_id, back):
                return False
        except Exception as e:
            print(f"[run_admission] could not requeue {record.run_id}: {e}", flush=True)
        # Could not move it back: it holds a slot now, so it runs.
    from datetime import datetime
    record.status = "running"
    record.started_at = datetime.fromisoformat(started)
    with _snapshot_lock:
        _snapshot["at"] = 0.0
    return True


def admit(registry, record) -> None:
    """Start a just-queued run at once when a slot is free. Best-effort."""
    try:
        try_start(registry, record, fresh=True)
    except Exception as e:
        print(f"[run_admission] admit failed for {record.run_id}: {e}", flush=True)


async def wait_turn(registry, record, cancel_token=None) -> bool:
    """Wait until a queued run may start. True: start it now. False: it was
    stopped while queued, and is recorded as stopped."""
    from services.run_registry import WORKER_ID
    while record.status == "queued":
        stopped = record.stop_requested or (cancel_token is not None and cancel_token.is_stopped())
        if not stopped:
            try:
                doc = await asyncio.to_thread(registry.fetch_doc, record.run_id)
            except Exception as e:
                print(f"[run_admission] queue poll failed for {record.run_id}: {e}", flush=True)
                await asyncio.sleep(RUN_QUEUE_POLL_SECONDS)
                continue
            status = (doc or {}).get("status")
            if status == "running" and doc.get("worker_id") == WORKER_ID:
                # Claimed, but the claim's answer was lost: it is ours to run.
                record.status = "running"
                return True
            stopped = doc is None or status != "queued" or bool(doc.get("stop_requested"))
        if stopped:
            registry.mark_complete(record, status="stopped", error=STOPPED_WHILE_QUEUED)
            return False
        try:
            if await asyncio.to_thread(try_start, registry, record, False):
                return True
        except Exception as e:
            print(f"[run_admission] start attempt failed for {record.run_id}: {e}", flush=True)
        await asyncio.sleep(RUN_QUEUE_POLL_SECONDS)
    return record.status == "running"


def positions(registry, run_ids: Iterable[str]) -> Dict[str, int]:
    """How many queued runs are ahead of each of these queued runs."""
    run_ids = set(run_ids)
    if not run_ids or registry._get_db() is None:
        return {}
    try:
        queued = [d for d in _find(registry, "queued", _QUEUE_FIELDS) if not d.get("stop_requested")]
    except Exception:
        return {}
    order = [d.get("run_id") for d in sorted(queued, key=_queue_key)]
    return {rid: i for i, rid in enumerate(order) if rid in run_ids}
