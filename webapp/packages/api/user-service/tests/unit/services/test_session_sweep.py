"""Sessions past their lifetime or idle limit are swept, so ones never
presented again don't pile up."""

from datetime import timedelta

import pytest

from config import settings
from models.session import Session
from services import session_service as ss
from services.database_service.memory import MemoryDBService
from time_utils import naive_utc_now

pytestmark = pytest.mark.unit

NOW = naive_utc_now()


class MangoMemoryDB(MemoryDBService):
    """MemoryDBService whose find() understands {"$lt": v}, as CouchDB's does."""

    def find(self, db_name, selector, fields=None, limit=10000):
        def match(doc):
            for k, v in selector.items():
                if isinstance(v, dict) and "$lt" in v:
                    if doc.get(k) is None or not doc[k] < v["$lt"]:
                        return False
                elif doc.get(k) != v:
                    return False
            return True
        return [d for d in self.list_all(db_name) if match(d)][:limit]


def _session(db, doc_id, expires_in_h=10, unused_h=0, last_used=True):
    s = Session(id=doc_id, user_uid="asf:" + doc_id, provider_type="asf", display_name=doc_id,
                expires_at=NOW + timedelta(hours=expires_in_h),
                last_refresh_at=NOW - timedelta(hours=unused_h),
                last_used_at=(NOW - timedelta(hours=unused_h)) if last_used else None)
    db.save(ss._SESSIONS_COLLECTION, doc_id, s.model_dump(by_alias=True, mode="json"))


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ss, "_indexes_ensured", False)
    monkeypatch.setattr(settings, "AUTH_CONFIG", {"session_idle_hours": 8, "workspace_refresh_minutes": 15})
    db = MangoMemoryDB()
    _session(db, "active", unused_h=1)
    _session(db, "expired", expires_in_h=-1, unused_h=1)
    _session(db, "idle", unused_h=9)
    _session(db, "old-expired", expires_in_h=-1, last_used=False)
    return db


def _left(db):
    return sorted(db.dbs[ss._SESSIONS_COLLECTION])


def test_expired_and_idle_sessions_are_deleted(db):
    assert ss.SessionService(db).sweep_sync() == 3
    assert _left(db) == ["active"]


def test_with_no_idle_limit_only_expired_ones_go(db, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_CONFIG", {"session_idle_hours": 0})
    assert ss.SessionService(db).sweep_sync() == 2
    assert _left(db) == ["active", "idle"]


def test_every_worker_sweeping_is_harmless(db):
    # Four workers, one after another: the first deletes, the rest find nothing.
    counts = [ss.SessionService(db).sweep_sync() for _ in range(4)]
    assert counts == [3, 0, 0, 0]
    assert _left(db) == ["active"]


def test_a_session_found_but_not_ended_is_kept(db, monkeypatch):
    # Whatever the query returns, only what get_by_id would refuse is deleted.
    monkeypatch.setattr(MangoMemoryDB, "find", lambda self, *a, **k: self.list_all(ss._SESSIONS_COLLECTION))
    ss.SessionService(db).sweep_sync()
    assert _left(db) == ["active"]


def _run_lifespan(monkeypatch, providers):
    """Start and stop the app's lifespan; how many times a sweep ran."""
    import asyncio
    import random

    import app_factory
    import auth
    import services.session_service

    swept = []

    class Fake:
        def sweep_sync(self):
            swept.append(1)
            return 0

    monkeypatch.setattr(services.session_service, "get_session_service", lambda db: Fake())
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0 if a > 0 else -3600.0)
    registry = auth.ProviderRegistry([])
    monkeypatch.setattr(registry, "has_any", lambda: providers)
    monkeypatch.setattr(auth, "get_registry", lambda: registry)

    async def go():
        async with app_factory.lifespan(None):
            for _ in range(20):
                await asyncio.sleep(0.01)

    asyncio.run(go())
    return len(swept)


def test_each_worker_starts_the_sweep_when_sign_in_is_configured(monkeypatch):
    assert _run_lifespan(monkeypatch, True) >= 1


def test_no_sweep_without_sign_in_providers(monkeypatch):
    assert _run_lifespan(monkeypatch, False) == 0
