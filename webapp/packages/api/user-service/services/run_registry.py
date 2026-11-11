"""Run registry — in-process record of agent runs (ISSUE-003).

A ``RunRecord`` holds everything the runs UX needs to display, replay, and
(in ISSUE-007) cancel an in-flight or recently-completed agent run. The
``RunRegistry`` is a singleton (per process) that owns the records.

Design constraints:

- **Single process.** Records live in memory. A multi-replica deployment
  would need sticky sessions or a shared store; that's out of scope for
  Phase 1 (issue text calls this out explicitly).
- **Closing the SSE connection does not kill the run.** Each record's
  asyncio Task runs independently of any subscriber. The registry holds
  a strong reference so the task isn't GC'd. The SSE response is a thin
  subscriber that reads events from a per-record subscriber queue.
- **Replay-then-live.** A new subscriber sees every event already in
  ``record.trace.events`` (replay), then transitions to live consumption
  via its own queue. Multiple subscribers per record are supported —
  each gets its own queue, attached to the trace via ``Trace.attach_queue``
  semantics. (Today Trace only supports one queue; this module wraps that
  by fanning out at append-time. See ``_FanoutTrace`` below.)
- **Eviction.** On completion, the record stays in memory for
  ``EVICTION_TTL_SECONDS`` (default 3600). After that, it's evicted —
  callers asking for it get 404. Persistence to CouchDB is a follow-up.
- **Auth scoping.** Records track ``user_id``. Lookup-by-id callers must
  check ownership before exposing the record. (After ISSUE-002 lands,
  widen to workspace membership.)
"""
from __future__ import annotations

import asyncio
import threading
import time
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Literal, Optional, Set

from services.agent_trace import Trace
from time_utils import naive_utc_now


# ISSUE-007 persistence: each worker process gets a stable identity
# generated at module import. Used to mark which worker owns each
# in-flight run so the stop-polling task can scan only its own runs
# in CouchDB rather than every running record in the registry. The
# value persists for the lifetime of the worker process and is lost
# on restart, which is the correct behavior -- a restarted worker
# is a different worker as far as cancel-token ownership is
# concerned, since the in-memory token dict is gone.
WORKER_ID = uuid.uuid4().hex

# Test-mode flag: when reset_run_registry_for_tests is called, this
# flips to True for the rest of the process lifetime. Any RunRegistry
# constructed while it's True skips the DB lookup in _get_db and
# operates purely in-memory. Lets unit tests exercise the registry
# without standing up CouchDB and without test records contaminating
# a real DB.
_test_mode_no_db = False

# Local in-memory cache TTL for completed records. The CouchDB
# record is the source of truth for completed runs; we keep a brief
# local cache so back-to-back reads (typical when a UI page renders
# right after completion) don't re-fetch.
LOCAL_CACHE_TTL_SECONDS = 300  # 5 minutes

# Backwards-compat alias for the constant the pre-persistence code
# called EVICTION_TTL_SECONDS. The name was specific to the old
# meaning -- a single in-memory eviction TTL of 1 hour -- which now
# split into LOCAL_CACHE_TTL_SECONDS (in-memory cache evict) and
# COUCH_RETENTION_SECONDS (CouchDB eviction). External callers
# (notably tests/unit/services/test_run_registry.py) imported the
# old name; keep it pointing at the local cache TTL so existing
# call sites that backdate _completed_at_monotonic past this value
# still trigger eviction the way they expect.
EVICTION_TTL_SECONDS = LOCAL_CACHE_TTL_SECONDS

# CouchDB retention for completed runs. Runs older than this get
# evicted by the periodic eviction task in app_factory's lifespan.
COUCH_RETENTION_SECONDS = 7 * 24 * 3600  # 7 days

# CouchDB database that holds run records. Separate DB from the
# data store so each can be backed up, queried, and reasoned about
# independently. Created on first write via the standard
# ensure-db-exists behavior of the database service.
RUN_REGISTRY_DB = "gofannon_runs"

DONE_SENTINEL = object()

