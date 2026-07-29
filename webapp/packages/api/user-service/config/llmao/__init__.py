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

As of 2026-07-29 Gemma is served from the Vast.ai box, not the AWS L40S. Both
geometries are recorded because AWS remains the rollback target:

    CURRENT -- Vast.ai (google/gemma-4-26B-A4B-it, BF16)
      vLLM                     : 0.25.1
      --max-model-len          : 131,072
      KV cache                 : ~855,000 tokens -- far above max-model-len,
                                 so the hang condition above cannot be reached
      --gpu-memory-utilization : 0.90
      prefix caching           : ON (flag absent; vLLM default)
      --reasoning-parser       : gemma4

    ROLLBACK -- AWS L40S (RedHatAI/gemma-4-26B-A4B-it-FP8-Dynamic, FP8)
      vLLM                     : 0.19.1
      --max-model-len          : 65,536  (fits under the ~68k-token KV cache)
      KV cache                 : ~68,016 tokens at gpu-util 0.95
      prefix caching           : OFF (--no-enable-prefix-caching)

The throughput figures that used to be here were measured on the AWS config and
have NOT been re-measured on Vast: ~5000+ tok/s prefill, ~80 tok/s single-stream
generation, requests effectively serial (concurrency >1 deadlocked the 0.19.1 v1
build; ASVS_MEDIUM_CONCURRENCY is 1). The 0.25.1 build may not have that
deadlock, and an ~855k-token KV cache has room for real concurrency. Treat all
of these as stale until re-measured.

OUTPUT SHARES THE WINDOW WITH INPUT. max_tokens is an output reservation inside
max-model-len, so input_tokens + max_tokens must stay under it. A request whose
input + max_tokens exceeds the serving limit gets a clean 400. The value below
is therefore the INPUT budget only, set below the serving limit to leave room
for output:

    _GEMMA_CONTEXT (input) + max_tokens (output) + margin  <=  131,072

The max_tokens CEILING for this model is 32,768 (see _params below), so the
input budget must leave that much room even though current call sites ask for
<= 8,192. 131,072 - 32,768 = 98,304; _GEMMA_CONTEXT is 97,000, leaving ~1,300
tokens of margin at the ceiling. Sizing against actual usage instead would
allow a bigger input budget, but converts the ceiling into a latent 400 for the
next caller who raises max_tokens.

Gemma's usable window is now genuinely ~131k, not the ~65k the L40S allowed.
The old note that it "cannot be raised to the 114k the model card advertises
without a bigger GPU" was true of the L40S and is no longer the constraint.

ON ROLLBACK to the AWS host this constant MUST come back down to 61,000. A
97,000-token input budget against a 65,536 serving limit fails on every large
request.

If the serving --max-model-len changes, change _GEMMA_CONTEXT here to match
(minus the output reservation). They must move together, and the mapping is NOT
one-to-one -- it is bounded by the max_tokens ceiling in _params, not by any
single tier's default. Do not reintroduce a single shared constant across
models.

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
_GEMMA_CONTEXT = 97000


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


# The key below is the model name that ends up in the JSON body sent to
# whatever LLMAO_API_BASE points at. llm_service.py builds
# f"{litellm_provider_prefix}/{model}" -> "openai/gemma4-26b", and the litellm
# SDK strips the "openai/" provider prefix client-side before the request goes
# out, so the endpoint receives the bare key.
#
# That means the correct value DEPENDS ON WHAT LLMAO_API_BASE POINTS AT:
#   - vLLM directly (current setup): must equal vLLM's --served-model-name,
#     i.e. "gemma4-26b".
#   - llmao's litellm proxy: must equal a model_name in that proxy's config,
#     which is generated from llmao's catalog.py and is currently
#     "selfhost/gemma4-26b".
#
# The two paths want DIFFERENT strings here. Do not "normalise" this key
# without changing LLMAO_API_BASE to match, and re-verify with one call
# through the target endpoint -- the SDK's prefix handling is version
# dependent, so confirm rather than assume.
models = {
    # Gemma 4 26B-A4B (MoE, ~4B active) -- general/MEDIUM tier. Candidate
    # replacement for Sonnet 4.6 on code inventory. Currently served BF16 on
    # the Vast.ai box; was FP8 on the AWS L40S. See the module docstring for
    # both serving geometries.
    "gemma4-26b": {
        "returns_thoughts": False,
        "supports_effort": False,
        # Leave False. Gemma's thinking is already off by default because the
        # server runs --reasoning-parser gemma4, and this flag is what gates
        # the chat_template_kwargs injection in llm_service.py. The kwarg was
        # observed to HANG this model on vLLM 0.19.1; that has not been
        # retested on 0.25.1 and there is no reason to find out.
        "supports_thinking": False,
        "context_window": _GEMMA_CONTEXT,
        # max_tokens ceiling is deliberately NOT the full serving window.
        #
        # (1) Output is reserved out of the SAME window as the input, so a
        #     ceiling equal to the window makes it easy for batch-sizing math
        #     to request more output than the prompt leaves room for -- the
        #     input+output>window 400 this pipeline already hit once, on the
        #     8k Qwen config that used to live in this dict.
        # (2) A runaway generation holds KV cache that other in-flight
        #     requests are competing for. NOTE: this argument was sized when
        #     the cache was ~58k tokens across --max-num-seqs 16 on the L40S.
        #     At the Vast box's ~855k-token cache it is much weaker, so (1)
        #     and the asymmetry below are what actually carry this decision
        #     now. Do not recompute the cap from cache size alone.
        #
        # 32k rather than something tighter because the failure modes are not
        # symmetric: a too-HIGH ceiling risks a hypothetical runaway request,
        # while a too-LOW one silently TRUNCATES long structured output --
        # a consolidation emitting 20-30k of merged JSON would come back
        # unparseable, and that reads as a model-quality problem rather than a
        # config limit. Current call sites ask for <=8192, so this is ~4x
        # headroom over real usage while still capping the pathological case.
        #
        # THIS VALUE IS THE BINDING TERM FOR _GEMMA_CONTEXT. The input budget
        # is sized as (serving --max-model-len) - (this ceiling) - margin, so
        # raising it here SHRINKS the usable input window. At 131,072 serving
        # and 32,768 here, the input budget is 97,000. The two move together;
        # see the module docstring.
        #
        # vLLM's own input+output<=max_model_len check remains the real
        # backstop; this is a policy cap on top of it.
        "parameters": _params(32768, 4096),
    },
}