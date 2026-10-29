"""Bounded public collection and offline build: python -m arb_research.resolution_whales_cli."""

import argparse
from decimal import Decimal
from datetime import datetime, timezone
import json
import gzip
import math
import os
from pathlib import Path
import time

import httpx
from polymarket.models.data import Resolution
from polymarket.errors import UnexpectedResponseError

from .compliance import PUBLIC_API_BUDGET
from .polymarket.client import PublicDataClient, UpstreamError
from .resolution_whales import feature_rows, instant, report, validate_sample, write_features


def now():
    return datetime.now(timezone.utc).isoformat()


class PublicReadError(RuntimeError):
    pass


class PublicAPI:
    """ARB-7 public transport plus a separately scoped UMA read adapter."""

    def __init__(self, *, client=None, uma_client=None, sleep=time.sleep):
        self.uma_client = uma_client
        self.proxy_configured = bool(os.environ.get("ARB_POLYMARKET_PROXY"))
        try:
            self.polymarket = PublicDataClient(proxy=os.environ.get("ARB_POLYMARKET_PROXY") or None, sleep=sleep)
        except UpstreamError:
            raise PublicReadError("Invalid scoped public-read proxy configuration") from None
        if client is not None:
            self.polymarket.http.close()
            self.polymarket.http = client
        self.guard, self.limiter, self.sleep = self.polymarket.guard, self.polymarket.limiter, sleep

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.polymarket.__exit__(*_)
        if self.uma_client:
            self.uma_client.close()

    def get(self, service, path, params=None):
        if service in {"gamma", "data"}:
            host = "gamma-api.polymarket.com" if service == "gamma" else "data-api.polymarket.com"
            try:
                return self.polymarket.get(host, path, params)
            except UpstreamError:
                mode = "scoped proxy" if self.proxy_configured else "direct; ARB_POLYMARKET_PROXY is unset"
                raise PublicReadError(f"Public {service} read failed ({mode})") from None
        if service != "uma" or path != "/api/resolve-deeplink":
            raise ValueError("Endpoint outside public read allowlist")
        if self.uma_client is None:
            # The approved proxy scope is Polymarket only. UMA stays direct.
            self.uma_client = httpx.Client(trust_env=False, follow_redirects=False, timeout=15)
        for attempt in range(3):
            self.limiter.acquire()
            PUBLIC_API_BUDGET.acquire()
            try:
                response = self.uma_client.get(f"https://oracle.uma.xyz{path}", params=params)
                if response.url.host != "oracle.uma.xyz":
                    raise PublicReadError("Unexpected response host")
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except ValueError:
                        raise PublicReadError("Invalid public JSON response") from None
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise PublicReadError(f"Public {service} read HTTP {response.status_code}")
                try:
                    delay = float(response.headers.get("Retry-After", 0))
                    if not math.isfinite(delay) or delay > 30:
                        raise PublicReadError("Public read requires a later retry")
                except ValueError:
                    delay = 0
                self.sleep(max(2 ** attempt, delay))
            except httpx.TransportError:
                if attempt == 2:
                    raise PublicReadError("Public UMA read failed (direct)") from None
                self.sleep(2 ** attempt)
        raise AssertionError("unreachable")


def pages(api, path, params, max_pages):
    """Retain identical filters, validate opaque cursors; bounded walks are explicit."""
    rows, cursor, seen = [], None, set()
    for _ in range(max_pages):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        body = api.get("data", path, query)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("Expected v2 data envelope")
        rows.extend(body["data"])
        p = body["pagination"]
        cursor = p["next_cursor"]
        if type(p["has_more"]) is not bool or bool(cursor) != p["has_more"]:
            raise ValueError("Inconsistent pagination")
        if not cursor:
            return rows, True
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Repeated/invalid cursor")
        seen.add(cursor)
    return rows, False


def normalize_market(m, event):
    def array(x):
        return json.loads(x) if isinstance(x, str) else x
    key = "positionIds" if m.get("version") == "v2" else "clobTokenIds"
    ids, labels = array(m.get(key) or []), array(m.get("outcomes") or [])
    if len(ids) != 2 or len(labels) != 2 or len(set(ids)) != 2:
        raise ValueError("Market requires two distinct outcome tokens")
    tags = event.get("tags") or []
    slugs = [t.get("slug", "") for t in tags]
    category = m.get("category") or event.get("category") or next((c for c in (
        "politics", "sports", "crypto", "finance", "economics", "geopolitics", "culture", "tech") if c in slugs), "unknown")
    return dict(market_id=str(m["id"]), condition_id=m["conditionId"], event_id=str(event["id"]),
        question=m.get("question"), rules=m.get("description"), category=category,
        tags=slugs, protocol=m.get("version", "unknown"),
        tokens=[dict(token_id=str(t), outcome=labels[i], outcome_index=i) for i, t in enumerate(ids)])


