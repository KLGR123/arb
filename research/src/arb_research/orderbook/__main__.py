import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import sys

from .storage import replay


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def timestamp(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("timezone required")
        delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return ((delta.days * 86400 + delta.seconds) * 10 ** 9 + delta.microseconds * 1000)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO-8601 timestamp with timezone") from exc


async def record(args):
    from .recorder import Recorder, resolve_assets, run_guarded

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    assets, geo = await resolve_assets(args.token, args.market)
    recorder = Recorder(args.output, assets, snapshot_seconds=args.snapshot_seconds)
    recorder.metrics["public_read_only"] = 1
    recorder.metrics["geoblock_blocked"] = int(geo["blocked"])
    await run_guarded(recorder, duration=args.duration)
    missing = set(assets) - recorder.seeded_assets
    if missing:
        raise ValueError(f"incomplete recording: no full book for {len(missing)} selected token(s)")
    print(json.dumps({"geoblock": geo, "access_mode": "public_read_only", "trading_allowed": False,
                      "assets_ids": assets, "metrics": dict(recorder.metrics)}))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Polymarket market recorder and receive-time L2 replay")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("record")
    live.add_argument("--token", action="append", default=[], help="outcome token ID (repeatable)")
    live.add_argument("--market", action="append", default=[], help="condition ID; resolve tokens through Gamma")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--duration", type=positive, help="seconds; default runs until SIGINT/SIGTERM")
    live.add_argument("--snapshot-seconds", type=positive, default=60)
    for name in ("replay", "gaps"):
        offline = commands.add_parser(name)
        offline.add_argument("input", type=Path)
        offline.add_argument("--at", type=timestamp, help="as observed at this receive-time (inclusive)")
        offline.add_argument("--at-ns", type=int, help="exact receive timestamp in nanoseconds")
        if name == "replay":
            offline.add_argument("--token", help="filter output only; scan complete journal for integrity")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.command == "record":
            if not args.token and not args.market:
                parser.error("record requires --token or --market")
            asyncio.run(record(args))
        else:
            if args.at is not None and args.at_ns is not None:
                parser.error("choose either --at or --at-ns")
            result = replay(args.input, args.at_ns if args.at_ns is not None else args.at)
            if args.command == "gaps":
                counts = {}
                for gap in result["gaps"]:
                    counts[gap["kind"]] = counts.get(gap["kind"], 0) + 1
                result = {"counts": counts, "gaps": result["gaps"],
                          "coverage": "observed discontinuities only; upstream sequence not guaranteed"}
            elif args.token:
                if args.token not in result["books"]:
                    raise ValueError("token has no observations at requested time")
                result["books"] = {args.token: result["books"][args.token]}
            print(json.dumps(result, ensure_ascii=False))
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 143
    except Exception as exc:
        # Proxy libraries may include the private route in exception messages.
        detail = "recording failed; check public network/configuration and capture completeness" \
            if args.command == "record" else str(exc)
        print(f"{type(exc).__name__}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
