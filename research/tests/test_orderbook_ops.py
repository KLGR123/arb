"""Offline operational boundaries, clock rollover and recovery accounting."""

import asyncio
from datetime import date
import json
from pathlib import Path

import pytest

from arb_research.orderbook.ops import daily_report, supervise
from arb_research.orderbook.storage import Journal, StorageLimit, records


BOOK = {"event_type": "book", "asset_id": "123", "market": "fixture",
        "timestamp": "1000", "hash": "same", "bids": [{"price": ".4", "size": "2"}],
        "asks": [{"price": ".6", "size": "2"}]}
SECOND = 10**9
MIDNIGHT = 1793404800 * SECOND  # 2026-10-31 UTC


def test_size_and_utc_rotation_replay_without_disconnect(tmp_path):
    journal = Journal(tmp_path, segment_bytes=600)
    for i in range(12):
        journal.write({"event_type": "health", "padding": "x" * 50},
                      MIDNIGHT + (i - 6) * SECOND, "control")
    journal.close()
    paths = list(tmp_path.glob("date=*/market=*/events.*.jsonl"))
    assert len(paths) >= 6
    assert len(list(tmp_path.glob("date=*"))) == 2
    assert all(p.stat().st_size <= 600 for p in paths)
    assert [r["seq"] for r in records(tmp_path)] == list(range(1, 13))


def test_quota_preserves_prior_evidence_across_restarts(tmp_path):
    journal = Journal(tmp_path, quota_bytes=650)
    journal.write(BOOK, MIDNIGHT)
    journal.close()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")}
    another = Journal(tmp_path, quota_bytes=650)
    with pytest.raises(StorageLimit):
        another.write(BOOK, MIDNIGHT + SECOND)
    another.close()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == before


def test_disk_reserve_and_oversize_fail_without_deleting(tmp_path, monkeypatch):
    from arb_research.orderbook import storage
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: type("Disk", (), {"free": 10})())
    journal = Journal(tmp_path, min_free_bytes=11, segment_bytes=10)
    with pytest.raises(StorageLimit, match="reserve"):
        journal.check_space()
    with pytest.raises(StorageLimit, match="single record"):
        journal.write(BOOK, MIDNIGHT)
    journal.close()


def test_daily_gaps_reconnect_hash_recovery_cross_midnight_and_missing_days(tmp_path):
    j = Journal(tmp_path, segment_bytes=600)
    j.write({"event_type": "started", "assets_ids": ["123"]}, MIDNIGHT - 10 * SECOND, "control")
    j.write({"event_type": "connected", "assets_ids": ["123"]}, MIDNIGHT - 9 * SECOND, "control")
    j.write(BOOK, MIDNIGHT - 8 * SECOND)
    # Same server hash with different depth is an observed hash conflict.
    j.write({**BOOK, "bids": [{"price": ".4", "size": "3"}]}, MIDNIGHT - 5 * SECOND)
    j.write({"event_type": "disconnected"}, MIDNIGHT - 2 * SECOND, "control")
    j.write({"event_type": "connected", "assets_ids": ["123"]}, MIDNIGHT + SECOND, "control")
    j.write({**BOOK, "hash": "fresh"}, MIDNIGHT + 3 * SECOND)
    j.write({"event_type": "stopped"}, MIDNIGHT + 10 * SECOND, "control")
    j.close()
    result = daily_report(tmp_path, date(2026, 10, 30))
    summary = result["summary"]
    assert summary["longest_session_seconds"] == 20
    assert summary["sessions"][0]["market_span_seconds"] == 11
    assert summary["sessions"][0]["seeded_assets"] == ["123"]
    assert summary["gaps"] == {"snapshot_mismatch": 1, "server_hash_conflict": 1, "disconnected": 1}
    assert summary["hash_conflict_recoveries"] == 1
    assert summary["unresolved_hash_assets"] == []
    assert summary["max_recovery_seconds"] == 5
    first, second, *missing = result["days"]
    assert first["observed_seconds"] == second["observed_seconds"] == 10
    assert first["unavailable_asset_seconds"] == 4  # initial seed 2 + disconnect 2
    assert second["unavailable_asset_seconds"] == 3
    assert first["reconnects"] == 1 and second["recoveries"] == 1
    assert all(r["status"] == "missing" and r["unobserved_seconds"] == 86400 for r in missing)
    json.dumps(result)  # Report contains no internal sets or Decimal objects.


