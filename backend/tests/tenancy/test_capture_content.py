"""`PUT /api/v1/orgs/{org}/capture-content` (J-M2, issue #2, DECISIONS B-2512).

The client's owner, or a platform administrator who added themselves to the client, sets the
client's own `capture_content` (`full`, `metadata`, or `null` to unset) in `tenants.settings`,
keeping every other key; each change is one audit event in that client with the old value, the
new value and the actor. Refusals (viewer, invalid body, stale version, archived client, a
platform administrator without a binding) change nothing and audit nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any  # Any: JSON bodies and settings documents
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.tenancy import service
from tests.identity.harness import login
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, make_people, tenancy_client
from tests.tenancy.test_service import CTX

ACTION = "tenancy.org.capture_content.updated"


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with tenancy_client(settings) as c:
        yield c


def _url(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/capture-content"


def _user(owner: Session, email: str) -> UUID:
    uid: UUID = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()
    return uid


def _tenant(owner: Session, tenant: UUID) -> tuple[dict[str, Any], int]:
    owner.rollback()  # a fresh snapshot
    settings, version = owner.execute(
        text("SELECT settings, row_version FROM shifde.tenants WHERE id = :t"), {"t": tenant}
    ).one()
    return dict(settings or {}), version


def _events(owner: Session, tenant: UUID) -> list[tuple[UUID | None, dict[str, Any]]]:
    owner.rollback()
    rows = owner.execute(
        text(
            "SELECT actor_id, details FROM shifde.audit_events "
            "WHERE tenant_id = :t AND action = :a ORDER BY occurred_at, id"
        ),
        {"t": tenant, "a": ACTION},
    )
    return [(r[0], dict(r[1] or {})) for r in rows]


def _seed(owner: Session, tenant: UUID, settings: dict[str, Any]) -> None:
    owner.execute(
        text("UPDATE shifde.tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
        {"s": json.dumps(settings), "t": tenant},
    )
    owner.commit()


def test_the_owner_sets_full_metadata_and_unsets_keeping_other_keys(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    _seed(owner_session, a.tenant, {"other": 1})
    _, version = _tenant(owner_session, a.tenant)
    actor = _user(owner_session, people.owner_a)
    headers = login(client, people.owner_a)
    for value in ("full", "metadata", None):
        body = {"capture_content": value, "row_version": version}
        r = client.put(_url(a.slug), json=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"capture_content": value, "row_version": version + 1}
        version += 1
        stored, stored_version = _tenant(owner_session, a.tenant)
        expected: dict[str, Any] = {"other": 1}
        if value is not None:
            expected["capture_content"] = value
        assert stored == expected and stored_version == version
    events = _events(owner_session, a.tenant)
    assert events == [
        (actor, {"setting": "capture_content", "old": None, "new": "full"}),
        (actor, {"setting": "capture_content", "old": "full", "new": "metadata"}),
        (actor, {"setting": "capture_content", "old": "metadata", "new": None}),
    ]
    assert _events(owner_session, world.b.tenant) == []


def test_a_self_added_platform_administrator_sets_it_and_one_without_a_binding_gets_404(
    client: TestClient, world: World, people: People, owner_session: Session, app_db: Database
) -> None:
    a, b = world.a, world.b
    admin = _user(owner_session, people.platform)
    headers = login(client, people.platform)
    _, version = _tenant(owner_session, a.tenant)
    body = {"capture_content": "full", "row_version": version}
    missing = client.put(_url("no-such-client"), json=body, headers=headers)
    refused = client.put(_url(a.slug), json=body, headers=headers)  # no binding in A yet
    assert refused.status_code == missing.status_code == 404
    assert refused.json()["detail"] == missing.json()["detail"]
    assert (
        _tenant(owner_session, a.tenant) == ({}, version) and _events(owner_session, a.tenant) == []
    )
    principal = Principal(
        user_id=admin, email=people.platform, display_name="p", is_platform_admin=True
    )
    with app_session(app_db, None, admin) as s:
        service.add_platform_admin_to_client(s, CTX, principal, a.tenant)
    r = client.put(_url(a.slug), json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"capture_content": "full", "row_version": version + 1}
    assert _events(owner_session, a.tenant) == [
        (admin, {"setting": "capture_content", "old": None, "new": "full"})
    ]
    _, b_version = _tenant(owner_session, b.tenant)  # still no binding in B
    other = client.put(_url(b.slug), json={**body, "row_version": b_version}, headers=headers)
    assert other.status_code == 404
    assert _tenant(owner_session, b.tenant) == ({}, b_version)
    assert _events(owner_session, b.tenant) == []


def test_a_viewer_is_refused_and_nothing_changes(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    before = _tenant(owner_session, a.tenant)
    headers = login(client, people.viewer_a)
    body = {"capture_content": "full", "row_version": before[1]}
    r = client.put(_url(a.slug), json=body, headers=headers)
    assert r.status_code == 403, r.text
    assert _tenant(owner_session, a.tenant) == before
    assert _events(owner_session, a.tenant) == []


@pytest.mark.parametrize(
    "body",
    [
        {"capture_content": "off"},
        {"capture_content": ""},
        {},
        {"capture_content": "full", "extra": 1},
    ],
    ids=["off", "empty", "missing", "extra key"],
)
def test_an_invalid_body_is_422_and_changes_nothing(
    body: dict[str, Any],
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
) -> None:
    a = world.a
    before = _tenant(owner_session, a.tenant)
    headers = login(client, people.owner_a)
    r = client.put(_url(a.slug), json={**body, "row_version": before[1]}, headers=headers)
    assert r.status_code == 422, r.text
    assert _tenant(owner_session, a.tenant) == before
    assert _events(owner_session, a.tenant) == []


def test_a_stale_row_version_is_409_and_changes_nothing(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    _seed(owner_session, a.tenant, {"other": 1})
    before = _tenant(owner_session, a.tenant)
    headers = login(client, people.owner_a)
    body = {"capture_content": "full", "row_version": before[1] + 1}
    r = client.put(_url(a.slug), json=body, headers=headers)
    assert r.status_code == 409, r.text
    assert "row_version mismatch" in r.json()["detail"]
    assert _tenant(owner_session, a.tenant) == before
    assert _events(owner_session, a.tenant) == []


def test_an_archived_client_is_refused_and_nothing_changes(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    owner_session.execute(
        text("UPDATE shifde.tenants SET status = 'archived' WHERE id = :t"), {"t": a.tenant}
    )
    owner_session.commit()
    before = _tenant(owner_session, a.tenant)
    headers = login(client, people.owner_a)
    body = {"capture_content": "full", "row_version": before[1]}
    r = client.put(_url(a.slug), json=body, headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "scope_archived"
    assert _tenant(owner_session, a.tenant) == before
    assert _events(owner_session, a.tenant) == []
