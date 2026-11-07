import asyncio
import json
from dataclasses import dataclass
import os
import random
import re
from config import settings
from config.provider_config import PROVIDER_CONFIG
# services.anthropic_wif (which imports boto3) is imported lazily inside the
# auth_mechanism == "wif" branches below, so a deployment without a WIF
# provider never loads it. boto3 is transitively present anyway via
# storage_service / dynamodb / observability_service.
from services.database_service import get_database_service
from services.user_service import get_user_service
from typing import Any, AsyncGenerator, Dict, List, Tuple, Optional
import httpx
import litellm

from services.litellm_logger import ensure_litellm_logging
from services.observability_service import get_observability_service
from services.user_service import UserService
from services.cancel_token import check_should_stop  # ISSUE-007

ensure_litellm_logging()

# Configure litellm defaults
litellm.drop_params = True
litellm.set_verbose = False

# ---------------------------------------------------------------------------
# litellm model-map registration, declared in provider config
# ---------------------------------------------------------------------------
# litellm decides several things by looking the model string up in its own
# model map: whether output_config.effort is supported (else it is DROPPED
# under drop_params, with only a log warning), cost tracking, max tokens, and
# the second gate in _supports_prompt_caching(). A model string the map does
# not know misses silently on all of them.
#
# The fix is to register an entry for that string. Which models need one, and
# what the entry says, is a fact about the model -- so it lives on the model's
# config entry, not here:
#
#     "litellm_registration": {
#         "copy_from": "<existing litellm map key>",   # optional
#         "overrides": {...},                          # merged on top
#         "aliases":   ["<other string>", ...],        # optional extra keys
#     }
#
# copy_from inherits whatever capability flags and costs litellm already knows
# rather than hand-writing them. If the source key is missing (a litellm
# upgrade renamed it) the entry is SKIPPED with a warning rather than failing
# startup -- the same guard the hard-coded Mythos block had.
#
# This replaces two hard-coded Mythos blocks. The live one copied
# anthropic.claude-mythos-preview onto the 1P string "claude-mythos-5"; it is
# now declared on that entry in config/anthropic_1p. The other registered
# "anthropic.claude-mythos-5" and "bedrock/mantle/anthropic.claude-mythos-5"
# for the removed bedrock-mythos provider; no config entry sends either
# string, so it is gone rather than migrated.


def _register_litellm_models(provider_config: Dict[str, Any]) -> List[str]:
    """Register every model that declares `litellm_registration`.

    Returns the list of keys registered, for tests and startup logging.
    Never raises: model metadata must not be able to break import.
    """
    registered: List[str] = []
    for provider_name, provider_cfg in provider_config.items():
        for model_name, model_cfg in (provider_cfg.get("models") or {}).items():
            reg = model_cfg.get("litellm_registration")
            if not reg:
                continue
            src = reg.get("copy_from")
            if src:
                if src not in litellm.model_cost:
                    print(
                        f"WARNING: {provider_name}/{model_name}: litellm model "
                        f"map has no {src!r} to copy from; not registered. "
                        f"Effort may be silently dropped for this model.",
                        flush=True,
                    )
                    continue
                info = {**litellm.model_cost[src], **(reg.get("overrides") or {})}
            else:
                info = dict(reg.get("overrides") or {})
            keys = [model_name, *(reg.get("aliases") or [])]
            try:
                litellm.register_model({k: info for k in keys})
                registered.extend(keys)
            except Exception as e:  # pragma: no cover - never fail import over metadata
                print(
                    f"WARNING: could not register {provider_name}/{model_name} "
                    f"with litellm: {e}",
                    flush=True,
                )
    return registered


_register_litellm_models(PROVIDER_CONFIG)

# Silences the "Provider List / Give Feedback" banner litellm prints via
# print() (not logging) whenever a model-registry lookup misses. Redundant for
# models registered above, but covers any other unregistered model string
# and keeps ~40% of noise out of the agent logs.
litellm.suppress_debug_info = True
# ---------------------------------------------------------------------------

# Default timeout (seconds) for LLM calls. Override with LLM_TIMEOUT_SECONDS env var.
DEFAULT_LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT_SECONDS", "600"))

# Limit (seconds) on opening the TCP/TLS connection, separate from the total.
# With one scalar timeout, an upstream that drops SYNs held each attempt for
# the full LLM_TIMEOUT_SECONDS before failing (ISSUE-021). litellm applies a
# separate connect limit for the openai (which includes llmao), azure and
# bedrock providers; for others it uses the total, as before.
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "10"))


# How long a streamed call may go between chunks, once it has started
# sending, before it is treated as stalled (seconds). Only applies when
# call_llm streams on the wire. Waiting for the FIRST chunk is not limited by
# it: a request queued on a busy model server sends nothing until it is
# scheduled, and retrying it would only queue it again. The call's own timeout
# bounds that wait and the call as a whole.
LLM_STREAM_IDLE_TIMEOUT = float(os.getenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "300"))


def _http_timeout(total: float) -> httpx.Timeout:
    """``total`` for reading and writing; at most LLM_CONNECT_TIMEOUT to connect."""
    return httpx.Timeout(total, connect=min(LLM_CONNECT_TIMEOUT, total))

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


