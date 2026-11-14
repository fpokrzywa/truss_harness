"""Distributed tracing without a vendor SDK (FR-OBS-003, NFR-OPS-002).

W3C `traceparent` propagation API request -> job -> run -> step -> adapter call: the request span
fixes the trace id; the run stores it (`runs.trace_id`) and its job carries a `traceparent`, so the
worker continues the SAME trace. Spans are kept in an in-process ring (tests, debugging) and handed
to the configured exporter:

* `none`    - record in-process only (default);
* `console` - one JSON line per span on stdout;
* `otlp`    - OTLP/HTTP JSON (`POST <endpoint>/v1/traces`) through a bounded background queue;
              failures are logged and dropped, so telemetry can never block or fail a request.

Spans NEVER carry prompt / completion / tool-argument content (FR-OBS-004): attribute keys that
name content are dropped, values are secret-scrubbed and truncated. Content capture is a run
artifact concern (`capture_content`), not a telemetry one.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol  # Any: attribute values (str | int | float | bool)
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from shifde.core.context import bind_log_context
from shifde.core.redaction import REDACTED, is_sensitive_key, scrub_text

log = structlog.get_logger("shifde.tracing")

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
# attribute names that would carry business content: never recorded
_CONTENT_KEYS = re.compile(
    r"(^|\.)(prompt|completion|messages?|content|input|output|arguments?|body|response|"
    r"request|text|query|rows?|payload)$",
    re.I,
)
MAX_ATTR = 200
RING_SIZE = 2048


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_ns: int
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)  # Any: scalar attribute values
    status: str = "ok"

    def set(self, key: str, value: Any) -> None:  # Any: scalar attribute value
        if _CONTENT_KEYS.search(key):
            return
        if isinstance(value, bool | int | float):
            self.attributes[key] = value  # numbers carry no secrets (e.g. `input_tokens`)
        elif is_sensitive_key(key):
            self.attributes[key] = REDACTED
        elif isinstance(value, str):
            self.attributes[key] = scrub_text(value)[:MAX_ATTR]
        elif value is not None:
            self.attributes[key] = scrub_text(str(value))[:MAX_ATTR]

    @property
    def duration_ms(self) -> float:
        return ((self.end_ns or time.time_ns()) - self.start_ns) / 1e6

    def traceparent(self) -> str:
        return format_traceparent(self.trace_id, self.span_id)


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def format_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """(trace_id, parent span id) of a valid header, else None."""
    m = _TRACEPARENT.match((value or "").strip().lower())
    if m is None or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return m.group(1), m.group(2)


_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("shifde_span", default=None)


def current_span() -> Span | None:
    return _current.get()


def current_trace_id() -> str | None:
    span = _current.get()
    return span.trace_id if span else None


def current_span_id() -> str | None:
    span = _current.get()
    return span.span_id if span else None


class Exporter(Protocol):
    def export(self, spans: list[Span]) -> None: ...
    def flush(self, timeout: float = 5.0) -> None: ...


class NoneExporter:
    def export(self, spans: list[Span]) -> None:
        return None

    def flush(self, timeout: float = 5.0) -> None:
        return None


def span_json(span: Span, service: str) -> dict[str, Any]:  # Any: JSON document
    return {
        "name": span.name,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "start_unix_nano": span.start_ns,
        "duration_ms": round(span.duration_ms, 3),
        "status": span.status,
        "service": service,
        "attributes": span.attributes,
    }


class ConsoleExporter:
    def __init__(self, service: str) -> None:
        self.service = service

    def export(self, spans: list[Span]) -> None:
        for span in spans:
            sys.stdout.write(json.dumps(span_json(span, self.service), default=str) + "\n")
        sys.stdout.flush()

    def flush(self, timeout: float = 5.0) -> None:
        sys.stdout.flush()


def _otlp_value(v: Any) -> dict[str, Any]:  # Any: scalar attribute value
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def otlp_payload(spans: list[Span], service: str) -> dict[str, Any]:  # Any: OTLP JSON document
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "shifde"},
                        "spans": [
                            {
                                "traceId": s.trace_id,
                                "spanId": s.span_id,
                                **({"parentSpanId": s.parent_span_id} if s.parent_span_id else {}),
                                "name": s.name,
                                "kind": 1,
                                "startTimeUnixNano": str(s.start_ns),
                                "endTimeUnixNano": str(s.end_ns or s.start_ns),
                                "attributes": [
                                    {"key": k, "value": _otlp_value(v)}
                                    for k, v in s.attributes.items()
                                ],
                                "status": {"code": 1 if s.status == "ok" else 2},
                            }
                            for s in spans
                        ],
                    }
                ],
            }
        ]
    }


class OtlpHttpExporter:
    """OTLP/HTTP JSON traces through a bounded queue and one daemon thread."""

    def __init__(
        self,
        endpoint: str,
        service: str,
        *,
        interval: float = 1.0,
        batch: int = 128,
        queue_size: int = 4096,
        timeout: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(endpoint)  # the path gets `/v1/traces`; a query / fragment stays after it
        self.url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/") + "/v1/traces", parts.query, "")
        )
        self.service = service
        self.batch = batch
        self.interval = interval
        self._queue: deque[Span] = deque(maxlen=queue_size)  # full queue drops the oldest span
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self.exported = 0
        self.failed = 0
        self._thread = threading.Thread(target=self._loop, daemon=True, name="otlp-exporter")
        self._thread.start()

    def export(self, spans: list[Span]) -> None:
        with self._lock:
            self._queue.extend(spans)
            size = len(self._queue)
        if size >= self.batch:
            self._wake.set()

    def _drain(self) -> list[Span]:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    def _send(self, spans: list[Span]) -> None:
        for i in range(0, len(spans), self.batch):
            chunk = spans[i : i + self.batch]
            try:
                resp = self._client.post(self.url, json=otlp_payload(chunk, self.service))
                resp.raise_for_status()
                self.exported += len(chunk)
            except httpx.HTTPError as exc:
                self.failed += len(chunk)
                log.warning("tracing.otlp_export_failed", error=type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            spans = self._drain()
            if spans:
                self._send(spans)

    def flush(self, timeout: float = 5.0) -> None:
        spans = self._drain()
        if spans:
            self._send(spans)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self.flush()
        self._client.close()


class Tracer:
    def __init__(self) -> None:
        self.service = "shifde"
        self.mode = "none"
        self.exporter: Exporter = NoneExporter()
        self._ring: deque[Span] = deque(maxlen=RING_SIZE)
        self._lock = threading.Lock()

    def configure(
        self,
        mode: str,
        *,
        service: str = "shifde",
        endpoint: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        """Select the exporter; returns the effective mode (`otlp` without an endpoint falls
        back to `none`, which `status()` reports)."""
        self.service = service
        old = self.exporter
        if mode == "console":
            self.exporter, self.mode = ConsoleExporter(service), "console"
        elif mode == "otlp" and endpoint:
            self.exporter = OtlpHttpExporter(endpoint, service, transport=transport)
            self.mode = "otlp"
        else:
            self.exporter, self.mode = NoneExporter(), "none"
        if isinstance(old, OtlpHttpExporter):
            old.close()
        return self.mode

    def finish(self, span: Span) -> None:
        span.end_ns = time.time_ns()
        with self._lock:
            self._ring.append(span)
        try:
            self.exporter.export([span])
        except Exception:  # noqa: BLE001 - an exporter bug must never fail a request
            log.warning("tracing.export_failed", exc_info=True)

    def spans(self, trace_id: str | None = None) -> list[Span]:
        with self._lock:
            items = list(self._ring)
        return [s for s in items if trace_id is None or s.trace_id == trace_id]

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()

    def status(self) -> dict[str, Any]:  # Any: JSON document
        exp = self.exporter
        return {
            "exporter": self.mode,
            "service": self.service,
            "otlp": (
                {"endpoint": _safe_url(exp.url), "exported": exp.exported, "failed": exp.failed}
                if isinstance(exp, OtlpHttpExporter)
                else None
            ),
            "buffered_spans": len(self._ring),
        }


def _safe_url(url: str) -> str:
    """The endpoint for display: no credentials (userinfo) and no query / fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return "(invalid endpoint)"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


tracer = Tracer()


@contextmanager
def start_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,  # Any: scalar attribute values
    *,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    bind_logs: bool = False,
) -> Iterator[Span]:
    """Open a span (child of the current one unless a trace is given explicitly)."""
    parent = _current.get()
    if trace_id is None:
        trace_id = parent.trace_id if parent else new_trace_id()
        parent_span_id = parent_span_id or (parent.span_id if parent else None)
    span = Span(name, trace_id, new_span_id(), parent_span_id, time.time_ns())
    for k, v in (attributes or {}).items():
        span.set(k, v)
    token = _current.set(span)
    if bind_logs:
        bind_log_context(trace_id=span.trace_id, span_id=span.span_id)
    try:
        yield span
    except BaseException as exc:
        span.status = "error"
        span.set("error.type", type(exc).__name__)
        raise
    finally:
        _current.reset(token)
        tracer.finish(span)


def configure_from_settings(settings: Any, service: str) -> str:  # Any: Settings (core)
    mode = getattr(settings, "otel_exporter", "none")
    endpoint = getattr(settings, "otel_otlp_endpoint", None) or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    return tracer.configure(mode, service=service, endpoint=endpoint)
