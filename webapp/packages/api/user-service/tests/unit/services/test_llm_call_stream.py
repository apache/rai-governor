"""call_llm(stream=True): streamed on the wire, returned in one piece.

A long non-streamed call sends nothing until it finishes; proxies and NAT
gateways drop connections that sit idle. Streaming keeps data flowing, and
call_llm reassembles the chunks so callers still get (content, thoughts).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from litellm.types.utils import (
    ChatCompletionDeltaToolCall, Delta, Function, ModelResponseStream,
    StreamingChoices, Usage,
)

from services import llm_service

PROVIDER, MODEL = "bedrock", "us.anthropic.claude-sonnet-4-6"


def _chunk(finish=None, usage=None, **delta):
    c = ModelResponseStream(
        id="s1", model="claude-sonnet-4-5",
        choices=[StreamingChoices(index=0, delta=Delta(**delta), finish_reason=finish)],
    )
    if usage:
        c.usage = Usage(**usage)
    return c


ANSWER = [
    _chunk(role="assistant", content="", reasoning_content="Think. "),
    _chunk(reasoning_content="Done thinking."),
    _chunk(content="Hello "),
    _chunk(content="world", finish="stop"),
    _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}),
]


async def _agen(items, fail_after=None, delay=0.0):
    for i, item in enumerate(items):
        if fail_after is not None and i == fail_after:
            raise llm_service.litellm.InternalServerError(
                message="Connection error.", llm_provider="bedrock", model=MODEL)
        if delay:
            await asyncio.sleep(delay)
        yield item


class _Resp:
    def __init__(self):
        msg = SimpleNamespace(content="plain", tool_calls=None, reasoning_content=None)
        self.choices = [SimpleNamespace(message=msg)]
        self._hidden_params = {}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: Mock())
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        # Backoff sleeps are skipped; the deadline test's slow stream is not.
        await real_sleep(0 if s >= 1 else s)

    monkeypatch.setattr(llm_service.asyncio, "sleep", fast_sleep)


def _user():
    us = Mock()
    us.get_effective_api_key.return_value = None
    return us


async def _call(**extra):
    us = extra.pop("user_service", None) or _user()
    out = await llm_service.call_llm(
        provider=PROVIDER, model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        parameters={"max_tokens": 100},
        user_service=us, user_id="u", **extra,
    )
    return out, us


@pytest.mark.asyncio
async def test_streamed_call_returns_the_same_shape(env, monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _agen(ANSWER)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, thoughts), us = await _call(stream=True, timeout=900)

    assert content == "Hello world"
    assert thoughts == {"reasoning_content": "Think. Done thinking."}
    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}
    # Reads may wait the idle limit; the call's timeout bounds the whole.
    assert seen["timeout"].read == min(llm_service.LLM_STREAM_IDLE_TIMEOUT, 900)
    us.add_usage.assert_called_once()
    assert us.add_usage.call_args.args[1] > 0


@pytest.mark.asyncio
async def test_streamed_tool_calls_come_back_assembled(env, monkeypatch):
    parts = [
        _chunk(role="assistant", tool_calls=[ChatCompletionDeltaToolCall(
            id="t1", type="function", index=0, function=Function(name="f", arguments='{"a":'))]),
        _chunk(tool_calls=[ChatCompletionDeltaToolCall(index=0, function=Function(arguments="1}"))]),
        _chunk(finish="tool_calls"),
    ]

    async def fake(**kwargs):
        return _agen(parts)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (_, thoughts), _ = await _call(stream=True)
    call = thoughts["tool_calls"][0]
    assert call["function"] == {"name": "f", "arguments": '{"a":1}'}


@pytest.mark.asyncio
async def test_model_config_turns_streaming_on_and_the_argument_wins(env, monkeypatch):
    modes = []

    async def fake(**kwargs):
        modes.append(bool(kwargs.get("stream")))
        return _agen(ANSWER) if kwargs.get("stream") else _Resp()

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    cfg = llm_service.PROVIDER_CONFIG[PROVIDER]["models"][MODEL]
    await _call()                                   # config default: off
    monkeypatch.setitem(cfg, "stream", True)
    await _call()                                   # config on
    await _call(stream=False)                       # argument overrides config
    assert modes == [False, True, False]


@pytest.mark.asyncio
async def test_a_stream_that_drops_is_requested_again(env, monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return _agen(ANSWER, fail_after=3 if calls["n"] == 1 else None)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True)
    assert calls["n"] == 2
    assert content == "Hello world"   # once, not with the first attempt's half


@pytest.mark.asyncio
async def test_an_empty_stream_is_retried_like_a_dropped_connection(env, monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return _agen([] if calls["n"] == 1 else ANSWER)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True)
    assert calls["n"] == 2
    assert content == "Hello world"


@pytest.mark.asyncio
async def test_the_call_timeout_bounds_the_whole_stream(env, monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return _agen(ANSWER, delay=0.05)   # each chunk arrives, but too slowly overall

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    with pytest.raises(llm_service.litellm.Timeout, match="not complete within"):
        await _call(stream=True, timeout=0.1)
    assert calls["n"] == 1 + llm_service.MAX_TIMEOUT_RETRIES
