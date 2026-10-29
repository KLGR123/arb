"""Point-in-time resolution/large-trader research. No execution entry points."""

from collections import defaultdict
from datetime import datetime, timezone
import math
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq

VERSION = "resolution-whales.features.v1"
DISPUTED = {"challenged", "disputed", "arbitration"}
TIME = pa.timestamp("us", tz="UTC")
SCHEMA = pa.schema([
    ("schema_version", pa.string()), ("market_id", pa.string()),
    ("condition_id", pa.string()), ("as_of", TIME), ("available_at", TIME),
    ("category", pa.string()), ("rule_subjective", pa.bool_()),
    ("rule_has_source_url", pa.bool_()), ("rule_chars", pa.int64()),
    ("resolution_status", pa.string()), ("was_disputed", pa.bool_()),
    ("risk_score", pa.float64()), ("peer_resolved_count", pa.int64()),
    ("peer_dispute_rate", pa.float64()), ("top_n", pa.int64()),
    ("observed_holder_count", pa.int64()), ("observed_gross_shares", pa.float64()),
    ("top_n_share_of_observed_holdings", pa.float64()),
    ("observed_hhi", pa.float64()), ("holders_walk_exhausted", pa.bool_()),
    ("flow_window_seconds", pa.int64()), ("large_trade_threshold_usd", pa.float64()),
    ("large_trade_count", pa.int64()), ("large_trade_buy_usd", pa.float64()),
    ("large_trade_sell_usd", pa.float64()), ("large_trade_net_usd", pa.float64()),
    ("large_trade_first_buy_at", TIME), ("large_trade_last_sell_at", TIME),
    ("trades_walk_exhausted", pa.bool_()),
    ("whale_cohort_available_at", TIME), ("whale_cohort_size", pa.int64()),
    ("whale_trade_count", pa.int64()), ("whale_net_flow_usd", pa.float64()),
], metadata={b"contract": VERSION.encode(), b"availability": b"local receipt time; no historical backfill"})


def instant(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) or str(value).isdigit():
        result = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("UTC offset required")
    return result.astimezone(timezone.utc)


def finite(value, minimum=0, maximum=math.inf):
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError("invalid numeric observation")
    return result


def rules(text):
    text = text or ""
    return {"rule_subjective": bool(re.search(r"\b(discretion|consensus|credible|substantial|ambiguous)\b", text, re.I)),
            "rule_has_source_url": bool(re.search(r"https?://", text)), "rule_chars": len(text)}


def validate_sample(s):
    """Validate the normalized boundary, including identity and source event time."""
    at = instant(s["observed_at"])
    m = s["market"]
    if not m["market_id"] or not m["condition_id"] or not m["tokens"]:
        raise ValueError("missing market identity")
    tokens = {str(t["token_id"]): t["outcome_index"] for t in m["tokens"]}
    r = s.get("resolution")
    lifecycle = s.get("uma_lifecycle")
    if lifecycle:
        ts = [instant(lifecycle[k]) for k in ("requestTimestamp", "proposalTimestamp", "disputeTimestamp",
              "settlementTimestamp") if lifecycle.get(k) is not None]
        if ts != sorted(ts) or any(t > at for t in ts):
            raise ValueError("invalid UMA lifecycle times")
    if r:
        if r["condition_id"] != m["condition_id"] or type(r["was_disputed"]) is not bool:
            raise ValueError("invalid resolution identity/flag")
        for key in ("last_update_timestamp", "resolved_at", "oracle_finalized_at"):
            if r.get(key) and instant(r[key]) > at:
                raise ValueError("resolution event is in the future")
        if r.get("payouts") is not None:
            payouts = [finite(p, maximum=1) for p in r["payouts"]]
            if len(payouts) != len(tokens) or not math.isclose(sum(payouts), 1):
                raise ValueError("invalid payout vector")
    for group in s.get("holders") or []:
        if group["token_id"] not in tokens:
            raise ValueError("unexpected holder token")
        wallets = set()
        for h in group["holders"]:
            if not h["proxy_wallet"] or h["proxy_wallet"].lower() in wallets:
                raise ValueError("duplicate/missing holder")
            wallets.add(h["proxy_wallet"].lower())
            finite(h["amount"])
    for t in s.get("trades") or []:
        if (t["condition_id"] != m["condition_id"] or t["token_id"] not in tokens
                or t["outcome_index"] != tokens[t["token_id"]]
                or t["side"] not in {"BUY", "SELL"} or not t["proxy_wallet"]):
            raise ValueError("unexpected trade identity/side")
        finite(t["price"], maximum=1)
        finite(t["size"])
        if instant(t["timestamp"]) > at:
            raise ValueError("trade event is in the future")
    for token, points in (s.get("prices") or {}).items():
        if token not in tokens:
            raise ValueError("unexpected price token")
        for p in points:
            finite(p["price"], maximum=1)
            finite(p["resolution_seconds"])
            if instant(p["timestamp"]) > at:
                raise ValueError("price event is in the future")
    return s


