"""Connection routes: SETH 3ba0e06 tests moved with their assertions (DECISIONS B-1506).

`test_p5_core.py::test_connection_call_deadline_applies_to_direct_invokes`: SETH's full app
(`create_app`) becomes the connections route app (`routes.connections_client`) with the same
settings change; SETH's world login and `/secrets` call become A's owner and `routes.secret`.
"""

from __future__ import annotations

import time

from shifde.adapters.testservers import RestTestServer
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    connections_client,
    org,
    rest_body,
    secret,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def test_connection_call_deadline_applies_to_direct_invokes(
    world: World,
    people: People,
    app_db: Database,
    rest_server: RestTestServer,
    settings: Settings,
) -> None:
    """Direct REST invoke through the API runs under `connection_call_deadline_seconds`."""
    app_settings = settings.model_copy(update={"connection_call_deadline_seconds": 0.5})
    A = org(world.a.slug)
    with connections_client(app_settings) as c:
        h = as_user(c, people.owner_a)
        sid = secret(app_db, settings, world.a.tenant, REST_TOKEN)
        conn = connection(c, h, rest_body(rest_server, sid), A)
        activate(c, h, conn["id"], A)
        started = time.monotonic()
        r = c.post(
            f"{A}/connections/{conn['id']}/invoke",
            headers=h,
            json={"method": "GET", "path": "/slow"},
        )
        assert time.monotonic() - started < 2.5
        assert r.status_code in (502, 504, 422), r.text  # classified failure, never a hang
        assert "timeout" in r.text.lower() or "deadline" in r.text.lower()
