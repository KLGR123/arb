from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest

from arb_research.cross_venue import candidates, edge, kalshi_books, markdown, normalize_poly, scan
from arb_research.cross_venue_cli import collect, main
from arb_research.kalshi import KalshiPublicAPI, discover, identifier
from arb_research.negrisk import D, dumps
from arb_research.rule_alignment import MODEL, RuleAligner, run_codex


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parent / "fixtures/cross-venue-contract.json").read_text())


def alignment(relation="same", differences=None):
    return {"relation": relation, "alignment": 1.0, "source_comparison": "Same CF index",
            "deadline_comparison": "Same observation instant", "boundary_comparison": "Strictly greater",
            "differences": differences or [], "explanation": "All specified binary payout conditions match"}


def aligner(tmp_path, relation="same", differences=None):
    return RuleAligner(tmp_path, runner=lambda *_: alignment(relation, differences))


def test_match_filters_time_title_underlying_and_active(snapshot):
    p, k = snapshot["polymarket"], snapshot["kalshi"]
    assert candidates(p, k)[0]["poly_id"] == "p1"
    k[0]["close_time"] = "2027-10-31T00:00:00Z"
    assert not candidates(p, k)
    k[0]["close_time"] = p[0]["close_time"]
    k[0]["title"] = "Ethereum at $100000 on October 31?"
    assert not candidates(p, k)
    k[0]["active"] = False
    assert not candidates(p, k)


def test_fixed_point_and_legacy_books_complement(snapshot):
    raw = snapshot["kalshi_books"]["KXBTC-TEST"]
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    fixed = kalshi_books(raw, now)
    assert fixed["yes"]["asks"] == [(D(".57"), D(9))]
    raw["raw"] = {"orderbook": {"yes": [[55, 7]], "no": [[43, 9]]}}
    assert kalshi_books(raw, now) == fixed
    raw["raw"]["orderbook"]["no"] = [[99, 3]]
    with pytest.raises(ValueError, match="crossed"):
        kalshi_books(raw, now)


def test_fee_rounding_depth_and_complementary_scenarios(snapshot, tmp_path):
    report = scan(snapshot, aligner(tmp_path))
    assert report["execution"] == "blocked"
    assert report["compliance"]["trading_allowed"] is False
    row = report["pairs"][0]
    quote = row["quotes"][0]
    assert quote["quantity"] == 7  # Never extrapolate beyond best price capacity.
    assert quote["kalshi_fee_usd"] == D(".13")  # ceil(7 * .07 * .55 * .45)
    assert quote["poly_fee_usd"] == D(".11760")
    assert quote["net_profit_usd"] == D(".80240")
    assert quote["inventory_required"]
    assert row["settlement_equivalent"] is True
    assert row["liquidity"]["kalshi"]["yes"]["bids"]["top_size"] == 7
    assert any(q["kind"] == "buy_both_complementary" for q in row["quotes"])
    assert "high" in markdown(report)


@pytest.mark.parametrize("relation,differences", [("different", []), ("uncertain", []), ("same", ["different fallback"] )])
def test_rule_mismatch_never_produces_edge(snapshot, tmp_path, relation, differences):
    row = scan(snapshot, aligner(tmp_path, relation, differences))["pairs"][0]
    assert row["risk"] == "critical"
    assert row["settlement_equivalent"] is False
    assert not row["quotes"]
    assert row["price_differences"]  # Price differences can be observed without payout equivalence.
    assert "Ask-price differences" in markdown(scan(snapshot, aligner(tmp_path, relation, differences)))


def test_inverse_maps_to_opposite_kalshi_outcome(snapshot, tmp_path):
    quotes = scan(snapshot, aligner(tmp_path, "inverse"))["pairs"][0]["quotes"]
    assert quotes[0]["poly_outcome"] == "yes" and quotes[0]["kalshi_outcome"] == "no"


@pytest.mark.parametrize("failure", ["inactive", "closed", "close_gap", "underlying"])
def test_market_closed_after_discovery_suppresses_edges(snapshot, tmp_path, failure):
    if failure == "inactive":
        snapshot["kalshi"][0]["active"] = False
    elif failure == "closed":
        snapshot["kalshi"][0]["close_time"] = "2026-10-29T00:00:00Z"
    elif failure == "close_gap":
        snapshot["kalshi"][0]["close_time"] = "2027-10-29T00:00:00Z"
    else:
        snapshot["kalshi"][0]["title"] = "Ethereum October 31"
    row = scan(snapshot, aligner(tmp_path))["pairs"][0]
    assert not row["quotes"]
    assert ("market_inactive_closed_or_close_time_unknown" in row["risks"]
            or "candidate_changed_since_discovery" in row["risks"])


