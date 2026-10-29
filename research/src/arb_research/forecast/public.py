"""Fixed public GET endpoints only. No SDK, account, or trading capability."""

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .context import probability, timestamp
from .runner import now


class TokenBucket:
    """One cross-process bucket per NAS user, shared across isolated checkouts.

    Deliberately conservative: 5 requests/second, burst 2, under 9,000/10s.
    SQLite serializes consumption; sleep happens outside the transaction.
    """

    def __init__(self, path=None, *, clock=time.time, sleep=time.sleep):
        self.path = Path(path or Path.home() / ".cache/arb/public-rate.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.sleep = clock, sleep
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("CREATE TABLE IF NOT EXISTS bucket (id INTEGER PRIMARY KEY, tokens REAL, at REAL)")
            db.execute("INSERT OR IGNORE INTO bucket VALUES (1, 2, ?)", (clock(),))

    def acquire(self):
        while True:
            with sqlite3.connect(self.path, timeout=30) as db:
                db.execute("BEGIN IMMEDIATE")
                tokens, at = db.execute("SELECT tokens, at FROM bucket WHERE id=1").fetchone()
                current = self.clock()
                tokens = min(2, tokens + max(0, current - at) * 5)
                acquired = tokens >= 1
                db.execute("UPDATE bucket SET tokens=?, at=? WHERE id=1",
                           (tokens - 1 if acquired else tokens, current))
            if acquired:
                return
            self.sleep((1 - tokens) / 5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public collector does not follow redirects")


class PublicClient:
    ROUTES = {
        "markets": "https://gamma-api.polymarket.com/markets",
        "resolutions": "https://data-api.polymarket.com/v2/resolutions",
        "prices": "https://data-api.polymarket.com/v2/prices-history",
    }

    def __init__(self, *, bucket=None, opener=None, sleep=time.sleep):
        self.bucket = bucket or TokenBucket()
        # Never inherit HTTP(S)_PROXY or system proxy settings.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sleep = sleep

    def get(self, route, **params):
        url = self.ROUTES[route] + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            self.bucket.acquire()
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                              "User-Agent": "arb-offline-eval/0"})
                with self.opener.open(request, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"Public {route} request failed (HTTP {exc.code})") from None
                retry_after = exc.headers.get("Retry-After", "0")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 0
                if not 0 <= wait <= 30:
                    # Do not violate a longer server cooldown by retrying early.
                    raise RuntimeError("Public API requested a cooldown above the retry budget") from None
                self.sleep(max(2 ** attempt, wait))
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise RuntimeError(f"Public {route} request timed out/unreachable after 4 attempts") from None
                self.sleep(2 ** attempt)


def array(value):
    return json.loads(value) if isinstance(value, str) else value


def normalize(gamma, resolution, prices, retrieved_at):
    """Keep post-resolution truth separate from candidate historical inputs."""
    labels, tokens = array(gamma["outcomes"]), array(gamma["clobTokenIds"])
    payouts = resolution["payouts"]
    if labels != ["Yes", "No"] or len(tokens) != 2:
        raise ValueError("Only ordered YES/NO markets are supported")
    if resolution["condition_id"].lower() != gamma["conditionId"].lower():
        raise ValueError("Resolution does not match market")
    if resolution["status"].upper() != "RESOLVED" or payouts not in ([1, 0], [0, 1]):
        raise ValueError("Require settled, binary, nonvoid payout")
    resolved_at = resolution["resolved_at"]
    timestamp(resolved_at)
    history = []
    for point in prices:
        # V2 may synthesize a settlement point at a historical bucket edge.
        # Its timestamp alone is NOT enough to prevent outcome leakage.
        if point["resolution_seconds"] <= 0:
            continue
        at = datetime.fromtimestamp(point["timestamp"], timezone.utc).isoformat()
        history.append({"timestamp": at, "available_at": at,
                        "p": probability(point["price"]), "kind": "trade",
                        "retrieved_at": retrieved_at,
                        "resolution_seconds": point["resolution_seconds"]})
    if not history:
        raise ValueError("No nonsettlement historical prices")
    return {"id": str(gamma["id"]), "source": "polymarket-public-v2",
            "condition_id": gamma["conditionId"], "yes_token_id": tokens[0],
            "created_at": gamma["createdAt"], "resolved_at": resolved_at,
            "y": int(payouts[0]), "retrieved_at": retrieved_at,
            "metadata_snapshots": [{"question": gamma["question"], "rules": gamma["description"],
                                    "available_at": retrieved_at, "captured_at": retrieved_at}],
            "prices": history, "evidence": [],
            "eligibility": "needs independently archived pre-forecast metadata",
            "price_availability_assumption": "trade timestamps, not original ingestion receipts"}


def collect(client, *, count=50, max_pages=10):
    if not 1 <= count <= 1000 or not 1 <= max_pages <= 100:
        raise ValueError("Collection limits outside supported bounds")
    markets, excluded, seen = [], [], set()
    for page in range(max_pages):
        batch = client.get("markets", closed="true", limit=100, offset=page * 100,
                           order="closedTime", ascending="false")
        if not batch:
            break
        for gamma in batch:
            market_id = str(gamma["id"])
            if market_id in seen:
                continue
            seen.add(market_id)
            try:
                if array(gamma.get("outcomes", [])) != ["Yes", "No"]:
                    raise ValueError("Not an ordered YES/NO market")
                if not gamma.get("closed") or gamma.get("umaResolutionStatus") != "resolved":
                    raise ValueError("Not marked resolved by Gamma")
                resolution_rows = client.get("resolutions", condition=gamma["conditionId"])["data"]
                if len(resolution_rows) != 1:
                    raise ValueError("Missing unique on-chain resolution")
                points, cursor, cursors = [], None, set()
                for _ in range(100):
                    params = {"token_id": array(gamma["clobTokenIds"])[0], "interval": "max"}
                    if cursor:
                        params["cursor"] = cursor
                    response = client.get("prices", **params)
                    points.extend(response["data"])
                    pagination = response["pagination"]
                    if not pagination["has_more"]:
                        break
                    cursor = pagination.get("next_cursor")
                    if not cursor or cursor in cursors:
                        raise ValueError("Invalid/repeated price history cursor")
                    cursors.add(cursor)
                else:
                    raise ValueError("Price pagination exceeded 100 pages")
                markets.append(normalize(gamma, resolution_rows[0], points, now()))
            except (KeyError, ValueError, TypeError) as exc:
                excluded.append({"market_id": market_id, "reason": str(exc)})
            if len(markets) == count:
                return {"markets": markets, "excluded": excluded, "requested": count}
    return {"markets": markets, "excluded": excluded, "requested": count}
