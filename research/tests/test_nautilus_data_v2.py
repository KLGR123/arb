"""Recorded HTTP data; pagination and malformed-input cases are controlled edits.

No test accesses the network. Synthetic book/v2 cases are explicitly contracts,
not evidence of a recorded historical order book or a live protocol-v2 market.
"""

from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from arb_research.nautilus_data_v2 import (
    DataError, DataV2Client, ResponseCache, book_quote, instruments_from_market,
    load_snapshot, ns, price_quotes, trade_ticks,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nautilus-data-v2"


def snapshot(name="btc"):
    return json.loads((FIXTURES / f"{name}-settled.json").read_text())


def make_store(raw, root):
    from arb_research.polymarket.models import Event, Market, Token, PricePage, Resolution
    from arb_research.polymarket.store import DataStore
    store = DataStore(root)
    markets = []
    for item in raw["markets"]:
        gamma = item["market"]
        instruments = instruments_from_market(gamma)
        markets.append(Market(id=str(gamma["id"]), event_id=str(raw["event"]["id"]),
                              condition_id=gamma["conditionId"], protocol=gamma["version"],
                              tokens=[Token(id=str(i.raw_symbol), outcome=i.outcome, outcome_index=n,
                                            protocol=gamma["version"]) for n, i in enumerate(instruments)]))
        for token, points in item["prices"].items():
            store.commit_page(str(gamma["id"]), token, PricePage(data=points),
                              {"through": raw["recorded_at"], "pending": None})
    event = Event(id=str(raw["event"]["id"]), markets=markets)
    store.put_event(event)
    rows = [Resolution(market_id=str(item["market"]["id"]), condition_id=r["condition_id"],
                       status=r["status"], was_disputed=r["was_disputed"], extended_review=r["extended_review"],
                       last_updated_at=str(r["last_update_timestamp"]), raw=r)
            for item in raw["markets"] for r in item["resolutions"]]
    store.put_resolutions(event, rows)
    return store


class Budget:
    def __init__(self):
        self.calls = 0

    def acquire(self):
        self.calls += 1


def test_loader_does_not_import_optional_execution_adapter():
    script = """
import builtins, sys
original = builtins.__import__
def no_adapter(name, *args, **kwargs):
    if name.startswith(('py_clob_client', 'nautilus_trader.adapters.polymarket')):
        raise AssertionError('unexpected adapter import')
    return original(name, *args, **kwargs)
builtins.__import__ = no_adapter
from arb_research.nautilus_data_v2 import load_snapshot
import json
loaded = load_snapshot(json.load(open(sys.argv[1])))
assert len(loaded) == 2
"""
    subprocess.run([sys.executable, "-c", script, str(FIXTURES / "btc-settled.json")], check=True)


@pytest.mark.parametrize("name,counts", [("eth", [1, 5]), ("btc", [3, 13])])
def test_recorded_instruments_trades_and_quotes(name, counts):
    raw = snapshot(name)
    assert raw["source"] == "recorded_public_api"
    loaded = load_snapshot(raw)
    assert [len(t.trades) for t in loaded] == counts
    for token in loaded:
        assert token.instrument.quote_currency.code == "pUSD"
        assert token.instrument.taker_fee == Decimal("0.07")
        assert token.metadata["settled"] is True
        assert len(token.quotes) == 1  # Real sparse series + removed settlement marker.
        assert token.quotes[0].bid_size.as_decimal() == 0
        assert token.quotes[0].bid_price == token.quotes[0].ask_price
        assert token.instrument.info.keys().isdisjoint({"winner", "closed", "outcomePrices", "resolution_metadata"})
        assert [t.ts_event for t in token.trades] == sorted(t.ts_event for t in token.trades)
        assert all(t.ts_event == t.ts_init for t in token.trades)
        assert len({t.trade_id for t in token.trades}) == len(token.trades)


def test_price_precision_local_bounds_and_instrument_selection():
    raw = snapshot()
    loaded = load_snapshot(raw)[1]
    assert Decimal("0.829999997") in [t.price.as_decimal() for t in loaded.trades]
    stamp = 1793288773
    selected = load_snapshot(raw, token_id=str(loaded.instrument.raw_symbol), start=stamp, end=stamp)
    assert len(selected) == 1
    assert len(selected[0].trades) == 2
    assert all(t.ts_event == stamp * 10**9 for t in selected[0].trades)
    with pytest.raises(DataError, match="start"):
        load_snapshot(raw, start=stamp+1, end=stamp)
    with pytest.raises(DataError, match="Token"):
        load_snapshot(raw, token_id="absent")


def test_arb7_store_reads_all_history_pages_and_rejects_pending_checkpoint(tmp_path):
    from arb_research.polymarket.models import PricePage
    raw = snapshot()
    store = make_store(raw, tmp_path)
    baseline = load_snapshot(raw)
    cached = load_snapshot(raw, price_store=store)
    assert [[str(t) for t in v.quotes] for v in cached] == [[str(t) for t in v.quotes] for v in baseline]
    token = str(cached[0].instrument.raw_symbol)
    rows = [{"timestamp": 1000+i, "price": 0.2, "resolution_seconds": 60} for i in range(1005)]
    store.commit_page(cached[0].instrument.info["market_id"], token, PricePage(data=rows),
                      {"through": raw["recorded_at"], "pending": None})
    loaded = load_snapshot(raw, token_id=token, start=1000, end=2004, price_store=store)
    assert len(loaded[0].quotes) == 1005
    store.commit_page(cached[0].instrument.info["market_id"], token, PricePage(data=[]),
                      {"through": raw["recorded_at"], "pending": {"cursor": "unfinished"}})
    with pytest.raises(DataError, match="checkpoint"):
        load_snapshot(raw, price_store=store)


def test_transaction_hash_is_not_a_unique_trade_id():
    raw = snapshot()["markets"][0]
    instrument = instruments_from_market(raw["market"])[1]
    row = next(t for t in raw["trades"] if t["token_id"] == str(instrument.raw_symbol))
    # Identical fills in one transaction remain two observations.
    ticks = trade_ticks(instrument, [row, deepcopy(row)])
    assert len(ticks) == 2 and ticks[0].trade_id != ticks[1].trade_id
    assert [t.trade_id for t in ticks] == [t.trade_id for t in trade_ticks(instrument, [row, row])]
    with pytest.raises(DataError, match="condition"):
        trade_ticks(instrument, [{**row, "condition_id": "0x"+"0"*64}])


def test_missing_or_stale_settlement_never_exposes_payout_quotes(tmp_path):
    raw = snapshot()
    store = make_store(raw, tmp_path)
    item = raw["markets"][0]
    item["resolutions"] = []
    with pytest.raises(DataError, match="resolution"):
        load_snapshot(raw)
    store.put_resolutions(store.event(str(raw["event"]["id"])), [])
    with pytest.raises(DataError, match="resolution"):
        load_snapshot(raw, price_store=store)
    item["resolutions"] = [{"condition_id": item["market"]["conditionId"], "status": "proposed"}]
    with pytest.raises(DataError, match="resolution"):
        load_snapshot(raw)


def test_store_snapshot_cannot_race_an_active_sync(tmp_path):
    raw = snapshot()
    store = make_store(raw, tmp_path)
    with store.writer():
        with pytest.raises(RuntimeError, match="sync owns"):
            load_snapshot(raw, price_store=store)


def test_explicit_protocol_and_fee_contracts():
    market = snapshot()["markets"][0]["market"]
    for protocol in (None, "future"):
        with pytest.raises((DataError, ValueError)):
            instruments_from_market({**market, "version": protocol})
    # Synthetic protocol-v2 mapping: CTF token IDs must not be chosen.
    ids = json.loads(market["clobTokenIds"])[::-1]
    v2 = instruments_from_market({**market, "version": "v2", "positionIds": json.dumps(ids)})
    assert [str(i.raw_symbol) for i in v2] == ids
    assert all(i.info["protocol"] == "v2" for i in v2)
    missing = {k: v for k, v in market.items() if k != "feeSchedule"}
    with pytest.raises(DataError, match="Fee schedule"):
        instruments_from_market(missing)
    assert instruments_from_market({**missing, "feesEnabled": False})[0].taker_fee == 0
    with pytest.raises(DataError, match="exponent"):
        instruments_from_market({**market, "feeSchedule": {**market["feeSchedule"], "exponent": 2}})


def test_exact_observations_retained_and_split_settlement_excluded():
    instrument = load_snapshot(snapshot())[0].instrument
    points = [{"timestamp": 100+i, "price": p, "resolution_seconds": width}
              for i, (p, width) in enumerate([(0.2, 60), (0.3, 0), (0.5, 0)])]
    quotes = price_quotes(instrument, points, settled=True)
    assert [q.bid_price.as_decimal() for q in quotes] == [Decimal("0.2"), Decimal("0.3")]
    assert len(price_quotes(instrument, points, settled=False)) == 3
    with pytest.raises(DataError):
        price_quotes(instrument, [{**points[0], "price": -0.1}])


def test_recorded_book_conversion_contract():
    # Controlled book, not a recorded historical snapshot.
    instrument = load_snapshot(snapshot())[0].instrument
    book = {"asset_id": str(instrument.raw_symbol), "market": instrument.info["condition_id"],
            "timestamp": "1793288773123", "bids": [{"price": ".2", "size": "4"}, {"price": ".3", "size": "5"}],
            "asks": [{"price": ".5", "size": "6"}, {"price": ".4", "size": "7"}]}
    quote = book_quote(instrument, book)
    assert quote.bid_price.as_decimal() == Decimal(".3")
    assert quote.ask_price.as_decimal() == Decimal(".4")
    assert quote.bid_size.as_decimal() == 5 and quote.ask_size.as_decimal() == 7
    assert quote.ts_event == 1793288773123000000
    with pytest.raises(DataError, match="Crossed"):
        book_quote(instrument, {**book, "asks": [{"price": ".1", "size": "1"}]})
    with pytest.raises(DataError, match="Two-sided"):
        book_quote(instrument, {**book, "asks": []})


def test_collect_cursor_walk_and_offline_cache(tmp_path):
    raw = snapshot()
    item = raw["markets"][0]
    calls = []
    budget = Budget()

    def handler(request):
        calls.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            body = raw["event"] if request.url.path.startswith("/events/") else item["market"]
        elif request.url.path == "/v2/resolutions":
            body = {"data": item["resolutions"]}
        else:
            if request.url.path == "/v2/trades":
                assert request.url.params["condition"] == item["market"]["conditionId"]
                assert request.url.params["taker_only"] == "true"
                assert "market" not in request.url.params and "offset" not in request.url.params
                rows = item["trades"]
            else:
                rows = item["prices"][request.url.params["token_id"]]
            first = "cursor" not in request.url.params
            body = {"data": rows[:2] if first else rows[2:],
                    "pagination": {"has_more": first, "next_cursor": "recorded-page-two" if first else None}}
        return httpx.Response(200, json=body)

    store = make_store(raw, tmp_path / "store")
    cache = ResponseCache(tmp_path / "responses")
    with DataV2Client(cache=cache, transport=httpx.MockTransport(handler), limiter=budget) as client:
        captured = client.collect(str(raw["event"]["id"]), store=store, market_id=item["market"]["id"])
    assert [len(t.trades) for t in load_snapshot(captured)] == [3, 13]
    assert budget.calls == len(calls)

    def no_network(request):
        pytest.fail("Offline cache attempted HTTP")

    with DataV2Client(cache=cache, offline=True, transport=httpx.MockTransport(no_network)) as client:
        cached = client.collect(str(raw["event"]["id"]), store=store, market_id=item["market"]["id"])
        assert cached["markets"] == captured["markets"]
        with pytest.raises(DataError, match="cache miss"):
            client.get("gamma", "/events/999")
        with pytest.raises(ValueError, match="allowlist"):
            client.get("data", "/trades")


@pytest.mark.parametrize("kind", ["repeat", "truncated", "envelope", "inconsistent"])
def test_incomplete_history_never_silently_succeeds(kind):
    def handler(request):
        if kind == "envelope":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"data": [], "pagination": {
            "has_more": kind != "inconsistent", "next_cursor": "same"}})
    with DataV2Client(transport=httpx.MockTransport(handler), limiter=Budget()) as client:
        with pytest.raises(DataError):
            client.pages("/v2/trades", {}, max_pages=1 if kind == "truncated" else 3)


