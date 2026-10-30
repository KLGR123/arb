import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from arb_research.fillable import NS, Timeline, analyze, walk_asks
from arb_research.orderbook.core import digest
from arb_research.updown import parse_market
from arb_research.updown_cli import read_recording

ROOT = Path(__file__).parents[1]


def synthetic():
    # Explicit synthetic timing/depth contract; market rules copied from real ARB-15 metadata.
    event = copy.deepcopy(read_recording(ROOT / "fixtures/updown/btc-5m-20261029.json.gz")["markets"][0]["event"])
    market = parse_market(event)
    raw = event["markets"][0]
    raw["orderPriceMinTickSize"], raw["orderMinSize"] = .01, 5
    start = int(market["start"])
    decision = start + 120
    events = []
    def add(second, payload, kind="event"):
        if kind == "event":
            payload["timestamp"] = str(round(second * 1000))
        row = {"version": 1, "session": "synthetic", "seq": len(events) + 1,
               "received_ns": round(second * NS), "kind": kind, "payload": payload}
        row["sha256"] = digest(row)
        events.append(row)
    add(decision - 1, {"event_type": "connected", "assets_ids": [market["up_token"], market["down_token"]]}, "control")
    for token in (market["up_token"], market["down_token"]):
        add(decision - .5, {"event_type": "book", "asset_id": token, "market": raw["conditionId"],
                           "bids": [{"price": ".3", "size": "100"}],
                           "asks": [{"price": ".4", "size": "10"}, {"price": ".42", "size": "20"}]})
    add(decision + .1, {"event_type": "price_change", "market": raw["conditionId"], "price_changes": [
        {"asset_id": market["up_token"], "price": ".4", "size": "0", "side": "SELL"}]})
    add(decision + 3, {"event_type": "stopped"}, "control")
    return {"recorded_at": "synthetic", "markets": [event], "events": events,
            "cex": {"points": [[s, "100"] for s in range(start - 1000, decision + 5)]}}, decision, add


def test_depth_weighted_fee_rounding_partial_and_tick_stress():
    fixture = json.loads((ROOT / "tests/fixtures/fillable-contract.json").read_text())
    for ticks, label in [(0, "zero_tick"), (1, "one_tick")]:
        r = walk_asks(fixture["asks"], fixture["shares"], fixture["rate"], fixture["tick"], ticks)
        expected = fixture[label]
        assert r["filled_shares"] == expected["filled"]
        assert r["cost_usd"] == float(expected["cost"])
        assert r["fee_usd"] == float(expected["fee"])
        assert r["fill_fraction"] == .75
        assert r["unfilled_shares"] == 10
        assert r["vwap"] == pytest.approx(float(expected["cost"]) / 30)
    assert walk_asks([], 10, .07, .01)["vwap"] is None
    assert walk_asks([{"price": ".99", "size": "100"}], 10, .07, .01, 1)["filled_shares"] == 0


@pytest.mark.parametrize("quantity,rate,tick,stress", [(0,.07,.01,0), (10,2,.01,0), (10,.07,0,0),
                                                       (10,.07,.01,-1), (10,.07,.01,1.5)])
def test_invalid_fill_inputs(quantity, rate, tick, stress):
    with pytest.raises(ValueError):
        walk_asks([], quantity, rate, tick, stress)


def test_received_books_arrival_not_future_and_fixed_fair_value():
    recording, decision, _ = synthetic()
    report = analyze(recording, shares=40, delays=(0,.05,.25,2), ticks=(0,1))
    rows = [r for r in report["probes"] if r["signal_id"].endswith(f":{decision}") and r["adverse_ticks"] == 0]
    assert [r["filled_shares"] for r in rows] == [30,30,20,20]
    # A flat path has zero volatility: the tie resolves Up with fair value 1.
    assert rows[0]["fillable_edge"] == pytest.approx(1 - 12.4/30 - .50904/30)
    assert rows[0]["paper_edge"] == pytest.approx(.65)
    assert rows[0]["expected_surplus_per_requested_share"] == pytest.approx(rows[0]["fillable_edge"] * .75)
    assert rows[0]["paper_edge"] == rows[-1]["paper_edge"]
    original = copy.deepcopy(recording)
    original["events"] = original["events"][:3] + [original["events"][-1]]
    original["events"][-1]["seq"] = 4
    original["events"][-1]["sha256"] = digest({k:v for k,v in original["events"][-1].items() if k != "sha256"})
    assert analyze(original, shares=40, delays=(0,), ticks=(0,))["signals"][0] == report["signals"][0]


