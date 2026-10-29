"""Allowlisted GET transport and official polymarket-client response parsers.

The SDK PublicClient does not expose transport injection. A small HTTP boundary
lets every attempt share the limiter, disables proxy discovery, and records raw
public responses for offline replay without accessing SDK private attributes.
"""

import json
import random
import re
import sqlite3
import time
from pathlib import Path
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import quote

import httpx
from polymarket.models import Event as SDKEvent
from polymarket.models import Market as SDKMarket
from polymarket.models.data import Resolution as SDKResolution

from ..compliance import GEOBLOCK_URL, PUBLIC_API_BUDGET, ComplianceGuard, GeoblockClient
from .models import Event, Market, PricePage, PricePoint, Resolution, Tag, Token


class UpstreamError(RuntimeError):
    pass


class SharedLimiter:
    """Token buckets shared by all arb sync processes for this OS user.

    A global 9000/10s bucket plus a conservative burst-one 19/s bucket keeps
    this layer below the stricter price-history allowance as well.
    """

    def __init__(self, path: Path | None = None, *, clock=time.time, sleep=time.sleep):
        self.path = path or Path.home() / ".cache/arb-research/polymarket-limits.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.sleep = clock, sleep
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS buckets (name TEXT PRIMARY KEY, tokens REAL, at REAL)")

    def acquire(self):
        while True:
            wait = 0.0
            with sqlite3.connect(self.path, timeout=30) as db:
                db.execute("BEGIN IMMEDIATE")
                now = self.clock()
                values = []
                for name, capacity, rate in [("global", 9000, 900), ("public", 1, 19)]:
                    row = db.execute("SELECT tokens, at FROM buckets WHERE name=?", (name,)).fetchone()
                    tokens = capacity if row is None else min(capacity, row[0] + max(0, now-row[1])*rate)
                    wait = max(wait, (1-tokens)/rate)
                    values.append((name, tokens, now))
                for name, tokens, at in values:
                    db.execute("INSERT OR REPLACE INTO buckets VALUES (?, ?, ?)",
                               (name, tokens-(1 if wait <= 0 else 0), at))
            if wait <= 0:
                return
            self.sleep(wait + 0.001)


def _tags(items) -> list[Tag]:
    return [Tag(id=str(t.id), label=t.label, slug=t.slug) for t in items]


