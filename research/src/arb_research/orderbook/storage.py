"""Versioned, UTC/market-partitioned JSONL journal. No network dependencies."""

from collections import OrderedDict
from datetime import datetime, timezone
import heapq
import json
import os
from pathlib import Path
import re
import uuid

from .core import Rebuilder, canonical, digest


def partition(market):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", market):
        raise ValueError("unsafe market partition")
    return market


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = uuid.uuid4().hex
        self.seq = 0
        self.handles = OrderedDict()

    def write(self, payload, received_ns, kind="event"):
        self.seq += 1
        row = {"version": 1, "session": self.session, "seq": self.seq,
               "received_ns": received_ns, "kind": kind, "payload": payload}
        row["sha256"] = digest(row)
        self.append("events", row, payload.get("market", "_control"))
        return row

    def append(self, stream, row, market="_control"):
        date = datetime.fromtimestamp(row["received_ns"] // 10 ** 9, timezone.utc).date()
        path = self.root / f"date={date}" / f"market={partition(market)}" / f"{stream}.{self.session}.jsonl"
        if path not in self.handles:
            path.parent.mkdir(parents=True, exist_ok=True)
            if len(self.handles) >= 64:
                _, handle = self.handles.popitem(last=False)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
            self.handles[path] = path.open("a", encoding="utf-8")
        handle = self.handles[path]
        self.handles.move_to_end(path)
        handle.write(canonical(row) + "\n")
        handle.flush()

    def sync(self):
        for handle in self.handles.values():
            handle.flush()
            os.fsync(handle.fileno())

    def close(self):
        self.sync()
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()


def _read(path):
    with path.open(encoding="utf-8") as handle:
        previous = None
        for line_no, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
                signature = row.pop("sha256")
                if not line.endswith("\n") or row["version"] != 1 or signature != digest(row):
                    raise ValueError("checksum/version/truncated line")
                row["sha256"] = signature
                order = (row["received_ns"], row["session"], row["seq"])
                if previous is not None and order <= previous:
                    raise ValueError("journal order regression")
                previous = order
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"invalid journal {path.name}:{line_no}: {exc}") from exc
            yield row


def records(root):
    paths = sorted(Path(root).glob("date=*/market=*/events.*.jsonl"))
    if not paths:
        raise ValueError("no events found")
    # Files are sorted individually; merge across days, markets and restart sessions.
    yield from heapq.merge(*(_read(path) for path in paths),
                          key=lambda row: (row["received_ns"], row["session"], row["seq"]))


def replay(root, at_ns=None):
    engine = Rebuilder()
    last = None
    for row in records(root):
        if at_ns is not None and row["received_ns"] > at_ns:
            break
        engine.apply(row)
        last = row["received_ns"]
    return {"as_of_received_ns": at_ns if at_ns is not None else last,
            "last_record_received_ns": last, "books": engine.export(), "gaps": engine.gaps}
