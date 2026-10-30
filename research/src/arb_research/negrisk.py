"""Paper-only negRisk valuation. No wallet, signer, or execution dependencies."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json

from .negrisk_protocol import asset_wire, diagnostic, group_reasons, identity, qualified_assets
from .polymarket.assets import require_assets, select_assets

D = Decimal
ZERO = D(0)
ONE = D(1)
FEE_TICK = D("0.00001")
CATEGORY_RATES = {
    "crypto": D("0.07"), "sports": D("0.05"), "finance": D("0.04"),
    "politics": D("0.04"), "economics": D("0.05"), "culture": D("0.05"),
    "weather": D("0.05"), "other": D("0.05"), "general": D("0.05"),
    "mentions": D("0.04"), "tech": D("0.04"), "geopolitics": ZERO,
}


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def timestamp(value):
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def taker_fee(shares, rate, price):
    """USD estimate per price-level fill; round to protocol's five decimals."""
    shares, rate, price = map(number, (shares, rate, price))
    if shares < 0 or not ZERO <= rate <= ONE or not ZERO <= price <= ONE:
        raise ValueError("invalid fee inputs")
    return (shares * rate * price * (ONE - price)).quantize(FEE_TICK, ROUND_HALF_UP)


@dataclass(frozen=True)
class Costs:
    split_usd: D = ZERO
    merge_usd: D = ZERO
    convert_usd: D = ZERO
    conversion_fee_bps: D = ZERO

    def __post_init__(self):
        for value in (self.split_usd, self.merge_usd, self.convert_usd, self.conversion_fee_bps):
            if not value.is_finite() or value < 0:
                raise ValueError("costs must be finite and nonnegative")
        if self.conversion_fee_bps >= 10000:
            raise ValueError("conversion fee must be below 10000 bps")


@dataclass
class Book:
    asset_id: str
    bids: list
    asks: list
    minimum: D
    as_of: datetime

    @classmethod
    def parse(cls, asset_id, raw, now, max_age):
        if str(raw.get("asset_id")) != asset_id:
            raise ValueError("book asset mismatch")
        as_of = timestamp(raw["timestamp"])
        age = (now - as_of).total_seconds()
        if age > max_age or age < -5:
            raise ValueError("stale or future orderbook")
        sides = []
        for side in ("bids", "asks"):
            levels = {}
            for row in raw[side]:
                p, q = number(row["price"]), number(row["size"])
                if not ZERO < p < ONE or q <= 0:
                    raise ValueError("invalid book level")
                levels[p] = levels.get(p, ZERO) + q
            sides.append(sorted(levels.items(), reverse=side == "bids"))
        minimum = number(raw["min_order_size"])
        if minimum <= 0:
            raise ValueError("invalid minimum order size")
        if sides[0] and sides[1] and sides[0][0][0] > sides[1][0][0]:
            raise ValueError("crossed orderbook")
        return cls(asset_id, *sides, minimum, as_of)

    def wire(self):
        return {
            "asset_id": self.asset_id, "as_of": self.as_of.isoformat(),
            "min_order_size": self.minimum,
            "best_bid": self.bids[0][0] if self.bids else None,
            "best_ask": self.asks[0][0] if self.asks else None,
            "bids": [{"price": p, "size": q} for p, q in self.bids],
            "asks": [{"price": p, "size": q} for p, q in self.asks],
        }


def fee_config(market, event, clob):
    # CLOB is authoritative; never apply the linear model to another exponent.
    schedule = clob.get("fd") or market.get("feeSchedule")
    if schedule:
        exponent = number(schedule.get("e", schedule.get("exponent", 1)))
        if exponent != ONE:
            raise ValueError("unsupported fee exponent")
        rate = number(schedule.get("r", schedule.get("rate")))
        source = "clob" if clob.get("fd") else "gamma"
    elif market.get("feesEnabled") is False:
        rate, source = ZERO, "fees_disabled"
    elif market.get("feesEnabled") is True:
        category = str(market.get("category") or event.get("category") or "").lower()
        if category not in CATEGORY_RATES:
            raise ValueError("fee schedule/category unavailable")
        rate, source = CATEGORY_RATES[category], "category_estimate"
    else:
        raise ValueError("fee configuration unavailable")
    if not ZERO <= rate <= ONE:
        raise ValueError("invalid fee rate")
    return rate, source


