"""One bounded public recorder, plus an offline UTC daily observation report."""

import argparse
import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import logging
from pathlib import Path
import signal
import sys

from .__main__ import positive
from .core import EVENTS, Rebuilder
from .recorder import Recorder, resolve_assets
from .storage import StorageLimit, records

DAY = 86400 * 10**9


def daily_report(root, start, days=7):
    """Validate every event; recompute gaps without retaining unbounded gap lists.

    Session windows measure recorder observation, not lossless upstream coverage.
    Missing calendar time remains explicit even when no gap event exists.
    """
    if not 1 <= days <= 366:
        raise ValueError("days must be in 1..366")
    first = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()) * 10**9
    rows = [{"date": str(start + timedelta(days=i)), "events": 0, "gaps": Counter(),
             "connections": 0, "reconnects": 0, "recoveries": 0,
             "hash_conflict_recoveries": 0, "max_recovery_seconds": 0,
             "unavailable_asset_seconds": 0, "observed_seconds": 0,
             "sessions": set()} for i in range(days)]
    engine, sessions, selected, unavailable, conflicts = Rebuilder(), {}, set(), {}, set()
    count, previous, last_ns = 0, None, None
    health = {}
    recovery_total = hash_recovery_total = 0
    max_recovery = 0
    all_gaps, event_counts = Counter(), Counter()

    def bucket(ns):
        index = (ns - first) // DAY
        return rows[index] if 0 <= index < days else None

    def add_interval(begin, end, field, multiplier=1):
        while begin < end:
            boundary = (begin // DAY + 1) * DAY
            stop = min(end, boundary)
            target = bucket(begin)
            if target is not None:
                target[field] += (stop - begin) / 10**9 * multiplier
            begin = stop

    for row in records(root):
        ns, event, session = row["received_ns"], row["payload"], row["session"]
        kind = event["event_type"]
        if kind == "health":
            health[session] = {"received_ns": ns, "counters": event.get("counters", {}),
                               "elapsed_seconds": event.get("elapsed_seconds"),
                               "available_assets": event.get("available_assets", [])}
        if previous is not None:
            add_interval(previous, ns, "unavailable_asset_seconds", len(unavailable))
        previous, last_ns = ns, ns
        count += 1
        event_counts[kind] += 1
        state = sessions.setdefault(session, {"first_ns": ns, "last_ns": ns, "started": False,
                                              "stopped": False, "assets": [], "seeded_assets": [],
                                              "first_market_ns": None, "last_market_ns": None})
        state["last_ns"] = ns
        if row["kind"] == "event" and kind in EVENTS:
            if state["first_market_ns"] is None:
                state["first_market_ns"] = ns
            state["last_market_ns"] = ns
        if kind == "started":
            state["started"] = True
            state["assets"] = event.get("assets_ids", [])
            selected.update(state["assets"])
            for asset in selected:
                unavailable.setdefault(asset, ns)
        target = bucket(ns)
        if target is not None:
            target["events"] += 1
            target["sessions"].add(session)
            target["connections"] += kind == "connected"
            target["reconnects"] += kind == "disconnected"
        engine.apply(row)
        if kind == "book":
            asset = event.get("asset_id")
            if asset in engine.books and engine.books[asset].available and asset not in state["seeded_assets"]:
                state["seeded_assets"].append(asset)
        for gap in engine.gaps:
            all_gaps[gap["kind"]] += 1
            if target is not None:
                target["gaps"][gap["kind"]] += 1
            if gap["kind"] == "server_hash_conflict":
                conflicts.add(gap["asset_id"])
        engine.gaps.clear()
        # Rebuilder may invalidate then heal within one full-book event.
        if kind == "book" and event.get("asset_id") in engine.books:
            asset = event["asset_id"]
            if engine.books[asset].available and asset in conflicts:
                hash_recovery_total += 1
                conflicts.remove(asset)
                if target is not None:
                    target["hash_conflict_recoveries"] += 1
        for asset in selected:
            available = asset in engine.books and engine.books[asset].available
            if available and asset in unavailable:
                duration = (ns - unavailable.pop(asset)) / 10**9
                recovery_total += 1
                max_recovery = max(max_recovery, duration)
                if target is not None:
                    target["recoveries"] += 1
                    target["max_recovery_seconds"] = max(target["max_recovery_seconds"], duration)
            elif not available:
                unavailable.setdefault(asset, ns)
        if kind == "stopped":
            state["stopped"] = True
            state["stop_reason"] = event.get("stop_reason", "unknown_legacy_capture")

    # Merge session windows to avoid double-counting accidentally overlapping runs.
    windows = sorted((s["first_ns"], s["last_ns"]) for s in sessions.values() if s["started"])
    merged = []
    for begin, end in windows:
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    for begin, end in merged:
        add_interval(begin, end, "observed_seconds")
    for target in rows:
        target["sessions"] = sorted(target["sessions"])
        target["gaps"] = dict(target["gaps"])
        target["unobserved_seconds"] = 86400 - target["observed_seconds"]
        target["status"] = "missing" if not target["events"] else "observed"
    spans = [{"session": sid, **s,
              "seconds": (s["last_ns"] - s["first_ns"]) / 10**9,
              "market_span_seconds": (s["last_market_ns"] - s["first_market_ns"]) / 10**9
              if s["first_market_ns"] is not None else 0} for sid, s in sessions.items()]
    return {"version": 1, "utc_start": str(start), "days": rows,
            "summary": {"validated_records": count, "events": dict(event_counts),
                        "gaps": dict(all_gaps), "sessions": spans,
                        "longest_session_seconds": max((s["seconds"] for s in spans if s["started"]), default=0),
                        "recoveries_including_initial_seed": recovery_total,
                        "hash_conflict_recoveries": hash_recovery_total,
                        "max_recovery_seconds": max_recovery,
                        "last_health_by_session": health,
                        "unresolved_hash_assets": sorted(conflicts),
                        "last_received_ns": last_ns, "selected_assets": sorted(selected)},
            "integrity": "all event row SHA-256 and local sequence recomputed; sequence gaps are reported",
            "limitations": ["Observed discontinuities only; no guaranteed upstream sequence or cryptographic server hash validation.",
                            "Session duration includes reconnects; observed_seconds does not mean gap-free data.",
                            "Final incomplete crash tail ends at last durable event; no coverage extrapolation.",
                            "Unavailability includes initial seeding and time between sessions; summed over assets.",
                            "Summary scans all supplied history; daily rows cover only the requested UTC calendar window."]}


async def supervise(recorder, *, duration=None, interval=30, **kwargs):
    """Keep health/quota checks alive during WS backoff as well as live traffic."""
    task = asyncio.create_task(recorder.run(duration=duration, **kwargs))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=interval)
            if not done:
                recorder.checkpoint()
        await task
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def record(args):
    args.output.mkdir(parents=True, exist_ok=True)
    # Acquire before discovery or constructing a Journal; never race a live writer.
    with (args.output / ".record.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
        assets, geo = await resolve_assets(args.token, args.market)
        recorder = Recorder(args.output, assets, health_records=True,
                            snapshot_seconds=float("inf"),
                            journal_options={"segment_bytes": int(args.segment_mib * 1024**2),
                                             "quota_bytes": int(args.quota_gib * 1024**3),
                                             "min_free_bytes": int(args.reserve_gib * 1024**3)})
        recorder.metrics.update(public_read_only=1, geoblock_blocked=int(geo["blocked"]),
                                geoblock_check_failed=int(bool(geo.get("error"))))
        recorder.journal.check_space()
        await supervise(recorder, duration=args.duration, interval=args.health_seconds)
        if set(assets) - recorder.seeded_assets:
            raise ValueError("selected assets never received complete books")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("record")
    live.add_argument("--token", action="append", default=[])
    live.add_argument("--market", action="append", default=[])
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--duration", type=positive)
    live.add_argument("--segment-mib", type=positive, default=64)
    live.add_argument("--quota-gib", type=positive, default=20)
    live.add_argument("--reserve-gib", type=positive, default=5)
    live.add_argument("--health-seconds", type=positive, default=30)
    report = commands.add_parser("report")
    report.add_argument("input", type=Path)
    report.add_argument("--start", type=date.fromisoformat, required=True)
    report.add_argument("--days", type=int, default=7)
    report.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        if args.command == "record":
            if not args.token and not args.market:
                parser.error("record requires --token or --market")
            asyncio.run(record(args))
        else:
            result = daily_report(args.input, args.start, args.days)
            body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.write_text(body, encoding="utf-8")
            else:
                print(body, end="")
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0  # Expected graceful operator stop; supervisor must not restart it.
    except StorageLimit:
        print("StorageLimit: recording stopped; quota or disk reserve reached; retain and archive evidence",
              file=sys.stderr)
        return 73
    except Exception as exc:
        # Network exceptions can contain the private proxy; disclose types only.
        print(f"{type(exc).__name__}: {args.command} failed; inspect storage, writer lock and public network configuration",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
