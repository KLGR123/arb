"""Paired scoring, market-cluster bootstrap, and collateralized paper payoffs."""

import math
import random
from statistics import mean

from .context import probability


def loss(p: float, y: int) -> float:
    p = min(1 - 1e-15, max(1e-15, p))
    return -(y * math.log(p) + (1 - y) * math.log1p(-p))


def quantile(values: list[float], p: float) -> float:
    position = (len(values) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def paper_bet(p, q, y, *, fee_bps=20, bankroll=100, kelly_fraction=0.25, cap=0.1):
    """s is signed YES shares per dollar of risk budget, fully collateralized.

    Buying NO is economically equivalent to shorting YES with collateral 1-q.
    Kelly allocates a fraction of risk budget to the favorable binary outcome.
    """
    probability(p)
    probability(q)
    if not 0 < q < 1 or y not in (0, 1):
        raise ValueError("Paper bet requires interior quote and binary truth")
    if not all(math.isfinite(v) for v in (fee_bps, bankroll, kelly_fraction, cap)):
        raise ValueError("Nonfinite betting setting")
    if not 0 <= fee_bps <= 10000 or bankroll <= 0 or not 0 <= kelly_fraction <= 1 or not 0 < cap <= 1:
        raise ValueError("Invalid fee, budget, Kelly fraction or cap")
    direction = 1 if p >= q else -1
    price = q if direction == 1 else 1 - q
    edge = abs(p - q)
    rate = fee_bps / 10000
    proper = 2 * (p - q)
    # Reserve fees as well as the full cost of buying YES/NO.
    proper = direction * min(abs(proper), 1 / (price * (1 + rate)))
    fraction = min(cap, kelly_fraction * edge / (1 - price))
    kelly = direction * fraction / (price * (1 + rate))
    result = {}
    for name, shares in (("proper", proper), ("fractional_kelly", kelly)):
        fees = bankroll * abs(shares) * price * rate
        result[name] = {
            "shares": shares * bankroll,
            "collateral": bankroll * abs(shares) * price,
            "fees": fees, "net_pnl": bankroll * shares * (y - q) - fees,
        }
    return result


def evaluate(rows: list[dict], *, bootstrap=2000, seed=11, fee_bps=20,
             bankroll=100, kelly_fraction=0.25, cap=0.1) -> dict:
    if not rows or bootstrap < 100:
        raise ValueError("Need nonempty rows and at least 100 bootstrap resamples")
    if len({r["market_id"] for r in rows}) != len(rows):
        raise ValueError("One forecast horizon per market is required")
    for row in rows:
        probability(row["p"])
        probability(row["q"])
        if type(row["y"]) is not int or row["y"] not in (0, 1):
            raise ValueError("Truth must be binary")
    model_brier = [(r["p"] - r["y"]) ** 2 for r in rows]
    market_brier = [(r["q"] - r["y"]) ** 2 for r in rows]
    deltas = [q - p for q, p in zip(market_brier, model_brier)]
    rng = random.Random(seed)
    # Resample paired errors, not independently sampled model and market scores.
    groups = {}
    for row, delta in zip(rows, deltas):
        groups.setdefault(row.get("cluster_id", row["market_id"]), []).append(delta)
    keys = list(groups)
    draws = sorted(mean(d for key in rng.choices(keys, k=len(keys)) for d in groups[key])
                   for _ in range(bootstrap))
    calibration = []
    for i in range(10):
        members = [r for r in rows if min(int(r["p"] * 10), 9) == i]
        calibration.append({"lower": i / 10, "upper": (i + 1) / 10,
                            "count": len(members),
                            "mean_p": mean(r["p"] for r in members) if members else None,
                            "observed_frequency": mean(r["y"] for r in members) if members else None})
    bets = [paper_bet(r["p"], r["q"], r["y"], fee_bps=fee_bps, bankroll=bankroll,
                      kelly_fraction=kelly_fraction, cap=cap) for r in rows]
    return {
        "n": len(rows), "brier": mean(model_brier), "market_brier": mean(market_brier),
        "log_loss": mean(loss(r["p"], r["y"]) for r in rows),
        "market_log_loss": mean(loss(r["q"], r["y"]) for r in rows),
        "brier_advantage": mean(deltas),
        "brier_advantage_ci95": [quantile(draws, 0.025), quantile(draws, 0.975)],
        "bootstrap": {"resamples": bootstrap, "seed": seed,
                      "unit": "expiry_block" if any("cluster_id" in r for r in rows) else "paired_market",
                      **({"clusters": len(keys)} if any("cluster_id" in r for r in rows) else {})},
        "calibration": calibration,
        "paper": {
            "currency": "pUSD≈USD", "budget_per_market": bankroll,
            "fee_bps_on_entry_notional": fee_bps, "kelly_fraction": kelly_fraction,
            "kelly_cap": cap, "allocation": "independent fixed budgets; no compounding",
            "total_budget": len(rows) * bankroll,
            **{name: {k: sum(b[name][k] for b in bets)
                      for k in ("collateral", "fees", "net_pnl")}
               for name in ("proper", "fractional_kelly")},
        },
    }
