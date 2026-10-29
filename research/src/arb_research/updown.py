"""Causal, paper-only CEX proxy pricing for arithmetic TWAP Up/Down markets.

This is a consumer of public recordings, not a Chainlink feed or execution model.
Times are Unix seconds. CEX points are finalized one-second candle closes.
"""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import NormalDist

from .negrisk import taker_fee


@dataclass(frozen=True)
class PricePath:
    points: tuple
    max_gap: float = 2.0

    def __post_init__(self):
        if len(self.points) < 2 or not math.isfinite(self.max_gap) or self.max_gap <= 0:
            raise ValueError("insufficient CEX path or invalid gap limit")
        times = tuple(float(p[0]) for p in self.points)
        prices = tuple(Decimal(str(p[1])) for p in self.points)
        if (any(not math.isfinite(t) for t in times)
                or any(not p.is_finite() or p <= 0 for p in prices)
                or any(b <= a for a, b in zip(times, times[1:]))):
            raise ValueError("CEX points must be positive, finite and strictly ordered")
        object.__setattr__(self, "times", times)
        object.__setattr__(self, "prices", prices)

    def index(self, at):
        if not math.isfinite(at):
            raise ValueError("invalid observation time")
        i = bisect_right(self.times, at) - 1
        if i < 0 or at - self.times[i] > self.max_gap or at > self.times[-1]:
            raise ValueError("CEX path does not cover observation")
        return i

    def integral(self, start, end):
        """Left-held exact decimal time integral on [start, end); reject gaps."""
        if not start < end:
            raise ValueError("empty integration window")
        i = self.index(start)
        self.index(end)
        total, cursor = Decimal(0), start
        while cursor < end:
            stop = min(end, self.times[i + 1] if i + 1 < len(self.times) else end)
            if stop - self.times[i] > self.max_gap:
                raise ValueError("CEX gap in TWAP window")
            total += self.prices[i] * Decimal(str(stop - cursor))
            cursor = stop
            i += 1
        return total

    def twap(self, end, lookback=60):
        if lookback <= 0 or not math.isfinite(lookback):
            raise ValueError("invalid TWAP lookback")
        return self.integral(end - lookback, end) / Decimal(str(lookback))

    def sigma(self, at, lookback=900):
        """Trailing realized log-return volatility per sqrt(second); no future rows."""
        end = self.index(at)
        start = bisect_right(self.times, at - lookback) - 1
        if start < 0 or end - start < 30:
            raise ValueError("insufficient volatility warmup")
        times = self.times[start:end + 1]
        if any(b - a > self.max_gap for a, b in zip(times, times[1:])):
            raise ValueError("CEX gap in volatility window")
        prices = self.prices[start:end + 1]
        returns = [math.log(float(b / a)) for a, b in zip(prices, prices[1:])]
        return math.sqrt(sum(r * r for r in returns) / (times[-1] - times[0]))


def fair_up(path, start, end, observed_at, lookback=60, sigma=None):
    """Normal local-price diffusion integrated over the final TWAP window.

    Before that window Var(avg)=S²σ²*(end-L-t+L/3). Inside it,
    Var(avg)=S²σ²*(end-t)³/(3L²). Known partial TWAP is fixed.
    """
    if not (math.isfinite(lookback) and 0 < lookback <= end - start
            and start <= observed_at <= end):
        raise ValueError("unsupported pricing window")
    strike = path.twap(start, lookback)
    spot = float(path.prices[path.index(observed_at)])
    sigma = path.sigma(observed_at) if sigma is None else sigma
    if not math.isfinite(sigma) or sigma < 0:
        raise ValueError("invalid volatility")
    window_start = end - lookback
    if observed_at <= window_start:
        expected = spot
        variance_time = window_start - observed_at + lookback / 3
    else:
        known = path.integral(window_start, observed_at)
        remaining = end - observed_at
        expected = float((known + Decimal(str(spot)) * Decimal(str(remaining)))
                         / Decimal(str(lookback)))
        variance_time = remaining ** 3 / (3 * lookback ** 2)
    if observed_at == end:
        return float(path.twap(end, lookback) >= strike)
    sd = spot * sigma * math.sqrt(variance_time)
    return float(expected >= float(strike)) if sd == 0 else NormalDist().cdf((expected - float(strike)) / sd)


