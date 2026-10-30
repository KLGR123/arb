"""Binary cross-venue paper valuation; no execution dependencies."""

from decimal import ROUND_CEILING
import re

from .negrisk import Book, D, assets, number, taker_fee, timestamp

STOP = set("will the a an be is of in on at to by for and or before after above below than over under yes no what how when price reach end this market".split())
STOP.update("january february march april may june july august september october november december monday tuesday wednesday thursday friday saturday sunday today tomorrow exceed hit more less between".split())


def terms(text):
    aliases = {"btc": "bitcoin", "eth": "ethereum"}
    return {aliases.get(x, x) for x in re.findall(r"[a-z0-9]+", text.lower())} - STOP


def normalize_poly(event, market):
    # ARB-7 compatibility: Gamma event -> market -> binary tokens; original IDs,
    # protocol marker and settlement text are preserved, no duplicate data store.
    return {"venue": "polymarket", "id": str(market["id"]),
            "event_id": str(event["id"]), "title": market["question"],
            "event_title": event.get("title", ""),
            "close_time": market.get("endDate"), "settlement_time": market.get("endDate"),
            "rules": market.get("description", ""),
            "resolution_source": market.get("resolutionSource", ""),
            "protocol": market.get("version", "v1"),
            "active": market.get("active") is True and not market.get("closed"),
            "tokens": assets(market)}


def normalize_kalshi(event, market, series):
    return {"venue": "kalshi", "id": market["ticker"],
            "event_id": event["event_ticker"],
            "title": " ".join(filter(None, (market.get("title"), market.get("yes_sub_title")))),
            "event_title": event.get("title", ""), "close_time": market.get("close_time"),
            "settlement_time": market.get("expected_expiration_time") or market.get("expiration_time"),
            "rules": "\n".join(filter(None, (market.get("rules_primary"), market.get("rules_secondary")))),
            "resolution_source": series.get("settlement_sources", []),
            "contract_terms_url": series.get("contract_terms_url"),
            "active": market.get("status") in ("active", "open"),
            "fee_type": series.get("fee_type"), "fee_multiplier": series.get("fee_multiplier")}


def candidates(poly, kalshi, limit=10, max_hours=48, min_similarity=.20):
    if limit < 1 or max_hours < 0 or not 0 <= min_similarity <= 1:
        raise ValueError("invalid candidate limits")
    found = []
    for p in poly:
        for k in kalshi:
            if not p["active"] or not k["active"]:
                continue
            try:
                gap = abs((timestamp(p["close_time"]) - timestamp(k["close_time"])).total_seconds())
            except (ValueError, TypeError):
                continue
            if gap > max_hours * 3600:
                continue
            pt, kt = terms(p["title"]), terms(k["title"])
            # Shared numbers alone cannot identify an underlying asset/event.
            shared = pt & kt
            if not any(x.isalpha() for x in shared):
                continue
            similarity = len(shared) / max(1, len(pt | kt))
            if similarity >= min_similarity:
                found.append({"poly_id": p["id"], "kalshi_id": k["id"],
                              "title_similarity": similarity, "close_gap_seconds": gap})
    return sorted(found, key=lambda x: (-x["title_similarity"], x["close_gap_seconds"],
                                       x["poly_id"], x["kalshi_id"]))[:limit]


def kalshi_books(raw, now, max_age=120):
    received = timestamp(raw["received_at"])
    age = (now - received).total_seconds()
    if age > max_age or age < -5:
        raise ValueError("stale or future Kalshi book")
    body = raw["raw"]
    fixed = body.get("orderbook_fp")
    bids = {}
    for side in ("yes", "no"):
        rows = fixed.get(side + "_dollars", []) if fixed is not None else body["orderbook"].get(side, [])
        levels = {}
        for price, size in rows or []:
            price, size = number(price), number(size)
            if fixed is None:
                price /= 100
            if not 0 < price < 1 or size <= 0:
                raise ValueError("invalid Kalshi book level")
            levels[price] = levels.get(price, D(0)) + size
        bids[side] = sorted(levels.items(), reverse=True)
    if bids["yes"] and bids["no"] and bids["yes"][0][0] + bids["no"][0][0] > 1:
        raise ValueError("crossed Kalshi orderbook")
    return {side: {"bids": bids[side], "asks": sorted((1-p, q) for p, q in bids[other]),
                   "time": received, "minimum": D(1)}
            for side, other in (("yes", "no"), ("no", "yes"))}


def poly_books(market, books, now, max_age=120):
    result = {}
    for side, asset in market["tokens"].items():
        parsed = Book.parse(asset, books[asset], now, max_age)
        result[side] = {"bids": parsed.bids, "asks": parsed.asks,
                        "time": parsed.as_of, "minimum": parsed.minimum}
    return result


def kalshi_rate(market):
    if market["fee_type"] not in ("quadratic", "quadratic_with_maker_fees"):
        raise ValueError("unsupported or missing Kalshi fee type")
    multiplier = number(market["fee_multiplier"])
    if not 0 <= multiplier <= 100:
        raise ValueError("invalid Kalshi fee multiplier")
    return D("0.07") * multiplier


