"""Audit Item 18 (J-M4): `/api/ready` answers 503 within 5 s when the database is down, with the
app's default `Settings` timeouts. The app's handle is swapped after start-up (the app refuses to
start over an unreachable database, B-2230) and wired the way `create_app` wires it."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shifde.app import create_app
from shifde.core.db import Database
from shifde.core.settings import Settings

CLOSED_LOCAL_PORT = "postgresql+psycopg://x:y@127.0.0.1:1/none"  # refused


@pytest.fixture(params=["refused", "silent"])
def url(request: pytest.FixtureRequest) -> Iterator[str]:
    """`refused`: a closed local port. `silent`: a local listener this test opens and never accepts
    from, so the handshake completes in the kernel's backlog and the server never answers."""
    if request.param == "refused":
        yield CLOSED_LOCAL_PORT
        return
    with socket.create_server(("127.0.0.1", 0), backlog=8) as listener:
        yield f"postgresql+psycopg://x:y@127.0.0.1:{listener.getsockname()[1]}/none"


def test_ready_answers_503_within_5s_when_database_is_down(settings: Settings, url: str) -> None:
    app = create_app(settings)
    app.state.db.dispose()
    app.state.db = Database(
        url,
        verify_role=False,
        connect_timeout=settings.db_connect_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )
    with TestClient(app, base_url="https://testserver") as c:
        start = time.monotonic()
        r = c.get("/api/ready")
        elapsed = time.monotonic() - start
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready" and r.json()["checks"]["database"] == "error"
        assert elapsed < 5.0, f"/api/ready took {elapsed:.1f} s to answer 503"
        assert c.get("/api/health").status_code == 200  # liveness does not touch the DB
