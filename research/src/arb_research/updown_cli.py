"""Bounded public recording and offline replay for ARB-15. No execution routes."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import time

import httpx

from .compliance import PUBLIC_API_BUDGET
from .polymarket.client import PublicDataClient, UpstreamError
from .updown import analyze


class StudySources:
    """Consume ARB-7's allowlisted transport; add only public CEX candles.

    No parallel metadata store or history API. Both sources share ARB-7's
    cross-process limiter and main's compliance budget. Proxy is Polymarket-only.
    """

    def __init__(self, *, sleep=time.sleep, limiter=None):
        self.sleep = sleep
        self.proxy_configured = bool(os.environ.get("ARB_POLYMARKET_PROXY"))
        try:
            self.poly = PublicDataClient(proxy=os.environ.get("ARB_POLYMARKET_PROXY") or None,
                                         limiter=limiter, sleep=sleep)
            self.cex = httpx.Client(trust_env=False, follow_redirects=False, timeout=15)
        except (UpstreamError, ValueError, httpx.HTTPError):
            raise ValueError("public_read_client_configuration_failed") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.poly.http.close()
        self.cex.close()

    def get(self, service, params=None):
        routes = {"gamma": ("gamma-api.polymarket.com", "/events"),
                  "history": ("data-api.polymarket.com", "/v2/prices-history")}
        if service in routes:
            try:
                return self.poly.get(*routes[service], params)
            except (UpstreamError, ValueError, httpx.HTTPError):
                direct = "_direct_no_scoped_proxy" if not self.proxy_configured else ""
                raise OSError(f"public_{service}_read_failed" + direct) from None
        if service != "cex":
            raise ValueError("endpoint outside study read-only allowlist")
        for attempt in range(3):
            self.poly.limiter.acquire()
            PUBLIC_API_BUDGET.acquire()
            try:
                response = self.cex.get("https://data-api.binance.vision/api/v3/klines", params=params)
                if response.url.host != "data-api.binance.vision" or 300 <= response.status_code < 400:
                    raise ValueError("public_read_redirect_rejected")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status == 429 or 500 <= status <= 599
                try:
                    retry_after = min(30, max(0, float(exc.response.headers.get("Retry-After", 0))))
                except ValueError:
                    retry_after = 0
                reason = f"public_cex_http_{status}"
            except (httpx.TransportError, ValueError):
                retryable, retry_after = True, 0
                reason = "public_cex_transport_or_json_failed"
            if attempt == 2 or not retryable:
                raise OSError(reason) from None
            self.sleep(max(retry_after, 2 ** attempt))
        raise AssertionError("unreachable")


def collect(source, start, count):
    """Consecutive fixed cohort, no outcome/volume selection; fail on missing data."""
    if type(start) is not int or start % 300 or not 1 <= count <= 100:
        raise ValueError("aligned epoch start and count 1..100 required")
    if start + count * 300 > datetime.now(timezone.utc).timestamp() - 600:
        raise ValueError("cohort must have ended at least ten minutes ago")
    recording = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
                 "selection": {"start": start, "count": count, "duration_seconds": 300,
                               "method": "consecutive BTC five-minute windows, no outcome filtering"},
                 "compliance": source.poly.guard.status(), "markets": [],
                 "cex": {"source": "https://data-api.binance.vision/api/v3/klines",
                         "symbol": "BTCUSDT", "interval": "1s", "price_field": "close",
                         "timestamp_field": "(close_time_ms + 1) / 1000", "points": [], "pages": []}}
    # Warmup 900s, including strike TWAP; include terminal tick at final boundary.
    cursor, stop = start - 1000, start + count * 300
    while cursor <= stop:
        params = {"symbol": "BTCUSDT", "interval": "1s", "startTime": cursor * 1000,
                  "endTime": stop * 1000, "limit": 1000}
        rows = source.get("cex", params)
        if not isinstance(rows, list) or not rows:
            raise ValueError("missing CEX page")
        last_open = int(rows[-1][0]) // 1000
        if last_open < cursor:
            raise ValueError("CEX pagination did not advance")
        recording["cex"]["pages"].append({"params": params, "rows": len(rows),
            "normalized_response_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()})
        for row in rows:
            tick = (int(row[6]) + 1) / 1000
            if tick <= stop:
                recording["cex"]["points"].append([tick, row[4]])
        cursor = last_open + 1
    for i in range(count):
        window = start + i * 300
        slug = f"btc-updown-5m-{window}"
        events = source.get("gamma", {"slug": slug})
        if not isinstance(events, list) or len(events) != 1 or events[0].get("slug") != slug:
            raise ValueError("requested Gamma event not found")
        event = events[0]
        from .updown import parse_market
        market = parse_market(event)
        params = {"token_id": market["up_token"], "start": window, "end": window + 300,
                  "bucket_seconds": 60, "limit": 1000}
        points, cursors = [], set()
        while True:
            page = source.get("history", params)
            if not isinstance(page.get("data"), list):
                raise ValueError("invalid Data API v2 envelope")
            points.extend(page["data"])
            pagination = page["pagination"]
            cursor = pagination.get("next_cursor")
            if not cursor:
                if pagination.get("has_more"):
                    raise ValueError("Data API pagination missing cursor")
                break
            if cursor in cursors or len(cursors) >= 20:
                raise ValueError("Data API pagination repeated or exceeded limit")
            cursors.add(cursor)
            params = {**params, "cursor": cursor}
        recording["markets"].append({"event": event, "history": points})
    return recording


def read_recording(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as stream:
        json.dump(value, stream, indent=None if path.suffix == ".gz" else 2, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--start", type=int, required=True)
    record.add_argument("--count", type=int, default=60)
    record.add_argument("--output", type=Path, required=True)
    replay = sub.add_parser("analyze")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            with StudySources() as source:
                result = collect(source, args.start, args.count)
        else:
            result = analyze(read_recording(args.input))
            result["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        write_json(args.output, result)
    except (ValueError, KeyError, OSError, TypeError, ArithmeticError):
        # Library errors may include caller-controlled payloads; never echo credentials.
        parser.exit(2, "Study failed: invalid/incomplete public data or public read unavailable. "
                       "Polymarket uses direct HTTPS when ARB_POLYMARKET_PROXY is unset; "
                       "CEX always uses direct HTTPS.\n")
    if args.command == "analyze":
        print(json.dumps({"status": result["status"], "settled_markets": result["settlement"]["markets"],
                          "agreement": result["settlement"]["agreement"], "errors": result["errors"]}))
        return 0 if result["status"] == "complete" else 2
    print(json.dumps({"recorded_markets": len(result["markets"]), "cex_points": len(result["cex"]["points"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
