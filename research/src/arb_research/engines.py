"""Offline SMA adapters: signals at close, execution at the next bar's open."""

import json
import math
import sys
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from importlib.metadata import version

from .contracts import CreateBacktest, now


def targets(request, bars):
    """No pre-interval warmup and no same-bar or future-price signal access."""
    fast = request.strategy.parameters.fast_window
    slow = request.strategy.parameters.slow_window
    closes = [b["close"] for b in bars]
    return [
        i >= slow and sum(closes[i-fast:i]) / fast > sum(closes[i-slow:i]) / slow
        for i in range(len(bars))
    ]


def fast(request, dataset):
    import pandas as pd
    import vectorbt as vbt

    bars = dataset["bars"]
    index = pd.to_datetime([b["close_timestamp"] for b in bars], utc=True)
    desired = pd.Series(targets(request, bars), index=index)
    portfolio = vbt.Portfolio.from_signals(
        close=pd.Series([b["close"] for b in bars], index=index),
        entries=desired, exits=~desired,
        price=pd.Series([b["open"] for b in bars], index=index),
        init_cash=request.initial_capital, size=0.95, size_type="percent",
        fees=request.execution.fee_bps / 10000,
        slippage=request.execution.slippage_bps / 10000,
        freq="1h",
    )
    equity = [{"timestamp": b["close_timestamp"], "value": float(value)}
              for b, value in zip(bars, portfolio.value())]
    fills = []
    for row in portfolio.orders.records_arr:
        bar = bars[int(row["idx"])]
        fills.append({
            "timestamp": bar["timestamp"], "side": "buy" if row["side"] == 0 else "sell",
            "quantity": float(row["size"]), "price": float(row["price"]),
            "fee": float(row["fees"]), "currency": "USD",
        })
    return equity, fills


def realistic(request, dataset):
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import BTC, USD
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    venue = Venue("SIM")
    instrument = CurrencyPair(
        instrument_id=InstrumentId(Symbol("SYNTH/USD"), venue), raw_symbol=Symbol("SYNTH/USD"),
        base_currency=BTC, quote_currency=USD, price_precision=8, size_precision=6,
        price_increment=Price(0.00000001, 8), size_increment=Quantity(0.000001, 6),
        maker_fee=Decimal(str(request.execution.fee_bps)) / 10000,
        taker_fee=Decimal(str(request.execution.fee_bps)) / 10000,
        margin_init=Decimal(0), margin_maint=Decimal(0), ts_event=0, ts_init=0,
    )
    bars = dataset["bars"]
    desired = targets(request, bars)
    slip = request.execution.slippage_bps / 10000
    schedule = {}
    ticks = []
    for i, bar in enumerate(bars):
        for opening, field, stamp in (
            (True, "open", "timestamp"), (False, "close", "close_timestamp"),
        ):
            ts = int(datetime.fromisoformat(bar[stamp]).timestamp()) * 1_000_000_000
            mid = bar[field]
            schedule[ts] = (i, opening)
            # Deterministic synthetic bid/ask spread applies bps to each market fill.
            ticks.append(QuoteTick(
                instrument_id=instrument.id,
                bid_price=instrument.make_price(mid * (1 - slip)),
                ask_price=instrument.make_price(mid * (1 + slip)),
                bid_size=instrument.make_qty(1e12), ask_size=instrument.make_qty(1e12),
                ts_event=ts, ts_init=ts,
            ))

    class SmaStrategy(Strategy):
        def __init__(self):
            super().__init__()
            self.cash = float(Money(request.initial_capital, USD).as_double())
            self.quantity = 0.0
            self.equity = []
            self.fills = []
            self.failures = []

        def on_start(self):
            self.subscribe_quote_ticks(instrument.id)

        def on_quote_tick(self, tick):
            i, opening = schedule[tick.ts_event]
            bar = bars[i]
            if not opening:
                self.equity.append({
                    "timestamp": bar["close_timestamp"],
                    "value": self.cash + self.quantity * bar["close"],
                })
                return
            if desired[i] and self.quantity <= 0:
                ask = tick.ask_price.as_double()
                amount = self.cash * 0.95 / (ask * (1 + request.execution.fee_bps / 10000))
                side = OrderSide.BUY
            elif not desired[i] and self.quantity > 0:
                amount = self.quantity
                side = OrderSide.SELL
            else:
                return
            # Spot lots, cash reserve, no leverage and no terminal forced liquidation.
            amount = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            if amount <= 0:
                return
            self.submit_order(self.order_factory.market(
                instrument_id=instrument.id, order_side=side,
                quantity=instrument.make_qty(amount),
            ))

        def on_order_filled(self, event):
            qty, price = event.last_qty.as_double(), event.last_px.as_double()
            fee = event.commission.as_double()
            buy = event.order_side == OrderSide.BUY
            self.cash += (-qty * price if buy else qty * price) - fee
            self.quantity = round(self.quantity + (qty if buy else -qty), 6)
            self.fills.append({
                "timestamp": datetime.fromtimestamp(event.ts_event / 1e9,
                                                    tz=request.start.tzinfo).isoformat().replace("+00:00", "Z"),
                "side": "buy" if buy else "sell", "quantity": qty, "price": price,
                "fee": fee, "currency": "USD",
            })

        def on_order_rejected(self, event):
            self.failures.append(str(event))

        def on_order_denied(self, event):
            self.failures.append(str(event))

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
            base_currency=None, starting_balances=[Money(request.initial_capital, USD)],
        )
        engine.add_instrument(instrument)
        engine.add_data(ticks)
        strategy = SmaStrategy()
        engine.add_strategy(strategy)
        engine.run()
        if strategy.failures or len(strategy.equity) != len(bars):
            raise RuntimeError("Nautilus strategy did not complete: " + str(strategy.failures))
        # Cross-check our normalized ledger with the native account after every run.
        account = engine.cache.account_for_venue(venue)
        if not math.isclose(account.balance_total(USD).as_double(), strategy.cash, abs_tol=0.02):
            raise RuntimeError("Nautilus native account and normalized ledger disagree")
        return strategy.equity, strategy.fills
    finally:
        engine.dispose()


def run(job):
    request = CreateBacktest.model_validate(job["request"])
    engine = job["engine"]
    equity, fills = (fast if engine == "fast" else realistic)(request, job["dataset"])
    values = [request.initial_capital] + [p["value"] for p in equity]
    peak, drawdown = values[0], 0.
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    package = "vectorbt" if engine == "fast" else "nautilus_trader"
    return {
        "engine": engine, "phase": "draft" if engine == "fast" else "final",
        "source": {"package": package, "version": version(package),
                   "adapter": "arb.sma_cross.v1"},
        "as_of": job["dataset"]["as_of"], "computed_at": now(),
        "metrics": {
            "initial_capital": request.initial_capital, "final_equity": values[-1],
            "total_return": values[-1] / values[0] - 1, "max_drawdown": drawdown,
            "fill_count": len(fills), "fees_paid": sum(f["fee"] for f in fills),
        },
        "equity": equity, "fills": fills,
    }


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), allow_nan=False))