class PublicDataClient:
    def __init__(self, *, limiter: SharedLimiter | None = None,
                 transport: httpx.BaseTransport | None = None,
                 record_dir: Path | None = None, sleep=time.sleep,
                 proxy: str | None = None):
        try:
            self.http = httpx.Client(transport=transport, proxy=proxy, trust_env=False,
                                     follow_redirects=False, timeout=httpx.Timeout(20, connect=5))
        except (ValueError, ImportError, httpx.InvalidURL):
            # Configuration errors may include a credential-bearing proxy URL.
            raise UpstreamError("Invalid public-data proxy configuration or missing SOCKS support") from None
        self.limiter = limiter or SharedLimiter()
        self.record_dir, self.sleep = record_dir, sleep
        self.guard = ComplianceGuard(geoblock=GeoblockClient(
            fetch=self._fetch_geoblock,
            budget=PUBLIC_API_BUDGET, sleep=sleep,
        ))

    def _fetch_geoblock(self, timeout: float) -> dict:
        # GeoblockClient owns its shared budget, bounded retries and cache.
        # Use the same explicit proxy, while preserving its timeout/size bounds.
        self.limiter.acquire()
        with self.http.stream("GET", GEOBLOCK_URL, timeout=timeout) as response:
            if response.status_code != 200:
                raise HTTPError(GEOBLOCK_URL, response.status_code,
                                "geoblock check failed", response.headers, None)
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 16384:
                    raise ValueError("oversized geoblock response")
        # Only the guard's normalized result is exposed; no IP/raw response is recorded.
        return json.loads(body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.http.close()

    def get(self, host: str, path: str, params: dict | None = None):
        if not ((host == "gamma-api.polymarket.com" and
                 re.fullmatch(r"/(events|markets)(?:/(?:slug/)?[\w%.-]+(?:/tags)?)?", path)) or
                (host == "data-api.polymarket.com" and path in
                 {"/v2/prices-history", "/v2/resolutions", "/v2/trades", "/v2/holders"})):
            raise ValueError("Endpoint is outside the public data allowlist")
        url = f"https://{host}{path}"
        for attempt in range(4):
            PUBLIC_API_BUDGET.acquire()
            self.limiter.acquire()
            try:
                response = self.http.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise UpstreamError(f"GET {url}: {type(exc).__name__}") from None
                self.sleep(2**attempt + random.random())
                continue
            if response.is_success:
                body = response.json()
                if self.record_dir is not None and host != "polymarket.com":
                    import uuid
                    self.record_dir.mkdir(parents=True, exist_ok=True)
                    (self.record_dir / f"{uuid.uuid4().hex}.json").write_text(json.dumps({
                        "url": str(response.url), "status": response.status_code,
                        "body": body, "recorded_at": int(time.time()),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                return body
            error = f"GET {url}: HTTP {response.status_code}, trace={response.headers.get('x-trace-id', 'unavailable')}"
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise UpstreamError(error)
            try:
                delay = max(float(response.headers.get("Retry-After", 0)), 2**attempt + random.random())
            except ValueError:
                delay = 2**attempt + random.random()
            if delay > 60:
                raise UpstreamError(f"{error}; retry after {delay}s")
            self.sleep(delay)
        raise AssertionError("Unreachable")

    def event(self, selector: str) -> Event:
        path = selector if selector.isdecimal() else "slug/" + quote(selector, safe="")
        raw = self.get("gamma-api.polymarket.com", f"/events/{path}")
        event = SDKEvent.parse_response(raw)
        markets = []
        for summary in event.markets:
            raw_market = self.get("gamma-api.polymarket.com", f"/markets/{summary.id}")
            market = SDKMarket.parse_response(raw_market)
            tags = self.get("gamma-api.polymarket.com", f"/markets/{summary.id}/tags")
            protocol = market.version.value if market.version else "unknown"
            # Gamma positionIds are the V2 token keys. Keep any historical CTF
            # pair too, explicitly tagged, rather than guessing from a date.
            tokens = []
            for i, outcome in enumerate([market.outcomes.yes, market.outcomes.no]):
                if outcome.token_id is not None:
                    tokens.append(Token(id=str(outcome.token_id), outcome=outcome.label,
                                        outcome_index=i, protocol="v1"))
                if outcome.position_id is not None:
                    tokens.append(Token(id=str(outcome.position_id), outcome=outcome.label,
                                        outcome_index=i, protocol="v2"))
            if not market.condition_id or not tokens:
                raise UpstreamError(f"Market {market.id} lacks condition or outcome token IDs")
            markets.append(Market(
                id=str(market.id), event_id=str(event.id), condition_id=str(market.condition_id),
                slug=market.slug, question=market.question, rules=market.description,
                resolution_source=raw_market.get("resolutionSource"), closed=market.state.closed,
                protocol=protocol, neg_risk=market.state.neg_risk,
                neg_risk_group=raw_market.get("negRiskMarketID") or event.trading.neg_risk_market_id,
                tags=[Tag(id=str(t["id"]), label=t.get("label"), slug=t.get("slug")) for t in tags],
                tokens=tokens,
            ))
        if not markets:
            raise UpstreamError(f"Event {event.id} has no markets")
        return Event(id=str(event.id), slug=event.slug, title=event.title,
                     description=event.description, closed=event.state.closed,
                     tags=_tags(event.tags), markets=markets)

    def prices(self, token_id: str, params: dict, cursor: str | None) -> PricePage:
        query = {"token_id": token_id, **params}
        if cursor:
            query["cursor"] = cursor
        body = self.get("data-api.polymarket.com", "/v2/prices-history", query)
        pagination = body["pagination"]
        next_cursor = pagination["next_cursor"]
        if bool(next_cursor) != pagination["has_more"]:
            raise UpstreamError("Inconsistent price-history pagination")
        return PricePage(data=[PricePoint.model_validate(p) for p in body["data"]], next_cursor=next_cursor)

    def resolutions(self, event: Event) -> list[Resolution]:
        rows = self.get("data-api.polymarket.com", "/v2/resolutions", {"event_id": event.id})["data"]
        markets = {m.condition_id: m.id for m in event.markets}
        result = []
        for raw in rows:
            r = SDKResolution.parse_response(raw)
            if str(r.condition_id) not in markets:
                raise UpstreamError("Unexpected resolution condition in event response")
            payouts = None if r.payouts is None else [float(p) for p in r.payouts]
            result.append(Resolution(
                market_id=markets[str(r.condition_id)], condition_id=str(r.condition_id),
                status=r.status.value, payouts=payouts,
                split_50_50=r.status.value == "resolved" and (
                    payouts == [0.5, 0.5] or r.price == Decimal("500000000000000000")),
                was_disputed=r.was_disputed, extended_review=r.extended_review,
                proposed_price=None if r.proposed_price is None else str(r.proposed_price),
                reproposed_price=None if r.reproposed_price is None else str(r.reproposed_price),
                price=None if r.price is None else str(r.price),
                resolved_at=None if r.resolved_at is None else r.resolved_at.isoformat(),
                last_updated_at=r.last_updated_at.isoformat(), raw=raw,
            ))
        return result
