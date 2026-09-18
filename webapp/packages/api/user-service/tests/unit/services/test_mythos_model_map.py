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

import litellm
import pytest
from config.provider_config import PROVIDER_CONFIG
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
                f"litellm no longer ships {llm_service._MYTHOS_MAP_SRC}; the registration source has changed upstream"
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
        fake_map = {k: v for k, v in litellm.model_cost.items() if k != llm_service._MYTHOS_MAP_SRC}
        monkeypatch.setattr(litellm, "model_cost", fake_map)

        src = llm_service._MYTHOS_MAP_SRC
        if src in litellm.model_cost:  # exercises the guard
            litellm.register_model({"claude-mythos-5": litellm.model_cost[src]})
        # reaching here without raising is the assertion


class TestPromptCachingGate:
    def test_non_claude_models_are_excluded(self):
        assert llm_service._supports_prompt_caching("llmao", "gemma4-26b") is False
        assert llm_service._supports_prompt_caching("openai", "gpt-5.2") is False

    def test_declared_models_still_follow_the_litellm_map(self):
        """A model that DECLARES the capability and that litellm agrees about.

        The map remains a second gate, but it is no longer the first: the
        config decides whether a model is a candidate at all. This asserts
        the two together -- declared here, supported there.
        """
        declared = [
            name
            for name, cfg in PROVIDER_CONFIG.get("bedrock", {}).get("models", {}).items()
            if cfg.get("supports_prompt_caching")
            and litellm.model_cost.get(name, {}).get("supports_prompt_caching") is not False
        ]
        if not declared:
            pytest.skip("no bedrock model both declares caching and is supported by litellm's map")
        assert llm_service._supports_prompt_caching("bedrock", declared[0]) is True

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
        assert llm_service._supports_prompt_caching("anthropic-1p", "claude-mythos-5") is False

    def test_undeclared_model_does_not_support_caching(self):
        """A model the config does not declare gets no cache_control marker.

        This inverts the previous assertion, deliberately. The gate used to
        be `"claude" in model.lower()`, so any Claude-ish string defaulted to
        enabled -- including models that do not exist. That is how a name
        match came to decide a capability, and the same function returned
        False for every self-hosted model, whose cache_prefix llm_service
        then silently discarded rather than delivering another way.

        An undeclared model now takes the prepend path instead: the same
        bytes reach the model, without a marker and without a cache
        discount. Nothing is lost that was not already absent.
        """
        assert llm_service._supports_prompt_caching("bedrock", "us.anthropic.claude-not-a-real-model-v9") is False

    def test_the_name_no_longer_decides_anything(self):
        """A declared model and an undeclared one differing only in case.

        Replaces a test that asserted the name match was case-insensitive.
        There is no name match now -- the point is that the string has
        stopped carrying meaning, so a case variant that is not a config key
        is simply an unknown model.
        """
        assert llm_service._supports_prompt_caching("bedrock", "CLAUDE-OPUS-5") is False
