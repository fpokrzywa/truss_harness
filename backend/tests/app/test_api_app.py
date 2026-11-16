"""Authentication, sessions, CSRF, Origin checks, health and error format."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shifde import __version__
from shifde.app import create_app
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.fullapp import World

ADMIN_A = "admin@demo-a.local"


def _protected_routes(client: TestClient) -> list[tuple[str, str]]:
    paths = client.get("/api/openapi.json").json()["paths"]
    return [
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith("/api/v1")
        for method in ops
    ]


def test_every_v1_route_denies_unauthenticated_requests(client: TestClient) -> None:
    routes = _protected_routes(client)
    assert len(routes) >= 15
    for method, path in routes:
        url = path.replace("{org}", "demo-a").replace("{ws}", "main").replace("{proj}", "sample")
        r = client.request(method, url, json={} if method != "GET" else None)
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["code"] == "unauthenticated"


def test_health_and_ready(client: TestClient) -> None:
    h = client.get("/api/health")
    assert h.status_code == 200 and h.json() == {"status": "ok", "version": __version__}
    r = client.get("/api/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "migrations": "ok"}}


def test_ready_reports_503_when_database_is_down(settings: Settings) -> None:
    # SETH built the app over an unreachable URL. Here `Database` verifies the role when it is
    # built (the app refuses to start unverified, B-2230), so the database goes away after
    # start-up: the app's handle is swapped for one over a dead address. Assertions unchanged.
    app = create_app(settings)
    app.state.db.dispose()
    app.state.db = Database(
        "postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=2", verify_role=False
    )
    with TestClient(app, base_url="https://testserver") as c:
        r = c.get("/api/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready" and r.json()["checks"]["database"] == "error"
        assert c.get("/api/health").status_code == 200  # liveness does not touch the DB


def test_request_id_is_echoed_or_generated(client: TestClient) -> None:
    assert (
        client.get("/api/health", headers={"X-Request-ID": "abc-12345678"}).headers["x-request-id"]
        == "abc-12345678"
    )
    generated = client.get("/api/health", headers={"X-Request-ID": "bad id!"}).headers[
        "x-request-id"
    ]
    assert generated != "bad id!" and len(generated) >= 8


def test_problem_json_shape_for_404_and_422(client: TestClient, world: World) -> None:
    nf = client.get("/api/nope")
    assert nf.status_code == 404 and nf.headers["content-type"].startswith(
        "application/problem+json"
    )
    assert nf.json()["code"] == "not_found" and nf.json()["request_id"]
    r = client.post("/api/auth/local/login", json={"email": "", "password": "secret-value-123"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert r.json()["errors"][0]["loc"][0] == "body"
    assert "secret-value-123" not in r.text  # input is never echoed


def test_logs_are_structured_and_never_contain_secrets(
    capsys: pytest.CaptureFixture[str], client: TestClient, world: World
) -> None:
    client.post(
        "/api/auth/local/login",
        json={"email": ADMIN_A, "password": "super-secret-password-xyz"},
        headers={"X-Request-ID": "req-log-12345"},
    )
    out = capsys.readouterr().out
    assert "super-secret-password-xyz" not in out
    assert '"request_id": "req-log-12345"' in out and "request.completed" in out


def test_openapi_and_docs_are_served(client: TestClient) -> None:
    spec = client.get("/api/openapi.json").json()
    assert "/api/v1/orgs/{org}/workspaces" in spec["paths"]
    assert client.get("/api/docs").status_code == 200
