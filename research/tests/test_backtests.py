import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from arb_research.api import create_app
from arb_research.contracts import CreateBacktest
from arb_research.datasets import snapshot
from arb_research.engines import run, targets
from arb_research.store import Store
from arb_research.worker import work_once


@pytest.fixture
def request_body():
    # Exact ARB-3 toRequest wire shape, including UTC end-of-day.
    return {
        "dataset_id": "demo-synthetic-1h-v2", "engine": "realistic",
        "strategy": {"id": "sma_cross", "parameters": {"fast_window": 10, "slow_window": 30}},
        "execution": {"fee_bps": 10, "slippage_bps": 5, "fill_policy": "next_bar_open"},
        "start": "2026-01-01T00:00:00Z", "end": "2026-01-14T23:59:59Z",
        "initial_capital": 10000, "currency": "USD",
    }


@pytest.fixture
def api(tmp_path):
    path = str(tmp_path / "jobs.sqlite3")
    app = create_app(path)
    with TestClient(app) as client:
        yield client, app.state.store, path


def test_real_engines_async_same_run_persistence_and_replay(api, request_body):
    client, store, path = api
    created = client.post("/backtests", json=request_body)
    assert created.status_code == 202
    queued = created.json()
    run_id = queued["run_id"]
    assert queued["status"] == "queued"
    assert queued["equity"] == [] and queued["metrics"] == {}
    assert all(s["status"] == "queued" for s in queued["stages"].values())
    assert created.headers["location"] == f"/backtests/{run_id}"
    assert len(client.get("/datasets").json()["datasets"]) == 2
    assert client.get(f"/backtests/{run_id}/bars").json()["bars"][0]["open"] == 100

    # Use production subprocess worker, not a stub, for both engine stages.
    assert work_once(store)
    draft = client.get(f"/backtests/{run_id}").json()
    assert draft["run_id"] == run_id and draft["status"] == "draft"
    assert draft["source"]["package"] == "vectorbt" and draft["phase"] == "draft"
    assert draft["metrics"]["fill_count"] > 0
    assert len(draft["equity"]) == 336
    assert draft["stages"]["realistic"]["status"] == "queued"
    assert work_once(Store(path))  # fresh worker connection survives process restart
    final = client.get(f"/backtests/{run_id}").json()
    assert final["status"] == "succeeded" and final["phase"] == "final"
    assert final["source"]["package"] == "nautilus_trader"
    assert final["stages"]["fast"]["result"] == draft["stages"]["fast"]["result"]
    assert final["as_of"] == "2026-02-01T00:00:00Z"
    assert final["metrics"]["fees_paid"] > 0 and len(final["fills"]) == 10
    assert final["metrics"]["final_equity"] == pytest.approx(12956.92, abs=0.02)
    # Both engines honor next-open, adverse bps and one-sided fee, independently.
    assert final["fills"][0]["timestamp"] == "2026-01-02T06:00:00Z"
    assert final["fills"][0]["price"] == pytest.approx(105.7924698)
    assert final["fills"][0]["fee"] == 9.49
    assert not work_once(store)

    # Stable cursor paging has no gaps/duplicates; fills/equity retain engine origin.
    events, cursor = [], 0
    while True:
        page = client.get(f"/backtests/{run_id}/events?after={cursor}&limit=71").json()
        if not page["events"]:
            assert page["next_cursor"] == cursor
            break
        events.extend(page["events"])
        cursor = page["next_cursor"]
    assert [e["seq"] for e in events] == sorted(set(e["seq"] for e in events))
    assert len([e for e in events if e["type"] == "fill"]) == 20
    assert [e["engine"] for e in events if e["type"] == "result_available"] == ["fast", "realistic"]
    assert all(e["run_id"] == run_id for e in events)
    with TestClient(create_app(path)) as restarted:
        assert restarted.get(f"/backtests/{run_id}").json() == final


