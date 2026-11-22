"""Pure-ASGI request-id + access-log middleware."""

from __future__ import annotations

import re
import time

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shifde.core.context import (
    WORKER_REQUEST_ID_PREFIXES,
    current_log_context,
    init_log_context,
    reset_log_context,
)
from shifde.core.ids import uuid7
from shifde.observability import metrics, tracing

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,128}$")
log = structlog.get_logger("shifde.access")


class RequestIdMiddleware:
    """Echo a valid inbound X-Request-ID (else generate one; an id with a worker-reserved prefix
    counts as invalid, #134), expose it as
    `scope['state']['request_id']`, and log one structured line per request. Query strings, headers,
    cookies and bodies are never logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_in = Headers(scope=scope)
        inbound = headers_in.get(REQUEST_ID_HEADER)
        valid = (
            inbound is not None
            and _VALID_REQUEST_ID.match(inbound) is not None
            and not inbound.startswith(WORKER_REQUEST_ID_PREFIXES)
        )
        request_id = inbound if inbound is not None and valid else str(uuid7())
        scope.setdefault("state", {})["request_id"] = request_id
        token = init_log_context(request_id)
        remote = tracing.parse_traceparent(headers_in.get("traceparent"))
        status = 500
        started = time.perf_counter()
        method = scope["method"]

        with tracing.start_span(
            f"{method} {scope['path']}",
            {"http.method": method},
            trace_id=remote[0] if remote else tracing.new_trace_id(),
            parent_span_id=remote[1] if remote else None,
            bind_logs=True,
        ) as span:

            async def send_wrapper(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    headers = MutableHeaders(scope=message)
                    if REQUEST_ID_HEADER not in headers:
                        headers[REQUEST_ID_HEADER] = request_id
                    headers["traceparent"] = span.traceparent()
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route_obj = scope.get("route")
                route = str(getattr(route_obj, "path", None) or "unmatched")
                span.name = f"{method} {route}"
                span.set("http.route", route)
                span.set("http.status_code", status)
                if status >= 500:
                    span.status = "error"
                ctx = current_log_context()
                span.set("tenant_id", ctx.get("tenant_id"))
                seconds = time.perf_counter() - started
                metrics.record_request(method, route, status, seconds)
                log.info(
                    "request.completed",
                    method=method,
                    path=scope["path"],
                    status=status,
                    duration_ms=round(seconds * 1000, 2),
                    tenant_id=ctx.get("tenant_id"),
                    principal_id=ctx.get("principal_id"),
                )
                reset_log_context(token)
