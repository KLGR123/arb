"""Immutable Parquet pages, SQLite commit manifest, DuckDB history queries.

Files become visible together with their checkpoint in one SQLite transaction.
A crash before that commit can leave an orphan file, never a skipped page.
"""

import fcntl
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .models import Event, History, PricePage, PriceRow, Resolution, ResolutionResult


class DataStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "manifest.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (token TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fragments (path TEXT PRIMARY KEY, market TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resolutions (market TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=30)

    @contextmanager
    def writer(self):
        # Cross-process single writer, including metadata and all token streams.
        with (self.root / "sync.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another sync owns this data directory") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def put_event(self, event: Event):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO events VALUES (?, ?)", (event.id, event.model_dump_json()))

    def event(self, selector: str) -> Event:
        with self.connect() as db:
            row = db.execute("SELECT body FROM events WHERE id=? OR json_extract(body, '$.slug')=?",
                             (selector, selector)).fetchone()
        if row is None:
            raise KeyError(selector)
        return Event.model_validate_json(row[0])

    def market_event(self, market_id: str) -> Event:
        with self.connect() as db:
            row = db.execute("""SELECT events.body FROM events, json_each(events.body, '$.markets') AS m
                                WHERE json_extract(m.value, '$.id')=? LIMIT 1""", (market_id,)).fetchone()
        if row is None:
            raise KeyError(market_id)
        return Event.model_validate_json(row[0])

    def checkpoint(self, token_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM checkpoints WHERE token=?", (token_id,)).fetchone()
        return json.loads(row[0]) if row else {"through": 0, "pending": None}

    def commit_page(self, market_id: str, token_id: str, page: PricePage, state: dict):
        paths = []
        by_date = {}
        version = time.time_ns()
        for point in page.data:
            date = datetime.fromtimestamp(point.timestamp, UTC).date().isoformat()
            by_date.setdefault(date, []).append({**point.model_dump(), "token_id": token_id,
                                                "market_id": market_id, "_version": version})
        for date, rows in by_date.items():
            directory = self.root / "prices" / f"market={quote(market_id, safe='')}" / f"date={date}"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{uuid.uuid4().hex}.parquet"
            temp = path.with_suffix(".partial")
            pq.write_table(pa.Table.from_pylist(rows), temp, compression="zstd")
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp, path)
            # Persist the rename before making the file visible in the manifest.
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            paths.append(str(path.relative_to(self.root)))
        with self.connect() as db:
            for path in paths:
                db.execute("INSERT INTO fragments VALUES (?, ?)", (path, market_id))
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (token_id, json.dumps(state)))

    def put_resolutions(self, event: Event, rows: list[Resolution]):
        with self.connect() as db:
            for market in event.markets:
                db.execute("DELETE FROM resolutions WHERE market=?", (market.id,))
            for row in rows:
                db.execute("INSERT OR REPLACE INTO resolutions VALUES (?, ?)",
                           (row.market_id, row.model_dump_json()))

    def resolutions(self, event: Event, market_id: str | None = None) -> ResolutionResult:
        ids = [m.id for m in event.markets if market_id is None or m.id == market_id]
        with self.connect() as db:
            rows = db.execute("SELECT body FROM resolutions WHERE market IN (SELECT value FROM json_each(?))",
                              (json.dumps(ids),)).fetchall()
        data = sorted([Resolution.model_validate_json(r[0]) for r in rows], key=lambda r: r.market_id)
        found = {r.market_id for r in data}
        return ResolutionResult(data=data, missing_market_ids=[i for i in ids if i not in found])

    def history(self, event: Event, *, market_id: str | None = None, token_id: str | None = None,
                start: int = 1, end: int = 253402300799, offset: int = 0, limit: int = 1000) -> History:
        if start <= 0 or end < start or offset < 0 or not 1 <= limit <= 10000:
            raise ValueError("Invalid history bounds or pagination")
        ids = [m.id for m in event.markets if market_id is None or m.id == market_id]
        with self.connect() as db:
            paths = [str(self.root / row[0]) for row in db.execute(
                "SELECT path FROM fragments WHERE market IN (SELECT value FROM json_each(?))", (json.dumps(ids),))]
        if not paths:
            return History(data=[])
        with duckdb.connect() as query:
            rows = query.execute("""
                SELECT market_id, token_id, timestamp, price, resolution_seconds
                FROM read_parquet(?, hive_partitioning=false)
                WHERE timestamp >= ? AND timestamp <= ? AND (? IS NULL OR token_id=?)
                QUALIFY row_number() OVER (
                    PARTITION BY market_id, token_id, timestamp, resolution_seconds ORDER BY _version DESC
                )=1
                ORDER BY timestamp, market_id, token_id, resolution_seconds
                LIMIT ? OFFSET ?
            """, [paths, start, end, token_id, token_id, limit+1, offset]).fetchall()
        data = [PriceRow(event_id=event.id, market_id=r[0], token_id=r[1], timestamp=r[2], price=r[3],
                         resolution_seconds=r[4]) for r in rows[:limit]]
        return History(data=data, next_offset=offset+limit if len(rows) > limit else None)