# ISSUE-022: worker liveness. Each worker writes a heartbeat doc every
# WORKER_HEARTBEAT_SECONDS. A run still marked "running" whose worker has
# not written one for WORKER_DEAD_AFTER_SECONDS belongs to a worker that is
# gone (restart, deploy, crash), so it can never finish and is marked error.
WORKERS_DB = "gofannon_workers"
WORKER_HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", "30"))
WORKER_DEAD_AFTER_SECONDS = float(os.getenv("WORKER_DEAD_AFTER_SECONDS", "120"))
# Heartbeat docs of workers silent this long are deleted.
WORKER_DOC_RETENTION_SECONDS = 24 * 3600
ORPHANED_RUN_ERROR = (
    "The API worker running this agent stopped (restart, deploy or crash) "
    "before the run finished. Nothing more will arrive; run it again."
)


# Indexes created on first persistence call. Idempotent at the
# database_service level, but we still guard with a module flag to
# skip the call entirely on subsequent uses.
_indexes_ensured = False


# Most run docs one owner's list reads. Retention (days) keeps an owner well
# under it; an int, because some backends' find() takes no None.
LIST_SCAN_CAP = 10000


def _ensure_indexes(db) -> None:
    """Create the indexes the registry needs. Idempotent."""
    global _indexes_ensured
    if _indexes_ensured:
        return
    # Polling task: 'find my owned in-flight runs that need stopping'.
    db.ensure_index(RUN_REGISTRY_DB, ["worker_id", "status"],
                    index_name="by_worker_status")
    # List endpoint: 'find runs for this user, optionally filtered by
    # agent_id, ordered by started_at descending'.
    db.ensure_index(RUN_REGISTRY_DB, ["user_id", "agent_id", "started_at"],
                    index_name="by_user_agent")
    # Eviction: 'find completed runs older than N days'.
    db.ensure_index(RUN_REGISTRY_DB, ["status", "completed_at"],
                    index_name="by_status_completed")
    # List endpoint, a project's runs (ISSUE-002).
    db.ensure_index(RUN_REGISTRY_DB, ["project", "started_at"],
                    index_name="by_project")
    _indexes_ensured = True


