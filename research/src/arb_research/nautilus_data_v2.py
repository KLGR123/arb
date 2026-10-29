"""Public Data API v2 -> Nautilus core types. No execution adapter imports.

The HTTP boundary is intentionally small: the unified SDK's public client does
not expose an httpx transport, so we use its response models behind an allowlist.
Price history comes from ARB-7 Parquet/DuckDB. JSON snapshots export portable
fixtures and cache supplementary public metadata/trade responses, not a sync DB.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from polymarket.errors import UnexpectedResponseError
from polymarket.models import Event as SDKEvent, Market as SDKMarket
from polymarket.models.data import PriceHistoryPoint, Trade

from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide, AssetClass, CurrencyType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Price, Quantity

from .polymarket.client import PublicDataClient, UpstreamError


class DataError(RuntimeError):
    """Safe public error; never contains transport configuration or credentials."""


def parse(model, raw):
    try:
        return model.parse_response(raw)
    except UnexpectedResponseError:
        raise DataError(f"Invalid {model.__name__} response fields") from None


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


class ResponseCache:
    """Atomic request-keyed snapshots; offline reads fail on a cache miss."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, service, path, params):
        key = json.dumps([service, path, params or {}], sort_keys=True)
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def read(self, service, path, params):
        return json.loads(self.path(service, path, params).read_text(encoding="utf-8"))["body"]

    def write(self, service, path, params, body):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(service, path, params)
        # Unique temporary file permits independent collectors to share a cache.
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, encoding="utf-8", delete=False) as f:
            f.write(dump({"service": service, "path": path, "params": params or {},
                          "recorded_at": int(time.time()), "body": body}))
            temp = f.name
        os.replace(temp, target)


class DataV2Client(PublicDataClient):
    """ARB-7 transport/limiter/guard with an optional offline response cache."""

    HOSTS = {"gamma": "gamma-api.polymarket.com", "data": "data-api.polymarket.com"}

    def __init__(self, *, cache=None, offline=False, refresh=False, transport=None,
                 sleep=time.sleep, limiter=None):
        if offline and refresh:
            raise DataError("offline and refresh are mutually exclusive")
        self.cache, self.offline, self.refresh = cache, offline, refresh
        try:
            super().__init__(transport=transport, sleep=sleep, limiter=limiter,
                             proxy=None if transport is not None or offline else os.environ.get("ARB_POLYMARKET_PROXY") or None)
        except UpstreamError:
            raise DataError("Invalid public-data proxy configuration or missing SOCKS support") from None

    def get(self, service, path, params=None):
        # Parent event/prices methods use host names; new methods use aliases.
        host = self.HOSTS.get(service, service)
        if host not in self.HOSTS.values():
            raise ValueError("Endpoint outside public read-only allowlist")
        service = next(key for key, value in self.HOSTS.items() if value == host)
        # Validate before cache reads too, so offline mode cannot widen authority.
        if not ((service == "gamma" and re.fullmatch(r"/(events|markets)(?:/(?:slug/)?[\w%.-]+(?:/tags)?)?", path))
                or (service == "data" and path in {"/v2/trades", "/v2/prices-history", "/v2/resolutions"})):
            raise ValueError("Endpoint outside public read-only allowlist")
        if self.cache is not None and not self.refresh:
            try:
                return self.cache.read(service, path, params)
            except FileNotFoundError:
                pass
        if self.offline:
            raise DataError("Offline response cache miss")
        try:
            body = super().get(host, path, params)
        except UpstreamError:
            raise DataError("Public data read failed; check HTTP endpoint, connectivity and ARB_POLYMARKET_PROXY configuration") from None
        if self.cache is not None:
            self.cache.write(service, path, params, body)
        return body

    def pages(self, path, params, *, max_pages=100):
        """Fail rather than silently return a truncated history."""
        rows, cursors = [], set()
        query = dict(params)
        for _ in range(max_pages):
            body = self.get("data", path, query)
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise DataError("Invalid v2 response envelope")
            paging = body.get("pagination", {})
            cursor = paging.get("next_cursor")
            if type(paging.get("has_more")) is not bool or bool(cursor) != paging["has_more"]:
                raise DataError("Invalid v2 pagination")
            rows.extend(body["data"])
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or cursor in cursors:
                raise DataError("Repeated or invalid pagination cursor")
            cursors.add(cursor)
            query = {**params, "cursor": cursor}
        raise DataError("History page limit reached; no complete result returned")

    def event_markets(self, selector, *, market_id=None):
        key = selector if str(selector).isdecimal() else "slug/" + quote(str(selector), safe="")
        raw_event = self.get("gamma", f"/events/{key}")
        event = parse(SDKEvent, raw_event)
        summaries = [m for m in event.markets if market_id is None or str(m.id) == str(market_id)]
        if not summaries:
            raise DataError("Event has no matching markets")
        return raw_event, [self.get("gamma", f"/markets/{m.id}") for m in summaries]

    def collect(self, event, *, store, market_id=None, max_pages=100):
        raw_event, markets = self.event_markets(event, market_id=market_id)
        items = []
        for market in markets:
            condition = market["conditionId"]
            instruments = instruments_from_market(market, event_id=str(raw_event["id"]))
            trades = self.pages("/v2/trades", {"condition": condition, "taker_only": "true", "limit": 1000},
                                max_pages=max_pages)
            prices, resolutions = stored_prices(store, str(raw_event["id"]), market, instruments)
            if not isinstance(resolutions, list) or any(r["condition_id"] != condition for r in resolutions):
                raise DataError("Unexpected resolution response")
            items.append({"market": market, "trades": trades, "prices": prices, "resolutions": resolutions})
        return {"schema_version": 1, "source": "recorded_public_api", "recorded_at": int(time.time()),
                "event": raw_event, "markets": items,
                "coverage": {"trades": "market feed, fixed three-year server window, taker-only, min size 0.01",
                             "prices": "ARB-7 Parquet/DuckDB complete cached series; actual width retained"}}


