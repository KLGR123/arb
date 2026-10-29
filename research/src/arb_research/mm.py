"""Deterministic offline, single-outcome market-making research prototype.

No exchange client, credentials, signing, or networking. See docs/mm-backtest.md
for the fixture contract and the deliberately approximate execution/reward model.
"""

import argparse
from bisect import bisect_left
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from arb_research.contracts import StrictModel


NonNegative = Annotated[float, Field(strict=True, ge=0, le=1e12)]
Positive = Annotated[float, Field(strict=True, gt=0, le=1e12)]
Probability = Annotated[float, Field(strict=True, gt=0, lt=1)]
Millis = Annotated[int, Field(strict=True, ge=0, le=4102444800000)]
DAY_MS = 86_400_000
SAMPLE_DIR = Path(__file__).with_name("fixtures") / "mm"


class Level(StrictModel):
    price: Probability
    size: Positive


class Book(StrictModel):
    timestamp_ms: Millis
    bids: Annotated[list[Level], Field(min_length=1)]
    asks: Annotated[list[Level], Field(min_length=1)]
    competitor_score: NonNegative
    # Optional recorded size-cutoff-adjusted midpoint, in probability units.
    reward_midpoint: Probability | None = None

    @model_validator(mode="after")
    def ordered(self):
        bid_prices = [level.price for level in self.bids]
        ask_prices = [level.price for level in self.asks]
        if bid_prices != sorted(set(bid_prices), reverse=True):
            raise ValueError("bids must have unique descending prices")
        if ask_prices != sorted(set(ask_prices)):
            raise ValueError("asks must have unique ascending prices")
        if bid_prices[0] >= ask_prices[0]:
            raise ValueError("book must be uncrossed")
        return self

    @property
    def midpoint(self) -> float:
        return float((Decimal(str(self.bids[0].price)) + Decimal(str(self.asks[0].price))) / 2)


class TakerIntent(StrictModel):
    timestamp_ms: Millis
    side: Literal["buy", "sell"]
    limit_price: Probability
    size: Positive


class Market(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    category: Literal["crypto", "sports", "politics"]
    tick_size: Annotated[float, Field(strict=True, ge=0.000001, le=0.1)]
    min_order_size: Positive
    fee_rate: Annotated[float, Field(strict=True, ge=0, le=1)]
    rebate_fraction: Annotated[float, Field(strict=True, ge=0, le=1)]
    reward_min_size: Positive
    reward_max_spread: Probability
    reward_daily_pool: NonNegative


class Fixture(StrictModel):
    schema_version: Literal["mm-fixture-v1"]
    provenance: Annotated[str, Field(min_length=1)]
    market: Market
    end_ms: Millis
    books: Annotated[list[Book], Field(min_length=2)]
    takers: list[TakerIntent]
    settlement_price: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def chronology_and_ticks(self):
        times = [book.timestamp_ms for book in self.books]
        if times != sorted(set(times)) or not times[0] < self.end_ms >= times[-1]:
            raise ValueError("books must be strictly ordered within a nonempty run")
        tick = Decimal(str(self.market.tick_size))
        for book in self.books:
            for level in book.bids + book.asks:
                if Decimal(str(level.price)) % tick:
                    raise ValueError("book prices must align with tick_size")
        for taker in self.takers:
            if not times[0] <= taker.timestamp_ms < self.end_ms:
                raise ValueError("taker send times must be inside the run")
            if Decimal(str(taker.limit_price)) % tick:
                raise ValueError("taker limit prices must align with tick_size")
        return self


class Config(StrictModel):
    initial_cash: NonNegative = 1000.0
    initial_inventory: NonNegative = 50.0
    target_inventory: NonNegative = 50.0
    max_inventory: Positive = 200.0
    order_size: Positive = 20.0
    center_offset: Annotated[float, Field(strict=True, ge=-1, le=1)] = 0.0
    spread: Probability = 0.02
    inventory_skew: Annotated[float, Field(strict=True, ge=0, le=1)] = 0.0001
    risk_aversion: Annotated[float, Field(strict=True, ge=0, le=100)] = 0.1
    variance_per_second: Annotated[float, Field(strict=True, ge=0, le=1)] = 0.0001
    horizon_seconds: Annotated[float, Field(strict=True, gt=0, le=86400)] = 60.0
    # A synthetic taker's send time is delayed before matching. For recorded
    # exchange execution times, use zero to avoid double-counting latency.
    taker_latency_ms: Annotated[int, Field(strict=True, ge=0, le=60000)] = 50
    markout_horizons_ms: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=86400000)]],
                                    Field(min_length=1, max_length=32)] = [1000, 5000]
    max_markout_gap_ms: Annotated[int, Field(strict=True, ge=0, le=60000)] = 1000

    @model_validator(mode="after")
    def positions(self):
        if max(self.initial_inventory, self.target_inventory) > self.max_inventory:
            raise ValueError("initial/target inventory cannot exceed max_inventory")
        if len(set(self.markout_horizons_ms)) != len(self.markout_horizons_ms):
            raise ValueError("markout horizons must be unique")
        return self