class _FanoutTrace(Trace):
    """A Trace that supports multiple attached subscriber queues.

    The base ``Trace.attach_queue`` overwrites the single queue slot. We
    need fanout for the registry: a long-lived "live" queue is consumed
    by SSE subscribers, and new subscribers also get the existing
    ``self.events`` list replayed before going live. Each subscriber's
    own queue is set via ``add_subscriber``; removal via
    ``remove_subscriber``.

    Path A integration: each subscriber tracks the loop that owns its
    queue. When append() fires from a thread other than the queue\'s
    owning loop (the executor runs the agent on a side thread), the
    put_nowait is routed through call_soon_threadsafe so the queue is
    only mutated by its owning loop. When no loop is provided
    (legacy callers), behavior is the original direct put_nowait.
    """

    def __init__(self) -> None:
        super().__init__()
        # subscriber → optional owning_loop. None means "emit from any
        # thread without bridging" (the legacy code path before
        # Path A).
        self._subscribers: Dict[asyncio.Queue, Optional[asyncio.AbstractEventLoop]] = {}

    def add_subscriber(
        self,
        queue: asyncio.Queue,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._subscribers[queue] = loop

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)

    def append(self, event: Dict[str, Any]) -> None:
        # Append to the immutable history first (parent stores in
        # self.events); the parent publishes, and _publish fans out.
        super().append(event)

    def _publish(self, event: Dict[str, Any]) -> None:
        # Every published event reaches every subscriber, the cap's
        # trace_truncated marker included.
        super()._publish(event)
        for q, loop in list(self._subscribers.items()):
            if loop is not None:
                # Route the put via the queue\'s owning loop. Safe
                # even when we\'re already on that loop\'s thread.
                loop.call_soon_threadsafe(self._safe_subscriber_put, q, event)
            else:
                try:
                    q.put_nowait(event)
                except Exception:
                    # Queue full or closed; drop the event for that
                    # subscriber rather than blocking other emitters.
                    pass

    @staticmethod
    def _safe_subscriber_put(queue: asyncio.Queue, event: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except Exception:
            pass


@dataclass
class RunRecord:
    """One agent run's full state."""

    run_id: str
    user_id: str
    agent_name: str
    # CouchDB doc id of the saved agent this run came from, when applicable.
    # None for runs initiated from the create-flow sandbox (no saved agent yet).
    # Used by the runs UI to deep-link to the per-agent runs page.
    agent_id: Optional[str] = None
    # The input dict the run was launched with. Stored so the runs UI
    # can pre-fill the form when revisiting a past run via deep link.
    # None for old records that predate this field.
    input_dict: Optional[Dict[str, Any]] = None
    # The project the run is for (ISSUE-002); its members see and stop it.
    project: Optional[str] = None
    started_at: datetime = field(default_factory=naive_utc_now)
    status: Literal["running", "success", "error", "stopped"] = "running"
    trace: _FanoutTrace = field(default_factory=_FanoutTrace)
    task: Optional[asyncio.Task] = None  # Set by the registry on register
    result: Optional[Any] = None
    error: Optional[str] = None
    schema_warnings: Optional[List[str]] = None
    ops_log: Optional[List[Dict[str, Any]]] = None
    completed_at: Optional[datetime] = None
    # Wall-clock timestamp (time.monotonic) for cheap eviction comparison.
    _completed_at_monotonic: Optional[float] = None
    # ISSUE-007 persistence: which worker owns the live cancel token
    # for this run. Used by the stop-polling task to scope its
    # CouchDB query to runs this worker can actually act on. When a
    # record is loaded from CouchDB on a non-owning worker (e.g. for
    # display in the runs UI) this field reflects the persisted
    # owner, not the local worker.
    worker_id: str = field(default_factory=lambda: WORKER_ID)
    # ISSUE-007 cross-worker stop: the stop endpoint flips this to
    # True on the CouchDB record. The owning worker's polling task
    # observes the change and flips its local cancel token. For
    # local fast-path stops (when the stop request lands on the
    # owning worker) the cancel token is flipped immediately and
    # stop_requested is still written to CouchDB so the record
    # reflects what happened.
    stop_requested: bool = False
    # Flag used by mark_complete to skip re-persisting an already-
    # completed record (defensive against double-call).
    _persisted_complete: bool = False
    # How many trace events the last checkpoint wrote (ISSUE-017).
    _checkpointed_events: int = 0
    # True for a copy read from the database: it has no live trace, even
    # when its worker_id is this worker's (a run from before a cache
    # eviction, or one this process no longer runs).
    _from_store: bool = False

    def to_summary(self) -> Dict[str, Any]:
        """Subset suitable for list endpoints — no events, no result body.

        Does include inputDict and error so the per-agent past-runs UI
        can render an input preview and inline error message without
        firing N+1 full-record fetches for each row.
        """
        return {
            "runId": self.run_id,
            "agentId": self.agent_id,
            "agentName": self.agent_name,
            "project": self.project,
            "status": self.status,
            "startedAt": self.started_at.isoformat(),
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "inputDict": self.input_dict,
            "error": self.error,
        }

    def to_full(self) -> Dict[str, Any]:
        return {
            **self.to_summary(),
            "events": list(self.trace.events),
            "result": self.result,
            "error": self.error,
            "schemaWarnings": self.schema_warnings,
            "opsLog": self.ops_log,
            # inputDict echoes the input the run was launched with so
            # the runs UI can pre-fill its form when a user revisits a
            # past run via deep link.
            "inputDict": self.input_dict,
        }


    def to_couch_doc(self, include_events: bool = False) -> Dict[str, Any]:
        """Serialize for CouchDB. Only persistable fields; no asyncio
        Task, no live Trace queues, no cancel-token reference (the
        token lives in run_cancel_registry keyed by run_id).

        include_events controls whether the trace events list ships
        with the doc. Set True only on completion -- in-flight writes
        skip events to keep the per-write payload small (events
        accumulate during the run and can reach tens of KB; writing
        them on every state change would create amplification).
        """
        doc = {
            "_id": self.run_id,
            "type": "run",
            "run_id": self.run_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "project": self.project,
            "input_dict": self.input_dict,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status,
            "worker_id": self.worker_id,
            "stop_requested": self.stop_requested,
            "result": self.result,
            "error": self.error,
            "schema_warnings": self.schema_warnings,
            "ops_log": self.ops_log,
        }
        if include_events:
            doc["events"] = list(self.trace.events)
        return doc

    @classmethod
    def from_couch_doc(cls, doc: Dict[str, Any]) -> "RunRecord":
        """Reconstruct from a CouchDB document.

        The reconstructed record is *view-only*: it has no task, no
        cancel token, and its Trace is a fresh fanout instance pre-
        populated with the persisted events (so SSE replay still
        works). Live-tailing an in-flight run owned by another worker
        is not supported; callers should treat such records as
        readable but not subscribable.
        """
        from datetime import datetime as _dt
        rec = cls(
            run_id=doc["run_id"],
            user_id=doc["user_id"],
            agent_name=doc["agent_name"],
            agent_id=doc.get("agent_id"),
            project=doc.get("project"),
            input_dict=doc.get("input_dict"),
        )
        # Override defaults set by dataclass init
        rec.started_at = _dt.fromisoformat(doc["started_at"])
        if doc.get("completed_at"):
            rec.completed_at = _dt.fromisoformat(doc["completed_at"])
        rec.status = doc.get("status", "running")
        rec.worker_id = doc.get("worker_id", "")
        rec.stop_requested = bool(doc.get("stop_requested", False))
        rec.result = doc.get("result")
        rec.error = doc.get("error")
        rec.schema_warnings = doc.get("schema_warnings")
        rec.ops_log = doc.get("ops_log")
        # Populate the trace with persisted events so SSE replay works
        # for completed-run deep links from other workers.
        events = doc.get("events") or []
        if events:
            rec.trace.events.extend(events)
        rec._persisted_complete = doc.get("status") in ("success", "error", "stopped")
        rec._from_store = True
        return rec


class RunRegistry:
    """Registry of RunRecords backed by CouchDB.

    Local in-memory cache holds the full record (including the live
    Trace with subscriber queues, the asyncio Task, and the cancel
    token reference) only on the worker that owns the in-flight run.
    Other workers that need to *display* a run read its CouchDB doc
    and reconstruct a view-only RunRecord via
    ``RunRecord.from_couch_doc``.

    Stop semantics:
        POST /runs/<id>/stop hits some worker (chosen by the load
        balancer). That worker calls ``request_stop``, which writes
        stop_requested=true to the CouchDB doc. If the worker also
        owns the local cancel token (fast path) it flips the token
        immediately. The owning worker's polling task
        (``poll_owned_stops`` in app_factory's lifespan) reads its
        own in-flight runs and flips local tokens for any newly
        flagged.
    """

    def __init__(self, db=None) -> None:
        # In-memory cache. For OWNED records this holds the live
        # RunRecord with task / trace / etc. For records reconstructed
        # from CouchDB on this worker (e.g. for read endpoints) we
        # cache briefly to avoid re-fetching on close-together reads
        # by the polling UI.
        self._records: Dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()
        # Lazy DB binding; set on first persistence operation when
        # the caller has access to settings. Tests can pass None.
        self._db = db
        # Test-mode short-circuit: if the test fixture flipped the
        # module flag, never try to look up a DB. Lets unit tests
        # exercise the in-memory path even now that the production
        # 'from config import settings' import actually succeeds in
        # the test environment.
        self._db_disabled = (db is None) and _test_mode_no_db
        # Track which run_ids were stop-flipped locally so the polling
        # task doesn't re-flip and so we can stop polling them.
        self._stop_flipped: set = set()
        # Serialises read-modify-write of run docs within this process: the
        # event checkpoint runs in a thread while completion runs on the
        # event loop. Across processes, CouchDB's _rev check plus the retry
        # in _update_run_doc does the same job.
        self._doc_lock = threading.Lock()

    # --- DB binding helpers -------------------------------------

    def _get_db(self):
        """Return the DatabaseService, lazily initializing on first use."""
        if self._db_disabled:
            return None
        if self._db is not None:
            return self._db
        try:
            from config import settings
            from services.database_service import get_database_service
            self._db = get_database_service(settings)
            _ensure_indexes(self._db)
        except Exception:
            # If DB isn't reachable at startup we degrade to
            # in-memory-only behavior. Tests use this path; prod
            # callers will hit a different error before this point.
            self._db = None
        return self._db

    def _update_run_doc(self, run_id: str, mutate, attempts: int = 5) -> bool:
        """Read the run doc, apply ``mutate``, save; retry on a write conflict.

        ``mutate`` gets the stored doc (or None when there is none) and
        returns the doc to save, or None to leave it alone. Returns True when
        a doc was saved. Every write to a run doc goes through here, so the
        event checkpoint, completion, stop requests and the orphan sweep
        cannot silently undo each other: a writer that loses the race reads
        the new revision and decides again.
        """
        db = self._get_db()
        if db is None:
            return False
        with self._doc_lock:
            for attempt in range(attempts):
                try:
                    stored = db.get(RUN_REGISTRY_DB, run_id)
                except Exception as e:
                    if getattr(e, "status_code", None) != 404:
                        raise
                    stored = None
                doc = mutate(dict(stored) if stored else None)
                if doc is None:
                    return False
                if stored and stored.get("_rev"):
                    doc["_rev"] = stored["_rev"]
                else:
                    doc.pop("_rev", None)
                try:
                    db.save(RUN_REGISTRY_DB, run_id, doc)
                    return True
                except Exception as e:
                    if getattr(e, "status_code", None) == 409 and attempt < attempts - 1:
                        continue
                    raise
        return False

    def _persist(self, record: RunRecord, include_events: bool = False) -> None:
        """Write the record's state to the database. Best-effort.

        Keeps what other writers own on the stored doc: a stop request set
        by another worker, and events already checkpointed when this write
        carries none. A conflict is retried, so completion always lands.
        """
        def mutate(stored):
            doc = record.to_couch_doc(include_events=include_events)
            if stored:
                if stored.get("stop_requested"):
                    doc["stop_requested"] = True
                if not include_events and stored.get("events"):
                    doc["events"] = stored["events"]
            return doc

        try:
            self._update_run_doc(record.run_id, mutate)
        except Exception as e:
            # Defensive: persistence failure mustn't break run-time
            # behavior. The in-memory record stays consistent.
            print(f"[run_registry] persist failed for {record.run_id}: {e}",
                  flush=True)

    # --- Public API ---------------------------------------------

    def new_record(
        self,
        *,
        user_id: str,
        agent_name: str,
        agent_id: Optional[str] = None,
        input_dict: Optional[Dict[str, Any]] = None,
        project: Optional[str] = None,
    ) -> RunRecord:
        """Create a record with a fresh run_id, cache locally, and
        persist a stub to CouchDB so other workers can see it."""
        run_id = str(uuid.uuid4())
        record = RunRecord(
            run_id=run_id,
            user_id=user_id,
            agent_name=agent_name,
            agent_id=agent_id,
            input_dict=input_dict,
            project=project,
        )
        # worker_id is set by the dataclass default to WORKER_ID, so
        # this record is implicitly owned by this worker.
        record.trace.run_id = run_id
        self._records[run_id] = record
        self._persist(record, include_events=False)
        return record

    def bind_task(self, record: RunRecord, task: asyncio.Task) -> None:
        record.task = task

    def get(self, run_id: str) -> Optional[RunRecord]:
        """Return the record by run_id. Local cache first, CouchDB
        fallback. Returns None if not found in either."""
        self._evict_expired_local()
        rec = self._records.get(run_id)
        if rec is not None and not (rec._from_store and rec.status == "running"):
            return rec
        # A stored copy of a run still going elsewhere goes stale: re-read it.
        # Fall back to CouchDB. This handles cross-worker read
        # (UI loads on worker B for a run owned by worker A) and
        # post-eviction lookup (record completed and was evicted
        # from local cache but is still within the 7-day retention).
        db = self._get_db()
        if db is None:
            return None
        try:
            doc = db.get(RUN_REGISTRY_DB, run_id)
        except Exception:
            return None
        if not doc:
            return None
        rec = RunRecord.from_couch_doc(doc)
        # Cache briefly so the UI's poll-while-running loop doesn't
        # re-fetch this same doc every interval.
        rec._completed_at_monotonic = time.monotonic() if rec.completed_at else None
        self._records[run_id] = rec
        return rec

    def list_for_user(
        self,
        user_id: str,
        limit: int = 100,
        agent_id: Optional[str] = None,
        projects: Iterable[str] = (),
    ) -> List[RunRecord]:
        """List records for a user, optionally filtered by agent_id.

        ``projects`` adds the runs of those projects, whoever started them
        (the projects the user belongs to, ISSUE-002).

        Queries CouchDB (the source of truth) rather than scanning
        the local cache, so this returns runs owned by any worker.
        Records are reconstructed from CouchDB docs; they are
        view-only (no live trace).
        """
        db = self._get_db()
        if db is None:
            # In-memory fallback for tests and dev without CouchDB.
            self._evict_expired_local()
            projects = set(projects)
            out = [r for r in self._records.values()
                   if r.user_id == user_id or (r.project and r.project in projects)]
            if agent_id is not None:
                out = [r for r in out if r.agent_id == agent_id]
            out.sort(key=lambda r: r.started_at, reverse=True)
            return out[:limit]

        # One equality query per owner, merged: only CouchDB has $or, and
        # the other backends' find() compares by equality.
        selectors = [{"user_id": user_id, "type": "run"}]
        selectors += [{"project": p, "type": "run"} for p in sorted(set(projects))]
        if agent_id is not None:
            for sel in selectors:
                sel["agent_id"] = agent_id
        try:
            # find() doesn't expose Mango sort and returns in storage
            # order: take each owner's runs whole (retention keeps that to
            # days) and sort here, or an owner with more than `limit` runs
            # loses its newest. started_at is ISO-8601, so reverse string
            # order is newest first.
            by_id: Dict[str, Dict[str, Any]] = {}
            for sel in selectors:
                for d in db.find(RUN_REGISTRY_DB, selector=sel, limit=LIST_SCAN_CAP):
                    by_id[d.get("run_id") or d.get("_id")] = d
            docs = list(by_id.values())
        except Exception as e:
            print(f"[run_registry] list_for_user query failed: {e}", flush=True)
            return []
        docs.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return [RunRecord.from_couch_doc(d) for d in docs[:limit]]

    def mark_complete(
        self,
        record: RunRecord,
        *,
        status: Literal["success", "error", "stopped"],
        result: Any = None,
        error: Optional[str] = None,
        schema_warnings: Optional[List[str]] = None,
        ops_log: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if record._persisted_complete and record.status == status:
            # Defensive: mark_complete can be called twice in the
            # cancel-handling paths (once from the AgentStopped catch,
            # once from the CancelledError catch added in ISSUE-007).
            # Idempotent on identical second call.
            return
        record.status = status
        record.result = result
        record.error = error
        record.schema_warnings = schema_warnings
        record.ops_log = ops_log
        record.completed_at = naive_utc_now()
        record._completed_at_monotonic = time.monotonic()

        # Signal end-of-stream to all current subscribers.
        for q in list(record.trace._subscribers):
            try:
                q.put_nowait(DONE_SENTINEL)
            except Exception:
                pass

        # Persist final state including events. This is the big write;
        # subsequent reads from other workers see the full record.
        self._persist(record, include_events=True)
        record._persisted_complete = True

    def request_stop(self, run_id: str, user_id: str, user: Optional[dict] = None) -> str:
        """Flag the run as stop-requested.

        Returns one of:
            'flipped_local'     - we own the cancel token and flipped it
            'persisted_remote'  - wrote stop_requested to CouchDB for the
                                  owning worker to pick up via polling
            'not_found'         - no record in CouchDB or local cache
            'forbidden'         - record exists but is for another user

        The endpoint should treat anything other than 'not_found' /
        'forbidden' as success (HTTP 202).
        """
        # Try local cache first (fast path).
        from auth.project_access import can_see_run

        def allowed(owner, project):
            if user is not None:
                return can_see_run(user, owner, project)
            return owner == user_id

        local = self._records.get(run_id)
        if local is not None and not allowed(local.user_id, local.project):
            return "forbidden"

        db = self._get_db()
        doc = None
        if db is not None:
            try:
                doc = db.get(RUN_REGISTRY_DB, run_id)
            except Exception:
                doc = None
        if doc is None and local is None:
            return "not_found"
        if doc is not None and not allowed(doc.get("user_id"), doc.get("project")):
            return "forbidden"

        # Update CouchDB unconditionally so the polling task on the
        # owning worker sees the flag. Skip if we have no DB (tests).
        if db is not None and doc is not None:
            def flag(stored):
                if stored is None:
                    return None
                stored["stop_requested"] = True
                return stored
            try:
                self._update_run_doc(run_id, flag)
            except Exception as e:
                print(f"[run_registry] request_stop persist failed: {e}",
                      flush=True)

        # Local fast-path: if this worker owns the live cancel token,
        # flip it immediately so stop latency on the owning-worker
        # case is effectively zero.
        from services.run_cancel_registry import get as get_token
        token = get_token(run_id)
        if token is not None:
            token.request_stop()
            if local is not None:
                local.stop_requested = True
            self._stop_flipped.add(run_id)
            return "flipped_local"
        return "persisted_remote"

    def poll_owned_stops(self) -> int:
        """Scan CouchDB for in-flight runs this worker owns that have
        been flagged for stop. Flip local cancel tokens for any new
        ones. Returns the count of newly-flipped tokens.

        Called every few seconds by the background poller. The query
        uses the by_worker_status index so it stays fast even with
        thousands of historical records.
        """
        db = self._get_db()
        if db is None:
            return 0
        try:
            docs = db.find(
                RUN_REGISTRY_DB,
                selector={
                    "worker_id": WORKER_ID,
                    "status": "running",
                    "stop_requested": True,
                },
                limit=200,
            )
        except Exception as e:
            print(f"[run_registry] poll query failed: {e}", flush=True)
            return 0
        from services.run_cancel_registry import get as get_token
        flipped = 0
        for d in docs:
            run_id = d.get("run_id")
            if not run_id or run_id in self._stop_flipped:
                continue
            token = get_token(run_id)
            if token is None:
                # Token already cleaned up but doc still says
                # running -- the run is probably completing right
                # now. Skip; the upcoming mark_complete will write
                # the final state.
                continue
            token.request_stop()
            self._stop_flipped.add(run_id)
            local = self._records.get(run_id)
            if local is not None:
                local.stop_requested = True
            flipped += 1
        return flipped

    def is_live_here(self, record: RunRecord) -> bool:
        """True when this worker is running ``record`` and holds its live trace."""
        return (
            record.worker_id == WORKER_ID
            and not record._from_store
            and self._records.get(record.run_id) is record
        )

    def fetch_persisted(self, run_id: str) -> Optional[RunRecord]:
        """Read the run from the database, bypassing the local cache.

        None means the run does not exist. Any other failure raises, so a
        caller can tell a database blip from a missing run.
        """
        db = self._get_db()
        if db is None:
            return None
        try:
            doc = db.get(RUN_REGISTRY_DB, run_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise
        return RunRecord.from_couch_doc(doc) if doc else None

    def checkpoint_events(self) -> int:
        """Write new trace events of this worker's running runs. Returns runs written.

        Lets a stream served by another worker follow a run while it runs
        (ISSUE-017); before, events reached the database only at completion.
        Only the events field changes, and only while both the stored doc
        and the local record say "running", so a checkpoint can never undo a
        completion or a stop request.
        """
        if self._get_db() is None:
            return 0
        written = 0
        for record in list(self._records.values()):
            if record.status != "running" or not self.is_live_here(record):
                continue
            # Past the cap the kept list stops growing, so compare how many
            # events were appended, not how many are kept.
            seq = record.trace._seq
            events = list(record.trace.events)
            if seq <= record._checkpointed_events:
                continue

            def add_events(stored, record=record, events=events):
                if not stored or stored.get("status") != "running" or record.status != "running":
                    return None
                stored["events"] = events
                return stored

            try:
                saved = self._update_run_doc(record.run_id, add_events)
            except Exception as e:
                print(f"[run_registry] event checkpoint failed for {record.run_id}: {e}", flush=True)
                continue
            if saved:
                record._checkpointed_events = seq
                written += 1
        return written

    def heartbeat(self) -> None:
        """Record that this worker is alive (ISSUE-022)."""
        db = self._get_db()
        if db is None:
            return
        doc = {
            "_id": WORKER_ID,
            "type": "worker",
            "worker_id": WORKER_ID,
            "last_seen": naive_utc_now().isoformat(),
            "pid": os.getpid(),
        }
        try:
            try:
                existing = db.get(WORKERS_DB, WORKER_ID)
                if existing and existing.get("_rev"):
                    doc["_rev"] = existing["_rev"]
            except Exception:
                pass
            db.save(WORKERS_DB, WORKER_ID, doc)
        except Exception as e:
            print(f"[run_registry] heartbeat failed: {e}", flush=True)

    def reconcile_orphans(self, now=None) -> int:
        """Mark running runs owned by dead workers as error. Returns the count.

        A worker is alive when its heartbeat is newer than
        WORKER_DEAD_AFTER_SECONDS. Runs owned by this worker are never
        touched here. Runs with no recorded worker are only touched once
        they are older than the same limit, so a run that has just started
        is never caught between its first write and its worker's first
        heartbeat.
        """
        db = self._get_db()
        if db is None:
            return 0
        now = now or naive_utc_now()
        try:
            running = db.find(RUN_REGISTRY_DB, selector={"status": "running"}, limit=None)
            workers = db.find(WORKERS_DB, selector={"type": "worker"}, limit=None)
        except Exception as e:
            print(f"[run_registry] orphan sweep query failed: {e}", flush=True)
            return 0

        last_seen = {}
        for w in workers:
            try:
                seen = datetime.fromisoformat(w["last_seen"])
            except Exception:
                continue
            age = (now - seen).total_seconds()
            if age > WORKER_DOC_RETENTION_SECONDS:
                try:
                    db.delete(WORKERS_DB, w["_id"])
                except Exception:
                    pass
                continue
            last_seen[w.get("worker_id") or w["_id"]] = age

        def closable(doc) -> bool:
            worker_id = doc.get("worker_id") or ""
            if worker_id == WORKER_ID:
                return False
            if worker_id in last_seen:
                # Known worker: closed once its heartbeat is stale.
                return last_seen[worker_id] > WORKER_DEAD_AFTER_SECONDS
            # No heartbeat on record: an old record, a worker from before
            # heartbeats, or one whose first beat failed. Allow the same
            # grace from the run's start before calling it dead.
            try:
                started = datetime.fromisoformat(doc["started_at"])
            except Exception:
                return False
            return (now - started).total_seconds() > WORKER_DEAD_AFTER_SECONDS

        closed = 0
        for doc in running:
            if not closable(doc):
                continue
            run_id = doc["_id"]
            worker_id = doc.get("worker_id") or ""

            def close(stored, worker_id=worker_id):
                # Decide again on the latest revision: the run may have
                # finished, or its worker may have come back.
                if not stored or stored.get("status") != "running" or not closable(stored):
                    return None
                stored["status"] = "error"
                stored["error"] = stored.get("error") or ORPHANED_RUN_ERROR
                stored["completed_at"] = now.isoformat()
                stored["orphaned_from_worker"] = worker_id or None
                return stored

            try:
                if not self._update_run_doc(run_id, close):
                    continue
            except Exception as e:
                print(f"[run_registry] could not close orphan {run_id}: {e}", flush=True)
                continue
            cached = self._records.get(run_id)
            if cached is not None and cached._from_store:
                cached.status = "error"
                cached.error = ORPHANED_RUN_ERROR
                cached.completed_at = now
                cached._persisted_complete = True
            closed += 1
        return closed

    def evict_old_completed(self) -> int:
        """Delete CouchDB records for runs completed more than
        COUCH_RETENTION_SECONDS ago. Returns count deleted."""
        db = self._get_db()
        if db is None:
            return 0
        from datetime import timedelta
        cutoff = (naive_utc_now() - timedelta(seconds=COUCH_RETENTION_SECONDS)).isoformat()
        try:
            docs = db.find(
                RUN_REGISTRY_DB,
                selector={
                    "type": "run",
                    "status": {"$ne": "running"},
                    "completed_at": {"$lt": cutoff},
                },
                limit=500,
            )
        except Exception as e:
            print(f"[run_registry] evict query failed: {e}", flush=True)
            return 0
        deleted = 0
        for d in docs:
            run_id = d.get("run_id")
            if not run_id:
                continue
            try:
                db.delete(RUN_REGISTRY_DB, run_id)
                deleted += 1
            except Exception:
                pass
        return deleted

    def _evict_expired_local(self) -> None:
        """Drop the local in-memory cache for completed records past
        the short local TTL. CouchDB still holds them; subsequent
        reads fall through to from_couch_doc."""
        now = time.monotonic()
        expired = [
            run_id
            for run_id, r in self._records.items()
            if r._completed_at_monotonic is not None
            and (now - r._completed_at_monotonic) > LOCAL_CACHE_TTL_SECONDS
        ]
        for run_id in expired:
            self._records.pop(run_id, None)
            self._stop_flipped.discard(run_id)


_registry: Optional[RunRegistry] = None


def get_run_registry() -> RunRegistry:
    """Process-singleton accessor."""
    global _registry
    if _registry is None:
        _registry = RunRegistry()
    return _registry


def reset_run_registry_for_tests() -> None:
    """Tests-only — discard the singleton AND opt this process out of
    CouchDB persistence for the remainder of its lifetime.

    The opt-out is sticky: once any test in the process calls this,
    no subsequent RunRegistry() instance will try to talk to CouchDB.
    Lets unit tests run without standing up a real DB and prevents
    test runs from polluting a shared CouchDB instance."""
    global _registry, _test_mode_no_db
    _registry = None
    _test_mode_no_db = True
