"""call_llm and stream_llm build the same request and follow the same policy.

stream_llm was a second copy of call_llm's setup that drifted: it sent no
reasoning to any model but llmao's, ignored cache_prefix, tools and the
stop button, had no retries and never charged the user. Both now go through
_prepare_request and _completion_with_retries.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from services import llm_service
from services.cancel_token import AgentStopped, CancelToken, bind_token, reset_token


class _Resp:
    def __init__(self, content="ok"):
        msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
        self.choices = [SimpleNamespace(message=msg)]
        self._hidden_params = {}


def _chunk(content=None, finish=None, usage=None, model="claude-sonnet-4-5"):
    c = ModelResponseStream(
        id="s1", model=model,
        choices=[StreamingChoices(index=0, delta=Delta(content=content), finish_reason=finish)],
    )
    if usage:
        c.usage = Usage(**usage)
    return c


async def _agen(items, fail_after=None):
    for i, item in enumerate(items):
        if fail_after is not None and i == fail_after:
            raise llm_service.litellm.InternalServerError(
                message="Connection error.", llm_provider="openai", model="m")
        yield item


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLMAO_API_BASE", "https://llm.example/v1")
    monkeypatch.setenv("LLMAO_API_KEY", "sk-test")
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: Mock())

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(llm_service.asyncio, "sleep", no_sleep)


def _user():
    us = Mock()
    us.get_effective_api_key.return_value = None
    return us


async def _drain(gen):
    return [c async for c in gen]


CASES = [
    ("llmao", "gemma4-26b"),                     # enable_thinking
    ("bedrock", "us.anthropic.claude-opus-4-8"),  # adaptive
    ("bedrock", "us.anthropic.claude-sonnet-4-6"),  # reasoning_effort
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model", CASES)
async def test_both_entry_points_send_the_same_request(env, monkeypatch, provider, model):
    seen = []

    async def fake(**kwargs):
        seen.append(dict(kwargs))
        if kwargs.get("stream"):
            return _agen([_chunk("x")])
        return _Resp()

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    args = dict(
        provider=provider, model=model,
        messages=[{"role": "user", "content": "hi"}],
        parameters={"reasoning_effort": "high", "temperature": 0.5, "max_tokens": 100},
        user_service=_user(), user_id="u",
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        timeout=1234,
        cache_prefix="LARGE STABLE PREFIX",
    )
    await llm_service.call_llm(**args)
    await _drain(llm_service.stream_llm(**args))

    plain, streamed = seen
    for key in ("stream", "stream_options"):
        streamed.pop(key, None)
    assert streamed.keys() == plain.keys()
    for key in plain:
        if key == "timeout":
            assert streamed[key].read == plain[key].read == 1234
        else:
            assert streamed[key] == plain[key], key


@pytest.mark.asyncio
async def test_stream_retries_before_the_first_chunk(env, monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception('Error code: 529 - {"type":"overloaded_error"}')
        return _agen([_chunk("a"), _chunk("b")])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    chunks = await _drain(llm_service.stream_llm(
        "llmao", "gemma4-26b", [{"role": "user", "content": "hi"}], {},
        user_service=_user(), user_id="u"))
    assert calls["n"] == 2
    assert [c.choices[0].delta.content for c in chunks] == ["a", "b"]


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_output_reached_the_caller(env, monkeypatch):
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return _agen([_chunk("a"), _chunk("b")], fail_after=1)

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    got = []
    with pytest.raises(llm_service.litellm.InternalServerError):
        async for c in llm_service.stream_llm(
                "llmao", "gemma4-26b", [{"role": "user", "content": "hi"}], {},
                user_service=_user(), user_id="u"):
            got.append(c)
    assert calls["n"] == 1
    assert len(got) == 1


@pytest.mark.asyncio
async def test_stream_charges_the_user_when_it_completes(env, monkeypatch):
    async def fake(**kwargs):
        return _agen([
            _chunk("Hello "), _chunk("world", finish="stop"),
            _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}),
        ])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    us = _user()
    await _drain(llm_service.stream_llm(
        "llmao", "gemma4-26b", [{"role": "user", "content": "hi"}], {},
        user_service=us, user_id="u"))
    us.add_usage.assert_called_once()
    assert us.add_usage.call_args.args[1] > 0


@pytest.mark.asyncio
async def test_stream_asks_for_usage_where_the_provider_takes_it(env, monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _agen([_chunk("x")])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    await _drain(llm_service.stream_llm(
        "llmao", "gemma4-26b", [{"role": "user", "content": "hi"}], {},
        user_service=_user(), user_id="u"))
    assert seen["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_stream_honours_the_stop_button(env, monkeypatch):
    called = {"n": 0}

    async def fake(**kwargs):
        called["n"] += 1
        return _agen([_chunk("x")])

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    token = CancelToken()
    token.request_stop()
    ctx = bind_token(token)
    try:
        with pytest.raises(AgentStopped):
            await _drain(llm_service.stream_llm(
                "llmao", "gemma4-26b", [{"role": "user", "content": "hi"}], {},
                user_service=_user(), user_id="u"))
    finally:
        reset_token(ctx)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_stream_reports_an_overflowing_prompt_plainly(env, monkeypatch):
    async def fake(**kwargs):
        raise Exception("prompt is too long: 250000 tokens > 200000 maximum")

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    with pytest.raises(ValueError, match="context window"):
        await _drain(llm_service.stream_llm(
            "bedrock", "us.anthropic.claude-opus-4-8", [{"role": "user", "content": "hi"}], {},
            user_service=_user(), user_id="u"))


def test_cost_is_read_where_litellm_puts_it():
    resp = SimpleNamespace(_hidden_params={"response_cost": 0.25}, usage=None)
    assert llm_service._extract_response_cost(resp) == 0.25
