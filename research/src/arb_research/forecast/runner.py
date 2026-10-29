"""Prediction boundary and append-only SQLite audit trail."""

import json
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Protocol

from .context import build_context, canonical, digest, probability, prompt_for, timestamp
from .metrics import evaluate


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Predictor(Protocol):
    model: str
    training_cutoff: str
    is_mock: bool

    def predict(self, prompt: str, *, sample: int) -> dict: ...


class MockPredictor:
    """Market shrinkage + deterministic noise; never reads truth or market ID."""

    is_mock = True
    training_cutoff = "2000-01-01T00:00:00Z"

    def __init__(self, model="mock-shrinkage-v1", seed=11):
        self.model, self.seed = model, seed

    def predict(self, prompt, *, sample):
        context = json.loads(prompt.split("\n", 1)[1])
        rng = random.Random(digest([prompt, self.model, sample, self.seed]))
        return {"p": max(0, min(1, 0.85 * context["market_price"] + 0.075
                              + rng.uniform(-0.06, 0.06)))}


class RecordedPredictor:
    """Replay externally generated model responses bound to the exact prompt.

    No live model credential is read by this module. The caller is responsible
    for using an authorized model with a documented training cutoff.
    """

    is_mock = False

    def __init__(self, model, training_cutoff, records):
        timestamp(training_cutoff)
        self.model, self.training_cutoff = model, training_cutoff
        self.records = {}
        for record in records:
            key = (record["prompt_sha256"], record["sample"])
            if key in self.records:
                raise ValueError("Duplicate recorded prediction")
            self.records[key] = probability(record["p"])

    def predict(self, prompt, *, sample):
        return {"p": self.records[(digest(prompt), sample)]}


class AuditStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS forecast_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    config_json TEXT NOT NULL, result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS forecast_predictions (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    market_id TEXT NOT NULL, model TEXT NOT NULL, sample INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(run_id, market_id, model, sample)
                );
            """)

    def start(self, config):
        run_id = str(uuid.uuid4())
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO forecast_runs VALUES (?, ?, ?, NULL)",
                       (run_id, now(), canonical(config)))
        return run_id

    def prediction(self, run_id, market_id, model, sample, payload):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO forecast_predictions VALUES (NULL, ?, ?, ?, ?, ?)",
                       (run_id, market_id, model, sample, canonical(payload)))

    def finish(self, run_id, result):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE forecast_runs SET result_json=? WHERE id=? AND result_json IS NULL",
                       (canonical(result), run_id))


def run(markets, predictors, store: AuditStore, *, samples=2, seed=11, bootstrap=2000,
        min_markets=50, fee_bps=20, bankroll=100, kelly_fraction=0.25, cap=0.1):
    predictors = list(predictors)
    if samples < 1 or not predictors or min_markets < 1:
        raise ValueError("Need predictors, samples and positive minimum cohort")
    if len({p.model for p in predictors}) != len(predictors):
        raise ValueError("Model identifiers must be unique")
    if len({m["id"] for m in markets}) != len(markets):
        raise ValueError("Duplicate market in cohort")
    config = {"samples_per_model": samples, "models": [p.model for p in predictors],
              "seed": seed, "bootstrap": bootstrap, "min_markets": min_markets,
              "fee_bps": fee_bps, "bankroll": bankroll,
              "kelly_fraction": kelly_fraction, "cap": cap,
              "dataset_sha256": digest(markets)}
    run_id = store.start(config)
    rows, excluded = [], []
    for market in markets:
        try:
            if type(market["y"]) is not int or market["y"] not in (0, 1):
                raise ValueError("Unresolved or fractional payout")
            if market["source"] == "synthetic" and not all(p.is_mock for p in predictors):
                raise ValueError("Synthetic dataset is only for mocks")
            context = build_context(market)
            for predictor in predictors:
                if timestamp(predictor.training_cutoff) > timestamp(market["forecast_at"]):
                    raise ValueError("Model training cutoff is later than forecast time")
        except (KeyError, ValueError) as exc:
            excluded.append({"market_id": market["id"], "reason": str(exc)})
            continue
        prompt = prompt_for(context)
        ps = []
        for predictor in predictors:
            for sample in range(samples):
                retrieved_at = now()
                # Predictor gets only the allowlisted prompt, not the evaluation row.
                payload = {"forecast_at": market["forecast_at"], "retrieved_at": retrieved_at,
                           "prompt": prompt, "context": context,
                           "context_sha256": digest(context),
                           "model_training_cutoff": predictor.training_cutoff,
                           "is_mock": predictor.is_mock}
                try:
                    response = predictor.predict(prompt, sample=sample)
                    p = probability(response["p"])
                except Exception as exc:
                    # Persist the failed attempt without storing provider messages/credentials.
                    payload.update(status="failed", predicted_at=now(), error_type=type(exc).__name__)
                    payload.pop("response", None)
                    store.prediction(run_id, market["id"], predictor.model, sample, payload)
                    store.finish(run_id, {"status": "prediction_failed", "market_id": market["id"],
                                          "model": predictor.model, "error_type": type(exc).__name__})
                    raise ValueError("Prediction failed; see sanitized SQLite audit status") from None
                payload.update(status="succeeded", predicted_at=now(), response={"p": p})
                store.prediction(run_id, market["id"], predictor.model, sample, payload)
                ps.append(p)
        rows.append({"market_id": market["id"], "forecast_at": market["forecast_at"],
                     "quote_at": context["quote_at"], "q": context["market_price"],
                     "p": mean(ps), "y": market["y"], "samples": len(ps)})
    if len(rows) < min_markets:
        result = {"status": "insufficient_cohort", "n": len(rows), "excluded": excluded,
                  "config": config}
        store.finish(run_id, result)
        raise ValueError(f"Only {len(rows)} eligible markets; require {min_markets}")
    result = {"schema_version": 1, "mode": "mock" if all(p.is_mock for p in predictors) else "model",
              "config": config, "rows": rows, "excluded": excluded,
              "metrics": evaluate(rows, bootstrap=bootstrap, seed=seed, fee_bps=fee_bps,
                                  bankroll=bankroll, kelly_fraction=kelly_fraction, cap=cap)}
    store.finish(run_id, result)
    # Operational timestamps / run ID live in SQLite; JSON metrics remain reproducible.
    return result