def edge(pbook, kbook, prate, krate, quantity, kind, max_skew=10):
    """Top-level capacity only, no extrapolation through deeper price levels."""
    if abs((pbook["time"] - kbook["time"]).total_seconds()) > max_skew:
        raise ValueError("unsynchronized books")
    sides = {"buy_poly_sell_kalshi": ("asks", "bids"),
             "buy_kalshi_sell_poly": ("bids", "asks"),
             "buy_both_complementary": ("asks", "asks")}
    pside, kside = sides[kind]
    if not pbook[pside] or not kbook[kside]:
        raise ValueError("empty required book side")
    pp, pq = pbook[pside][0]
    kp, kq = kbook[kside][0]
    q = min(quantity, pq, kq).to_integral_value(rounding="ROUND_FLOOR")
    if q < max(D(1), pbook["minimum"], kbook["minimum"]):
        raise ValueError("insufficient top-level depth")
    pf = taker_fee(q, prate, pp)
    kf = (q * krate * kp * (1-kp)).quantize(D("0.01"), rounding=ROUND_CEILING)
    gross = {"buy_poly_sell_kalshi": kp - pp, "buy_kalshi_sell_poly": pp - kp,
             "buy_both_complementary": 1 - pp - kp}[kind] * q
    return {"kind": kind, "quantity": q, "poly_price": pp, "kalshi_price": kp,
            "gross_edge_per_share": gross/q, "poly_fee_usd": pf, "kalshi_fee_usd": kf,
            "net_edge_per_share": (gross-pf-kf)/q, "net_profit_usd": gross-pf-kf,
            "inventory_required": kind != "buy_both_complementary",
            "assumption": "equivalent binary payouts; top-level snapshot; pUSD approximately USD"}


def scan(snapshot, aligner, quantity=D(10), max_age=120, max_skew=10):
    if not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value() or max_age <= 0 or max_skew < 0:
        raise ValueError("positive integral quantity, age and nonnegative skew required")
    now = timestamp(snapshot["as_of"])
    pm = {x["id"]: x for x in snapshot["polymarket"]}
    km = {x["id"]: x for x in snapshot["kalshi"]}
    rows, errors = [], list(snapshot.get("errors", []))
    for candidate in snapshot["candidates"]:
        p, k = pm[candidate["poly_id"]], km[candidate["kalshi_id"]]
        row = {**candidate, "poly_title": p["title"], "kalshi_title": k["title"],
               "risk": "high", "risks": ["regional_restrictions_research_only", "model_requires_human_rule_review"],
               "quotes": [], "alignment": None, "price_differences": {}}
        try:
            lifecycle_ok = (p["active"] is True and k["active"] is True
                            and timestamp(p["close_time"]) > now and timestamp(k["close_time"]) > now)
        except (ValueError, TypeError, KeyError):
            lifecycle_ok = False
        if not lifecycle_ok:
            row["risks"].append("market_inactive_closed_or_close_time_unknown")
        coverage = snapshot.get("coverage", {})
        shortlist_ok = bool(candidates([p], [k], 1, coverage.get("max_close_gap_hours", 48),
                                       coverage.get("min_title_similarity", .20)))
        if not shortlist_ok:
            row["risks"].append("candidate_changed_since_discovery")
        try:
            record = aligner.compare(p, k)
            row["alignment"] = record["output"]
            row["rule_cache_key"] = record["key"]
        except (ValueError, OSError, KeyError):
            row["risks"].append("rule_comparison_unavailable")
            errors.append({"pair": [p["id"], k["id"]], "stage": "rules", "reason": "rule_comparison_unavailable"})
        alignment = row["alignment"] or {}
        relation = alignment.get("relation", "uncertain")
        rules_ok = (lifecycle_ok and shortlist_ok and relation in ("same", "inverse") and alignment.get("alignment", 0) >= .95
                    and not alignment.get("differences") and bool(p["rules"].strip()) and bool(k["rules"].strip()))
        row["settlement_equivalent"] = rules_ok
        if not rules_ok:
            row["risks"].append("rules_different_or_unverified")
            row["risk"] = "critical"
        try:
            settlement_gap = abs((timestamp(p["settlement_time"]) - timestamp(k["settlement_time"])).total_seconds())
            row["settlement_gap_seconds"] = settlement_gap
            if settlement_gap:
                row["risks"].append("settlement_time_gap")
        except (ValueError, TypeError):
            row["risks"].append("settlement_time_unknown")
        try:
            pb = poly_books(p, snapshot["poly_books"], now, max_age)
            kb = kalshi_books(snapshot["kalshi_books"][k["id"]], now, max_age)
            row["liquidity"] = {}
            for venue, books in (("polymarket", pb), ("kalshi", kb)):
                row["liquidity"][venue] = {}
                for outcome, values in books.items():
                    row["liquidity"][venue][outcome] = {
                        "as_of": values["time"].isoformat(), "minimum": values["minimum"],
                        **{side: {"levels": len(values[side]),
                                  "top_size": values[side][0][1] if values[side] else D(0),
                                  "total_size": sum((q for _, q in values[side]), D(0))}
                           for side in ("bids", "asks")}}
                    if any(not values[side] or values[side][0][1] < max(quantity, values["minimum"])
                           for side in ("bids", "asks")):
                        row["risks"].append("thin_or_missing_top_level_depth")
            for side in ("yes", "no"):
                for other in ("yes", "no"):
                    if pb[side]["asks"] and kb[other]["asks"]:
                        row["price_differences"][f"poly_{side}_minus_kalshi_{other}_ask"] = pb[side]["asks"][0][0]-kb[other]["asks"][0][0]
            prate, psource = snapshot["poly_fees"][p["id"]]
            prate = number(prate)
            if not 0 <= prate <= 1 or psource not in ("clob", "gamma", "fees_disabled"):
                raise ValueError("authoritative Polymarket fee unavailable")
            krate = kalshi_rate(k)
            row["fees"] = {"poly_rate": prate, "poly_source": psource,
                           "kalshi_rate": krate, "kalshi_source": "series_quadratic_schedule_estimate"}
            row["risks"].append("kalshi_fee_schedule_estimate")
            if rules_ok:
                for side, opposite in (("yes", "no"), ("no", "yes")):
                    same = side if relation == "same" else opposite
                    complement = opposite if relation == "same" else side
                    for other, kind in ((same, "buy_poly_sell_kalshi"), (same, "buy_kalshi_sell_poly"),
                                        (complement, "buy_both_complementary")):
                        try:
                            quote = edge(pb[side], kb[other], prate, krate, quantity, kind, max_skew)
                            row["quotes"].append({**quote, "poly_outcome": side, "kalshi_outcome": other})
                            if quote["quantity"] < quantity:
                                row["risks"].append("quantity_capped_by_top_level_depth")
                        except ValueError as exc:
                            row["risks"].append(str(exc))
                if not row["quotes"]:
                    errors.append({"pair": [p["id"], k["id"]], "stage": "valuation",
                                   "reason": "no_synchronized_liquid_quote"})
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            row["risks"].append("book_or_fee_data_unavailable")
            errors.append({"pair": [p["id"], k["id"]], "stage": "valuation", "reason": type(exc).__name__})
        row["risks"] = sorted(set(row["risks"]))
        rows.append(row)
    return {"schema_version": "cross-venue-v1", "as_of": snapshot["as_of"],
            "execution": "blocked", "compliance": snapshot.get("compliance", {}),
            "coverage": snapshot.get("coverage", {}), "pairs": rows, "errors": errors,
            "assumptions": ["Research only; never opens accounts or executes", "No guaranteed arbitrage",
                            "No funding, withdrawal, FX or tax costs modeled", "Kalshi restricts China; no eligibility inferred from proxy"],
            "summary": {"candidates": len(rows), "valued": sum(bool(x["quotes"]) for x in rows),
                        "settlement_equivalent": sum(x["settlement_equivalent"] for x in rows),
                        "positive_conditional_quotes": sum(q["net_edge_per_share"] > 0 for x in rows for q in x["quotes"])}}


