"""SETH 3ba0e06 `tests/test_p5_core.py` pure helpers: the safe expression language and the
carried egress deadline items (assertions unchanged; no database)."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from shifde.core import safeexpr
from shifde.core.egress import EgressGuard, EgressViolation, installation_layer


@pytest.mark.parametrize(
    "expr,ctx,expected",
    [
        ("input.n > 3", {"input": {"n": 5}}, True),
        ("input.n > 3 and not input.skip", {"input": {"n": 5, "skip": False}}, True),
        (
            "nodes.a.output.label == 'ok' || false",
            {"nodes": {"a": {"output": {"label": "ok"}}}},
            True,
        ),
        ("'x' in input.tags", {"input": {"tags": ["x", "y"]}}, True),
        ("input.items[1].k == 2", {"input": {"items": [{"k": 1}, {"k": 2}]}}, True),
        ("input.missing.deep == null", {"input": {}}, True),
        ("input.n < 'a'", {"input": {"n": 1}}, False),  # incomparable -> False, never an error
        ("1 == true", {}, False),
        ("(1 + 2) == 3", None, None),
        ("input.__class__", None, None),
        ("len(input)", None, None),
        ("input.a.b.c(", None, None),
        ("x" * 2000, None, None),
        ("(" * 40 + "1" + ")" * 40, None, None),
        ("", None, None),
    ],
)
def test_safe_expression_language(expr: str, ctx: dict[str, Any] | None, expected: Any) -> None:
    if ctx is None:
        with pytest.raises(safeexpr.ExprError):
            safeexpr.parse(expr)
    else:
        assert safeexpr.evaluate_condition(expr, ctx) is expected


# ================================================================== carried items
def _guard(resolver: Any, inner: httpx.BaseTransport | None = None) -> EgressGuard:
    return EgressGuard([installation_layer(None, env="test")], resolver=resolver, inner=inner)


def test_dns_resolution_cannot_outlive_the_call_deadline() -> None:
    def slow(_host: str) -> list[str]:
        time.sleep(3)
        return ["127.0.0.1"]

    guard = _guard(slow)
    guard.set_deadline(0.3)
    started = time.monotonic()
    with pytest.raises(EgressViolation) as exc:
        guard.resolve_pinned("slow.example", 80, "http")
    assert exc.value.code == "deadline" and time.monotonic() - started < 1.5
    unbounded = _guard(lambda _h: ["127.0.0.1"])
    assert unbounded.resolve_pinned("ok.example", 80, "http").ip == "127.0.0.1"


def test_every_http_step_timeout_is_capped_by_the_remaining_deadline() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json={"ok": True})

    guard = _guard(lambda _h: ["127.0.0.1"], httpx.MockTransport(handler))
    guard.set_deadline(2.0)
    with guard.client(timeout=httpx.Timeout(30.0)) as client:
        client.get("http://svc.example/a")
        time.sleep(1.0)  # the budget shrinks; the NEXT request must see only what is left
        client.get("http://svc.example/b")
    first, second = seen
    assert all(v is not None and v <= 2.0 for v in first.values())
    assert all(v is not None and v <= 1.2 for v in second.values()), second
    assert set(first) == {"connect", "read", "write", "pool"}
