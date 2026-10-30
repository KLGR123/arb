"""Offline protocol/transport tests. Never connect to Polymarket or read secrets."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from arb_research.orderbook.recorder import Recorder, TokenBucket, public_get, run_guarded
from arb_research.orderbook.storage import Journal, records, replay


MARKET = "0xfixture"
BOOK = {"event_type": "book", "asset_id": "123", "market": MARKET, "timestamp": "1000",
        "hash": "book-a", "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.60", "size": "200"}]}
DELTA = {"event_type": "price_change", "market": MARKET, "timestamp": "1001",
         "price_changes": [{"asset_id": "123", "price": "0.50", "size": "20", "side": "BUY",
                            "hash": "book-b", "best_bid": "0.50", "best_ask": "0.60"}]}


@pytest.fixture(autouse=True)
def isolate_private_network_configuration(monkeypatch):
    # Tests must never use the runtime's real private route, including local WS peers.
    monkeypatch.delenv("ARB_POLYMARKET_PROXY", raising=False)


def write_fixture(root):
    """Record a deterministic public-protocol fixture through the production writer."""
    journal = Journal(root)
    journal.write({"event_type": "connected", "assets_ids": ["123", "456"]}, 1_000, "control")
    journal.write(BOOK, 2_000)
    journal.write({**BOOK, "asset_id": "456", "market": "0xother"}, 2_000)
    journal.write(DELTA, 3_000)
    replace = copy.deepcopy(DELTA)
    replace["price_changes"][0].update(size="7", hash="book-c")
    journal.write(replace, 4_000)
    remove = copy.deepcopy(DELTA)
    remove["price_changes"][0].update(size="0", hash="book-d", best_bid="0.40")
    journal.write(remove, 5_000)
    journal.write({"event_type": "tick_size_change", "market": MARKET, "asset_id": "123",
                   "new_tick_size": "0.001", "old_tick_size": "0.01"}, 6_000)
    journal.write({"event_type": "last_trade_price", "market": MARKET, "asset_id": "123",
                   "price": "0.55", "size": "3", "side": "BUY"}, 7_000)
    journal.close()


def test_receive_time_replay_replaces_deletes_and_keeps_markets_separate(tmp_path):
    write_fixture(tmp_path)
    assert list(records(tmp_path))[2]["seq"] == 3  # tie ordered by local sequence
    before = replay(tmp_path, 1_999)
    assert not before["books"]["123"]["available"]
    at = replay(tmp_path, 3_000)
    assert at["books"]["123"]["bids"][0] == {"price": "0.5", "size": "20"}
    assert replay(tmp_path, 4_000)["books"]["123"]["bids"][0]["size"] == "7"
    final = replay(tmp_path)
    assert final["books"]["123"]["bids"] == [{"price": "0.4", "size": "100"}]
    assert final["books"]["456"]["market"] == "0xother"
    assert final["books"]["123"]["tick_size"] == "0.001"
    assert final["books"]["123"]["last_trade_price"] == "0.55"
    assert not final["gaps"]


def test_gaps_invalidate_until_full_book_and_resubscription(tmp_path):
    journal = Journal(tmp_path)
    journal.write(BOOK, 1)
    journal.write({"event_type": "disconnected"}, 2, "control")
    journal.write(DELTA, 3)
    journal.write({"event_type": "connected", "assets_ids": ["123"]}, 4, "control")
    journal.write(BOOK, 5)
    journal.close()
    at_gap = replay(tmp_path, 3)
    assert at_gap["books"]["123"]["bids"] == []
    assert not at_gap["books"]["123"]["available"]
    final = replay(tmp_path)
    assert final["books"]["123"]["available"]
    assert {g["kind"] for g in final["gaps"]} == {"disconnected", "delta_without_book"}


@pytest.mark.parametrize("mode", ["best", "hash", "snapshot", "malformed"])
def test_continuity_checks(tmp_path, mode):
    journal = Journal(tmp_path)
    journal.write(BOOK, 1)
    event = copy.deepcopy(DELTA)
    expected = "best_price_mismatch"
    if mode == "best":
        event["price_changes"][0]["best_bid"] = "0.51"
    elif mode == "hash":
        event["price_changes"][0]["hash"] = BOOK["hash"]
        expected = "server_hash_conflict"
    elif mode == "snapshot":
        event = {**BOOK, "bids": [], "hash": "new"}
        expected = "snapshot_mismatch"
    else:
        event["price_changes"] = [None]
        expected = "invalid_event"
    journal.write(event, 2)
    journal.close()
    result = replay(tmp_path)
    assert expected in {g["kind"] for g in result["gaps"]}
    assert result["books"]["123"]["available"] == (mode == "snapshot")


def test_atomic_multi_level_delta_and_numeric_hash_equivalence(tmp_path):
    journal = Journal(tmp_path)
    journal.write(BOOK, 1)
    journal.write({**BOOK, "bids": [{"price": "0.4", "size": "100.0"}]}, 2)
    event = copy.deepcopy(DELTA)
    event["price_changes"].append({**event["price_changes"][0], "price": "0.51", "size": "1",
                                    "hash": "book-z", "best_bid": "0.51"})
    journal.write(event, 3)
    journal.close()
    result = replay(tmp_path)
    assert not result["gaps"]
    assert len(result["books"]["123"]["bids"]) == 3


def test_checksum_and_missing_partition_are_detected(tmp_path):
    write_fixture(tmp_path)
    other = next(tmp_path.glob("date=*/market=0xother/events.*"))
    other.unlink()
    assert replay(tmp_path)["gaps"][0]["kind"] == "journal_sequence"
    event_file = next(tmp_path.glob("date=*/market=0xfixture/events.*"))
    event_file.write_text(event_file.read_text().replace('"size":"100"', '"size":"101"', 1))
    with pytest.raises(ValueError, match="checksum"):
        replay(tmp_path)


def test_truncated_journal_fails_loudly(tmp_path):
    write_fixture(tmp_path)
    event_file = next(tmp_path.glob("date=*/market=0xfixture/events.*"))
    event_file.write_text(event_file.read_text()[:-1])
    with pytest.raises(ValueError, match="truncated"):
        replay(tmp_path)


def test_utc_rollover_and_restart_sessions(tmp_path):
    midnight = 86_400 * 10 ** 9
    first = Journal(tmp_path)
    first.write(BOOK, midnight - 1)
    first.close()
    second = Journal(tmp_path)
    second.write({"event_type": "connected", "assets_ids": ["123"]}, midnight, "control")
    second.write(DELTA, midnight + 1)
    second.write(BOOK, midnight + 2)
    second.close()
    assert len(list(tmp_path.glob("date=*"))) == 2
    assert not replay(tmp_path, midnight + 1)["books"]["123"]["available"]
    assert replay(tmp_path)["books"]["123"]["available"]


def test_crash_restart_invalidates_old_book_before_connect(tmp_path):
    first = Journal(tmp_path)
    first.write(BOOK, 1)
    first.close()  # Simulate a crash: no stopped or disconnected event.
    second = Journal(tmp_path)
    second.write({"event_type": "started", "assets_ids": ["123"]}, 2, "control")
    second.close()
    result = replay(tmp_path, 2)
    assert result["last_record_received_ns"] == 2
    assert result["books"]["123"]["reason"] == "recorder_restarted"
    assert not result["books"]["123"]["available"]
    assert result["books"]["123"]["bids"] == []


class FakeSocket:
    def __init__(self, messages=(), *, pong=True, fail=False):
        self.messages = list(messages)
        self.sent = []
        self.pong = pong
        self.fail = fail

    async def send(self, value):
        self.sent.append(value)
        if value == "PING" and self.pong:
            self.messages.append("PONG")

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.fail:
            self.fail = False
            raise ConnectionResetError("fixture disconnect")
        await asyncio.sleep(.001)
        return await self.recv()


class Connector:
    def __init__(self, sockets):
        self.sockets, self.calls = sockets, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        socket = self.sockets[min(len(self.calls) - 1, len(self.sockets) - 1)]

        class Context:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                return False
        return Context()


def test_reconnect_heartbeat_periodic_snapshot_and_replay(tmp_path):
    first = FakeSocket([json.dumps([BOOK, DELTA])], fail=True)
    second = FakeSocket([json.dumps(BOOK)])
    connector = Connector([first, second])
    recorder = Recorder(tmp_path, ["123"], snapshot_seconds=.008, heartbeat_seconds=.005,
                        pong_seconds=.02, budget=TokenBucket(10000, 10000))

    delays = []

    async def no_backoff(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    asyncio.run(run_guarded(recorder, duration=.2, connect=connector, sleep=no_backoff))
    assert len(connector.calls) == 2
    assert all(call[1]["proxy"] is None for call in connector.calls)
    assert json.loads(second.sent[0]) == {"type": "market", "assets_ids": ["123"]}
    assert "PING" in second.sent and recorder.metrics["pongs"] > 0
    result = replay(tmp_path)
    assert "disconnected" in {g["kind"] for g in result["gaps"]}
    assert result["books"]["123"]["reason"] == "recording_stopped"
    snapshots = list(tmp_path.glob("date=*/market=*/snapshots.*"))
    assert snapshots and sum(len(p.read_text().splitlines()) for p in snapshots) > 1
    assert json.loads((tmp_path / "metrics.json").read_text())["counters"]["reconnects"] == 1
    assert len(delays) == 1 and delays[0] > 0
    # Historical book during the second connection is fully seeded.
    books = [row for row in records(tmp_path) if row["payload"]["event_type"] == "book"]
    assert replay(tmp_path, books[-1]["received_ns"])["books"]["123"]["available"]


def test_missing_pong_forces_reconnect_even_with_book_traffic(tmp_path):
    socket = FakeSocket([json.dumps(BOOK)], pong=False)
    connector = Connector([socket])
    recorder = Recorder(tmp_path, ["123"], heartbeat_seconds=.002, pong_seconds=.004,
                        budget=TokenBucket(10000, 10000))
    # Leave scheduling headroom to actually send PING and observe its timeout.
    asyncio.run(recorder.run(duration=1, connect=connector))
    assert recorder.metrics["gap_heartbeat_timeout"] >= 1
    assert recorder.metrics["reconnects"] >= 1


def test_writer_lock_and_disk_failure_stop(tmp_path, monkeypatch):
    import fcntl

    # The injected write failure must be reached independently of other tests'
    # process-wide rate budget and filesystem/thread scheduling on the NAS.
    recorder = Recorder(tmp_path, ["123"], budget=TokenBucket(10000, 10000))
    with (tmp_path / ".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="another recorder"):
            asyncio.run(run_guarded(recorder, duration=.01, connect=Connector([FakeSocket()])))
    original = recorder.journal.write

    def disk_full(payload, *args):
        if payload["event_type"] == "book":
            raise OSError(28, "disk full")
        return original(payload, *args)

    monkeypatch.setattr(recorder.journal, "write", disk_full)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(recorder.run(duration=1, connect=Connector([FakeSocket([json.dumps(BOOK)])])))
    assert not recorder.metrics["reconnects"]


def test_generic_network_oserror_reconnects(tmp_path):
    import errno

    recorder = Recorder(tmp_path, ["123"], budget=TokenBucket(10000, 10000))

    class Unreachable:
        async def __aenter__(self):
            raise OSError(errno.ENETUNREACH, "fixture network unreachable")

        async def __aexit__(self, *args):
            return False

    asyncio.run(recorder.run(duration=.01, connect=lambda *args, **kwargs: Unreachable()))
    assert recorder.metrics["reconnects"] == 1
    assert any(g["kind"] == "disconnected" for g in replay(tmp_path)["gaps"])


def test_real_local_websocket_peer_reconnects_and_responds_to_ping(tmp_path):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from arb_research.orderbook.recorder import WS_URL

    async def scenario():
        connections = []

        async def peer(ws):
            subscription = json.loads(await ws.recv())
            connections.append(subscription)
            await ws.send(json.dumps([BOOK, DELTA]))
            if len(connections) == 1:
                await ws.close(code=1012, reason="fixture restart")
                return
            async for frame in ws:
                assert frame == "PING"
                await ws.send("PONG")

        async with serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def local_connect(url, **options):
                assert url == WS_URL
                return connect(f"ws://127.0.0.1:{port}", **options)

            async def no_backoff(delay):
                await asyncio.sleep(0)

            recorder = Recorder(tmp_path, ["123"], heartbeat_seconds=.01, pong_seconds=.05,
                                budget=TokenBucket(10000, 10000))
            await run_guarded(recorder, duration=.2, connect=local_connect, sleep=no_backoff)
            assert len(connections) == 2
            assert recorder.metrics["pongs"] > 0
            assert recorder.metrics["reconnects"] == 1
            assert recorder.seeded_assets == {"123"}
        events = list(records(tmp_path))
        last_delta = [r for r in events if r["payload"]["event_type"] == "price_change"][-1]
        book = replay(tmp_path, last_delta["received_ns"])["books"]["123"]
        assert book["available"] and book["bids"][0] == {"price": "0.5", "size": "20"}

    asyncio.run(scenario())


def test_cli_empty_capture_is_failure(tmp_path, monkeypatch, capsys):
    from arb_research.orderbook import __main__ as cli
    from arb_research.orderbook import recorder as module

    async def allowed(*args):
        return ["123"], {"blocked": False, "country": "TW"}

    async def empty(recorder, **kwargs):
        await recorder.run(duration=.005, connect=Connector([FakeSocket()]))

    monkeypatch.setattr(module, "resolve_assets", allowed)
    monkeypatch.setattr(module, "run_guarded", empty)
    assert cli.main(["record", "--token", "123", "--output", str(tmp_path), "--duration", ".005"]) == 1
    assert "capture completeness" in capsys.readouterr().err


def test_http_retry_and_geo_guard_without_network(monkeypatch):
    import httpx
    from arb_research.orderbook import recorder as module

    calls = []

    async def take():
        calls.append("budget")

    class Budget:
        pass

    budget = Budget()
    budget.take = take

    class Response:
        def __init__(self, status):
            self.status = status

        def raise_for_status(self):
            if self.status == 429:
                request = httpx.Request("GET", "https://example.invalid")
                raise httpx.HTTPStatusError("limited", request=request,
                                            response=httpx.Response(429, request=request))

        def json(self):
            return {"ok": True}

    class Client:
        def __init__(self):
            self.count = 0

        async def get(self, *args, **kwargs):
            self.count += 1
            return Response(429 if self.count == 1 else 200)

    assert asyncio.run(public_get(Client(), "https://example.invalid", budget=budget))["ok"]
    assert len(calls) == 2

    from arb_research.compliance import GeoblockClient

    monkeypatch.setattr(module, "GEO_CLIENT", GeoblockClient(
        fetch=lambda timeout: {"blocked": True, "country": "US", "region": "VA"}))
    assets, geo = asyncio.run(module.resolve_assets(["123"], []))
    assert assets == ["123"] and geo["blocked"] is True

    monkeypatch.setattr(module, "GEO_CLIENT", GeoblockClient(
        fetch=lambda timeout: {"blocked": False, "country": "US", "region": "VA"}))
    assets, geo = asyncio.run(module.resolve_assets(["123"], []))
    assert assets == ["123"] and geo["country"] == "US"

    monkeypatch.setattr(module, "GEO_CLIENT", GeoblockClient(
        fetch=lambda timeout: {"blocked": False}, sleep=lambda delay: None))
    assets, geo = asyncio.run(module.resolve_assets(["123"], []))
    assert assets == ["123"] and geo["error"] == "invalid_response"


def test_failed_geoblock_warns_but_records_public_data_and_denies_trading(tmp_path, monkeypatch, capsys, caplog):
    from arb_research.compliance import (ComplianceDenied, ComplianceGuard,
                                         ComplianceSettings, GeoblockClient)
    from arb_research.orderbook import __main__ as cli
    from arb_research.orderbook import recorder as module

    private = "socks5h://fixture-secret@fixture.invalid:1080"

    def unavailable(timeout):
        raise OSError(private)

    geo = GeoblockClient(fetch=unavailable, sleep=lambda _: None)
    monkeypatch.setattr(module, "GEO_CLIENT", geo)

    async def capture(recorder, **kwargs):
        await recorder.run(duration=.01, connect=Connector([FakeSocket([json.dumps(BOOK)])]))

    monkeypatch.setattr(module, "run_guarded", capture)
    assert cli.main(["record", "--token", "123", "--output", str(tmp_path / "capture")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["geoblock"]["error"] == "check_failed"
    assert result["metrics"]["geoblock_check_failed"] == 1
    assert result["trading_allowed"] is False
    assert "continuing public read-only recording" in caplog.text
    assert private not in caplog.text and private not in json.dumps(result)
    guard = ComplianceGuard(ComplianceSettings(True, str(tmp_path / "audit.sqlite3")), geoblock=geo)
    with pytest.raises(ComplianceDenied) as denied:
        guard.require("order", caller="test.failed_geoblock_public_recorder")
    assert denied.value.decision["reason"] == "geoblock_unavailable"


def test_recorder_uses_shared_compliance_budget(monkeypatch):
    from arb_research.orderbook import recorder as module

    calls = []
    monkeypatch.setattr(module.PUBLIC_API_BUDGET, "acquire", lambda: calls.append("global"))

    async def scenario():
        budget = TokenBucket(10000, 10000)
        await budget.take()
        await budget.take()

    asyncio.run(scenario())
    assert calls == ["global", "global"]


def test_explicit_proxy_reaches_http_and_ws_without_global_environment(tmp_path, monkeypatch):
    import httpx
    from arb_research.compliance import GeoblockClient
    from arb_research.orderbook import recorder as module

    route = "socks5h://fixture-user:fixture-password@fixture.invalid:1080"
    monkeypatch.setenv("ARB_POLYMARKET_PROXY", route)
    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.invalid:1234")
    options = []
    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        # No HTTP requests are made for an explicit token list.
        return original(trust_env=False)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(module, "GEO_CLIENT", GeoblockClient(
        fetch=lambda timeout: {"blocked": True, "country": "US", "region": "VA"}))
    assert asyncio.run(module.resolve_assets(["123"], []))[1]["blocked"] is True
    assert options[0]["proxy"] == route and options[0]["trust_env"] is False
    connector = Connector([FakeSocket([json.dumps(BOOK)])])
    asyncio.run(Recorder(tmp_path, ["123"]).run(duration=.01, connect=connector))
    assert connector.calls[0][1]["proxy"] == route
    assert route not in "".join(p.read_text() for p in tmp_path.rglob("*.json*"))


def test_private_proxy_errors_are_not_printed(tmp_path, monkeypatch, capsys):
    from arb_research.orderbook import __main__ as cli
    from arb_research.orderbook import recorder as module

    private = "socks5h://fixture-secret@fixture.invalid:1080"

    async def failure(*args):
        raise RuntimeError(private)

    monkeypatch.setattr(module, "resolve_assets", failure)
    assert cli.main(["record", "--token", "123", "--output", str(tmp_path)]) == 1
    assert private not in capsys.readouterr().err


def test_public_geoblock_does_not_enable_trading(tmp_path, monkeypatch):
    from arb_research.compliance import (ComplianceDenied, ComplianceGuard,
                                         ComplianceSettings, GeoblockClient)
    from arb_research.orderbook import recorder as module

    geo = GeoblockClient(fetch=lambda timeout: {"blocked": True, "country": "US", "region": "VA"})
    monkeypatch.setattr(module, "GEO_CLIENT", geo)
    assert asyncio.run(module.resolve_assets(["123"], []))[0] == ["123"]
    for enabled in (False, True):
        guard = ComplianceGuard(ComplianceSettings(enabled, str(tmp_path / "audit.sqlite3")), geoblock=geo)
        with pytest.raises(ComplianceDenied) as denied:
            guard.require("order", caller="test.public_recorder")
        assert denied.value.decision["allowed"] is False
        assert denied.value.decision["reason"] == "geoblocked"


def test_cli_offline(tmp_path, capsys):
    from arb_research.orderbook.__main__ import main

    write_fixture(tmp_path)
    assert main(["replay", str(tmp_path), "--at-ns", "3000", "--token", "123"]) == 0
    assert json.loads(capsys.readouterr().out)["books"]["123"]["bids"][0]["price"] == "0.5"
    assert main(["gaps", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {}
    assert main(["replay", str(tmp_path), "--token", "999"]) == 1


def test_real_recording_rebuilds_deltas_to_authoritative_books():
    from decimal import Decimal
    import hashlib

    root = Path(__file__).parent / "fixtures/orderbook-live"
    capture = json.loads((root / "capture.json").read_text())
    for relative, expected in capture["sha256"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
    rows = list(records(root))
    assert len(rows) == 3456
    deltas_seen, anchors, reseeded = set(), 0, 0

    def numeric_depth(levels, reverse):
        return sorted([(Decimal(r["price"]), Decimal(r["size"])) for r in levels
                       if Decimal(r["size"])], reverse=reverse)

    for row in rows:
        event = row["payload"]
        if event["event_type"] == "price_change":
            deltas_seen.update(c["asset_id"] for c in event["price_changes"])
        elif event["event_type"] == "book" and event["asset_id"] in deltas_seen:
            # Compare the pre-snapshot delta-built depth with the SERVER's next
            # full book, not a state produced after applying that snapshot.
            prior = replay(root, row["received_ns"] - 1)["books"][event["asset_id"]]
            if not prior["available"]:
                # A detected gap requires this authoritative snapshot to reseed.
                reseeded += 1
                continue
            assert numeric_depth(prior["bids"], True) == numeric_depth(event["bids"], True)
            assert numeric_depth(prior["asks"], False) == numeric_depth(event["asks"], False)
            anchors += 1
    assert anchors >= 2
    final = replay(root, capture["last_market_received_ns"])
    assert len(final["books"]) == 2 and all(b["available"] for b in final["books"].values())
    assert final["gaps"] == capture["observed_gaps"]
    assert sorted(g["kind"] for g in final["gaps"]) == [
        "disconnected", "server_hash_conflict", "server_hash_conflict"]
    assert reseeded == 2 and capture["reconnects"] == 1
    assert capture["snapshot_rows"] == 34
    assert capture["pings"] == capture["pongs"] == 16


def test_proxy_failures_reconnect_without_logging_route(tmp_path, monkeypatch, caplog):
    from python_socks import ProxyError

    route = "socks5h://fixture-private@fixture.invalid:1080"
    monkeypatch.setenv("ARB_POLYMARKET_PROXY", route)

    class Failure:
        async def __aenter__(self):
            raise ProxyError("unreachable " + route)

        async def __aexit__(self, *args):
            return False

    recorder = Recorder(tmp_path, ["123"])
    asyncio.run(recorder.run(duration=.01, connect=lambda *a, **kw: Failure()))
    assert recorder.metrics["reconnects"] == 1
    assert route not in caplog.text
    assert route not in "".join(p.read_text() for p in tmp_path.rglob("*.json*"))
