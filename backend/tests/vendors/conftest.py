"""Vendors tests: the vendor routes over a minimal FastAPI app (B-920 / B-1020 pattern).

The app is the identity harness (login routes, error handlers) plus `vendors.api.router` behind
`require_auth`, which the `app` port will mount (B-501), and the process-wide integration
runtime (`app.state.integration`) the adapters routes read. People and clients are the tenancy
route-test world (fresh slugs and ids per test); the route runs as `shifde_app`.

Rows the tests add to the vendor catalog are removed after each test, as the schema owner: the
eight seeded global rows (`created_by IS NULL`) stay, every other row goes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: TestClient options

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.vendors import api
from tests.conftest import CLEAN_TABLES
from tests.identity import harness
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

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

REGISTRY = AdapterRegistry.load(include_fake=True)


@contextmanager
def vendors_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.state.integration = IntegrationRuntime(settings=settings, registry=REGISTRY)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with vendors_client(settings) as c:
        yield c


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture(autouse=True)
def clean_vendors(owner_db: Database) -> Iterator[None]:
    def clean() -> None:
        with owner_db.session() as s:
            s.execute(text("DELETE FROM shifde.vendors WHERE created_by IS NOT NULL"))
            s.commit()

    clean()
    yield
    clean()