def test_retries_share_budget_and_do_not_expose_transport_secrets():
    budget, sleeps = Budget(), []
    codes = iter([429, 503, 200])
    def handler(request):
        return httpx.Response(next(codes), json={"data": []}, headers={"Retry-After": "2"})
    with DataV2Client(transport=httpx.MockTransport(handler), limiter=budget, sleep=sleeps.append) as client:
        assert client.get("data", "/v2/resolutions") == {"data": []}
    assert budget.calls == 3 and len(sleeps) == 2 and min(sleeps) >= 2
    def broken(request):
        raise httpx.ConnectError("credential-bearing-proxy-value")
    with DataV2Client(transport=httpx.MockTransport(broken), limiter=budget, sleep=lambda _: None) as client:
        with pytest.raises(DataError) as exc:
            client.get("data", "/v2/resolutions")
    assert "credential-bearing" not in str(exc.value)


@pytest.mark.parametrize("status", [302, 400, 401])
def test_no_redirects_or_nonretryable_retries(status):
    budget = Budget()
    with DataV2Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)),
                      limiter=budget) as client:
        with pytest.raises(DataError, match="Public data read"):
            client.get("data", "/v2/resolutions")
    assert budget.calls == 1


@pytest.mark.skipif(importlib.util.find_spec("py_clob_client_v2") is None, reason="optional replay fee model extra")
@pytest.mark.parametrize("name", ["eth", "btc"])
def test_real_settled_event_backtest(name):
    from arb_research.nautilus_data_v2_cli import replay
    selected = next(t for t in load_snapshot(snapshot(name)) if len(t.trades) >= 4)
    actual = replay(selected)
    expected = json.loads((FIXTURES / f"{name}-result.json").read_text())
    assert actual == expected
    assert len(actual["fills"]) == 2 and actual["settled"] is True
    assert Decimal(actual["total_commission"]) > 0