@pytest.mark.parametrize("failure", ["fee", "stale", "asset", "missing", "nan", "source"])
def test_missing_invalid_data_suppresses_edges(snapshot, tmp_path, failure):
    if failure == "fee":
        del snapshot["poly_fees"]["p1"]
    elif failure == "stale":
        snapshot["kalshi_books"]["KXBTC-TEST"]["received_at"] = "2026-10-29T00:00:00Z"
    elif failure == "asset":
        snapshot["poly_books"]["py"]["asset_id"] = "wrong"
    elif failure == "missing":
        del snapshot["poly_books"]["py"]
    elif failure == "nan":
        snapshot["kalshi"][0]["fee_multiplier"] = "NaN"
    else:
        snapshot["poly_fees"]["p1"][1] = "category_estimate"
    report = scan(snapshot, aligner(tmp_path))
    assert not report["pairs"][0]["quotes"]
    assert report["errors"]


def test_skew_and_insufficient_depth_are_explicit(snapshot, tmp_path):
    snapshot["poly_books"]["py"]["timestamp"] = "2026-10-29T23:59:40Z"
    snapshot["poly_books"]["pn"]["timestamp"] = "2026-10-29T23:59:40Z"
    report = scan(snapshot, aligner(tmp_path))
    row = report["pairs"][0]
    assert not row["quotes"]
    assert "unsynchronized books" in row["risks"]
    assert report["errors"][0]["reason"] == "no_synchronized_liquid_quote"


def test_cache_replay_hash_changes_and_validation(snapshot, tmp_path):
    calls = []
    def runner(prompt, schema):
        calls.append(prompt)
        return alignment()
    a = RuleAligner(tmp_path, runner=runner)
    p, k = snapshot["polymarket"][0], snapshot["kalshi"][0]
    first = a.compare(p, k)
    assert first["model"] == MODEL
    assert a.compare(p, k) == first and len(calls) == 1
    k["rules"] += " New clarification"
    assert a.compare(p, k)["key"] != first["key"] and len(calls) == 2
    first["output"]["alignment"] = "1"
    path = tmp_path / (first["key"] + ".json")
    path.write_text(json.dumps(first))
    k["rules"] = k["rules"].removesuffix(" New clarification")
    with pytest.raises(ValueError):
        a.compare(p, k)


def test_offline_cli_never_calls_model_or_network(snapshot, tmp_path, monkeypatch):
    a = aligner(tmp_path / "rules")
    a.compare(snapshot["polymarket"][0], snapshot["kalshi"][0])
    fixture = tmp_path / "snapshot.json"
    fixture.write_text(json.dumps(snapshot))
    def forbidden(*_, **__):
        raise AssertionError("offline path must not access network/model")
    monkeypatch.setattr(httpx.Client, "get", forbidden)
    monkeypatch.setattr("arb_research.rule_alignment.run_codex", forbidden)
    assert main(["--fixture", str(fixture), "--cache", str(tmp_path/"rules"), "--json", str(tmp_path/"out.json")]) == 0
    assert main(["--fixture", str(fixture), "--cache", str(tmp_path/"missing"), "--json", str(tmp_path/"out.json")]) == 2


class Bucket:
    def __init__(self):
        self.calls = 0
    def acquire(self):
        self.calls += 1


def test_public_transport_allowlist_retries_and_secret_sanitization():
    requests, delays = [], []
    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert not any("key" in k.lower() or "authorization" in k.lower() for k in request.headers)
        return httpx.Response(429 if len(requests) == 1 else 200, json={"events": []}, headers={"Retry-After":"2"})
    bucket = Bucket()
    with KalshiPublicAPI(httpx.Client(transport=httpx.MockTransport(handle)), bucket, delays.append) as api:
        assert api.get("/events") == {"events": []}
        for path in ("/portfolio/orders", "/markets/../../orders", "/events?secret=1", "https://elsewhere.test"):
            with pytest.raises(ValueError):
                api.get(path)
    assert delays == [2] and bucket.calls == 2
    def failed(request):
        raise httpx.ConnectError("socks5h://password@private-proxy", request=request)
    with KalshiPublicAPI(httpx.Client(transport=httpx.MockTransport(failed)), attempts=1) as api:
        with pytest.raises(OSError) as exc:
            api.get("/events")
        assert "password" not in str(exc.value) and "private-proxy" not in str(exc.value)


def test_pagination_detects_repeated_cursors():
    class API:
        def get(self, path, params):
            return {"events": [], "cursor": "repeat"}
    with pytest.raises(ValueError, match="repeated"):
        discover(API(), pages=3)


def test_decimal_threshold_identifiers_are_public_paths():
    assert identifier("KXFED-26DEC-T3.75") == "KXFED-26DEC-T3.75"
    for value in ("..", ".hidden", "KX/../orders", "KX..T", "KX?secret=1"):
        with pytest.raises(ValueError):
            identifier(value)
    seen = []
    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"market": {"ticker": "KXFED-26DEC-T3.75"}})
    with KalshiPublicAPI(httpx.Client(transport=httpx.MockTransport(handle))) as api:
        api.get("/markets/" + identifier("KXFED-26DEC-T3.75"))
    assert seen == ["/trade-api/v2/markets/KXFED-26DEC-T3.75"]


