"""Offline depth/fee/exclusion contracts; synthetic and recorded data separated."""

import copy

from negrisk_legacy import legacy_report
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from arb_research.negrisk import Costs, D, dumps
from arb_research.negrisk_cli import ReadOnlyAPI
from arb_research.negrisk_deep import collect_deep, deep_scan, discover, load, main, resolution_reason


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def raw():
    snapshot = json.loads((Path(__file__).parent / "fixtures/negrisk-contract.json").read_text())
    snapshot.update(selected_event_id="1", resolutions={}, resolution_checked_at=snapshot["as_of"])
    for m in snapshot["events"][0]["markets"]:
        snapshot["resolutions"][m["id"]] = {"data": [{"condition_id": m["conditionId"],
            "status": "posed", "was_disputed": False, "extended_review": False}]}
    return dict(schema_version="negrisk-deep.raw.v1", started_at=snapshot["as_of"],
                finished_at=snapshot["as_of"], snapshots=[snapshot], coverage={})


def test_retains_negative_edge_and_rounded_fees(raw):
    # A complete basket with positive gross and negative net at every size.
    for asset, book in raw["snapshots"][0]["books"].items():
        book["asks"] = [{"price": ".33" if asset.endswith("1") else ".70", "size": "20"}]
        book["bids"] = [{"price": ".32" if asset.endswith("1") else ".69", "size": "20"}]
    report = deep_scan(raw)
    basket = next(c for c in report["groups"][0]["candidates"] if c["kind"] == "buy_yes_basket")
    assert basket["gross_edge_per_share"] == D(".01")
    assert basket["taker_fees_usd"] == D(".23217")  # three fills, half-up to 5dp
    assert basket["net_edge_per_share"] == D("-.036434")
    assert basket["blockers"] == ["fees_or_operation_costs"]
    assert report["summary"]["positive_net_candidates"] == 0


def test_rebate_scenario_never_changes_baseline_or_fills(raw):
    base = deep_scan(raw)
    scenario = deep_scan(raw, rebate=D(".5"))
    for a, b in zip(base["groups"][0]["candidates"], scenario["groups"][0]["candidates"]):
        assert a["net_profit_usd"] == b["net_profit_usd"]
        assert a["legs"] == b["legs"]
        assert b["rebate_scenario_usd"] == b["taker_fees_usd"] / 2
        assert b["net_after_rebate_scenario_usd"] == b["net_profit_usd"] + b["rebate_scenario_usd"]
    for invalid in ("-.1", ".51", "NaN"):
        with pytest.raises(ValueError):
            deep_scan(raw, rebate=invalid)


@pytest.mark.parametrize("case,reason", [("empty", "empty_required_book_side"),
    ("skew", "book_time_skew"), ("minimum", "depth_below_minimum")])
def test_unvalued_candidates_are_null_with_reason(raw, case, reason):
    books = raw["snapshots"][0]["books"]
    if case == "empty":
        books["101"]["asks"] = []
    elif case == "skew":
        books["101"]["timestamp"] = "2026-10-29T23:59:40Z"
    else:
        books["101"]["min_order_size"] = "10000"
    report = deep_scan(raw)
    basket = next(c for c in report["groups"][0]["candidates"] if c["kind"] == "buy_yes_basket")
    assert basket["gross_edge_per_share"] is None
    assert basket["net_edge_per_share"] is None
    assert basket["blockers"] == [reason]


def test_augmented_excludes_baskets_but_reports_binary_candidates(raw):
    raw["snapshots"][0]["events"][0]["negRiskAugmented"] = True
    group = deep_scan(raw)["groups"][0]
    assert not group["complete"]
    assert "augmented_negrisk" in group["blockers"]
    assert {c["kind"] for c in group["candidates"]} == {"buy_binary_merge"}


def test_authoritative_fees_only_and_cannot_enable_trading(raw):
    s = raw["snapshots"][0]
    s["compliance"] = dict(trading_allowed=True, mode="live", allowlist_configured=True)
    m = s["events"][0]["markets"][0]
    m.pop("feeSchedule")
    m["category"] = "crypto"
    report = deep_scan(raw)
    group = report["groups"][0]
    assert group["valued_market_count"] == 2
    assert not group["complete"]
    assert any(e["reason"] == "authoritative fee unavailable" for e in report["errors"])
    assert group["compliance"]["mode"] == "read_only"
    assert group["compliance"]["trading_allowed"] is False
    assert group["geoblock"]["execution"] == "blocked"


@pytest.mark.parametrize("change", ["missing", "condition", "status", "dispute", "review", "malformed", "null_row"])
def test_resolution_missing_mismatch_and_unsafe_state_exclude_market(raw, change):
    responses = raw["snapshots"][0]["resolutions"]
    if change == "missing":
        del responses["1"]
    elif change == "malformed":
        responses["1"] = {"data": []}
    elif change == "null_row":
        responses["1"] = {"data": [None]}
    else:
        key, value = {"condition": ("condition_id", "wrong"), "status": ("status", "resolved"),
                      "dispute": ("was_disputed", True), "review": ("extended_review", True)}[change]
        responses["1"]["data"][0][key] = value
    group = deep_scan(raw)["groups"][0]
    assert not group["complete"]
    assert group["valued_market_count"] == 2
    assert group["resolution_blocks"][0]["market_id"] == "1"
    assert "resolution_exclusions" in group["blockers"]
    assert all(c["kind"] == "buy_binary_merge" for c in group["candidates"])


