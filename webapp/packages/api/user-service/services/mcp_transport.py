"""A bearer token goes to an MCP server only over TLS, or to this host."""

from typing import Optional
from urllib.parse import urlsplit

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def token_over_plain_http(url: str, token: Optional[str]) -> bool:
    """True when ``url`` would carry ``token`` in the clear across a network."""
    if not token:
        return False
    parts = urlsplit(url)
    return parts.scheme.lower() == "http" and (parts.hostname or "") not in _LOOPBACK


REFUSAL = "An MCP server that takes a token must use https:// (or be on localhost)"