# --- Transient-failure classification ------------------------------------
#
# These predicates decide whether a provider exception is retried from the
# MAX_OVERLOADED_RETRIES budget or fails the call on first contact. They are
# module-level rather than inline in the retry loop so they can be unit-tested
# directly: see tests/unit/services/test_llm_service_retries.py.
#
# All three take the exception string ALREADY LOWERCASED, as the call site
# does with str(e).lower().


# 529 as a standalone token, not as three digits inside a longer run of
# characters. A bare `"529" in err_str` also matched a token count ("5290
# input tokens"), a truncation offset ("1529 tokens"), a model name
# ("qwen-529b") and an identifier ("req_529_ab"), each of which would open
# the retry budget for an error that then fails identically on every
# attempt. The boundaries exclude digits, letters and underscores on both
# sides, so the status code in "Error code: 529 - {...}" still matches.
_HTTP_529_RE = re.compile(r"(?<![0-9a-z_])529(?![0-9a-z_])")


def _is_overloaded_error(err_str: str) -> bool:
    """Provider overload: HTTP 529 / overloaded_error.

    litellm surfaces this as APIConnectionError or a generic exception, NOT
    RateLimitError, so it is caught in the generic handler and routed through
    the same exponential-backoff-with-jitter as rate-limit before falling
    through to the terminal error classifier. 529 means "provider busy,
    retry" and is highly likely to succeed on a backed-off retry.

    "overloaded" remains the primary matcher: a real 529 carries
    overloaded_error in its body, so narrowing the numeric test cannot lose
    a genuine overload.
    """
    return (
        "overloaded" in err_str
        or bool(_HTTP_529_RE.search(err_str))
        or "\"type\":\"overloaded_error\"" in err_str
    )


def _is_gateway_error(err_str: str) -> bool:
    """A 502 from a reverse proxy in front of the model endpoint.

    When the upstream drops the connection mid-response, the proxy answers
    502 and litellm maps it to BadGatewayError. That is the same transient
    condition as a 503 or a 500 api_error: the request never reached a
    verdict, so a backed-off retry is likely to get one.

    The tests match a class name or a full phrase, never a bare "502", so an
    error string that merely contains those digits -- a token count like
    "50200 input tokens", a model named qwen-502b -- does not open the retry
    budget.

    504 is deliberately excluded. A gateway timeout means the proxy gave up
    waiting, so if its limit is shorter than the caller's, every long call
    would 504 and then retry, each attempt burning the full proxy timeout
    before failing again -- turning one slow failure into several. It belongs
    here only once the proxy's own limit has been measured against the
    request timeouts in use.
    """
    return (
        "badgateway" in err_str
        or "bad gateway" in err_str
        or "502 proxy error" in err_str
    )


def _is_transient_5xx_error(err_str: str) -> bool:
    """Transient server-side failures from the provider.

    HTTP 500 with "api_error", litellm's ServiceUnavailableError, endpoints
    that return "The server had an error while processing your request" under
    load, and gateway-class 502s. These succeed on a backed-off retry just
    like a 529, but are NOT matched by the overloaded checks (different
    type/status), so without this they fail the call on the first hit.

    "connection error" is here because litellm reports a dropped upstream
    that way with no status code in the text at all -- see
    _is_transient_exception, which is the primary matcher for that case.
    """
    return (
        "serviceunavailable" in err_str
        or "500 internal server error" in err_str
        or "\"type\":\"api_error\"" in err_str
        or "the server had an error" in err_str
        or "connection error" in err_str
        or _is_gateway_error(err_str)
    )


# Exception CLASSES whose instances are transient. Checked before any of the
# string matchers above, because the string is the provider's and the class is
# litellm's: every predicate here that matches on message text has eventually
# been defeated by a provider rewording its errors, while the class name is
# stable across backends and versions.
#
# Twice now, for the same reason:
#   BadGatewayError  -- "<title>502 Proxy Error</title>", matched by no test
#                       until "badgateway" was added to _is_gateway_error.
#   InternalServerError -- "InternalServerError: OpenAIException - Connection
#                       error." litellm writes the class as one token, so
#                       "500 internal server error" did not match, and
#                       "Connection error." matched nothing at all. Two audit
#                       bundles failed with one attempt each and the retry
#                       budget was never opened.
_TRANSIENT_EXC_NAMES = frozenset({
    "InternalServerError",      # HTTP 500 and litellm's catch-all for it
    "ServiceUnavailableError",  # HTTP 503
    "BadGatewayError",          # HTTP 502
    "APIConnectionError",       # socket dropped before a verdict
})

# Checked FIRST and overriding the set above, because the hierarchy nests a
# timeout under a transient parent: APIConnectionTimeoutError subclasses
# APIConnectionError, so walking the MRO alone would retry it.
#
# A timeout must not be retried. The caller already waited its full budget, so
# retrying burns that budget again per attempt and fails the same way -- one
# slow failure becomes several. This is the same reasoning that keeps 504 out
# of _is_gateway_error.
#
# ContextWindowExceededError subclasses BadRequestError rather than any server
# error, so the MRO walk would not catch it regardless; it is named anyway so
# the intent survives a future litellm reshuffle. It has to fail fast, because
# callers size their next prompt from it.
_TERMINAL_EXC_NAMES = frozenset({
    "Timeout",
    "APITimeoutError",
    "APIConnectionTimeoutError",
    "ContextWindowExceededError",
})