def stored_prices(store, event_id, market, instruments):
    """Read ARB-7 public contracts; never write fragments/checkpoints ourselves."""
    # A history walk uses several SQLite/DuckDB queries. Hold the syncer's
    # public lock so a refresh cannot reorder rows between offset pages.
    with store.writer():
        return _stored_prices_locked(store, event_id, market, instruments)


def _stored_prices_locked(store, event_id, market, instruments):
    try:
        event = store.event(event_id)
    except KeyError:
        raise DataError("Event absent from ARB-7 store; run polymarket-data sync first") from None
    cached = next((m for m in event.markets if m.id == str(market["id"])), None)
    if cached is None or cached.condition_id != market["conditionId"]:
        raise DataError("ARB-7 cached market/condition mismatch; sync metadata first")
    prices = {}
    for instrument in instruments:
        token = str(instrument.raw_symbol)
        if token not in {t.id for t in cached.tokens}:
            raise DataError("Token absent from ARB-7 cached metadata")
        checkpoint = store.checkpoint(token)
        if not checkpoint["through"] or checkpoint["pending"]:
            raise DataError("Incomplete ARB-7 price checkpoint; finish polymarket-data sync first")
        offset, rows = 0, []
        while True:
            page = store.history(event, market_id=cached.id, token_id=token, offset=offset, limit=1000)
            rows.extend({"timestamp": p.timestamp, "price": p.price,
                         "resolution_seconds": p.resolution_seconds} for p in page.data)
            if page.next_offset is None:
                break
            if page.next_offset <= offset:
                raise DataError("Nonadvancing ARB-7 history pagination")
            offset = page.next_offset
        prices[token] = rows
    resolutions = [r.raw for r in store.resolutions(event, cached.id).data]
    resolution_state(market, resolutions)
    return prices, resolutions


def resolution_state(market, rows):
    condition = market["conditionId"]
    if any(r.get("condition_id") != condition for r in rows):
        raise DataError("Resolution condition mismatch")
    settled = any(r.get("status") == "resolved" for r in rows)
    gamma_resolved = any(str(market.get(key, "")).lower() == "resolved"
                         for key in ("umaResolutionStatus", "resolutionStatus"))
    if (market.get("closed") is True and not rows) or (gamma_resolved and not settled):
        raise DataError("Missing or stale resolution for closed/resolved market; refresh ARB-7 sync")
    return settled


def decimal(value, *, low=Decimal(0), high=None):
    try:
        result = Decimal(str(value))
    except Exception:
        raise DataError("Invalid numeric field") from None
    if not result.is_finite() or result < low or (high is not None and result > high):
        raise DataError("Numeric field outside supported bounds")
    return result


def ns(value):
    """Epoch seconds -> ns without floating point timestamp conversion."""
    if isinstance(value, datetime):
        return ns(value.isoformat())
    if isinstance(value, str) and not value.isdecimal():
        from pandas import Timestamp
        result = Timestamp(value)
        if result.tzinfo is None:
            raise DataError("Timestamp must include a timezone")
        return int(result.value)
    return int(decimal(value) * 1_000_000_000)


