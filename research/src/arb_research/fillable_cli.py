"""Bounded public BTC book recording and offline fill-cost research; no orders."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time

from .orderbook.recorder import Recorder, run_guarded
from .orderbook.storage import records
from .orderbook.core import digest
from .negrisk_cli import ReadOnlyAPI
from .updown import parse_market
from .updown_cli import StudySources, read_recording, write_json


def collect(source, duration, transport="websocket"):
    if not 10 <= duration <= 300:
        raise ValueError("recording duration must be 10..300 seconds")
    start = int(time.time()) // 300 * 300
    events = []
    for window in (start, start + 300):
        rows = source.get("gamma", {"slug": f"btc-updown-5m-{window}"})
        if len(rows) != 1:
            raise ValueError("missing BTC event")
        parse_market(rows[0], require_resolved=False)
        events.append(rows[0])
    assets = [m[token] for e in events for m in [parse_market(e, require_resolved=False)]
              for token in ("up_token", "down_token")]
    compliance = source.poly.guard.status()
    # The durable ARB-8 journal validates checksum, sequence and discontinuities.
    # A temporary journal is folded into the immutable, portable recording.
    if transport == "websocket":
        with tempfile.TemporaryDirectory(prefix="arb-btc-books-") as directory:
            recorder = Recorder(directory, assets, snapshot_seconds=30)
            asyncio.run(run_guarded(recorder, duration=duration))
            journal = list(records(directory))
    elif transport == "rest":
        journal = []
        deadline = time.monotonic() + duration
        with ReadOnlyAPI() as api:
            while time.monotonic() < deadline:
                cycle = time.monotonic()
                now = time.time()
                for event in events:
                    market = parse_market(event, require_resolved=False)
                    if not market["start"] <= now < market["end"]:
                        continue
                    for asset in (market["up_token"], market["down_token"]):
                        sent = time.time_ns()
                        raw = api.get("clob", "/book", {"token_id": asset})
                        received = time.time_ns()
                        if str(raw["asset_id"]) != asset or raw["market"] != event["markets"][0]["conditionId"]:
                            raise ValueError("REST book identity mismatch")
                        payload = {"event_type": "book", **raw}
                        row = {"version": 1, "session": "rest-snapshots", "seq": len(journal) + 1,
                               "sent_ns": sent, "received_ns": received, "kind": "event",
                               "source": "rest_snapshot", "payload": payload}
                        row["sha256"] = digest(row)
                        journal.append(row)
                time.sleep(max(0, .5 - (time.monotonic() - cycle)))
    else:
        raise ValueError("unknown public book transport")
    if not journal:
        raise ValueError("no public books recorded")
    stop = int(journal[-1]["received_ns"] // 10**9)
    points, pages, cursor = [], [], start - 1000
    while cursor < stop:
        params = {"symbol": "BTCUSDT", "interval": "1s", "startTime": cursor * 1000,
                  "endTime": (stop - 1) * 1000, "limit": 1000}
        rows = source.get("cex", params)
        if not rows or int(rows[-1][0]) // 1000 < cursor:
            raise ValueError("missing CEX page")
        pages.append({"params": params, "rows": len(rows),
                      "response_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()})
        points.extend([[(int(r[6]) + 1) / 1000, r[4]] for r in rows
                       if (int(r[6]) + 1) / 1000 <= stop])
        cursor = int(rows[-1][0]) // 1000 + 1
    return {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "selection": "current and next consecutive BTC five-minute markets; fixed wall-clock duration",
            "requested_duration_seconds": duration, "book_transport": transport,
            "markets": events, "events": journal,
            "compliance": compliance, "cex": {"points": points, "pages": pages,
            "source": "https://data-api.binance.vision/api/v3/klines", "symbol": "BTCUSDT",
            "interval": "1s", "timestamp_field": "(close_time_ms + 1) / 1000", "price_field": "close"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--duration", type=float, default=180)
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--transport", choices=["websocket", "rest"], default="websocket")
    replay = sub.add_parser("analyze")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--shares", type=float, default=100)
    replay.add_argument("--delays", type=float, nargs="+", default=[0, .05, .25, 1, 2])
    replay.add_argument("--ticks", type=int, nargs="+", default=[0, 1, 3, 5])
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            with StudySources() as source:
                result = collect(source, args.duration, args.transport)
        else:
            from .fillable import analyze
            result = analyze(read_recording(args.input), shares=args.shares,
                             delays=args.delays, ticks=args.ticks)
            result["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError):
        parser.exit(2, "Fill study failed: invalid/incomplete recording or public read unavailable. "
                    "Polymarket reads use scoped ARB_POLYMARKET_PROXY; when unset, direct HTTPS/WS is used.\n")
    print(json.dumps({"recorded_events": len(result["events"])} if args.command == "record"
                     else {"status": result["status"], "signals": len(result["signals"])}))
    return 0 if args.command == "record" or result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
