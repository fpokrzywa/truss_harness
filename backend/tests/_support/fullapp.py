"""The full-app harness: `create_app` (every router that exists on main) over a world seeded by
`shifde.cli.seeding.seed_dev_data` (SETH 3ba0e06 `tests/conftest.py` `world`/`client`/`login`).

The world uses SETH's fixed names (`demo-a`, `demo-b`, `admin@demo-a.local`, ...), so the tests
moved from SETH keep their assertions; `tests/app/conftest.py` gives each test its own database.
Requests run as `shifde_app` through the real app; the seed runs as the schema owner.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any  # Any: TestClient options

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.cli.seeding import seed_dev_data
from shifde.core.settings import Settings
from shifde.identity.passwords import PasswordService

ORIGIN = "http://127.0.0.1:3710"


@dataclass
class World:
    passwords: dict[str, str]


def fresh_world(owner: Session, *, with_groups: bool = False) -> World:
    """Seed the demo organizations and people (owner rights, on a database of its own); commits."""
    creds = seed_dev_data(owner, PasswordService(fast=True), with_groups=with_groups)
    owner.commit()
    return World(passwords=creds)


@contextmanager
def full_client(
    settings: Settings,
    *,
    http_transport: httpx.BaseTransport | None = None,
    **kwargs: Any,
) -> Iterator[TestClient]:
    app = create_app(settings, http_transport=http_transport)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def login(client: TestClient, world: World, email: str) -> dict[str, str]:
    """Log in and return headers carrying the CSRF token."""
    r = client.post(
        "/api/auth/local/login", json={"email": email, "password": world.passwords[email]}
    )
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}