def normalize_resolution(raw, *, outcomes=()):
    r = Resolution.parse_response(raw)
    payouts = [float(p) for p in r.payouts] if r.payouts is not None else None
    basis = "condition_payouts" if payouts is not None else None
    # Older UMA rows expose an oracle price rather than condition payouts.
    # Map only the documented binary constants and verified YES/NO ordering.
    if (payouts is None and r.status.value == "resolved"
            and [str(o).lower() for o in outcomes] == ["yes", "no"]
            and r.price in {Decimal(0), Decimal(5 * 10**17), Decimal(10**18)}):
        yes = float(r.price / Decimal(10**18))
        payouts, basis = [yes, 1 - yes], "uma_final_price"
    return dict(condition_id=str(r.condition_id), status=r.status.value,
        was_disputed=r.was_disputed, extended_review=r.extended_review,
        new_version_q=r.question_rules_updated,
        last_update_timestamp=r.last_updated_at.isoformat(),
        resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
        payouts=payouts, payout_basis=basis,
        oracle_finalized_at=r.last_updated_at.isoformat() if basis == "uma_final_price" else None,
        proposed_price=str(r.proposed_price) if r.proposed_price is not None else None,
        reproposed_price=str(r.reproposed_price) if r.reproposed_price is not None else None,
        price=str(r.price) if r.price is not None else None)


def normalize_uma(body, resolution_raw):
    e = body["entity"]
    qid, tx = resolution_raw.get("question_id"), resolution_raw.get("transaction_hash")
    if (body["chainId"] != 137 or body["oracleType"] != "Optimistic Oracle V2"
            or not qid or not e["id"].endswith("-" + qid)
            or tx not in [e.get(k) for k in ("requestHash", "proposalHash", "disputeHash", "settlementHash")]):
        raise ValueError("UMA lifecycle identity mismatch")
    result = {k: e.get(k) for k in ("requestTimestamp", "proposalTimestamp", "disputeTimestamp",
        "settlementTimestamp", "requestHash", "proposalHash", "disputeHash", "settlementHash")}
    ts = [instant(result[k]) for k in ("requestTimestamp", "proposalTimestamp", "disputeTimestamp",
                                      "settlementTimestamp") if result[k] is not None]
    if ts != sorted(ts):
        raise ValueError("UMA lifecycle time order mismatch")
    return dict(question_id=qid, source="https://oracle.uma.xyz/api/resolve-deeplink", **result)


