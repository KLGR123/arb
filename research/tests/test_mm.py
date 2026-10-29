import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from arb_research.mm import (
    Config, DAY_MS, Fixture, Market, SAMPLE_DIR, fee_equivalent,
    minimum_score, order_score, quote_prices, simulate,
)


def market(**overrides):
    return Market(id="test", category="crypto", tick_size=.01, min_order_size=1.0,
                  fee_rate=.07, rebate_fraction=.2, reward_min_size=10.0,
                  reward_max_spread=.05, reward_daily_pool=86400.0).model_copy(update=overrides)


def book(time, bid=.49, ask=.51, size=10.0, competitor=10.0):
    return {"timestamp_ms": time, "bids": [{"price": bid, "size": size}],
            "asks": [{"price": ask, "size": size}], "competitor_score": competitor}


def fixture(books=None, takers=None, end=10000, outcome=None, **market_overrides):
    return Fixture.model_validate({
        "schema_version": "mm-fixture-v1", "provenance": "synthetic test",
        "market": market(**market_overrides).model_dump(), "end_ms": end,
        "books": books or [book(0), book(end)], "takers": takers or [],
        "settlement_price": outcome,
    })


def config(**overrides):
    return Config(risk_aversion=0.0, inventory_skew=0.0, **overrides)


def taker(time=1000, side="sell", size=25.0, limit=.49):
    return {"timestamp_ms": time, "side": side, "size": size, "limit_price": limit}


def test_inventory_reservation_price_passivity_and_tick_rounding():
    cfg = Config(risk_aversion=.1, variance_per_second=.01, horizon_seconds=1.0,
                 inventory_skew=.001, spread=.02)
    center, bid, ask = quote_prices(.5, 60, market(), cfg)
    assert center == pytest.approx(.48)
    assert bid == .46 and ask == .5
    assert quote_prices(.5, 40, market(), cfg)[0] == pytest.approx(.52)
    shifted = quote_prices(.5, 50, market(), cfg.model_copy(update={"center_offset": .02}))
    assert shifted[0] == pytest.approx(.52)
    result = simulate(fixture(), cfg.model_copy(update={"center_offset": .4}))
    assert result["quotes"][0]["bid"]["price"] < .51


@pytest.mark.parametrize("mid,bid,ask", [
    (.1, .09, .11), (.17, .16, .18), (.4, .39, .41), (.52, .51, .53),
    (.58, .57, .59), (.68, .67, .69), (.83, .82, .84),
])
def test_exact_tick_boundary_never_widens_one_tick(mid, bid, ask):
    assert quote_prices(mid, 50, market(), config()) == (mid, bid, ask)


def test_midpoint_arithmetic_does_not_move_boundary_quotes_or_fills():
    # (.1 + .2) / 2 in binary is .15000000000000002; bid/ask must be .14/.16.
    data = fixture(books=[book(0, bid=.1, ask=.2), book(10000, bid=.1, ask=.2)])
    result = simulate(data, config())
    assert result["quotes"][0]["midpoint"] == .15
    assert result["quotes"][0]["bid"]["price"] == .14
    assert result["quotes"][0]["ask"]["price"] == .16
    # Reviewer repro: exactly .41 should fill, not become a .42 ask.
    data = fixture(books=[book(0, bid=.39, ask=.41), book(10000, bid=.39, ask=.41)],
                   takers=[taker(side="buy", size=30.0, limit=.41)])
    assert simulate(data, config())["fills"][0]["size"] == 20


def test_price_time_priority_partial_fills_and_no_depth_reuse():
    data = fixture(takers=[taker(size=7.0), taker(time=2000, size=8.0), taker(time=3000, size=30.0)])
    result = simulate(data, config())
    # First 10 shares exhaust the external bid; then 5 + 15 fill our 20.
    assert [(f["timestamp_ms"], f["size"]) for f in result["fills"]] == [(2050, 5.0), (3050, 15.0)]
    assert result["quotes"][0]["bid"]["queue_ahead"] == 10
    assert result["ending_inventory"] == 70
    assert result["ending_cash"] == pytest.approx(990.2)
    assert result["pnl"]["trading_pnl"] == pytest.approx(.2)
    assert result["pnl"]["fees"] == 0
    assert sum(f["size"] for f in result["fills"]) <= 45


def test_better_prices_precede_our_order_and_worse_levels_follow():
    first = book(0)
    first["bids"] = [{"price": .50, "size": 5.0}, {"price": .49, "size": 10.0}, {"price": .48, "size": 100.0}]
    first["asks"] = [{"price": .52, "size": 10.0}]
    # Mid .51, quote bid .50 joins behind only the 5 shares at .50.
    result = simulate(fixture(books=[first, book(10000)], takers=[taker(size=12.0)]), config())
    assert result["fills"][0]["price"] == .50
    assert result["fills"][0]["size"] == 7


def test_limit_not_reaching_our_quote_never_fills_and_no_trade_no_fill():
    data = fixture(takers=[taker(limit=.50, size=100.0)])
    assert simulate(data, config())["fills"] == []
    assert simulate(fixture(), config())["fills"] == []


