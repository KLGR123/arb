"""Explicit transport configuration for public market data only."""

import os
from urllib.parse import urlsplit


def public_proxy():
    """Read the task's private setting without exposing it in validation errors."""
    value = os.environ.get("ARB_POLYMARKET_PROXY")
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "socks5h" or not parsed.hostname or not parsed.port
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValueError("invalid ARB_POLYMARKET_PROXY configuration") from None
    return value


def fetch_public_geoblock(timeout):
    """Inject into main's GeoblockClient; never alter the trading guard singleton."""
    import httpx
    import json

    with httpx.Client(proxy=public_proxy(), trust_env=False, timeout=timeout,
                      follow_redirects=False) as client:
        with client.stream("GET", "https://polymarket.com/api/geoblock") as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 16384:
                    raise ValueError("oversized geoblock response")
    return json.loads(body)
