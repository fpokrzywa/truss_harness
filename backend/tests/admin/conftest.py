"""Admin part 1 test world: tenancy's two clients (`tests/tenancy/conftest.py`) and route people
(`tests/tenancy/routes.py`), and a minimal app with `admin.api.router` mounted behind the
app-level guards and authentication, as `tests/tenancy/routes.py` does (B-1020; `app` mounts the
router later, B-501).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: TestClient options

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.admin import api
from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.core.ssrf import Resolver
from shifde.identity.dependencies import require_auth
from shifde.tenancy import api as tenancy_api
from tests.identity.harness import ORIGIN, make_app
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["admin_client", "owner_session", "people", "world"]


@contextmanager
def admin_client(
    settings: Settings,
    *,
    http_transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    **kwargs: Any,
) -> Iterator[TestClient]:
    """`http_transport` and `resolver` reach the app's OIDC client (the fake IdP of
    `tests/_support/fake_oidc.py`), as `tests/identity/harness.py::make_app` takes them."""
    app = make_app(settings, http_transport=http_transport, resolver=resolver)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    app.include_router(tenancy_api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
