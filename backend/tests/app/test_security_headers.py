"""SP4-2 (B-2772): the go-live security headers on every API response, through the real stack."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.fullapp import ORIGIN, World

CSP = "default-src 'none'; frame-ancestors 'none'"
BASE = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
}
HSTS = "Strict-Transport-Security"


def _app(settings: Settings, **update: Any) -> FastAPI:  # Any: Settings field overrides
    app = create_app(settings.model_copy(update=update))

    @app.get("/test-only/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/test-only/cached")
    def cached(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "max-age=60"
        return {"status": "ok"}

    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(
        app,
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        raise_server_exceptions=False,
    )


def _assert_base(response: httpx.Response, *, csp: bool = True) -> None:
    for name, value in BASE.items():
        if name == "Content-Security-Policy" and not csp:
            assert name not in response.headers
        else:
            assert response.headers.get(name) == value, (response.status_code, name)


def test_every_status_carries_the_headers(settings: Settings, world: World) -> None:
    with _client(_app(settings)) as c:
        responses = {
            200: c.get("/healthz"),
            401: c.get("/api/v1/orgs"),
            404: c.get("/no/such/path"),
            422: c.post("/api/auth/local/login", json={"email": 5}),
            500: c.get("/test-only/boom"),
        }
    with _client(_app(settings, max_request_body_bytes=16)) as c:
        responses[413] = c.post("/api/auth/local/login", content=b"x" * 100)
    for status, response in responses.items():
        assert response.status_code == status
        _assert_base(response)
        assert HSTS not in response.headers
    assert responses[500].json()["code"] == "internal_error"


def test_a_route_set_cache_control_is_kept(settings: Settings, world: World) -> None:
    with _client(_app(settings)) as c:
        r = c.get("/test-only/cached")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "max-age=60"
    assert r.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("env", ["test", "development"])
def test_no_hsts_outside_production(settings: Settings, world: World, env: str) -> None:
    with _client(_app(settings, env=env)) as c:
        for r in (c.get("/healthz"), c.get("/test-only/boom")):
            assert r.status_code in (200, 500)
            assert HSTS not in r.headers


def test_hsts_in_production_unless_off(settings: Settings, world: World) -> None:
    with _client(_app(settings, env="production")) as c:
        ok, boom = c.get("/healthz"), c.get("/test-only/boom")
    assert (ok.status_code, boom.status_code) == (200, 500)
    for r in (ok, boom):
        assert r.headers[HSTS] == "max-age=31536000; includeSubDomains"
        _assert_base(r)
    with _client(_app(settings, env="production", hsts_max_age_seconds=0)) as c:
        for r in (c.get("/healthz"), c.get("/test-only/boom")):
            assert HSTS not in r.headers


def test_negative_hsts_max_age_is_refused() -> None:
    with pytest.raises(ValueError, match="hsts_max_age_seconds"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://x",
            env="test",
            hsts_max_age_seconds=-1,
        )
    default = Settings(_env_file=None, database_url="postgresql+psycopg://x", env="test")
    assert default.hsts_max_age_seconds == 31536000


def test_development_docs_have_no_csp(settings: Settings, world: World) -> None:
    with _client(_app(settings, env="development")) as c:
        for path in ("/api/docs", "/api/openapi.json"):
            r = c.get(path)
            assert r.status_code == 200, path
            _assert_base(r, csp=False)
        _assert_base(c.get("/healthz"))  # other development routes keep the strict CSP
    with _client(_app(settings)) as c:
        r = c.get("/api/docs")
        assert r.status_code == 200
        _assert_base(r)
