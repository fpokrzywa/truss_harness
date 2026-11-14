"""Installation tests: the overview route over a minimal FastAPI app (B-920 pattern).

The app is the identity harness (login routes, error handlers, `reject_basic_auth`,
`check_origin`) plus `installation.api.router`, which the `app` port will mount. People are
written as the schema owner; the route runs as `shifde_app`. `installation_settings` is a
singleton the other modules' cleanup does not touch, so a fixture restores it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.installation import api
from tests.conftest import CLEAN_TABLES
from tests.identity import harness

for _table in (
    "login_lockouts",
    "user_identities",
    "sessions",
    "local_credentials",
    "memberships",
    "idp_configs",
    "users",
    "audit_events",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_installation_settings(owner_db: Database) -> Iterator[None]:
    def reset() -> None:
        with owner_db.session() as s:
            s.execute(
                text(
                    "UPDATE shifde.installation_settings SET branding = '{}', "
                    "feature_flags = '{}', default_limits = '{}' WHERE id = 1"
                )
            )
            s.commit()

    reset()
    yield
    reset()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """The identity routes plus the installation router as `shifde_app`."""
    app = harness.make_app(settings)
    app.include_router(api.router, dependencies=[Depends(reject_basic_auth), Depends(check_origin)])
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}
        ) as c:
            yield c
    finally:
        app.state.db.dispose()
