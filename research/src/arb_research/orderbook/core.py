"""Network-free state machine shared by the live recorder and offline replay."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json


EVENTS = {"book", "price_change", "last_trade_price", "tick_size_change"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("price/size must be finite and nonnegative")
    return number


def levels(rows):
    result = {}
    for row in rows:
        price, size = decimal(row["price"]), decimal(row["size"])
        if price > 1:
            raise ValueError("outcome price exceeds 1")
        if size:
            result[price] = size
    return result


@dataclass
class Book:
    market: str = ""
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    available: bool = False
    reason: str = "awaiting_book"
    server_hash: str | None = None
    timestamp: str | None = None
    tick_size: str | None = None
    last_trade_price: str | None = None

    def depth(self):
        return {
            name: [{"price": format(p.normalize(), "f"), "size": format(side[p].normalize(), "f")}
                   for p in sorted(side, reverse=reverse)]
            for name, side, reverse in (("bids", self.bids, True), ("asks", self.asks, False))
        }

    def export(self):
        return {
            "market": self.market, "available": self.available, "reason": self.reason,
            # Never expose old depth as an available book across a discontinuity.
            **(self.depth() if self.available else {"bids": [], "asks": []}),
            "server_hash": self.server_hash, "timestamp": self.timestamp,
            "tick_size": self.tick_size, "last_trade_price": self.last_trade_price,
        }


class Rebuilder:
    def __init__(self):
        self.books: dict[str, Book] = {}
        self.gaps: list[dict] = []
        self.last_seq = {}

    def gap(self, kind, row, asset=None, **details):
        self.gaps.append({"kind": kind, "received_ns": row["received_ns"],
                          "session": row["session"], "seq": row["seq"],
                          "asset_id": asset, **details})
        targets = [self.books[asset]] if asset in self.books else self.books.values()
        for book in targets:
            book.available, book.reason = False, kind

    def apply(self, row):
        """Apply in receive order; sequence here is OUR durable journal sequence."""
        session, seq = row["session"], row["seq"]
        previous = self.last_seq.get(session, 0)
        if seq != previous + 1:
            self.gap("journal_sequence", row, expected=previous + 1, actual=seq)
        self.last_seq[session] = seq
        event = row["payload"]
        kind = event.get("event_type")
        if row["kind"] == "control":
            if kind == "started":
                # A prior process may have crashed without a final stopped record.
                for book in self.books.values():
                    book.available, book.reason = False, "recorder_restarted"
            elif kind == "connected":
                for asset in event["assets_ids"]:
                    self.books.setdefault(asset, Book())
                for book in self.books.values():
                    book.available, book.reason = False, "awaiting_book"
            elif kind == "stopped":
                for book in self.books.values():
                    book.available, book.reason = False, "recording_stopped"
            elif kind in {"disconnected", "invalid_frame", "heartbeat_timeout"}:
                self.gap(kind, row)
            return
        if kind not in EVENTS:
            return
        try:
            self._event(event, row)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            # Malformed batches may have mutated several assets; invalidate all.
            self.gap("invalid_event", row)

    def _event(self, event, row):
        kind = event["event_type"]
        market = event["market"]
        if kind == "price_change":
            # All replacements in the event form one atomic book update.
            grouped = {}
            for change in event["price_changes"]:
                grouped.setdefault(change["asset_id"], []).append(change)
            for asset, changes in grouped.items():
                book = self.books.setdefault(asset, Book(market=market))
                if not book.available:
                    # A partial delta cannot seed or heal an unknown book.
                    self.gap("delta_without_book", row, asset)
                    continue
                for change in changes:
                    price, size = decimal(change["price"]), decimal(change["size"])
                    if price > 1 or change["side"] not in {"BUY", "SELL"}:
                        raise ValueError("invalid level")
                    side = book.bids if change["side"] == "BUY" else book.asks
                    if size:
                        side[price] = size
                    else:
                        side.pop(price, None)
                # The last change for an asset describes its final top of book.
                change = changes[-1]
                old_hash, old_depth = book.server_hash, getattr(book, "_hash_depth", None)
                book.server_hash = change.get("hash")
                depth = digest(book.depth())
                if old_hash and old_hash == book.server_hash and old_depth != depth:
                    self.gap("server_hash_conflict", row, asset)
                book._hash_depth = depth
                for key, actual in (("best_bid", max(book.bids, default=Decimal(0))),
                                    ("best_ask", min(book.asks, default=Decimal(1)))):
                    if change.get(key) is not None and decimal(change[key]) != actual:
                        self.gap("best_price_mismatch", row, asset, field=key)
                book.timestamp = event.get("timestamp")
            return
        asset = event["asset_id"]
        book = self.books.setdefault(asset, Book())
        book.market = market
        if kind == "book":
            bids, asks = levels(event["bids"]), levels(event["asks"])
            if book.available and (book.bids != bids or book.asks != asks):
                # The snapshot is authoritative, but continuity before it is uncertain.
                self.gap("snapshot_mismatch", row, asset)
            if book.server_hash and book.server_hash == event.get("hash"):
                candidate = Book(bids=bids, asks=asks)
                if getattr(book, "_hash_depth", None) != digest(candidate.depth()):
                    self.gap("server_hash_conflict", row, asset)
            book.bids, book.asks = bids, asks
            book.available, book.reason = True, "snapshot_seeded"
            book.server_hash = event.get("hash")
            book._hash_depth = digest(book.depth())
            book.timestamp = event.get("timestamp")
        elif kind == "tick_size_change":
            decimal(event["new_tick_size"])
            book.tick_size = event["new_tick_size"]
        elif kind == "last_trade_price":
            decimal(event["price"])
            book.last_trade_price = event["price"]

    def export(self):
        return {asset: book.export() for asset, book in sorted(self.books.items())}
