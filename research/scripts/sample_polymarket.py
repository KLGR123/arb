"""Explicit public Gamma sampling, direct HTTPS only; never loads a trading SDK."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx

from validate_polymarket import coverage


class TokenBucket:
    """One shared bucket for every host/retry, deliberately below venue limits."""

    def __init__(self, rate=2.0, capacity=1.0):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.updated = capacity, time.monotonic()

    def acquire(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            time.sleep((1 - self.tokens) / self.rate)
            self.updated = time.monotonic()
            self.tokens = 1
        self.tokens -= 1


BUCKET = TokenBucket()


def get_json(client, url, params=None, sleep=time.sleep):
    for attempt in range(3):
        BUCKET.acquire()
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                raise
            sleep(2 ** attempt)
    raise AssertionError("unreachable")


def collect(client, pages):
    result = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "selection": "Gamma active=true closed=false, id descending, offset pages of 100",
              "geoblock": None, "geoblock_error": None, "markets": [],
              "collection_status": "complete", "collection_error": None}
    try:
        result["geoblock"] = get_json(client, "https://polymarket.com/api/geoblock")
    except (httpx.HTTPError, ValueError) as error:
        # Location unknown: record it. This command performs public reads only.
        result["geoblock_error"] = str(error)
    for page in range(pages):
        try:
            rows = get_json(client, "https://gamma-api.polymarket.com/markets", {
                "active": "true", "closed": "false", "order": "id", "ascending": "false",
                "limit": 100, "offset": page * 100,
            })
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("id") for row in rows):
                raise ValueError("Expected Gamma markets array with market IDs")
        except (httpx.HTTPError, ValueError) as error:
            result["collection_status"] = "partial" if result["markets"] else "failed"
            result["collection_error"] = f"{type(error).__name__}: {error}"
            break
        for row in rows:
            result["markets"].append({key: row.get(key) for key in (
                "id", "slug", "active", "closed", "version", "updatedAt",
            )})
        if len(rows) < 100:
            break
    result["coverage"] = coverage(result["markets"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Ignore proxy environment variables and do not follow redirects to another host.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
        result = collect(client, args.pages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["coverage"]))
    if result["collection_status"] != "complete":
        raise SystemExit(f"Sampling {result['collection_status']}: {result['collection_error']}")


if __name__ == "__main__":
    main()
