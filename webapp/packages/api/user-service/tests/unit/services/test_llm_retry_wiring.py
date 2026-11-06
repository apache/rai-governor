"""The retry loop actually consults each transient-failure predicate.

test_llm_service_retries.py checks what the predicates return. These check
that _completion_with_retries calls them: a predicate that survives a
refactor as dead code is green on every unit test and wrong in production.
That nearly happened when the loop moved out of call_llm.

In ASVS run 07aacba, two audit bundles failed after one attempt on
"InternalServerError: OpenAIException - Connection error." because only the
string predicates were consulted; twelve requirements went unanalysed.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import llm_service

PROVIDER, MODEL = "llmao", "qwen3.8-27b"


class _Resp:
    def __init__(self):
        msg = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
        self.choices = [SimpleNamespace(message=msg, finish_reason="stop")]
        self._hidden_params = {}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLMAO_API_BASE", "https://llm.example/v1")
    monkeypatch.setenv("LLMAO_API_KEY", "sk-test")
    monkeypatch.setattr(llm_service, "get_observability_service", lambda: Mock())

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(llm_service.asyncio, "sleep", no_sleep)


def _flaky(monkeypatch, error):
    """acompletion fails once with `error`, then succeeds."""
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise error
        return _Resp()

    monkeypatch.setattr(llm_service.litellm, "acompletion", fake)
    return calls


async def _call():
    us = Mock()
    us.get_effective_api_key.return_value = None
    return await llm_service.call_llm(
        provider=PROVIDER, model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        parameters={"max_tokens": 10}, user_service=us, user_id="u")


def _class_only_error():
    # The message must match NONE of the string predicates, or the retry
    # could come from the text path and prove nothing about the class check.
    # Not the real "Connection error." text: that string is matched now.
    e = llm_service.litellm.InternalServerError(
        message="the model did not respond", llm_provider="openai", model=MODEL)
    text = str(e).lower()
    assert not llm_service._is_overloaded_error(text)
    assert not llm_service._is_transient_5xx_error(text)
    return e


@pytest.mark.asyncio
async def test_retry_loop_consults_the_class_predicate(env, monkeypatch):
    calls = _flaky(monkeypatch, _class_only_error())
    content, _ = await _call()
    assert content == "ok"
    assert calls["n"] == 2, "a transient exception class did not open the retry budget"


@pytest.mark.asyncio
async def test_without_the_class_predicate_that_error_is_not_retried(env, monkeypatch):
    """Guards the test above: it must depend on the class check."""
    monkeypatch.setattr(llm_service, "_is_transient_exception", lambda e: False)
    calls = _flaky(monkeypatch, _class_only_error())
    with pytest.raises(llm_service.litellm.InternalServerError):
        await _call()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_loop_consults_the_overloaded_predicate(env, monkeypatch):
    calls = _flaky(monkeypatch, Exception('{"type":"overloaded_error"}'))
    await _call()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_loop_consults_the_5xx_predicate(env, monkeypatch):
    calls = _flaky(monkeypatch, Exception("The server had an error while processing your request"))
    await _call()
    assert calls["n"] == 2
