"""Record a complete, real two-engine ARB-4 run for the offline web replay.

Run from research: uv run --locked python scripts/record_replay.py
"""

import json
import tempfile
from pathlib import Path

from arb_research.contracts import CreateBacktest
from arb_research.datasets import snapshot
from arb_research.store import Store
from arb_research.worker import work_once


def main():
    request = CreateBacktest.model_validate({
        "dataset_id": "demo-synthetic-1h-v2", "engine": "realistic",
        "strategy": {"id": "sma_cross", "parameters": {"fast_window": 10, "slow_window": 30}},
        "execution": {"fee_bps": 10, "slippage_bps": 5, "fill_policy": "next_bar_open"},
        "start": "2026-01-01T00:00:00Z", "end": "2026-01-14T23:59:59Z",
        "initial_capital": 10000, "currency": "USD",
    })
    with tempfile.TemporaryDirectory() as directory:
        store = Store(str(Path(directory) / "recording.sqlite3"))
        run_id = store.create(request, snapshot(request))
        assert work_once(store) and work_once(store)
        run = store.get(run_id)
        assert run["status"] == "succeeded"
        assert all(s["result"] for s in run["stages"].values())
        bundle = {"version": 1, "run": run, "bars": store.bars(run_id)}
    target = Path(__file__).resolve().parents[2] / "apps/web/src/features/backtest/fixtures/recorded-run.json"
    target.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Recorded {run_id}: {len(bundle['bars']['bars'])} bars, both engines")


if __name__ == "__main__":
    main()
