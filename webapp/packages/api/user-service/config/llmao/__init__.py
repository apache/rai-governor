"""Self-hosted models served by llmao's litellm proxy (vLLM behind it).

Context windows here are the vLLM SERVING limit (--max-model-len), NOT the
architectural maximum the model cards or llmao's own catalog advertise. The
serving limit is what actually rejects a request, and get_context_window()
feeds the agents' batch-sizing math directly -- so an aspirational number here
produces batches that vLLM refuses at request time.

As of 2026-07-23 Gemma is the SOLE resident on the L40S and Qwen is no longer
served (see the commented-out entry below):

    selfhost/gemma4-26b -> 131072  (FP8, ~26 GB weights, gpu-util 0.90,
                                    CUDA graphs on, --max-num-seqs 16)

Earlier co-resident numbers (Gemma 32768 / Qwen 8192, both --enforce-eager)
no longer apply. If llmao changes a model's --max-model-len, change that
model's number here to match -- they must move together, per model. Do not
reintroduce a single shared constant.

Measured on the solo config: ~1200 tok/s prefill, ~45 tok/s single-stream
generation. KV cache is 58,320 tokens, so max concurrency is ~4x at a full
131k prompt but far higher at realistic prompt sizes; --max-num-seqs 16 is
the ceiling.

OUTPUT SHARES THE WINDOW WITH INPUT. max_tokens is an output reservation
inside max-model-len, so input_tokens + max_tokens must stay under it.
Observed in practice on the old 8k Qwen config: 4096 output + 4097 input =
8193 -> hard 400 ("maximum context length is 8192 tokens"). The same rule
applies at any window size: keep max_tokens well under it so the prompt fits.

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
_GEMMA_CONTEXT = 114688
# _QWEN_CONTEXT = 8192   # unused: Qwen is no longer served (see below)


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
    # Qwen3 8B (FP8) -- NOT SERVED as of 2026-07-23. Removed from the GPU once
    # discovery's per-domain batching made the relevance-filter (LIGHT) tier
    # redundant: the pipeline stopped calling it, so it was holding ~12 GB of
    # VRAM for nothing and forcing Gemma into --enforce-eager at gpu-util 0.68.
    # Gemma now has the whole card. LIGHT is back on Haiku 4.5.
    # To restore: uncomment this, re-add the vllm-qwen8b docker::run block in
    # llmao.pp (plus its litellm require + LLMAO_SELFHOST_QWEN8B_URL env), and
    # re-derive both models' windows for co-residency -- the 131072 below is a
    # sole-resident number and will NOT fit alongside another model.
    # "selfhost/qwen3-8b": {
    #     "returns_thoughts": False,
    #     "supports_effort": False,
    #     "supports_thinking": True,
    #     "context_window": 8192,
    #     "parameters": _params(8192, 1024),
    # },
    # Gemma 4 26B-A4B (FP8, MoE ~4B active) -- general tier. Candidate
    # replacement for Sonnet 4.6 on code inventory.
    "selfhost/gemma4-26b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": True,
        "context_window": _GEMMA_CONTEXT,
        # max_tokens ceiling is deliberately NOT the full 131k window. Two
        # reasons: (1) output is reserved out of the SAME window as the input,
        # so a ceiling equal to the window makes it easy for batch-sizing math
        # to request more output than the prompt leaves room for -- the
        # input+output>window 400 we already hit on the old 8k Qwen config;
        # (2) KV cache is 58,320 tokens shared across --max-num-seqs 16, so one
        # runaway 100k generation starves every other in-flight request.
        #
        # 32k rather than something tighter because the failure modes are not
        # symmetric: a too-HIGH ceiling risks a hypothetical runaway request,
        # while a too-LOW one silently TRUNCATES long structured output --
        # a consolidation emitting 20-30k of merged JSON would come back
        # unparseable, and that reads as a model-quality problem rather than a
        # config limit. Current call sites ask for <=8192, so this is ~4x
        # headroom over real usage while still capping the pathological case.
        # vLLM's own input+output<=max_model_len check remains the real
        # backstop; this is a policy cap on top of it.
        "parameters": _params(32768, 4096),
    },
}