def markdown(report):
    def clean(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["# Cross-venue paper research", "", f"As of: {report['as_of']}. Execution: blocked.",
             "All edges are conditional estimates, subject to human settlement-rule review.", "",
             "| Polymarket / Kalshi | Rule relation | Risk | Differences / risks |",
             "| --- | --- | --- | --- |"]
    for row in report["pairs"]:
        rule = row["alignment"] or {}
        differences = rule.get("differences", []) + row["risks"]
        lines.append(f"| {clean(row['poly_title'])} / {clean(row['kalshi_title'])} | {rule.get('relation', 'unavailable')} | {row['risk']} | {clean('; '.join(differences))} |")
    lines += ["", "Ask-price differences (Polymarket minus Kalshi, USD/share; not payout equivalence):", "",
              "| Pair | YES/YES | YES/NO | NO/YES | NO/NO |", "| --- | ---: | ---: | ---: | ---: |"]
    for row in report["pairs"]:
        values = [row["price_differences"].get(f"poly_{p}_minus_kalshi_{k}_ask", "unavailable")
                  for p, k in (("yes", "yes"), ("yes", "no"), ("no", "yes"), ("no", "no"))]
        lines.append(f"| {row['poly_id']} / {row['kalshi_id']} | " + " | ".join(map(str, values)) + " |")
    lines += ["", "| Pair | Paper scenario | Outcomes P/K | Shares | Net USD/share |",
              "| --- | --- | --- | ---: | ---: |"]
    for row in report["pairs"]:
        for quote in row["quotes"]:
            lines.append(f"| {row['poly_id']} / {row['kalshi_id']} | {quote['kind']} | {quote['poly_outcome']}/{quote['kalshi_outcome']} | {quote['quantity']} | {quote['net_edge_per_share']} |")
    lines += ["", f"Coverage: {report['coverage']}", f"Data errors: {len(report['errors'])}.",
              "Kalshi China restriction and venue eligibility remain separate from public data access."]
    return "\n".join(lines) + "\n"