def fee_rate(raw):
    schedule = raw.get("feeSchedule")
    if schedule:
        if decimal(schedule.get("exponent", 1)) != 1:
            raise DataError("Nautilus 1.231.0 fee model only supports exponent 1")
        return decimal(schedule["rate"], high=Decimal(1)), "gamma_schedule_current"
    if raw.get("feesEnabled") is False:
        return Decimal(0), "gamma_fees_disabled"
    raise DataError("Fee schedule missing; refusing to assume zero fees")


def instruments_from_market(raw, *, event_id=None):
    market = parse(SDKMarket, raw)
    protocol = market.version.value if market.version else "unknown"
    if protocol not in ("v1", "v2") or not market.condition_id:
        raise DataError("Explicit v1/v2 protocol and condition ID required")
    rate, source = fee_rate(raw)
    # Snapshot minimum tick can change over the life of a market. Keep high
    # precision for historical observations; do not round trades to today's tick.
    tick = decimal(raw.get("orderPriceMinTickSize"), high=Decimal(1))
    if tick == 0:
        raise DataError("Positive market tick size required")
    if market.state.end_date is None:
        raise DataError("Market end date required for BinaryOption expiration")
    result, ids = [], set()
    for outcome in (market.outcomes.yes, market.outcomes.no):
        token = outcome.position_id if protocol == "v2" else outcome.token_id
        if token is None or str(token) in ids:
            raise DataError("Distinct protocol-specific outcome IDs required")
        token = str(token)
        ids.add(token)
        result.append(BinaryOption(
            instrument_id=InstrumentId(Symbol(f"{market.condition_id}-{token}"), Venue("POLYMARKET")),
            raw_symbol=Symbol(token), asset_class=AssetClass.ALTERNATIVE,
            currency=Currency("pUSD", 6, 0, "Polymarket USD", CurrencyType.CRYPTO),
            price_precision=16, size_precision=6,
            price_increment=Price.from_str("0.0000000000000001"), size_increment=Quantity.from_str("0.000001"),
            activation_ns=0, expiration_ns=ns(market.state.end_date), ts_event=0, ts_init=0,
            maker_fee=Decimal(0), taker_fee=rate, outcome=outcome.label, description=market.question,
            # Whitelist metadata: never expose outcomePrices/winner/resolution,
            # closed flags, current quotes or arbitrary Gamma payload to strategy.
            info={"event_id": event_id, "market_id": str(market.id), "condition_id": str(market.condition_id),
                  "token_id": token, "protocol": protocol, "neg_risk": market.state.neg_risk,
                  "market_tick_size_at_capture": str(tick), "fee_source": source,
                  "price_increment_kind": "historical_observation_precision"},
        ))
    return result


def _exact(value, precision=6):
    number = decimal(value)
    if number != number.quantize(Decimal(10) ** -precision):
        raise DataError(f"Observation exceeds supported {precision}-decimal precision")
    return f"{number:.{precision}f}"


def trade_ticks(instrument, rows, *, start=None, end=None):
    """Inclusive local bounds. Hash + row occurrence retains identical real fills.

    v2's public rows omit sequence/log id. Transaction hash alone is NOT unique.
    We retain multiplicity and preserve server order within each second (reversed
    from newest-first). Identity is reproducible for an identical recorded feed.
    """
    parsed = []
    for row in rows:
        if row.get("condition_id") != instrument.info["condition_id"]:
            raise DataError("Trade condition mismatch")
        if str(row.get("token_id")) != str(instrument.raw_symbol):
            continue
        trade = parse(Trade, row)
        stamp = ns(trade.timestamp)
        if (start is not None and stamp < ns(start)) or (end is not None and stamp > ns(end)):
            continue
        decimal(trade.price, high=Decimal(1))
        if trade.size <= 0:
            raise DataError("Trade size must be positive")
        parsed.append((stamp, row, trade))
    parsed.reverse()
    parsed.sort(key=lambda item: item[0])
    occurrences, ticks = Counter(), []
    for stamp, row, trade in parsed:
        # Include economic fields and wallet in the identity, exclude mutable profile text.
        identity = [row[k] for k in ("transaction_hash", "proxy_wallet", "token_id", "side", "timestamp", "price", "size")]
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        occurrences[digest] += 1
        ticks.append(TradeTick(
            instrument_id=instrument.id, price=Price.from_str(_exact(trade.price, 16)),
            size=Quantity.from_str(_exact(trade.size)),
            aggressor_side=AggressorSide.BUYER if trade.side == "BUY" else AggressorSide.SELLER,
            trade_id=TradeId(f"{digest}-{occurrences[digest]}"), ts_event=stamp, ts_init=stamp,
        ))
    return ticks


