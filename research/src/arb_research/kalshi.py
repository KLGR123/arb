"""Unauthenticated Kalshi public GETs only; no account or execution API."""

from datetime import datetime, timezone
import os
import re
import time

import httpx

from .compliance import PUBLIC_API_BUDGET
from .negrisk_cli import GLOBAL_BUCKET


def identifier(value):
    # Real threshold tickers include decimals, e.g. KXFED-26DEC-T3.75.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value) or ".." in value:
        raise ValueError("invalid public market identifier")
    return value


class KalshiPublicAPI:
    HOST = "external-api.kalshi.com"

    def __init__(self, client=None, bucket=GLOBAL_BUCKET, sleep=time.sleep, attempts=3):
        self.bucket, self.sleep, self.attempts = bucket, sleep, attempts
        try:
            self.client = client or httpx.Client(
                proxy=os.environ.get("ARB_POLYMARKET_PROXY") or None,
                trust_env=False, follow_redirects=False, timeout=20,
                headers={"Accept": "application/json", "User-Agent": "arb-kalshi-readonly/1"})
        except (ValueError, ImportError, httpx.HTTPError):
            raise ValueError("invalid_public_read_proxy_configuration") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def get(self, path, params=None):
        segment = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"
        if ".." in path or not re.fullmatch(rf"/(?:events|markets)(?:/{segment}(?:/orderbook)?)?|/series/{segment}", path):
            raise ValueError("endpoint outside public read allowlist")
        for attempt in range(self.attempts):
            self.bucket.acquire()
            PUBLIC_API_BUDGET.acquire()
            try:
                response = self.client.get(f"https://{self.HOST}/trade-api/v2{path}", params=params)
                if response.url.host != self.HOST or 300 <= response.status_code < 400:
                    raise ValueError("public read redirect rejected")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code not in (429, 500, 502, 503, 504) or attempt + 1 == self.attempts:
                    raise OSError(f"kalshi_public_http_{code}") from None
                try:
                    delay = min(30, max(0, float(exc.response.headers.get("Retry-After", 0))))
                except ValueError:
                    delay = 0
                self.sleep(max(delay, 2 ** attempt))
            except httpx.TransportError:
                if attempt + 1 == self.attempts:
                    mode = "proxy" if os.environ.get("ARB_POLYMARKET_PROXY") else "direct_no_proxy_configured"
                    raise OSError(f"kalshi_public_transport_failed_{mode}") from None
                self.sleep(2 ** attempt)


def discover(api, series=None, pages=2, event_tickers=()):
    """Bounded cursor pagination; raw events and markets retained for replay."""
    events, markets, seen, cursor = [], [], set(), None
    if event_tickers:
        for ticker in event_tickers:
            data = api.get("/events/" + identifier(ticker), {"with_nested_markets": "true"})
            events.append(data["event"])
            # with_nested_markets=true currently leaves the top-level array empty.
            markets.extend(data.get("markets") or data["event"].get("markets", []))
        return events, markets
    for _ in range(pages):
        params = {"status": "open", "limit": 100}
        if series:
            params["series_ticker"] = identifier(series)
        if cursor:
            params["cursor"] = cursor
        data = api.get("/events", {**params, "with_nested_markets": "true"})
        for event in data["events"]:
            events.append(event)
            markets.extend(event.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("Kalshi repeated pagination cursor")
        seen.add(cursor)
    return events, markets


def book(api, ticker):
    raw = api.get("/markets/" + identifier(ticker) + "/orderbook")
    return {"raw": raw, "received_at": datetime.now(timezone.utc).isoformat()}