def test_explicit_event_prefers_populated_nested_markets():
    class API:
        def get(self, path, params):
            return {"event": {"event_ticker":"KXFED-26DEC", "markets":[{"ticker":"KXFED-26DEC-T3.75"}]},
                    "markets": []}
    events, markets = discover(API(), event_tickers=["KXFED-26DEC"])
    assert events[0]["event_ticker"] == "KXFED-26DEC"
    assert markets == [{"ticker":"KXFED-26DEC-T3.75"}]


def test_poly_v2_asset_ids_preserved():
    p = normalize_poly({"id": "e"}, {"id": "p", "question": "Test", "version": "v2",
        "positionIds": '["new-yes", "new-no"]', "clobTokenIds": '["old-yes", "old-no"]', "outcomes": '["Yes","No"]'})
    assert p["tokens"] == {"yes":"new-yes", "no":"new-no"}


@pytest.mark.parametrize("quantity", [D("NaN"), D(0), D("1.5")])
def test_invalid_quantity_rejected_before_work(snapshot, tmp_path, quantity):
    with pytest.raises(ValueError):
        scan(snapshot, aligner(tmp_path), quantity=quantity)


def test_local_model_command_and_environment_boundary(monkeypatch):
    monkeypatch.setenv("ARB_POLYMARKET_PROXY", "socks5h://secret-host:123")
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward-this")
    monkeypatch.setenv("MULTICA_TOKEN", "never-forward-token")
    monkeypatch.setenv("ARB_CODEX_BIN", "/local/codex")
    def execute(command, **kwargs):
        assert command[0] == "/local/codex" and command[command.index("--model")+1] == MODEL
        assert command[command.index("--sandbox")+1] == "read-only"
        assert "--ephemeral" in command
        assert not any(any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PROXY")) for k in kwargs["env"])
        assert kwargs["input"] == "public rules only" and kwargs["timeout"] == 240
        Path(command[command.index("--output-last-message")+1]).write_text(json.dumps(alignment()))
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr("subprocess.run", execute)
    assert run_codex("public rules only", {}) == alignment()


def test_collector_contract_and_guard_use(snapshot):
    p, k = snapshot["polymarket"][0], snapshot["kalshi"][0]
    pmarket = {"id": "p1", "question": p["title"], "description": p["rules"],
               "outcomes": ["Yes", "No"], "clobTokenIds": ["py", "pn"],
               "active": True, "closed": False, "endDate": p["close_time"], "conditionId": "condition-1"}
    pevent = {"id":"e1", "title":"Bitcoin", "markets": [pmarket]}
    kmarket = {"ticker": k["id"], "event_ticker": k["event_id"], "title": k["title"],
               "close_time": k["close_time"], "expiration_time": k["settlement_time"],
               "status":"active", "rules_primary": k["rules"]}
    kevent = {"event_ticker": k["event_id"], "series_ticker":"KXBTC", "title":"Bitcoin"}
    class Poly:
        def get(self, service, path, params=None):
            if service == "geo":
                return {"blocked":True,"country":"CN","region":""}
            if service == "gamma":
                return pevent
            if path.startswith("/clob-markets/"):
                return {"fd":{"r":"0.07","e":1}}
            return snapshot["poly_books"][params["token_id"]]
    class Kalshi:
        def get(self, path, params=None):
            if path.startswith("/events/"):
                return {"event": kevent, "markets":[kmarket]}
            if path.startswith("/series/"):
                return {"series":{"fee_type":"quadratic","fee_multiplier":1,"settlement_sources":[]}}
            if path.endswith("/orderbook"):
                return snapshot["kalshi_books"][k["id"]]["raw"]
            return {"market": kmarket}
    result = collect(Poly(), Kalshi(), event_ids=["e1"], kalshi_events=[k["event_id"]])
    assert len(result["candidates"]) == 1 and not result["errors"]
    assert result["compliance"]["geoblock"]["blocked"] is True
    assert result["compliance"]["trading_allowed"] is False
    assert result["poly_fees"]["p1"] == [D(".07"), "clob"]


def test_recorded_real_pairing_replays_without_network_or_model(monkeypatch):
    root = Path(__file__).parent.parent / "fixtures/cross-venue/20261030"
    snapshot = json.loads((root / "snapshot.json").read_text())
    expected = json.loads((root / "report.json").read_text())
    def forbidden(*_, **__):
        raise AssertionError("recorded replay must not call network or model")
    monkeypatch.setattr(httpx.Client, "get", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    report = scan(snapshot, RuleAligner(root / "rules", cache_only=True))
    assert json.loads(dumps(report)) == expected
    assert report["coverage"]["poly_markets"] == 5
    assert report["coverage"]["kalshi_markets"] == 11
    assert len(report["pairs"]) == 3 and not report["errors"]
    assert report["summary"]["settlement_equivalent"] == 0
    assert all(p["risk"] == "critical" and not p["quotes"] for p in report["pairs"])
