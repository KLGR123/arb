"""python -m arb_research.cross_venue_cli --help"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .compliance import ComplianceGuard, GeoblockClient
from .cross_venue import candidates, markdown, normalize_kalshi, normalize_poly, scan
from .kalshi import KalshiPublicAPI, book, discover, identifier
from .negrisk import D, dumps, fee_config, number
from .negrisk_cli import ReadOnlyAPI
from .polymarket.client import SharedLimiter
from .rule_alignment import RuleAligner


def collect(poly, kalshi, *, event_ids=(), kalshi_events=(), series=None,
            pages=1, pair_limit=5, max_hours=48, min_similarity=.20):
    result = {"as_of": "", "polymarket": [], "kalshi": [], "candidates": [],
              "poly_books": {}, "poly_fees": {}, "kalshi_books": {},
              "raw": {"poly_events": [], "kalshi_events": [], "kalshi_markets": [], "series": {}, "clob": {}},
              "errors": [], "coverage": {"pages_limit": pages, "pair_limit": pair_limit,
                                         "max_close_gap_hours": max_hours, "min_title_similarity": min_similarity,
                                         "selection": "explicit_events" if event_ids or kalshi_events else "bounded_discovery"}}
    guard = ComplianceGuard(geoblock=GeoblockClient(fetch=lambda _: poly.get("geo", "/api/geoblock")))
    result["compliance"] = guard.status()
    pevents = []
    try:
        if event_ids:
            pevents = [poly.get("gamma", "/events/" + identifier(eid)) for eid in event_ids]
        else:
            for page in range(pages):
                batch = poly.get("gamma", "/events", {"active": "true", "closed": "false",
                                "limit": 100, "offset": page*100, "order": "volume24hr", "ascending": "false"})
                pevents.extend(batch)
                if len(batch) < 100:
                    break
    except (ValueError, OSError):
        result["errors"].append({"stage": "poly_discovery", "reason": "public_read_failed"})
    result["raw"]["poly_events"] = pevents
    p_raw = {}
    for event in pevents:
        for market in event.get("markets", []):
            try:
                p = normalize_poly(event, market)
                result["polymarket"].append(p)
                p_raw[p["id"]] = (event, market)
            except (ValueError, KeyError, TypeError):
                # Non-binary / non-tokenized markets are outside the valuation universe.
                continue
    try:
        kevents, kmarkets = discover(kalshi, series, pages, kalshi_events)
        result["raw"]["kalshi_events"], result["raw"]["kalshi_markets"] = kevents, kmarkets
        emap = {e["event_ticker"]: e for e in kevents}
        # Rules and fee series are fetched only for shortlisted pairs below.
        for market in kmarkets:
            event = emap[market["event_ticker"]]
            result["kalshi"].append(normalize_kalshi(event, market, {}))
        result["candidates"] = candidates(result["polymarket"], result["kalshi"],
                                             pair_limit, max_hours, min_similarity)
        selected = {p["kalshi_id"] for p in result["candidates"]}
        series_cache = result["raw"]["series"]
        for i, k in enumerate(result["kalshi"]):
            if k["id"] not in selected:
                continue
            try:
                market = kalshi.get("/markets/" + identifier(k["id"]))["market"]
                event = emap[market["event_ticker"]]
                series_id = event["series_ticker"]
                if series_id not in series_cache:
                    series_cache[series_id] = kalshi.get("/series/" + identifier(series_id))["series"]
                result["kalshi"][i] = normalize_kalshi(event, market, series_cache[series_id])
                result["kalshi_books"][k["id"]] = book(kalshi, k["id"])
                result["raw"].setdefault("kalshi_details", {})[k["id"]] = market
            except (ValueError, KeyError, OSError):
                result["errors"].append({"stage": "kalshi_detail", "market_id": k["id"], "reason": "public_read_failed"})
    except (ValueError, KeyError, OSError):
        result["errors"].append({"stage": "kalshi_discovery", "reason": "public_read_failed"})
    for mid in sorted({p["poly_id"] for p in result["candidates"]}):
        try:
            event, market = p_raw[mid]
            clob = poly.get("clob", "/clob-markets/" + identifier(market["conditionId"]))
            result["raw"]["clob"][mid] = clob
            rate, source = fee_config(market, event, clob)
            result["poly_fees"][mid] = [rate, source]
            tokens = next(p["tokens"] for p in result["polymarket"] if p["id"] == mid)
            for asset in tokens.values():
                result["poly_books"][asset] = poly.get("clob", "/book", {"token_id": asset})
        except (ValueError, OSError, KeyError):
            result["errors"].append({"stage": "poly_detail", "market_id": mid, "reason": "public_read_failed"})
    result["as_of"] = datetime.now(timezone.utc).isoformat()
    result["coverage"].update({"poly_markets": len(result["polymarket"]), "kalshi_markets": len(result["kalshi"]),
                               "selected_pairs": len(result["candidates"]), "exhaustive": False})
    return result


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kalshi / Polymarket public paper-only pairing and edge research")
    parser.add_argument("--fixture", type=Path, help="Offline snapshot; implies cached rule comparisons only")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(".state/cross-venue/rules"))
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--poly-event-id", action="append", default=[])
    parser.add_argument("--kalshi-event", action="append", default=[])
    parser.add_argument("--kalshi-series")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--max-pairs", type=int, default=5)
    parser.add_argument("--max-close-gap-hours", type=float, default=48)
    parser.add_argument("--min-title-similarity", type=float, default=.20)
    parser.add_argument("--quantity", type=number, default=D(10))
    parser.add_argument("--max-book-age", type=int, default=120)
    parser.add_argument("--max-book-skew", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        if (not 1 <= args.pages <= 100 or not 1 <= args.max_pairs <= 100
                or not 0 <= args.max_close_gap_hours <= 24*366
                or not 0 <= args.min_title_similarity <= 1
                or not args.quantity.is_finite() or args.quantity < 1
                or args.quantity != args.quantity.to_integral_value()
                or args.max_book_age < 1 or args.max_book_skew < 0):
            raise ValueError("invalid scan limits")
        for value in args.poly_event_id + args.kalshi_event + ([args.kalshi_series] if args.kalshi_series else []):
            identifier(value)
        if args.fixture:
            snapshot = json.loads(args.fixture.read_text(encoding="utf-8"))
        else:
            limiter = SharedLimiter()
            with ReadOnlyAPI(bucket=limiter) as poly, KalshiPublicAPI(bucket=limiter) as kalshi:
                snapshot = collect(poly, kalshi, event_ids=args.poly_event_id, kalshi_events=args.kalshi_event,
                                   series=args.kalshi_series, pages=args.pages, pair_limit=args.max_pairs,
                                   max_hours=args.max_close_gap_hours, min_similarity=args.min_title_similarity)
        # Persist public input before a potentially unavailable local model call.
        if args.record:
            write(args.record, dumps(snapshot))
        aligner = RuleAligner(args.cache, cache_only=args.cache_only or bool(args.fixture))
        report = scan(snapshot, aligner, args.quantity, args.max_book_age, args.max_book_skew)
        if args.json:
            write(args.json, dumps(report))
        else:
            print(dumps(report), end="")
        if args.markdown:
            write(args.markdown, markdown(report))
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError):
        parser.error("cross_venue_scan_failed: check public connectivity, fixture schema and local Codex/cache configuration")
    return 2 if report["errors"] or not report["pairs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
