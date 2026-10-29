"""Explicit, resumable data refresh. Query handlers never perform network I/O."""

import time

from .client import PublicDataClient, UpstreamError
from .models import SyncResult
from .store import DataStore

BUCKET = 43200  # Retained whole-life tier, also stable across incremental windows.
WINDOW = 15 * 86400


def sync_event(client: PublicDataClient, store: DataStore, selector: str, *, now: int | None = None) -> SyncResult:
    target = int(time.time()) if now is None else now
    count, empty = 0, []
    with store.writer():
        compliance = client.guard.status()
        # The shared guard always denies trading. Geoblocked/unknown regions
        # may still fetch public data; no protected operation is invoked here.
        if compliance["trading_allowed"]:
            raise RuntimeError("Public data sync requires read-only compliance mode")
        event = client.event(selector)
        store.put_event(event)
        resolutions = client.resolutions(event)
        store.put_resolutions(event, resolutions)
        for market in event.markets:
            for token in market.tokens:
                state = store.checkpoint(token.id)
                seen = set()
                # Finish a persisted window even if this run's clock is earlier.
                while state["pending"] or state["through"] < target:
                    if state["pending"] is None:
                        through = state["through"]
                        if through == 0:
                            params = {"interval": "max", "bucket_seconds": BUCKET}
                            until = target
                        else:
                            start = max(1, through-BUCKET*2)
                            until = min(target, start+WINDOW)
                            params = {"start": start, "end": until, "bucket_seconds": BUCKET}
                        state["pending"] = {"params": params, "until": until, "cursor": None}
                    pending = state["pending"]
                    cursor = pending["cursor"]
                    if cursor is not None and cursor in seen:
                        raise UpstreamError("Repeated price-history cursor; checkpoint preserved")
                    seen.add(cursor)
                    try:
                        page = client.prices(token.id, pending["params"], cursor)
                    except UpstreamError:
                        # First-page errors leave no checkpoint; the identical
                        # request is reconstructed. Later pages were committed.
                        raise
                    if page.next_cursor is not None and (page.next_cursor == cursor or page.next_cursor in seen):
                        raise UpstreamError("Repeated price-history cursor; checkpoint preserved")
                    count += len(page.data)
                    if page.next_cursor:
                        pending["cursor"] = page.next_cursor
                    else:
                        state["through"] = pending["until"]
                        state["pending"] = None
                        seen.clear()
                    store.commit_page(market.id, token.id, page, state)
                # Empty series is an explicit coverage gap, never a zero price.
                if not store.history(event, market_id=market.id, token_id=token.id, limit=1).data:
                    empty.append(token.id)
        missing = store.resolutions(event).missing_market_ids
        return SyncResult(compliance=compliance, event_id=event.id, markets=len(event.markets),
                          tokens=sum(len(m.tokens) for m in event.markets), points_received=count,
                          resolutions=len(resolutions), missing_resolution_market_ids=missing,
                          empty_token_ids=empty)
