models = {
    # =========================================================================
    # Anthropic Claude Mythos Preview (via Bedrock Mantle).
    # 1M token context, 128K max output. Same adaptive thinking shape as
    # Opus 4.7+/4.8 (thinking={"type":"adaptive"}, output_config={"effort":"high"}).
    # VERIFIED: llm_service._is_opus_4_7_or_later() now matches this model
    # explicitly ("claude-mythos" in model -> True), so reasoning_effort is
    # translated to thinking={"type":"adaptive"} + output_config={"effort":...}
    # rather than the legacy thinking.type.enabled shape. Adaptive thinking
    # IS active for Mythos whenever reasoning_effort != "disable".
    # NOTE: litellm.drop_params=True means the raw reasoning_effort kwarg is
    # popped and re-expressed; a litellm warning mentioning reasoning_effort
    # does NOT mean the effort was ignored.
    # =========================================================================
    "anthropic.claude-mythos-5": {
        "returns_thoughts": True,
        "supports_effort": True,
        "supports_thinking": True,
        "context_window": 1000000,
        "parameters": {
            "temperature": {
                "type": "float",
                "default": 1.0,
                "min": 0.0,
                "max": 1.0,
                "description": (
                    "REJECTED by this model: sending temperature returns HTTP 400 "
                    "'`temperature` is deprecated for this model' (confirmed "
                    "2026-07-20 — all 9 classification batches 400'd instantly "
                    "when temperature=0 was sent). drop_params does NOT strip it: "
                    "claude-mythos-5 is not fully in litellm's registry (the same "
                    "gap that dropped output_config), so temperature passes through "
                    "and Bedrock rejects it. Callers MUST omit temperature for this "
                    "model. This entry is kept only to document the constraint; the "
                    "ASVS tier configs leave *_TEMPERATURE=none accordingly."
                ),
                "mutually_exclusive_with": ["top_p"],
            },
            "top_p": {
                "type": "float",
                "default": 0.9,
                "min": 0.0,
                "max": 1.0,
                "description": "Nucleus sampling (0.1=conservative, 0.95=diverse)",
                "mutually_exclusive_with": ["temperature"],
            },
            "reasoning_effort": {
                "type": "choice",
                "default": "disable",
                "choices": ["disable", "low", "medium", "high", "xhigh", "max"],
                "description": "Reasoning Effort: Enables extended thinking. xhigh/max are Opus 4.7+ only.",
            },
            "max_tokens": {
                "type": "integer",
                "default": 16384,
                "min": 1,
                "max": 128000,
                "description": "Maximum tokens in response",
            },
        },
    },
}