def assets(market):
    selected = require_assets(select_assets(market))
    result = {asset.outcome.strip().lower(): asset.asset_id for asset in selected}
    if sorted(result) != ["no", "yes"]:
        raise ValueError("expected distinct binary YES/NO assets")
    return result


@dataclass
class Leg:
    market_id: str
    outcome: str
    side: str
    book: Book
    rate: D
    ratio: D = ONE
    identity: dict | None = None

    @property
    def levels(self):
        return self.book.asks if self.side == "buy" else self.book.bids

    def value(self, quantity):
        remaining, value, fee = quantity * self.ratio, ZERO, ZERO
        for price, size in self.levels:
            fill = min(remaining, size)
            value += fill * price
            fee += taker_fee(fill, self.rate, price)
            remaining -= fill
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError("insufficient depth")
        return value, fee


def opportunity(kind, event_id, legs, collateral_per_unit, fixed_cost, risks, max_shares, max_skew,
                *, include_nonpositive=False):
    """Choose maximum modeled profit among synchronized depth breakpoints."""
    if not legs or any(not leg.levels for leg in legs):
        return None
    times = [leg.book.as_of for leg in legs]
    if (max(times) - min(times)).total_seconds() > max_skew:
        return None
    minimum = max(leg.book.minimum / leg.ratio for leg in legs)
    capacity = min(sum(q for _, q in leg.levels) / leg.ratio for leg in legs)
    ceiling = min(capacity, max_shares)
    if ceiling < minimum:
        return None
    candidates = {ceiling, minimum}
    for leg in legs:
        cumulative = ZERO
        for _, q in leg.levels:
            cumulative += q / leg.ratio
            if minimum <= cumulative <= ceiling:
                candidates.add(cumulative)
    best = None
    for quantity in sorted(candidates):
        buys, sells, fees = ZERO, ZERO, ZERO
        detail = []
        for leg in legs:
            value, fee = leg.value(quantity)
            if leg.side == "buy":
                buys += value
            else:
                sells += value
            fees += fee
            detail.append({**(leg.identity or {}), "market_id": leg.market_id, "asset_id": leg.book.asset_id,
                           "outcome": leg.outcome, "side": leg.side,
                           "shares": quantity * leg.ratio, "notional_usd": value, "fee_usd": fee})
        gross = sells - buys + quantity * collateral_per_unit
        net = gross - fees - fixed_cost
        if best is None or net > best["net_profit_usd"]:
            best = {"kind": kind, "event_id": event_id, "quantity": quantity,
                    "depth_capacity_shares": capacity, "gross_profit_usd": gross,
                    "taker_fees_usd": fees, "operation_cost_usd": fixed_cost,
                    "net_profit_usd": net, "net_edge_per_share": net / quantity,
                    "risks": sorted(set(risks)), "legs": detail}
    # A snapshot estimate, never a guaranteed/executable arbitrage claim.
    return best if best and (include_nonpositive or best["net_profit_usd"] > 0) else None


