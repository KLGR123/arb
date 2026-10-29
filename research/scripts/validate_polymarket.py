"""Offline 1.231.0 adapter diagnostics. No live clients or network transport.

The replay uses settled metadata plus synthetic trades, NOT recorded history.
The retired /trades route below is only a fixture dispatch key, never requested.
"""

import argparse
import asyncio
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from importlib.metadata import version
import inspect
import json
from pathlib import Path
from types import SimpleNamespace


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "polymarket"


def adapter_types():
    if version("nautilus_trader") != "1.231.0":
        raise RuntimeError("This diagnostic requires nautilus_trader==1.231.0")
    from nautilus_trader.adapters.polymarket.fee_model import PolymarketFeeModel
    from nautilus_trader.adapters.polymarket.loaders import PolymarketDataLoader

    return PolymarketDataLoader, PolymarketFeeModel


class FixtureHttpClient:
    """In-memory HTTP-shaped responses; unexpected requests fail closed."""

    def __init__(self, rate="0", protocol=None):
        self.fixture = json.loads((FIXTURES / "settled-metadata.json").read_text())
        self.market = deepcopy(self.fixture["market"])
        gamma = deepcopy(self.fixture["gamma_market"])
        # Controlled scenario parameters, not a recorded fee schedule.
        gamma["feeSchedule"] = {"rate": rate, "exponent": 1, "takerOnly": True}
        self.market["feeSchedule"] = gamma["feeSchedule"]
        if protocol is not None:
            gamma["version"] = protocol
            self.market["version"] = protocol
        self.event = {"markets": [gamma], "closed": True}
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if url == "https://gamma-api.polymarket.com/events":
            assert params == {"slug": self.fixture["event_slug"]}
            body = [self.event]
        elif url == f"https://clob.polymarket.com/markets/{self.market['condition_id']}":
            body = self.market
        elif url == "https://data-api.polymarket.com/trades":
            assert params["market"] == self.market["condition_id"]
            body = [
                {"asset": self.market["tokens"][0]["token_id"], "price": price,
                 "size": "100", "side": "BUY", "timestamp": 1773100800 + index,
                 "transactionHash": f"0x{index + 1:064x}"}
                for index, price in enumerate(self.fixture["synthetic_prices"])
            ][::-1]  # Native loader must restore chronology.
        else:
            raise AssertionError(f"Unrecorded request: {url} {params}")
        return SimpleNamespace(status=200, body=json.dumps(deepcopy(body)).encode())


async def load_scenario(rate="0", protocol=None, token_index=0):
    loader_type, _ = adapter_types()
    client = FixtureHttpClient(rate, protocol)
    loaders = await loader_type.from_event_slug(
        client.fixture["event_slug"], token_index=token_index,
        http_client=client, sanitize_info=True,
    )
    return loaders[0], client


def fee_checks(instrument):
    _, model_type = adapter_types()
    from nautilus_trader.model.enums import LiquiditySide
    from nautilus_trader.model.objects import Price, Quantity

    model = model_type(maker_rebates_enabled=False)
    results = []
    for qty, price in [("100", "0.01"), ("100", "0.30"), ("100", "0.50"),
                       ("100", "0.70"), ("100", "0.99"), ("0.002", "0.5")]:
        expected = (Decimal(qty) * Decimal("0.07") * Decimal(price)
                    * (1 - Decimal(price))).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
        actual = model.get_commission(
            SimpleNamespace(liquidity_side=LiquiditySide.TAKER),
            Quantity.from_str(qty), Price.from_str(price), instrument,
        ).as_decimal()
        results.append({"quantity": qty, "price": price, "reference_half_up": str(expected),
                        "actual": str(actual), "matches": actual == expected})
    maker = model.get_commission(
        SimpleNamespace(liquidity_side=LiquiditySide.MAKER),
        Quantity.from_str("100"), Price.from_str("0.5"), instrument,
    )
    return {"grid": results, "maker_rebates_disabled": str(maker)}


