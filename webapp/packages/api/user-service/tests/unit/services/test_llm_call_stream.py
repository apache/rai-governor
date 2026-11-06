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
    # The HTTP read limit is the call's own; gaps between chunks are limited
    # separately, after the first chunk.
    assert seen["timeout"].read == 900
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


@pytest.mark.asyncio
async def test_an_answer_lost_to_thinking_is_reported(env, monkeypatch, capsys):
    """llmao's gateway flags this for non-streamed calls; streams bypass it."""
    obs = Mock()
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: obs)
    thinking_only = [
        _chunk(role="assistant", content="", reasoning_content="Thinking and thinking..."),
        _chunk(finish="length"),
    ]

    async def fake(**kwargs):
        return _agen(thinking_only)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True)
    assert content == ""   # still returned: agents retry with thinking off
    assert "output limit reached" in capsys.readouterr().out
    events = [c.kwargs.get("event_type") for c in obs.log.call_args_list]
    assert "llm_output_budget_spent_thinking" in events


@pytest.mark.asyncio
async def test_a_complete_answer_is_not_reported(env, monkeypatch, capsys):
    async def fake(**kwargs):
        return _agen(ANSWER)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    await _call(stream=True)
    assert "output limit reached" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_proxy_502_is_named_as_a_proxy_error(env, monkeypatch, capsys):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm_service.litellm.BadGatewayError(
                message="<title>502 Proxy Error</title>", model=MODEL, llm_provider="openai")
        return _Resp()

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    await _call()
    assert "502 from a proxy in front of the model" in capsys.readouterr().out


def _plain(content, finish):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content="r" * 40)
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish)], _hidden_params={})
    return resp


@pytest.mark.asyncio
async def test_a_plain_answer_cut_off_at_max_tokens_is_reported(env, monkeypatch, capsys):
    obs = Mock()
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: obs)

    async def fake(**kwargs):
        return _plain('{"findings": [{"title": "half', "length")

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call()
    assert content == '{"findings": [{"title": "half'   # returned unchanged
    out = capsys.readouterr().out
    assert "answer cut off at the output limit" in out
    assert f"{PROVIDER}/{MODEL}" in out
    events = [c.kwargs.get("event_type") for c in obs.log.call_args_list]
    assert events == ["llm_output_truncated"]


@pytest.mark.asyncio
async def test_a_streamed_answer_cut_off_is_reported(env, monkeypatch, capsys):
    async def fake(**kwargs):
        return _agen([_chunk(role="assistant", content="Half an ans"), _chunk(finish="length")])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True)
    assert content == "Half an ans"
    out = capsys.readouterr().out
    assert out.count("WARNING") == 1
    assert "answer cut off at the output limit" in out


@pytest.mark.asyncio
async def test_a_complete_plain_answer_is_not_reported(env, monkeypatch, capsys):
    async def fake(**kwargs):
        return _plain("All done.", "stop")

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    await _call()
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_stream_llm_reports_a_cut_off_answer_at_the_end(env, monkeypatch, capsys):
    async def fake(**kwargs):
        return _agen([_chunk(role="assistant", content="Half"), _chunk(finish="length")])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    got = [c async for c in llm_service.stream_llm(
        PROVIDER, MODEL, [{"role": "user", "content": "hi"}], {"max_tokens": 5},
        user_service=_user(), user_id="u")]
    assert len(got) == 2   # chunks still reach the caller unchanged
    assert "answer cut off at the output limit" in capsys.readouterr().out


async def _agen_delays(items, delays):
    for item, delay in zip(items, delays):
        await asyncio.sleep(delay)
        yield item


@pytest.mark.asyncio
async def test_a_slow_first_chunk_is_waited_for_not_retried(env, monkeypatch):
    """A request queued on a busy server sends nothing until scheduled.
    Timing it out and retrying would only queue it again."""
    monkeypatch.setattr(llm_service, "LLM_STREAM_IDLE_TIMEOUT", 0.05)
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        # First chunk after 3x the idle limit, the rest promptly.
        return _agen_delays(ANSWER, [0.15] + [0.0] * (len(ANSWER) - 1))

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True, timeout=5)
    assert content == "Hello world"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_stream_that_stalls_after_starting_is_retried(env, monkeypatch):
    monkeypatch.setattr(llm_service, "LLM_STREAM_IDLE_TIMEOUT", 0.05)
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Starts, then goes quiet for 3x the idle limit.
            return _agen_delays(ANSWER, [0.0, 0.15] + [0.0] * (len(ANSWER) - 2))
        return _agen(ANSWER)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    (content, _), _ = await _call(stream=True, timeout=5)
    assert calls["n"] == 2
    assert content == "Hello world"
