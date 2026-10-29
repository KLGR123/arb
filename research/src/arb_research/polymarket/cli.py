"""Sync public data or query the local snapshot as JSON."""

import argparse
import json
import os
import sys
from pathlib import Path

from .client import PublicDataClient
from .store import DataStore
from .sync import sync_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("ARB_POLYMARKET_DATA_DIR", ".state/polymarket"))
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Sync complete available history for each event ID or slug")
    sync.add_argument("--event", action="append", required=True)
    sync.add_argument("--record-dir", type=Path, help="Record public responses for offline replay")
    query = commands.add_parser("query", help="Query local snapshots without network access")
    selector = query.add_mutually_exclusive_group(required=True)
    selector.add_argument("--event")
    selector.add_argument("--market")
    query.add_argument("--kind", choices=["metadata", "prices", "resolutions"], default="prices")
    query.add_argument("--token-id")
    query.add_argument("--start", type=int, default=1)
    query.add_argument("--end", type=int, default=253402300799)
    query.add_argument("--offset", type=int, default=0)
    query.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        store = DataStore(args.data_dir)
        if args.command == "sync":
            with PublicDataClient(record_dir=args.record_dir,
                                  proxy=os.environ.get("ARB_POLYMARKET_PROXY")) as client:
                result = [sync_event(client, store, selector).model_dump() for selector in args.event]
        else:
            event = store.event(args.event) if args.event else store.market_event(args.market)
            if args.kind == "metadata":
                result = event if args.event else next(m for m in event.markets if m.id == args.market)
            elif args.kind == "resolutions":
                result = store.resolutions(event, args.market)
            else:
                result = store.history(event, market_id=args.market, token_id=args.token_id,
                                       start=args.start, end=args.end, offset=args.offset, limit=args.limit)
            result = result.model_dump(mode="json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f"polymarket-data: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
