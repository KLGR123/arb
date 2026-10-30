"""Bounded negRisk census with nonpositive valuations and explicit exclusions.

Paper arithmetic only. Retains ARB-10 output compatibility and shares its fee,
book and depth engine with ARB-17. No account or execution client is constructed.
"""

import argparse
import copy
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path

from .negrisk import Book, Costs, D, Leg, ONE, ZERO, dumps, number, opportunity, scan, timestamp
from .negrisk_cli import ReadOnlyAPI, collect


def now():
    return datetime.now(timezone.utc).isoformat()


def discover(api, max_events, max_pages, max_markets):
    """Record every visited event's selection reason, including capped groups."""
    selected, census, seen, seen_pages = [], [], set(), set()
    exhausted, pages_read = False, 0
    for page_index in range(max_pages):
        page = api.get("gamma", "/events", {"active": "true", "closed": "false",
            "limit": 100, "offset": page_index * 100, "order": "volume24hr", "ascending": "false"})
        pages_read += 1
        if not isinstance(page, list):
            raise ValueError("expected Gamma event array")
        ids = tuple(str(e["id"]) for e in page)
        if ids and ids in seen_pages:
            raise ValueError("repeated Gamma page")
        seen_pages.add(ids)
        for e in page:
            eid = str(e["id"])
            if eid in seen:
                continue
            seen.add(eid)
            count = len(e.get("markets", []))
            reason = ("not_active_negrisk" if e.get("negRisk") is not True or
                      e.get("active") is not True or e.get("closed") else
                      "market_count_outside_bounds" if not 2 <= count <= max_markets else
                      "event_limit" if len(selected) >= max_events else "selected")
            census.append(dict(event_id=eid, title=e.get("title", ""), market_count=count, reason=reason))
            if reason == "selected":
                selected.append(eid)
        if len(page) < 100:
            exhausted = True
            break
        if len(selected) >= max_events:
            break
    return selected, dict(order="volume24hr_desc", page_size=100, max_pages=max_pages,
        pages_read=pages_read, max_events=max_events, max_markets_per_event=max_markets,
        discovery_exhausted=exhausted, events_seen=len(census), selected_events=len(selected), census=census)


def collect_deep(api, max_events=30, max_pages=5, max_markets=12):
    result = dict(schema_version="negrisk-deep.raw.v1", started_at=now(), snapshots=[], errors=[],
                  network_mode="scoped_proxy" if os.environ.get("ARB_POLYMARKET_PROXY") else "direct_ARB_POLYMARKET_PROXY_unset")
    try:
        selected, result["coverage"] = discover(api, max_events, max_pages, max_markets)
    except (OSError, ValueError, KeyError, TypeError):
        result.update(coverage={}, finished_at=now())
        result["errors"].append(dict(stage="discovery", reason="public_discovery_failed"))
        return result
    for eid in selected:
        # Each group has its own book reference time. A broad sequential scan must
        # not age the earliest group against the end of the entire collection.
        snapshot = collect(api, max_events=1, event_ids=(eid,))
        snapshot["selected_event_id"] = eid
        snapshot["resolutions"] = {}
        for event in snapshot["events"]:
            for m in event.get("markets", []):
                mid = str(m.get("id", ""))
                try:
                    response = api.get("data", "/v2/resolutions", {"condition": m["conditionId"]})
                    snapshot["resolutions"][mid] = response
                except (OSError, ValueError, KeyError, TypeError):
                    snapshot["errors"].append(dict(market_id=mid, stage="resolution", reason="public_v2_resolution_failed"))
        snapshot["resolution_checked_at"] = now()
        result["snapshots"].append(snapshot)
    result["finished_at"] = now()
    return result


def resolution_reason(market, response):
    condition = market.get("conditionId")
    if not isinstance(condition, str) or not condition:
        return "resolution_missing_or_identity_mismatch"
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        return "resolution_unavailable"
    rows = response["data"]
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("condition_id") != condition:
        return "resolution_missing_or_identity_mismatch"
    row = rows[0]
    # Only pre-proposal markets qualify. Missing state never means unresolved.
    if row.get("status") != "posed" or row.get("was_disputed") is not False or row.get("extended_review") is not False:
        return "resolution_not_clean_posed"
    return None


def value_candidate(kind, event_id, legs, collateral, fixed_cost, risks, max_shares, max_skew, rebate):
    result = dict(kind=kind, event_id=event_id, gross_edge_per_share=None,
                  net_edge_per_share=None, blockers=[])
    if any(not leg.levels for leg in legs):
        result["blockers"] = ["empty_required_book_side"]
        return result
    times = [leg.book.as_of for leg in legs]
    if (max(times) - min(times)).total_seconds() > max_skew:
        result["blockers"] = ["book_time_skew"]
        return result
    row = opportunity(kind, event_id, legs, collateral, fixed_cost, risks,
                      max_shares, max_skew, include_nonpositive=True)
    if row is None:
        result["blockers"] = ["depth_below_minimum"]
        return result
    row["gross_edge_per_share"] = row["gross_profit_usd"] / row["quantity"]
    # Baseline is always before rebates; optional scenario uses the SAME fills.
    row["rebate_scenario_usd"] = row["taker_fees_usd"] * rebate
    row["net_after_rebate_scenario_usd"] = row["net_profit_usd"] + row["rebate_scenario_usd"]
    row["net_after_rebate_scenario_per_share"] = row["net_after_rebate_scenario_usd"] / row["quantity"]
    row["blockers"] = [] if row["net_profit_usd"] > 0 else [
        "fees_or_operation_costs" if row["gross_profit_usd"] > 0 else "no_gross_edge"]
    return row