def latest_samples(samples, cutoff):
    selected = {}
    for s in samples:
        validate_sample(s)
        at = instant(s["observed_at"])
        if at <= cutoff:
            mid = s["market"]["market_id"]
            if mid in selected and instant(selected[mid]["observed_at"]) == at and selected[mid] != s:
                raise ValueError("conflicting snapshots at identical receipt time")
            if mid not in selected or instant(selected[mid]["observed_at"]) < at:
                selected[mid] = s
    return selected


def holder_stats(s, top_n):
    if s.get("holders") is None:
        return dict(observed_holder_count=None, observed_gross_shares=None,
                    top_n_share_of_observed_holdings=None, observed_hhi=None)
    balances = defaultdict(float)
    for group in s["holders"]:
        for h in group["holders"]:
            balances[h["proxy_wallet"].lower()] += finite(h["amount"])
    amounts = sorted((q for q in balances.values() if q > 0), reverse=True)
    total = sum(amounts)
    return dict(observed_holder_count=len(amounts), observed_gross_shares=total,
                top_n_share_of_observed_holdings=sum(amounts[:top_n]) / total if total else None,
                observed_hhi=sum((q / total) ** 2 for q in amounts) if total else None)


def whale_wallets(s, top_n):
    balances = defaultdict(float)
    for group in s.get("holders") or []:
        for h in group["holders"]:
            balances[h["proxy_wallet"].lower()] += h["amount"]
    return sorted(((w, q) for w, q in balances.items() if q > 0), key=lambda x: (-x[1], x[0]))[:top_n]


def large_trades(s, cutoff, window_seconds, threshold):
    # Threshold is fixed before observing trades. Never select historical whales
    # using today's winners/holders, and never treat a truncated feed as full flow.
    if s.get("trades") is None:
        return None
    result = []
    for t in s["trades"]:
        age = (cutoff - instant(t["timestamp"])).total_seconds()
        if 0 <= age < window_seconds and finite(t["size"]) * finite(t["price"], maximum=1) >= threshold:
            result.append(t)
    return result


def feature_rows(samples, cutoffs, *, top_n=10, window_seconds=86400, threshold_usd=1000):
    if top_n < 1 or window_seconds < 1 or finite(threshold_usd) <= 0:
        raise ValueError("positive feature parameters required")
    rows = []
    for cutoff in sorted({instant(c) for c in cutoffs}):
        selected = latest_samples(samples, cutoff)
        for mid, s in sorted(selected.items()):
            m, r = s["market"], s.get("resolution")
            category = m.get("category") or "unknown"
            peers = [p["resolution"] for pid, p in selected.items() if pid != mid
                     and (p["market"].get("category") or "unknown") == category
                     and p.get("resolution") and p["resolution"]["status"] == "resolved"]
            status = r["status"] if r else None
            rule = rules(m.get("rules"))
            risk = None if not r else min(1., .7 * (status in DISPUTED)
                + .2 * bool(r.get("extended_review")) + .1 * bool(r.get("new_version_q")))
            trades = large_trades(s, cutoff, window_seconds, threshold_usd)
            buy = None if trades is None else sum(t["size"] * t["price"] for t in trades if t["side"] == "BUY")
            sell = None if trades is None else sum(t["size"] * t["price"] for t in trades if t["side"] == "SELL")
            buys = [instant(t["timestamp"]) for t in trades or [] if t["side"] == "BUY"]
            sells = [instant(t["timestamp"]) for t in trades or [] if t["side"] == "SELL"]
            # Freeze the address cohort before the flow window begins. Today's
            # holders must never choose yesterday's allegedly predictive flow.
            prior = [x for x in samples if x["market"]["market_id"] == mid
                     and x.get("holders") is not None
                     and instant(x["observed_at"]).timestamp() <= cutoff.timestamp() - window_seconds]
            base = max(prior, key=lambda x: instant(x["observed_at"])) if prior else None
            cohort = {w for w, _ in whale_wallets(base, top_n)} if base else None
            whale_trades = None if cohort is None or s.get("trades") is None else [t for t in s["trades"]
                if t["proxy_wallet"].lower() in cohort
                and 0 <= (cutoff - instant(t["timestamp"])).total_seconds() < window_seconds]
            rows.append(dict(schema_version=VERSION, market_id=mid, condition_id=m["condition_id"],
                as_of=cutoff, available_at=instant(s["observed_at"]), category=category, **rule,
                resolution_status=status, was_disputed=None if not r else r["was_disputed"],
                risk_score=risk, peer_resolved_count=len(peers),
                peer_dispute_rate=sum(p["was_disputed"] for p in peers) / len(peers) if peers else None,
                top_n=top_n, **holder_stats(s, top_n),
                holders_walk_exhausted=s.get("holders_walk_exhausted", False),
                flow_window_seconds=window_seconds, large_trade_threshold_usd=float(threshold_usd),
                large_trade_count=None if trades is None else len(trades),
                large_trade_buy_usd=buy, large_trade_sell_usd=sell,
                large_trade_net_usd=None if buy is None else buy - sell,
                large_trade_first_buy_at=min(buys) if buys else None,
                large_trade_last_sell_at=max(sells) if sells else None,
                trades_walk_exhausted=s.get("trades_walk_exhausted", False),
                whale_cohort_available_at=instant(base["observed_at"]) if base else None,
                whale_cohort_size=len(cohort) if cohort is not None else None,
                whale_trade_count=len(whale_trades) if whale_trades is not None else None,
                whale_net_flow_usd=sum(t["size"] * t["price"] * (1 if t["side"] == "BUY" else -1)
                                      for t in whale_trades) if whale_trades is not None else None))
    return rows


