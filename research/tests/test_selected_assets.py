"""One frozen identity contract exercised by Python and the TypeScript dashboard."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arb_research.negrisk import assets, scan
from arb_research.negrisk_cli import collect
from arb_research.polymarket.api import create_app
from arb_research.polymarket.assets import select_assets, stream_key
from arb_research.polymarket.models import Market, Event, PricePage, PricePoint
from arb_research.polymarket.store import DataStore
from arb_research.polymarket.sync import sync_event
from test_polymarket import Replay, client, NOW

CASES = json.loads((Path(__file__).parent / "fixtures/selected-assets.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_shared_contract(case):
    view = select_assets(case["raw"], event_id="100", observed_at="2026-10-30T00:00:00Z")
    assert view.model_dump(mode="json") == case["selection"]
    assert json.loads(view.model_dump_json()) == case["selection"]
    if view.status == "selected":
        assert assets(case["raw"]) == {a.outcome.lower(): a.asset_id for a in view.assets}
    else:
        assert not view.assets
        with pytest.raises(ValueError, match=view.reason):
            assets(case["raw"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_loader_sync_api_share_identity_and_quarantine(tmp_path, case):
    replay, store = Replay(), DataStore(tmp_path / "data")
    replay.fixture["market"].update(case["raw"])
    for key in ("version", "conditionId", "outcomes", "positionIds", "clobTokenIds"):
        if key not in case["raw"]:
            replay.fixture["market"].pop(key, None)
    # Use the valid fixture condition for the SDK resolution parser. The shared
    # boundary's missing-condition case is covered above and directly below.
    if "conditionId" not in case["raw"]:
        with client(tmp_path, replay) as api:
            assert api.event("100").markets[0].asset_selection.reason == "missing_condition_id"
        return
    replay.fixture["market"]["conditionId"] = replay.fixture["resolutions"]["data"][0]["condition_id"]
    with client(tmp_path, replay) as api:
        result = sync_event(api, store, "100", now=NOW)
    requested = {r.url.params["token_id"] for r in replay.requests if r.url.path == "/v2/prices-history"}
    expected = {a["asset_id"] for a in case["selection"]["assets"]}
    assert requested == expected
    assert bool(result.quarantined_markets) == (case["selection"]["status"] == "quarantined")
    with TestClient(create_app(str(store.root))) as api:
        market = api.get("/markets/200").json()
        assert {a["asset_id"] for a in market["asset_selection"]["assets"]} == expected
        rows = api.get("/events/100/prices").json()["data"]
        assert {r["token_id"] for r in rows} == expected
        assert api.get("/markets/200/prices?token_id=300").json()["data"] == [r for r in rows if r["token_id"] == "300"]


def test_quarantined_scanner_does_not_request_any_books_or_fees():
    raw = json.loads((Path(__file__).parent / "fixtures/negrisk-contract.json").read_text())
    for market in raw["events"][0]["markets"]:
        market.pop("version")
    class API:
        def get(self, service, path, params=None):
            if service == "geo":
                return {"blocked": True, "country": "US", "region": "VA"}
            assert service == "gamma", "Quarantined assets reached CLOB"
            return raw["events"]
    result = collect(API(), max_events=1)
    assert not result["books"] and not result["clob_markets"]
    report = scan(result)
    assert not report["opportunities"]
    market_errors = [e for e in report["errors"] if e.get("stage") != "group"]
    group_errors = [e for e in report["errors"] if e.get("stage") == "group"]
    assert market_errors and group_errors
    assert all("missing_protocol_version" in e["reason"] for e in market_errors)
    assert all(e["reason"] == "incomplete_negrisk_group" for e in group_errors)


def test_historical_rows_and_cursors_never_cross_ledgers(tmp_path):
    store = DataStore(tmp_path)
    raw = {"id": "200", "version": "v1", "outcomes": ["Yes", "No"],
           "clobTokenIds": ["9007199254740993", "2"],
           "positionIds": ["9007199254740993", "2"], "conditionId": "0xcondition"}
    view = select_assets(raw, event_id="100")
    market = Market(id="200", event_id="100", condition_id="0xcondition", protocol="v1",
                    tokens=[], asset_selection=view)
    event = Event(id="100", markets=[market])
    page = PricePage(data=[PricePoint(timestamp=1, price=.4, resolution_seconds=0)])
    old = view.assets[0]
    store.commit_page("200", old.asset_id, page, {"through": 1, "pending": None}, asset=old)
    assert store.history(event).data[0].token_id == old.asset_id
    raw["version"] = "v2"
    market.protocol = "v2"
    market.asset_selection = select_assets(raw, event_id="100")
    new = market.asset_selection.assets[0]
    assert store.history(event).data == []
    assert stream_key(old) != stream_key(new)
    assert store.checkpoint(stream_key(new))["through"] == 0
    page.data[0].price = .7
    store.commit_page("200", new.asset_id, page, {"through": 1, "pending": None}, asset=new)
    assert [p.price for p in store.history(event).data] == [.7]
    market.asset_selection = select_assets({**raw, "version": "future"}, event_id="100")
    assert store.history(event).data == []
    legacy = market.model_dump()
    legacy.pop("asset_selection")
    assert Market.model_validate(legacy).asset_selection.reason == "identity_view_missing_resync_required"


@pytest.mark.parametrize("module", [None, "3", "future", "2", 2, "neg_risk", "combo", "binary"])
def test_v2_negrisk_valuation_requires_explicit_supported_module(module):
    raw = json.loads((Path(__file__).parent / "fixtures/negrisk-contract.json").read_text())
    for market in raw["events"][0]["markets"]:
        market.update(version="v2", positionIds=market["clobTokenIds"], module=module)
    report = scan(raw)
    if module == "neg_risk":
        assert report["opportunities"] and not report["errors"]
    else:
        assert not report["opportunities"]
        market_errors = [e for e in report["errors"] if e.get("stage") == "market"]
        assert market_errors
        assert all(e["reason"] in {"unsupported_or_missing_negrisk_module", "unsupported_combo_module"}
                   for e in market_errors)
