"""Audit Item 5b (REQ F-15), core part: a global request-body cap and a reusable rate-limit
primitive (identity later applies the limiter to API-key failures and the OIDC callback)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from shifde.core.bodylimit import MaxBodySizeMiddleware
from shifde.core.errors import RateLimited
from shifde.core.ratelimit import SlidingWindowLimiter
from shifde.core.settings import Settings


def _client(limit: int) -> TestClient:
    app = FastAPI()
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=limit)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    return TestClient(app)


def test_body_within_the_cap_passes() -> None:
    assert _client(100).post("/echo", content=b"x" * 100).json() == {"n": 100}


def test_declared_oversize_body_is_413_problem_json() -> None:
    r = _client(100).post("/echo", content=b"x" * 101)
    assert r.status_code == 413
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "payload_too_large"


def test_streamed_oversize_body_without_content_length_is_413() -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b"x" * 30

    r = _client(100).post("/echo", content=chunks())
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"


def test_zero_disables_the_cap() -> None:
    assert _client(0).post("/echo", content=b"x" * 5000).json() == {"n": 5000}


def test_default_setting_is_bounded_and_positive() -> None:
    kwargs: dict[str, Any] = {"_env_file": None, "database_url": "postgresql+psycopg://x"}
    s = Settings(env="test", **kwargs)
    assert 0 < s.max_request_body_bytes <= 16 * 1024 * 1024


def test_limiter_message_is_configurable_and_default_unchanged() -> None:
    lim = SlidingWindowLimiter(1, message="too many callbacks")
    lim.check("k")
    with pytest.raises(RateLimited, match="too many callbacks"):
        lim.check("k")
    other = SlidingWindowLimiter(1)
    other.check("k")
    with pytest.raises(RateLimited, match="too many login attempts"):
        other.check("k")
