"""Offline, independent taker probes against received L2. Never an execution API."""

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal as D
import math

from .negrisk import number, taker_fee
from .orderbook.core import Book, Rebuilder, digest
from .updown import PricePath, distribution, fair_up, parse_market

NS = 10**9


def walk_asks(asks, shares, fee_rate, tick_size, adverse_ticks=0):
    """Walk displayed asks then charge a hypothetical adverse shift per share.

    The shift is a stress cost, not fabricated new displayed liquidity. Depth
    shifted to >=1 is unavailable. Fees round per filled price level like ARB-10.
    """
    shares, rate, tick = map(number, (shares, fee_rate, tick_size))
    if shares <= 0 or not 0 <= rate <= 1 or not 0 < tick < 1:
        raise ValueError("invalid fill parameters")
    if type(adverse_ticks) is not int or not 0 <= adverse_ticks <= 100:
        raise ValueError("invalid slippage ticks")
    parsed = [(number(r["price"]), number(r["size"])) for r in asks]
    if (any(not 0 < p < 1 or q <= 0 for p, q in parsed)
            or len({p for p, _ in parsed}) != len(parsed)):
        raise ValueError("invalid or duplicate ask level")
    remaining, cost, fee, raw_cost, fills = shares, D(0), D(0), D(0), []
    for price, size in sorted(parsed):
        stressed = price + tick * adverse_ticks
        if stressed >= 1 or remaining == 0:
            break
        quantity = min(size, remaining)
        charge = taker_fee(quantity, rate, stressed)
        cost += quantity * stressed
        raw_cost += quantity * price
        fee += charge
        remaining -= quantity
        fills.append({"displayed_price": float(price), "stressed_price": float(stressed),
                      "shares": float(quantity), "fee_usd": float(charge)})
    filled = shares - remaining
    return {"filled_shares": float(filled), "unfilled_shares": float(remaining),
            "fill_fraction": float(filled / shares), "cost_usd": float(cost),
            "fee_usd": float(fee), "slippage_cost_usd": float(cost - raw_cost),
            "vwap": float(cost / filled) if filled else None,
            "fee_usd_per_share": float(fee / filled) if filled else None,
            "fills": fills}


@dataclass
class Frame:
    at: int
    books: dict
    updated: dict
    epochs: dict


class Timeline:
    """ARB-8 replay in receive order; gaps invalidate probes across reconnects."""

    def __init__(self, events, *, rest_snapshots=False):
        engine, updated, epochs = Rebuilder(), {}, Counter()
        self.frames, self.times, self.gaps = [], [], []
        previous = None
        for row in events:
            if (type(row["received_ns"]) is not int or row["version"] != 1
                    or row["sha256"] != digest({k: v for k, v in row.items() if k != "sha256"})):
                raise ValueError("invalid event checksum/version/time")
            order = row["received_ns"], row["session"], row["seq"]
            if previous is not None and order <= previous:
                raise ValueError("journal order regression")
            previous = order
            if rest_snapshots:
                # Independent REST snapshots have no continuity guarantee. A new
                # full snapshot replaces the previous poll; it is not a WS delta
                # mismatch. In this explicit mode holding a snapshot until the
                # next receipt is only an optimistic sampling assumption.
                if row.get("source") != "rest_snapshot" or row["payload"].get("event_type") != "book":
                    raise ValueError("expected independent REST snapshot")
                engine.books[row["payload"]["asset_id"]] = Book()
            elif row.get("source") == "rest_snapshot":
                raise ValueError("REST snapshot requires explicit sampling mode")
            engine.apply(row)
            if row["payload"].get("event_type") == "book" and row["payload"].get("tick_size") is not None:
                engine.books[row["payload"]["asset_id"]].tick_size = str(row["payload"]["tick_size"])
            gaps, engine.gaps = engine.gaps, []
            self.gaps.extend(gaps)
            for gap in gaps:
                targets = [gap["asset_id"]] if gap.get("asset_id") else list(engine.books)
                for asset in targets:
                    epochs[asset] += 1
            event = row["payload"]
            if row["kind"] == "control":
                # started/connected/stopped also break continuity, even without a gap row.
                for asset in engine.books:
                    epochs[asset] += 1
            elif event.get("event_type") == "book":
                updated[event["asset_id"]] = row["received_ns"]
            elif event.get("event_type") == "price_change":
                for change in event["price_changes"]:
                    updated[change["asset_id"]] = row["received_ns"]
            self.times.append(row["received_ns"])
            self.frames.append(Frame(row["received_ns"], engine.export(), updated.copy(), dict(epochs)))
        if not self.frames:
            raise ValueError("empty book recording")

    def frame(self, at):
        i = bisect_right(self.times, at) - 1
        if i < 0 or at > self.times[-1]:
            raise ValueError("book recording does not cover time")
        return self.frames[i]