def _is_transient_exception(exc: BaseException) -> bool:
    """Classify a provider failure by exception class rather than message.

    The MRO is walked by NAME rather than by importing litellm's classes:
    litellm renames and re-homes these between versions, and an ImportError
    raised from this module would take down every call rather than one.
    """
    try:
        names = {cls.__name__ for cls in type(exc).__mro__}
    except Exception:  # pragma: no cover - defensive; __mro__ is always there
        names = {type(exc).__name__}
    if names & _TERMINAL_EXC_NAMES:
        return False
    return bool(names & _TRANSIENT_EXC_NAMES)


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
    # Where litellm itself puts the cost it computed: on every response it
    # returns, and on a response rebuilt from a stream (stream_chunk_builder),
    # which has no standard_logging_object.
    if hasattr(response_obj, "_hidden_params") and isinstance(response_obj._hidden_params, dict):
        cost_value = response_obj._hidden_params.get("response_cost")
        if cost_value is not None:
            try:
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

# How a model is told to reason. One value per model, declared in provider
# config as `thinking_style`, resolved once per call by _thinking_style().
#
#   "enable_thinking"   vLLM chat-template switch: extra_body.
#                       chat_template_kwargs.enable_thinking (+ the graded
#                       level, where the model declares thinking_levels).
#                       Self-hosted models behind llmao.
#   "adaptive"          thinking={"type":"adaptive"} + output_config.effort,
#                       set directly so litellm does not translate
#                       reasoning_effort into the legacy thinking.type.enabled
#                       shape these models reject. Opus 4.7+, Mythos.
#   "reasoning_effort"  pass reasoning_effort through for litellm to translate.
#                       Every other model.
#   "none"              send no reasoning parameter at all.
#
# This replaces _is_opus_4_7_or_later(), which decided the adaptive shape by
# substring-matching the model string ("claude-opus-4-7", "claude-opus-4-8",
# "claude-opus-5", "claude-mythos"). A name cannot know what a model accepts,
# and every new model meant editing that tuple. The four entries it matched
# now declare "adaptive" in config.
THINKING_STYLES = ("enable_thinking", "adaptive", "reasoning_effort", "none")


def _thinking_style(provider_cfg: Dict[str, Any], model_config: Dict[str, Any]) -> str:
    """Resolve a model's thinking style from config.

    An explicit `thinking_style` wins. Otherwise the default is derived so
    that every model not declaring one behaves exactly as before:

      - a provider served from a custom OpenAI-compatible endpoint (declares
        api_base_env_var -- only llmao does) whose model supports_thinking
        -> "enable_thinking". This is the gate that used to sit inline in
        call_llm and stream_llm; see the comment there for why it is
        api_base_env_var and not supports_thinking alone.
      - anything else -> "reasoning_effort". Deliberately NOT keyed on
        supports_effort: reasoning_effort has always been passed through for
        every non-adaptive model, and litellm (drop_params=True) decides per
        model whether it survives. Keying on supports_effort would stop
        sending it to e.g. Sonnet and Haiku, a behaviour change.

    Raises ValueError on an unknown explicit value: a typo in config should
    fail the call loudly, not fall back to a default that looks like it works.
    """
    style = model_config.get("thinking_style")
    if style is not None:
        if style not in THINKING_STYLES:
            raise ValueError(
                f"unknown thinking_style {style!r}; expected one of {THINKING_STYLES}"
            )
        return style
    if provider_cfg.get("api_base_env_var") and model_config.get("supports_thinking"):
        return "enable_thinking"
    return "reasoning_effort"


# Temperature, reconciled against what the model accepts. Three config flags,
# checked in this order; at most one of the last two may be set on a model:
#
#   rejects_temperature
#       The model rejects the parameter outright, thinking or not. Mythos:
#       HTTP 400 "`temperature` is deprecated for this model". Always dropped.
#
#   rejects_temperature_with_thinking
#       Rejected while thinking is on, except 1.0. Anthropic on Bedrock and
#       via OpenRouter: HTTP 400 for temperature + thinking, documented as
#       "Locked to 1.0 when thinking enabled", so 1.0 itself is accepted and
#       kept.
#
#   requires_nonzero_temperature_with_thinking
#       0.0 with thinking on drives the model into repetition loops inside the
#       thinking block. Self-hosted llmao models. An explicit 0.0 is DROPPED,
#       not raised to some chosen value: vLLM then applies the model's own
#       generation_config, which is the vendor-tuned value (Gemma 4 publishes
#       1.0; the Qwen family 0.6-1.0 depending on version and mode), and
#       failing that vLLM's own default of 1.0 -- never 0. An ABSENT
#       temperature is left alone for the same reason. This matters because
#       llmao's config default is 0.0 ("deterministic classification"), so any
#       caller taking defaults and turning thinking on lands exactly here.
#
# This replaces a copy of the same rule in each ASVS agent's _make_tier, keyed
# on provider NAME: {"bedrock", "bedrock_mythos", "anthropic"}. Two of those
# providers no longer exist, and the one Mythos now runs on, "anthropic-1p",
# was never in the set -- the agents were safe only because the Mythos tier's
# env omits temperature. The non-zero rule existed there only as a comment;
# nothing enforced it.
_NONZERO_TEMP_WARNED: set = set()