def deep_scan(raw, costs=Costs(), max_shares=D(1000), max_age=120, max_skew=10, rebate=ZERO):
    rebate = number(rebate)
    if not ZERO <= rebate <= D("0.5"):
        raise ValueError("rebate scenario must be between 0 and 0.5")
    groups, errors = [], list(raw.get("errors", []))
    for original in raw["snapshots"]:
        snapshot = copy.deepcopy(original)
        if any(str(e.get("id")) != original["selected_event_id"] for e in snapshot["events"]):
            snapshot["events"] = []
            snapshot.setdefault("errors", []).append(dict(stage="event_identity", reason="selected_event_mismatch"))
        resolution_blocks = []
        for event in snapshot["events"]:
            for m in event.get("markets", []):
                mid = str(m.get("id", ""))
                reason = resolution_reason(m, snapshot.get("resolutions", {}).get(mid))
                if reason:
                    resolution_blocks.append(dict(market_id=mid, reason=reason))
                    m["acceptingOrders"] = False
        base = scan(snapshot, costs, max_shares, max_age, max_skew, authoritative_only=True)
        errors.extend(dict(selected_event_id=original.get("selected_event_id"), **e) for e in base["errors"])
        if not base["groups"]:
            groups.append(dict(event_id=original["selected_event_id"], title="", complete=False,
                market_count=0, valued_market_count=0, markets=[], candidates=[], best=None,
                blockers=["event_no_longer_active_negrisk"], resolution_blocks=resolution_blocks))
        for group in base["groups"]:
            event_id = group["event_id"]
            now_time = timestamp(snapshot["as_of"])
            parsed = []
            for m in group["markets"]:
                books = {side: Book.parse(m[side]["asset_id"], snapshot["books"][m[side]["asset_id"]],
                                         now_time, max_age) for side in ("yes", "no")}
                parsed.append((m, books, m["fee_rate"]))
            candidates = []

            def add(kind, legs, collateral, fixed, risks, anchor=None):
                row = value_candidate(kind, event_id, legs, collateral, fixed, risks, max_shares, max_skew, rebate)
                if anchor is not None:
                    row["anchor_market_id"] = anchor
                candidates.append(row)

            for m, books, rate in parsed:
                if not any(r in m["risks"] for r in ("other_outcome", "placeholder_outcome")):
                    add("buy_binary_merge", [Leg(m["market_id"], k, "buy", books[k], rate) for k in ("yes", "no")],
                        ONE, costs.merge_usd, m["risks"], m["market_id"])
            blockers = sorted(set(group["risks"]) & {
                "incomplete_group", "augmented_negrisk", "other_outcome", "placeholder_outcome"})
            if group["complete"]:
                risks = sorted(set(group["risks"] + [r for m, _, _ in parsed for r in m["risks"]]))
                add("buy_yes_basket", [Leg(m["market_id"], "yes", "buy", b["yes"], f) for m, b, f in parsed],
                    ONE, ZERO, risks + ["hold_to_resolution"])
                ratio = ONE - costs.conversion_fee_bps / 10000
                for anchor, (m, b, f) in enumerate(parsed):
                    add("split_sell_yes_basket", [Leg(x["market_id"], "yes", "sell", y["yes"], fee,
                        ONE if i == anchor else ratio) for i, (x, y, fee) in enumerate(parsed)],
                        -ONE, costs.split_usd + costs.convert_usd, risks, m["market_id"])
                    add("no_to_yes", [Leg(m["market_id"], "no", "buy", b["no"], f)] + [
                        Leg(x["market_id"], "yes", "sell", y["yes"], fee, ratio)
                        for i, (x, y, fee) in enumerate(parsed) if i != anchor],
                        ZERO, costs.convert_usd, risks, m["market_id"])
            valued = [c for c in candidates if c["net_edge_per_share"] is not None]
            best = max(valued, key=lambda c: c["net_profit_usd"], default=None)
            if resolution_blocks:
                blockers.append("resolution_exclusions")
            if not best:
                blockers.append("no_valued_candidate")
            elif best["blockers"]:
                blockers.extend(best["blockers"])
            event = next(e for e in snapshot["events"] if str(e["id"]) == event_id)
            groups.append(dict(event_id=event_id, title=group["title"], as_of=snapshot["as_of"],
                resolution_checked_at=snapshot.get("resolution_checked_at"), complete=group["complete"],
                market_count=len(event.get("markets", [])), valued_market_count=len(parsed),
                markets=[{k: m[k] for k in ("market_id", "fee_rate", "fee_source", "risks")} for m, _, _ in parsed],
                yes_ask_sum=group["yes_ask_sum"], yes_bid_sum=group["yes_bid_sum"],
                candidates=candidates, best=best, blockers=sorted(set(blockers)), resolution_blocks=resolution_blocks,
                compliance=base["compliance"], geoblock=base["geoblock"]))
    all_candidates = [c for g in groups for c in g["candidates"]]
    return dict(schema_version="negrisk-deep.report.v1", mode="read_only", execution="blocked", collateral="pUSD≈USD",
        started_at=raw["started_at"], finished_at=raw["finished_at"], coverage=raw.get("coverage", {}),
        network_mode=raw.get("network_mode", "unknown"),
        assumptions=dict(split_usd=costs.split_usd, merge_usd=costs.merge_usd, convert_usd=costs.convert_usd,
            conversion_fee_bps=costs.conversion_fee_bps, max_shares=max_shares, max_book_age_seconds=max_age,
            max_book_skew_seconds=max_skew, authoritative_fees_only=True, baseline_rebate_rate=ZERO,
            rebate_scenario_rate=rebate, rebate_scenario_eligibility="unverified_not_baseline",
            fee_rounding="per_price_level_half_up_5dp", selection_objective="maximum_net_profit_before_rebates"),
        summary=dict(groups_scanned=len(groups), complete_groups=sum(g["complete"] for g in groups),
            markets_seen=sum(g["market_count"] for g in groups), markets_valued=sum(g["valued_market_count"] for g in groups),
            candidates_valued=sum(c["net_edge_per_share"] is not None for c in all_candidates),
            positive_net_candidates=sum(c.get("net_profit_usd", ZERO) > 0 for c in all_candidates),
            errors=len(errors)), groups=groups, errors=errors)


