import asyncio
import json
import os
import random
from config import settings
from config.provider_config import PROVIDER_CONFIG
# boto3 and aws-bedrock-token-generator are imported lazily inside
# the token-minting path to avoid forcing those deps onto module
# load when no federated provider is configured. boto3 is
# already transitively present via storage_service / dynamodb /
# observability_service.
from services.database_service import get_database_service
from services.user_service import get_user_service
from typing import Any, AsyncGenerator, Dict, List, Tuple, Optional
import litellm

from services.litellm_logger import ensure_litellm_logging
from services.observability_service import get_observability_service
from services.user_service import UserService
from services.cancel_token import check_should_stop  # ISSUE-007

ensure_litellm_logging()

# Configure litellm defaults
litellm.drop_params = True
litellm.set_verbose = False

# litellm's model map knows Mythos only as "anthropic.claude-mythos-preview".
# Our 1P model string is "claude-mythos-5", which misses the lookup entirely,
# so _model_supports_effort_param() returns False and output_config.effort is
# silently dropped under drop_params -- ASVS_HEAVY_REASONING has no effect.
#
# thinking={"type":"adaptive"} still gets through, so the model does reason;
# it just picks its own effort rather than the one the tier asked for. The
# same miss also zeroes cost tracking and defeats the prompt-caching check
# below.
#
# Copy the existing entry rather than hand-writing one, so we inherit whatever
# capability flags and costs litellm already knows. Guarded on the source
# existing so a litellm upgrade that renames it cannot break startup.
_MYTHOS_MAP_SRC = "anthropic.claude-mythos-preview"
if _MYTHOS_MAP_SRC in litellm.model_cost:
    litellm.register_model({
        "claude-mythos-5": {
            **litellm.model_cost[_MYTHOS_MAP_SRC],
            "litellm_provider": "anthropic",
        }
    })

# ---------------------------------------------------------------------------
# Mythos model registration
# ---------------------------------------------------------------------------
# litellm (checked against 1.90.0) ships a registry entry for
# `anthropic.claude-mythos-preview` but NOT for `anthropic.claude-mythos-5`,
# so every registry lookup for the model we actually call misses. Three
# consequences, all silent:
#
#   1. supports_output_config -> False, so litellm DROPS reasoning_effort:
#        "Dropping unsupported `output_config` for model=%s (drop_params=True).
#         Effort is only supported on Opus 4.5+, Sonnet 4.6+, and Mythos
#         Preview."
#      (litellm/llms/anthropic/chat/transformation.py). Note `thinking` is
#      NOT dropped -- so adaptive thinking runs with NO effort ceiling, which
#      is not what any caller asked for.
#   2. supports_reasoning -> False, so reasoning_effort isn't offered as a
#      supported param in the first place.
#   3. get_max_tokens() raises, printing litellm's "Provider List / Give
#      Feedback" banner on EVERY call (one banner per call, including
#      successful ones). See BerriAI/litellm#7961: the provider prefix is
#      resolved, stripped, and the bare dotted model name is then re-looked-up
#      without it -- which never matches for `anthropic.claude-*` names.
#
# Registered under BOTH the bare and the prefixed key because the lookup
# happens at both points. Values are copied verbatim from litellm's own
# claude-mythos-preview entry rather than invented.
_MYTHOS_MODEL_INFO = {
    "input_cost_per_token": 0,
    "output_cost_per_token": 0,
    "litellm_provider": "bedrock",
    "max_input_tokens": 1000000,
    "max_output_tokens": 128000,
    "max_tokens": 128000,
    "mode": "chat",
    "supports_function_calling": True,
    "supports_vision": True,
    "supports_prompt_caching": False,
    "supports_reasoning": True,
    "supports_tool_choice": True,
    "supports_output_config": True,
}

try:
    litellm.register_model({
        "anthropic.claude-mythos-5": _MYTHOS_MODEL_INFO,
        "bedrock/mantle/anthropic.claude-mythos-5": _MYTHOS_MODEL_INFO,
    })
except Exception as _e:  # pragma: no cover - never fail import over metadata
    print(f"WARNING: could not register Mythos model info with litellm: {_e}",
          flush=True)

# Silences the "Provider List / Give Feedback" banner litellm prints via
# print() (not logging) whenever a model-registry lookup misses. Redundant for
# Mythos once registered above, but covers any other unregistered model string
# and keeps ~40% of noise out of the agent logs.
litellm.suppress_debug_info = True
# ---------------------------------------------------------------------------

# Default timeout (seconds) for LLM calls. Override with LLM_TIMEOUT_SECONDS env var.
DEFAULT_LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT_SECONDS", "600"))

# Max retries on timeout. Override with LLM_TIMEOUT_RETRIES env var.
#
# Defaulted to 0 until 2026-07-16, which meant a single hung request killed a
# whole pipeline run. Bedrock/Mantle intermittently accepts a request and then
# never responds: the same prompt+params that hangs past a 900s ceiling
# returns in ~180s on the next attempt (measured, back-to-back, same process).
# The retry path below is correct and was simply never enabled, so the first
# bad draw ended a 40-minute, ~$100 run that a 5-second backoff would have
# survived. 2 retries is the smallest number that covers a single hang.
MAX_TIMEOUT_RETRIES = int(os.getenv("LLM_TIMEOUT_RETRIES", "2"))