def scan(snapshot, costs=Costs(), max_shares=D("1000"), max_age=120, max_skew=10, near_hours=48,
         *, authoritative_only=False):
    now = timestamp(snapshot["as_of"])
    if max_shares <= 0 or not max_shares.is_finite() or max_age <= 0 or max_skew < 0 or near_hours < 0:
        raise ValueError("invalid scan limits")
    opportunities, groups, errors = [], [], list(snapshot.get("errors", []))
    geoblock = {**snapshot.get("geoblock", {}), "execution": "blocked"}
    compliance = dict(snapshot.get("compliance", {}))
    if compliance:
        # Replay input cannot turn a paper report into an execution permission.
        compliance.update(trading_allowed=False, allowlist_configured=False, mode="read_only")
    for event in snapshot["events"]:
        if event.get("negRisk") is not True or event.get("closed") or event.get("active") is not True:
            continue
        event_id = str(event["id"])
        risks = ["partial_fill", "non_atomic_snapshot", "operation_cost_assumptions"]
        if event.get("negRiskAugmented"):
            risks.append("augmented_negrisk")
        parsed = []
        seen_markets, seen_conditions, seen_assets = set(), set(), set()
        markets = event.get("markets", [])
        group = {"event_id": event_id, "title": event.get("title", ""),
                 "markets": [], "risks": risks, "complete": False,
                 "yes_ask_sum": None, "yes_bid_sum": None,
                 "yes_ask_deviation": None, "yes_bid_deviation": None,
                 "skipped_reasons": group_reasons(event), "skipped_markets": [],
                 "conversion_ratio": None}
        for market in markets:
            mid = str(market.get("id", ""))
            view = identity(market, event_id, snapshot["as_of"])
            try:
                if (market.get("closed") or market.get("active") is not True
                        or market.get("acceptingOrders") is not True
                        or market.get("enableOrderBook") is not True):
                    raise ValueError("inactive/closed/non-accepting market")
                selected = qualified_assets(market, view)
                ids = {k: a.asset_id for k, a in selected.items()}
                condition = str(market["conditionId"])
                if mid in seen_markets or condition in seen_conditions or seen_assets.intersection(ids.values()):
                    raise ValueError("duplicate market/condition/asset in group")
                clob = snapshot.get("clob_markets", {}).get(mid, {})
                schedule = clob.get("fd") or market.get("feeSchedule")
                if authoritative_only and schedule and not any(k in schedule for k in ("e", "exponent")):
                    raise ValueError("authoritative fee exponent unavailable")
                rate, source = fee_config(market, event, clob)
                if authoritative_only and source == "category_estimate":
                    raise ValueError("authoritative fee unavailable")
                books = {k: Book.parse(v, snapshot["books"][v], now, max_age) for k, v in ids.items()}
                if any(str(snapshot["books"][v].get("market")) != condition for v in ids.values()):
                    raise ValueError("book condition mismatch")
                seen_markets.add(mid)
                seen_conditions.add(condition)
                seen_assets.update(ids.values())
                mrisk = list(risks)
                if source == "category_estimate":
                    mrisk.append("category_fee_estimate")
                end = market.get("endDate") or event.get("endDate")
                if not end:
                    mrisk.append("unknown_resolution_time")
                elif (timestamp(end) - now).total_seconds() <= near_hours * 3600:
                    mrisk.append("near_resolution")
                other = bool(market.get("negRiskOther"))
                placeholder = bool(market.get("negRiskPlaceholder"))
                if other:
                    mrisk.append("other_outcome")
                if placeholder:
                    mrisk.append("placeholder_outcome")
                group["markets"].append({"market_id": mid, "question": market.get("question", ""),
                                         "neg_risk_market_id": market["negRiskMarketID"],
                                         "protocol_version": view.protocol_version,
                                         "module": market.get("module") or "v1_ctf",
                                         "asset_selection": view.model_dump(mode="json"),
                                         "assets": {k: asset_wire(a) for k, a in selected.items()},
                                         "fee_rate": rate, "fee_source": source, "risks": mrisk,
                                         "yes": books["yes"].wire(), "no": books["no"].wire()})
                parsed.append((market, books, rate, mrisk))
                if not other and not placeholder:
                    legs = [Leg(mid, k, "buy", books[k], rate,
                                identity=asset_wire(selected[k])) for k in ("yes", "no")]
                    result = opportunity("buy_binary_merge", event_id, legs, ONE, costs.merge_usd,
                                         mrisk, max_shares, max_skew)
                    if result:
                        opportunities.append(result)
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                skipped = diagnostic(market, view, str(exc), event_id=event_id, stage="market")
                errors.append(skipped)
                group["skipped_markets"].append(skipped)
        ids = {p[0]["negRiskMarketID"] for p in parsed}
        complete = (len(parsed) == len(markets) and len(parsed) >= 2 and len(ids) == 1
                    and not group["skipped_reasons"])
        if event.get("negRiskMarketID") and ids != {event["negRiskMarketID"]}:
            complete = False
        group["complete"] = complete
        if complete:
            group["conversion_ratio"] = ONE - costs.conversion_fee_bps / 10000
        if not complete:
            risks.append("incomplete_group")
            group["skipped_reasons"] = sorted(set(group["skipped_reasons"] + ["incomplete_negrisk_group"]))
            errors.append({"event_id": event_id, "stage": "group",
                           "reason": ",".join(group["skipped_reasons"])})
        if any(m.get("negRiskOther") for m in markets):
            risks.append("other_outcome")
        if any(m.get("negRiskPlaceholder") for m in markets):
            risks.append("placeholder_outcome")
        if "augmented_negrisk" in risks or "other_outcome" in risks or "placeholder_outcome" in risks:
            group["complete"] = False
        for side in ("ask", "bid"):
            levels = [getattr(p[1]["yes"], side + "s") for p in parsed]
            if group["complete"] and all(levels):
                total = sum((x[0][0] for x in levels), ZERO)
                group[f"yes_{side}_sum"] = total
                group[f"yes_{side}_deviation"] = total - ONE
        groups.append(group)
        # Augmented sets may hide unnamed outcomes. Never infer exhaustiveness.
        if not complete or any(r in risks for r in ("augmented_negrisk", "other_outcome", "placeholder_outcome")):
            continue
        combined_risks = risks + [r for p in parsed for r in p[3]]
        def leg(m, outcome, side, book, fee, ratio=ONE):
            selected = qualified_assets(m, identity(m, event_id, snapshot["as_of"]))
            return Leg(str(m["id"]), outcome, side, book, fee, ratio,
                       asset_wire(selected[outcome]))

        buy_legs = [leg(m, "yes", "buy", b["yes"], f) for m, b, f, _ in parsed]
        result = opportunity("buy_yes_basket", event_id, buy_legs, ONE, ZERO,
                             combined_risks + ["hold_to_resolution"], max_shares, max_skew)
        if result:
            opportunities.append(result)
        ratio = group["conversion_ratio"]
        # Split one binary pair, convert its NO into the remaining YES shares.
        for anchor, (market, books, fee, _) in enumerate(parsed):
            legs = [leg(m, "yes", "sell", b["yes"], f,
                        ONE if i == anchor else ratio) for i, (m, b, f, _) in enumerate(parsed)]
            result = opportunity("split_sell_yes_basket", event_id, legs, -ONE,
                                 costs.split_usd + costs.convert_usd, combined_risks,
                                 max_shares, max_skew)
            if result:
                result["anchor_market_id"] = str(market["id"])
                opportunities.append(result)
            legs = [leg(market, "no", "buy", books["no"], fee)] + [
                leg(m, "yes", "sell", b["yes"], f, ratio)
                for i, (m, b, f, _) in enumerate(parsed) if i != anchor]
            result = opportunity("no_to_yes", event_id, legs, ZERO, costs.convert_usd,
                                 combined_risks, max_shares, max_skew)
            if result:
                result["anchor_market_id"] = str(market["id"])
                opportunities.append(result)
    opportunities.sort(key=lambda r: (-r["net_edge_per_share"], -r["net_profit_usd"], r["event_id"], r["kind"], r.get("anchor_market_id", "")))
    return {"schema_version": "1.1", "as_of": snapshot["as_of"], "mode": "read_only",
            "currency": "USD", "collateral": "pUSD≈USD", "geoblock": geoblock,
            "coverage": snapshot.get("coverage", {}), "compliance": compliance,
            "assumptions": {"split_usd": costs.split_usd, "merge_usd": costs.merge_usd,
                            "convert_usd": costs.convert_usd, "conversion_fee_bps": costs.conversion_fee_bps,
                            "max_shares": max_shares, "max_book_age_seconds": max_age,
                            "max_book_skew_seconds": max_skew, "near_resolution_hours": near_hours,
                            "fee_rounding": "per_price_level_half_up_5dp"},
            "summary": {"groups_scanned": len(groups), "opportunities": len(opportunities), "errors": len(errors)},
            "groups": groups, "opportunities": opportunities, "errors": errors}


def dumps(value):
    return json.dumps(value, default=lambda v: format(v, "f") if isinstance(v, D) else str(v),
                      ensure_ascii=False, indent=2, allow_nan=False) + "\n"
