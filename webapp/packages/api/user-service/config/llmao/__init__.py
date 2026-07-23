"""Self-hosted models served by llmao's litellm proxy (vLLM behind it).

Context windows here are the vLLM SERVING limit (--max-model-len 32768 in
llmao's docker-compose), NOT the architectural maximum the model cards or
llmao's own catalog advertise (131072). The serving limit is what actually
rejects a request, and get_context_window() feeds the agents' batch-sizing
math directly -- so an aspirational number here produces batches that vLLM
refuses at request time.

If llmao raises --max-model-len (VRAM permitting: three vLLM servers share
one L40S at 0.38/0.38/0.18 utilization), raise these to match. They must
move together.

Both models are Qwen3/Gemma-4 reasoning models that think BY DEFAULT. For
the cheap-tier work these replace (relevance filtering, code inventory),
thinking is not wanted: it was Haiku 4.5 and Sonnet 4.6 without extended
thinking before the Mythos swap. Disabling it is per-model:

  * Qwen3: chat template kwarg, passed through litellm as
      extra_body={"chat_template_kwargs": {"enable_thinking": False}}
  * Gemma 4: verify against the served model before relying on it.

Both llmao's proxy and gofannon run litellm with drop_params=True, so a
param neither layer recognises is silently discarded -- a thinking-disable
that does not land fails quietly, not loudly. Verify with a probe call and
check the response for reasoning_content / <think> blocks.
"""

# vLLM --max-model-len from llmao/infra/docker/docker-compose.yml.
_SERVED_CONTEXT = 32768

_COMMON_PARAMS = {
    "temperature": {
        "type": "float",
        "default": 0.0,
        "min": 0.0,
        "max": 2.0,
        "description": "Randomness. 0 for deterministic classification work.",
    },
    "top_p": {
        "type": "float",
        "default": 1.0,
        "min": 0.0,
        "max": 1.0,
        "description": "Nucleus sampling",
    },
    "max_tokens": {
        "type": "integer",
        "default": 4096,
        "min": 1,
        # Output shares the served context window with the input, so no
        # single call can ask for the whole window as output.
        "max": _SERVED_CONTEXT,
        "description": "Maximum tokens in response",
    },
}

# NOTE: these keys must match `model_name` in llmao's litellm/config.yaml
# verbatim -- the proxy routes on that string. They stay "selfhost/*" (llmao's
# naming) even though the gofannon provider is called "llmao": the provider
# name is ours, the model id is theirs. Renaming these to llmao/* yields a 400
# from the proxy.
models = {
    # Qwen3 8B (FP8) — fast tier. Replaces Haiku 4.5 for relevance filtering.
    "selfhost/qwen3-8b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": True,   # on by default; disable per-call
        "context_window": 8192,
        "parameters": dict(_COMMON_PARAMS),
    },
    # Gemma 4 26B-A4B (FP8, MoE ~4B active) — general tier. Candidate
    # replacement for Sonnet 4.6 on code inventory.
    "selfhost/gemma4-26b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": True,
        "context_window": _SERVED_CONTEXT,
        "parameters": dict(_COMMON_PARAMS),
    },
}