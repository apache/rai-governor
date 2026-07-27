"""Self-hosted models served by llmao (vLLM behind litellm, or vLLM direct).

Context windows here are the vLLM SERVING limit (--max-model-len), NOT the
architectural maximum the model cards or llmao's own catalog advertise. The
serving limit is what actually rejects a request, and get_context_window()
feeds the agents' batch-sizing math directly -- so an aspirational number here
produces batches that vLLM refuses (or worse, hangs on) at request time.

CRITICAL -- max-model-len must stay <= the vLLM KV cache size, or requests
HANG instead of erroring. This is the single most important rule in this file.
The KV cache holds a fixed number of tokens (measured, not configured). If
--max-model-len is set ABOVE that, a prompt sized between the cache size and
max-model-len is accepted by vLLM and then hangs forever -- no error, no
timeout, the engine just never schedules it. Real batches land in that dead
zone; small probes do not, which makes it look intermittent and network-like.
It is neither. Keep max-model-len at or below the KV cache token count.

As of 2026-07-25 Gemma is the SOLE resident on the L40S; Qwen is no longer
served (see the commented-out entry below):

    gemma4-26b  (FP8, ~26 GB weights, gpu-util 0.95, CUDA graphs on)
      vLLM --max-model-len : 65,536   (fits under the ~68k-token KV cache)
      KV cache             : ~68,016 tokens at gpu-util 0.95
      prefix caching       : OFF (--no-enable-prefix-caching)

Measured on the solo config: ~5000+ tok/s prefill, ~80 tok/s single-stream
generation. Requests run effectively serial (concurrency >1 deadlocks this
vLLM v1 build; ASVS_MEDIUM_CONCURRENCY is 1).

OUTPUT SHARES THE WINDOW WITH INPUT. max_tokens is an output reservation
inside max-model-len, so input_tokens + max_tokens must stay under it. A
request with 49,537 input + 16,000 output = 65,537 is one token over the
65,536 limit and gets a clean 400. The value below is therefore the INPUT
budget only, set below the serving limit to leave room for output:

    _GEMMA_CONTEXT (input) + max_tokens (output) + margin  <=  65,536

At the classification tier's 4000 max_tokens, 61000 leaves ~600 tokens of
margin. Phases that use a larger max_tokens (e.g. Domains at 16000) will still
400 on large inputs -- those need a lower per-phase max_tokens or a
larger-context tier (Opus/Sonnet), not Gemma. Gemma's honest usable window on
this card is ~65k total; it cannot be raised to the 114k the model card
advertises without a bigger GPU (114k of KV cache does not fit in 46 GB
alongside the weights).

If llmao changes Gemma's --max-model-len, change _GEMMA_CONTEXT here to match
(minus the output reservation). They must move together. Do not reintroduce a
single shared constant across models.

THINKING / enable_thinking -- DO NOT SEND THE KWARG TO GEMMA.
Earlier revisions injected extra_body={"chat_template_kwargs":
{"enable_thinking": False}} to suppress reasoning. For Gemma on this vLLM
build that kwarg HANGS the request (vLLM accepts it and never responds). Gemma
also does not need it: with --reasoning-parser gemma4 its thinking is already
off by default (verified by probe -- clean JSON, reasoning_content is None,
no <think> block). The kwarg was both unnecessary and harmful. This is why the
gemma entry sets supports_thinking = False: that flag gates the kwarg
injection in llm_service.py, and False keeps it from ever being sent. Do not
set it True for Gemma. (Possibly related to vLLM issue #39130, Gemma +
thinking-off + xgrammar.)

Both llmao's proxy and gofannon run litellm with drop_params=True, so a param
neither layer recognises is silently discarded -- a thinking-disable that does
not land fails quietly, not loudly. Re-probe after any serving change rather
than assuming.
"""

# vLLM serving limits, per model. These are NOT interchangeable, and each is
# the INPUT budget (serving --max-model-len minus that tier's output
# reservation), NOT the raw --max-model-len. See the module docstring.
_GEMMA_CONTEXT = 61000
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
    "gemma4-26b": {
        "returns_thoughts": False,
        "supports_effort": False,
        "supports_thinking": False,
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