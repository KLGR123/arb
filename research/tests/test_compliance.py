import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from arb_research import compliance
from arb_research.api import create_app
from arb_research.compliance import (
    AuditLog, ComplianceDenied, ComplianceGuard, ComplianceSettings, GeoblockClient,
    TokenBucket, compliance_dependency, guarded,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not access the network")
    monkeypatch.setattr(compliance, "build_opener", forbidden)


@pytest.fixture
def payloads():
    return json.loads((Path(__file__).parent / "fixtures/geoblock.json").read_text())


def make_guard(tmp_path, payload, enabled=False):
    return ComplianceGuard(ComplianceSettings(enabled, str(tmp_path / "audit.sqlite3")),
                           geoblock=GeoblockClient(fetch=lambda _: payload))


def audit_rows(guard):
    with sqlite3.connect(guard.settings.audit_path) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT decision FROM compliance_audit")]


def test_default_config_and_env_do_not_allow_trading(monkeypatch, tmp_path, payloads):
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    assert ComplianceSettings.from_env().trading_enabled is False
    for value, enabled in [("false", False), ("true", True), ("TRUE", True), ("garbage", False)]:
        monkeypatch.setenv("TRADING_ENABLED", value)
        assert ComplianceSettings.from_env().trading_enabled is enabled
        guard = make_guard(tmp_path, payloads["clear"], enabled)
        with pytest.raises(ComplianceDenied) as exc:
            guard.require("order", caller="test")
        assert exc.value.decision["allowed"] is False
        assert exc.value.decision["reason"] == (
            "allowlist_not_implemented" if enabled else "trading_disabled")


@pytest.mark.parametrize("action", ["order", "sign", "deposit", "cancel", "withdraw", "unknown"])
def test_all_actions_denied_and_audited(tmp_path, payloads, action):
    guard = make_guard(tmp_path, payloads["blocked"], True)
    with pytest.raises(ComplianceDenied) as exc:
        guard.require(action, caller="strategy.fixture")
    assert exc.value.decision["reason"] == "geoblocked"
    row, = audit_rows(guard)
    assert row["caller"] == "strategy.fixture" and row["action"] == action
    assert row["trading_enabled"] is True and row["recorded_at"]
    assert row["geoblock"]["country"] == "US" and row["geoblock"]["region"] == "VA"
    assert "203.0.113" not in json.dumps(row)


@pytest.mark.parametrize("payload", [None, [], {}, {"blocked": "false"},
    {"blocked": False, "country": "US", "region": None},
    {"blocked": False, "country": "", "region": ""},
    {"blocked": 0, "country": "ES", "region": "MD"}])
def test_invalid_schema_fails_closed(tmp_path, payload):
    guard = make_guard(tmp_path, payload, True)
    with pytest.raises(ComplianceDenied) as exc:
        guard.require("order", caller="test")
    assert exc.value.decision["reason"] == "geoblock_unavailable"
    assert audit_rows(guard)[0]["geoblock"]["blocked"] is True


def test_timeout_retries_caches_failure_and_never_reuses_expired_clear(tmp_path, payloads):
    now, calls, sleeps = [0], [], []
    def fetch(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            return payloads["clear"]
        raise TimeoutError("sensitive transport information")
    geo = GeoblockClient(fetch=fetch, clock=lambda: now[0], sleep=sleeps.append)
    assert geo.check().blocked is False
    assert geo.check().cached is True
    now[0] = 61
    guard = make_guard(tmp_path, payloads["clear"], True)
    guard.geoblock = geo
    with pytest.raises(ComplianceDenied):
        guard.require("sign", caller="test")
    assert geo.check().blocked and geo.check().cached
    assert len(calls) == 4 and sleeps == [0.25, 0.5]
    assert "sensitive" not in json.dumps(audit_rows(guard))


@pytest.mark.parametrize("code, attempts", [(429, 3), (503, 3), (403, 1), (302, 1)])
def test_http_retry_is_bounded(code, attempts):
    calls = []
    def fetch(_):
        calls.append(1)
        raise HTTPError(compliance.GEOBLOCK_URL, code, "fixture", {}, None)
    result = GeoblockClient(fetch=fetch, sleep=lambda _: None).check()
    assert result.blocked and result.error == "http_error"
    assert len(calls) == attempts


def test_single_refresh_and_concurrent_durable_audit(tmp_path, payloads):
    guard = make_guard(tmp_path, payloads["clear"])
    calls = []
    guard.geoblock.fetch = lambda _: calls.append(1) or payloads["clear"]
    def invoke(i):
        with pytest.raises(ComplianceDenied):
            guard.require("cancel", caller=f"test.{i}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(invoke, range(16)))
    assert len(calls) == 1
    assert len(audit_rows(guard)) == 16
    # A new audit object writes to the same persisted log after restart.
    AuditLog(guard.settings.audit_path).record(audit_rows(guard)[0])
    assert len(audit_rows(guard)) == 17


def test_sync_async_decorators_never_enter_protected_body(tmp_path, payloads):
    guard = make_guard(tmp_path, payloads["clear"], True)
    entered = []
    @guarded("sign", caller="strategy.sync", guard=guard)
    def sync_strategy():
        entered.append(1)
    @guarded("deposit", caller="strategy.async", guard=guard)
    async def async_strategy():
        entered.append(2)
    with pytest.raises(ComplianceDenied):
        sync_strategy()
    with pytest.raises(ComplianceDenied):
        asyncio.run(async_strategy())
    assert entered == [] and len(audit_rows(guard)) == 2


def test_fastapi_status_and_dependency(tmp_path, payloads):
    guard = make_guard(tmp_path, payloads["blocked"], True)
    app = create_app(str(tmp_path / "jobs.sqlite3"), compliance=guard)
    @app.post("/fixture-protected", dependencies=[Depends(
        compliance_dependency("order", caller="api.fixture"))])
    def protected():
        pytest.fail("Protected body must never run")
    with TestClient(app) as client:
        response = client.get("/compliance/status")
        assert response.status_code == 200
        status = response.json()
        assert status["trading_enabled"] and not status["trading_allowed"]
        assert not status["allowlist_configured"] and status["mode"] == "read_only"
        assert status["geoblock"]["country"] == "US"
        assert client.post("/compliance/status").status_code == 405
        denied = client.post("/fixture-protected")
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "compliance_denied"
    assert len(audit_rows(guard)) == 1


def test_audit_failure_still_denies(tmp_path, payloads):
    guard = make_guard(tmp_path, payloads["clear"], True)
    guard.audit = AuditLog(str(tmp_path))  # A directory cannot be opened as SQLite.
    @guarded("order", caller="test", guard=guard)
    def protected():
        pytest.fail("Audit failure must not bypass guard")
    with pytest.raises(ComplianceDenied) as exc:
        protected()
    assert exc.value.decision["reason"] == "audit_unavailable"


def test_shared_token_bucket_throttles():
    now, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    bucket = TokenBucket(2, 2, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        bucket.acquire()
    assert sleeps == [0.5, 0.5]


def test_transport_fixed_endpoint_no_proxy_or_redirect(monkeypatch, payloads):
    observed = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 16385
            return json.dumps(payloads["blocked"]).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == compliance.GEOBLOCK_URL
            assert request.get_method() == "GET" and timeout == 2
            return Response()
    def opener(*handlers):
        observed.extend(handlers)
        return Opener()
    monkeypatch.setattr(compliance, "build_opener", opener)
    assert compliance._fetch_geoblock(2) == payloads["blocked"]
    assert observed[0].proxies == {}
    assert observed[1].redirect_request(None, None, 302, None, None, "https://other") is None
