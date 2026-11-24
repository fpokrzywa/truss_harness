"""The additive rate-limit changes of SP8-2 (design 2026-11-23 section 10 items 15 and 16): a
per-call `limit` on `SlidingWindowLimiter.check`, and `RateLimited.retry_after` rendered as a
`Retry-After` header. Callers that pass neither are unchanged."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shifde.core.errors import RateLimited
from shifde.core.problem import install_error_handlers
from shifde.core.ratelimit import SlidingWindowLimiter


def test_a_per_call_limit_overrides_the_instance_limit() -> None:
    limiter = SlidingWindowLimiter(10, clock=lambda: 100.0)
    limiter.check("k", limit=2)
    limiter.check("k", limit=2)
    with pytest.raises(RateLimited):
        limiter.check("k", limit=2)
    limiter.check("k")  # the instance limit (10) still admits the key: 3 hits recorded
    with pytest.raises(RateLimited):
        limiter.check("k", limit=3)


def test_a_per_call_limit_of_zero_disables_the_check() -> None:
    limiter = SlidingWindowLimiter(1, clock=lambda: 100.0)
    for _ in range(5):
        limiter.check("k", limit=0)


def test_retry_after_is_rendered_only_when_set() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/later")
    def later() -> None:
        raise RateLimited("x", retry_after=7)

    @app.get("/plain")
    def plain() -> None:
        raise RateLimited("x")

    with TestClient(app) as c:
        later_r = c.get("/later")
        plain_r = c.get("/plain")
    assert later_r.status_code == 429 and later_r.headers["Retry-After"] == "7"
    assert later_r.json()["code"] == "rate_limited"
    assert plain_r.status_code == 429 and "retry-after" not in plain_r.headers
    assert RateLimited("x").retry_after is None
