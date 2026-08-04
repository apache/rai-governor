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

As of 2026-08-04 the two models live on DIFFERENT hosts, and both are reached
through llmao's litellm proxy rather than direct to vLLM:

    GEMMA -- off-host (google/gemma-4-26B-A4B-it, BF16), fronted by Fastly
      vLLM                     : 0.25.1
      --max-model-len          : 131,072
      KV cache                 : 855,962 tokens -- far above max-model-len,
                                 so the hang condition above cannot be reached
      --gpu-memory-utilization : 0.90
      prefix caching           : ON (flag absent; vLLM default)
      --reasoning-parser       : gemma4
      measured                 : ~100 tok/s sustained single-stream,
                                 ~492 tok/s aggregate at concurrency 8

    QWEN -- AWS L40S, SOLE resident (Qwen/Qwen3-8B-FP8)
      vLLM                     : 0.25.1
      --max-model-len          : 40,960  (Qwen3-8B's max_position_embeddings;
                                 NOT raisable -- vLLM refuses at startup, and
                                 VLLM_ALLOW_LONG_MAX_MODEL_LEN produces NaN on
                                 this RoPE model)
      KV cache                 : 216,864 tokens (29.78 GiB at gpu-util 0.90)
      max concurrency          : 5.29x at full context, far more at realistic
                                 prompt sizes
      prefix caching           : ON
      --reasoning-parser       : qwen3
      attention                : FLASH_ATTN

The 0.19.1 concurrency deadlock that forced ASVS_MEDIUM_CONCURRENCY=1 does NOT
reproduce on 0.25.1: 8 concurrent sustained, zero queueing, 2.55x aggregate
throughput for a 36% per-request slowdown. Both hosts now run 0.25.1.

THINKING DEFAULTS DIFFER BETWEEN THESE TWO MODELS, and it is not guessable.
Measured 2026-08-04 through the proxy, same prompt, no kwarg sent:

    qwen3-8b    -> 893 chars of reasoning   (thinks by DEFAULT)
    gemma4-26b  -> 0 chars                  (thinks only when ASKED)

Gemma is quiet because the server runs --reasoning-parser gemma4. Qwen reasons
unless told not to. A tier that leaves reasoning unset therefore gets opposite
behaviour from the two models -- and Qwen at a low max_tokens returns
content=None with the whole budget spent in reasoning_content.

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

Both llmao's proxy and gofannon run litellm with drop_params=True, so a param
neither layer recognises is silently discarded -- a thinking-disable that does
not land fails quietly, not loudly. Re-probe after any serving change rather
than assuming.
"""

# vLLM serving limits, per model. These are NOT interchangeable, and each is
# the INPUT budget (serving --max-model-len minus that tier's output
# reservation), NOT the raw --max-model-len. See the module docstring.
_GEMMA_CONTEXT = 97000

# Qwen3-8B serves 40,960 (its max_position_embeddings). Subtracting the 8,192
# max_tokens ceiling below leaves 32,768 of input budget with ~0 slack, so this
# is set at 30,000 to keep a real margin. The 8,192 this model ran under while
# it SHARED the L40S with Gemma was a co-residency artifact, not a model limit
# -- but 40,960 is a hard architectural ceiling and will not rise.
_QWEN_CONTEXT = 30000


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
        "supports_thinking": True,
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
        "parameters": _params(32768, 16384),
    },

    # Qwen3 8B FP8 -- fast/LIGHT tier. Sole resident on the AWS L40S.
    "qwen3-8b": {
        # The vLLM qwen3 parser splits thinking out of content, so a caller
        # that leaves reasoning unset gets an answer with the thinking block
        # already removed -- but the tokens were still SPENT producing it.
        "returns_thoughts": True,
        # Binary on/off only. vLLM's enable_thinking is a boolean; there is no
        # graded scale underneath it, and offering low/medium/high would imply
        # gradation this transport cannot express.
        "supports_effort": False,
        # True so llm_service's gate fires and enable_thinking is SENT. That
        # matters in the false direction here: unlike Gemma, Qwen reasons by
        # default, so a LIGHT tier that wants a fast direct answer needs
        # enable_thinking=false actively sent -- which only happens when this
        # is True and the tier's reasoning_effort is 'disable'/omitted.
        "supports_thinking": True,
        "context_window": _QWEN_CONTEXT,
        # Ceiling 8,192 rather than the 32k Gemma gets. Two reasons, and the
        # first is the binding one: 40,960 serving minus this ceiling leaves
        # 32,768 for input, and _QWEN_CONTEXT is 30,000 -- a bigger ceiling
        # eats the input budget on a model with a quarter of Gemma's window.
        #
        # Second, the default of 2,048 is deliberately well above a one-line
        # answer. Thinking consumes the SAME budget as the response, so a
        # LIGHT-tier default sized for the answer alone returns content=None
        # whenever reasoning is on -- observed at max_tokens=5, where all 5
        # went to reasoning_content.
        "parameters": _params(8192, 2048),
    },
}