def test_restart_downtime_and_crash_tail_are_not_continuous_coverage(tmp_path):
    for offset in (0, 20):
        j = Journal(tmp_path)
        j.write({"event_type": "started", "assets_ids": ["123"]}, MIDNIGHT + offset * SECOND, "control")
        j.write({"event_type": "connected", "assets_ids": ["123"]}, MIDNIGHT + (offset + 1) * SECOND, "control")
        j.write(BOOK, MIDNIGHT + (offset + 2) * SECOND)
        # First ends normally; second crashes after last durable health.
        j.write({"event_type": "stopped" if offset == 0 else "health"}, MIDNIGHT + (offset + 10) * SECOND, "control")
        j.close()
    result = daily_report(tmp_path, date(2026, 10, 31))
    assert result["summary"]["longest_session_seconds"] == 10
    assert result["days"][0]["observed_seconds"] == 20
    assert result["days"][0]["unobserved_seconds"] == 86380
    assert result["days"][0]["unavailable_asset_seconds"] == 14  # two seeds + between runs
    path = next(tmp_path.glob("date=*/market=*/events.*.jsonl"))
    path.write_text(path.read_text() + '{"truncated":')
    with pytest.raises(ValueError, match="invalid journal"):
        daily_report(tmp_path, date(2026, 10, 31))


def test_supervisor_health_during_backoff_and_graceful_stop():
    class Waiting:
        def __init__(self):
            self.checks, self.closed = 0, False

        async def run(self, **kwargs):
            try:
                await asyncio.Future()
            finally:
                self.closed = True

        def checkpoint(self):
            self.checks += 1
            if self.checks == 3:
                raise StorageLimit("full")

    recorder = Waiting()
    with pytest.raises(StorageLimit):
        asyncio.run(supervise(recorder, interval=.001))
    assert recorder.checks == 3 and recorder.closed


def test_health_counters_are_journaled_and_proxy_errors_are_sanitized(tmp_path, monkeypatch, capsys):
    from arb_research.orderbook import ops
    from arb_research.orderbook.recorder import Recorder
    monkeypatch.delenv("ARB_POLYMARKET_PROXY", raising=False)
    recorder = Recorder(tmp_path, ["123"], health_records=True)
    recorder.control("started", assets_ids=["123"])
    recorder.metrics["pongs"] = 5
    recorder.checkpoint()
    recorder.journal.close()
    assert list(records(tmp_path))[-1]["payload"]["counters"]["pongs"] == 5

    async def fail(args):
        raise OSError("socks5h://private-secret@route.invalid:1080")

    monkeypatch.setattr(ops, "record", fail)
    assert ops.main(["record", "--token", "123", "--output", str(tmp_path)]) == 1
    assert "private-secret" not in capsys.readouterr().err


def test_report_real_fixture_matches_independent_capture():
    root = Path(__file__).parent / "fixtures/orderbook-live"
    report = daily_report(root, date(2026, 10, 29))
    capture = json.loads((root / "capture.json").read_text())
    assert report["summary"]["validated_records"] == 3456
    assert report["summary"]["gaps"] == {"server_hash_conflict": 2, "disconnected": 1}
    assert report["summary"]["hash_conflict_recoveries"] == 2
    assert report["days"][0]["reconnects"] == capture["reconnects"]
    assert report["summary"]["unresolved_hash_assets"] == []


def test_ops_lock_rejects_second_writer_before_network(tmp_path, monkeypatch):
    import fcntl
    from arb_research.orderbook import ops

    async def unexpected(*args):
        pytest.fail("network discovery must not happen without exclusive ownership")

    monkeypatch.setattr(ops, "resolve_assets", unexpected)
    with (tmp_path / ".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ops.main(["record", "--token", "123", "--output", str(tmp_path)]) == 1
    assert not list(tmp_path.glob("date=*"))