def test_missing_authoritative_exponent_rejected_only_in_deep_scan(raw):
    s = raw["snapshots"][0]
    s["clob_markets"] = {"1": {"fd": {"r": ".07"}}}
    report = deep_scan(raw)
    assert report["groups"][0]["valued_market_count"] == 2
    assert not report["groups"][0]["complete"]
    assert any(e["reason"] == "authoritative fee exponent unavailable" for e in report["errors"])


def test_selected_event_identity_mismatch_is_not_counted_as_another_group(raw):
    raw["snapshots"][0]["selected_event_id"] = "999"
    report = deep_scan(raw)
    assert report["groups"][0]["event_id"] == "999"
    assert not report["groups"][0]["candidates"]
    assert any(e["reason"] == "selected_event_mismatch" for e in report["errors"])


def test_missing_market_condition_only_excludes_affected_market(raw):
    del raw["snapshots"][0]["events"][0]["markets"][0]["conditionId"]
    group = deep_scan(raw)["groups"][0]
    assert group["valued_market_count"] == 2
    assert not group["complete"]
    assert group["resolution_blocks"] == [{"market_id": "1", "reason": "resolution_missing_or_identity_mismatch"}]


def test_discovery_records_bounds_duplicates_and_repeated_pages():
    def event(eid, count=3, **kw):
        return dict(id=eid, title="synthetic", negRisk=True, active=True, closed=False,
                    markets=[{}] * count, **kw)
    page = [event("1"), event("1"), event("2", 13), event("3"), event("4")]
    class API:
        def get(self, *args):
            return copy.deepcopy(page)
    selected, coverage = discover(API(), 2, 3, 12)
    assert selected == ["1", "3"]
    assert coverage["discovery_exhausted"]
    assert coverage["events_seen"] == 4
    assert [x["reason"] for x in coverage["census"]] == [
        "selected", "market_count_outside_bounds", "selected", "event_limit"]
    page[:] = [event(str(i), 13) for i in range(100)]
    with pytest.raises(ValueError, match="repeated"):
        discover(API(), 2, 3, 12)


def test_collect_uses_only_public_v2_and_preserves_geo_denial(raw):
    s = raw["snapshots"][0]
    class API:
        calls = []
        def get(self, service, path, params=None):
            self.calls.append((service, path, params))
            if service == "gamma":
                return s["events"][0] if path == "/events/1" else s["events"]
            if service == "geo":
                return dict(blocked=True, country="US", region="VA", ip="do-not-record")
            if service == "data":
                return next(r for r in s["resolutions"].values() if r["data"][0]["condition_id"] == params["condition"])
            if path == "/book":
                return s["books"][params["token_id"]]
            return {"fd": {"r": ".07", "e": 1}}
    api = API()
    result = collect_deep(api, 1, 1, 12)
    assert len(result["snapshots"]) == 1
    assert len([c for c in api.calls if c[0] == "data"]) == 3
    assert all(c[1] == "/v2/resolutions" for c in api.calls if c[0] == "data")
    assert result["snapshots"][0]["compliance"]["trading_allowed"] is False
    assert "do-not-record" not in dumps(result)


def test_transport_rejects_v1_and_account_or_write_routes():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": []})
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        api = ReadOnlyAPI(client=client)
        assert api.get("data", "/v2/resolutions") == {"data": []}
        for service, path in [("data", "/resolutions"), ("data", "/v2/positions"),
                              ("clob", "/order"), ("data", "/v2/resolutions?evil")]:
            with pytest.raises(ValueError):
                api.get(service, path)
    assert len(calls) == 1


def test_fixture_cli_replay_and_invalid_bounds_make_zero_network_calls(raw, tmp_path):
    source, out = tmp_path / "snapshot.json", tmp_path / "report.json"
    source.write_text(dumps(raw))
    with patch("socket.socket", side_effect=AssertionError("network forbidden")):
        assert main(["--fixture", str(source), "--json", str(out)]) == 0
        assert json.loads(out.read_text()) == json.loads(dumps(deep_scan(raw)))
        for flags in (["--max-events", "0"], ["--rebate-scenario-rate", ".51"], ["--max-shares", "0"]):
            with pytest.raises(SystemExit):
                main(["--json", str(out), *flags])


def test_recorded_live_report_replays_offline():
    source = ROOT / "research/fixtures/negrisk-deep/20261030/snapshot.json.gz"
    expected = ROOT / "docs/reports/negrisk-deep-2026-10-30.json"
    with patch("socket.socket", side_effect=AssertionError("network forbidden")):
        report = deep_scan(load(source), Costs(D(".01"), D(".01"), D(".01")))
    assert legacy_report(json.loads(dumps(report)), json.loads(expected.read_text())) == json.loads(expected.read_text())
    assert report["summary"]["groups_scanned"] >= 20
    assert report["execution"] == "blocked"