def price_quotes(instrument, rows, *, settled=False, start=None, end=None):
    """Analysis-only midpoint proxy: bid=ask=price, size=0, never invent depth.

    A settled series has an appended resolution point with resolution_seconds=0.
    Drop the final zero-resolution point conservatively, even for a split payout.
    Other zero-resolution rows are exact observations and must be retained.
    """
    points = [parse(PriceHistoryPoint, row) for row in rows]
    if settled and points and points[-1].resolution_seconds == 0:
        points.pop()
    quotes = []
    for point in sorted(points, key=lambda p: p.timestamp):
        stamp = ns(point.timestamp)
        if (start is not None and stamp < ns(start)) or (end is not None and stamp > ns(end)):
            continue
        decimal(point.price, high=Decimal(1))
        if point.resolution_seconds < 0:
            raise DataError("Negative price resolution")
        price = Price.from_str(_exact(point.price, 16))
        quotes.append(QuoteTick(instrument_id=instrument.id, bid_price=price, ask_price=price,
                                bid_size=Quantity.from_str("0.000000"), ask_size=Quantity.from_str("0.000000"),
                                ts_event=stamp, ts_init=stamp))
    return quotes


def book_quote(instrument, raw):
    """Convert a caller-supplied recorded L1/L2 snapshot, never fetch current book."""
    if str(raw["asset_id"]) != str(instrument.raw_symbol) or raw["market"] != instrument.info["condition_id"]:
        raise DataError("Book instrument mismatch")
    bids = [(decimal(r["price"], high=Decimal(1)), decimal(r["size"])) for r in raw["bids"]]
    asks = [(decimal(r["price"], high=Decimal(1)), decimal(r["size"])) for r in raw["asks"]]
    bids, asks = [x for x in bids if x[1] > 0], [x for x in asks if x[1] > 0]
    if not bids or not asks:
        raise DataError("Two-sided nonempty book required")
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if bid > ask:
        raise DataError("Crossed book")
    stamp = ns(decimal(raw["timestamp"]) / 1000)  # CLOB snapshot epoch milliseconds
    return QuoteTick(instrument_id=instrument.id, bid_price=Price.from_str(_exact(bid, 16)),
                     ask_price=Price.from_str(_exact(ask, 16)),
                     bid_size=Quantity.from_str(_exact(sum(q for p, q in bids if p == bid))),
                     ask_size=Quantity.from_str(_exact(sum(q for p, q in asks if p == ask))),
                     ts_event=stamp, ts_init=stamp)


@dataclass
class LoadedToken:
    instrument: BinaryOption
    trades: list[TradeTick]
    quotes: list[QuoteTick]
    metadata: dict


def load_snapshot(snapshot, *, token_id=None, start=None, end=None, price_store=None):
    if snapshot.get("schema_version") != 1:
        raise DataError("Unsupported snapshot schema")
    if start is not None and end is not None and ns(start) > ns(end):
        raise DataError("start must not exceed end")
    result = []
    for item in snapshot["markets"]:
        instruments = instruments_from_market(item["market"], event_id=str(snapshot["event"]["id"]))
        if price_store is not None:
            prices, resolutions = stored_prices(price_store, str(snapshot["event"]["id"]), item["market"], instruments)
            item = {**item, "prices": prices, "resolutions": resolutions}
        known = {str(i.raw_symbol) for i in instruments}
        if any(str(t.get("token_id")) not in known for t in item["trades"]):
            raise DataError("Trade token missing from market metadata")
        settled = resolution_state(item["market"], item.get("resolutions", []))
        for instrument in instruments:
            token = str(instrument.raw_symbol)
            if token_id is not None and str(token_id) != token:
                continue
            prices = item["prices"][token]
            result.append(LoadedToken(
                instrument, trade_ticks(instrument, item["trades"], start=start, end=end),
                price_quotes(instrument, prices, settled=settled, start=start, end=end),
                {"quote_kind": "zero_size_midpoint_proxy", "settled": settled,
                 "resolution_seconds": sorted({p["resolution_seconds"] for p in prices}),
                 "coverage": snapshot.get("coverage", {}), "source": snapshot.get("source"),
                 "same_second_order": "reversed_server_order; subsecond sequence unavailable"},
            ))
    if token_id is not None and not result:
        raise DataError("Token not found in snapshot")
    return result