def _reconcile_temperature(
    kwargs: Dict[str, Any],
    model_config: Dict[str, Any],
    thinking_active: bool,
    model_label: str = "",
) -> None:
    """Adjust kwargs["temperature"] in place per the model's declared flags."""
    if "temperature" not in kwargs:
        return
    if model_config.get("rejects_temperature"):
        kwargs.pop("temperature", None)
        return
    if not thinking_active:
        return
    temperature = kwargs["temperature"]
    if model_config.get("rejects_temperature_with_thinking"):
        if temperature != 1.0:
            kwargs.pop("temperature", None)
        return
    if model_config.get("requires_nonzero_temperature_with_thinking"):
        try:
            is_zero = float(temperature) == 0.0
        except (TypeError, ValueError):
            is_zero = False
        if is_zero:
            kwargs.pop("temperature", None)
            if model_label not in _NONZERO_TEMP_WARNED:
                _NONZERO_TEMP_WARNED.add(model_label)
                print(
                    f"WARNING: {model_label}: temperature=0.0 with thinking on "
                    f"causes repetition loops on this model; dropped so the "
                    f"model's own default applies. Set a non-zero temperature "
                    f"for this tier to silence this. (Logged once per model.)",
                    flush=True,
                )


def _supports_prompt_caching(provider: str, model: str) -> bool:
    """Whether this model honours cache_control ephemeral blocks.

    Declared per model in the provider config, not inferred from the name.
    The previous implementation was `"claude" in model.lower()`, which is how
    every self-hosted model came to have its cache_prefix silently discarded
    -- a name match cannot know what a model supports, and the one place that
    knowledge belongs is the config.

    litellm's own model map is still consulted as a second gate for models
    that declare support, because it knows about cases the config should not
    have to track: Mythos is a Claude model but litellm reports
    supports_prompt_caching=false for it, and injecting the marker anyway
    converts the first message to block form to carry a cache_control the
    model ignores -- no cache hit, and a large inventory prefix re-sent on
    every batch expecting to be cached across the pass.

    Defaults to False: a model that has not declared the capability does not
    get the marker, and llm_service delivers its cache_prefix as prepended
    text instead. Nothing is lost either way.
    """
    model_config = (
        PROVIDER_CONFIG.get(provider, {}).get("models", {}).get(model, {})
    )
    if not model_config.get("supports_prompt_caching", False):
        return False
    return litellm.model_cost.get(model, {}).get("supports_prompt_caching", True)


def _prepend_prefix_to_message(
    messages: List[Dict[str, Any]], cache_prefix: str
) -> List[Dict[str, Any]]:
    """Prepend `cache_prefix` to the first message so it ALWAYS reaches the
    model, on providers that cannot carry it as a cache_control block.

    Without this, a caller passing cache_prefix= to a non-caching provider
    has the content silently discarded. asvs_audit did exactly that: it built
    the analysis system prompt and code inventory as a cache_prefix and passed
    nothing else, so every self-hosted HEAVY call analysed raw source with no
    auditor role, no ASVS rubric and no severity guidance. It looked like the
    model finding less; it was the model being asked less.

    Returns a NEW list; the caller's is not mutated.
    """
    if not messages:
        return [{"role": "user", "content": cache_prefix}]

    new_messages = [dict(m) for m in messages]
    first = new_messages[0]
    content = first.get("content", "")

    # Guard against double delivery. A caller that both inlines the prefix
    # AND passes it as cache_prefix would otherwise send it twice -- doubling
    # an already-large prompt and handing the model its instructions two
    # times over, with nothing raising.
    if isinstance(content, str):
        if content.startswith(cache_prefix):
            return new_messages
        first["content"] = f"{cache_prefix}\n\n{content}" if content else cache_prefix
    elif isinstance(content, list):
        blocks = list(content)
        if blocks and blocks[0].get("type") == "text" \
                and (blocks[0].get("text") or "").startswith(cache_prefix):
            return new_messages
        first["content"] = [{"type": "text", "text": cache_prefix}] + blocks
    else:
        first["content"] = cache_prefix

    new_messages[0] = first
    return new_messages


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



# ---------------------------------------------------------------------------
# Request setup and retry policy shared by call_llm and stream_llm
# ---------------------------------------------------------------------------
# These were two copies that drifted: stream_llm sent no reasoning to any
# model but llmao's, ignored cache_prefix and tools, had no retries, and
# never charged the user. Both entry points now build the request here and
# run it under the same retry policy.


@dataclass
class _LLMRequest:
    provider: str
    model: str
    model_string: str
    provider_cfg: Dict[str, Any]
    model_config: Dict[str, Any]
    kwargs: Dict[str, Any]
    reasoning_effort: str
    thinking_style: str
    timeout: float
    # The user's own key for this provider, if any. Minted bearer tokens and
    # proxy keys go straight into kwargs and are not kept here.
    api_key: Optional[str]
    user_service: Optional[UserService]
    user_id: Optional[str]
    user_basic_info: Optional[Dict[str, Any]]