def quote_prices(midpoint: float, inventory: float, market: Market, config: Config) -> tuple[float, float, float]:
    """A-S inspired reservation price, fixed variance, passive tick rounding."""
    # Do all price arithmetic before rounding in decimal. Converting an already
    # computed float (e.g. .4 + .01 == .41000000000000003) widens a full tick.
    def decimal(value):
        return Decimal(str(value))

    risk = decimal(config.risk_aversion) * decimal(config.variance_per_second) * decimal(config.horizon_seconds)
    center = decimal(midpoint) + decimal(config.center_offset) - (decimal(inventory) - decimal(config.target_inventory)) * (decimal(config.inventory_skew) + risk)
    half = (decimal(config.spread) + risk) / 2
    tick = Decimal(str(market.tick_size))

    def rounded(price, rounding):
        return float((price / tick).to_integral_value(rounding=rounding) * tick)

    return float(center), rounded(center - half, ROUND_FLOOR), rounded(center + half, ROUND_CEILING)


def order_score(price: float, size: float, midpoint: float, market: Market) -> float:
    distance = abs(price - midpoint)
    if size < market.reward_min_size or distance >= market.reward_max_spread:
        return 0.0
    return size * ((market.reward_max_spread - distance) / market.reward_max_spread) ** 2


def minimum_score(bid_score: float, ask_score: float, midpoint: float) -> float:
    two_sided = min(bid_score, ask_score)
    if 0.1 <= midpoint <= 0.9:
        return max(two_sided, bid_score / 3, ask_score / 3)
    return two_sided


