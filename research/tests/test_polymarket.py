import copy
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from arb_research.polymarket.api import create_app
from arb_research.polymarket.cli import main
from arb_research.polymarket.client import PublicDataClient, SharedLimiter, UpstreamError
from arb_research.polymarket.models import PricePage, PricePoint
from arb_research.polymarket.store import DataStore
from arb_research.polymarket.sync import sync_event, WINDOW
from arb_research.compliance import ComplianceDenied

FIXTURE = json.loads((Path(__file__).parent / "fixtures/polymarket/contract.json").read_text())
NOW = 1793100000


class Replay:
    def __init__(self):
        self.fixture = copy.deepcopy(FIXTURE)
        self.requests = []
        self.fail_token = None
        self.fail_cursor = None

    def __call__(self, request):
        assert request.method == "GET"
        self.requests.append(request)
        path = request.url.path
        if path == "/api/geoblock":
            body = {"blocked": True, "country": "US", "region": "VA"}
        elif path in ["/events/100", "/events/slug/contract-event"]:
            body = self.fixture["event"]
        elif path == "/markets/200":
            body = self.fixture["market"]
        elif path == "/markets/200/tags":
            body = self.fixture["tags"]
        elif path == "/v2/resolutions":
            assert request.url.params["event_id"] == "100"
            body = self.fixture["resolutions"]
        elif path == "/v2/prices-history":
            token, cursor = request.url.params["token_id"], request.url.params.get("cursor")
            if token == self.fail_token and cursor == self.fail_cursor:
                raise httpx.ReadTimeout("injected timeout", request=request)
            body = self.fixture["prices"][1 if cursor else 0]
        else:
            pytest.fail(f"Unexpected network request: {request.url}")
        return httpx.Response(200, json=body)


def client(tmp_path, replay):
    return PublicDataClient(transport=httpx.MockTransport(replay),
                            limiter=SharedLimiter(tmp_path / "limits.sqlite3"), sleep=lambda _s: None)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    # Any accidental use of the real HTTP transport, including query handlers,
    # fails immediately. Recorded-response transport remains usable.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Tests must never call the external network")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def test_metadata_protocol_tokens_rules_tags_split_settlement(tmp_path):
    replay = Replay()
    with client(tmp_path, replay) as api:
        event = api.event("contract-event")
        market = event.markets[0]
        assert event.tags[0].label == "Contract"
        assert market.tags[0].slug == "contract"
        assert market.neg_risk_group == replay.fixture["event"]["negRiskMarketID"]
        assert market.protocol == "v2" and market.rules.startswith("Resolve according")
        assert [(t.id, t.protocol) for t in market.tokens] == [
            ("300", "v1"), ("400", "v2"), ("301", "v1"), ("401", "v2")]
        resolution = api.resolutions(event)[0]
        assert resolution.payouts == [0.5, 0.5] and resolution.split_50_50
        assert resolution.was_disputed and resolution.status == "resolved"
        replay.fixture["market"].pop("version")
        assert api.event("100").markets[0].protocol == "unknown"


