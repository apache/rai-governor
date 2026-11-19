"""Per-user and per-IP request limits, counted in this process's memory.

Fixed windows: each (limit, key) pair may make ``count`` requests per
``period``; the next one gets 429 with Retry-After until the window ends.
Counters live in each gunicorn worker, so with N workers a client can get up
to N times a limit. That is enough to stop a script hammering an endpoint,
which is all these are for.

Routes take a limit as a dependency:

    @router.post("/x", dependencies=[per_user("run_start", get_current_user)])

``per_user`` keys on the signed-in user's uid and is given the route's own
user dependency, so FastAPI resolves (and caches) it once, and test overrides
of it apply here too.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from fastapi import Depends, HTTPException, Request

from services.observability_service import get_observability_service

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True)
class Limit:
    count: int
    period: int  # seconds

    def __str__(self) -> str:
        unit = next((u for u, s in _UNITS.items() if s == self.period), None)
        return f"{self.count}/{unit}" if unit else f"{self.count}/{self.period}s"


def parse_limit(value: str) -> Limit:
    """``"30/minute"`` -> Limit(30, 60). Units: second, minute, hour, day."""
    count, _, unit = value.strip().partition("/")
    period = _UNITS.get(unit.strip().lower().rstrip("s"))
    if period is None or not count.strip().isdigit() or int(count) < 1:
        raise ValueError(f"not a rate limit: {value!r} (want e.g. 30/minute)")
    return Limit(int(count), period)


def _env_limit(name: str, default: str) -> Limit:
    var = f"RATE_LIMIT_{name.upper()}"
    raw = os.getenv(var, default)
    try:
        return parse_limit(raw)
    except ValueError as e:
        # A typo in the environment shouldn't stop the API from starting.
        get_observability_service().log(
            level="WARNING", event_type="config_error",
            message=f"{var}: {e}; using {default}",
        )
        return parse_limit(default)


ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

# Generous: a person using the UI should never meet these.
LIMITS = {
    # Sign-in start and callback per client IP; a sign-in is two requests.
    "login": _env_limit("login", "30/minute"),
    # Browser log events, per user (or per IP before sign-in).
    "client_log": _env_limit("client_log", "120/minute"),
    # Starting an agent run, a deployed agent or a workload.
    "run_start": _env_limit("run_start", "60/hour"),
    "memory_search": _env_limit("memory_search", "120/minute"),
    "memory_recall": _env_limit("memory_recall", "30/minute"),
    # Writing, reviewing and triaging memory entries.
    "memory_write": _env_limit("memory_write", "60/minute"),
    "data_store_write": _env_limit("data_store_write", "120/minute"),
    "pipeline_import": _env_limit("pipeline_import", "60/hour"),
}

# How many proxies append to X-Forwarded-For in front of the API: Apache,
# then nginx. The client is the address the outermost one appended; entries
# left of it are whatever the client sent and can't be trusted.
PROXY_HOPS = int(os.getenv("RATE_LIMIT_PROXY_HOPS", "2"))

# Keys kept per process before the least recently used are dropped.
MAX_KEYS = int(os.getenv("RATE_LIMIT_MAX_KEYS", "10000"))


class RateLimiter:
    """Fixed-window counters with LRU eviction; safe across threads."""

    def __init__(self, max_keys: int = MAX_KEYS, clock: Callable[[], float] = time.monotonic):
        self.max_keys = max_keys
        self.clock = clock
        self._windows: "OrderedDict[Tuple[str, str], list]" = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, name: str, key: str, limit: Limit) -> Tuple[bool, int, bool]:
        """Count one request. Returns (allowed, seconds until the window
        ends, whether this is the first refusal in the window)."""
        now = self.clock()
        k = (name, key)
        with self._lock:
            w = self._windows.get(k)
            if w is None or now >= w[0] + limit.period:
                w = [now, 0]
                self._windows[k] = w
            self._windows.move_to_end(k)
            while len(self._windows) > self.max_keys:
                self._windows.popitem(last=False)
            w[1] += 1
            retry = max(1, math.ceil(w[0] + limit.period - now))
            return w[1] <= limit.count, retry, w[1] == limit.count + 1

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()

    def __len__(self) -> int:
        return len(self._windows)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """The client's address as the outermost trusted proxy saw it.

    uvicorn (forwarded_allow_ips="*") takes the leftmost X-Forwarded-For
    entry, which the client can set, so it isn't used as a key here.
    """
    hops = [h.strip() for v in request.headers.getlist("x-forwarded-for")
            for h in v.split(",") if h.strip()]
    if hops and PROXY_HOPS > 0:
        # Fewer entries than proxies: not behind the full chain (dev).
        return hops[-PROXY_HOPS] if len(hops) >= PROXY_HOPS else hops[0]
    return request.client.host if request.client else "unknown"


def check(name: str, key: str) -> None:
    """Count a request against limit ``name``; 429 when over it."""
    if not ENABLED:
        return
    limit = LIMITS[name]
    allowed, retry, first_refusal = limiter.hit(name, key, limit)
    if allowed:
        return
    if first_refusal:
        # Once per window and key, so a flood doesn't flood the log too.
        get_observability_service().log(
            level="WARNING", event_type="rate_limited",
            message=f"Rate limit {name} ({limit}) reached by {key}",
            metadata={"limit": name, "key": key},
        )
    raise HTTPException(
        status_code=429,
        detail=f"Too many requests. Try again in {retry} seconds.",
        headers={"Retry-After": str(retry)},
    )


def per_ip(name: str):
    async def _dep(request: Request) -> None:
        check(name, f"ip:{client_ip(request)}")
    return Depends(_dep)


def per_user(name: str, current_user: Callable):
    async def _dep(user: dict = Depends(current_user)) -> None:
        check(name, f"user:{user.get('uid')}")
    return Depends(_dep)


def per_user_or_ip(name: str):
    """For routes open to everyone: the session user the observability
    middleware found, else the client IP."""
    async def _dep(request: Request) -> None:
        user = getattr(request.state, "user", None)
        uid: Optional[str] = user.get("uid") if isinstance(user, dict) else None
        check(name, f"user:{uid}" if uid else f"ip:{client_ip(request)}")
    return Depends(_dep)