def collect(api, *, event_slugs=(), event_limit=4, markets_per_event=2, max_pages=2):
    result = dict(schema_version="resolution-whales.raw.v1", started_at=now(), samples=[], errors=[],
                  selection=dict(event_slugs=list(event_slugs), event_limit=event_limit,
                    markets_per_event=markets_per_event, max_pages=max_pages,
                    discovery="explicit events" if event_slugs else "volume-desc closed and active events"))
    result["compliance"] = api.guard.status()
    if event_slugs:
        events = [api.get("gamma", "/events/slug/" + slug) for slug in event_slugs]
    else:
        events = []
        for closed in ("true", "false"):
            events.extend(api.get("gamma", "/events", {"closed": closed, "limit": event_limit,
                "order": "volume", "ascending": "false"}))
    seen = set()
    for event in events:
        for m in (event.get("markets") or [])[:markets_per_event]:
            mid = str(m.get("id", ""))
            if mid in seen:
                continue
            seen.add(mid)
            try:
                market = normalize_market(m, event)
            except (KeyError, ValueError, TypeError):
                result["errors"].append(dict(market_id=mid, stage="metadata", reason="invalid_metadata"))
                continue
            s = dict(market=market, resolution=None, uma_lifecycle=None, holders=None, trades=None, prices={},
                holders_walk_exhausted=False, trades_walk_exhausted=False, prices_walk_exhausted={})
            condition = market["condition_id"]
            for stage in ("resolution", "holders", "trades", "prices"):
                try:
                    if stage == "resolution":
                        data = api.get("data", "/v2/resolutions", {"condition": condition})["data"]
                        if len(data) > 1:
                            raise ValueError("duplicate resolution")
                        s[stage] = normalize_resolution(data[0],
                            outcomes=[t["outcome"] for t in market["tokens"]]) if data else None
                        if data and data[0].get("was_disputed") and data[0].get("question_id") and data[0].get("transaction_hash"):
                            try:
                                body = api.get("uma", "/api/resolve-deeplink", {
                                    "transactionHash": data[0]["transaction_hash"], "chainId": 137})
                                s["uma_lifecycle"] = normalize_uma(body, data[0])
                            except (PublicReadError, KeyError, ValueError, TypeError):
                                result["errors"].append(dict(market_id=mid, stage="uma_lifecycle", reason="public_read_or_identity_failed"))
                    elif stage == "holders":
                        groups, exhausted = pages(api, "/v2/holders", {"condition": condition,
                            "limit": 100, "include_pnl": "true"}, max_pages)
                        merged = {}
                        for group in groups:
                            token = group["token_id"]
                            merged.setdefault(token, {})
                            for h in group["holders"]:
                                # Public addresses only. Discard profile/bio/name entirely.
                                h = {k: h[k] for k in ("proxy_wallet", "amount")}
                                wallet = h["proxy_wallet"].lower()
                                if wallet in merged[token]:
                                    raise ValueError("Holder walk changed during pagination")
                                merged[token][wallet] = h
                        s[stage] = [dict(token_id=t, holders=list(h.values())) for t, h in merged.items()]
                        s["holders_walk_exhausted"] = exhausted
                    elif stage == "trades":
                        data, exhausted = pages(api, "/v2/trades", {"condition": condition,
                            "limit": 1000, "taker_only": "true"}, max_pages)
                        s[stage] = [{k: t[k] for k in ("proxy_wallet", "condition_id", "token_id",
                            "outcome_index", "side", "size", "price", "timestamp", "transaction_hash")} for t in data]
                        s["trades_walk_exhausted"] = exhausted
                    else:
                        for token in market["tokens"]:
                            tid = token["token_id"]
                            data, exhausted = pages(api, "/v2/prices-history",
                                {"token_id": tid, "interval": "max", "limit": 10000}, max_pages)
                            s[stage][tid] = data
                            s["prices_walk_exhausted"][tid] = exhausted
                except (PublicReadError, UnexpectedResponseError, KeyError, ValueError, TypeError):
                    # Never interpolate raw exceptions: transports/configuration may carry credentials.
                    result["errors"].append(dict(market_id=mid, stage=stage, reason="public_read_or_schema_failed"))
            s["observed_at"] = now()
            try:
                validate_sample(s)
                result["samples"].append(s)
            except (ValueError, TypeError, KeyError):
                result["errors"].append(dict(market_id=mid, stage="validation", reason="invalid_sample"))
    result["finished_at"] = now()
    return result


def dump(value, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    Path(path).write_bytes(gzip.compress(raw, mtime=0) if str(path).endswith(".gz") else raw)


def load(path):
    raw = Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("collect")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--event", action="append", default=[])
    live.add_argument("--event-limit", type=int, choices=range(1, 11), default=4)
    live.add_argument("--markets-per-event", type=int, choices=range(1, 11), default=2)
    live.add_argument("--max-pages", type=int, choices=range(1, 11), default=2)
    build = sub.add_parser("build")
    build.add_argument("--input", type=Path, action="append", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--as-of", action="append", default=[])
    build.add_argument("--threshold-usd", type=float, default=1000)
    build.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            with PublicAPI() as api:
                raw = collect(api, event_slugs=args.event, event_limit=args.event_limit,
                    markets_per_event=args.markets_per_event, max_pages=args.max_pages)
            dump(raw, args.output)
            print(json.dumps(dict(samples=len(raw["samples"]), errors=len(raw["errors"]))))
            if raw["errors"] or not raw["samples"]:
                raise SystemExit(2)
        else:
            samples = []
            for path in args.input:
                raw = load(path)
                if raw["schema_version"] != "resolution-whales.raw.v1":
                    raise ValueError("Unsupported input version")
                samples.extend(raw["samples"])
            cuts = args.as_of or [s["observed_at"] for s in samples]
            rows = feature_rows(samples, cuts, top_n=args.top_n, threshold_usd=args.threshold_usd)
            args.output.mkdir(parents=True, exist_ok=True)
            write_features(rows, args.output / "features.parquet")
            dump(report(samples, as_of=max(map(instant, cuts)), threshold_usd=args.threshold_usd),
                 args.output / "report.json")
            print(json.dumps(dict(feature_rows=len(rows), markets=len({r["market_id"] for r in rows}))))
    except (PublicReadError, ValueError, TypeError, KeyError):
        raise SystemExit("Public collection/build failed; verify scoped proxy, input schema and coverage") from None


if __name__ == "__main__":
    main()
