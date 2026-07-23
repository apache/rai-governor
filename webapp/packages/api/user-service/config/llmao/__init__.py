"""Self-hosted models served by llmao's litellm proxy (vLLM behind it).

Context windows here are the vLLM SERVING limit (--max-model-len), NOT the
architectural maximum the model cards or llmao's own catalog advertise. The
serving limit is what actually rejects a request, and get_context_window()
feeds the agents' batch-sizing math directly -- so an aspirational number here
produces batches that vLLM refuses at request time.

The two models DO NOT share a window. Both are co-resident on one L40S
(~44 GiB, ~98.7% full), and the 8B's KV-cache slice only fits 8k:

    selfhost/gemma4-26b -> 32768   (FP8, ~28 GB weights, gpu-util 0.68)
    selfhost/qwen3-8b   ->  8192   (FP8,  ~8 GB weights, gpu-util 0.26)

Both run --enforce-eager. If llmao raises a model's --max-model-len (VRAM
permitting), raise that model's number here to match. They must move together,
per model -- do not reintroduce a single shared constant.

OUTPUT SHARES THE WINDOW WITH INPUT. max_tokens is an output reservation
inside max-model-len, so input_tokens + max_tokens must stay under it.
Observed in practice on the 8B: 4096 output + 4097 input = 8193 -> hard 400
("maximum context length is 8192 tokens"). Keep max_tokens modest on Qwen
(1024-2048) so there is room for the prompt.

Both are reasoning models that think BY DEFAULT. For the cheap-tier work these
replace (relevance filtering, code inventory) thinking is not wanted: it was
Haiku 4.5 and Sonnet 4.6 without extended thinking before the Mythos swap.
Disabling it is a chat-template kwarg, not reasoning_effort (which neither
model understands):

    extra_body={"chat_template_kwargs": {"enable_thinking": False}}

vLLM serves both with a reasoning parser (--reasoning-parser qwen3 / gemma4)
so the toggle is honored. Verified 2026-07-23 by probe: Qwen3-8B returns clean
JSON with no <think> block and no reasoning_content when nothing is specified.

Both llmao's proxy and gofannon run litellm with drop_params=True, so a param
neither layer recognises is silently discarded -- a thinking-disable that does
not land fails quietly, not loudly. Re-probe after any serving change rather
than assuming.

Known caveat: Gemma + thinking-off + xgrammar structured output has a vLLM bug
(issue #39130) where grammar constraints are silently bypassed. Plain text and
ordinary JSON prompting are fine; if a tier depends on ENFORCED schema, verify
it is actually enforced.
"""

# vLLM --max-model-len, per model. These are NOT interchangeable.
_GEMMA_CONTEXT = 32768
_QWEN_CONTEXT = 8192


def _params(window, default_max_tokens):
    """Parameter spec for one model.

    max_tokens' ceiling is that model's own served window -- a shared ceiling
    would let a caller request more output than the model can produce, which
    vLLM rejects at request time. The default is deliberately well under the
    window so the prompt always has room.
    """
    return {
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
            "default": default_max_tokens,
            "min": 1,
            # Output shares the served context window with the input, so no
            # single call can ask for the whole window as output.
            "max": window,
            "description": "Maximum tokens in response (shares the window with input)",
        },
    }


# NOTE: these keys must match `model_name` in llmao's litellm/config.yaml
# verbatim -- the proxy routes on that string. They stay "selfhost/*" (llmao's
# naming) even though the gofannon provider is called "llmao": the provider
# name is ours, the model id is theirs. Renaming these to llmao/* yields a 400
# from the proxy.
models = {
    # Qwen3 8B (FP8) -- fast tier. Replaces Haiku 4.5 for relevance filtering.
    # 8k window is TIGHT: keep per-call max_tokens at 1024-2048.
    "selfhost/qwen3-8b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": True,   # on by default; disable per-call
        "context_window": _QWEN_CONTEXT,
        "parameters": _params(_QWEN_CONTEXT, 1024),
    },
    # Gemma 4 26B-A4B (FP8, MoE ~4B active) -- general tier. Candidate
    # replacement for Sonnet 4.6 on code inventory.
    "selfhost/gemma4-26b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": True,
        "context_window": _GEMMA_CONTEXT,
        "parameters": _params(_GEMMA_CONTEXT, 4096),
    },
}