def fee_equivalent(size: float, price: float, rate: float) -> float:
    # Protocol precision approximation, never a fee charged to this maker.
    quantity, probability, coefficient = map(lambda x: Decimal(str(x)), (size, price, rate))
    value = quantity * coefficient * probability * (1 - probability)
    return float(value.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _adjusted_midpoint(book: Book, market: Market) -> float | None:
    if book.reward_midpoint is not None:
        return book.reward_midpoint

    def cutoff(levels):
        size = 0.0
        for level in levels:
            size += level.size
            if size >= market.reward_min_size:
                return level.price
        return None

    bid, ask = cutoff(book.bids), cutoff(book.asks)
    return None if bid is None or ask is None else float((Decimal(str(bid)) + Decimal(str(ask))) / 2)


def _markouts(fills: list[dict], fixture: Fixture, config: Config) -> dict:
    times = [book.timestamp_ms for book in fixture.books]
    summary = {}
    for horizon in config.markout_horizons_ms:
        valid = []
        for fill in fills:
            target = fill["timestamp_ms"] + horizon
            i = bisect_left(times, target)
            value = None
            if i < len(times) and times[i] - target <= config.max_markout_gap_ms:
                future = fixture.books[i]
                sign = 1 if fill["side"] == "buy" else -1
                drift = sign * (future.midpoint - fill["midpoint"])
                value = {
                    "sample_timestamp_ms": future.timestamp_ms,
                    "signed_midpoint_drift": drift,
                    "adverse_selection_cost": -drift * fill["size"],
                    "execution_markout": sign * (future.midpoint - fill["price"]) * fill["size"],
                }
                valid.append((fill["size"], value))
            fill.setdefault("markouts", {})[str(horizon)] = value
        volume = sum(size for size, _ in valid)
        summary[str(horizon)] = {
            "observed_fills": len(valid), "missing_fills": len(fills) - len(valid),
            "observed_size": volume,
            "size_weighted_signed_midpoint_drift": (
                sum(size * value["signed_midpoint_drift"] for size, value in valid) / volume if volume else None),
            "adverse_selection_cost": sum(value["adverse_selection_cost"] for _, value in valid),
            "execution_markout": sum(value["execution_markout"] for _, value in valid),
        }
    return summary


def simulate(fixture: Fixture, config: Config | None = None) -> dict:
    config = config or Config()
    market = fixture.market
    cash, inventory = config.initial_cash, config.initial_inventory
    initial_equity = cash + inventory * fixture.books[0].midpoint
    book = fixture.books[0]
    external: dict[str, dict[float, float]] = {"buy": {}, "sell": {}}
    orders: dict[str, dict | None] = {"buy": None, "sell": None}
    fills, quotes, equity = [], [], []
    daily: dict[str, dict] = {}
    rebate, rewards = 0.0, 0.0
    eligible_seconds, score_seconds = 0.0, 0.0
    # Book updates precede arrivals at equal timestamps; takers retain file order.
    events = [(b.timestamp_ms, 0, i, b) for i, b in enumerate(fixture.books)]
    events += [(t.timestamp_ms + config.taker_latency_ms, 1, i, t)
               for i, t in enumerate(fixture.takers)
               if t.timestamp_ms + config.taker_latency_ms < fixture.end_ms]
    events.sort(key=lambda e: e[:3])

    def day_entry(timestamp):
        day = datetime.fromtimestamp(timestamp / 1000, UTC).date().isoformat()
        return daily.setdefault(day, {"date": day, "maker_rebate": 0.0, "liquidity_rewards": 0.0})

    def score():
        midpoint = _adjusted_midpoint(book, market)
        if midpoint is None:
            return 0.0
        scores = [order_score(order["price"], order["remaining"], midpoint, market) if order else 0.0
                  for order in orders.values()]
        return minimum_score(*scores, midpoint)

    def accrue(start, end):
        nonlocal rewards, eligible_seconds, score_seconds
        own = score()
        seconds = (end - start) / 1000
        if own > 0:
            eligible_seconds += seconds
        score_seconds += own * seconds
        share = own / (own + book.competitor_score) if own else 0.0
        while start < end:
            stop = min(end, (start // DAY_MS + 1) * DAY_MS)
            earned = market.reward_daily_pool * share * (stop - start) / DAY_MS
            day_entry(start)["liquidity_rewards"] += earned
            rewards += earned
            start = stop

    def reprice(timestamp):
        center, bid, ask = quote_prices(book.midpoint, inventory, market, config)
        tick = Decimal(str(market.tick_size))
        # Post-only: never turn a stale/model quote into a taker order.
        bid = min(bid, float(Decimal(str(book.asks[0].price)) - tick))
        ask = max(ask, float(Decimal(str(book.bids[0].price)) + tick))
        for side, price in (("buy", bid), ("sell", ask)):
            orders[side] = None
            if not 0 < price < 1:
                continue
            size = min(config.order_size, config.max_inventory - inventory, cash / price) if side == "buy" else min(config.order_size, inventory)
            if size < market.min_order_size:
                continue
            ahead = sum(size for p, size in external[side].items()
                        if (p >= price if side == "buy" else p <= price))
            orders[side] = {"price": price, "remaining": size, "queue_ahead": ahead}
        quotes.append({"timestamp_ms": timestamp, "midpoint": book.midpoint, "center": center,
                       "inventory": inventory, "cash": cash,
                       "bid": dict(orders["buy"]) if orders["buy"] else None,
                       "ask": dict(orders["sell"]) if orders["sell"] else None})

    def match(taker, timestamp):
        nonlocal cash, inventory, rebate
        side = "sell" if taker.side == "buy" else "buy"
        order = orders[side]
        levels = external[side]
        # External levels already present at this snapshot are ahead at ties.
        candidates = [(price, 0, size) for price, size in levels.items() if size > 0]
        if order:
            candidates.append((order["price"], 1, order["remaining"]))
        candidates.sort(key=lambda x: (x[0] if side == "sell" else -x[0], x[1]))
        remaining = taker.size
        for price, own, available in candidates:
            if (side == "sell" and price > taker.limit_price) or (side == "buy" and price < taker.limit_price):
                break
            quantity = min(available, remaining)
            remaining -= quantity
            if own:
                sign = 1 if side == "buy" else -1
                cash -= sign * quantity * price
                inventory += sign * quantity
                order["remaining"] -= quantity
                equivalent = fee_equivalent(quantity, price, market.fee_rate)
                earned = equivalent * market.rebate_fraction
                rebate += earned
                day_entry(timestamp)["maker_rebate"] += earned
                fills.append({"id": len(fills) + 1, "timestamp_ms": timestamp,
                              "side": side, "price": price, "size": quantity,
                              "midpoint": book.midpoint, "fee": 0.0,
                              "fee_equivalent": equivalent, "maker_rebate": earned})
                if order["remaining"] <= 1e-10:
                    orders[side] = None
            else:
                levels[price] -= quantity
            if remaining <= 1e-10:
                break

    previous = fixture.books[0].timestamp_ms
    for timestamp, kind, _, event in events:
        accrue(previous, timestamp)
        if kind == 0:
            book = event
            external = {"buy": {l.price: l.size for l in book.bids},
                        "sell": {l.price: l.size for l in book.asks}}
            if timestamp < fixture.end_ms:
                reprice(timestamp)
            else:
                orders = {"buy": None, "sell": None}
        else:
            match(event, timestamp)
        equity.append({"timestamp_ms": timestamp, "cash": cash, "inventory": inventory,
                       "midpoint": book.midpoint, "trading_pnl": cash + inventory * book.midpoint - initial_equity,
                       "settlement_jump": 0.0,
                       "maker_rebate": rebate, "liquidity_rewards": rewards,
                       "equity_with_accruals": cash + inventory * book.midpoint + rebate + rewards})
        previous = timestamp
    accrue(previous, fixture.end_ms)
    trading = cash + inventory * book.midpoint - initial_equity
    jump = inventory * (fixture.settlement_price - book.midpoint) if fixture.settlement_price is not None else 0.0
    inventory_before_settlement = inventory
    if fixture.settlement_price is not None:
        cash += inventory * fixture.settlement_price
        inventory = 0.0
    pnl = {"trading_pnl": trading, "maker_rebate": rebate, "liquidity_rewards": rewards,
           "settlement_jump": jump, "fees": 0.0,
           "total": trading + rebate + rewards + jump}
    equity.append({"timestamp_ms": fixture.end_ms, "cash": cash, "inventory": inventory,
                   "midpoint": book.midpoint, "trading_pnl": trading, "settlement_jump": jump,
                   "maker_rebate": rebate, "liquidity_rewards": rewards,
                   "equity_with_accruals": initial_equity + pnl["total"]})
    canonical = json.dumps(fixture.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": "mm-result-v1", "mode": "offline_approximation", "currency": "pUSD",
        "usd_per_pusd_assumption": 1.0, "market": market.model_dump(),
        "provenance": fixture.provenance, "fixture_sha256": sha256(canonical.encode()).hexdigest(),
        "config": config.model_dump(), "start_ms": fixture.books[0].timestamp_ms, "end_ms": fixture.end_ms,
        "pnl": pnl, "initial_equity": initial_equity,
        "ending_equity_with_accruals": initial_equity + pnl["total"],
        "ending_cash": cash, "ending_inventory": inventory,
        "inventory_before_settlement": inventory_before_settlement,
        "reward_scoring": {"eligible_seconds": eligible_seconds, "score_seconds": score_seconds},
        "daily_accruals": sorted(daily.values(), key=lambda entry: entry["date"]),
        "takers_arriving_after_end": sum(t.timestamp_ms + config.taker_latency_ms >= fixture.end_ms for t in fixture.takers),
        "adverse_selection": _markouts(fills, fixture, config),
        "fills": fills, "quotes": quotes, "equity": equity,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path, help="local mm-fixture-v1 JSON")
    source.add_argument("--sample", choices=["crypto", "sports", "politics"])
    parser.add_argument("--config", type=Path, help="local Config JSON overrides")
    parser.add_argument("--output", type=Path, help="JSON result (default: stdout)")
    args = parser.parse_args()
    try:
        path = args.fixture or SAMPLE_DIR / f"{args.sample}.json"
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        config = Config.model_validate_json(args.config.read_text(encoding="utf-8")) if args.config else Config()
        payload = json.dumps(simulate(fixture, config), indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"mm backtest: {exc}\n")


if __name__ == "__main__":
    main()
