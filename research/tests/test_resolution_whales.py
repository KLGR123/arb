from copy import deepcopy
import json
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from arb_research.resolution_whales import (
    SCHEMA, feature_rows, instant, read_features, report, validate_sample, write_features,
)
from arb_research.resolution_whales_cli import (
    PublicAPI, PublicReadError, load, normalize_resolution, normalize_uma, pages,
)


@pytest.fixture
def samples():
    return json.loads((Path(__file__).parent / "fixtures/resolution-whales-contract.json").read_text())["samples"]


def test_receipt_time_and_future_invariance(samples):
    cut = "2026-10-28T12:00:00Z"
    assert feature_rows(samples, ["2026-10-28T11:59:59Z"]) == []
    prefix = feature_rows(samples[:1], [cut])
    assert feature_rows(samples, [cut]) == prefix
    assert prefix[0]["resolution_status"] == "disputed"
    assert prefix[0]["risk_score"] == .7
    assert not any("payout" in k or "resolved_at" in k for k in prefix[0])
    changed = deepcopy(samples)
    changed[1]["resolution"]["payouts"] = [1, 0]
    assert feature_rows(changed, [cut]) == prefix


def test_concentration_and_threshold_flows(samples):
    row = feature_rows(samples, [samples[0]["observed_at"]], top_n=1)[0]
    assert row["observed_holder_count"] == 2  # case-insensitive across both tokens
    assert row["observed_gross_shares"] == 100
    assert row["top_n_share_of_observed_holdings"] == .8
    assert row["observed_hhi"] == pytest.approx(.68)
    assert row["large_trade_count"] == 2
    assert row["large_trade_net_usd"] == 0
    assert row["large_trade_buy_usd"] == 1000
    assert not row["holders_walk_exhausted"]
    high = feature_rows(samples, [samples[0]["observed_at"]], threshold_usd=1001)[0]
    assert high["large_trade_count"] == 0
    short = feature_rows(samples, [samples[0]["observed_at"]], window_seconds=60)[0]
    assert short["large_trade_count"] == 0


def test_missing_is_not_zero(samples):
    s = samples[0]
    s.update(holders=None, trades=None, resolution=None)
    row = feature_rows([s], [s["observed_at"]])[0]
    assert row["observed_gross_shares"] is None
    assert row["large_trade_count"] is None
    assert row["risk_score"] is None
    assert row["peer_dispute_rate"] is None
    row2 = feature_rows(samples, [samples[1]["observed_at"]])[0]
    assert row2["observed_gross_shares"] == 0
    assert row2["top_n_share_of_observed_holdings"] is None


def test_report_preserves_missing_feed_and_payout_denominators(samples):
    s = samples[0]
    s["trades"] = None
    r = report([s], as_of=s["observed_at"])
    m = r["markets"][0]
    assert m["large_buy_count"] is None
    assert m["labeled_large_buy_count"] is None
    assert m["trades_available"] is False
    assert all(w["sampled_buy_usd"] is None and w["sampled_sell_usd"] is None
               for w in m["whales_retrospective"])
    s["trades"] = []
    assert report([s], as_of=s["observed_at"])["markets"][0]["large_buy_count"] == 0
    terminal = samples[1]
    terminal["resolution"]["payouts"] = None
    r = report([terminal], as_of=terminal["observed_at"])
    assert all(g["payout_known_count"] == 0 and g["payout_unknown_count"] == 1
               and g["split_50_50_rate"] is None for g in r["groups"])


@pytest.mark.parametrize("field,value", [("price", float("nan")), ("size", -1),
    ("timestamp", 9999999999), ("condition_id", "other"), ("token_id", "other"),
    ("outcome_index", 999), ("side", "EXECUTE")])
def test_invalid_observations_rejected(samples, field, value):
    samples[0]["trades"][0][field] = value
    with pytest.raises(ValueError):
        validate_sample(samples[0])


def test_duplicate_holders_and_conflicting_snapshots(samples):
    s = deepcopy(samples[0])
    s["holders"][0]["holders"].append(s["holders"][0]["holders"][0])
    with pytest.raises(ValueError, match="holder"):
        validate_sample(s)
    s = deepcopy(samples[0])
    s["market"]["category"] = "sports"
    with pytest.raises(ValueError, match="conflicting"):
        feature_rows([samples[0], s], [s["observed_at"]])


