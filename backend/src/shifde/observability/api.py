"""Unauthenticated liveness and readiness probes."""

from __future__ import annotations

import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any  # Any: JSON stats document

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from shifde import __version__
from shifde.core.db import Database
from shifde.core.deps import SettingsDep
from shifde.core.errors import Forbidden, Unauthenticated
from shifde.core.health import database_reachable, migration_state
from shifde.observability import metrics

router = APIRouter(prefix="/api", tags=["health"])


class HealthOut(BaseModel):
    status: str
    version: str


class ReadyOut(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/health")
def health() -> HealthOut:
    """Liveness: the process is up. Does not touch the database."""
    return HealthOut(status="ok", version=__version__)


READY_DEADLINE_SECONDS = 3.0
_probe_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ready-probe")


def _probe(db: Database) -> dict[str, str]:
    checks = {"database": "error", "migrations": "unknown"}
    with db.session() as session:
        if database_reachable(session):
            checks["database"] = "ok"
            checks["migrations"] = migration_state(session).status
    return checks


@router.get("/ready", response_model=ReadyOut, responses={503: {"model": ReadyOut}})
def ready(request: Request) -> JSONResponse:
    """Readiness: database reachable and its migration revision equals the code head. The probe
    runs under a deadline, so a database that is down answers 503 well before the connect timeout
    (audit Item 18, B-2516)."""
    probe = _probe_pool.submit(_probe, request.app.state.db)
    try:
        checks = probe.result(timeout=READY_DEADLINE_SECONDS)
    except TimeoutError:
        probe.cancel()  # a queued probe never starts; a running one ends at the connect timeout
        checks = {"database": "error", "migrations": "unknown"}
    ok = checks["database"] == "ok" and checks["migrations"] == "ok"
    body = ReadyOut(status="ready" if ok else "not_ready", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)


# ---------------------------------------------------------------- metrics
log = structlog.get_logger("shifde.metrics")
OPS_WINDOW_SECONDS = 300
_LOOPBACK = {"127.0.0.1", "::1"}
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")
_WEB_PROXY_HEADER = "x-shifde-via"  # injected by the Next.js web proxy on every proxied request
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None  # Any: ops stats document


def _authorize_metrics(request: Request, settings: SettingsDep) -> None:
    """`Authorization: Bearer <SHIFDE_METRICS_TOKEN>` when a token is configured. Without a token
    only loopback clients, outside production, and never a request that came through a proxy: a
    same-host reverse proxy makes every client look like loopback, so forwarding headers without
    configured trusted proxies (which restore the real peer address) are refused."""
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
    if token:
        header = request.headers.get("authorization", "")
        presented = header[7:] if header[:7].lower() == "bearer " else ""
        if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
            raise Unauthenticated("a valid metrics bearer token is required")
        return
    if settings.env == "production":
        raise Forbidden("metrics need SHIFDE_METRICS_TOKEN in production")
    if _WEB_PROXY_HEADER in request.headers:  # the web proxy never restores the peer address
        raise Forbidden("metrics are not available through the web proxy without a bearer token")
    if not settings.trusted_proxies and any(h in request.headers for h in _FORWARD_HEADERS):
        raise Forbidden(
            "metrics are not available through a proxy without SHIFDE_METRICS_TOKEN "
            "(or SHIFDE_TRUSTED_PROXIES)"
        )
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK:
        raise Forbidden("metrics are available to loopback clients only (set SHIFDE_METRICS_TOKEN)")


def ops_stats(db: Database) -> dict[str, Any]:  # Any: ops stats document
    """Aggregate-only operational counters across tenants (cached for 5 s per process)."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache[0] < 5.0:
            return _cache[1]
    with db.session() as session:
        raw = session.execute(
            text("SELECT shifde.shifde_ops_stats(:w)"), {"w": OPS_WINDOW_SECONDS}
        ).scalar_one()
    stats = dict(raw) if isinstance(raw, dict) else dict(json.loads(raw))
    with _cache_lock:
        _cache = (now, stats)
    return stats


@router.get("/metrics")
def prometheus_metrics(request: Request, settings: SettingsDep) -> Response:
    """Prometheus text exposition (version 0.0.4): API process instruments + database-derived
    operational gauges. Protected by `SHIFDE_METRICS_TOKEN` or loopback-only."""
    _authorize_metrics(request, settings)
    body = metrics.registry.render()
    try:
        body += metrics.render_ops(ops_stats(request.app.state.db))
    except SQLAlchemyError:
        log.warning("metrics.ops_stats_unavailable", exc_info=True)
        body += "# shifde_ops_stats unavailable: database error\n"
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")
