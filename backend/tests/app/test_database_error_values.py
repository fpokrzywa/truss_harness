"""Issue #384: the log line of an unhandled database error holds no value the server named in its
own message or DETAIL, nor does the 500 answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.dberrors import (
    BAD_UUID,
    UNIQUE_VIOLATION,
    Failure,
    fail_naming,
    new_marker,
    rendered_logs,
)
from tests._support.fullapp import ORIGIN


@pytest.mark.parametrize("failure", [BAD_UUID, UNIQUE_VIOLATION], ids=["uuid", "unique"])
def test_an_unhandled_database_error_logs_no_value_the_server_named(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failure: Failure,
) -> None:
    marker = new_marker()
    named: list[str] = []
    app = create_app(settings)

    @app.get("/test-only/database-error")
    def broken() -> None:
        with app.state.db.session() as s:
            fail_naming(s, marker, failure, named)

    client = TestClient(
        app,
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        raise_server_exceptions=False,
    )
    with rendered_logs(capsys, caplog) as shown, client as c:
        r = c.get("/test-only/database-error")
        logged = shown()
    assert any(marker in said for said in named)
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "request.unhandled_exception" in logged
    failure.check_log(logged, marker)
    assert marker not in r.text and failure.server_text not in r.text
