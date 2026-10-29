"""Standalone local read-only API: uvicorn arb_research.polymarket.api:create_app --factory."""

import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from .models import Event, History, Market, ResolutionResult
from .store import DataStore


def create_app(data_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="arb Polymarket public data", version="1.0.0")
    store = DataStore(data_dir or os.environ.get("ARB_POLYMARKET_DATA_DIR", ".state/polymarket"))
    app.state.store = store

    def event(selector: str) -> Event:
        try:
            return store.event(selector)
        except KeyError as exc:
            raise HTTPException(404, "Event has not been synced") from exc

    def market_event(market_id: str) -> Event:
        try:
            return store.market_event(market_id)
        except KeyError as exc:
            raise HTTPException(404, "Market has not been synced") from exc

    @app.get("/events/{selector}", response_model=Event)
    def get_event(selector: str):
        return event(selector)

    @app.get("/markets/{market_id}", response_model=Market)
    def get_market(market_id: str):
        return next(m for m in market_event(market_id).markets if m.id == market_id)

    def history(e, market_id, token_id, start, end, offset, limit):
        if end < start:
            raise HTTPException(422, "end must be at or after start")
        return store.history(e, market_id=market_id, token_id=token_id, start=start,
                             end=end, offset=offset, limit=limit)

    @app.get("/events/{selector}/prices", response_model=History)
    def event_prices(selector: str, token_id: str | None = None,
                     start: Annotated[int, Query(gt=0)] = 1,
                     end: Annotated[int, Query(gt=0)] = 253402300799,
                     offset: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=10000)] = 1000):
        return history(event(selector), None, token_id, start, end, offset, limit)

    @app.get("/markets/{market_id}/prices", response_model=History)
    def market_prices(market_id: str, token_id: str | None = None,
                      start: Annotated[int, Query(gt=0)] = 1,
                      end: Annotated[int, Query(gt=0)] = 253402300799,
                      offset: Annotated[int, Query(ge=0)] = 0,
                      limit: Annotated[int, Query(ge=1, le=10000)] = 1000):
        return history(market_event(market_id), market_id, token_id, start, end, offset, limit)

    @app.get("/events/{selector}/resolutions", response_model=ResolutionResult)
    def event_resolutions(selector: str):
        return store.resolutions(event(selector))

    @app.get("/markets/{market_id}/resolutions", response_model=ResolutionResult)
    def market_resolutions(market_id: str):
        return store.resolutions(market_event(market_id), market_id)

    return app
