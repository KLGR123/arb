"""Offline ablation checks: prompt boundary, paired replay and fee accounting."""

import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from arb_research.forecast.ablation import context_for, paired_price_effect, predict, prompt_for, protocol_paper, score, validate_market
from arb_research.forecast.codex import CodexPredictor
from arb_research.forecast.context import digest
from arb_research.forecast.metrics import evaluate

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "docs" / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cohort():
    return load("llm-ablation-cohort-v1.json")


@pytest.fixture
def baseline():
    return load("llm-forecast-real-v0.json")


@pytest.fixture
def replay(cohort, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    def execute(args, **kwargs):
        assert 'model_reasoning_effort="high"' in args
        assert "gpt-6.1-terra" in args
        assert not any(k in kwargs["env"] for k in ("ARB_POLYMARKET_PROXY", "OPENAI_API_KEY"))
        context = json.loads(kwargs["input"].split("\n", 1)[1])
        p = context.get("market_price", .5)
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"p": p})}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}]
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

    predictor = CodexPredictor("mock-codex", effort="high", execute=execute)
    return predict(cohort, predictor, tmp_path / "predictions.json", workers=4)


def test_blind_prompt_ignores_prices_truth_books_and_arbitrary_metadata(cohort):
    market = cohort["markets"][0]
    blind = prompt_for(market)
    changed = copy.deepcopy(market)
    changed.update(q=.123456, y=1-market["y"], mid=.777, bids=[.8], asks=[.9],
                   evidence={"leak": "the final winner"}, resolution_receipt={"leak": True})
    changed["source_receipt"].update(outcomePrices="[1,0]", volume=9999, winner="leak")
    assert prompt_for(changed) == blind
    context = context_for(market)
    with_price = context_for(market, "with_price")
    assert with_price.pop("market_price") == market["q"]
    assert with_price == context
    assert all(key not in context for key in ("market_id", "condition_id", "y", "q", "quote_at"))
    assert "http" not in blind


@pytest.mark.parametrize("text", ["Bitcoin price tomorrow", "Current market price is 0.8", "See https://example.com"])
def test_prompt_refuses_crypto_and_price_hints(cohort, text):
    market = copy.deepcopy(cohort["markets"][0]); market["rules"] = text
    with pytest.raises(ValueError):
        prompt_for(market)


def test_real_source_mappings_and_historical_quote_boundaries(cohort):
    assert len(cohort["markets"]) >= 50
    for market in cohort["markets"]:
        validate_market(market)
    m = copy.deepcopy(cohort["markets"][0])
    mutations = [dict(q=0), dict(target_outcome="wrong"), dict(y=1-m["y"]),
                 dict(quote_at="2099-01-01T00:00:00Z"), dict(fee_rate=".99"),
                 dict(rules="tampered rules"), dict(forecast_at="2026-01-01T00:00:00Z")]
    for mutation in mutations:
        with pytest.raises(ValueError):
            validate_market({**m, **mutation})


def test_mocked_local_codex_and_paired_scores(cohort, replay, baseline):
    result = score(cohort, replay, baseline)
    assert len(replay["predictions"]) == len(cohort["markets"]) * 2
    assert not replay["failures"]
    assert result["arms"]["blind"]["metrics"]["brier"] == .25
    assert result["arms"]["with_price"]["metrics"]["brier"] == result["arms"]["market"]["metrics"]["brier"]
    assert result["arms"]["market"]["metrics"]["paper_protocol"]["proper"]["net_pnl"] == 0
    assert result["paired_price_removal"]["brier_improvement"] == pytest.approx(
        result["arms"]["market"]["metrics"]["brier"] - .25)


@pytest.mark.parametrize("mutation", ["prompt", "audit", "missing", "duplicate", "cohort", "failures", "effort"])
def test_replay_rejects_changed_evidence(cohort, replay, baseline, mutation):
    if mutation == "prompt":
        replay["predictions"][0]["prompt"] += " changed"
    elif mutation == "audit":
        replay["predictions"][0]["model_audit"]["tool_calls"] = 1
    elif mutation == "missing":
        replay["predictions"].pop()
    elif mutation == "duplicate":
        replay["predictions"].append(replay["predictions"][0])
    elif mutation == "cohort":
        cohort["markets"][0]["question"] += " changed"
    elif mutation == "failures":
        replay["failures"].append({"error_type": "RuntimeError"})
    elif mutation == "effort":
        replay["config"]["effort"] = "low"
    with pytest.raises(ValueError):
        score(cohort, replay, baseline)


def test_fee_kernel_and_yes_no_payoffs():
    # YES: 20 shares at .4 => 8 collateral, .24 fee, gross payoff 12.
    # NO: 20 shares at .4 => same economics with opposite signed YES shares.
    for p, q, y in ((.5, .4, 1), (.5, .6, 0)):
        result = protocol_paper([{"p": p, "q": q, "y": y}], [{"fee_rate": ".05"}])
        assert result["proper"]["collateral"] == pytest.approx(8)
        assert result["proper"]["fees"] == pytest.approx(.24)
        assert result["proper"]["net_pnl"] == pytest.approx(11.76)
    for p, q, y in ((1, .9, 0), (0, .1, 1), (1, .01, 0), (0, .99, 1)):
        result = protocol_paper([{"p": p, "q": q, "y": y}], [{"fee_rate": "1"}])
        for key in ("proper", "fractional_kelly"):
            assert result[key]["collateral"] + result[key]["fees"] <= 100
            assert result[key]["net_pnl"] >= -100


def test_scoring_known_probabilities():
    rows = [{"market_id": "a", "p": .8, "q": .5, "y": 1},
            {"market_id": "b", "p": .2, "q": .5, "y": 0}]
    metrics = evaluate(rows)
    assert metrics["brier"] == pytest.approx(.04)
    assert metrics["market_brier"] == .25
    assert metrics["log_loss"] == pytest.approx(.2231435513142097)


def test_paired_effect_accepts_extreme_model_probabilities():
    blind = [{"market_id": "a", "cluster_id": "event", "p": .5, "y": 1},
             {"market_id": "b", "cluster_id": "event", "p": .5, "y": 0}]
    control = [{**blind[0], "p": 1}, {**blind[1], "p": 0}]
    result = paired_price_effect(blind, control)
    assert result["brier_improvement"] == -.25
    assert result["ci95"] == [-.25, -.25]
    with pytest.raises(ValueError):
        paired_price_effect(blind, list(reversed(control)))


def test_real_recorded_fixture_replays_exactly(cohort, baseline):
    predictions = load("llm-ablation-predictions-v1.json")
    expected = load("llm-ablation-results-v1.json")
    assert score(cohort, predictions, baseline) == expected
    assert predictions["config"]["markets_sha256"] == digest(cohort["markets"])