async def _prepare_request(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    *,
    tools: Optional[List[Dict[str, Any]]],
    user_service: Optional[UserService],
    user_id: Optional[str],
    user_basic_info: Optional[Dict[str, Any]],
    timeout: Optional[float],
    cache_prefix: Optional[str],
) -> _LLMRequest:
    """Build the litellm request: model string, messages, reasoning,
    temperature, tools, timeout, credentials. Checks the user's allowance."""
    check_should_stop()  # ISSUE-007: cancel at structural boundary
    model_config = PROVIDER_CONFIG.get(provider, {}).get("models", {}).get(model, {})

    # If the provider config supplies an explicit litellm prefix (e.g.
    # anthropic-1p -> "anthropic"), use it so litellm routes
    # through its native handler for that backend. Defaults to the
    # gofannon provider key for backwards compatibility.
    provider_cfg = PROVIDER_CONFIG.get(provider, {})
    litellm_prefix = provider_cfg.get("litellm_provider_prefix", provider)
    model_string = f"{litellm_prefix}/{model}"


    # Filter out None values from parameters (e.g., top_p with default None)
    filtered_params = {k: v for k, v in parameters.items() if v is not None}
    # The timeout argument wins; a timeout inside parameters is the
    # streaming path's old spelling and is still honoured.
    _params_timeout = filtered_params.pop("timeout", None)

    # cache_prefix is a GUARANTEED-DELIVERED input whose transport varies by
    # capability: a cache_control block where the provider supports one,
    # prepended text where it does not. It is never dropped.
    #
    # Callers pass it ONCE and must not also inline it into the message --
    # the prepend path would then send it twice.
    #
    # The OpenAI 'responses' path below builds its own input from the
    # original `messages`, so leaving effective_messages in block-form does
    # not affect it.
    effective_messages = messages
    if cache_prefix:
        if _supports_prompt_caching(provider, model):
            effective_messages = _inject_cache_prefix(messages, cache_prefix)
        else:
            effective_messages = _prepend_prefix_to_message(messages, cache_prefix)

    kwargs = {
        "model": model_string,
        "messages": effective_messages,
        **filtered_params,
    }

    reasoning_effort = kwargs.pop('reasoning_effort', 'disable')
    # How this model is told to reason; see THINKING_STYLES. Resolved once
    # here and used by every branch below, so the llmao gate and the
    # adaptive/legacy split read the same decision.
    thinking_style = _thinking_style(provider_cfg, model_config)
    
    # Self-hosted reasoning models (Qwen3, Gemma 4 via llmao's vLLM proxy)
    # think by default. They do NOT understand OpenAI's reasoning_effort; the
    # switch is the chat-template kwarg enable_thinking, passed to vLLM via
    # extra_body. Translate our reasoning_effort intent into it.
    # reasoning_effort == 'disable' -> thinking off.
    #
    # GATE: thinking_style == "enable_thinking". By default that resolves ONLY
    # for providers served from a custom OpenAI-compatible endpoint (llmao ->
    # vLLM), identified by the provider declaring api_base_env_var. chat_template_kwargs is a vLLM concept, and
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
    # (_thinking_style() now holds that derivation; test_provider_config
    # checks that no model declares "enable_thinking" explicitly on a provider
    # without api_base_env_var.)
    #
    # reasoning_effort is popped above and re-applied per style further down
    # ("reasoning_effort" passes it through, "adaptive" translates it into
    # thinking + output_config), so popping it here does not strip thinking
    # from the Bedrock tiers.
    #
    # NOTE: both llmao's proxy and gofannon run litellm with drop_params=True,
    # so if this param is malformed or the vLLM server lacks the matching
    # --reasoning-parser, it is silently dropped and thinking stays ON. Verify
    # with a probe call: a disabled response must have empty/no reasoning_content
    # and no <think> block. (llmao serves these with --reasoning-parser gemma4 /
    # qwen3; this per-call value is explicit in both directions, so it does not
    # depend on a server-side default.)
    if thinking_style == "enable_thinking":
        _enable_thinking = (reasoning_effort != 'disable')
        _extra = dict(kwargs.get("extra_body", {}))
        _ctk = dict(_extra.get("chat_template_kwargs", {}))
        _ctk["enable_thinking"] = _enable_thinking

        # Some models grade their reasoning as well as switching it on, and
        # take the level in the same chat_template_kwargs. Send it when the
        # model declares the value as one it accepts; otherwise the level is
        # our internal intent and stops here, translated into the boolean
        # above.
        #
        # This matters more than it looks. Qwen3.8-27B's levels are
        # low/medium/xhigh and ITS DEFAULT IS XHIGH -- the maximum -- so a
        # call that turns thinking on without naming a level asks for maximum
        # deliberation. Measured on a 38k audit prompt, 8,000 budget, three
        # repeats, medians:
        #
        #     low      58s    3,726c thinking    6,438c content
        #     medium   78s    4,192c thinking    9,397c content
        #     xhigh   147s   23,110c thinking    4,976c content
        #
        # xhigh costs 1.9x the wall time to produce HALF the answer: it
        # spends the budget reasoning rather than answering. Without this
        # branch every self-hosted call runs there, and the tier's setting is
        # discarded.
        if reasoning_effort in (model_config.get("thinking_levels") or ()):
            _ctk["reasoning_effort"] = reasoning_effort
        _extra["chat_template_kwargs"] = _ctk
        kwargs["extra_body"] = _extra
        # These models do not accept OpenAI's reasoning_effort: qwen3.8-27b
        # takes only xhigh/medium/low, the others reject it outright, and
        # 'enable' is not a value any of them take. The standard-completion
        # branch below therefore sends nothing further for this style.

    # Thinking is on when the caller asked for it and the model's style
    # actually sends it. Applies to every branch below, responses API included.
    _reconcile_temperature(
        kwargs, model_config,
        thinking_active=(reasoning_effort != 'disable' and thinking_style != "none"),
        model_label=f"{provider}/{model}",
    )

    if tools:
        kwargs["tools"] = tools

    # Set timeout for the LLM call
    effective_timeout = timeout or _params_timeout or DEFAULT_LLM_TIMEOUT
    kwargs["timeout"] = _http_timeout(effective_timeout)

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

    return _LLMRequest(
        provider=provider,
        model=model,
        model_string=model_string,
        provider_cfg=provider_cfg,
        model_config=model_config,
        kwargs=kwargs,
        reasoning_effort=reasoning_effort,
        thinking_style=thinking_style,
        timeout=effective_timeout,
        api_key=api_key,
        user_service=user_service,
        user_id=user_id,
        user_basic_info=user_basic_info,
    )