# Retries specifically for a rejected minted bearer token. One is
# normally enough: the retry mints a fresh token from fresh STS creds. A
# second failure means the problem isn't the token (bad trust policy,
# IMDS down), so don't spin.
MAX_AUTH_REMINT_RETRIES = int(os.getenv("LLM_AUTH_REMINT_RETRIES", "1"))

# Max retries on rate-limit / 429. Override with LLM_RATE_LIMIT_RETRIES.
# Bedrock and OpenAI both throttle on a per-minute token-bucket, so this
# needs real backoff with jitter — an immediate retry just hits the same
# empty bucket. Five attempts with exponential backoff (1, 2, 4, 8, 16s
# + jitter) gives roughly 30 seconds of patience per call, enough to
# ride through most quota refills.
MAX_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "5"))

# Max retries on provider overload (HTTP 529 overloaded_error). Distinct from
# rate-limit: 529 is the provider saying "too busy right now", not a quota
# bucket. litellm surfaces it as APIConnectionError (or a generic exception
# with "overloaded" in the body), NOT as RateLimitError, so it would otherwise
# fall through to the terminal branch and permanently fail the call. It is the
# MOST retryable error there is; reuse the same exponential-backoff-with-jitter
# as rate-limit. Especially important on newer or capacity-limited endpoints
# under stacked concurrency, where transient overloads are more likely.
MAX_OVERLOADED_RETRIES = int(os.getenv("LLM_OVERLOADED_RETRIES", "5"))

# Cap on individual backoff sleep, regardless of attempt number.
RATE_LIMIT_BACKOFF_CAP_SECONDS = int(os.getenv("LLM_RATE_LIMIT_BACKOFF_CAP", "60"))


def _extract_response_cost(response_obj: Any) -> Optional[float]:
    standard_logging = None
    if hasattr(response_obj, "_hidden_params") and isinstance(response_obj._hidden_params, dict):
        standard_logging = response_obj._hidden_params.get("standard_logging_object")
    if isinstance(standard_logging, dict):
        try:
            cost_value = standard_logging.get("response_cost")
            if cost_value is not None:
                return float(cost_value)
        except Exception:
            pass
    usage = getattr(response_obj, "usage", None)
    if usage and getattr(usage, "total_cost", None) is not None:
        try:
            return float(getattr(usage, "total_cost"))
        except Exception:
            return None
    return None

def _is_opus_4_7_or_later(model: str) -> bool:
    """Opus 4.7+ uses adaptive thinking with output_config.effort
    instead of the old reasoning_effort -> thinking.type.enabled
    translation that litellm does for Opus 4.6 and earlier.

    Mythos preview uses the same adaptive thinking shape as Opus 4.7+,
    so we match it here despite the function name. If more non-Opus
    models join this club, consider renaming to e.g.
    _supports_adaptive_thinking."""
    if "claude-mythos" in model:
        return True
    return any(tag in model for tag in (
        "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5"
    ))


# In-process cache of Mantle bearer tokens, keyed by role ARN.
# STS AssumeRole defaults to 1-hour sessions and bearer-token validity
# is bounded by the credentials' lifetime, so we refresh ~5 min before
# expiry to avoid mid-request expiration.
_BEDROCK_MYTHOS_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_REFRESH_MARGIN_SECONDS = 300

# Requested STS session length. AssumeRole defaults to 1h, which is tight
# against a single 1800s call plus the refresh margin. Bounded by the
# role's MaxSessionDuration (we fall back to the default if it's lower).
_STS_SESSION_DURATION_SECONDS = int(
    os.getenv("BEDROCK_MYTHOS_STS_DURATION_SECONDS", "10800")  # 3h
)


def _supports_prompt_caching(provider: str, model: str) -> bool:
    """Prompt caching via cache_control content blocks is an
    Anthropic/Bedrock-Claude feature. Restrict to Claude models so the
    marker is never injected into a provider that would reject or
    mishandle it. (litellm ignores cache_control for non-Anthropic
    providers, but being explicit keeps the block-form transform off the
    OpenAI 'responses' path too.)

    The name test alone is not sufficient: Mythos is a Claude model but
    litellm reports supports_prompt_caching=false for it. Injecting the
    marker anyway converts the first message to block form to carry a
    cache_control the model ignores -- no cache hit, and asvs_audit passes
    a large inventory prefix on EVERY heavy batch expecting it to be cached
    across the pass. Consult the model map, defaulting to today's behaviour
    when the model is absent from it.
    """
    if "claude" not in model.lower():
        return False
    return litellm.model_cost.get(model, {}).get("supports_prompt_caching", True)