def test_gap_recovery_cannot_create_a_fill_across_discontinuity():
    recording, decision, _ = synthetic()
    row = recording["events"][3]
    row["kind"], row["payload"] = "control", {"event_type": "disconnected"}
    row["sha256"] = digest({k:v for k,v in row.items() if k != "sha256"})
    report = analyze(recording, shares=40, delays=(0,.25), ticks=(0,))
    delayed = [r for r in report["probes"] if r["delay_seconds"] == .25]
    assert delayed[0]["filled_shares"] == 0
    assert delayed[0]["fillable_edge"] is None
    assert report["gaps"]


def test_minimum_staleness_tail_missing_rules_and_checksum_are_rejected():
    recording, _, _ = synthetic()
    with pytest.raises(ValueError, match="minimum"):
        analyze(recording, shares=1)
    report = analyze(recording, shares=40, delays=(0,2), ticks=(0,), max_age=.75)
    assert any(r["reason"] == "stale_book" for r in report["probes"])
    assert report["excluded"]
    recording["events"][0]["seq"] = 100
    with pytest.raises(ValueError, match="checksum"):
        Timeline(recording["events"])
    recording, _, _ = synthetic()
    recording["markets"][0]["markets"][0]["feeSchedule"]["exponent"] = 2
    with pytest.raises(ValueError):
        analyze(recording)


@pytest.mark.parametrize("input_name,report_name", [
    ("btc-books-20261030.json.gz", "crypto-updown-fillable-edge.json"),
    ("btc-rest-books-20261030.json.gz", "crypto-updown-fillable-edge-rest.json"),
])
def test_real_recording_reproduces_report_without_network(monkeypatch, input_name, report_name):
    def no_network(*args, **kwargs):
        raise AssertionError("offline CI")
    monkeypatch.setattr(httpx.Client, "get", no_network)
    recording = ROOT / "fixtures/updown" / input_name
    report = analyze(read_recording(recording))
    published = json.loads((ROOT.parent / "docs/reports" / report_name).read_text())
    assert report["status"] == "complete"
    assert report["execution"] == "disabled"
    assert report["compliance"]["trading_allowed"] is False
    assert report["signals"]
    for key in ("coverage", "baseline", "sensitivity", "buckets", "signals", "probes", "excluded", "gaps"):
        assert report[key] == published[key]
    assert hashlib.sha256(recording.read_bytes()).hexdigest() == published["input_sha256"]
    for r in report["probes"]:
        if r["filled_shares"]:
            assert r["arrival_book_received_ns"] <= r["arrival_ns"]
            assert r["cost_usd"] + r["fee_usd"] > 0


def test_live_rule_validation_does_not_weaken_settlement_default():
    recording, _, _ = synthetic()
    event = recording["markets"][0]
    event["markets"][0]["closed"] = False
    with pytest.raises(ValueError):
        parse_market(event)
    assert parse_market(event, require_resolved=False)["actual_up"] is None


