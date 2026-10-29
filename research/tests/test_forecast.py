"""Network-free acceptance tests; unittest also runs without engine dependencies."""

import copy
import io
import json
import math
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path

from arb_research.forecast.context import build_context, digest, probability, prompt_for
from arb_research.forecast.demo import cohort
from arb_research.forecast.metrics import evaluate, loss, paper_bet
from arb_research.forecast.public import PublicClient, TokenBucket, collect, normalize
from arb_research.forecast.runner import AuditStore, MockPredictor, RecordedPredictor, run


FIXTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("forecast-public-v2.json").read_text())


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "audit.sqlite3"
        self.market = cohort(1)[0]

    def run_cohort(self, **kwargs):
        return run(cohort(), [MockPredictor("a"), MockPredictor("b")],
                   AuditStore(self.db), bootstrap=100, **kwargs)

    def test_cutoff_is_an_allowlist_and_future_mutations_do_not_change_prompt(self):
        before = prompt_for(build_context(self.market))
        self.market.update(y=1 - self.market["y"], outcomePrices=[0, 1],
                           current_description="THE WINNER WAS YES")
        self.market["evidence"][1]["text"] = "THE WINNER WAS YES"
        self.market["prices"][1]["p"] = 0
        self.market["metadata_snapshots"].append({
            "question": "Settled YES", "rules": "Winner YES",
            "available_at": "2026-05-03T00:00:00Z", "captured_at": "2026-05-03T00:00:00Z"})
        self.assertEqual(before, prompt_for(build_context(self.market)))
        self.assertNotIn("DO NOT INCLUDE", before)

    def test_backdated_evidence_retrieved_after_cutoff_is_excluded(self):
        self.market["evidence"][1]["published_at"] = "2026-05-01T00:00:00Z"
        self.assertEqual([e["id"] for e in build_context(self.market)["evidence"]], ["before"])

    def test_metadata_captured_after_cutoff_cannot_be_backdated(self):
        self.market["metadata_snapshots"][0]["captured_at"] = "2026-05-04T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "metadata snapshot"):
            build_context(self.market)

    def test_settlement_point_before_cutoff_is_never_a_quote(self):
        self.market["prices"][1].update(timestamp="2026-05-02T11:59:00Z",
                                         available_at="2026-05-02T11:59:00Z")
        self.assertEqual(build_context(self.market)["market_price"], self.market["prices"][0]["p"])

    def test_quote_availability_staleness_and_forecast_lifecycle(self):
        for mutate in (
            lambda m: m["prices"][0].update(available_at="2026-05-04T00:00:00Z"),
            lambda m: m.update(forecast_at=m["resolved_at"]),
            lambda m: m.update(forecast_at="2026-04-30T00:00:00Z"),
            lambda m: m["prices"][0].update(timestamp="2026-05-01T00:00:00Z"),
            lambda m: m.update(forecast_at="2026-05-02T12:00:00"),
        ):
            with self.subTest(mutate=mutate):
                m = copy.deepcopy(self.market)
                mutate(m)
                with self.assertRaises(ValueError):
                    build_context(m)

    def test_probabilities_reject_bad_outputs(self):
        for value in (True, "0.5", -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probability(value)
        self.assertEqual(probability(1), 1)

    def test_scores_match_hand_computation_and_pairing(self):
        rows = [{"market_id": "a", "p": 0.75, "q": 0.5, "y": 1},
                {"market_id": "b", "p": 0.25, "q": 0.5, "y": 0}]
        scores = evaluate(rows, bootstrap=100)
        self.assertAlmostEqual(scores["brier"], 0.0625)
        self.assertAlmostEqual(scores["log_loss"], -math.log(0.75))
        self.assertEqual(scores["brier_advantage_ci95"], [0.1875, 0.1875])
        self.assertEqual(sum(b["count"] for b in scores["calibration"]), 2)
        self.assertTrue(math.isfinite(loss(0, 1)))
        self.assertTrue(math.isfinite(loss(1, 0)))
        same = [{**r, "p": r["q"]} for r in rows]
        self.assertEqual(evaluate(same, bootstrap=100)["brier_advantage_ci95"], [0, 0])

    def test_calibration_includes_endpoints_and_empty_bins(self):
        rows = [{"market_id": str(i), "p": i, "q": 0.5, "y": i} for i in (0, 1)]
        bins = evaluate(rows, bootstrap=100)["calibration"]
        self.assertEqual(bins[0]["count"], 1)
        self.assertEqual(bins[-1]["count"], 1)
        self.assertIsNone(bins[1]["mean_p"])

    def test_proper_and_kelly_buy_yes_and_no_hand_computation(self):
        for p, q, y in ((0.7, 0.4, 1), (0.3, 0.6, 0)):
            bets = paper_bet(p, q, y, fee_bps=100, bankroll=100, cap=0.1)
            self.assertAlmostEqual(abs(bets["proper"]["shares"]), 60)
            self.assertAlmostEqual(bets["proper"]["fees"], 0.24)
            self.assertAlmostEqual(bets["proper"]["net_pnl"], 35.76)
            self.assertAlmostEqual(bets["fractional_kelly"]["collateral"] * 1.01, 10)
        zero = paper_bet(0.4, 0.4, 1)
        self.assertEqual(zero["proper"]["net_pnl"], 0)
        self.assertEqual(zero["fractional_kelly"]["fees"], 0)

    def test_betting_is_collateralized_and_settings_validated(self):
        for p in (0, 0.2, 0.8, 1):
            for q in (0.001, 0.4, 0.999):
                for y in (0, 1):
                    bets = paper_bet(p, q, y)
                    self.assertAlmostEqual(bets["proper"]["shares"] / 100, 2 * (p - q))
                    for bet in bets.values():
                        self.assertLessEqual(bet["collateral"] + bet["fees"], 100)
                        self.assertGreaterEqual(bet["net_pnl"], -100)
        for options in ({"fee_bps": -1}, {"cap": 2}, {"bankroll": 0}, {"kelly_fraction": 2}):
            with self.assertRaises(ValueError):
                paper_bet(0.6, 0.5, 1, **options)

    def test_sixty_market_ensemble_is_reproducible_and_durable(self):
        first = self.run_cohort()
        self.assertEqual(first, self.run_cohort())
        self.assertEqual(first["metrics"]["n"], 60)
        self.assertEqual(first["mode"], "mock")
        self.assertEqual(first["rows"][0]["samples"], 4)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0], 2)
            payloads = db.execute("SELECT payload_json FROM forecast_predictions").fetchall()
            self.assertEqual(len(payloads), 480)
            for (raw,) in payloads:
                record = json.loads(raw)
                self.assertIn("retrieved_at", record)
                self.assertIn("predicted_at", record)
                self.assertEqual(record["context_sha256"], digest(record["context"]))
                self.assertNotIn("DO NOT INCLUDE", record["prompt"])

    def test_invalid_model_output_is_recorded_without_provider_secrets(self):
        class Invalid(MockPredictor):
            def predict(self, *args, **kwargs):
                raise RuntimeError("Authorization: secret-value")
        with self.assertRaisesRegex(ValueError, "Prediction failed"):
            run(cohort(1), [Invalid()], AuditStore(self.db), min_markets=1, bootstrap=100)
        with sqlite3.connect(self.db) as db:
            raw = db.execute("SELECT payload_json FROM forecast_predictions").fetchone()[0]
            self.assertNotIn("secret-value", raw)
            self.assertEqual(json.loads(raw)["status"], "failed")

    def test_insufficient_cohort_and_duplicate_markets_fail_closed(self):
        store = AuditStore(self.db)
        with self.assertRaisesRegex(ValueError, "Only 1"):
            run([self.market], [MockPredictor()], store)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            run([self.market, self.market], [MockPredictor()], store)

    def test_model_knowledge_cutoff_and_synthetic_inputs_are_guarded(self):
        class Late(MockPredictor):
            training_cutoff = "2027-01-01T00:00:00Z"
        class Real(MockPredictor):
            is_mock = False
        for model in (Late(), Real()):
            with self.assertRaisesRegex(ValueError, "Only 0"):
                run([self.market], [model], AuditStore(self.db), min_markets=1)

    def test_recorded_model_response_is_bound_to_exact_prompt(self):
        prompt = prompt_for(build_context(self.market))
        model = RecordedPredictor("authorized-model", "2026-01-01T00:00:00Z",
                                  [{"prompt_sha256": digest(prompt), "sample": 0, "p": 0.6}])
        self.assertEqual(model.predict(prompt, sample=0), {"p": 0.6})
        with self.assertRaises(KeyError):
            model.predict(prompt + "tampered", sample=0)