def test_peer_rate_uses_only_available_other_markets(samples):
    peer = deepcopy(samples[1])
    peer["market"]["market_id"] = "peer"
    early = feature_rows(samples + [peer], [samples[0]["observed_at"]])[0]
    assert early["peer_resolved_count"] == 0
    late = feature_rows(samples + [peer], [samples[1]["observed_at"]])[0]
    assert late["peer_resolved_count"] == 1
    assert late["peer_dispute_rate"] == 1


def test_parquet_round_trip_and_backtest_reader(samples, tmp_path):
    path = tmp_path / "features.parquet"
    rows = feature_rows(samples, [s["observed_at"] for s in samples])
    write_features(rows, path)
    assert pq.read_table(path).schema.equals(SCHEMA, check_metadata=True)
    assert read_features(path, as_of="2026-10-28T11:59:59Z") == []
    assert read_features(path, as_of=samples[0]["observed_at"])[0]["resolution_status"] == "disputed"
    assert read_features(path, as_of=samples[1]["observed_at"])[0]["resolution_status"] == "resolved"
    rows[0]["available_at"] = instant("2026-11-01T00:00:00Z")
    write_features(rows, path)
    with pytest.raises(ValueError, match="availability"):
        read_features(path, as_of="2026-11-02T00:00:00Z")
    write_features([], path)
    assert read_features(path, as_of="2026-11-02T00:00:00Z") == []


def test_report_does_not_invent_dispute_start(samples):
    r = report(samples[1:], as_of=samples[1]["observed_at"])
    assert r["markets"][0]["split_50_50"] is True
    assert r["markets"][0]["observed_dispute_to_resolution_seconds"] is None
    r = report(samples, as_of=samples[1]["observed_at"])
    assert r["markets"][0]["observed_dispute_to_resolution_seconds"] == 86400
    assert r["markets"][0]["dispute_sampled_price_range"] == pytest.approx(.6)


def test_official_sdk_micro_payout_normalization():
    raw = dict(condition_id="0x" + "1" * 64, status="resolved", extended_review=False,
        was_disputed=True, new_version_q=False, transaction_hash="", log_index="",
        last_update_timestamp="2026-10-28T10:00:00Z", payouts=[500000, 500000],
        resolved_at="2026-10-28T10:00:00Z", proposed_price="69")
    parsed = normalize_resolution(raw)
    assert parsed["payouts"] == [.5, .5]
    assert parsed["proposed_price"] is None


def test_cursor_walk_retains_filters_and_marks_truncation():
    class API:
        def __init__(self):
            self.calls = []
        def get(self, service, path, query):
            self.calls.append(query)
            return dict(data=[len(self.calls)], pagination=dict(has_more=len(self.calls) == 1,
                next_cursor="opaque" if len(self.calls) == 1 else None))
    api = API()
    assert pages(api, "/v2/trades", {"condition": "c", "taker_only": "true"}, 2) == ([1, 2], True)
    assert api.calls[1] == {"condition": "c", "taker_only": "true", "cursor": "opaque"}
    assert pages(API(), "/v2/trades", {}, 1) == ([1], False)


def test_repeated_cursor_fails():
    class API:
        def get(self, *_):
            return dict(data=[], pagination=dict(has_more=True, next_cursor="same"))
    with pytest.raises(ValueError, match="Repeated"):
        pages(API(), "/v2/holders", {}, 3)


def test_public_transport_is_get_only_and_retries_without_secrets(monkeypatch):
    calls, delays = [], []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"data": []})
    with PublicAPI(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=delays.append) as api:
        assert api.get("data", "/v2/resolutions", {"condition": "c"}) == {"data": []}
        for service, path in [("data", "/trades"), ("data", "/order"),
                              ("gamma", "/events/../orders"), ("geo", "/api/geoblock?x=1")]:
            with pytest.raises(ValueError):
                api.get(service, path)
    assert all(r.method == "GET" for r in calls)
    assert delays == [2]
    def failed(request):
        raise httpx.ConnectError("socks://SECRET@PRIVATE", request=request)
    monkeypatch.delenv("ARB_POLYMARKET_PROXY", raising=False)
    with PublicAPI(client=httpx.Client(transport=httpx.MockTransport(failed)), sleep=lambda _: None) as api:
        with pytest.raises(PublicReadError, match="unset") as exc:
            api.get("data", "/v2/holders")
        assert "SECRET" not in str(exc.value)


