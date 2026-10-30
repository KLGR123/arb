"""Daily paper scanner: python -m arb_research.negrisk_cli --help."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import threading
import time
from urllib.error import HTTPError

import httpx

from .compliance import ComplianceGuard, GeoblockClient, PUBLIC_API_BUDGET

from .negrisk import Costs, D, dumps, number, scan
from .negrisk_protocol import diagnostic, identity, qualified_assets


class TokenBucket:
    """One shared process bucket across Gamma, CLOB, geoblock and retries.

    Default 10 req/s with a burst of 10: below the general 9000/10s ceiling
    and the lower endpoint limits. Inject clock/sleep for offline tests.
    """

    def __init__(self, rate=10, capacity=10, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.capacity = rate, capacity
        self.tokens = float(capacity)
        self.clock, self.sleep = clock, sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                self.sleep((1 - self.tokens) / self.rate)
                self.updated = self.clock()
                self.tokens = 0
            else:
                self.tokens -= 1


GLOBAL_BUCKET = TokenBucket()


class ReadOnlyAPI:
    def __init__(self, timeout=10, attempts=3, bucket=GLOBAL_BUCKET, sleep=time.sleep, client=None):
        self.timeout, self.attempts, self.bucket, self.sleep = timeout, attempts, bucket, sleep
        try:
            # Explicit per-client scope, never set global HTTP(S)_PROXY. No secrets in diagnostics.
            self.client = client or httpx.Client(
                proxy=os.environ.get("ARB_POLYMARKET_PROXY") or None,
                trust_env=False, follow_redirects=False, timeout=timeout,
                headers={"User-Agent": "arb-negrisk-readonly/1.0", "Accept": "application/json"})
        except (ValueError, ImportError, httpx.HTTPError):
            raise ValueError("invalid_read_only_proxy_configuration") from None

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, service, path, params=None):
        hosts = {"gamma": "gamma-api.polymarket.com", "clob": "clob.polymarket.com",
                 "geo": "polymarket.com", "data": "data-api.polymarket.com"}
        if service not in hosts:
            raise ValueError("unknown service")
        valid = ((service == "gamma" and (path == "/events" or path.startswith("/events/")))
                 or (service == "clob" and (path == "/book" or path.startswith("/clob-markets/")))
                 or (service == "data" and path == "/v2/resolutions")
                 or (service == "geo" and path == "/api/geoblock"))
        if not valid or any(c in path for c in ("?", "#", "..", "\\")):
            raise ValueError("endpoint is outside read-only allowlist")
        url = f"https://{hosts[service]}{path}"
        # GeoblockClient owns geoblock retries; other reads retry here.
        attempts = 1 if service == "geo" else self.attempts
        for attempt in range(attempts):
            self.bucket.acquire()
            PUBLIC_API_BUDGET.acquire()
            try:
                response = self.client.get(url, params=params)
                if response.url.host != hosts[service]:
                    raise ValueError("unexpected response host")
                if 300 <= response.status_code < 400:
                    raise ValueError("redirect rejected by read-only transport")
                response.raise_for_status()
                return response.json(parse_float=D)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                    # Deliberately omit raw transport exceptions, proxy URLs and credentials.
                    raise HTTPError("", code, "public_read_failed", {}, None) from None
                try:
                    retry_after = max(0, min(float(exc.response.headers.get("Retry-After", 0)), 30))
                except ValueError:
                    retry_after = 0
                self.sleep(max(retry_after, min(2 ** attempt, 8)) + random.uniform(0, .2))
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise OSError("public_read_transport_failed") from None
                self.sleep(min(2 ** attempt, 8) + random.uniform(0, .2))


def collect(api, max_events=10, max_pages=5, event_ids=()):
    snapshot = {"as_of": "", "events": [], "books": {}, "clob_markets": {}, "errors": [],
                "geoblock": {"status": "unknown", "execution": "blocked"},
                "coverage": {"max_events": max_events, "max_pages": max_pages, "pages_read": 0,
                             "discovery_exhausted": False, "selected_events": 0}}
    # Reuse main's deny-by-default guard and its validated/sanitized geoblock result.
    guard = ComplianceGuard(geoblock=GeoblockClient(fetch=lambda _: api.get("geo", "/api/geoblock")))
    snapshot["compliance"] = guard.status()
    geo = snapshot["compliance"]["geoblock"]
    snapshot["geoblock"] = {"status": "unknown" if geo["error"] else
                           "blocked" if geo["blocked"] or geo["country"] == "US" else "allowed",
                           "country": geo["country"], "region": geo["region"], "execution": "blocked"}
    if geo["error"]:
        snapshot["errors"].append({"stage": "geoblock", "reason": geo["error"]})
    seen = set()
    seen_pages = set()
    try:
        if event_ids:
            pages = ([api.get("gamma", f"/events/{eid}") for eid in event_ids],)
        else:
            pages = (api.get("gamma", "/events", {"active": "true", "closed": "false",
                     "limit": 100, "offset": i * 100, "order": "volume24hr", "ascending": "false"})
                     for i in range(max_pages))
        for page in pages:
            snapshot["coverage"]["pages_read"] += 1
            if not isinstance(page, list):
                raise ValueError("Gamma events response must be a list")
            page_ids = tuple(str(e["id"]) for e in page)
            if page_ids and page_ids in seen_pages:
                raise ValueError("Gamma pagination repeated a page")
            seen_pages.add(page_ids)
            for event in page:
                eid = str(event["id"])
                if eid in seen:
                    continue
                seen.add(eid)
                if event.get("negRisk") is True and event.get("active") is True and not event.get("closed"):
                    snapshot["events"].append(event)
                    if len(snapshot["events"]) >= max_events:
                        break
            if len(snapshot["events"]) >= max_events:
                break
            if len(page) < 100:
                snapshot["coverage"]["discovery_exhausted"] = not bool(event_ids)
                break
    except (OSError, ValueError, KeyError, TypeError) as exc:
        snapshot["errors"].append({"stage": "discovery", "reason": str(exc)})
    snapshot["coverage"]["selected_events"] = len(snapshot["events"])
    snapshot["coverage"]["selection"] = "explicit_ids" if event_ids else "volume24hr_desc_bounded"
    for event in snapshot["events"]:
        for market in event.get("markets", []):
            if market.get("closed") or market.get("active") is not True:
                continue
            mid = str(market.get("id", ""))
            view = identity(market, str(event["id"]))
            try:
                ids = {k: a.asset_id for k, a in qualified_assets(market, view).items()}
            except (ValueError, KeyError, TypeError) as exc:
                snapshot["errors"].append(diagnostic(market, view, str(exc),
                                                    event_id=str(event["id"]), stage="assets"))
                continue
            try:
                condition = str(market["conditionId"])
                snapshot["clob_markets"][mid] = api.get("clob", "/clob-markets/" + condition)
            except (OSError, ValueError, KeyError) as exc:
                snapshot["errors"].append(diagnostic(market, view, str(exc), stage="fees"))
            for asset_id in ids.values():
                if asset_id in snapshot["books"]:
                    continue
                try:
                    snapshot["books"][asset_id] = api.get("clob", "/book", {"token_id": asset_id})
                except (OSError, ValueError) as exc:
                    snapshot["errors"].append(diagnostic(market, view, str(exc),
                                                        asset_id=asset_id, stage="book"))
    snapshot["as_of"] = datetime.now(timezone.utc).isoformat()
    return snapshot


def markdown(report):
    summary = report["summary"]
    lines = [f"# negRisk paper report — {report['as_of']}", "",
             f"Groups: {summary['groups_scanned']}; positive estimates: {summary['opportunities']}; errors: {summary['errors']}.",
             f"Geoblock: {report['geoblock'].get('status', 'unknown')}; execution is always blocked.",
             "Snapshot estimates include taker fees and configured operation costs. See JSON for coverage and assumptions.", "",
             "| Event | Markets | Complete set | YES asks | YES bids |",
             "| --- | ---: | --- | ---: | ---: |"]
    for group in report["groups"]:
        title = group["title"].replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        ask = group["yes_ask_sum"] if group["yes_ask_sum"] is not None else "—"
        bid = group["yes_bid_sum"] if group["yes_bid_sum"] is not None else "—"
        lines.append(f"| {group['event_id']}: {title} | {len(group['markets'])} | {group['complete']} | {ask} | {bid} |")
    lines += ["",
             "| Event | Strategy | Shares | Net USD/share | Net USD | Risks |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    for row in report["opportunities"]:
        lines.append(f"| {row['event_id']} | {row['kind']} | {row['quantity']} | {row['net_edge_per_share']:.6f} | {row['net_profit_usd']:.5f} | {', '.join(row['risks'])} |")
    if report["errors"]:
        lines += ["", "Incomplete data: inspect `errors` in the JSON report."]
    skipped = [(g["event_id"], reason) for g in report["groups"]
               for reason in g.get("skipped_reasons", [])]
    if skipped:
        lines += ["", "Skipped group strategies:"]
        lines += [f"- Event {eid}: {reason}" for eid, reason in skipped]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only negRisk paper scanner. Never signs or executes orders.")
    parser.add_argument("--fixture", type=Path, help="Offline snapshot; makes zero network requests")
    parser.add_argument("--record", type=Path, help="Save replayable public raw snapshot")
    parser.add_argument("--json", type=Path, help="Write versioned JSON report (otherwise stdout)")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--max-events", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-shares", type=number, default=D(1000))
    parser.add_argument("--max-book-age", type=int, default=120)
    parser.add_argument("--max-book-skew", type=int, default=10)
    parser.add_argument("--near-hours", type=int, default=48)
    for arg in ("split-usd", "merge-usd", "convert-usd", "conversion-fee-bps"):
        parser.add_argument("--" + arg, type=number, default=D(0))
    args = parser.parse_args(argv)
    if args.max_events < 1 or args.max_pages < 1 or any(not eid.isdigit() for eid in args.event_id):
        parser.error("positive discovery limits and numeric event IDs required")
    try:
        costs = Costs(args.split_usd, args.merge_usd, args.convert_usd, args.conversion_fee_bps)
        # Validate limits before making any network requests.
        if args.max_shares <= 0 or args.max_book_age <= 0 or args.max_book_skew < 0 or args.near_hours < 0:
            raise ValueError("invalid scan limits")
        if args.fixture:
            snapshot = json.loads(args.fixture.read_text(), parse_float=D)
        else:
            with ReadOnlyAPI() as api:
                snapshot = collect(api, args.max_events, args.max_pages, args.event_id)
        report = scan(snapshot, costs, args.max_shares, args.max_book_age, args.max_book_skew, args.near_hours)
    except (ValueError, OSError, KeyError, ArithmeticError) as exc:
        parser.error(str(exc))
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(dumps(snapshot), encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(dumps(report), encoding="utf-8")
    else:
        print(dumps(report), end="")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report), encoding="utf-8")
    # Empty/incomplete scans cannot masquerade as successful "no opportunities".
    return 2 if report["errors"] or not report["groups"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