def test_rest_refresh_is_explicit_sampling_not_a_websocket_continuity_claim():
    recording, decision, _ = synthetic()
    # Three initial full snapshots and an authoritative replacement at t+0.1.
    old = recording["events"][1]
    replacement = recording["events"][3]
    replacement["payload"] = copy.deepcopy(old["payload"])
    replacement["payload"]["asks"] = [{"price": ".6", "size": "100"}]
    replacement["payload"]["timestamp"] = str((decision + .1) * 1000)
    events = recording["events"][1:4]
    for i, row in enumerate(events, 1):
        row["seq"], row["source"] = i, "rest_snapshot"
        row["sha256"] = digest({k:v for k,v in row.items() if k != "sha256"})
    with pytest.raises(ValueError, match="explicit sampling"):
        Timeline(events)
    timeline = Timeline(events, rest_snapshots=True)
    assert not timeline.gaps
    # Last receipt is also the bound of observation: do not extrapolate past it.
    assert timeline.frame(round((decision + .1) * NS)).books[old["payload"]["asset_id"]]["asks"][0]["price"] == "0.6"
    with pytest.raises(ValueError, match="cover"):
        timeline.frame((decision + 1) * NS)


def test_future_cex_changes_do_not_change_current_signal_or_fills():
    recording, decision, _ = synthetic()
    report = analyze(recording, shares=40, delays=(0,.25), ticks=(0,))
    for p in recording["cex"]["points"]:
        if p[0] > decision:
            p[1] = "10000"
    changed = analyze(recording, shares=40, delays=(0,.25), ticks=(0,))
    assert changed["signals"][0] == report["signals"][0]
    assert changed["probes"][:2] == report["probes"][:2]


def test_no_fill_is_null_and_partial_below_minimum_is_not_counted_as_success():
    recording, decision, _ = synthetic()
    for row in recording["events"]:
        if row["payload"]["event_type"] == "book":
            row["payload"]["asks"] = [{"price": ".4", "size": "2"}]
            row["sha256"] = digest({k:v for k,v in row.items() if k != "sha256"})
    report = analyze(recording, shares=40, delays=(0,), ticks=(0,))
    probe = next(r for r in report["probes"] if r["signal_id"].endswith(f":{decision}"))
    assert probe["reason"] == "below_minimum_fill"
    assert probe["fillable_edge"] is None
    assert probe["expected_surplus_per_requested_share"] == 0


@pytest.mark.parametrize("offset", [-20, 2])
def test_stale_or_future_server_timestamp_is_rejected(offset):
    recording, decision, _ = synthetic()
    for row in recording["events"]:
        if row["payload"]["event_type"] == "book":
            row["payload"]["timestamp"] = str((decision + offset) * 1000)
            row["sha256"] = digest({k:v for k,v in row.items() if k != "sha256"})
    report = analyze(recording, shares=40, delays=(0,), ticks=(0,))
    excluded = next(r for r in report["excluded"] if r["decision_at"] == decision)
    assert excluded["reason"] == "stale_or_future_server_timestamp"


def test_unavailable_selected_book_stays_in_paper_signal_denominator():
    recording, decision, _ = synthetic()
    for p in recording["cex"]["points"]:
        if p[0] >= decision - 2:
            p[1] = "99"
    down = recording["events"][2]
    down["payload"]["timestamp"] = str((decision - 20) * 1000)
    down["sha256"] = digest({k:v for k,v in down.items() if k != "sha256"})
    report = analyze(recording, shares=40, delays=(0,), ticks=(0,))
    signal = next(s for s in report["signals"] if s["decision_at"] == decision)
    assert signal["side"] == "Down"
    assert signal["ask_depth_within_5_ticks"] is None
    assert signal["decision_book_reason"] == "stale_or_future_server_timestamp"
    assert report["buckets"]["depth_unavailable"]["probes"] > 0
    probe = next(r for r in report["probes"] if r["signal_id"] == signal["signal_id"])
    assert probe["fillable_edge"] is None and probe["filled_shares"] == 0


def test_protocol_v2_uses_position_ids_instead_of_legacy_ctf_tokens():
    recording, _, _ = synthetic()
    event = recording["markets"][0]
    event["markets"][0]["version"] = "v2"
    event["markets"][0]["positionIds"] = ["123", "456"]
    parsed = parse_market(event)
    assert (parsed["up_token"], parsed["down_token"]) == ("123", "456")