class PublicTests(unittest.TestCase):
    def test_v2_normalization_rejects_synthetic_settlement_and_modern_metadata(self):
        f = copy.deepcopy(FIXTURE)
        market = normalize(f["gamma"], f["resolutions"]["data"][0], f["prices"]["data"],
                           "2026-10-30T00:00:00Z")
        self.assertEqual(len(market["prices"]), 1)
        self.assertEqual(market["y"], 1)
        market["forecast_at"] = "2026-05-02T12:00:00Z"
        with self.assertRaisesRegex(ValueError, "metadata snapshot"):
            build_context(market)
        for payouts in ([0.5, 0.5], [1, 1], []):
            f["resolutions"]["data"][0]["payouts"] = payouts
            with self.assertRaises(ValueError):
                normalize(f["gamma"], f["resolutions"]["data"][0], f["prices"]["data"],
                          "2026-10-30T00:00:00Z")

    def test_collector_paginates_and_matches_outcome_token(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get(self, route, **params):
                self.calls.append((route, params))
                if route == "markets":
                    return [FIXTURE["gamma"]]
                if route == "resolutions":
                    return FIXTURE["resolutions"]
                if "cursor" not in params:
                    return {"data": FIXTURE["prices"]["data"][:1],
                            "pagination": {"has_more": True, "next_cursor": "second"}}
                return {"data": FIXTURE["prices"]["data"][1:],
                        "pagination": {"has_more": False}}
        client = Client()
        result = collect(client, count=1)
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(client.calls[-1][1]["cursor"], "second")
        self.assertEqual(client.calls[-1][1]["token_id"], "101")

    def test_rate_limit_retry_after_and_nonretryable_errors(self):
        class Bucket:
            n = 0

            def acquire(self):
                self.n += 1
        class Opener:
            def __init__(self, code):
                self.code, self.n = code, 0

            def open(self, request, timeout):
                self.n += 1
                if self.n == 1:
                    raise urllib.error.HTTPError(request.full_url, self.code, "test",
                                                 {"Retry-After": "3"}, None)
                return io.StringIO('{"data": []}')
        sleeps, bucket = [], Bucket()
        client = PublicClient(bucket=bucket, opener=Opener(429), sleep=sleeps.append)
        self.assertEqual(client.get("resolutions", condition="x"), {"data": []})
        self.assertEqual(sleeps, [3])
        self.assertEqual(bucket.n, 2)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            PublicClient(bucket=bucket, opener=Opener(403)).get("markets")

    def test_two_clients_share_persistent_token_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock, sleeps = [100.0], []

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds
            a = TokenBucket(Path(tmp) / "bucket.sqlite3", clock=lambda: clock[0], sleep=sleep)
            b = TokenBucket(a.path, clock=lambda: clock[0], sleep=sleep)
            a.acquire()
            b.acquire()
            b.acquire()
            self.assertAlmostEqual(sum(sleeps), 0.2)


if __name__ == "__main__":
    unittest.main()
