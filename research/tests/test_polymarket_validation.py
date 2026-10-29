"""Offline adapter compatibility, fee accounting and public-read transport contracts."""

import asyncio
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validation = script("validate_polymarket")
sampling = script("sample_polymarket")


@pytest.fixture(scope="module")
def result():
    pytest.importorskip("py_clob_client_v2", reason="uv sync --frozen --extra polymarket-validation")
    return asyncio.run(validation.run_validation())


def test_synthetic_replay_accounts_for_native_fees_and_moves_known_resolution_flags(result):
    assert result["version"] == "1.231.0"
    assert Decimal(result["fee_enabled_replay"]["total_commission"]) == Decimal("0.322")
    assert Decimal(result["fee_free_replay"]["total_commission"]) == 0
    assert result["fee_enabled_replay"]["final_cash"] == "1001.678000 pUSD"
    assert [Decimal(f["price"]) for f in result["fee_enabled_replay"]["fills"]] == [Decimal("0.3"), Decimal("0.5")]
    assert result["resolution_metadata"]["closed"] is True
    assert result["instrument_info_has_winner"] is False
    # The built-in sanitizer does NOT remove terminal token prices.
    assert result["instrument_info_terminal_prices"] == [0, 1]
    assert all(row["matches"] for row in result["fee_checks"]["grid"][:5])
    assert result["fee_checks"]["grid"][5]["matches"] is False
    assert result["real_settled_v1_history"]["status"] == "not_validated"


def test_missing_protocol_and_v2_are_admitted_but_do_not_prove_support(result):
    assert result["protocol_fixture_admission"] == {"None": "BinaryOption", "v1": "BinaryOption", "v2": "BinaryOption"}


def test_known_float_rounding_boundary():
    pytest.importorskip("py_clob_client_v2")
    from nautilus_trader.adapters.polymarket.common.parsing import calculate_commission
    from nautilus_trader.model.enums import LiquiditySide

    assert calculate_commission(Decimal("0.007"), Decimal("0.5"), Decimal("0.02"), LiquiditySide.TAKER) == 0.00003
    # Exact result is 0.000035; the implementation converts to float before round().
    assert Decimal("0.007") * Decimal("0.02") * Decimal("0.5") ** 2 == Decimal("0.000035")


def test_sample_counts_do_not_infer_versions_from_token_ids():
    markets = [{"id": "a", "version": "v1", "active": True, "closed": False},
               {"id": "b", "version": "v2", "active": True, "closed": False},
               {"id": "c", "clobTokenIds": "[1,2]", "active": True, "closed": False},
               {"id": "d", "version": "v1", "active": True, "closed": True}]
    assert validation.coverage(markets + markets)["counts"] == {"v1": 1, "v2": 1, "unknown": 1}
    assert validation.coverage([])["shares"]["v1"] is None


def test_sampler_public_routes_retry_and_geoblock_are_offline(monkeypatch):
    calls = []
    monkeypatch.setattr(sampling.BUCKET, "acquire", lambda: None)

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.host == "polymarket.com":
            return httpx.Response(200, json={"blocked": True, "country": "US", "region": "VA"})
        assert request.url.host == "gamma-api.polymarket.com"
        assert request.url.path == "/markets"
        assert request.url.params["active"] == "true"
        return httpx.Response(200, json=[{"id": "1", "active": True, "closed": False, "version": "v2"}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        value = sampling.collect(client, pages=1)
    assert value["geoblock"]["blocked"] is True
    assert value["coverage"]["counts"]["v2"] == 1
    assert len(calls) == 2  # Blocked location never enables any execution path.

    attempts, sleeps = [], []

    def retry_handler(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) < 3 else 200, json=[])

    with httpx.Client(transport=httpx.MockTransport(retry_handler)) as client:
        assert sampling.get_json(client, "https://gamma-api.polymarket.com/markets", sleep=sleeps.append) == []
    assert sleeps == [1, 2]
    assert len(attempts) == 3


def test_fixture_transport_rejects_unexpected_routes():
    with pytest.raises(AssertionError, match="Unrecorded request"):
        asyncio.run(validation.FixtureHttpClient().get("https://invalid.example/"))


def test_sampler_preserves_unknown_location_and_failed_sample(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectTimeout("fixture timeout")

    monkeypatch.setattr(sampling, "get_json", fail)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected HTTP"))) as client:
        value = sampling.collect(client, pages=1)
    assert value["geoblock"] is None
    assert value["geoblock_error"] == "fixture timeout"
    assert value["collection_status"] == "failed"
    assert "ConnectTimeout" in value["collection_error"]
    assert value["coverage"]["shares"] == {"v1": None, "v2": None, "unknown": None}
