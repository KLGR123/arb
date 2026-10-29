"""Explicit online collect and offline replay; neither creates a live client."""

import argparse
from decimal import Decimal
from importlib.metadata import version
import json
from pathlib import Path

from .nautilus_data_v2 import DataError, DataV2Client, ResponseCache, dump, load_snapshot


def replay(loaded):
    """ARB-9's two-fill mechanism, using real ticks and current recorded fees.

    Quotes derived from history have no depth and are deliberately NOT fed to
    the matching engine. The run tests ingestion, not realistic profitability.
    """
    if version("nautilus_trader") != "1.231.0":
        raise DataError("Replay requires nautilus_trader==1.231.0")
    if len(loaded.trades) < 4:
        raise DataError("Replay needs at least four recorded trades")
    try:
        from nautilus_trader.adapters.polymarket.fee_model import PolymarketFeeModel
    except ImportError:
        raise DataError("Replay fee model needs the optional polymarket-validation extra") from None
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.objects import Money
    from nautilus_trader.trading.strategy import Strategy

    instrument = loaded.instrument

    class RoundTrip(Strategy):
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
                    instrument_id=instrument.id, quantity=instrument.make_qty(1),
                    order_side=OrderSide.BUY if self.seen == 1 else OrderSide.SELL,
                ))

        def on_order_filled(self, event):
            self.fills.append({"price": str(event.last_px), "quantity": str(event.last_qty),
                               "commission": str(event.commission), "ts_event": event.ts_event})

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    strategy = RoundTrip()
    try:
        engine.add_venue(venue=instrument.id.venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
                         base_currency=None, starting_balances=[Money(1000, instrument.quote_currency)],
                         fee_model=PolymarketFeeModel(maker_rebates_enabled=False))
        engine.add_instrument(instrument)
        engine.add_data(loaded.trades)
        engine.add_strategy(strategy)
        engine.run()
        result = engine.get_result()
        if len(strategy.fills) != 2 or strategy.seen != len(loaded.trades):
            raise DataError("Replay did not process all ticks and two simulated fills")
        return {"nautilus_version": version("nautilus_trader"), "mode": "offline_backtest",
                "source": loaded.metadata["source"], "market_id": instrument.info["market_id"],
                "token_id": str(instrument.raw_symbol), "outcome": instrument.outcome,
                "protocol": instrument.info["protocol"], "settled": loaded.metadata["settled"],
                "trade_ticks": strategy.seen, "quote_ticks_available": len(loaded.quotes),
                "quote_ticks_used_for_execution": 0, "orders": result.total_orders, "fills": strategy.fills,
                "initial_cash": "1000.000000 pUSD",
                "final_cash": str(engine.cache.account_for_venue(instrument.id.venue)
                                  .balance_total(instrument.quote_currency)),
                "total_commission": str(sum((Decimal(f["commission"].split()[0].replace(",", ""))
                                              for f in strategy.fills), Decimal(0))),
                "fee_rate": str(instrument.taker_fee), "fee_source": instrument.info["fee_source"],
                "limitations": ["ingestion smoke, not strategy performance", "current fee schedule applied to historical trades",
                                "no order book, spread, latency or settlement simulation", "maker rebates disabled"]}
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Fetch allowlisted public data (explicit network action)")
    collect.add_argument("event", help="Gamma event ID or slug")
    collect.add_argument("--market-id", help="Optional market within the event")
    collect.add_argument("--data-root", default=".state/polymarket", help="Existing ARB-7 store (sync first)")
    collect.add_argument("--cache", default=".state/nautilus-data-v2/requests")
    collect.add_argument("--offline", action="store_true", help="Only read existing response cache")
    collect.add_argument("--refresh", action="store_true", help="Replace cached responses with fresh reads")
    collect.add_argument("--max-pages", type=int, default=100)
    collect.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("replay", help="Local BacktestEngine only; no HTTP client constructed")
    run.add_argument("snapshot", type=Path)
    run.add_argument("--token-id", help="Default: first token with at least four trades")
    run.add_argument("--data-root", help="Optionally read prices/resolutions from an ARB-7 store")
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            from .polymarket.store import DataStore
            if not 1 <= args.max_pages <= 1000:
                raise DataError("max-pages must be 1..1000")
            with DataV2Client(cache=ResponseCache(args.cache), offline=args.offline, refresh=args.refresh) as client:
                value = client.collect(args.event, store=DataStore(args.data_root),
                                       market_id=args.market_id, max_pages=args.max_pages)
            load_snapshot(value)  # Validate before calling the capture successful.
        else:
            from .polymarket.store import DataStore
            loaded = load_snapshot(json.loads(args.snapshot.read_text(encoding="utf-8")), token_id=args.token_id,
                                   price_store=DataStore(args.data_root) if args.data_root else None)
            candidate = next((token for token in loaded if len(token.trades) >= 4), None)
            if candidate is None:
                raise DataError("No token with at least four trades")
            value = replay(candidate)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(dump(value), encoding="utf-8")
    except DataError as exc:
        parser.exit(1, f"{exc}\n")
    except (RuntimeError, ValueError, KeyError, OSError):
        # Parser/transport errors can embed external inputs. Keep CLI diagnostics
        # static; library DataError messages remain safe and useful to callers.
        parser.exit(1, "Public-data collection/replay failed; check metadata, connectivity, cache and optional extra.\n")
    print(f"{args.command}: complete")


if __name__ == "__main__":
    main()