def replay(loader, ticks):
    """Two simulated taker fills; BacktestEngine is the only execution surface."""
    _, model_type = adapter_types()
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    instrument = loader.instrument

    class RecordedFees(model_type):
        def __init__(self):
            super().__init__(maker_rebates_enabled=False)
            self.commissions = []

        def get_commission(self, order, fill_qty, fill_px, instrument):
            value = super().get_commission(order, fill_qty, fill_px, instrument)
            self.commissions.append(value.as_decimal())
            return value

    class FixtureStrategy(Strategy):
        def __init__(self):
            super().__init__()
            self.seen = 0
            self.fills = []

        def on_start(self):
            self.subscribe_trade_ticks(instrument.id)

        def on_trade_tick(self, tick):
            self.seen += 1
            if self.seen in (1, 3):
                self.submit_order(self.order_factory.market(
                    instrument_id=instrument.id,
                    order_side=OrderSide.BUY if self.seen == 1 else OrderSide.SELL,
                    quantity=instrument.make_qty(10),
                ))

        def on_order_filled(self, event):
            self.fills.append({"price": str(event.last_px), "commission": str(event.commission)})

    fees = RecordedFees()
    strategy = FixtureStrategy()
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=instrument.id.venue, oms_type=OmsType.NETTING,
            account_type=AccountType.CASH, base_currency=None,
            starting_balances=[Money(1000, instrument.quote_currency)], fee_model=fees,
        )
        engine.add_instrument(instrument)
        engine.add_data(ticks)
        engine.add_strategy(strategy)
        engine.run()
        result = engine.get_result()
        if len(strategy.fills) != 2 or strategy.seen != 4:
            raise AssertionError(f"Expected 4 ticks / 2 fills, got {strategy.seen} / {strategy.fills}")
        return {"ticks": strategy.seen, "orders": result.total_orders, "fills": strategy.fills,
                "total_commission": str(sum(fees.commissions, Decimal(0))),
                "final_cash": str(engine.cache.account_for_venue(instrument.id.venue)
                                  .balance_total(instrument.quote_currency)),
                "data_kind": "synthetic trades on upstream settled metadata"}
    finally:
        engine.dispose()


def coverage(markets):
    active = {str(m["id"]): m for m in markets if m.get("active") is True and m.get("closed") is False}
    counts = {"v1": 0, "v2": 0, "unknown": 0}
    for market in active.values():
        protocol = market.get("version")
        counts[protocol if protocol in ("v1", "v2") else "unknown"] += 1
    total = len(active)
    return {"active_unique_markets": total, "counts": counts,
            "shares": {key: value / total if total else None for key, value in counts.items()}}


async def run_validation():
    loader_type, model_type = adapter_types()
    loader, client = await load_scenario("0.07", "v1")
    ticks = await loader.load_trades()
    protocols = {}
    for protocol in (None, "v1", "v2"):
        candidate, _ = await load_scenario(protocol=protocol)
        protocols[str(protocol)] = type(candidate.instrument).__name__
    free_loader, _ = await load_scenario()
    free_ticks = await free_loader.load_trades()
    sample = json.loads((FIXTURES / "active-sample.json").read_text())
    return {
        "version": version("nautilus_trader"),
        "sdk_dependency": {"py-clob-client-v2": version("py-clob-client-v2")},
        "api": {"from_event_slug": str(inspect.signature(loader_type.from_event_slug)),
                "load_trades": str(inspect.signature(loader_type.load_trades)),
                "fee_model": str(inspect.signature(model_type))},
        "protocol_fixture_admission": protocols,
        "resolution_metadata": loader.resolution_metadata,
        "instrument_info_has_winner": any("winner" in t for t in loader.instrument.info.get("tokens", [])),
        "instrument_info_terminal_prices": [t.get("price") for t in loader.instrument.info.get("tokens", [])],
        "fee_checks": fee_checks(loader.instrument),
        "fee_enabled_replay": replay(loader, ticks),
        "fee_free_replay": replay(free_loader, free_ticks),
        "fixture_dispatches": client.calls,
        "active_sample": coverage(sample["markets"]),
        "real_settled_v1_history": {
            "status": "not_validated",
            "reason": "Native loader uses retired Data API /trades; no live legacy calls allowed. NAS public HTTPS timed out. Settled source fixture has no protocol version; synthetic replay is not historical validation.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run_validation())
    encoded = json.dumps(result, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