def usable(frame, asset, at, condition, max_age):
    book = frame.books.get(asset)
    if not book or not book["available"]:
        raise ValueError("book_unavailable")
    if book["market"] != condition:
        raise ValueError("book_market_mismatch")
    if asset not in frame.updated or at - frame.updated[asset] > max_age * NS:
        raise ValueError("stale_book")
    if book["timestamp"] is None:
        raise ValueError("missing_server_timestamp")
    server_age = D(at) / NS - number(book["timestamp"]) / 1000
    if server_age > number(max_age) or server_age < -1:
        raise ValueError("stale_or_future_server_timestamp")
    bids = [(number(r["price"]), number(r["size"])) for r in book["bids"]]
    asks = [(number(r["price"]), number(r["size"])) for r in book["asks"]]
    if (not bids or not asks or any(not 0 < p < 1 or q <= 0 for p, q in bids + asks)
            or max(p for p, _ in bids) >= min(p for p, _ in asks)):
        raise ValueError("empty_or_crossed_book")
    return book


def summarize(rows):
    fills = [r for r in rows if r["filled_shares"] > 0]
    return {"probes": len(rows), "filled_probes": len(fills),
            "full_fills": sum(r["fill_fraction"] == 1 for r in rows),
            "positive_paper_signals": sum(r["paper_net_edge"] > 0 for r in rows),
            "positive_fillable_probes": sum(r["fillable_edge"] is not None and r["fillable_edge"] > 0 for r in rows),
            "reasons": dict(Counter(r["reason"] for r in rows)),
            "paper_edge": distribution([r["paper_edge"] for r in rows]),
            "paper_net_edge": distribution([r["paper_net_edge"] for r in rows]),
            "fillable_edge": distribution([r["fillable_edge"] for r in fills]),
            "paired_paper_net_edge": distribution([r["paper_net_edge"] for r in fills]),
            "edge_erosion": distribution([r["paper_net_edge"] - r["fillable_edge"] for r in fills]),
            "fill_fraction": distribution([r["fill_fraction"] for r in rows]),
            "expected_surplus_per_requested_share": distribution([r["expected_surplus_per_requested_share"] for r in rows])}