def test_naive_time_and_invalid_parameters(samples):
    with pytest.raises(ValueError, match="offset"):
        instant("2026-10-28T10:00:00")
    for kwargs in ({"top_n": 0}, {"window_seconds": 0}, {"threshold_usd": float("nan")}):
        with pytest.raises(ValueError):
            feature_rows(samples, [samples[0]["observed_at"]], **kwargs)


def test_whale_cohort_is_frozen_before_window(samples):
    early, late = samples
    assert feature_rows(samples, [early["observed_at"]])[0]["whale_net_flow_usd"] is None
    trade = deepcopy(early["trades"][0])
    trade.update(timestamp=int(instant("2026-10-29T11:00:00Z").timestamp()), size=50)
    late["trades"] = [trade]
    row = feature_rows(samples, [late["observed_at"]], top_n=1)[0]
    assert row["whale_cohort_size"] == 1
    assert row["whale_net_flow_usd"] == 25
    assert row["whale_cohort_available_at"] == instant(early["observed_at"])
    # Changing today's holder table cannot choose the earlier address cohort.
    late["holders"] = [{"token_id": "y", "holders": [{"proxy_wallet": "0xNEW", "amount": 99999}]}]
    assert feature_rows(samples, [late["observed_at"]], top_n=1)[0]["whale_net_flow_usd"] == 25


def test_recorded_resolution_and_uma_identity():
    raw = load(Path(__file__).parent / "fixtures/resolution-whales-upstream.json")
    r = normalize_resolution(raw["resolution"], outcomes=["Yes", "No"])
    assert r["payouts"] == [0, 1]
    assert r["payout_basis"] == "uma_final_price"
    assert r["resolved_at"] is None
    assert normalize_resolution(raw["resolution"], outcomes=["No", "Yes"])["payouts"] is None
    u = normalize_uma(raw["uma"], raw["resolution"])
    assert int(u["settlementTimestamp"]) - int(u["disputeTimestamp"]) == 343115
    raw["uma"]["entity"]["id"] = "unrelated"
    with pytest.raises(ValueError, match="identity"):
        normalize_uma(raw["uma"], raw["resolution"])


def test_uma_transport_uses_separate_direct_client():
    calls = []
    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"data": []})
    def poly_only(request):
        assert request.url.host != "oracle.uma.xyz"
        return httpx.Response(200, json={"data": []})
    with PublicAPI(client=httpx.Client(transport=httpx.MockTransport(poly_only)),
                   uma_client=httpx.Client(transport=httpx.MockTransport(handle))) as api:
        api.get("uma", "/api/resolve-deeplink", {"transactionHash": "x", "chainId": 137})
    assert calls == ["oracle.uma.xyz"]


def test_recorded_live_samples_replay_offline(tmp_path):
    root = Path(__file__).parent.parent / "fixtures/resolution-whales"
    raw = [load(root / name) for name in ("live-20261030.json.gz", "disputed-20261030.json.gz")]
    samples = [s for batch in raw for s in batch["samples"]]
    cutoff = max(instant(s["observed_at"]) for s in samples)
    assert len(samples) == 9
    assert all(batch["compliance"]["trading_allowed"] is False for batch in raw)
    rows = feature_rows(samples, [cutoff])
    write_features(rows, tmp_path / "live.parquet")
    assert len(read_features(tmp_path / "live.parquet", as_of=cutoff)) == 9
    r = report(samples, as_of=cutoff)
    disputed = next(m for m in r["markets"] if m["market_id"] == "546814")
    assert disputed["was_disputed"] is True
    assert disputed["oracle_dispute_to_settlement_seconds"] == 343115
    assert disputed["dispute_sampled_price_range"] is not None