def markdown(report):
    s = report["summary"]
    lines = ["# negRisk deep scan", "", f"UTC: {report['started_at']} → {report['finished_at']}", "",
        f"Groups {s['groups_scanned']}; complete {s['complete_groups']}; markets valued {s['markets_valued']}/{s['markets_seen']}; "
        f"positive net candidates {s['positive_net_candidates']}; errors {s['errors']}.",
        "Paper estimates only; execution blocked. Baseline excludes unverified rebates.", "",
        "Each row shows the candidate with greatest net USD profit at its selected depth; edges are USD per basket unit.", "",
        "| Event | Complete | Markets valued/seen | Strategy | Quantity | Gross edge | Fee USD | Net edge | Blockers |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for g in report["groups"]:
        b = g["best"] or {}
        def fmt(key):
            value = b.get(key)
            return "—" if value is None else f"{number(value):.6f}"
        title = g["title"].replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| {g['event_id']} {title} | {g['complete']} | {g['valued_market_count']}/{g['market_count']} | "
            f"{b.get('kind', '—')} | {fmt('quantity')} | {fmt('gross_edge_per_share')} | {fmt('taker_fees_usd')} | "
            f"{fmt('net_edge_per_share')} | {', '.join(g['blockers']) or 'positive_paper_estimate'} |")
    return "\n".join(lines) + "\n"


def load(path):
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return json.loads(data, parse_float=D)


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body.encode(), mtime=0) if path.suffix == ".gz" else body.encode())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", type=Path)
    p.add_argument("--record", type=Path)
    p.add_argument("--json", required=True, type=Path)
    p.add_argument("--markdown", type=Path)
    p.add_argument("--max-events", type=int, default=30)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--max-markets", type=int, default=12)
    p.add_argument("--max-shares", type=number, default=D(1000))
    p.add_argument("--max-book-age", type=int, default=120)
    p.add_argument("--max-book-skew", type=int, default=10)
    for arg in ("split-usd", "merge-usd", "convert-usd", "conversion-fee-bps", "rebate-scenario-rate"):
        p.add_argument("--" + arg, type=number, default=ZERO)
    a = p.parse_args(argv)
    if min(a.max_events, a.max_pages, a.max_book_age) < 1 or a.max_markets < 2 or a.max_book_skew < 0 or a.max_shares <= 0:
        p.error("invalid collection/valuation bounds")
    try:
        costs = Costs(a.split_usd, a.merge_usd, a.convert_usd, a.conversion_fee_bps)
        if not ZERO <= a.rebate_scenario_rate <= D("0.5"):
            raise ValueError("rebate scenario must be between 0 and 0.5")
        if a.fixture:
            raw = load(a.fixture)
        else:
            with ReadOnlyAPI() as api:
                raw = collect_deep(api, a.max_events, a.max_pages, a.max_markets)
        if a.record:
            write(a.record, dumps(raw))
        report = deep_scan(raw, costs, a.max_shares, a.max_book_age, a.max_book_skew, a.rebate_scenario_rate)
        write(a.json, dumps(report))
        if a.markdown:
            write(a.markdown, markdown(report))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        p.error("deep scan failed; check public data/fixture and scoped proxy configuration")
    return 2 if report["errors"] or not report["groups"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