def analyze(recording, *, shares=100, delays=(0, .05, .25, 1, 2), ticks=(0, 1, 3, 5), max_age=5):
    """Freeze the decision fair value/direction; consume only books received by arrival.

    Each (signal, delay, stress) is an independent counterfactual. No sequential
    inventory, queue position, fill probability or exchange execution is modeled.
    """
    shares = number(shares)
    if shares <= 0 or not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("invalid quantity or book age")
    if (not delays or 0 not in delays or len(set(delays)) != len(delays)
            or any(not math.isfinite(d) or not 0 <= d <= 10 for d in delays)
            or not ticks or 0 not in ticks or len(set(ticks)) != len(ticks)
            or any(type(t) is not int or not 0 <= t <= 100 for t in ticks)):
        raise ValueError("invalid scenarios; zero baseline required")
    rest = recording.get("book_transport") == "rest"
    timeline = Timeline(recording["events"], rest_snapshots=rest)
    path = PricePath(tuple(tuple(p) for p in recording["cex"]["points"]))
    signals, probes, excluded = [], [], []
    seen = set()
    for event in recording["markets"]:
        market = parse_market(event, require_resolved=False)
        if market["id"] in seen:
            raise ValueError("duplicate market")
        seen.add(market["id"])
        raw = event["markets"][0]
        condition = raw["conditionId"]
        minimum, default_tick = number(raw["orderMinSize"]), number(raw["orderPriceMinTickSize"])
        if minimum <= 0 or not 0 < default_tick < 1 or shares < minimum:
            raise ValueError("unsupported minimum or tick size")
        # All scenarios probe the same decision cohort. Expiry and tail clipping
        # are counted rather than silently dropping probes.
        first = max(math.ceil(timeline.times[0] / NS), int(market["start"]))
        last = min(math.floor(timeline.times[-1] / NS), int(market["end"]) - 1)
        for second in range(first, last + 1):
            at = second * NS
            try:
                frame = timeline.frame(at)
                up = usable(frame, market["up_token"], at, condition, max_age)
                midpoint = (max(number(r["price"]) for r in up["bids"])
                            + min(number(r["price"]) for r in up["asks"])) / 2
                # One-second finalized close is assumed visible after an extra second.
                # Collection is retrospective: this lag is an assumption, not a receipt.
                observed = second - 1
                sigma = path.sigma(observed)
                probability = fair_up(path, market["start"], market["end"], observed, sigma=sigma)
                side = "Up" if probability >= float(midpoint) else "Down"
                asset = market["up_token"] if side == "Up" else market["down_token"]
                value = probability if side == "Up" else 1 - probability
                reference = midpoint if side == "Up" else 1 - midpoint
                paper = value - float(reference)
                paper_net = paper - float(taker_fee(shares, market["fee_rate"], reference) / shares)
                # A computable paper signal stays in the denominator even when
                # the selected outcome's decision book is missing or stale.
                tick, depth, decision_ask, decision_reason = default_tick, None, None, None
                try:
                    book = usable(frame, asset, at, condition, max_age)
                    tick = number(book["tick_size"]) if book["tick_size"] else default_tick
                    decision_ask = min(number(r["price"]) for r in book["asks"])
                    depth = sum((number(r["size"]) for r in book["asks"]
                                 if number(r["price"]) <= decision_ask + 5 * tick), D(0))
                except ValueError as exc:
                    decision_reason = str(exc)
            except (ValueError, KeyError, ArithmeticError) as exc:
                excluded.append({"market_id": market["id"], "decision_at": second, "reason": str(exc)})
                continue
            signal = {"signal_id": f"{market['id']}:{second}", "market_id": market["id"],
                      "decision_at": second, "cex_observed_at": observed, "side": side,
                      "remaining_seconds": market["end"] - second,
                      "sigma_per_sqrt_second": sigma, "fair_value": value,
                      "reference_price": float(reference), "paper_edge": paper, "paper_net_edge": paper_net,
                      "decision_best_ask": float(decision_ask) if decision_ask is not None else None,
                      "decision_book_reason": decision_reason,
                      "ask_depth_within_5_ticks": float(depth) if depth is not None else None,
                      "tick_size": float(tick)}
            signals.append(signal)
            for delay in delays:
                arrival = at + round(delay * NS)
                for adverse in ticks:
                    result = {"signal_id": signal["signal_id"], "delay_seconds": delay, "adverse_ticks": adverse,
                              "arrival_ns": arrival, "paper_edge": paper, "paper_net_edge": paper_net,
                              "filled_shares": 0., "fill_fraction": 0., "unfilled_shares": float(shares),
                              "fillable_edge": None, "expected_surplus_per_requested_share": 0.,
                              "cost_usd": 0., "fee_usd": 0., "slippage_cost_usd": 0., "vwap": None,
                              "fee_usd_per_share": None, "fills": [], "reason": "unavailable"}
                    try:
                        if arrival >= market["end"] * NS:
                            raise ValueError("expired")
                        if arrival > timeline.times[-1]:
                            raise ValueError("recording_tail")
                        future = timeline.frame(arrival)
                        arrived = usable(future, asset, arrival, condition, max_age)
                        if future.epochs.get(asset, 0) != frame.epochs.get(asset, 0):
                            raise ValueError("continuity_gap")
                        arrival_tick = number(arrived["tick_size"]) if arrived["tick_size"] else default_tick
                        fill = walk_asks(arrived["asks"], shares, market["fee_rate"], arrival_tick, adverse)
                        if 0 < fill["filled_shares"] < float(minimum):
                            raise ValueError("below_minimum_fill")
                        result.update(fill)
                        result["reason"] = "full" if fill["fill_fraction"] == 1 else "partial" if fill["filled_shares"] else "no_depth"
                        result["arrival_book_received_ns"] = future.updated[asset]
                        if fill["filled_shares"]:
                            result["fillable_edge"] = value - fill["vwap"] - fill["fee_usd_per_share"]
                            result["expected_surplus_per_requested_share"] = result["fillable_edge"] * fill["fill_fraction"]
                    except (ValueError, KeyError, ArithmeticError) as exc:
                        result["reason"] = str(exc)
                    probes.append(result)
    baseline = [r for r in probes if r["delay_seconds"] == 0 and r["adverse_ticks"] == 0]
    lookup = {r["signal_id"]: r for r in signals}
    predicates = {
        "remaining_0_60s": lambda s: s["remaining_seconds"] <= 60,
        "remaining_60_180s": lambda s: 60 < s["remaining_seconds"] <= 180,
        "remaining_180_300s": lambda s: 180 < s["remaining_seconds"] <= 300,
        "sigma_below_0.00005": lambda s: s["sigma_per_sqrt_second"] < .00005,
        "sigma_0.00005_0.00010": lambda s: .00005 <= s["sigma_per_sqrt_second"] < .00010,
        "sigma_at_least_0.00010": lambda s: s["sigma_per_sqrt_second"] >= .00010,
        "depth_unavailable": lambda s: s["ask_depth_within_5_ticks"] is None,
        "depth_below_100_shares": lambda s: s["ask_depth_within_5_ticks"] is not None and s["ask_depth_within_5_ticks"] < 100,
        "depth_100_1000_shares": lambda s: s["ask_depth_within_5_ticks"] is not None and 100 <= s["ask_depth_within_5_ticks"] < 1000,
        "depth_at_least_1000_shares": lambda s: s["ask_depth_within_5_ticks"] is not None and s["ask_depth_within_5_ticks"] >= 1000,
    }
    return {"schema_version": 1, "study": "btc-updown-fillable-edge", "execution": "disabled",
            "status": "complete" if signals else "incomplete", "recorded_at": recording["recorded_at"],
            "compliance": recording.get("compliance"),
            "assumptions": {"collateral": "pUSD assumed USD", "cex": "BTCUSDT assumed USD, Chainlink proxy",
                            "model": "ARB-15 fair_up with trailing 900s sigma and TWAP60",
                            "cex_visibility": "retrospective finalized 1s close with assumed extra 1s lag; no CEX receive times",
                            "fair_value": "fixed at decision for all arrival scenarios",
                            "reference": "Up L2 midpoint, Down=1-Up midpoint (paper reference only)",
                            "fills": "independent optimistic displayed-ask taker probes; no actual orders or fill probability",
                            "slippage": "adverse_ticks * arrival tick added to every consumed level; shifted price >=1 unavailable",
                            "fees": "negrisk.taker_fee per stressed level, half-up 5dp; no rebates",
                            "no_fill_edge": "null; zero surplus per requested share; excluded from filled edge distribution",
                            "minimum": "reject requested size or partial fill below market minimum",
                            "book_clock": "local received_ns, no interpolation, invalidate WS gaps",
                            "book_transport": recording.get("book_transport", "websocket"),
                            "rest_sampling": "hold last received snapshot to next receipt within max_age; unobserved changes unknown" if rest else None,
                            "max_book_age_seconds": max_age, "server_clock_future_tolerance_seconds": 1,
                            "requested_shares": float(shares),
                            "depth_bucket": "decision side ask quantity within 5 current ticks of best ask",
                            "result_scope": "descriptive short recording; complete means replay has signals, not statistical power"},
            "coverage": {"markets": len(seen), "markets_with_signals": len({s["market_id"] for s in signals}),
                         "first_received_ns": timeline.times[0], "last_received_ns": timeline.times[-1],
                         "events": len(recording["events"]), "signals": len(signals),
                         "excluded_decisions": len(excluded), "gaps": len(timeline.gaps)},
            "baseline": summarize(baseline),
            "sensitivity": [{"delay_seconds": d, "adverse_ticks": t,
                             **summarize([r for r in probes if r["delay_seconds"] == d and r["adverse_ticks"] == t])}
                            for d in delays for t in ticks],
            "buckets": {label: summarize([r for r in baseline if predicate(lookup[r["signal_id"]])])
                        for label, predicate in predicates.items()},
            "signals": signals, "probes": probes, "excluded": excluded, "gaps": timeline.gaps}