def test_promote_is_idempotent_and_retains_original_request(api, request_body):
    client, store, _ = api
    request_body["engine"] = "fast"
    run_id = client.post("/backtests", json=request_body).json()["run_id"]
    # Real VectorBT path already covered above; exercise promotion around durable jobs.
    job = store.claim()
    result = run(job)
    store.finish(job, result)
    assert store.get(run_id)["status"] == "succeeded"
    for _ in range(2):
        promoted = client.post(f"/backtests/{run_id}/realistic")
        assert promoted.status_code == 202
        assert promoted.json()["run_id"] == run_id
        assert promoted.json()["status"] == "draft"
        assert promoted.json()["request"]["engine"] == "fast"
        assert promoted.json()["requested_engine"] == "realistic"
        assert promoted.json()["stages"]["fast"]["result"] == result
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


@pytest.mark.parametrize("change", [
    {"dataset_id": "../../outside"}, {"dataset_id": "live:BTC"},
    {"provider": "live"}, {"engine": "unknown"}, {"initial_capital": -1},
    {"initial_capital": True}, {"initial_capital": 0.001}, {"initial_capital": 123.456},
    {"currency": "USDT"}, {"start": "2026-01-01"},
    {"end": "2026-02-01T00:00:00Z"},
    {"start": "2026-01-14T00:00:00Z"},
    {"strategy": {"id": "python", "parameters": {"fast_window": 10, "slow_window": 30}}},
    {"strategy": {"id": "sma_cross", "parameters": {"fast_window": True, "slow_window": 30}}},
    {"execution": {"fee_bps": 10, "slippage_bps": 10000, "fill_policy": "next_bar_open"}},
    {"execution": {"fee_bps": 10, "slippage_bps": 5, "fill_policy": "same_close"}},
])
def test_invalid_requests_never_enqueue(api, request_body, change):
    client, store, _ = api
    assert client.post("/backtests", json={**request_body, **change}).status_code == 422
    assert store.claim() is None


def test_not_found_and_cursor_validation(api):
    client, _, _ = api
    for path in ["", "/events", "/bars"]:
        assert client.get("/backtests/absent" + path).status_code == 404
    assert client.post("/backtests/absent/realistic").status_code == 404
    assert client.get("/backtests/absent/events?after=-1").status_code == 422


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_return_422(api, request_body, number):
    client, store, _ = api
    request_body["initial_capital"] = number
    response = client.post("/backtests", content=json.dumps(request_body),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert store.claim() is None


def test_atomic_claim_and_expired_worker_cannot_overwrite(api, request_body):
    client, store, path = api
    run_id = client.post("/backtests", json=request_body).json()["run_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: Store(path).claim(), range(4)))
    assert sum(j is not None for j in claims) == 1
    old = next(j for j in claims if j)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE engine='fast'")
    new = store.claim()
    assert new["engine"] == "realistic"
    store.finish(old, None, error="late_result")
    assert store.get(run_id)["stages"]["fast"]["error"] == "worker_lease_expired"
    store.finish(new, None, error="test_failure")
    assert store.get(run_id)["status"] == "failed"


def test_stage_failure_preserves_draft(api, request_body):
    client, store, _ = api
    run_id = client.post("/backtests", json=request_body).json()["run_id"]
    job = store.claim()
    store.finish(job, run(job))
    before = store.get(run_id)

    def fail(_):
        raise RuntimeError("private environment detail")

    assert work_once(store, fail)
    after = client.get(f"/backtests/{run_id}").json()
    assert after["status"] == "partial"
    assert after["equity"] == before["equity"]
    assert after["source"]["package"] == "vectorbt"
    assert after["stages"]["realistic"]["error"] == "engine_execution_failed"
    assert "private environment" not in str(after)


def test_no_lookahead_and_costs_reduce_equity(request_body):
    request = CreateBacktest.model_validate(request_body)
    dataset = snapshot(request)
    changed = copy.deepcopy(dataset["bars"])
    changed[90]["close"] *= 10
    assert targets(request, changed)[:91] == targets(request, dataset["bars"])[:91]
    for engine in ("fast", "realistic"):
        job = dict(request=request_body, dataset=dataset, engine=engine)
        with_costs = run(job)
        no_costs = copy.deepcopy(job)
        no_costs["request"]["execution"].update(fee_bps=0, slippage_bps=0)
        without = run(no_costs)
        assert with_costs["metrics"]["final_equity"] < without["metrics"]["final_equity"]
        assert without["metrics"]["fees_paid"] == 0