def test_sync_resume_idempotence_incremental_and_offline_queries(tmp_path, capsys):
    replay, store = Replay(), DataStore(tmp_path / "data")
    replay.fail_token, replay.fail_cursor = "300", "opaque-page-2"
    with client(tmp_path, replay) as api:
        with pytest.raises(UpstreamError):
            sync_event(api, store, "100", now=NOW)
        assert store.checkpoint("300")["pending"]["cursor"] == "opaque-page-2"
        assert len(store.history(store.event("100")).data) == 1
        replay.fail_token = None
        replay.requests.clear()
        result = sync_event(api, DataStore(store.root), "100", now=NOW)
        assert result.compliance["trading_allowed"] is False
        assert result.compliance["geoblock"]["blocked"] is True
        prices = [r for r in replay.requests if r.url.path == "/v2/prices-history"]
        assert prices[0].url.params["cursor"] == "opaque-page-2"
        assert result.empty_token_ids == [] and result.missing_resolution_market_ids == []
        assert len(store.history(store.event("100")).data) == 8
        replay.requests.clear()
        sync_event(api, store, "100", now=NOW)
        assert not any(r.url.path == "/v2/prices-history" for r in replay.requests)
        assert len(store.history(store.event("100")).data) == 8
        sync_event(api, store, "100", now=NOW+WINDOW*2)
        windows = [r.url.params for r in replay.requests if "start" in r.url.params]
        assert windows and all(int(p["end"])-int(p["start"]) <= WINDOW for p in windows)
        assert store.checkpoint("300")["through"] == NOW+WINDOW*2
        assert len(store.history(store.event("100")).data) == 8
    with TestClient(create_app(str(store.root))) as app:
        assert app.get("/events/contract-event").json()["id"] == "100"
        assert app.get("/markets/200").json()["tokens"][0]["id"] == "300"
        first = app.get("/events/100/prices", params={"limit": 3}).json()
        second = app.get("/events/100/prices", params={"limit": 10, "offset": first["next_offset"]}).json()
        assert len(first["data"])+len(second["data"]) == 8
        assert app.get("/markets/200/prices?token_id=300").json()["data"][0]["price"] == 0.25
        assert app.get("/events/100/resolutions").json()["data"][0]["split_50_50"]
        assert app.get("/markets/200/resolutions").status_code == 200
        assert app.get("/events/100/prices?start=4&end=3").status_code == 422
        assert app.get("/events/100/prices?limit=0").status_code == 422
        assert app.get("/events/unknown").status_code == 404
        assert app.get("/markets/unknown/prices").status_code == 404
        assert app.post("/events/100").status_code == 405
        schema = app.get("/openapi.json").json()
        assert schema["paths"]["/events/{selector}/prices"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("History")
        assert all(set(methods) == {"get"} for methods in schema["paths"].values())
    assert main(["--data-dir", str(store.root), "query", "--event", "100", "--kind", "resolutions"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["status"] == "resolved"


def test_failed_manifest_commit_orphan_is_invisible_and_replay_safe(tmp_path):
    replay, store = Replay(), DataStore(tmp_path / "data")
    with client(tmp_path, replay) as api:
        event = api.event("100")
    store.put_event(event)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints
                      BEGIN SELECT RAISE(ABORT, 'injected crash'); END""")
    page = PricePage(data=[PricePoint(timestamp=1793000000, price=0.25, resolution_seconds=43200)])
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_page("200", "300", page, {"through": NOW, "pending": None})
    assert list(store.root.rglob("*.parquet"))
    assert store.history(event).data == []
    assert store.checkpoint("300")["through"] == 0
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_checkpoint")
    store.commit_page("200", "300", page, {"through": NOW, "pending": None})
    assert len(store.history(event).data) == 1
    # Latest overlapping correction wins without returning duplicate points.
    page.data[0].price = 0.3
    store.commit_page("200", "300", page, {"through": NOW, "pending": None})
    assert [p.price for p in store.history(event).data] == [0.3]


def test_rate_limit_shared_across_instances_and_retries(tmp_path):
    clock = [100.0]
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    path = tmp_path / "limits.sqlite3"
    a = SharedLimiter(path, clock=lambda: clock[0], sleep=sleep)
    b = SharedLimiter(path, clock=lambda: clock[0], sleep=sleep)
    a.acquire()
    b.acquire()
    assert sum(sleeps) >= 1/19
    attempts = []
    def transport(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(attempts) == 1 else httpx.Response(200, json={"ok": True})
    with PublicDataClient(transport=httpx.MockTransport(transport), limiter=a, sleep=sleep) as api:
        assert api.get("data-api.polymarket.com", "/v2/resolutions", {"event_id": "100"}) == {"ok": True}
        with pytest.raises(ValueError):
            api.get("clob.polymarket.com", "/order")
        with pytest.raises(ValueError):
            api.get("data-api.polymarket.com", "/positions")
    assert len(attempts) == 2 and max(sleeps) >= 2


def test_missing_data_and_resolution_refresh_never_infer_settled_from_closed(tmp_path):
    replay, store = Replay(), DataStore(tmp_path / "data")
    replay.fixture["prices"] = [{"data": [], "pagination": {"next_cursor": None, "has_more": False}}]*2
    replay.fixture["resolutions"] = {"data": []}
    with client(tmp_path, replay) as api:
        result = sync_event(api, store, "100", now=NOW)
        assert len(result.empty_token_ids) == 4
        assert result.missing_resolution_market_ids == ["200"]
        assert store.resolutions(store.event("100")).data == []
        replay.fixture["resolutions"] = copy.deepcopy(FIXTURE["resolutions"])
        sync_event(api, store, "100", now=NOW)
        assert store.resolutions(store.event("100")).data[0].status == "resolved"
        replay.fixture["resolutions"] = {"data": []}
        sync_event(api, store, "100", now=NOW)
        assert store.resolutions(store.event("100")).missing_market_ids == ["200"]


@pytest.mark.parametrize("status", ["proposed", "disputed", "challenged", "reproposed", "arbitration", "active"])
def test_resolution_lifecycle_preserved(tmp_path, status):
    replay = Replay()
    row = replay.fixture["resolutions"]["data"][0]
    row.update(status=status, payouts=None, resolved_at=None, proposed_price="69", reproposed_price="0.5")
    with client(tmp_path, replay) as api:
        result = api.resolutions(api.event("100"))[0]
    assert result.status == status and not result.split_50_50
    assert result.proposed_price is None and result.reproposed_price == "0.5"


def test_pagination_cycles_and_malformed_points_do_not_advance_checkpoint(tmp_path):
    replay, store = Replay(), DataStore(tmp_path / "data")
    replay.fixture["prices"][1]["pagination"] = {"next_cursor": "opaque-page-2", "has_more": True}
    with client(tmp_path, replay) as api:
        with pytest.raises(UpstreamError, match="Repeated"):
            sync_event(api, store, "100", now=NOW)
        assert store.checkpoint("300")["pending"]["cursor"] == "opaque-page-2"
        replay.fixture["prices"][1] = copy.deepcopy(FIXTURE["prices"][1])
        replay.fixture["prices"][1]["data"][0]["price"] = float("inf")
        with pytest.raises(ValueError):
            sync_event(api, store, "100", now=NOW)
        assert store.checkpoint("300")["through"] == 0


def test_writer_exclusion(tmp_path):
    store = DataStore(tmp_path / "data")
    with store.writer():
        with pytest.raises(RuntimeError, match="Another sync"):
            with DataStore(store.root).writer():
                pytest.fail("Second writer acquired lock")


def test_cli_passes_only_explicit_public_proxy_and_guard_still_denies(tmp_path, monkeypatch, capsys):
    import arb_research.polymarket.cli as cli
    proxy = "socks5h://fixture-user:fixture-password@127.0.0.1:1080"
    monkeypatch.setenv("ARB_POLYMARKET_PROXY", proxy)
    monkeypatch.setenv("HTTPS_PROXY", "http://unused-global.invalid:1234")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("ARB_COMPLIANCE_AUDIT_DB", str(tmp_path / "audit.sqlite3"))
    seen = []
    def factory(**kwargs):
        seen.append(kwargs["proxy"])
        return client(tmp_path, Replay())
    monkeypatch.setattr(cli, "PublicDataClient", factory)
    assert cli.main(["--data-dir", str(tmp_path / "data"), "sync", "--event", "100"]) == 0
    output = capsys.readouterr()
    assert seen == [proxy] and proxy not in output.out + output.err
    result = json.loads(output.out)[0]
    assert result["compliance"]["trading_enabled"] is True
    assert result["compliance"]["trading_allowed"] is False
    with client(tmp_path, Replay()) as api:
        with pytest.raises(ComplianceDenied) as denied:
            api.guard.require("order", caller="polymarket.fixture")
        assert denied.value.decision["reason"] == "geoblocked"
        assert "fixture-password" not in str(denied.value)


def test_proxy_constructor_errors_are_redacted():
    with pytest.raises(UpstreamError) as error:
        PublicDataClient(proxy="invalid-proxy://fixture-user:fixture-password@localhost")
    assert "fixture-password" not in str(error.value)
    assert "fixture-user" not in str(error.value)


def test_transport_error_traceback_cannot_expose_proxy_credentials(tmp_path):
    import traceback
    def failed(request):
        raise httpx.ProxyError("fixture-user:fixture-password", request=request)
    with PublicDataClient(transport=httpx.MockTransport(failed),
                          limiter=SharedLimiter(tmp_path / "limits.sqlite3"),
                          sleep=lambda _s: None) as api:
        with pytest.raises(UpstreamError) as error:
            api.get("data-api.polymarket.com", "/v2/resolutions", {"event_id": "100"})
    rendered = "".join(traceback.format_exception(error.value))
    assert "fixture-password" not in rendered and "fixture-user" not in rendered


def test_uma_split_settlement_without_condition_payouts(tmp_path):
    replay = Replay()
    replay.fixture["resolutions"]["data"][0].update(payouts=None, price="500000000000000000")
    with client(tmp_path, replay) as api:
        result = api.resolutions(api.event("100"))[0]
    assert result.payouts is None and result.price == "500000000000000000"
    assert result.split_50_50


def test_two_recorded_settled_events_replay_all_tokens_and_query_api(tmp_path):
    recorded = json.loads((Path(__file__).parent / "fixtures/polymarket/recorded-events.json").read_text())
    def key(url):
        p = urlsplit(str(url))
        return p.scheme, p.netloc, p.path, tuple(sorted(parse_qsl(p.query)))
    responses = {key(row["url"]): row["body"] for row in recorded["responses"]}
    seen = []
    def replay(request):
        assert request.method == "GET"
        if request.url.path == "/api/geoblock":
            geo = recorded["compliance"]["geoblock"]
            return httpx.Response(200, json={k: geo[k] for k in ["blocked", "country", "region"]})
        seen.append(key(request.url))
        assert key(request.url) in responses, f"Unrecorded request: {request.url}"
        return httpx.Response(200, json=responses[key(request.url)])
    store = DataStore(tmp_path / "data")
    with PublicDataClient(transport=httpx.MockTransport(replay),
                          limiter=SharedLimiter(tmp_path / "limits.sqlite3")) as api:
        for event_id, expected in [("27824", 2118), ("45883", 2127)]:
            result = sync_event(api, store, event_id, now=NOW)
            assert result.markets == 4 and result.tokens == 8
            assert result.points_received == expected
            assert not result.empty_token_ids and not result.missing_resolution_market_ids
            assert not result.compliance["trading_allowed"]
            event = store.event(event_id)
            assert all(m.tags and m.rules and m.neg_risk_group for m in event.markets)
            assert all(r.status == "resolved" for r in store.resolutions(event).data)
            first = store.history(event, limit=10000).model_dump()
            assert len(first["data"]) == expected
            assert all(store.history(event, token_id=t.id, limit=1).data for m in event.markets for t in m.tokens)
            # Identical-watermark sync refreshes metadata, but is a price no-op.
            before = len([k for k in seen if k[2] == "/v2/prices-history"])
            assert sync_event(api, store, event_id, now=NOW).points_received == 0
            assert len([k for k in seen if k[2] == "/v2/prices-history"]) == before
            assert store.history(event, limit=10000).model_dump() == first
    assert set(seen) == set(responses)
    with TestClient(create_app(str(store.root))) as app:
        for event_id, expected in [("27824", 2118), ("45883", 2127)]:
            response = app.get(f"/events/{event_id}/prices?limit=10000")
            assert response.status_code == 200 and len(response.json()["data"]) == expected
            resolved = app.get(f"/events/{event_id}/resolutions").json()
            assert len(resolved["data"]) == 4 and not resolved["missing_market_ids"]