def test_books_reprice_only_causally_and_latency_changes_arrival_book():
    books = [book(0), book(1025, bid=.59, ask=.61), book(10000, bid=.59, ask=.61)]
    data = fixture(books=books, takers=[taker(size=25.0, limit=.4)])
    immediate = simulate(data, config(taker_latency_ms=0))
    delayed = simulate(data, config(taker_latency_ms=50))
    assert immediate["fills"][0]["price"] == .49
    assert delayed["fills"][0]["price"] == .59
    assert immediate["fills"][0]["midpoint"] == .5
    assert delayed["fills"][0]["midpoint"] == .6
    # A pending intent cannot fill at/after the run's exclusive end.
    pending = simulate(fixture(takers=[taker(time=9990)]), config())
    assert pending["fills"] == [] and pending["takers_arriving_after_end"] == 1


def test_capital_inventory_limits_and_minimum_size():
    data = fixture(takers=[taker(size=1000.0), taker(time=2000, side="buy", size=1000.0, limit=.9)])
    result = simulate(data, config(initial_cash=2.0, initial_inventory=3.0, target_inventory=3.0,
                                   max_inventory=5.0, order_size=20.0))
    assert result["fills"][0]["size"] == 2
    assert result["fills"][1]["size"] == 3
    assert all(0 <= e["inventory"] <= 5 and e["cash"] >= 0 for e in result["equity"])
    empty = simulate(data, config(initial_cash=0.1, initial_inventory=0.0))
    assert empty["quotes"][0]["bid"] is None and empty["quotes"][0]["ask"] is None
    assert empty["fills"] == []


def test_fee_curve_rebate_is_not_own_fee_and_fee_holiday_zeroes_rebate():
    assert fee_equivalent(100, .5, .07) == 1.75
    assert fee_equivalent(100, .3, .07) == fee_equivalent(100, .7, .07) == 1.47
    result = simulate(fixture(takers=[taker()]), config())
    fill = result["fills"][0]
    assert fill["fee_equivalent"] == .2624  # 15 * .07 * .49 * .51, 5 decimals
    assert fill["maker_rebate"] == pytest.approx(.05248)
    assert fill["fee"] == 0
    assert simulate(fixture(takers=[taker()], fee_rate=0.0), config())["pnl"]["maker_rebate"] == 0


def test_rewards_minimum_size_distance_and_two_sided_extremes():
    assert order_score(.49, 100, .5, market()) == pytest.approx(64)
    assert order_score(.49, 9, .5, market()) == 0
    assert order_score(.44, 100, .5, market()) == 0
    assert minimum_score(90, 0, .5) == 30
    assert minimum_score(90, 60, .5) == 60
    assert minimum_score(90, 0, .09) == 0
    assert minimum_score(90, 0, .91) == 0
    assert minimum_score(90, 0, .10) == 30
    assert minimum_score(90, 0, .90) == 30


def test_rewards_integrate_normalized_share_and_stop_when_fill_below_threshold():
    # 20 shares at +/- .01 produce score 12.8. External aggregate score = 10.
    result = simulate(fixture(), config())
    assert result["pnl"]["liquidity_rewards"] == pytest.approx(10 * 12.8 / 22.8)
    assert result["reward_scoring"]["eligible_seconds"] == 10
    # At 1.05s bid remaining becomes 5, so it ceases scoring; ask scores / 3.
    partial = simulate(fixture(takers=[taker()]), config())
    expected = 1.05 * 12.8 / 22.8 + 8.95 * (12.8/3) / (10 + 12.8/3)
    assert partial["pnl"]["liquidity_rewards"] == pytest.approx(expected)
    assert simulate(fixture(reward_daily_pool=0.0), config())["pnl"]["liquidity_rewards"] == 0


def test_adjusted_midpoint_requires_depth_or_explicit_recorded_value():
    data = fixture(books=[book(0, size=5.0), book(10000, size=5.0)])
    assert simulate(data, config())["pnl"]["liquidity_rewards"] == 0
    payload = data.model_dump()
    for entry in payload["books"]:
        entry["reward_midpoint"] = .5
    assert simulate(Fixture.model_validate(payload), config())["pnl"]["liquidity_rewards"] > 0


def test_reward_day_boundary_and_last_interval():
    data = fixture(books=[book(DAY_MS - 1000), book(DAY_MS + 1000)], end=DAY_MS + 3000)
    result = simulate(data, config())
    days = result["daily_accruals"]
    assert [d["date"] for d in days] == ["1970-01-01", "1970-01-02"]
    assert days[1]["liquidity_rewards"] == pytest.approx(3 * days[0]["liquidity_rewards"])
    assert sum(d["liquidity_rewards"] for d in days) == pytest.approx(result["pnl"]["liquidity_rewards"])
    assert result["reward_scoring"]["eligible_seconds"] == 4