def parse_market(event):
    """Require per-market 5m TWAP60 rules and an explicitly resolved binary payout."""
    import json
    from datetime import datetime

    def array(value):
        return json.loads(value) if isinstance(value, str) else value

    markets = event.get("markets", [])
    if len(markets) != 1:
        raise ValueError("expected one market per crypto event")
    market = markets[0]
    config = market.get("cryptoMarketConfig", {})
    source = market.get("resolutionSource", "")
    if (config.get("asset") != "btc" or config.get("duration") != "5m"
            or config.get("twapEnabled") is not True or config.get("twapLookbackSeconds") != 60
            or source != "https://data.chain.link/streams/btc-usd-twap-60s-streams"
            or "TWAP" not in market.get("description", "")
            or "greater than or equal" not in market.get("description", "")):
        raise ValueError("unsupported or unverified market rules")
    outcomes, payouts = array(market["outcomes"]), array(market["outcomePrices"])
    tokens = array(market["clobTokenIds"])
    if (market.get("closed") is not True or market.get("umaResolutionStatus") != "resolved"
            or outcomes != ["Up", "Down"] or len(tokens) != 2
            or [Decimal(str(p)) for p in payouts] not in ([Decimal(1), Decimal(0)], [Decimal(0), Decimal(1)])):
        raise ValueError("market is not a resolved Up/Down binary")
    start = datetime.fromisoformat(market["eventStartTime"].replace("Z", "+00:00")).timestamp()
    end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00")).timestamp()
    if end - start != 300 or market["slug"] != f"btc-updown-5m-{int(start)}":
        raise ValueError("market window mismatch")
    fee = market.get("feeSchedule", {})
    if market.get("feesEnabled") is not True or fee.get("exponent") != 1 or fee.get("takerOnly") is not True:
        raise ValueError("unsupported fee schedule")
    rate = float(fee["rate"])
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("invalid fee rate")
    return {"id": str(market["id"]), "slug": market["slug"], "start": start, "end": end,
            "up_token": tokens[0], "actual_up": int(Decimal(str(payouts[0]))), "fee_rate": rate}


def distribution(values):
    values = sorted(values)
    if not values:
        return {"n": 0, "mean": None, "p05": None, "p50": None, "p95": None}
    def quantile(q):
        x = (len(values) - 1) * q
        lo, hi = math.floor(x), math.ceil(x)
        return values[lo] + (values[hi] - values[lo]) * (x - lo)
    return {"n": len(values), "mean": sum(values) / len(values),
            **{name: quantile(q) for name, q in (("p05", .05), ("p50", .5), ("p95", .95))}}


def summarize(rows):
    return {"samples": len(rows), "model_minus_market": distribution([r["error"] for r in rows]),
            "absolute_error": distribution([abs(r["error"]) for r in rows]),
            "probability_staleness": distribution([r["probability_staleness"] for r in rows]),
            "brier_model": distribution([r["brier_model"] for r in rows]),
            "brier_market": distribution([r["brier_market"] for r in rows]),
            "net_edge_usd_per_share": distribution([r["net_edge"] for r in rows]),
            "positive_net_edge_samples": sum(r["net_edge"] > 0 for r in rows)}


