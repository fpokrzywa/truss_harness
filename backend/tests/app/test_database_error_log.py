"""Issue #373: the log line of an unhandled database error holds no SQL parameter, nor does the
500 answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.dberrors import ERROR, fail_with, new_marker, rendered_logs
from tests._support.fullapp import ORIGIN


def test_an_unhandled_database_error_logs_no_parameter(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = new_marker()
    app = create_app(settings)

    @app.get("/test-only/database-error")
    def broken() -> None:
        with app.state.db.session() as s:
            fail_with(s, marker)

    client = TestClient(
        app,
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        raise_server_exceptions=False,
    )
    with rendered_logs(capsys, caplog) as shown, client as c:
        r = c.get("/test-only/database-error")
        logged = shown()
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "request.unhandled_exception" in logged and ERROR in logged
    assert "division by zero" in logged
    assert marker not in logged
    assert marker not in r.text
