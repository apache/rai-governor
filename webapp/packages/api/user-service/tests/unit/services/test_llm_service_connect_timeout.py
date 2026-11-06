"""ISSUE-021: a separate, short connect limit on LLM calls.

With one scalar timeout, an upstream that drops SYNs held each attempt for
the whole LLM_TIMEOUT_SECONDS (600 s by default, three attempts) before
failing. call_llm and stream_llm now pass httpx.Timeout with a connect
limit of LLM_CONNECT_TIMEOUT_SECONDS (10 s by default).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services import llm_service


class _Response:
    def __init__(self):
        msg = SimpleNamespace(content="ok", tool_calls=None)
        self.choices = [SimpleNamespace(message=msg)]
        self.usage = SimpleNamespace(total_cost=0.0)


def _capture():
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Response()

    return seen, fake


@pytest.mark.asyncio
async def test_call_llm_sends_a_short_connect_limit(monkeypatch):
    seen, fake = _capture()
    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: Mock())
    monkeypatch.setattr(llm_service, "LLM_CONNECT_TIMEOUT", 10.0)

    await llm_service.call_llm(
        provider="openai",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        parameters={},
        user_service=Mock(),
        user_id="u",
        timeout=600,
    )

    t = seen["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert (t.connect, t.read, t.write, t.pool) == (10.0, 600, 600, 600)


def test_connect_limit_never_exceeds_the_total(monkeypatch):
    monkeypatch.setattr(llm_service, "LLM_CONNECT_TIMEOUT", 10.0)
    assert llm_service._http_timeout(3).connect == 3
    assert llm_service._http_timeout(600).connect == 10.0


@pytest.mark.asyncio
async def test_stream_llm_sets_a_timeout_and_keeps_a_callers_total(monkeypatch):
    seen, _ = _capture()

    async def fake_stream(**kwargs):
        seen.update(kwargs)

        async def gen():
            if False:
                yield None

        return gen()

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake_stream)
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: Mock())
    monkeypatch.setattr(llm_service, "LLM_CONNECT_TIMEOUT", 10.0)

    async for _ in llm_service.stream_llm(
        provider="openai",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        parameters={"timeout": 120},
        user_service=Mock(),
        user_id="u",
    ):
        pass

    t = seen["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert (t.connect, t.read) == (10.0, 120.0)