@pytest.mark.parametrize("outcome", [0, 1, None])
def test_pnl_decomposition_settlement_and_equity_reconcile(outcome):
    data = fixture(books=[book(0), book(10000, bid=.59, ask=.61)], takers=[taker()], outcome=outcome)
    result = simulate(data, config())
    pnl = result["pnl"]
    # Buy 15 @ .49; initial 50 shares marked .50, final 65 marked .60.
    assert pnl["trading_pnl"] == pytest.approx(6.65)
    assert pnl["settlement_jump"] == pytest.approx(65 * (outcome - .60) if outcome is not None else 0)
    assert pnl["total"] == pytest.approx(sum(pnl[k] for k in (
        "trading_pnl", "maker_rebate", "liquidity_rewards", "settlement_jump")) - pnl["fees"])
    assert result["ending_equity_with_accruals"] - result["initial_equity"] == pytest.approx(pnl["total"])
    assert result["ending_cash"] + result["ending_inventory"] * .60 + pnl["maker_rebate"] + pnl["liquidity_rewards"] == pytest.approx(result["ending_equity_with_accruals"])
    assert result["equity"][-1]["equity_with_accruals"] == result["ending_equity_with_accruals"]


def test_markout_signs_size_weighting_and_missing_future_are_explicit():
    data = fixture(books=[book(0), book(2050, bid=.47, ask=.49), book(10000)], takers=[taker()])
    result = simulate(data, config(markout_horizons_ms=[1000, 20000], max_markout_gap_ms=0))
    markout = result["fills"][0]["markouts"]["1000"]
    assert markout["signed_midpoint_drift"] == pytest.approx(-.02)
    assert markout["adverse_selection_cost"] == pytest.approx(.3)
    assert markout["execution_markout"] == pytest.approx(-.15)
    assert result["adverse_selection"]["1000"]["observed_fills"] == 1
    assert result["fills"][0]["markouts"]["20000"] is None
    assert result["adverse_selection"]["20000"]["size_weighted_signed_midpoint_drift"] is None
    stale = simulate(data, config(markout_horizons_ms=[1001], max_markout_gap_ms=1))
    assert stale["adverse_selection"]["1001"]["missing_fills"] == 1
    sell_data = fixture(books=[book(0), book(2050, bid=.51, ask=.53), book(10000)],
                        takers=[taker(side="buy", limit=.8)])
    assert simulate(sell_data, config())["fills"][0]["markouts"]["1000"]["adverse_selection_cost"] == pytest.approx(.3)


@pytest.mark.parametrize("category", ["crypto", "sports", "politics"])
def test_three_samples_are_deterministic_and_json_serializable(category):
    data = Fixture.model_validate_json((SAMPLE_DIR / f"{category}.json").read_text())
    result = simulate(data)
    assert result == simulate(data)
    json.dumps(result, allow_nan=False)
    assert result["fills"] and result["pnl"]["maker_rebate"] > 0 and result["pnl"]["liquidity_rewards"] > 0
    assert result["adverse_selection"]["1000"]["observed_fills"] > 0
    assert len(result["fixture_sha256"]) == 64
    assert all(0 <= e["inventory"] <= 200 and e["cash"] >= 0 for e in result["equity"])


@pytest.mark.parametrize("change", [
    {"books": [book(1000), book(0)]}, {"books": [book(0), book(0)]},
    {"books": [book(0, bid=.51), book(10000)]},
    {"books": [book(0, bid=.491), book(10000)]}, {"end_ms": 0},
    {"settlement_price": True}, {"settlement_price": 2},
    {"takers": [taker(time=10000)]}, {"live": True},
])
def test_invalid_fixture_rejected(change):
    with pytest.raises(ValidationError):
        Fixture.model_validate({**fixture().model_dump(), **change})


@pytest.mark.parametrize("change", [
    {"initial_cash": -1}, {"initial_inventory": 201}, {"initial_cash": float("nan")},
    {"initial_cash": float("inf")}, {"order_size": 0}, {"taker_latency_ms": True},
    {"markout_horizons_ms": [1, 1]}, {"markout_horizons_ms": []}, {"api_key": "forbidden"},
])
def test_invalid_config_rejected(change):
    with pytest.raises(ValidationError):
        Config.model_validate(change)


def test_cli_custom_fixture_config_output_and_validation(tmp_path):
    source, cfg, output = [tmp_path / name for name in ("fixture.json", "config.json", "result.json")]
    source.write_text(fixture(takers=[taker()]).model_dump_json())
    cfg.write_text(config().model_dump_json())
    command = [sys.executable, "-m", "arb_research.mm", "--fixture", str(source),
               "--config", str(cfg), "--output", str(output)]
    proc = subprocess.run(command, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(output.read_text())["fills"][0]["size"] == 15
    cfg.write_text('{"order_size": -1}')
    output.unlink()
    invalid = subprocess.run(command, capture_output=True, text=True)
    assert invalid.returncode == 2 and not output.exists()


def test_offline_run_never_opens_network(monkeypatch):
    import socket

    def reject(*args, **kwargs):
        raise AssertionError("offline simulation attempted network access")

    monkeypatch.setattr(socket, "socket", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    for path in SAMPLE_DIR.glob("*.json"):
        assert simulate(Fixture.model_validate_json(path.read_text()))["fills"]
