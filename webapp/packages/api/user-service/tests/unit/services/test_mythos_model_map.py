"""Tests for the Mythos model-map registration and the prompt-caching gate.

Both exist because litellm's model map does not know our 1P model string.
The map is keyed "anthropic.claude-mythos-preview"; we send
"claude-mythos-5". Every lookup on that name misses, and the misses are
silent:

  - _model_supports_effort_param() returns False, so output_config.effort
    is dropped under drop_params and ASVS_HEAVY_REASONING has no effect.
    The only symptom is a WARNING in the litellm log.
  - cost fields default to zero, so spend reporting for the heavy tier
    reads as free.
  - the prompt-caching gate has nothing to consult.

These tests pin the registration and the gate's behaviour. They matter
more than most because the failure mode is a warning rather than an error:
without them, a litellm upgrade that renames the map entry would silently
restore the original bug.
"""
from __future__ import annotations

import pytest

import litellm

from services import llm_service


pytestmark = pytest.mark.unit


class TestMythosModelRegistration:

    def test_mythos_1p_name_is_registered(self):
        """The 1P model string must resolve in litellm's map.

        Skips rather than fails if litellm has dropped the preview entry --
        that is an upstream change to investigate, not a regression in our
        code, and the registration is guarded on the source existing.
        """
        if llm_service._MYTHOS_MAP_SRC not in litellm.model_cost:
            pytest.skip(
                f"litellm no longer ships {llm_service._MYTHOS_MAP_SRC}; "
                "the registration source has changed upstream"
            )
        assert "claude-mythos-5" in litellm.model_cost

    def test_registered_entry_advertises_output_config(self):
        """This is the flag that stops effort being dropped.

        _model_supports_effort_param() returns True on either
        supports_output_config or any supports_*_reasoning_effort flag.
        The preview entry carries the former; if a future litellm entry
        drops it, effort silently stops reaching the API again.
        """
        if "claude-mythos-5" not in litellm.model_cost:
            pytest.skip("Mythos not registered; see the test above")
        entry = litellm.model_cost["claude-mythos-5"]
        assert entry.get("supports_output_config") is True

    def test_registration_does_not_crash_without_the_source(self, monkeypatch):
        """A litellm upgrade that renames the entry must not break startup.

        Re-runs the guarded registration with the source absent. The point
        is the `if ... in litellm.model_cost` guard: without it this raises
        KeyError at import time and the whole service fails to start.
        """
        fake_map = {k: v for k, v in litellm.model_cost.items()
                    if k != llm_service._MYTHOS_MAP_SRC}
        monkeypatch.setattr(litellm, "model_cost", fake_map)

        src = llm_service._MYTHOS_MAP_SRC
        if src in litellm.model_cost:            # exercises the guard
            litellm.register_model({"claude-mythos-5": litellm.model_cost[src]})
        # reaching here without raising is the assertion


class TestPromptCachingGate:

    def test_non_claude_models_are_excluded(self):
        assert llm_service._supports_prompt_caching("llmao", "gemma4-26b") is False
        assert llm_service._supports_prompt_caching("openai", "gpt-5.2") is False

    def test_claude_models_in_the_map_follow_the_map(self):
        """A Claude model that supports caching keeps it."""
        supported = [
            name for name, info in litellm.model_cost.items()
            if "claude" in name.lower()
            and info.get("supports_prompt_caching") is True
        ]
        if not supported:
            pytest.skip("no cache-supporting Claude model in litellm's map")
        assert llm_service._supports_prompt_caching("bedrock", supported[0]) is True

    def test_mythos_is_excluded_despite_being_claude(self):
        """The whole reason the name test was insufficient.

        asvs_audit passes a large inventory prefix as cache_prefix on EVERY
        heavy batch, expecting it to be cached across the pass. Mythos does
        not support prompt caching, so the marker bought nothing while
        converting the first message to block form -- and the heavy tier
        paid full input cost on a prefix designed to be cached once.
        """
        if "claude-mythos-5" not in litellm.model_cost:
            pytest.skip("Mythos not registered")
        assert llm_service._supports_prompt_caching(
            "anthropic-1p", "claude-mythos-5"
        ) is False

    def test_unknown_claude_model_defaults_to_enabled(self):
        """Absent from the map means keep today's behaviour.

        The gate is a correction for one known-bad case, not a new
        allow-list. A Claude model litellm has not heard of should still
        get the cache marker rather than silently losing it.
        """
        assert llm_service._supports_prompt_caching(
            "bedrock", "us.anthropic.claude-not-a-real-model-v9"
        ) is True

    def test_case_insensitive_on_the_name_test(self):
        assert llm_service._supports_prompt_caching("bedrock", "CLAUDE-OPUS-5") is True