"""
Anthropic 1P Workload Identity Federation helper.

Exchanges an AWS STS web identity JWT for a short-lived Anthropic
OAuth access token via the RFC 7523 jwt-bearer grant. Result is cached
per federation-rule until near expiry, then re-minted.

Auth chain in production:
  EC2 instance role -> sts:GetWebIdentityToken -> POST /v1/oauth/token -> sk-ant-oat01-...
  (IMDS)              (regional STS endpoint)     (Anthropic)             (access token)

The AWS side requires:
  - Outbound web identity federation enabled on the AWS account (one-time)
  - sts:GetWebIdentityToken action allowed on the instance role
  - boto3 client pinned to a specific region (GetWebIdentityToken is
    only on regional STS endpoints)

The Anthropic side requires:
  - Federation issuer registered pointing at the AWS account's STS issuer
  - Service account created
  - Federation rule matching the instance role's ARN as subject_prefix
    and https://api.anthropic.com as audience

Configuration comes from env vars set by puppet:
  ANTHROPIC_FEDERATION_RULE_ID
  ANTHROPIC_ORGANIZATION_ID
  ANTHROPIC_SERVICE_ACCOUNT_ID
  ANTHROPIC_WORKSPACE_ID   (optional; required only when the rule
                            covers multiple workspaces)

CRITICAL: ANTHROPIC_API_KEY must NOT be set in the container's env.
The anthropic SDK silently prefers static keys over federation, which
would make us think we've moved off static credentials when we haven't.
The puppet manifest explicitly omits it and this module raises if it
detects it at runtime.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import boto3
import requests

logger = logging.getLogger(__name__)


# Anthropic OAuth token exchange endpoint
_ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"

# JWT audience for the STS-issued token
_STS_AUDIENCE = "https://api.anthropic.com"

# Refresh margin: re-mint the access token this many seconds before
# it expires so we never send a request with a stale token
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Fallback used only before the first successful exchange tells us the real
# figure. Anthropic documents access tokens as short-lived (~10 min).
_ASSUMED_TOKEN_LIFETIME_SECONDS = 600

# STS region for GetWebIdentityToken. GetWebIdentityToken is only on
# regional endpoints; global STS does not offer it.
_STS_REGION = os.environ.get("ANTHROPIC_1P_STS_REGION", "us-east-1")

# Duration of the STS-issued JWT itself. Shorter than the access
# token, so we can re-mint the JWT each time we exchange without
# worrying about STS-token reuse policy.
_STS_TOKEN_DURATION_SECONDS = 900

# Cache: federation_rule_id -> {"token": str, "expires_at": float}
# Keyed by rule id because in principle we could mint for different
# rules within the same process (e.g., different workspaces).
_ANTHROPIC_1P_TOKEN_CACHE: dict = {}


class AnthropicWifConfigError(RuntimeError):
    """Raised when WIF configuration is missing or invalid."""


class AnthropicWifExchangeError(RuntimeError):
    """Raised when the /v1/oauth/token exchange fails."""


def _assert_no_static_api_key() -> None:
    """
    Refuse to run if ANTHROPIC_API_KEY is set. Static key silently
    overrides federation in the anthropic SDK. Failing loud here is
    better than silently degrading to static-key auth.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise AnthropicWifConfigError(
            "ANTHROPIC_API_KEY is set in the environment but this build "
            "is configured for Workload Identity Federation. Refusing to "
            "run to prevent silent fallback to static-key auth. Remove "
            "ANTHROPIC_API_KEY from the container's environment."
        )


def _load_wif_config() -> dict:
    """Read the federation identifiers from env. Raise on any missing."""
    required = (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise AnthropicWifConfigError(
            f"Anthropic WIF config missing env vars: {', '.join(missing)}. "
            "These should be set by the puppet-managed docker-run script."
        )
    return {
        "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
        "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
        "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
        # workspace_id is optional; required only when the federation
        # rule covers multiple workspaces
        "workspace_id": os.environ.get("ANTHROPIC_WORKSPACE_ID"),
    }


def _mint_sts_web_identity_token() -> str:
    """
    Call AWS STS GetWebIdentityToken with the Anthropic audience.
    The resulting JWT asserts the caller's IAM identity and is signed
    by AWS. Anthropic verifies it against the federation issuer we
    registered for this AWS account.
    """
    # Pin the STS client to a region. GetWebIdentityToken is not
    # available on the global STS endpoint.
    sts = boto3.client("sts", region_name=_STS_REGION)
    resp = sts.get_web_identity_token(
        Audience=[_STS_AUDIENCE],
        SigningAlgorithm="RS256",
        DurationSeconds=_STS_TOKEN_DURATION_SECONDS,
    )
    return resp["WebIdentityToken"]


def _exchange_jwt_for_access_token(jwt: str, cfg: dict) -> dict:
    """
    POST the JWT to Anthropic's OAuth endpoint. Returns the parsed
    JSON response including access_token and expires_in.
    """
    body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
        "federation_rule_id": cfg["federation_rule_id"],
        "organization_id": cfg["organization_id"],
        "service_account_id": cfg["service_account_id"],
    }
    if cfg["workspace_id"]:
        body["workspace_id"] = cfg["workspace_id"]

    resp = requests.post(
        _ANTHROPIC_TOKEN_URL,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )

    if resp.status_code != 200:
        # Log the error body for diagnosis. Common failures include
        # invalid_grant (rule mismatch, wrong audience, wrong ARN)
        # and invalid_client (rule/org/service_account mismatch).
        try:
            err_body = resp.json()
        except ValueError:
            err_body = {"raw": resp.text[:500]}
        logger.error(
            "Anthropic WIF token exchange failed: status=%d body=%s",
            resp.status_code, err_body,
        )
        raise AnthropicWifExchangeError(
            f"Anthropic WIF exchange returned {resp.status_code}: {err_body}"
        )

    return resp.json()


