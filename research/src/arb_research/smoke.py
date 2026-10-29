"""Deterministic offline integration smoke, not a business strategy or runner API."""

import argparse
import json
from importlib.metadata import version
from math import isclose


def fast_smoke() -> dict:
    import pandas as pd
    import vectorbt as vbt

    prices = pd.Series(
        [100.0, 101.0, 102.0, 103.0],
        index=pd.date_range("2026-01-01", periods=4, freq="1h", tz="UTC"),
    )
    portfolio = vbt.Portfolio.from_holding(prices, init_cash=1000.0, freq="1h")
    final_value = float(portfolio.value().iloc[-1])
    if not isclose(final_value, 1030.0, abs_tol=1e-8):
        raise AssertionError(f"Unexpected VectorBT fixture result: {final_value}")
    return {
        "engine": "fast",
        "package": "vectorbt",
        "version": version("vectorbt"),
        "bars": len(prices),
        "initial_value": 1000.0,
        "final_value": final_value,
        "status": "passed",
    }


def realistic_smoke() -> dict:
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.trading.strategy import Strategy

    instrument = TestInstrumentProvider.btcusdt_binance()
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")

    class BarCounter(Strategy):
        """Subscribe and count only: this smoke never submits orders."""

        def __init__(self):
            super().__init__()
            self.seen = 0

        def on_start(self):
            self.subscribe_bars(bar_type)

        def on_bar(self, bar):
            self.seen += 1

    bars = []
    for index in range(4):
        timestamp = 1_767_225_600_000_000_000 + index * 60_000_000_000
        price = 100 + index
        bars.append(Bar(
            bar_type=bar_type,
            open=instrument.make_price(price),
            high=instrument.make_price(price + 1),
            low=instrument.make_price(price - 1),
            close=instrument.make_price(price + 0.5),
            volume=instrument.make_qty(10),
            ts_event=timestamp,
            ts_init=timestamp,
        ))

    engine = BacktestEngine(config=BacktestEngineConfig(
        logging=LoggingConfig(bypass_logging=True),
    ))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=None,
            starting_balances=[Money(1000, USDT)],
        )
        engine.add_instrument(instrument)
        engine.add_data(bars)
        counter = BarCounter()
        engine.add_strategy(counter)
        engine.run()
        result = engine.get_result()
        if counter.seen != len(bars) or result.total_orders != 0:
            raise AssertionError(
                f"Expected 4 bars and zero orders; got {counter.seen} and {result.total_orders}",
            )
        return {
            "engine": "realistic",
            "package": "nautilus_trader",
            "version": version("nautilus_trader"),
            "bars": counter.seen,
            "orders": result.total_orders,
            "status": "passed",
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["fast", "realistic", "all"], default="all")
    args = parser.parse_args()
    for name, run in [("fast", fast_smoke), ("realistic", realistic_smoke)]:
        if args.engine in (name, "all"):
            print(json.dumps(run(), sort_keys=True))


if __name__ == "__main__":
    main()
