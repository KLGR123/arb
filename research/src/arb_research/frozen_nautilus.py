"""Frozen ARB-7 identities + ARB-14 Data v2 trades -> Nautilus core objects.

No transport, SDK, execution adapter, store writer or settlement imports. Terms
must be qualified point-in-time inputs; capture-time Gamma terms are not a
historical fee schedule. Protocol V2 deliberately has no opt-in switch.
"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
from importlib.metadata import version
import json
import re

from pydantic import BaseModel, ConfigDict, Field

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide, AssetClass, CurrencyType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Price, Quantity

from .polymarket.models import Event, Market


NAUTILUS_VERSION = "1.231.0"
MAX_NS = 2**64 - 1


class FrozenDataError(ValueError):
    """Static diagnostics, without echoing arbitrary input payloads."""


class FrozenTerms(BaseModel):
    """One immutable, caller-qualified historical instrument/fee segment.

    known_at_ns covers ALL these fields, including the expiry and fee formula.
    valid_from_ns is inclusive; valid_until_ns is exclusive. Knowledge and
    effectiveness are distinct. Split a changing schedule into separate batches.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    event_id: str
    market_id: str
    condition_id: str
    known_at_ns: int = Field(strict=True, gt=0, le=MAX_NS)
    valid_from_ns: int = Field(strict=True, gt=0, le=MAX_NS)
    valid_until_ns: int = Field(strict=True, gt=0, le=MAX_NS)
    expiration_ns: int = Field(strict=True, gt=0, le=MAX_NS)
    fees_enabled: bool = Field(strict=True)
    fee_rate: Decimal = Field(ge=0, le=1)
    fee_exponent: int = Field(strict=True)


@dataclass(frozen=True)
class FrozenTicks:
    instruments: tuple[BinaryOption, ...]
    ticks: tuple[TradeTick, ...]


def _protocol(protocol):
    if protocol != "v1":
        raise FrozenDataError("Only explicit Protocol v1/CTF is qualified; Protocol V2 is disabled")


def _identity(market):
    _protocol(market.protocol)
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", market.condition_id):
        raise FrozenDataError("Invalid condition identity")
    if not market.id or not market.event_id or len(market.tokens) != 2:
        raise FrozenDataError("Binary market identity required")
    ids = set()
    for token in market.tokens:
        _protocol(token.protocol)
        if (not re.fullmatch(r"[1-9][0-9]{0,77}", token.id)
                or int(token.id) >= 2**256 or token.id in ids or not token.outcome):
            raise FrozenDataError("Distinct uint256 CTF token identities required")
        ids.add(token.id)
    if sorted(t.outcome_index for t in market.tokens) != [0, 1]:
        raise FrozenDataError("Distinct binary outcome indexes required")


def _ns(value):
    if type(value) is not int or not 0 < value <= MAX_NS:
        raise FrozenDataError("Timestamp must be positive uint64 nanoseconds")
    return value


def _exact(value, precision, *, positive=False, high=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise FrozenDataError("Invalid numeric observation")
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or (positive and number == 0)
                or (high is not None and number > high)):
            raise FrozenDataError("Numeric observation outside supported bounds")
        with localcontext() as context:
            context.prec = 80
            if number != number.quantize(Decimal(10) ** -precision):
                raise FrozenDataError("Observation exceeds supported decimal precision")
            return f"{number:.{precision}f}"
    except InvalidOperation:
        raise FrozenDataError("Invalid numeric observation") from None


