# =========================================================================
# Anthropic first-party API (via Workload Identity Federation).
#
# Access is via short-lived OAuth tokens minted by exchanging an AWS STS
# web identity JWT with Anthropic's /v1/oauth/token endpoint (RFC 7523
# jwt-bearer grant). No static ANTHROPIC_API_KEY is used, and one being
# present in the container environment is treated as an error -- see
# services/anthropic_wif.py::_assert_no_static_api_key.
#
# Model IDs are the 1P model strings: no "us." region prefix, no ARN.
# litellm routes them under its "anthropic" provider prefix.
#
# SCHEMA NOTE: entries follow the same shape as every other provider in
# config/ -- a "parameters" dict of {type, default, min/max/choices,
# description}. That structure is not decorative:
#   - models/chat.py:19 builds the request schema from it
#   - dependencies.py:312 clamps caller max_tokens to parameters.max_tokens.max
#   - the agent editor UI renders its controls from it
# A config carrying flat keys like max_output_tokens or
# input_cost_per_million instead would load without error and then silently
# fail to clamp, validate, or render.
# =========================================================================


def _params(max_tokens_max, max_tokens_default=16384, effort_choices=None,
            temperature=True):
    """Build a parameters block.

    Keeps the entries below readable and makes the per-model differences
    (effort scale, output ceiling, temperature support) obvious.
    """
    p = {}
    if temperature:
        p["temperature"] = {
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 1.0,
            "description": (
                "Randomness. Locked to 1.0 when thinking is enabled -- the "
                "Anthropic API rejects any other value alongside extended "
                "thinking with HTTP 400."
            ),
            "mutually_exclusive_with": ["top_p"],
        }
    p["top_p"] = {
        "type": "float",
        "default": 0.9,
        "min": 0.0,
        "max": 1.0,
        "description": "Nucleus sampling (0.1=conservative, 0.95=diverse)",
        "mutually_exclusive_with": ["temperature"],
    }
    p["reasoning_effort"] = {
        "type": "choice",
        "default": "disable",
        "choices": effort_choices or ["disable", "low", "medium", "high"],
        "description": (
            "Reasoning Effort: enables extended thinking. xhigh/max are "
            "Opus 4.7+ and Mythos only."
        ),
    }
    p["max_tokens"] = {
        "type": "integer",
        "default": max_tokens_default,
        "min": 1,
        "max": max_tokens_max,
        "description": "Maximum tokens in response",
    }
    return p


_FULL_EFFORT = ["disable", "low", "medium", "high", "xhigh", "max"]

models = {
    # =====================================================================
    # Claude Mythos 5 -- preview.
    #
    # MIGRATED FROM bedrock-mythos. That provider reached Mythos through
    # Bedrock Mantle in Anthropic's preview AWS account via cross-account
    # STS AssumeRole; that path is being decommissioned, so 1P replaces it
    # rather than adding to it.
    #
    # Two things carried over from the Bedrock entry that are NOT
    # Bedrock-specific and still apply:
    #
    #   1. temperature is REJECTED by this model -- HTTP 400 "`temperature`
    #      is deprecated for this model". Confirmed 2026-07-20 when all 9
    #      classification batches 400'd instantly on temperature=0. The
    #      parameter is OMITTED here rather than documented-and-present,
    #      because on Bedrock litellm's drop_params did NOT strip it (the
    #      model is not fully in litellm's registry) and it reached the API.
    #      Omitting it is the only reliable guard.
    #
    #   2. adaptive thinking: reasoning_effort becomes
    #      thinking={"type":"adaptive"} + output_config={"effort":...} rather
    #      than the legacy thinking.type.enabled shape. Declared below as
    #      thinking_style. (Previously inferred from "claude-mythos" in the
    #      model NAME by llm_service._is_opus_4_7_or_later(), now removed.)
    #
    # litellm_registration: litellm's model map knows Mythos only as
    # "anthropic.claude-mythos-preview". Our 1P string "claude-mythos-5" misses
    # the lookup, so _model_supports_effort_param() returns False and
    # output_config.effort is silently DROPPED under drop_params --
    # ASVS_HEAVY_REASONING has no effect, cost tracking reads zero, and the
    # prompt-caching gate has nothing to consult. Copying the preview entry
    # inherits whatever capability flags and costs litellm already knows.
    # Previously a hard-coded block at the top of llm_service.
    # =====================================================================
    "claude-mythos-5": {
        "returns_thoughts": True,
        "supports_effort": True,
        "supports_thinking": True,
        "supports_prompt_caching": True,
        "thinking_style": "adaptive",
        # See note 1 above: rejected outright, thinking or not. Omitting it
        # from parameters keeps it out of the UI; this drops it in
        # llm_service for any caller that sends one anyway.
        "rejects_temperature": True,
        "litellm_registration": {
            "copy_from": "anthropic.claude-mythos-preview",
            "overrides": {"litellm_provider": "anthropic"},
        },
        "context_window": 1000000,
        "parameters": _params(128000, 16384, _FULL_EFFORT, temperature=False),
    },
}