"""The platform-only writes into a client that is not active (F-03 follow-up, issue #36).

`POST /orgs/{org}/users` and `POST /platform/orgs/{org}/group-bindings` take a raw slug, not
`ScopeDep`, so they check the client's status themselves: a suspended or archived client is
409 `scope_archived` naming its status (as `ScopeDep` routes answer) and nothing is written; a
client owner still gets the one 403 of an active client; active again, the write is accepted.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests.admin.conftest import admin_client
from tests.identity.harness import login
from tests.tenancy.conftest import Client, World
from tests.tenancy.routes import People


def _system_role(owner: Session, key: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": key}
    ).scalar_one()
    return found


def _set_status(owner: Session, tenant: UUID, status: str) -> None:
    owner.execute(
        text("UPDATE shifde.tenants SET status = :s WHERE id = :t"), {"s": status, "t": tenant}
    )
    owner.commit()


def _refused(r: httpx.Response, status: str) -> None:
    assert r.status_code == 409, r.text
    assert (r.json()["code"], r.json()["detail"]) == ("scope_archived", f"organization is {status}")


def _count(owner: Session, sql: str, **params: Any) -> int:  # Any: query parameters
    owner.rollback()
    found: int = owner.execute(text(sql), params).scalar_one()
    return found


def _audited(owner: Session, tenant: UUID, action: str) -> int:
    return _count(
        owner,
        "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t AND action = :a",
        t=tenant,
        a=action,
    )


@pytest.mark.parametrize("status", ["suspended", "archived"])
def test_a_client_that_is_not_active_gets_no_new_person(
    settings: Settings,
    world: World,
    people: People,
    owner_session: Session,
    status: str,
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")

    def body(c: Client) -> Any:
        email = f"new-{c.tenant.hex[-12:]}-{uuid7().hex[-8:]}@route.test"
        return {"email": email, "display_name": "New", "role_ids": [str(viewer)]}

    with admin_client(settings) as c:
        active_refusal = c.post(
            f"/api/v1/orgs/{a.slug}/users", json=body(a), headers=login(c, people.owner_a)
        )
    assert active_refusal.status_code == 403
    _set_status(owner_session, a.tenant, status)
    members = _count(
        owner_session, "SELECT count(*) FROM shifde.memberships WHERE tenant_id = :t", t=a.tenant
    )
    audited = _audited(owner_session, a.tenant, "identity.user.created")
    with admin_client(settings) as c:
        refused = c.post(
            f"/api/v1/orgs/{a.slug}/users", json=body(a), headers=login(c, people.owner_a)
        )
        assert refused.status_code == 403  # the same answer as for an active client
        assert refused.json() == active_refusal.json()
        headers = login(c, people.platform)
        sent = body(a)
        _refused(c.post(f"/api/v1/orgs/{a.slug}/users", json=sent, headers=headers), status)
        assert (
            _count(
                owner_session,
                "SELECT count(*) FROM shifde.users WHERE primary_email = :e",
                e=sent["email"],
            )
            == 0
        )
        assert (
            _count(
                owner_session,
                "SELECT count(*) FROM shifde.memberships WHERE tenant_id = :t",
                t=a.tenant,
            )
            == members
        )
        assert _audited(owner_session, a.tenant, "identity.user.created") == audited
        other = c.post(f"/api/v1/orgs/{b.slug}/users", json=body(b), headers=headers)
        assert other.status_code == 201, other.text
        _set_status(owner_session, a.tenant, "active")
        created = c.post(f"/api/v1/orgs/{a.slug}/users", json=sent, headers=headers)
        assert created.status_code == 201, created.text
    assert _audited(owner_session, a.tenant, "identity.user.created") == audited + 1


@pytest.mark.parametrize("status", ["suspended", "archived"])
def test_a_client_that_is_not_active_gets_no_new_group_binding(
    settings: Settings,
    world: World,
    people: People,
    owner_session: Session,
    status: str,
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")

    def body(c: Client) -> Any:
        return {
            "group_id": str(c.group),
            "role_id": str(viewer),
            "scope": {"type": "tenant", "id": str(c.tenant)},
        }

    def bindings() -> int:
        return _count(
            owner_session,
            "SELECT count(*) FROM shifde.role_bindings "
            "WHERE tenant_id = :t AND subject_type = 'group'",
            t=a.tenant,
        )

    url_a = f"/api/v1/platform/orgs/{a.slug}/group-bindings"
    with admin_client(settings) as c:
        active_refusal = c.post(url_a, json=body(a), headers=login(c, people.owner_a))
    assert active_refusal.status_code == 403
    _set_status(owner_session, a.tenant, status)
    before = bindings()
    audited = _audited(owner_session, a.tenant, "authz.group.bound")
    with admin_client(settings) as c:
        refused = c.post(url_a, json=body(a), headers=login(c, people.owner_a))
        assert refused.status_code == 403  # the same answer as for an active client
        assert refused.json() == active_refusal.json()
        headers = login(c, people.platform)
        _refused(c.post(url_a, json=body(a), headers=headers), status)
        assert bindings() == before
        assert _audited(owner_session, a.tenant, "authz.group.bound") == audited
        other = c.post(
            f"/api/v1/platform/orgs/{b.slug}/group-bindings", json=body(b), headers=headers
        )
        assert other.status_code == 201, other.text
        _set_status(owner_session, a.tenant, "active")
        made = c.post(url_a, json=body(a), headers=headers)
        assert made.status_code == 201, made.text
    assert bindings() == before + 1
    assert _audited(owner_session, a.tenant, "authz.group.bound") == audited + 1
