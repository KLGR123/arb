"""Local research API. Run via uvicorn; workers run as separate processes."""

import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .compliance import ComplianceGuard
from .contracts import CreateBacktest
from .datasets import catalog, snapshot
from .store import Store


def create_app(db_path: str | None = None, *, compliance: ComplianceGuard | None = None) -> FastAPI:
    app = FastAPI(title="arb offline backtests", version="1.0.0")
    store = Store(db_path or os.environ.get("ARB_BACKTEST_DB", ".state/backtests.sqlite3"))
    app.state.store = store
    app.state.compliance = compliance or ComplianceGuard()

    @app.get("/compliance/status")
    def compliance_status():
        return app.state.compliance.status()

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError):
        # Non-finite JSON values must yield 422, without re-serializing NaN/Infinity.
        errors = [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Unknown run id"})

    @app.get("/datasets")
    def datasets():
        return {"datasets": catalog()}

    @app.post("/backtests", status_code=202)
    def create(request: CreateBacktest):
        try:
            dataset = snapshot(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_id = store.create(request, dataset)
        return JSONResponse(status_code=202, content=store.get(run_id),
                            headers={"Location": f"/backtests/{run_id}"})

    @app.get("/backtests/{run_id}")
    def get(run_id: str):
        return store.get(run_id)

    @app.post("/backtests/{run_id}/realistic", status_code=202)
    def promote(run_id: str):
        store.promote(run_id)
        return store.get(run_id)

    @app.get("/backtests/{run_id}/events")
    def events(run_id: str, after: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=1000)] = 200):
        return store.events(run_id, after, limit)

    @app.get("/backtests/{run_id}/bars")
    def bars(run_id: str):
        return store.bars(run_id)

    return app
