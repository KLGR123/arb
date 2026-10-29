"""Explicitly synthetic cohort; no claims about Polymarket or LLM skill."""

import random


def cohort(n=60, seed=11):
    rng = random.Random(seed)
    result = []
    for i in range(n):
        q = round(rng.uniform(0.05, 0.95), 6)
        result.append({
            "id": f"synthetic-{i:03d}", "source": "synthetic",
            "created_at": "2026-05-01T00:00:00Z",
            "forecast_at": "2026-05-02T12:00:00Z", "resolved_at": "2026-05-03T00:00:00Z",
            "y": int(rng.random() < q),
            "metadata_snapshots": [{"question": f"Will synthetic trial {i} succeed?",
                                    "rules": "YES iff the seeded Bernoulli trial succeeds.",
                                    "available_at": "2026-05-01T00:00:00Z",
                                    "captured_at": "2026-05-01T00:00:00Z"}],
            "prices": [
                {"timestamp": "2026-05-02T11:00:00Z", "available_at": "2026-05-02T11:00:00Z",
                 "p": q, "kind": "trade"},
                {"timestamp": "2026-05-03T00:00:00Z", "available_at": "2026-05-03T00:00:00Z",
                 "p": 1, "kind": "settlement"},
            ],
            "evidence": [
                {"id": "before", "text": "A synthetic trial is scheduled.", "source": "synthetic",
                 "published_at": "2026-05-01T00:00:00Z", "captured_at": "2026-05-01T00:00:00Z"},
                {"id": "after", "text": "DO NOT INCLUDE: the trial has settled.", "source": "synthetic",
                 "published_at": "2026-05-03T00:00:00Z", "captured_at": "2026-05-03T00:00:00Z"},
            ],
        })
    return result
