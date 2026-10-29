"""Public market channel only: fixed endpoints, no credentials or proxy support."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import logging
import os
import random
import sys
import time

from ..compliance import GeoblockClient, PUBLIC_API_BUDGET
from .core import EVENTS, Rebuilder, canonical
from .storage import Journal


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LOG = logging.getLogger(__name__)
GEO_CLIENT = GeoblockClient()


class ResyncRequired(Exception):
    pass


class NetworkFailure(Exception):
    pass


async def network(operation):
    """Classify OSError only at socket operations; disk failures must propagate."""
    try:
        return await operation
    except OSError as exc:
        raise NetworkFailure(type(exc).__name__) from exc


@asynccontextmanager
async def open_market(connect, **options):
    context = connect(WS_URL, **options)
    ws = await network(context.__aenter__())
    try:
        yield ws
    except BaseException:
        # A close failure must not mask a durability error or task cancellation.
        try:
            await context.__aexit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        await network(context.__aexit__(None, None, None))


class TokenBucket:
    """One process-wide budget for HTTP, connects and subscription/heartbeat frames.

    Conservative 5/s, burst 5, under the shared compliance budget.
    Run one recorder process for the selected markets.
    """

    def __init__(self, rate=5, capacity=5):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.updated = float(capacity), time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    break
                await asyncio.sleep((1 - self.tokens) / self.rate)
        await asyncio.to_thread(PUBLIC_API_BUDGET.acquire)


NETWORK_BUDGET = TokenBucket()


async def public_get(client, url, params=None, budget=NETWORK_BUDGET):
    for attempt in range(4):
        await budget.take()
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 3:
                raise
            await asyncio.sleep(min(30, 2 ** attempt + random.random()))


async def resolve_assets(tokens, markets):
    import httpx

    # Reuse ARB-6's validated, cached, fail-closed direct geoblock client.
    result = await asyncio.to_thread(GEO_CLIENT.check)
    geo = {key: getattr(result, key) for key in ("blocked", "country", "region")}
    if result.error:
        raise ValueError(f"geoblock unavailable: {result.error}; recording blocked")
    if geo["blocked"] or geo["country"].upper() == "US" or geo["region"].upper() == "VA":
        raise ValueError("geoblock denied this exit; recording blocked (no proxy fallback)")
    # Explicitly ignore HTTP(S)_PROXY/ALL_PROXY and disallow redirects.
    async with httpx.AsyncClient(trust_env=False, timeout=10, follow_redirects=False) as client:
        assets = set(tokens)
        for market in markets:
            rows = await public_get(client, "https://gamma-api.polymarket.com/markets",
                                    {"condition_ids": market})
            matched = [row for row in rows if row.get("conditionId") == market]
            if len(matched) != 1:
                raise ValueError(f"condition market not found: {market}")
            ids = matched[0]["clobTokenIds"]
            assets.update(json.loads(ids) if isinstance(ids, str) else ids)
    if not assets or len(assets) > 200 or any(not str(a).isdigit() for a in assets):
        raise ValueError("select 1..200 numeric outcome token IDs")
    return sorted(assets), {key: geo.get(key) for key in ("blocked", "country", "region")}


class Recorder:
    def __init__(self, root, assets, *, snapshot_seconds=60, heartbeat_seconds=10,
                 pong_seconds=10, initial_book_seconds=30, budget=NETWORK_BUDGET):
        self.journal = Journal(root)
        self.assets = assets
        self.engine = Rebuilder()
        self.snapshot_seconds = snapshot_seconds
        self.heartbeat_seconds, self.pong_seconds = heartbeat_seconds, pong_seconds
        self.initial_book_seconds = initial_book_seconds
        self.budget = budget
        self.metrics = Counter()
        self.last_received_ns = 0
        self.seeded_assets = set()

    def now(self):
        # Wall clock adjustments must not regress the receive-order journal.
        self.last_received_ns = max(time.time_ns(), self.last_received_ns + 1)
        return self.last_received_ns

    def emit(self, payload, kind="event"):
        row = self.journal.write(payload, self.now(), kind)
        self.engine.apply(row)
        self.metrics["records"] += 1
        self.metrics[f"event_{payload['event_type']}"] += 1
        gaps, self.engine.gaps = self.engine.gaps, []
        for gap in gaps:
            self.metrics["gaps_total"] += 1
            self.metrics[f"gap_{gap['kind']}"] += 1
            self.journal.append("gaps", gap)
            LOG.warning(canonical(gap))
        return gaps

    def control(self, kind, **fields):
        return self.emit({"event_type": kind, **fields}, "control")

    def checkpoint(self):
        for asset, book in self.engine.export().items():
            self.journal.append("snapshots", {
                "version": 1, "session": self.journal.session, "through_seq": self.journal.seq,
                "received_ns": self.now(), "asset_id": asset, "book": book,
            }, book["market"] or "_control")
        self.journal.sync()
        metrics = {"session": self.journal.session, "through_seq": self.journal.seq,
                   "received_ns": self.last_received_ns, "counters": dict(self.metrics),
                   "available_books": sum(b.available for b in self.engine.books.values()),
                   "upstream_sequence": "not_guaranteed_by_market_protocol",
                   "upstream_hash": "retained_and_consistency_checked_not_cryptographically_verified"}
        target = self.journal.root / "metrics.json"
        temporary = target.with_suffix(f".{self.journal.session}.tmp")
        temporary.write_text(canonical(metrics) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    async def connection(self, ws, deadline):
        await self.budget.take()
        await asyncio.wait_for(network(ws.send(canonical({"type": "market", "assets_ids": self.assets}))),
                               self.pong_seconds)
        self.control("connected", assets_ids=self.assets)
        now = time.monotonic()
        ping_at, snapshot_at = now + self.heartbeat_seconds, now + self.snapshot_seconds
        initial_at = now + self.initial_book_seconds
        awaiting = set(self.assets)
        pong_by = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if pong_by is not None and now >= pong_by:
                self.control("heartbeat_timeout")
                raise TimeoutError("market PONG timeout")
            if awaiting and now >= initial_at:
                raise TimeoutError("missing initial book snapshots")
            if now >= ping_at:
                await self.budget.take()
                await asyncio.wait_for(network(ws.send("PING")), self.pong_seconds)
                self.metrics["pings"] += 1
                if pong_by is None:
                    pong_by = now + self.pong_seconds
                ping_at = now + self.heartbeat_seconds
            if now >= snapshot_at:
                self.checkpoint()
                snapshot_at = now + self.snapshot_seconds
            timers = [deadline, ping_at, snapshot_at]
            if pong_by is not None:
                timers.append(pong_by)
            if awaiting:
                timers.append(initial_at)
            try:
                raw = await asyncio.wait_for(network(ws.recv()), max(.001, min(timers) - time.monotonic()))
            except asyncio.TimeoutError:
                continue
            if raw == "PONG":
                pong_by = None
                self.metrics["pongs"] += 1
                continue
            try:
                messages = json.loads(raw)
                messages = messages if isinstance(messages, list) else [messages]
                if not all(isinstance(m, dict) and isinstance(m.get("event_type"), str) for m in messages):
                    raise ValueError("unexpected market frame")
            except (ValueError, TypeError):
                self.control("invalid_frame", raw=str(raw))
                raise ResyncRequired("invalid market frame")
            reconnect = False
            # Persist the entire received batch before attempting a resubscription.
            for message in messages:
                gaps = self.emit(message)
                if message["event_type"] == "book":
                    asset = message.get("asset_id")
                    if asset in self.engine.books and self.engine.books[asset].available:
                        awaiting.discard(asset)
                        self.seeded_assets.add(asset)
                reconnect |= any(g["kind"] in {"best_price_mismatch", "invalid_event",
                                                "server_hash_conflict"} for g in gaps)
            if reconnect:
                raise ResyncRequired("book continuity uncertain; reconnect for fresh snapshots")

    async def run(self, *, duration=None, connect=None, sleep=asyncio.sleep):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        if connect is None:
            from websockets.asyncio.client import connect
        deadline = time.monotonic() + duration if duration is not None else float("inf")
        attempt = 0
        self.control("started", assets_ids=self.assets)
        try:
            while time.monotonic() < deadline:
                try:
                    await self.budget.take()
                    started = time.monotonic()
                    # A fixed public endpoint, never user/authenticated streams or proxies.
                    async with open_market(connect, proxy=None,
                                           open_timeout=min(10, max(.01, deadline - started)),
                                           close_timeout=2, max_size=16 * 1024 * 1024,
                                           ping_interval=None) as ws:
                        await self.connection(ws, deadline)
                except (ConnectionClosed, InvalidHandshake, NetworkFailure,
                        TimeoutError, ResyncRequired) as exc:
                    self.control("disconnected", error_type=type(exc).__name__)
                    self.metrics["reconnects"] += 1
                    if time.monotonic() - started > 60:
                        attempt = 0
                    delay = min(30, 2 ** min(attempt, 5)) + random.random()
                    attempt += 1
                    await sleep(min(delay, max(0, deadline - time.monotonic())))
        finally:
            try:
                self.control("stopped")
                self.checkpoint()
            finally:
                self.journal.close()


async def run_guarded(recorder, **kwargs):
    """Serialize writers for this root. Lock is automatically released on a crash."""
    import fcntl

    path = recorder.journal.root / ".record.lock"
    with path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another recorder owns this output directory") from exc
        await recorder.run(**kwargs)
