"""Read-only compliance boundary. There is deliberately no trading allow path."""

import inspect
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from fastapi import HTTPException, Request as APIRequest

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TokenBucket:
    """Shared per-process budget; future public API clients should reuse this bucket."""

    def __init__(self, capacity=900, rate=810, *, clock=time.monotonic, sleep=time.sleep):
        # Burst + ten seconds of refill stays within the 9,000-request budget.
        self.capacity, self.rate = capacity, rate
        self.clock, self.sleep = clock, sleep
        self.tokens, self.updated = capacity, clock()
        self.lock = Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            self.sleep(delay)


PUBLIC_API_BUDGET = TokenBucket()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_geoblock(timeout: float) -> dict:
    # Ignore proxy environment variables and never follow an endpoint redirect.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(GEOBLOCK_URL, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("unexpected_status")
        body = response.read(16_385)
        if len(body) > 16_384:
            raise ValueError("oversized_response")
        return json.loads(body)


@dataclass(frozen=True)
class GeoblockResult:
    blocked: bool
    country: str | None
    region: str | None
    checked_at: str
    error: str | None = None
    cached: bool = False


class GeoblockClient:
    def __init__(self, *, fetch=None, ttl=60.0, timeout=2.0,
                 clock=time.monotonic, sleep=time.sleep, budget=PUBLIC_API_BUDGET):
        if not 0 < ttl <= 300 or not 0 < timeout <= 10:
            raise ValueError("geoblock TTL/timeout outside supported bounds")
        self.fetch = fetch or _fetch_geoblock
        self.ttl, self.timeout = ttl, timeout
        self.clock, self.sleep, self.budget = clock, sleep, budget
        self.lock = Lock()
        self.result = None
        self.expires_at = 0.0

    def check(self) -> GeoblockResult:
        # Serialize refreshes so concurrent callers do not stampede the endpoint.
        with self.lock:
            if self.result is not None and self.clock() < self.expires_at:
                return replace(self.result, cached=True)
            result = None
            for attempt in range(3):
                try:
                    self.budget.acquire()
                    payload = self.fetch(self.timeout)
                    if (not isinstance(payload, dict)
                            or type(payload.get("blocked")) is not bool
                            or not isinstance(payload.get("country"), str)
                            or len(payload["country"]) != 2
                            or not payload["country"].isascii()
                            or not payload["country"].isalpha()
                            or not isinstance(payload.get("region"), str)
                            or len(payload["region"]) > 32):
                        raise ValueError("invalid_response")
                    result = GeoblockResult(payload["blocked"], payload["country"],
                                            payload["region"], utc_now())
                    break
                except HTTPError as exc:
                    retry = exc.code == 429 or 500 <= exc.code < 600
                    exc.close()
                    error = "http_error"
                except (ValueError, TypeError, UnicodeError):
                    retry, error = False, "invalid_response"
                except Exception:
                    # Never expose raw transport errors, IPs, URLs or environment details.
                    retry, error = True, "check_failed"
                if not retry or attempt == 2:
                    result = GeoblockResult(True, None, None, utc_now(), error)
                    break
                self.sleep(0.25 * 2 ** attempt)
            self.result = result
            self.expires_at = self.clock() + self.ttl
            return result


@dataclass(frozen=True)
class ComplianceSettings:
    trading_enabled: bool = False
    audit_path: str = ".state/compliance.sqlite3"

    @classmethod
    def from_env(cls):
        return cls(trading_enabled=os.environ.get("TRADING_ENABLED", "false").lower() == "true",
                   audit_path=os.environ.get("ARB_COMPLIANCE_AUDIT_DB", ".state/compliance.sqlite3"))


class ComplianceDenied(PermissionError):
    def __init__(self, decision: dict):
        self.decision = decision
        super().__init__(f"Action denied by compliance guard: {decision['reason']}")


class AuditLog:
    def __init__(self, path: str):
        self.path = path

    def record(self, decision: dict):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS compliance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
                    caller TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL
                )""")
                db.execute("INSERT INTO compliance_audit(recorded_at, caller, action, decision) "
                           "VALUES (?, ?, ?, ?)", (decision["recorded_at"], decision["caller"],
                                                    decision["action"], json.dumps(decision)))
        finally:
            db.close()


class ComplianceGuard:
    def __init__(self, settings=None, *, geoblock=None, audit=None):
        self.settings = settings or ComplianceSettings.from_env()
        self.geoblock = geoblock or GeoblockClient()
        self.audit = audit or AuditLog(self.settings.audit_path)

    def status(self) -> dict:
        return {"trading_enabled": self.settings.trading_enabled, "trading_allowed": False,
                "allowlist_configured": False, "mode": "read_only",
                "geoblock": asdict(self.geoblock.check())}

    def require(self, action: str, *, caller: str):
        # Action/caller are static integration labels, never request payloads or secrets.
        geo = self.geoblock.check()
        if geo.error:
            reason = "geoblock_unavailable"
        elif geo.blocked:
            reason = "geoblocked"
        elif not self.settings.trading_enabled:
            reason = "trading_disabled"
        else:
            reason = "allowlist_not_implemented"
        decision = {"recorded_at": utc_now(), "caller": caller, "action": action,
                    "allowed": False, "reason": reason, "geoblock": asdict(geo),
                    "trading_enabled": self.settings.trading_enabled,
                    "allowlist_configured": False}
        try:
            self.audit.record(decision)
        except Exception:
            # Broken persistence must never execute the protected operation either.
            decision["reason"] = "audit_unavailable"
            raise ComplianceDenied(decision) from None
        raise ComplianceDenied(decision)


@lru_cache(maxsize=1)
def get_guard() -> ComplianceGuard:
    """Process singleton; read environment at first use, restart after config changes."""
    return ComplianceGuard()


def guarded(action: str, *, caller: str, guard=None):
    """One-line strategy integration, preserving sync/async function signatures."""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapped(*args, **kwargs):
                # Keep blocking network/audit I/O off an async strategy's event loop.
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool((guard or get_guard()).require, action, caller=caller)
                return await function(*args, **kwargs)
            return async_wrapped

        @wraps(function)
        def wrapped(*args, **kwargs):
            (guard or get_guard()).require(action, caller=caller)
            return function(*args, **kwargs)
        return wrapped
    return decorate


def compliance_dependency(action: str, *, caller: str):
    """FastAPI runs this synchronous dependency in its worker thread pool."""
    def dependency(request: APIRequest):
        guard = getattr(request.app.state, "compliance", None) or get_guard()
        try:
            guard.require(action, caller=caller)
        except ComplianceDenied as exc:
            raise HTTPException(status_code=403, detail={"code": "compliance_denied",
                                                       "reason": exc.decision["reason"]}) from None
    return dependency