def _inject_cache_prefix(
    messages: List[Dict[str, Any]], cache_prefix: str
) -> List[Dict[str, Any]]:
    """Return a NEW message list with `cache_prefix` prepended to the first
    message as its own cacheable content block. The first message's content
    is converted to block form so the prefix block can carry cache_control
    while the original content follows uncached. The caller's list is not
    mutated.

    Marker is exactly {"type": "ephemeral"} — NO ttl (Bedrock rejects ttl;
    litellm translates this OpenAI-format marker to Bedrock cachePoint)."""
    if not messages:
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": cache_prefix,
                 "cache_control": {"type": "ephemeral"}},
            ],
        }]

    new_messages = [dict(m) for m in messages]
    first = new_messages[0]
    original_content = first.get("content", "")

    prefix_block = {
        "type": "text",
        "text": cache_prefix,
        "cache_control": {"type": "ephemeral"},
    }

    if isinstance(original_content, str):
        tail_blocks = [{"type": "text", "text": original_content}] if original_content else []
    elif isinstance(original_content, list):
        tail_blocks = original_content
    else:
        tail_blocks = []

    first["content"] = [prefix_block] + tail_blocks
    new_messages[0] = first
    return new_messages


async def call_llm(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    tools: Optional[List[Dict[str, Any]]] = None,
    user_service: Optional[UserService] = None,
    user_id: Optional[str] = None,
    user_basic_info: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    cache_prefix: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Calls the specified language model using litellm, handling different API styles.
    Returns a tuple of (content, thoughts).

    Args:
        timeout: Per-call timeout in seconds. Defaults to LLM_TIMEOUT_SECONDS env var (600).
        cache_prefix: Optional large, stable text (system prompt, ASVS
            requirement text, shared inventory) to cache across calls that
            share it. When provided for a Claude model, it is sent as a
            separate content block marked cache_control={"type":"ephemeral"}
            ahead of the existing message content. litellm translates that
            marker to Bedrock's native cachePoint; cached input bills at
            ~10% on a hit. Ignored for non-Claude models and for the OpenAI
            'responses' API style (that path builds its own input from the
            original messages). Prompts under the provider minimum (2,048
            tokens for Sonnet 4.x) are silently processed without caching —
            no error — so pass only a genuinely large prefix.

            The marker carries NO ttl field: Bedrock rejects ttl (litellm
            issues #17250 / #15880); the native Anthropic API accepts it but
            we target Bedrock.
    """
    check_should_stop()  # ISSUE-007: cancel at structural boundary
    model_config = PROVIDER_CONFIG.get(provider, {}).get("models", {}).get(model, {})
    api_style = model_config.get("api_style")

    # If the provider config supplies an explicit litellm prefix (e.g.
    # anthropic-1p -> "anthropic"), use it so litellm routes
    # through its native handler for that backend. Defaults to the
    # gofannon provider key for backwards compatibility.
    provider_cfg = PROVIDER_CONFIG.get(provider, {})
    litellm_prefix = provider_cfg.get("litellm_provider_prefix", provider)
    model_string = f"{litellm_prefix}/{model}"
    
    thoughts = None
    content = ""

    # Filter out None values from parameters (e.g., top_p with default None)
    filtered_params = {k: v for k, v in parameters.items() if v is not None}

    # Inject a cacheable prefix block when requested and supported. Claude
    # models only; the OpenAI 'responses' path below builds its own input
    # from the original `messages`, so leaving effective_messages in
    # block-form does not affect it.
    effective_messages = messages
    if cache_prefix and _supports_prompt_caching(provider, model):
        effective_messages = _inject_cache_prefix(messages, cache_prefix)

    kwargs = {
        "model": model_string,
        "messages": effective_messages,
        **filtered_params,
    }

    reasoning_effort = kwargs.pop('reasoning_effort', 'disable')
    # Set when the llmao gate below consumes reasoning_effort into
    # extra_body, so the per-provider re-adds further down leave it alone.
    _llmao_thinking_handled = False
    
    # Self-hosted reasoning models (Qwen3, Gemma 4 via llmao's vLLM proxy)
    # think by default. They do NOT understand OpenAI's reasoning_effort; the
    # switch is the chat-template kwarg enable_thinking, passed to vLLM via
    # extra_body. Translate our reasoning_effort intent into it.
    # reasoning_effort == 'disable' -> thinking off.
    #
    # GATE: this applies ONLY to providers served from a custom
    # OpenAI-compatible endpoint (llmao -> vLLM), identified by the provider
    # declaring api_base_env_var. chat_template_kwargs is a vLLM concept, and
    # Bedrock's Converse API rejects any unknown top-level field with
    # 400 "extra_body: Extra inputs are not permitted".
    #
    # Do NOT gate on supports_thinking alone. That flag is True on the Bedrock
    # Anthropic models as well -- it means "this model can do extended
    # thinking", NOT "this model accepts enable_thinking". Gating on it alone
    # attached extra_body to Sonnet and failed every architecture
    # classification batch on 2026-07-23. api_base_env_var is the real
    # discriminator (llmao is the only provider that sets it), so Bedrock is
    # excluded by construction rather than by an explicit denylist.
    #
    # reasoning_effort is popped above and re-applied per-provider further
    # down (the Anthropic/Bedrock paths re-add it, and the Opus 4.7+ path
    # translates it into thinking + output_config), so popping it here does
    # not strip thinking from the Bedrock tiers.
    #
    # NOTE: both llmao's proxy and gofannon run litellm with drop_params=True,
    # so if this param is malformed or the vLLM server lacks the matching
    # --reasoning-parser, it is silently dropped and thinking stays ON. Verify
    # with a probe call: a disabled response must have empty/no reasoning_content
    # and no <think> block. (llmao serves these with --reasoning-parser gemma4 /
    # qwen3; this per-call value is explicit in both directions, so it does not
    # depend on a server-side default.)
    if provider_cfg.get("api_base_env_var") and model_config.get("supports_thinking"):
        _enable_thinking = (reasoning_effort != 'disable')
        _extra = dict(kwargs.get("extra_body", {}))
        _ctk = dict(_extra.get("chat_template_kwargs", {}))
        _ctk["enable_thinking"] = _enable_thinking
        _extra["chat_template_kwargs"] = _ctk
        kwargs["extra_body"] = _extra
        # These models do not accept OpenAI's reasoning_effort: qwen3.8-27b
        # takes only xhigh/medium/low, the others reject it outright, and
        # 'enable' is not a value any of them take.
        _llmao_thinking_handled = True

    if tools:
        kwargs["tools"] = tools

    # Set timeout for the LLM call
    effective_timeout = timeout or DEFAULT_LLM_TIMEOUT
    kwargs["timeout"] = effective_timeout

    if user_service is None:
        user_service = get_user_service(get_database_service(settings))
    if user_id is None:
        user_id = "anonymous"

    if user_service and user_id:
        user_service.require_allowance(user_id, basic_info=user_basic_info)

    # Get the effective API key (user's key takes precedence over env var)
    api_key = None
    if user_service and user_id:
        api_key = user_service.get_effective_api_key(user_id, provider, basic_info=user_basic_info)
    # If no user-specific key, litellm will use environment variables
    if api_key:
        kwargs["api_key"] = api_key

    # Anthropic 1P: mint a bearer token by exchanging an AWS STS web identity
    # JWT. Dispatched on a config key rather than a provider name, matching
    # the pattern the removed bedrock-mythos branch used.
    if provider_cfg.get("auth_mechanism") == "wif":
        from services.anthropic_wif import get_anthropic_1p_bearer_token
        kwargs["api_key"] = await get_anthropic_1p_bearer_token(
            # Require the token to outlive this call, not merely to be valid
            # at dispatch. 1P tokens last ~600s and HEAVY-tier calls run to
            # 1800s, so a token valid at dispatch can die mid-flight -- and
            # the retry would then replay it and fail identically, which
            # looks like a timeout cascade rather than an auth failure.
            min_validity_seconds=int(effective_timeout or 0),
        )

    # Providers served from a custom OpenAI-compatible endpoint (the llmao
    # provider -> llmao's litellm proxy). Without api_base, litellm's "openai"
    # handler would call api.openai.com. Fail loudly on a missing base rather
    # than silently sending internal prompts to a public endpoint.
    _api_base_var = provider_cfg.get("api_base_env_var")
    if _api_base_var:
        _api_base = os.environ.get(_api_base_var)
        if not _api_base:
            raise ValueError(
                f"provider '{provider}' requires {_api_base_var} to be set "
                f"(the base URL of its OpenAI-compatible endpoint); refusing "
                f"to fall back to the default OpenAI endpoint."
            )
        kwargs["api_base"] = _api_base
        # Set the key explicitly too. These providers borrow litellm's
        # "openai" handler, which would otherwise pick up OPENAI_API_KEY from
        # the environment and send it to our own proxy.
        _key_var = provider_cfg.get("api_key_env_var")
        if _key_var:
            _key = os.environ.get(_key_var)
            if not _key:
                raise ValueError(
                    f"provider '{provider}' requires {_key_var} to be set "
                    f"(a litellm virtual key for this caller)."
                )
            kwargs["api_key"] = _key


    # Only use aresponses API when we actually need its features (tools or reasoning)
    # Otherwise use standard acompletion which is more reliable
    use_responses_api = (
        api_style == "responses" and 
        (tools or reasoning_effort != 'disable')
    )

    if use_responses_api:
        # Use aresponses and aget_responses for OpenAI's special tools like built-in web search
        kwargs.pop('messages', None) # aresponses uses 'input' not 'messages'
        if reasoning_effort != 'disable':
            kwargs['reasoning'] = {'effort': reasoning_effort, 'summary': 'auto'}

        try:
            # For 'responses' API style, system prompt is 'instructions' and user prompt is 'input'
            system_prompts = [msg["content"] for msg in messages if msg["role"] == "system"]
            other_messages = [msg for msg in messages if msg["role"] != "system"]

            if system_prompts:
                kwargs["instructions"] = "\n\n".join(system_prompts)

            # Note: The 'input' for aresponses is the last user message. Conversation history is not directly supported.
            input_text = ""
            for msg in reversed(other_messages):
                if msg.get("role") == "user":
                    msg_content = msg.get("content", "")
                    if isinstance(msg_content, str):
                        input_text = msg_content
                    elif isinstance(msg_content, list):
                        # Handle list-format content (e.g., multimodal)
                        input_text = " ".join(
                            item.get("text", "") for item in msg_content 
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    break
            
            if not input_text:
                # Fall back to standard completion if no user input found
                kwargs['messages'] = messages
                if reasoning_effort != 'disable':
                    kwargs['reasoning_effort'] = reasoning_effort
                    kwargs.pop('reasoning', None)
                response = await litellm.acompletion(**kwargs)
                message = response.choices[0].message
                content = message.content if isinstance(message.content, str) else ""
                return content, None
            
            response_obj = await litellm.aresponses(input=input_text, **kwargs)
            
            final_response = None
            for _ in range(15): # Poll for up to 30 seconds
                await asyncio.sleep(2)
                # Pass api_key to aget_responses as well (needed when using user-specific keys)
                poll_kwargs = {"response_id": response_obj.id}
                if api_key:
                    poll_kwargs["api_key"] = api_key
                response_status = await litellm.aget_responses(**poll_kwargs)
                if response_status.status == "completed":
                    final_response = response_status
                    break
            
            if final_response:
                # Helper to get attribute from object or dict
                def get_attr(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)
                
                # Extract thoughts/summary from the first output block
                output_list = get_attr(final_response, 'output', [])
                if len(output_list) > 0 and output_list[0]:
                    first_output = output_list[0]
                    summary = get_attr(first_output, 'summary')
                    if summary:
                        summary_texts = []
                        for s in summary:
                            text = get_attr(s, 'text')
                            if text:
                                summary_texts.append(text)
                        thoughts = {"summary": summary_texts}
                    else:
                        # Fallback for other tool outputs
                        if hasattr(first_output, 'model_dump'):
                            thoughts = first_output.model_dump()
                        elif isinstance(first_output, dict):
                            thoughts = first_output
                
                # Extract content from the second output block (or first if it's the only one with content)
                if len(output_list) > 1 and output_list[1]:
                    second_output = output_list[1]
                    second_content = get_attr(second_output, 'content')
                    if second_content and len(second_content) > 0:
                        content = get_attr(second_content[0], 'text', '')
                
                if not content and len(output_list) > 0 and output_list[0]:
                    # Fallback if there's only one output block
                    first_content = get_attr(output_list[0], 'content')
                    if first_content and len(first_content) > 0:
                        content = get_attr(first_content[0], 'text', '')
                
                # If content is still empty, try to extract from any output block
                if not content:
                    for output_item in output_list:
                        if output_item:
                            item_content = get_attr(output_item, 'content')
                            if item_content:
                                for content_block in item_content:
                                    text = get_attr(content_block, 'text', '')
                                    if text:
                                        content = text
                                        break
                            if content:
                                break
                
                # Final validation - log if we still have no content
                if not content:
                    observability = get_observability_service()
                    observability.log(
                        level="WARNING",
                        event_type="empty_response_content",
                        message="Could not extract content from aresponses API response",
                        user_id=user_id,
                        metadata={
                            "model": model_string,
                            "output_list_length": len(output_list),
                            "output_structure": str(output_list)[:500]  # Truncate for logging
                        }
                    )
            else:
                raise Exception("Polling for OpenAI Responses API timed out.")

        except Exception as e:
            observability = get_observability_service()
            
            # Check for authentication errors and provide user-friendly message
            error_str = str(e).lower()
            if "invalid_api_key" in error_str or "authentication" in error_str or "api key" in error_str:
                key_source = "user-specific" if api_key else "environment variable"
                error_msg = f"Invalid API key for {provider}. Please check your {key_source} API key in your profile settings."
                observability.log(
                    level="ERROR",
                    event_type="invalid_api_key",
                    message=error_msg,
                    user_id=user_id,
                    metadata={
                        "provider": provider,
                        "key_source": key_source,
                        "original_error": str(e)[:200],
                    }
                )
                raise ValueError(error_msg) from e
            
            # Check for context window overflow errors
            # TODO make this generalized for any provider
            if "prompt is too long" in error_str or "context_length_exceeded" in error_str or "maximum context length" in error_str:
                context_window = model_config.get("context_window", "unknown")
                error_msg = (
                    f"Prompt exceeded {model}'s context window of {context_window} tokens. "
                    f"Use hierarchical consolidation to process data in smaller groups."
                )
                observability.log(
                    level="ERROR",
                    event_type="context_window_exceeded",
                    message=error_msg,
                    user_id=user_id,
                    metadata={
                        "provider": provider,
                        "model": model,
                        "context_window": context_window,
                        "original_error": str(e)[:500],
                    }
                )
                raise ValueError(error_msg) from e

            observability.log_exception(
                e,
                user_id=user_id,
                metadata={
                    "context": "litellm.aresponses",
                    "model": model_string,
                    "provider": provider,
                }
            )
            raise
    else:
        # Standard acompletion call for most models
        if reasoning_effort != 'disable' and not _llmao_thinking_handled:
            if _is_opus_4_7_or_later(model):
                # Opus 4.7+ rejects the old thinking.type.enabled format
                # that litellm's reasoning_effort translation produces.
                # Set the adaptive-thinking params directly and skip
                # litellm's translation.
                kwargs['thinking'] = {'type': 'adaptive'}
                kwargs['output_config'] = {'effort': reasoning_effort}
            else:
                kwargs['reasoning_effort'] = reasoning_effort

        # Retry loop. Two exception classes are retryable here:
        #   - litellm.Timeout       — slow network or model; modest linear backoff.
        #   - litellm.RateLimitError — provider throttling (Bedrock 429,
        #     OpenAI 429); needs slow exponential backoff with jitter so
        #     parallel callers don't synchronise their retries into a
        #     thundering herd.
        # Other exceptions fall through to the error-classifier branch
        # below (auth, context window, generic). Those are not retryable.
        last_exception = None
        # +MAX_AUTH_REMINT_RETRIES: an expired bearer costs an attempt, and
        # without its own budget an auth failure would eat into the timeout
        # or rate-limit allowance.
        max_attempts = (
            1
            + MAX_TIMEOUT_RETRIES
            + MAX_RATE_LIMIT_RETRIES
            + MAX_OVERLOADED_RETRIES
            + MAX_AUTH_REMINT_RETRIES
        )
        timeout_attempts_used = 0
        rate_limit_attempts_used = 0
        overloaded_attempts_used = 0
        auth_remint_attempts_used = 0

        async def _refresh_bearer_token_if_needed() -> None:
            """Re-mint the bearer before a retry.

            The token was minted before the loop. A long call (up to 1800s)
            can outlive the STS credentials that signed it, in which case
            every retry would replay the SAME dead token and fail
            identically -- which is exactly what a 3x900s "timeout" cascade
            looks like from the outside. Invalidate and re-mint so each
            attempt gets a token good for its own duration.
            """
            if provider_cfg.get("auth_mechanism") != "wif":
                return
            from services.anthropic_wif import (
                invalidate_anthropic_1p_token,
                get_anthropic_1p_bearer_token,
            )
            invalidate_anthropic_1p_token()
            kwargs["api_key"] = await get_anthropic_1p_bearer_token(
                min_validity_seconds=int(effective_timeout or 0),
            )

        for attempt in range(max_attempts):
            try:
                response = await litellm.acompletion(**kwargs)
                break  # Success
            except litellm.AuthenticationError as e:
                # Bedrock rejects an expired presigned token as an auth
                # error. The cached bearer is poisoned for this whole
                # worker, so drop it and retry with a fresh one --
                # otherwise every later call on this process inherits it.
                # Only retry for minted tokens: for a static API key a
                # re-mint changes nothing and the key is genuinely bad.
                last_exception = e
                if (
                    provider_cfg.get("auth_mechanism") == "wif"
                    and auth_remint_attempts_used < MAX_AUTH_REMINT_RETRIES
                ):
                    auth_remint_attempts_used += 1
                    print(
                        f"  Auth error on attempt {attempt + 1} for "
                        f"{model_string}; re-minting bearer token and "
                        f"retrying "
                        f"({auth_remint_attempts_used}/"
                        f"{MAX_AUTH_REMINT_RETRIES})...",
                        flush=True,
                    )
                    await _refresh_bearer_token_if_needed()
                    continue
                raise
            except litellm.Timeout as e:
                last_exception = e
                if timeout_attempts_used < MAX_TIMEOUT_RETRIES:
                    timeout_attempts_used += 1
                    wait_secs = 5 * timeout_attempts_used
                    print(
                        f"  Timeout on attempt {attempt + 1} "
                        f"for {model_string} (timeout={effective_timeout}s). "
                        f"Retrying in {wait_secs}s...",
                        flush=True,
                    )
                    await asyncio.sleep(wait_secs)
                    # A timeout is indistinguishable from an expired-token
                    # stall at this layer, so refresh before retrying.
                    await _refresh_bearer_token_if_needed()
                    continue
                raise
            except litellm.RateLimitError as e:
                last_exception = e
                if rate_limit_attempts_used < MAX_RATE_LIMIT_RETRIES:
                    # Exponential backoff with jitter. The 2**n curve
                    # plus uniform(0, n+1) jitter spreads parallel
                    # callers' retries across time so the next attempt
                    # batch doesn't arrive at the provider as
                    # synchronously as the failed batch did.
                    backoff = min(
                        RATE_LIMIT_BACKOFF_CAP_SECONDS,
                        (2 ** rate_limit_attempts_used)
                        + random.uniform(0, rate_limit_attempts_used + 1),
                    )
                    rate_limit_attempts_used += 1
                    print(
                        f"  RateLimit on attempt {attempt + 1} "
                        f"for {model_string}; sleeping {backoff:.1f}s "
                        f"(retry {rate_limit_attempts_used}/{MAX_RATE_LIMIT_RETRIES})",
                        flush=True,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # Exhausted rate-limit retries. Log and re-raise so the
                # caller can decide whether to apply a domain-specific
                # fallback (e.g., the bundle's score=5 default for failed
                # relevance batches).
                observability = get_observability_service()
                observability.log_exception(
                    e,
                    user_id=user_id,
                    metadata={
                        "context": "litellm.acompletion",
                        "model": kwargs.get('model'),
                        "provider": provider,
                        "tools": kwargs.get('tools'),
                        "rate_limit_retries_exhausted": MAX_RATE_LIMIT_RETRIES,
                    }
                )
                raise
            except Exception as e:
                # Provider overload (HTTP 529 / overloaded_error). litellm
                # surfaces this as APIConnectionError or a generic exception,
                # NOT RateLimitError, so it is caught here and routed through
                # the same exponential-backoff-with-jitter as rate-limit before
                # falling through to the terminal error classifier. 529 means
                # "provider busy, retry" and is highly likely to succeed on a
                # backed-off retry.
                _err_str = str(e).lower()
                _is_overloaded = (
                    "overloaded" in _err_str
                    or "529" in _err_str
                    or "\"type\":\"overloaded_error\"" in _err_str
                )
                # Transient server-side failures from the provider: HTTP 500
                # with "api_error", and litellm's ServiceUnavailableError. The
                # Mantle endpoint returns these under load ("The server had an
                # error while processing your request") and they succeed on a
                # backed-off retry just like a 529 -- but they are NOT caught by
                # the overloaded checks above (different type/status), so
                # without this they fail the work item on the first hit. Routed
                # through the same backoff budget as overload.
                _is_transient_5xx = (
                    "serviceunavailable" in _err_str
                    or "500 internal server error" in _err_str
                    or "\"type\":\"api_error\"" in _err_str
                    or "the server had an error" in _err_str
                )
                if (_is_overloaded or _is_transient_5xx) and overloaded_attempts_used < MAX_OVERLOADED_RETRIES:
                    last_exception = e
                    backoff = min(
                        RATE_LIMIT_BACKOFF_CAP_SECONDS,
                        (2 ** overloaded_attempts_used)
                        + random.uniform(0, overloaded_attempts_used + 1),
                    )
                    overloaded_attempts_used += 1
                    _kind = "overloaded (529)" if _is_overloaded else "server error (5xx)"
                    print(
                        f"  {_kind} on attempt {attempt + 1} "
                        f"for {model_string}; sleeping {backoff:.1f}s "
                        f"(retry {overloaded_attempts_used}/{MAX_OVERLOADED_RETRIES})",
                        flush=True,
                    )
                    await asyncio.sleep(backoff)
                    continue
                observability = get_observability_service()
                
                # Check for authentication errors and provide user-friendly message
                error_str = str(e).lower()
                if "invalid_api_key" in error_str or "authentication" in error_str or "api key" in error_str:
                    key_source = "user-specific" if api_key else "environment variable"
                    error_msg = f"Invalid API key for {provider}. Please check your {key_source} API key in your profile settings."
                    observability.log(
                        level="ERROR",
                        event_type="invalid_api_key",
                        message=error_msg,
                        user_id=user_id,
                        metadata={
                            "provider": provider,
                            "key_source": key_source,
                            "original_error": str(e)[:200],
                        }
                    )
                    raise ValueError(error_msg) from e
                
                # Check for context window overflow errors
                if "prompt is too long" in error_str or "context_length_exceeded" in error_str or "maximum context length" in error_str:
                    context_window = model_config.get("context_window", "unknown")
                    error_msg = (
                        f"Prompt exceeded {model}'s context window of {context_window} tokens. "
                        f"Use hierarchical consolidation to process data in smaller groups."
                    )
                    observability.log(
                        level="ERROR",
                        event_type="context_window_exceeded",
                        message=error_msg,
                        user_id=user_id,
                        metadata={
                            "provider": provider,
                            "model": model,
                            "context_window": context_window,
                            "original_error": str(e)[:500],
                        }
                    )
                    raise ValueError(error_msg) from e

                observability.log_exception(
                    e,
                    user_id=user_id,
                    metadata={
                        "context": "litellm.acompletion",
                        "model": kwargs.get('model'),
                        "provider": provider,
                        "tools": kwargs.get('tools'),
                    }
                )
                raise
        message = response.choices[0].message
        content = message.content if isinstance(message.content, str) else ""
        
        # Extract various forms of "thoughts"
        thoughts_payload = {}
        if message.tool_calls:
            thoughts_payload['tool_calls'] = [tc.model_dump() for tc in message.tool_calls]
        
        if hasattr(message, 'reasoning_content') and message.reasoning_content:
            thoughts_payload['reasoning_content'] = message.reasoning_content

        if isinstance(message.content, list): # Handle Anthropic's block-based content
            content_blocks = message.content
            thought_blocks = [block for block in content_blocks if block.get("type") == "thought"]
            tool_use_blocks = [block for block in content_blocks if block.get("type") == "tool_use"]
            text_blocks = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
            
            if thought_blocks:
                thoughts_payload['anthropic_thoughts'] = thought_blocks
            if tool_use_blocks:
                thoughts_payload['anthropic_tool_uses'] = tool_use_blocks
            
            content = "\n".join(text_blocks)

        thoughts = thoughts_payload if thoughts_payload else None

    # Ensure thoughts are JSON serializable
    if thoughts is not None:
        thoughts = json.loads(json.dumps(thoughts, default=str))

    if user_service and user_id:
        response_cost = None
        try:
            response_cost = _extract_response_cost(final_response if use_responses_api else response)
        except Exception:
            response_cost = None
        if response_cost is not None:
            user_service.add_usage(user_id, response_cost, basic_info=user_basic_info)

    return content, thoughts


async def stream_llm(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    user_service: Optional[UserService] = None,
    user_id: Optional[str] = None,
    user_basic_info: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Any, None]:
    """
    Stream LLM responses using litellm's async streaming.

    This function provides streaming capability while maintaining the same
    interface and checks as call_llm (user allowance, logging, etc).

    Yields chunks from the LLM response stream.

    Note: Cost tracking is not available for streaming responses.
    """
    # If the provider config supplies an explicit litellm prefix (e.g.
    # anthropic-1p -> "anthropic"), use it so litellm routes
    # through its native handler for that backend. Defaults to the
    # gofannon provider key for backwards compatibility.
    provider_cfg = PROVIDER_CONFIG.get(provider, {})
    model_config = PROVIDER_CONFIG.get(provider, {}).get("models", {}).get(model, {})
    litellm_prefix = provider_cfg.get("litellm_provider_prefix", provider)
    model_string = f"{litellm_prefix}/{model}"

    # Filter out None values from parameters
    filtered_params = {k: v for k, v in parameters.items() if v is not None}

    kwargs = {
        "model": model_string,
        "messages": messages,
        "stream": True,
        **filtered_params,
    }

    # Reasoning, mirroring the gate in call_llm.
    #
    # This previously read `kwargs.pop('reasoning_effort', None)` with the
    # note "not typically used in streaming", which discarded the caller's
    # intent without acting on it. For Bedrock that was harmless -- the
    # parameter simply went unused. For the llmao provider it was not: those
    # models are reached through a vLLM endpoint where reasoning is
    # controlled by chat_template_kwargs.enable_thinking, and sending nothing
    # leaves each model at ITS OWN default. Two of the three think by
    # default, and on qwen3-8b that means the whole token budget is spent
    # reasoning and the stream carries no content at all -- measured, 400
    # chunks and zero output.
    #
    # So the pop is kept (reasoning_effort must never reach vLLM: it accepts
    # that parameter on one of the three models and rejects it on the
    # others), but the value is now translated first.
    #
    # GATE: api_base_env_var, not supports_thinking alone. That flag is also
    # True on the Bedrock Anthropic models, where it means "can do extended
    # thinking", NOT "accepts enable_thinking" -- Converse rejects unknown
    # top-level fields with 400 "extra_body: Extra inputs are not permitted".
    # Only the llmao provider sets api_base_env_var, so Bedrock is excluded
    # by construction rather than by a denylist.
    _reasoning_effort = kwargs.pop('reasoning_effort', 'disable')

    if provider_cfg.get("api_base_env_var") and model_config.get("supports_thinking"):
        _extra = dict(kwargs.get("extra_body", {}))
        _ctk = dict(_extra.get("chat_template_kwargs", {}))
        _ctk["enable_thinking"] = (_reasoning_effort != 'disable')
        _extra["chat_template_kwargs"] = _ctk
        kwargs["extra_body"] = _extra

    if user_service is None:
        user_service = get_user_service(get_database_service(settings))
    if user_id is None:
        user_id = "anonymous"

    if user_service and user_id:
        user_service.require_allowance(user_id, basic_info=user_basic_info)

    # Get the effective API key (user's key takes precedence over env var)
    api_key = None
    if user_service and user_id:
        api_key = user_service.get_effective_api_key(user_id, provider, basic_info=user_basic_info)

    # If no user-specific key, litellm will use environment variables
    if api_key:
        kwargs["api_key"] = api_key

    # Anthropic 1P: mint a short-lived bearer token at request time. The
    # token overrides any user-specific key above, since WIF auth is
    # account-scoped rather than per-user.
    if provider_cfg.get("auth_mechanism") == "wif":
        from services.anthropic_wif import get_anthropic_1p_bearer_token
        kwargs["api_key"] = await get_anthropic_1p_bearer_token(
            # Streaming has no retry loop, so a token that dies mid-stream
            # can't be recovered -- require it to cover the whole call.
            min_validity_seconds=int(kwargs.get("timeout") or 0),
        )

    # Custom OpenAI-compatible endpoint (llmao proxy). Same
    # reasoning as the non-streaming path: fail loudly rather than defaulting
    # to api.openai.com with OPENAI_API_KEY.
    _api_base_var = provider_cfg.get("api_base_env_var")
    if _api_base_var:
        _api_base = os.environ.get(_api_base_var)
        if not _api_base:
            raise ValueError(
                f"provider '{provider}' requires {_api_base_var} to be set."
            )
        kwargs["api_base"] = _api_base
        _key_var = provider_cfg.get("api_key_env_var")
        if _key_var:
            _key = os.environ.get(_key_var)
            if not _key:
                raise ValueError(
                    f"provider '{provider}' requires {_key_var} to be set."
                )
            kwargs["api_key"] = _key

    try:
        response = await litellm.acompletion(**kwargs)
        async for chunk in response:
            yield chunk
    except Exception as e:
        observability = get_observability_service()
        observability.log_exception(
            e,
            user_id=user_id,
            metadata={
                "context": "stream_llm",
                "model": model_string,
                "provider": provider,
            }
        )
        raise