def _apply_completion_reasoning(req: _LLMRequest) -> None:
    """Add the reasoning parameters chat completion needs for this style.

    "enable_thinking" is already in extra_body (see _prepare_request), and
    "none" takes no reasoning parameter. The OpenAI 'responses' path sends
    reasoning its own way and does not call this.
    """
    if req.reasoning_effort == 'disable':
        return
    if req.thinking_style == "adaptive":
        # Opus 4.7+ and Mythos reject the old thinking.type.enabled
        # format that litellm's reasoning_effort translation
        # produces. Set the adaptive-thinking params directly and
        # skip litellm's translation.
        req.kwargs['thinking'] = {'type': 'adaptive'}
        req.kwargs['output_config'] = {'effort': req.reasoning_effort}
    elif req.thinking_style == "reasoning_effort":
        req.kwargs['reasoning_effort'] = req.reasoning_effort


def _charge_usage(req: _LLMRequest, response_obj: Any) -> None:
    """Add the call's cost to the user's spend, when it can be read."""
    if not (req.user_service and req.user_id):
        return
    try:
        response_cost = _extract_response_cost(response_obj)
    except Exception:
        response_cost = None
    if response_cost is not None:
        req.user_service.add_usage(
            req.user_id, response_cost, basic_info=req.user_basic_info
        )


