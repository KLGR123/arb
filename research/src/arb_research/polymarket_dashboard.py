"""Export an ARB-7 local snapshot, or reproduce one from recorded public responses.

Neither mode uses the network. The collector and its trading guard are unchanged.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .polymarket.store import DataStore


class ReadOnlyStore(DataStore):
    def __init__(self, root: Path):
        # DataStore.__init__ creates schemas; a consumer must not do that.
        self.root = root.resolve(strict=True)
        self.db_path = self.root / "manifest.sqlite3"
        self._snapshot = None

    @contextmanager
    def connect(self):
        if self._snapshot is not None:
            yield self._snapshot
            return
        db = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def snapshot(self):
        # Coordinate with ARB-7's existing exclusive sync lock without creating
        # or modifying a file. A database snapshot alone can pin a mid-sync state.
        with (self.root / "sync.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Sync is active; export after it completes") from None
            try:
                with self.connect() as db:
                    db.execute("BEGIN")
                    self._snapshot = db
                    try:
                        yield self
                    finally:
                        self._snapshot = None
                        db.rollback()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def export_dashboard(store, event_id, market_id=None, *, limit=10000,
                     collected_at=None, compliance=None, recorded=None):
    with store.snapshot():
        return _export_dashboard(store, event_id, market_id, limit=limit,
                                 collected_at=collected_at, compliance=compliance, recorded=recorded)


def _export_dashboard(store, event_id, market_id, *, limit, collected_at, compliance, recorded):
    event = store.event(event_id)
    market = next((m for m in event.markets if m.id == market_id), None) if market_id else next(iter(event.markets), None)
    if market is None:
        raise ValueError("Selected market is absent from the synced event")
    checkpoints = [store.checkpoint(t.id) for m in event.markets for t in m.tokens]
    incomplete = (not checkpoints or any(s["pending"] or not s["through"] for s in checkpoints)
                  or len({s["through"] for s in checkpoints}) != 1)
    return {
        "schema_version": 2,
        "source": "recorded-sync" if recorded else "local-sync",
        "as_of": recorded["recorded_at"] if recorded else datetime.now(UTC).isoformat(),
        "currency": "pUSD",
        "event": event.model_dump(mode="json"),
        "market_id": market.id,
        "outcome_index": 0,
        "prices": store.history(event, market_id=market.id, limit=limit).model_dump(mode="json"),
        "resolutions": store.resolutions(event, market.id).model_dump(mode="json"),
        "compliance": compliance,
        "quality": {
            # Query time / file mtime / price time are not collection times.
            "collected_at": collected_at,
            "stale_after_seconds": 3600,
            "expected_price_interval_seconds": None,
            # Aligned old watermarks cannot prove that a later refresh did not
            # fail before its first checkpoint. ARB-7 has no durable completion
            # receipt: local exports remain unknown unless demonstrably partial.
            # Only this process's successful, isolated recorded replay is known.
            "sync_incomplete": True if incomplete else (False if recorded else None),
        },
        "provenance": recorded or {"recorded_at": None, "input_sha256": None},
        # ARB-7 does not provide forecasts, paper fills/PnL, or L2 snapshots.
        "forecasts": [], "pnl": [], "book": [],
    }


def replay_recording(path: Path, data_dir: Path, event_id: str):
    import httpx
    from .polymarket.client import PublicDataClient
    from .polymarket.sync import sync_event

    raw = path.read_bytes()
    recorded = json.loads(raw)
    at = recorded["provenance"]["recorded_at_utc"]
    target = int(datetime.fromisoformat(at).timestamp())

    def key(url):
        parts = urlsplit(str(url))
        return parts.scheme, parts.netloc, parts.path, tuple(sorted(parse_qsl(parts.query)))

    responses = {key(r["url"]): r for r in recorded["responses"]}

    def transport(request):
        if request.method != "GET":
            raise ValueError("Replay permits GET only")
        if request.url.host == "polymarket.com" and request.url.path == "/api/geoblock":
            geo = recorded["compliance"]["geoblock"]
            return httpx.Response(200, json={k: geo[k] for k in ["blocked", "country", "region"]})
        row = responses.get(key(request.url))
        if row is None:
            raise ValueError("Unrecorded public request; replay cannot use the network")
        return httpx.Response(row["status"], json=row["body"])

    class OfflineLimiter:
        def acquire(self):
            pass

    store = DataStore(data_dir)
    with PublicDataClient(transport=httpx.MockTransport(transport), limiter=OfflineLimiter()) as client:
        sync_event(client, store, event_id, now=target)
    return at, recorded["compliance"], {
        "recorded_at": at, "input_sha256": hashlib.sha256(raw).hexdigest(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--market")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--recording", type=Path, help="Offline replay into a NEW data directory")
    parser.add_argument("--collected-at", help="Explicit known collection timestamp, never inferred")
    parser.add_argument("--sync-result", type=Path, help="Optional ARB-7 sync stdout JSON containing compliance")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.is_relative_to(args.data_dir.resolve()):
        parser.error("Output must be outside the source data directory")
    if any(path and output == path.resolve() for path in [args.recording, args.sync_result]):
        parser.error("Output must not overwrite a recording or sync result")
    if args.recording:
        if args.collected_at or args.sync_result:
            parser.error("Recording replay preserves its own collection time and compliance")
        if args.data_dir.exists():
            parser.error("Replay requires a new directory, to avoid mixing recordings and live syncs")
        collected, compliance, recorded = replay_recording(args.recording, args.data_dir, args.event)
    else:
        collected, compliance, recorded = args.collected_at, None, None
        if args.sync_result:
            receipt = json.loads(args.sync_result.read_text(encoding="utf-8"))
            event_id = ReadOnlyStore(args.data_dir).event(args.event).id
            matches = [r for r in receipt if r["event_id"] == event_id]
            if len(matches) != 1:
                parser.error("Sync result must contain exactly one entry for the selected event")
            compliance = matches[0]["compliance"]
        if collected is not None:
            parsed = datetime.fromisoformat(collected)
            if parsed.tzinfo is None:
                parser.error("--collected-at requires an ISO timestamp with a timezone")
    data = export_dashboard(ReadOnlyStore(args.data_dir), args.event, args.market, limit=args.limit,
                            collected_at=collected, compliance=compliance, recorded=recorded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Readers see either the old complete envelope or the new complete one.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                     delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(handle.name, args.output)
    print(f"Exported {len(data['prices']['data'])} prices; source={data['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