def analyze(recording, delays=(0, .05, .25, 1, 2)):
    """Replay fixed recordings. Minute references are available only at bucket end.

    Subsecond latencies are quantized to observed one-second data. Quotes are
    hypothetical reference prices, never executable bids/asks or fill estimates.
    """
    if (not delays or 0 not in delays or any(not math.isfinite(d) or d < 0 or d > 10 for d in delays)
            or len(set(delays)) != len(delays)):
        raise ValueError("invalid delay scenarios: zero baseline is required")
    path = PricePath(tuple(tuple(p) for p in recording["cex"]["points"]))
    markets, settlements, samples, errors = [], [], [], []
    selection = recording["selection"]
    count, cohort_start = selection["count"], selection["start"]
    if (type(count) is not int or not 1 <= count <= 100 or type(cohort_start) is not int
            or cohort_start % 300 or selection["duration_seconds"] != 300):
        raise ValueError("invalid declared cohort")
    if len(recording["markets"]) != count:
        errors.append({"reason": "recorded market count differs from declared cohort"})
    seen = set()
    for cohort_index, item in enumerate(recording["markets"]):
        try:
            market = parse_market(item["event"])
            if market["id"] in seen:
                raise ValueError("duplicate market")
            if market["start"] != cohort_start + cohort_index * 300:
                raise ValueError("market is outside declared consecutive cohort")
            seen.add(market["id"])
            open_price, close_price = path.twap(market["start"]), path.twap(market["end"])
            predicted = int(close_price >= open_price)
            settlements.append({"market_id": market["id"], "slug": market["slug"],
                                "cex_open_twap": str(open_price), "cex_close_twap": str(close_price),
                                "cex_up": predicted, "actual_up": market["actual_up"],
                                "matches": predicted == market["actual_up"]})
            history = item["history"]
            quotes = []
            for point in history:
                width, price, timestamp = point["resolution_seconds"], float(point["price"]), float(point["timestamp"])
                if not math.isfinite(price) or not 0 <= price <= 1 or not math.isfinite(timestamp):
                    raise ValueError("invalid history observation")
                # Settlement points and wider buckets are never used as trading observations.
                if width == 60 and market["start"] <= timestamp and timestamp + width < market["end"]:
                    quotes.append((timestamp + width, price))
            expected_times = [market["start"] + i * 60 for i in range(1, 5)]
            if [q[0] for q in quotes] != expected_times:
                raise ValueError("four ordered minute reference buckets are required")
            markets.append(market)
            quote_times = [q[0] for q in quotes]
            for decision_at, reference in quotes:
                fresh_probability = fair_up(path, market["start"], market["end"], decision_at)
                for data_delay in delays:
                    observed_at = math.floor(decision_at - data_delay)
                    if observed_at < market["start"]:
                        continue
                    sigma = path.sigma(observed_at)
                    probability = fair_up(path, market["start"], market["end"], observed_at, sigma=sigma)
                    buy_up = probability >= reference
                    for order_delay in delays:
                        arrival = decision_at + order_delay
                        if arrival >= market["end"]:
                            continue
                        # Do not advance into a future quote bucket, interpolate, or cross expiry.
                        j = bisect_right(quote_times, arrival) - 1
                        execution_reference = quotes[j][1]
                        arrival_probability = fair_up(path, market["start"], market["end"], math.floor(arrival))
                        price = execution_reference if buy_up else 1 - execution_reference
                        value = arrival_probability if buy_up else 1 - arrival_probability
                        fee = float(taker_fee(1, market["fee_rate"], price))
                        samples.append({"market_id": market["id"], "decision_at": decision_at,
                                        "remaining_seconds": market["end"] - decision_at,
                                        "sigma_per_sqrt_second": sigma,
                                        "data_delay_seconds": data_delay, "order_delay_seconds": order_delay,
                                        "fair_up": probability, "market_up": reference,
                                        "market_source_at": decision_at - 60,
                                        "probability_staleness": probability - fresh_probability,
                                        "side": "Up" if buy_up else "Down", "error": probability - reference,
                                        "brier_model": (probability - market["actual_up"]) ** 2,
                                        "brier_market": (reference - market["actual_up"]) ** 2,
                                        "arrival_fair_up": arrival_probability, "reference_price": price,
                                        "fee_usd_per_share": fee, "net_edge": value - price - fee})
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            errors.append({"slug": item.get("event", {}).get("slug"), "reason": str(exc)})
    baseline = [s for s in samples if s["data_delay_seconds"] == 0 and s["order_delay_seconds"] == 0]
    n, matches = len(settlements), sum(s["matches"] for s in settlements)
    # Wilson interval; the consecutive sample is not independent/random.
    interval = None
    if n:
        z, p = 1.959963984540054, matches / n
        centre = (p + z*z/(2*n)) / (1 + z*z/n)
        half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
        interval = [centre - half, centre + half]
    buckets = {}
    for label, predicate in {
        "remaining_0_60s": lambda r: 0 < r["remaining_seconds"] <= 60,
        "remaining_60_180s": lambda r: 60 < r["remaining_seconds"] <= 180,
        "remaining_180_300s": lambda r: 180 < r["remaining_seconds"] <= 300,
        "sigma_below_0.00005": lambda r: r["sigma_per_sqrt_second"] < .00005,
        "sigma_0.00005_0.00010": lambda r: .00005 <= r["sigma_per_sqrt_second"] < .00010,
        "sigma_at_least_0.00010": lambda r: r["sigma_per_sqrt_second"] >= .00010,
    }.items():
        buckets[label] = summarize([r for r in baseline if predicate(r)])
    return {"schema_version": 1, "study": "btc-5m-cex-twap-proxy", "recorded_at": recording["recorded_at"],
            "assumptions": {"cex_is_chainlink_proxy": True, "cex_quote_currency": "USDT assumed USD",
                            "collateral": "pUSD assumed USD", "cex_step_seconds": 1,
                            "market_reference_seconds": 60, "market_reference_available": "timestamp + resolution_seconds",
                            "latency_quantization": "floor decision-data_delay and arrival to observed seconds",
                            "down_reference": "1 - Up reference (no observed Down ask)",
                            "edge_is_executable": False, "spread_depth_slippage_costs_included": False,
                            "edge_interpretation": "model value at arrival minus minute reference minus one-share taker fee",
                            "confidence_interval": "Wilson descriptive only; consecutive windows are dependent",
                            "volatility_lookback_seconds": 900, "model": "zero-drift local arithmetic Brownian TWAP"},
            "status": "complete" if n >= 50 and len(markets) == len(recording["markets"]) and not errors else "incomplete",
            "execution": "disabled", "compliance": recording.get("compliance"),
            "coverage": {"declared_markets": count, "recorded_markets": len(recording["markets"]),
                         "priced_markets": len(markets), "expected_baseline_points": count * 4,
                         "baseline_points": len(baseline)},
            "settlement": {"markets": n, "matches": matches, "agreement": matches/n if n else None,
                           "wilson_95": interval, "rows": settlements},
            "baseline": summarize(baseline), "buckets": buckets,
            "latency": [{"data_delay_seconds": d, "order_delay_seconds": o,
                         **summarize([s for s in samples if s["data_delay_seconds"] == d and s["order_delay_seconds"] == o])}
                        for d in delays for o in delays], "errors": errors, "samples": baseline}
