from .bedrock import models as bedrock_models
from .anthropic_1p import models as anthropic_1p_models
from .llmao import models as llmao_models
from .gemini import models as gemini_models
from .openai import models as openai_models
from .openrouter import models as openrouter_models
from .perplexity import models as perplexity_models
PROVIDER_CONFIG = {
    "openai": {
        "api_key_env_var": "OPENAI_API_KEY",
        "models": openai_models
    },
    "gemini": {
        "api_key_env_var": "GEMINI_API_KEY",
        "models":  gemini_models,
    },
    "perplexity": {
        "api_key_env_var": "PERPLEXITYAI_API_KEY",
        "models": perplexity_models,
    },
    "bedrock": {
        "api_key_env_var": "AWS_BEARER_TOKEN_BEDROCK",
        "models": bedrock_models
    },
    # Anthropic first-party API via Workload Identity Federation.
    #
    # Replaces the former "bedrock-mythos" provider, which reached Mythos
    # through Bedrock Mantle in Anthropic's preview AWS account via
    # cross-account STS AssumeRole. That path is being decommissioned.
    #
    # No static key: an AWS STS web identity JWT is exchanged with
    # Anthropic's /v1/oauth/token endpoint for a short-lived bearer token,
    # minted per request in llm_service.
    #
    # ANTHROPIC_API_KEY MUST NOT be set in this container. litellm prefers a
    # static key over an injected one, so its presence silently bypasses the
    # WIF path entirely -- you would get working calls on the wrong auth and
    # no signal. services/anthropic_wif.py raises if it finds one. This is
    # also why the static "anthropic" provider was removed rather than kept
    # alongside: the two cannot coexist in one container.
    #
    # auth_mechanism is the dispatch key. llm_service branches on config
    # keys rather than provider names -- see the assume_role_arn pattern it
    # replaced -- so this follows the same shape.
    "anthropic-1p": {
        "auth_mechanism": "wif",
        "litellm_provider_prefix": "anthropic",
        "models": anthropic_1p_models,
    },
    # Self-hosted models behind llmao's gateway at llm.apache.org (vLLM per
    # model, three today).
    #
    # Routed as litellm's "openai" provider because the gateway is
    # OpenAI-compatible; api_base points at it and it routes by model_name to
    # whichever vLLM is serving that model. Which GPU box that is changes
    # without notice -- boxes are rented and get rebuilt -- and none of it is
    # visible from here.
    #
    # "openai" rather than "hosted_vllm" deliberately, and tested: both give
    # identical results on all three models, with and without extra_body. The
    # gateway IS an OpenAI-compatible proxy, so "openai" describes it
    # accurately; "hosted_vllm" would describe one hop further down. (llmao
    # routes internally with a hosted_vllm/ prefix, because litellm's openai
    # provider translates /v1/messages to vLLM's /v1/responses and breaks
    # Anthropic-shaped tool calls. That is a different hop and does not apply
    # here.)
    #
    # LLMAO_API_BASE is https://llm.apache.org/v1, set in p6 hiera. This was
    # previously a Tailscale address reaching the litellm port directly, from
    # before the gateway existed; that host is gone and the port is now
    # loopback-only, so the direct path is not available even in principle.
    #
    # LLMAO_API_KEY is a PAT minted under the gofannon project (Other Keys in
    # the portal, so created_by is recorded), held in p6 eyaml. It
    # authenticates at the gateway and the traffic is attributable and
    # revocable in litellm's spend logs -- the identity concern in the
    # previous version of this comment no longer applies.
    #
    # NOTE: minted keys currently carry no rpm/tpm/max_parallel_requests, and
    # team budgets do not bind because self-hosted spend records as 0.00. A
    # retry loop here has nothing stopping it from saturating the fleet.
    "llmao": {
        "litellm_provider_prefix": "openai",
        "api_base_env_var": "LLMAO_API_BASE",
        "api_key_env_var": "LLMAO_API_KEY",
        "models": llmao_models,
    },
    "openrouter": {
        "api_key_env_var": "OPENROUTER_API_KEY",
        "models": openrouter_models,
    },
    "ollama": {
        "models": {
            "llama2": {
                "parameters": {
                    "temperature": {
                        "type": "float",
                        "default": 0.7,
                        "min": 0.0,
                        "max": 1.0,
                        "description": "Controls randomness"
                    },
                    "num_predict": {
                        "type": "integer",
                        "default": 512,
                        "min": 1,
                        "max": 2048,
                        "description": "Maximum tokens to generate"
                    },
                    "top_p": {
                        "type": "float",
                        "default": 0.9,
                        "min": 0.0,
                        "max": 1.0,
                        "description": "Nucleus sampling"
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 40,
                        "min": 1,
                        "max": 100,
                        "description": "Top-k sampling"
                    },
                    "repeat_penalty": {
                        "type": "float",
                        "default": 1.1,
                        "min": 0.0,
                        "max": 2.0,
                        "description": "Penalty for repetition"
                    }
                }
            },
            "mistral": {
                "parameters": {
                    "temperature": {
                        "type": "float",
                        "default": 0.7,
                        "min": 0.0,
                        "max": 1.0,
                        "description": "Controls randomness"
                    },
                    "num_predict": {
                        "type": "integer",
                        "default": 512,
                        "min": 1,
                        "max": 2048,
                        "description": "Maximum tokens to generate"
                    }
                }
            }
        }
    }
}