def convert_frozen_ticks(market: Market, rows, *, terms: FrozenTerms,
                         start_ns=None, end_ns=None) -> FrozenTicks:
    """Convert a frozen newest-first Data v2 taker feed, without mutating it.

    Validate the whole batch before inclusive filtering. IDs are allocated before
    filtering so windows retain the same IDs. Same-second order reverses the
    recorded server order; it is not a reconstructed on-chain sequence.
    """
    if version("nautilus_trader") != NAUTILUS_VERSION:
        raise FrozenDataError("Conversion requires nautilus_trader==1.231.0")
    _identity(market)
    # Revalidate even model_copy/model_construct objects supplied by Python callers.
    terms = FrozenTerms.model_validate(terms.model_dump())
    if (terms.event_id, terms.market_id, terms.condition_id) != (
            market.event_id, market.id, market.condition_id):
        raise FrozenDataError("Historical terms identity mismatch")
    if (max(terms.known_at_ns, terms.valid_from_ns) >= terms.valid_until_ns
            or terms.expiration_ns <= terms.known_at_ns):
        raise FrozenDataError("Invalid historical terms interval")
    if terms.fee_exponent != 1 or (not terms.fees_enabled and terms.fee_rate != 0):
        raise FrozenDataError("Unsupported or contradictory fee schedule")
    if start_ns is not None:
        _ns(start_ns)
    if end_ns is not None:
        _ns(end_ns)
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise FrozenDataError("start_ns must not exceed end_ns")
    if not isinstance(rows, (list, tuple)):
        raise FrozenDataError("Frozen trades must be a materialized sequence")

    instruments = tuple(BinaryOption(
        instrument_id=InstrumentId(Symbol(f"{market.condition_id}-{token.id}"), Venue("POLYMARKET")),
        raw_symbol=Symbol(token.id), asset_class=AssetClass.ALTERNATIVE,
        currency=Currency("pUSD", 6, 0, "Polymarket USD", CurrencyType.CRYPTO),
        price_precision=16, size_precision=6,
        price_increment=Price.from_str("0.0000000000000001"),
        size_increment=Quantity.from_str("0.000001"),
        activation_ns=max(terms.known_at_ns, terms.valid_from_ns), expiration_ns=terms.expiration_ns,
        ts_event=terms.known_at_ns, ts_init=terms.known_at_ns,
        maker_fee=Decimal(0), taker_fee=terms.fee_rate,
        outcome=token.outcome,
        # No question, closed status, negRisk snapshot, payout, quotes or raw payload.
        info={"event_id": market.event_id, "market_id": market.id,
              "condition_id": market.condition_id, "token_id": token.id,
              "protocol": "v1", "outcome_index": token.outcome_index,
              "terms_known_at_ns": terms.known_at_ns,
              "fee_model": "taker_rate_times_p_times_one_minus_p;maker_rebates_disabled",
              "price_increment_kind": "historical_observation_precision"},
    ) for token in sorted(market.tokens, key=lambda t: t.outcome_index))
    by_token = {str(i.raw_symbol): i for i in instruments}
    outcomes = {token.id: token for token in market.tokens}
    parsed = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            raise FrozenDataError("Invalid trade row")
        token = row.get("token_id")
        if (row.get("condition_id") != market.condition_id
                or not isinstance(token, str) or token not in by_token):
            raise FrozenDataError("Trade condition/token identity mismatch")
        outcome = outcomes[token]
        if (("outcome_index" in row and (type(row["outcome_index"]) is not int
                                         or row["outcome_index"] != outcome.outcome_index))
                or ("outcome" in row and row["outcome"] != outcome.outcome)):
            raise FrozenDataError("Trade outcome identity mismatch")
        if row.get("side") not in ("BUY", "SELL"):
            raise FrozenDataError("Trade side must be BUY or SELL")
        seconds = row.get("timestamp")
        # Data API v2 timestamps are integer epoch seconds, never guessed units.
        if type(seconds) is not int:
            raise FrozenDataError("Trade timestamp must be integer epoch seconds")
        stamp = _ns(seconds * 10**9)
        if not max(terms.known_at_ns, terms.valid_from_ns) <= stamp < terms.valid_until_ns:
            raise FrozenDataError("Trade precedes known metadata or exceeds fee validity")
        price = _exact(row.get("price"), 16, high=Decimal(1))
        size = _exact(row.get("size"), 6, positive=True, high=Decimal("34028236692093"))
        tx, wallet = row.get("transaction_hash"), row.get("proxy_wallet")
        if (not isinstance(tx, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx)
                or not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet)):
            raise FrozenDataError("Trade transaction/wallet identity missing or invalid")
        identity = [market.condition_id, token, stamp, row["side"], price, size, tx.lower(), wallet.lower()]
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:32]
        parsed.append((stamp, token, row["side"], price, size, digest))
    parsed.sort(key=lambda r: r[0])
    occurrences, ticks = Counter(), []
    for stamp, token, side, price, size, digest in parsed:
        occurrences[digest] += 1
        if (start_ns is not None and stamp < start_ns) or (end_ns is not None and stamp > end_ns):
            continue
        ticks.append(TradeTick(
            instrument_id=by_token[token].id, price=Price.from_str(price), size=Quantity.from_str(size),
            aggressor_side=AggressorSide.BUYER if side == "BUY" else AggressorSide.SELLER,
            trade_id=TradeId(f"{digest}-{occurrences[digest]}"), ts_event=stamp, ts_init=stamp,
        ))
    return FrozenTicks(instruments, tuple(ticks))


def load_frozen_snapshot(snapshot, *, event: Event, terms: dict[str, FrozenTerms],
                         start_ns=None, end_ns=None) -> FrozenTicks:
    """Bind an ARB-14 portable snapshot to frozen ARB-7 normalized identities.

    ARB-7 price-history rows lack trade size/side and are never fabricated into
    trades. Prices, resolution records and capture-time fee fields are ignored.
    """
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise FrozenDataError("Unsupported frozen snapshot schema")
    if str(snapshot["event"]["id"]) != event.id:
        raise FrozenDataError("Snapshot event identity mismatch")
    markets = {m.id: m for m in event.markets}
    if len(markets) != len(event.markets):
        raise FrozenDataError("Duplicate ARB-7 market identities")
    instruments, ticks, seen, conditions, tokens = [], [], set(), set(), set()
    for item in snapshot["markets"]:
        raw = item["market"]
        _protocol(raw.get("version"))
        market_id = str(raw["id"])
        market = markets.get(market_id)
        if market is None or market.event_id != event.id or market_id in seen:
            raise FrozenDataError("Snapshot market identity mismatch or duplicate")
        _identity(market)
        ids = raw.get("clobTokenIds")
        labels = raw.get("outcomes")
        try:
            ids = json.loads(ids) if isinstance(ids, str) else ids
            labels = json.loads(labels) if isinstance(labels, str) else labels
        except (ValueError, TypeError):
            raise FrozenDataError("Invalid frozen outcome identities") from None
        ordered = sorted(market.tokens, key=lambda t: t.outcome_index)
        if (raw.get("conditionId") != market.condition_id
                or ids != [t.id for t in ordered] or labels != [t.outcome for t in ordered]):
            raise FrozenDataError("Snapshot and ARB-7 condition/outcome identities disagree")
        if market.condition_id in conditions or tokens.intersection(ids):
            raise FrozenDataError("Overlapping frozen market identities")
        if market_id not in terms:
            raise FrozenDataError("Qualified historical terms required for every market")
        batch = convert_frozen_ticks(market, item["trades"], terms=terms[market_id],
                                     start_ns=start_ns, end_ns=end_ns)
        seen.add(market_id)
        conditions.add(market.condition_id)
        tokens.update(ids)
        instruments.extend(batch.instruments)
        ticks.extend(batch.ticks)
    if not seen or set(terms) != seen:
        raise FrozenDataError("Historical terms must match the frozen market set")
    return FrozenTicks(tuple(instruments), tuple(sorted(ticks, key=lambda t: t.ts_event)))
