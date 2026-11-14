"""P8 observability, the tests that need no app fixtures: span attributes and
content scrubbing, traceparent parsing, the OTLP, unreachable-collector and console exporters.
Moved from SETH 3ba0e06 `tests/test_p8_observability.py` (assertions unchanged). The rest need
the app or the database and are deferred (see docs/build/carry_forward.md)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from shifde.observability import metrics, tracing
from tests._support.collector import CaptureServer

SECRET_PROMPT = "TOPSECRET-PROMPT-BODY-777"
SECRET_REPLY = "TOPSECRET-REPLY-888"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


@pytest.fixture(autouse=True)
def clean_telemetry() -> Iterator[None]:
    tracing.tracer.clear()
    tracing.tracer.configure("none")
    metrics.registry.reset()
    yield
    tracing.tracer.configure("none")


def _lines(out: str) -> list[dict[str, Any]]:
    found = []
    for line in out.splitlines():
        if line.startswith("{"):
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
    return found


def test_spans_drop_content_keys_scrub_secret_values_and_bound_their_size() -> None:
    with tracing.start_span(
        "unit",
        {
            "prompt": SECRET_PROMPT,
            "gen_ai.completion": SECRET_REPLY,
            "arguments": {"a": 1},
            "input_tokens": 12,
            "note": "Bearer abcdefghijklmnop",
            "api_key": "hunter2hunter2",
            "long": "x" * 5000,
            "ok": True,
        },
    ) as span:
        pass
    assert "prompt" not in span.attributes and "gen_ai.completion" not in span.attributes
    assert "arguments" not in span.attributes
    assert span.attributes["input_tokens"] == 12 and span.attributes["ok"] is True
    assert "abcdefghijklmnop" not in span.attributes["note"]
    assert span.attributes["api_key"] == "[REDACTED]"
    assert len(span.attributes["long"]) == tracing.MAX_ATTR


def test_traceparent_parsing_rejects_malformed_and_zero_ids() -> None:
    assert tracing.parse_traceparent(f"00-{TRACE}-{PARENT}-01") == (TRACE, PARENT)
    for bad in (
        None, "", "garbage", f"01-{TRACE}-{PARENT}-01", f"00-{'0' * 32}-{PARENT}-01",
        f"00-{TRACE}-{'0' * 16}-01", f"00-{TRACE[:-1]}-{PARENT}-01", f"00-{TRACE.upper()}x-{PARENT}-01",
    ):  # fmt: skip
        assert tracing.parse_traceparent(bad) is None


# ------------------------------------------------------------------ exporters
def test_the_otlp_exporter_posts_otlp_http_json_and_never_sends_content() -> None:
    with CaptureServer() as collector:
        assert (
            tracing.tracer.configure("otlp", service="shifde-test", endpoint=collector.url)
            == "otlp"
        )
        attrs = {"tenant_id": "t-1", "prompt": SECRET_PROMPT, "n": 3}
        with tracing.start_span("work", attrs), tracing.start_span("inner"):
            pass
        tracing.tracer.exporter.flush()
        status = tracing.tracer.status()
    assert status["exporter"] == "otlp" and status["otlp"]["exported"] == 2
    (path, payload) = collector.requests[0]
    assert path == "/v1/traces"
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"][0]["value"]["stringValue"] == "shifde-test"
    spans = {s["name"]: s for s in resource["scopeSpans"][0]["spans"]}
    assert set(spans) == {"work", "inner"}
    assert spans["inner"]["parentSpanId"] == spans["work"]["spanId"]
    assert (
        spans["inner"]["traceId"] == spans["work"]["traceId"] and len(spans["work"]["spanId"]) == 16
    )
    keys = {a["key"] for a in spans["work"]["attributes"]}
    assert keys == {"tenant_id", "n"}
    assert SECRET_PROMPT not in json.dumps(payload)


def test_an_unreachable_collector_never_breaks_the_caller_and_otlp_needs_an_endpoint() -> None:
    assert tracing.tracer.configure("otlp", endpoint=None) == "none"  # honest fallback
    assert tracing.tracer.status()["exporter"] == "none"
    tracing.tracer.configure("otlp", endpoint="http://127.0.0.1:9")  # nothing listens there
    with tracing.start_span("lost"):
        pass
    tracing.tracer.exporter.flush()
    status = tracing.tracer.status()
    assert status["otlp"]["failed"] == 1 and status["otlp"]["exported"] == 0


def test_the_console_exporter_writes_one_json_line_per_span(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracing.tracer.configure("console", service="svc")
    with tracing.start_span("console.span", {"n": 1}):
        pass
    line = next(r for r in _lines(capsys.readouterr().out) if r.get("name") == "console.span")
    assert line["service"] == "svc" and line["attributes"] == {"n": 1}
    assert len(line["trace_id"]) == 32 and line["status"] == "ok"
