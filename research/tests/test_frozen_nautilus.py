"""Offline conversion qualification; synthetic terms never certify live fees."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.instruments import BinaryOption

from arb_research.frozen_nautilus import (
    FrozenDataError, FrozenTerms, convert_frozen_ticks, load_frozen_snapshot,
)
from arb_research.polymarket.models import Event, Market, Token


FIXTURE = Path(__file__).parent / "fixtures/frozen-nautilus/ctf.json"
RECORDED = Path(__file__).parent / "fixtures/nautilus-data-v2"


def fixture():
    return json.loads(FIXTURE.read_text())


def load(raw, **kwargs):
    return load_frozen_snapshot(raw["snapshot"], event=Event.model_validate(raw["event"]),
                                terms={k: FrozenTerms.model_validate(v) for k, v in raw["terms"].items()},
                                **kwargs)


def serialized(batch):
    return ([BinaryOption.to_dict(i) for i in batch.instruments], [TradeTick.to_dict(t) for t in batch.ticks])


def test_ctf_identity_precision_order_duplicates_fees_and_input_immutability():
    raw = fixture()
    before = deepcopy(raw)
    batch = load(raw)
    assert raw == before
    assert [str(i.raw_symbol) for i in batch.instruments] == ["101", "202"]
    for instrument in batch.instruments:
        assert str(instrument.id) == f"{raw['event']['markets'][0]['condition_id']}-{instrument.raw_symbol}.POLYMARKET"
        assert instrument.quote_currency.code == "pUSD"
        assert (instrument.price_precision, instrument.size_precision) == (16, 6)
        assert instrument.taker_fee == Decimal("0.07") and instrument.maker_fee == 0
        assert instrument.ts_event == instrument.ts_init == 1793280000000000000
        assert instrument.expiration_ns == 1793289600000000000
        assert instrument.description is None
    assert [t.ts_event for t in batch.ticks] == [1793286000000000000, 1793286000000000000,
                                               1793286001000000000, 1793286002000000000]
    assert len({t.trade_id for t in batch.ticks}) == 4
    assert batch.ticks[0].size.as_decimal() == Decimal("10.000001")
    assert batch.ticks[2].price.as_decimal() == Decimal("0.829999997")
    assert batch.ticks[-1].aggressor_side == AggressorSide.SELLER
    assert batch.ticks[0].aggressor_side == AggressorSide.BUYER
    assert all(t.ts_init == t.ts_event for t in batch.ticks)
    assert serialized(load(raw)) == serialized(batch)


def test_bounds_keep_identity_and_validate_excluded_rows():
    raw = fixture()
    full = load(raw)
    selected = load(raw, start_ns=1793286001000000000, end_ns=1793286002000000000)
    assert [TradeTick.to_dict(t) for t in selected.ticks] == [TradeTick.to_dict(t) for t in full.ticks[2:]]
    assert load(raw, start_ns=1793286003000000000).ticks == ()
    with pytest.raises(FrozenDataError, match="start_ns"):
        load(raw, start_ns=2, end_ns=1)
    raw["snapshot"]["markets"][0]["trades"][-1]["price"] = "NaN"
    with pytest.raises(FrozenDataError):
        load(raw, start_ns=1793286002000000000)


@pytest.mark.parametrize("protocol", [None, "unknown", "future", "v2"])
def test_protocol_default_deny_in_raw_and_normalized_identities(protocol):
    raw = fixture()
    raw["snapshot"]["markets"][0]["market"]["version"] = protocol
    raw["snapshot"]["markets"][0]["market"]["positionIds"] = '["101","202"]'
    with pytest.raises(FrozenDataError, match="Protocol V2 is disabled"):
        load(raw)
    if protocol in ("v2", "unknown"):
        raw = fixture()
        raw["event"]["markets"][0]["protocol"] = protocol
        with pytest.raises(FrozenDataError, match="Protocol V2 is disabled"):
            load(raw)
        raw = fixture()
        raw["event"]["markets"][0]["tokens"][0]["protocol"] = protocol
        with pytest.raises(FrozenDataError, match="Protocol V2 is disabled"):
            load(raw)


@pytest.mark.parametrize("field,value", [
    ("condition_id", "0x" + "ff"*32), ("token_id", "999"),
    ("outcome_index", 1), ("outcome_index", True), ("outcome", "No"),
    ("transaction_hash", None), ("proxy_wallet", "invalid"), ("side", "HOLD"),
])
def test_trade_identity_rejections(field, value):
    raw = fixture()
    raw["snapshot"]["markets"][0]["trades"][-1][field] = value
    with pytest.raises(FrozenDataError):
        load(raw)


@pytest.mark.parametrize("field,value", [
    ("price", "0.12345678901234567"), ("size", "1.0000001"),
    ("price", "NaN"), ("price", "Infinity"), ("price", "-0.01"), ("price", "1.01"),
    ("price", True), ("size", "0"), ("size", "-1"), ("size", "34028236692094"),
    ("timestamp", 1793286000.5), ("timestamp", "1793286000"), ("timestamp", True),
    ("timestamp", 1793286000000), ("timestamp", -1),
])
def test_reject_lossy_or_invalid_observations(field, value):
    raw = fixture()
    raw["snapshot"]["markets"][0]["trades"][-1][field] = value
    with pytest.raises(FrozenDataError):
        load(raw)


@pytest.mark.parametrize("price", ["0", "1", "0.1234567890123456"])
def test_price_endpoints_and_exact_sixteen_decimals(price):
    raw = fixture()
    for row in raw["snapshot"]["markets"][0]["trades"]:
        row["price"] = price
    assert all(t.price.as_decimal() == Decimal(price) for t in load(raw).ticks)


@pytest.mark.parametrize("field,value", [
    ("event_id", "other"), ("market_id", "other"), ("condition_id", "other"),
    ("known_at_ns", 1793286001000000000), ("valid_until_ns", 1793286002000000000),
    ("valid_from_ns", 1793286001000000000), ("valid_from_ns", 1793290000000000000),
    ("expiration_ns", 1), ("fee_exponent", 2), ("fees_enabled", False),
])
def test_historical_terms_identity_time_and_fee_rejections(field, value):
    raw = fixture()
    raw["terms"]["market-ctf"][field] = value
    with pytest.raises(FrozenDataError):
        load(raw)


def test_missing_fees_and_explicit_zero_fee_schedule():
    raw = fixture()
    del raw["terms"]["market-ctf"]["fee_rate"]
    with pytest.raises(ValidationError):
        load(raw)
    raw["terms"]["market-ctf"].update(fees_enabled=False, fee_rate="0")
    assert all(i.taker_fee == 0 for i in load(raw).instruments)
    # An arbitrary terminal field cannot be smuggled through the terms schema.
    raw["terms"]["market-ctf"]["winner"] = "Yes"
    with pytest.raises(ValidationError):
        load(raw)


def test_announced_fee_change_only_applies_after_effective_time():
    raw = fixture()
    raw["terms"]["market-ctf"].update(known_at_ns=1793280000000000000,
                                      valid_from_ns=1793286001000000000)
    # Even slicing to a later window cannot hide an out-of-segment input row.
    with pytest.raises(FrozenDataError, match="fee validity"):
        load(raw, start_ns=1793286001000000000)
    raw["snapshot"]["markets"][0]["trades"] = raw["snapshot"]["markets"][0]["trades"][:2]
    batch = load(raw)
    assert batch.ticks[0].ts_event == 1793286001000000000  # Effective boundary inclusive.
    assert batch.instruments[0].activation_ns == 1793286001000000000


def test_multi_market_output_is_globally_sorted_and_rejects_shared_tokens():
    raw = fixture()
    second = deepcopy(raw["event"]["markets"][0])
    second.update(id="second", condition_id="0x" + "44"*32)
    second["tokens"][0]["id"], second["tokens"][1]["id"] = "303", "404"
    raw["event"]["markets"].append(second)
    item = deepcopy(raw["snapshot"]["markets"][0])
    item["market"].update(id="second", conditionId=second["condition_id"], clobTokenIds='["303","404"]')
    for row in item["trades"]:
        row.update(condition_id=second["condition_id"], token_id={"101": "303", "202": "404"}[row["token_id"]])
        row["timestamp"] -= 1
    raw["snapshot"]["markets"].append(item)
    raw["terms"]["second"] = {**raw["terms"]["market-ctf"], "market_id": "second",
                               "condition_id": second["condition_id"]}
    batch = load(raw)
    assert len(batch.instruments) == 4 and len(batch.ticks) == 8
    assert len({t.trade_id for t in batch.ticks}) == 8
    assert [t.ts_event for t in batch.ticks] == sorted(t.ts_event for t in batch.ticks)
    second["tokens"][0]["id"] = "101"
    item["market"]["clobTokenIds"] = '["101","404"]'
    with pytest.raises(FrozenDataError, match="Overlapping"):
        load(raw)


def test_terminal_metadata_and_capture_time_fees_cannot_change_earlier_output():
    raw = fixture()
    expected = serialized(load(raw))
    market = raw["snapshot"]["markets"][0]["market"]
    market.update(closed=False, outcomePrices='[0,1]', winner="No", endDate="2090-01-01",
                  feeSchedule={"rate": "0.01"}, feesEnabled=False, negRisk=True,
                  question="Future winner No", orderPriceMinTickSize="0.5")
    raw["event"]["closed"] = False
    raw["event"]["markets"][0].update(closed=False, question="Future winner No", neg_risk=True)
    raw["snapshot"]["event"].update(winner="No", closed=False)
    raw["snapshot"]["markets"][0]["resolutions"] = [{"winner": "No", "payouts": [0, 1]}]
    raw["snapshot"]["markets"][0]["prices"] = {"101": [{"price": 0}]}
    assert serialized(load(raw)) == expected
    raw["terms"]["market-ctf"]["known_at_ns"] = 1793286001000000000
    with pytest.raises(FrozenDataError, match="precedes known metadata"):
        load(raw, start_ns=1793286002000000000)


def test_normalized_identity_and_snapshot_join_rejections():
    mutations = [
        lambda r: r["snapshot"]["event"].update(id="other"),
        lambda r: r["snapshot"]["markets"][0]["market"].update(id="other"),
        lambda r: r["snapshot"]["markets"][0]["market"].update(conditionId="other"),
        lambda r: r["snapshot"]["markets"][0]["market"].update(clobTokenIds='["202","101"]'),
        lambda r: r["snapshot"]["markets"][0]["market"].update(outcomes='["No","Yes"]'),
        lambda r: r["event"]["markets"][0].update(event_id="other"),
        lambda r: r["event"]["markets"][0]["tokens"][1].update(id="101"),
        lambda r: r["event"]["markets"][0]["tokens"][1].update(outcome_index=0),
        lambda r: r["event"]["markets"][0]["tokens"][1].update(id=str(2**256)),
        lambda r: r["snapshot"]["markets"].append(deepcopy(r["snapshot"]["markets"][0])),
        lambda r: r["terms"].clear(),
    ]
    for mutate in mutations:
        raw = fixture()
        mutate(raw)
        with pytest.raises(FrozenDataError):
            load(raw)


@pytest.mark.parametrize("name,expected_count", [("btc", 16), ("eth", 6)])
def test_recorded_v1_trades_with_explicitly_synthetic_historical_terms(name, expected_count):
    # Recorded ARB-14 trades + synthetic terms is a conversion test, NOT evidence
    # that capture-time Gamma terms were known at those historical trade times.
    snapshot = json.loads((RECORDED / f"{name}-settled.json").read_text())
    raw = snapshot["markets"][0]["market"]
    event_id, market_id = str(snapshot["event"]["id"]), str(raw["id"])
    ids, labels = json.loads(raw["clobTokenIds"]), json.loads(raw["outcomes"])
    market = Market(id=market_id, event_id=event_id, condition_id=raw["conditionId"], protocol="v1",
                    tokens=[Token(id=t, outcome=o, outcome_index=n, protocol="v1")
                            for n, (t, o) in enumerate(zip(ids, labels))])
    terms = FrozenTerms(event_id=event_id, market_id=market_id, condition_id=market.condition_id,
                        known_at_ns=1793280000000000000, valid_from_ns=1793280000000000000,
                        valid_until_ns=1793290000000000000,
                        expiration_ns=1793289600000000000, fees_enabled=True,
                        fee_rate="0.07", fee_exponent=1)
    batch = load_frozen_snapshot(snapshot, event=Event(id=event_id, markets=[market]), terms={market_id: terms})
    assert len(batch.ticks) == expected_count
    assert len({t.trade_id for t in batch.ticks}) == expected_count
    assert [t.ts_event for t in batch.ticks] == sorted(t.ts_event for t in batch.ticks)


def test_version_pin_is_enforced(monkeypatch):
    monkeypatch.setattr("arb_research.frozen_nautilus.version", lambda _: "2.0.0")
    with pytest.raises(FrozenDataError, match="1.231.0"):
        load(fixture())


def test_fresh_process_has_no_network_sdk_or_adapter_dependency():
    script = '''
import builtins, json, socket, sys
def deny(*args, **kwargs):
    raise AssertionError("network operation attempted")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.startswith(("httpx", "requests", "polymarket", "py_clob_client",
                        "nautilus_trader.adapters", "arb_research.nautilus_data_v2")):
        raise AssertionError("transport/SDK/adapter dependency: " + name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from arb_research.frozen_nautilus import FrozenTerms, load_frozen_snapshot
from arb_research.polymarket.models import Event
raw = json.load(open(sys.argv[1]))
batch = load_frozen_snapshot(raw["snapshot"], event=Event.model_validate(raw["event"]),
    terms={k: FrozenTerms.model_validate(v) for k, v in raw["terms"].items()})
assert len(batch.ticks) == 4
'''
    subprocess.run([sys.executable, "-c", script, str(FIXTURE)], check=True, timeout=60)