def write_features(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_table(table, path, compression="zstd")
    return table


def read_features(path, *, as_of):
    """Safe reader for pandas/ARB-12 or an ARB-14/Nautilus custom-data adapter."""
    table = pq.read_table(path)
    if not table.schema.equals(SCHEMA, check_metadata=True):
        raise ValueError("unsupported feature schema")
    cutoff = instant(as_of)
    rows = table.to_pylist()
    if any(r["available_at"] > r["as_of"] for r in rows):
        raise ValueError("feature timestamp precedes availability")
    latest = {}
    for r in sorted(rows, key=lambda r: r["as_of"]):
        if r["as_of"] <= cutoff and r["available_at"] <= cutoff:
            latest[r["market_id"]] = r
    return list(latest.values())


def report(samples, *, as_of, threshold_usd=1000):
    """Descriptive outcomes stay separate from the predictive Parquet contract."""
    cutoff = instant(as_of)
    selected = latest_samples(samples, cutoff)
    groups = defaultdict(list)
    detail = []
    for mid, s in sorted(selected.items()):
        r = s.get("resolution")
        history = sorted((x for x in samples if x["market"]["market_id"] == mid
                          and instant(x["observed_at"]) <= cutoff), key=lambda x: instant(x["observed_at"]))
        # Only observed disputed states establish a start. A terminal was_disputed
        # flag cannot reconstruct a past dispute interval.
        starts = [instant(x["resolution"]["last_update_timestamp"]) for x in history
                  if x.get("resolution") and x["resolution"]["status"] in DISPUTED
                  and x["resolution"].get("last_update_timestamp")]
        end_value = (r.get("resolved_at") or r.get("oracle_finalized_at")) if r else None
        end = instant(end_value) if end_value else None
        start = min(starts) if starts else None
        duration = (end - start).total_seconds() if start and end and end >= start else None
        lifecycle = s.get("uma_lifecycle") or {}
        oracle_start = instant(lifecycle["disputeTimestamp"]) if lifecycle.get("disputeTimestamp") else None
        oracle_end = instant(lifecycle["settlementTimestamp"]) if lifecycle.get("settlementTimestamp") else None
        oracle_duration = (oracle_end - oracle_start).total_seconds() if oracle_start and oracle_end else None
        price_start, price_end = (oracle_start, oracle_end) if oracle_duration is not None else (start, end)
        ranges = []
        for points in (s.get("prices") or {}).values():
            ps = [p["price"] for p in points if price_start and price_end
                  and price_start <= instant(p["timestamp"])
                  and p["timestamp"] + p["resolution_seconds"] < price_end.timestamp()
                  and p["resolution_seconds"] > 0]
            if len(ps) > 1:
                ranges.append(max(ps) - min(ps))
        trades = large_trades(s, cutoff, 3 * 366 * 86400, threshold_usd)
        buys = [t for t in trades or [] if t["side"] == "BUY"]
        payouts = r.get("payouts") if r and r["status"] == "resolved" else None
        eligible = [t for t in buys if payouts and end and instant(t["timestamp"]) < end
                    and t["outcome_index"] < len(payouts)]
        cash = sum(t["price"] * t["size"] for t in eligible)
        trades_known = s.get("trades") is not None
        labels_known = trades_known and payouts is not None and end is not None
        item = dict(market_id=mid, question=s["market"].get("question"),
                    category=s["market"].get("category") or "unknown",
                    status=r["status"] if r else None, was_disputed=r["was_disputed"] if r else None,
                    split_50_50=None if not payouts else payouts == [.5, .5],
                    payout_basis=r.get("payout_basis") if r else None,
                    observed_dispute_to_resolution_seconds=duration,
                    oracle_dispute_to_settlement_seconds=oracle_duration,
                    dispute_sampled_price_range=max(ranges) if ranges else None,
                    trades_available=trades_known,
                    trades_walk_exhausted=s.get("trades_walk_exhausted", False),
                    holders_available=s.get("holders") is not None,
                    holders_walk_exhausted=s.get("holders_walk_exhausted", False),
                    prices_walk_exhausted=s.get("prices_walk_exhausted", {}),
                    large_buy_count=len(buys) if trades_known else None,
                    labeled_large_buy_count=len(eligible) if labels_known else None,
                    large_buy_payout_minus_cost_usd=sum(t["size"] * (payouts[t["outcome_index"]] - t["price"])
                                                       for t in eligible) if eligible else None,
                    large_buy_cost_usd=cash if eligible else None,
                    **holder_stats(s, 10), **rules(s["market"].get("rules")))
        item["whales_retrospective"] = []
        for wallet, shares in whale_wallets(s, 10):
            ts = [t for t in s.get("trades") or [] if t["proxy_wallet"].lower() == wallet]
            wb, ws = [t for t in ts if t["side"] == "BUY"], [t for t in ts if t["side"] == "SELL"]
            labeled = [t for t in wb if payouts and end and instant(t["timestamp"]) < end]
            item["whales_retrospective"].append(dict(wallet=wallet, observed_gross_shares=shares,
                sampled_trade_count=len(ts) if trades_known else None,
                sampled_buy_usd=sum(t["price"] * t["size"] for t in wb) if trades_known else None,
                sampled_sell_usd=sum(t["price"] * t["size"] for t in ws) if trades_known else None,
                first_sampled_buy_at=min((instant(t["timestamp"]).isoformat() for t in wb), default=None),
                last_sampled_sell_at=max((instant(t["timestamp"]).isoformat() for t in ws), default=None),
                labeled_buy_count=len(labeled) if labels_known else None,
                buy_payout_minus_cost_usd=sum(t["size"] * (payouts[t["outcome_index"]] - t["price"])
                                              for t in labeled) if labeled else None))
        detail.append(item)
        groups[("category", item["category"])].append(item)
        groups[("rule_subjective", str(item["rule_subjective"]))].append(item)
        groups[("rule_has_source_url", str(item["rule_has_source_url"]))].append(item)
    grouped = []
    for (dimension, value), items in sorted(groups.items()):
        known = [i for i in items if i["was_disputed"] is not None]
        resolved = [i for i in items if i["status"] == "resolved"]
        payout_known = [i for i in resolved if i["split_50_50"] is not None]
        durations = [i["observed_dispute_to_resolution_seconds"] for i in items
                     if i["observed_dispute_to_resolution_seconds"] is not None]
        oracle_durations = [i["oracle_dispute_to_settlement_seconds"] for i in items
                            if i["oracle_dispute_to_settlement_seconds"] is not None]
        grouped.append(dict(dimension=dimension, value=value, markets=len(items),
            resolution_observed=len(known), resolved=len(resolved),
            dispute_rate=sum(i["was_disputed"] for i in known) / len(known) if known else None,
            payout_known_count=len(payout_known), payout_unknown_count=len(resolved) - len(payout_known),
            split_50_50_count=sum(i["split_50_50"] is True for i in resolved),
            split_50_50_rate=sum(i["split_50_50"] for i in payout_known) / len(payout_known) if payout_known else None,
            duration_observed_count=len(durations),
            oracle_duration_count=len(oracle_durations),
            mean_oracle_dispute_seconds=sum(oracle_durations) / len(oracle_durations) if oracle_durations else None,
            mean_observed_dispute_seconds=sum(durations) / len(durations) if durations else None))
    return dict(as_of=cutoff.isoformat(), market_count=len(detail), groups=grouped, markets=detail,
        limitations=["Bounded selection; no population or causal inference.",
                    "Concentration denominator is observed gross holdings, not market-wide supply.",
                    "Large trades use fixed USD threshold, taker rows only; buys/sells are not full position entry/exit.",
                    "Payout-minus-cost is a per-buy hold-to-settlement counterfactual, excluding sells and fees; not wallet PnL.",
                    "Whale address detail selects current sampled holders retrospectively; never a historical model input.",
                    "Dispute duration/range requires an identity-verified UMA lifecycle or recorded dispute state; never inferred from final flag.",
                    "Historical observations become usable only at local receipt time."])