def invalidate_anthropic_1p_token() -> None:
    """Drop the cached token so the next call mints a fresh one.

    Needed on the retry path. A 401/403 mid-run means the cached token is
    dead; without this the retry replays it and fails identically, turning
    an auth problem into what looks like a timeout cascade. Mirrors
    _invalidate_bedrock_mythos_token in llm_service.
    """
    _ANTHROPIC_1P_TOKEN_CACHE.clear()
    logger.info("Invalidated cached Anthropic 1P token; next call will re-mint")


def get_anthropic_1p_bearer_token(min_validity_seconds: int = 0) -> str:
    """
    Return a valid Anthropic access token, refreshing via the WIF
    exchange if the cached one is near expiry.

    Callers should pass this token to litellm as `api_key` for anthropic
    provider calls, or use it as a Bearer header on direct HTTP calls.

    min_validity_seconds: require the token to remain valid for at least
    this long, not merely to be valid at dispatch. Anthropic access tokens
    last ~600s; ASVS HEAVY-tier calls run with a 1800s timeout and single
    Opus calls have been measured at 3+ minutes. A token that is valid when
    the request goes out can therefore expire mid-flight, and the retry
    then replays the SAME dead token and fails identically -- which from
    the outside looks like a timeout cascade, not an auth failure. Pass the
    call's own timeout here so the token is guaranteed to outlive it.

    This mirrors _get_bedrock_mythos_api_key's parameter of the same name,
    which exists because that exact cascade was observed in production.
    """
    _assert_no_static_api_key()

    cfg = _load_wif_config()
    rule_id = cfg["federation_rule_id"]

    # Cache lookup. The token must outlive both the refresh margin AND the
    # caller's stated requirement.
    # Cap the requirement at the refresh margin plus whatever a fresh token
    # could actually provide. Without this cap, a caller asking for 1800s
    # against a ~600s token makes the cache check ALWAYS fail, so every single
    # call performs a full WIF exchange -- the cache stops working entirely
    # while appearing to be present.
    cached = _ANTHROPIC_1P_TOKEN_CACHE.get(rule_id)
    if cached:
        lifetime = cached.get("lifetime", _ASSUMED_TOKEN_LIFETIME_SECONDS)
        # The cap is (lifetime - 2*margin) so that a token which JUST cleared
        # the requirement still has margin left; capping at lifetime-margin
        # makes `required` equal the lifetime and the check can never pass.
        usable = max(0, lifetime - 2 * _TOKEN_REFRESH_MARGIN_SECONDS)
        required = _TOKEN_REFRESH_MARGIN_SECONDS + min(
            max(0, min_validity_seconds), usable
        )
        if time.time() < cached["expires_at"] - required:
            return cached["token"]

    # Refresh: mint a new STS JWT and exchange it
    logger.info("Minting fresh Anthropic 1P access token via WIF exchange")
    jwt = _mint_sts_web_identity_token()
    payload = _exchange_jwt_for_access_token(jwt, cfg)

    access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 600)
    _ANTHROPIC_1P_TOKEN_CACHE[rule_id] = {
        "token": access_token,
        "expires_at": time.time() + expires_in,
        # Kept so the cache check knows what a fresh token can offer.
        "lifetime": expires_in,
    }
    # A freshly minted token may still be shorter-lived than the caller asked
    # for -- Anthropic caps these around 600s while our heaviest tier allows
    # 1800s. Whether that actually matters is UNCONFIRMED: it depends on
    # whether the API validates the token once at request acceptance or
    # continuously for the life of the request.
    #
    # The Bedrock Mantle path did NOT survive mid-flight expiry, which is why
    # min_validity_seconds exists at all -- but that was SigV4 presigned-request
    # auth, a different mechanism, so it may not generalise to a bearer token.
    #
    # Warn rather than raise. Blocking here would fail every heavy-tier call
    # on a constraint that probably does not apply; if it does apply, this log
    # line is what points at the cause when a long call dies as a "timeout".
    # Question is with Anthropic; drop this block once answered either way.
    if min_validity_seconds and expires_in < min_validity_seconds:
        logger.warning(
            "Anthropic 1P token lives %ds but caller requested %ds. If the API "
            "validates continuously rather than at acceptance, a call longer "
            "than %ds may die mid-flight and surface as a client timeout "
            "rather than an auth error.",
            expires_in, min_validity_seconds, expires_in,
        )

    logger.info(
        "Anthropic 1P token cached, valid for %d seconds", expires_in,
    )
    return access_token


def clear_token_cache() -> None:
    """Testing hook: clear the process-local token cache."""
    _ANTHROPIC_1P_TOKEN_CACHE.clear()