async def _completion_with_retries(req: _LLMRequest, make_attempt, context: str):
    """Run ``make_attempt()`` under the retry policy and return its result.

    ``make_attempt`` is an async callable that makes one request with
    ``req.kwargs`` (read at call time: an auth re-mint replaces the key).
    Errors that are not retried, or that exhaust their budget, are logged
    under ``context`` and raised, as user-facing ValueErrors for a bad key
    or an overflowing context window.
    """
    kwargs = req.kwargs
    provider = req.provider
    model = req.model
    model_config = req.model_config
    provider_cfg = req.provider_cfg
    model_string = req.model_string
    effective_timeout = req.timeout
    user_id = req.user_id
    api_key = req.api_key
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
            return await make_attempt()
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
                    f"for {model_string} (timeout={effective_timeout}s, "
                    f"connect={min(LLM_CONNECT_TIMEOUT, effective_timeout)}s). "
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
                    "context": context,
                    "model": kwargs.get('model'),
                    "provider": provider,
                    "tools": kwargs.get('tools'),
                    "rate_limit_retries_exhausted": MAX_RATE_LIMIT_RETRIES,
                }
            )
            raise
        except Exception as e:
            # Retryable provider failures -- overload (529) and transient
            # 5xx, including gateway-class 502 -- share one backoff budget
            # and are classified by the module-level predicates above, so
            # a flapping endpoint cannot exceed MAX_OVERLOADED_RETRIES in
            # total however its errors are spelled.
            _err_str = str(e).lower()
            _is_overloaded = _is_overloaded_error(_err_str)
            # Class first, text second: the class is litellm's and stable,
            # the text is the provider's and is not.
            _is_transient_class = _is_transient_exception(e)
            _is_transient_5xx = (
                _is_transient_class or _is_transient_5xx_error(_err_str)
            )
            if (_is_overloaded or _is_transient_5xx) and overloaded_attempts_used < MAX_OVERLOADED_RETRIES:
                last_exception = e
                backoff = min(
                    RATE_LIMIT_BACKOFF_CAP_SECONDS,
                    (2 ** overloaded_attempts_used)
                    + random.uniform(0, overloaded_attempts_used + 1),
                )
                overloaded_attempts_used += 1
                if _is_overloaded:
                    _kind = "overloaded (529)"
                elif _is_gateway_error(_err_str):
                    # litellm raises BadGatewayError only for a literal HTTP
                    # 502, which comes from a proxy between us and the model
                    # timing out or losing its upstream -- not from the model.
                    _kind = "bad gateway (502 from a proxy in front of the model)"
                elif _is_transient_class:
                    # Name the class that opened the budget, so the next
                    # time this fires the log says which exception it was
                    # instead of leaving it to be inferred from the text.
                    _kind = f"transient {type(e).__name__}"
                else:
                    _kind = "server error (5xx)"
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
                    "context": context,
                    "model": kwargs.get('model'),
                    "provider": provider,
                    "tools": kwargs.get('tools'),
                }
            )
            raise
    # Every branch above returns, continues or raises; a budget that ran out
    # on a continue lands here.
    raise last_exception



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
    stream: Optional[bool] = None,
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
        stream: Receive the response from the provider as a stream and
            reassemble it, instead of waiting for it in one piece. The
            return value is the same either way. A long non-streamed call
            sends nothing until it finishes, and proxies and NAT gateways
            drop connections that sit idle, often well inside the call's
            timeout; a streamed one sends data the whole time. None (the
            default) takes the model's "stream" flag in provider config,
            which defaults to False. Not used for the OpenAI 'responses'
            API style.
    """
    req = await _prepare_request(
        provider, model, messages, parameters,
        tools=tools,
        user_service=user_service,
        user_id=user_id,
        user_basic_info=user_basic_info,
        timeout=timeout,
        cache_prefix=cache_prefix,
    )
    # The names the branches below were written against.
    kwargs = req.kwargs
    model_config = req.model_config
    model_string = req.model_string
    reasoning_effort = req.reasoning_effort
    api_key = req.api_key
    user_id = req.user_id
    api_style = model_config.get("api_style")

    thoughts = None
    content = ""

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
        _apply_completion_reasoning(req)

        use_stream = model_config.get("stream", False) if stream is None else bool(stream)

        async def _attempt():
            if use_stream:
                return await _stream_and_rebuild(req)
            return await litellm.acompletion(**req.kwargs)

        response = await _completion_with_retries(
            req, _attempt, context="litellm.acompletion"
        )
        _warn_if_incomplete(response, req)
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

    _charge_usage(req, final_response if use_responses_api else response)

    return content, thoughts


async def stream_llm(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    user_service: Optional[UserService] = None,
    user_id: Optional[str] = None,
    user_basic_info: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: Optional[int] = None,
    cache_prefix: Optional[str] = None,
) -> AsyncGenerator[Any, None]:
    """Stream an LLM response; yields litellm's chunks unchanged.

    The request is built exactly as call_llm builds it: the same thinking
    styles, temperature rules, cache_prefix, tools, credentials and timeout.
    Failures are retried under call_llm's policy until the first chunk
    arrives. After that they are raised, since the caller already holds
    part of the answer and the call cannot be repeated behind its back.
    The cost is charged to the user when the stream completes.

    Models with the OpenAI 'responses' API style stream through plain chat
    completion, as before.
    """
    req = await _prepare_request(
        provider, model, messages, parameters,
        tools=tools,
        user_service=user_service,
        user_id=user_id,
        user_basic_info=user_basic_info,
        timeout=timeout,
        cache_prefix=cache_prefix,
    )
    _apply_completion_reasoning(req)
    req.kwargs["stream"] = True
    if _stream_usage_supported(req):
        # Usage arrives in the last chunk only when asked for; without it the
        # call cannot be charged. Anthropic sends usage regardless and does
        # not take the parameter.
        req.kwargs["stream_options"] = {"include_usage": True}

    async def _open():
        stream = await litellm.acompletion(**req.kwargs)
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = None
        return stream, first

    stream, first = await _completion_with_retries(req, _open, context="stream_llm")
    if first is None:
        return
    chunks = [first]
    try:
        yield first
        async for chunk in stream:
            chunks.append(chunk)
            yield chunk
    except Exception as e:
        get_observability_service().log_exception(
            e,
            user_id=req.user_id,
            metadata={
                "context": "stream_llm",
                "model": req.model_string,
                "provider": req.provider,
                "chunks_received": len(chunks),
            },
        )
        raise
    rebuilt = _rebuild_stream(chunks, req)
    if rebuilt is not None:
        _warn_if_incomplete(rebuilt, req)
    _charge_usage(req, rebuilt)


async def _stream_and_rebuild(req: _LLMRequest) -> Any:
    """One streamed attempt for call_llm, reassembled into a ModelResponse.

    Nothing reaches the caller until the stream is complete, so a failure
    anywhere in it can be retried like any other. The response carries the
    same content, reasoning, tool calls, usage and cost as a non-streamed
    one, so everything after the call is unchanged.

    Timeouts, in two phases:

    - Until the first chunk, only the call's timeout applies. A request
      waiting in a busy model server's queue sends nothing, and that is not
      a failure: retrying it would put it back in the same queue.
    - After the first chunk, a gap longer than LLM_STREAM_IDLE_TIMEOUT
      (capped at the call's timeout) is a stalled stream and raises
      litellm.Timeout, retried under the timeout budget.

    The call's timeout bounds the whole attempt throughout.
    """
    kwargs = dict(req.kwargs)
    kwargs["stream"] = True
    if _stream_usage_supported(req):
        kwargs["stream_options"] = {"include_usage": True}
    # The HTTP read limit is the whole call's; the gap between chunks is
    # enforced below, after the first one.
    kwargs["timeout"] = _http_timeout(req.timeout)
    idle_limit = min(LLM_STREAM_IDLE_TIMEOUT, req.timeout)
    chunks: List[Any] = []

    async def _consume():
        stream = await litellm.acompletion(**kwargs)
        it = stream.__aiter__()
        try:
            chunks.append(await it.__anext__())
        except StopAsyncIteration:
            return
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=idle_limit)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise litellm.Timeout(
                    message=(
                        f"stream stalled: nothing for {idle_limit:.0f}s after "
                        f"{len(chunks)} chunks"
                    ),
                    model=req.model_string,
                    llm_provider=req.provider,
                ) from e
            chunks.append(chunk)

    try:
        await asyncio.wait_for(_consume(), timeout=req.timeout)
    except asyncio.TimeoutError as e:
        raise litellm.Timeout(
            message=(
                f"streamed response not complete within {req.timeout}s "
                f"({len(chunks)} chunks received)"
            ),
            model=req.model_string,
            llm_provider=req.provider,
        ) from e
    response = _rebuild_stream(chunks, req)
    if response is None:
        # An empty or unreadable stream: the connection ended without an
        # answer, which is retried like a dropped connection.
        raise litellm.APIConnectionError(
            message=f"stream ended with no usable response ({len(chunks)} chunks)",
            llm_provider=req.provider,
            model=req.model_string,
        )
    return response


def _warn_if_incomplete(response: Any, req: _LLMRequest) -> None:
    """Say so when an answer is cut off or empty.

    - Cut off (finish_reason "length", some answer): the answer stops
      mid-way. The caller gets half an answer, such as a finding block or
      JSON object with no end, and nothing says it is incomplete.
    - Empty at the limit (finish_reason "length", no answer): a reasoning
      model spent all of max_tokens thinking. llmao's gateway reports this
      as an error for non-streamed calls only.
    - Empty otherwise (finish_reason "stop" or anything else, no answer, no
      tool calls): either the model said nothing, or a reasoning parser
      filed the whole answer as reasoning because it never saw the end of
      thinking. The reasoning length tells the two apart.

    The response is returned unchanged in every case. Agents already handle
    an empty answer (most retry with thinking off), and raising would take
    that away from them.
    """
    try:
        choice = response.choices[0]
        finish = choice.finish_reason
        content = choice.message.content
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
    except Exception:
        return
    if isinstance(content, list):
        content = "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    content = content if isinstance(content, str) else ""
    if finish != "length":
        if content.strip():
            return
        try:
            if response.choices[0].message.tool_calls:
                return   # a tool call is the answer
        except Exception:
            pass
    max_tokens = req.kwargs.get("max_tokens")
    label = f"{req.provider}/{req.model}"
    if finish != "length":
        event_type = "llm_output_empty"
        if reasoning.strip():
            why = ("The answer may have been filed as reasoning: a reasoning "
                   "parser that misses the end of thinking puts everything "
                   "there.")
        else:
            why = "The model returned nothing."
        message = (
            f"{label}: empty answer (finish_reason={finish}, "
            f"max_tokens={max_tokens}, {len(reasoning):,} chars of reasoning). "
            f"{why}"
        )
    elif content.strip():
        event_type = "llm_output_truncated"
        message = (
            f"{label}: answer cut off at the output limit (finish_reason=length, "
            f"max_tokens={max_tokens}, {len(content):,} chars of answer, "
            f"{len(reasoning):,} chars of reasoning). The caller got an "
            f"incomplete answer; raise max_tokens or lower the reasoning effort."
        )
    else:
        event_type = "llm_output_budget_spent_thinking"
        message = (
            f"{label}: empty answer, output limit reached "
            f"(finish_reason=length, max_tokens={max_tokens}, "
            f"{len(reasoning):,} chars of reasoning). The model spent its output "
            f"budget thinking; raise max_tokens or lower the reasoning effort."
        )
    print(f"  WARNING: {message}", flush=True)
    get_observability_service().log(
        level="WARNING",
        event_type=event_type,
        message=message,
        user_id=req.user_id,
        metadata={
            "provider": req.provider,
            "model": req.model,
            "max_tokens": max_tokens,
            "finish_reason": finish,
            "answer_chars": len(content),
            "reasoning_chars": len(reasoning),
        },
    )


def _stream_usage_supported(req: _LLMRequest) -> bool:
    """Whether litellm passes stream_options through for this model."""
    try:
        supported = litellm.get_supported_openai_params(
            model=req.model,
            custom_llm_provider=req.provider_cfg.get(
                "litellm_provider_prefix", req.provider
            ),
        )
    except Exception:
        return False
    return bool(supported) and "stream_options" in supported


def _rebuild_stream(chunks: List[Any], req: _LLMRequest) -> Any:
    """Reassemble streamed chunks into one ModelResponse (content, reasoning,
    tool calls, usage and cost), or None when there is nothing to build."""
    if not chunks:
        return None
    try:
        return litellm.stream_chunk_builder(
            chunks, messages=req.kwargs.get("messages")
        )
    except Exception:
        return None
