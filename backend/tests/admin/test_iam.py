"""Admin part 1 behaviour: platform-only permissions, platform group audit fan-out and self-add,
the PDP's membership rule for direct bindings, and what client shapes never carry.

Carry_forward rows pinned here (DECISIONS B-1401..B-1406): role create/update refuse
`PLATFORM_ONLY_ACTIONS` with 422 (authz review 2 item 4); group membership changes audited in
each client where the group is bound, never naming another, and a platform administrator joining
such a group is a self-add there (identity review 5 item 3); direct user bindings need an active
membership in the PDP (tenancy review 3 item 1); `users.last_login_at` and
`installation_settings.updated_by` stay out of client shapes (identity review 1 item 6, tenancy
review 1 item 6).
"""

from __future__ import annotations

import json
from typing import Any  # Any: JSON documents
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.admin import schemas
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.tenancy import repository as tenancy_repository
from tests.admin.conftest import admin_client
from tests.admin.test_cross_client import _bind_group, _system_role, _uid
from tests.identity.harness import login
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People


@pytest.mark.parametrize("key", sorted(authz.PLATFORM_ONLY_ACTIONS))
def test_roles_refuse_platform_only_permissions_with_422(
    settings: Settings, world: World, people: People, owner_session: Session, key: str
) -> None:
    a = world.a
    tag = uuid7().hex[-12:]
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        body = {"key": f"r-{tag}", "name": "R", "permissions": ["workspace:read", key]}
        r = c.post(f"/api/v1/orgs/{a.slug}/roles", json=body, headers=headers)
        assert r.status_code == 422, r.text
        assert key in r.json()["detail"]
        made = c.post(
            f"/api/v1/orgs/{a.slug}/roles",
            json={**body, "permissions": ["workspace:read"]},
            headers=headers,
        )
        assert made.status_code == 201
        patch = {"permissions": [key], "row_version": made.json()["row_version"]}
        r = c.patch(f"/api/v1/orgs/{a.slug}/roles/{made.json()['id']}", json=patch, headers=headers)
        assert r.status_code == 422, r.text
    held = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.role_permissions WHERE permission_key = :k "
            "AND tenant_id = :t"
        ),
        {"k": key, "t": a.tenant},
    ).scalar_one()
    assert held == 0


def _events(owner: Session, action: str, target: UUID) -> list[tuple[UUID | None, dict[str, Any]]]:
    rows = owner.execute(
        text(
            "SELECT tenant_id, details FROM shifde.audit_events "
            "WHERE action = :a AND (target_id = :t OR details->>'through_group_id' = :t) "
            "ORDER BY occurred_at"
        ),
        {"a": action, "t": str(target)},
    ).all()
    return [(r[0], r[1]) for r in rows]


def test_group_changes_are_audited_in_each_bound_client_and_a_self_add_is_shown_there(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")
    admin_id = _uid(owner_session, people.platform)
    # the fixture names the group after A's slug; a neutral name keeps the check below exact
    owner_session.execute(
        text("UPDATE shifde.groups SET name = 'FDEs' WHERE id = :g"), {"g": a.group}
    )
    owner_session.commit()
    for email, client in ((people.owner_a, a), (people.owner_b, b)):
        with admin_client(settings) as c:
            headers = login(c, email)
            assert (
                _bind_group(c, headers, client.slug, client.tenant, a.group, viewer).status_code
                == 201
            )
    with admin_client(settings) as c:
        headers = login(c, people.platform)
        group = c.get(f"/api/v1/platform/groups/{a.group}").json()
        put = {"user_ids": [str(a.staff), str(admin_id)], "row_version": group["row_version"]}
        r = c.put(f"/api/v1/platform/groups/{a.group}/members", json=put, headers=headers)
        assert r.status_code == 200, r.text
        lonely = c.post(
            "/api/v1/platform/groups",
            json={"slug": f"l-{uuid7().hex[-12:]}", "name": "L"},
            headers=headers,
        ).json()
        put = {"user_ids": [str(admin_id)], "row_version": lonely["row_version"]}
        r = c.put(f"/api/v1/platform/groups/{lonely['id']}/members", json=put, headers=headers)
        assert r.status_code == 200
    changed = _events(owner_session, "identity.group.members_changed", a.group)
    assert sorted(str(t) for t, _ in changed) == sorted(["None", str(a.tenant), str(b.tenant)])
    for tenant, details in changed:
        assert details["added"] == [str(admin_id)]  # the staff member was already in it
        if tenant is not None:
            other = b if tenant == a.tenant else a
            blob = json.dumps(details)
            assert str(other.tenant) not in blob and other.slug not in blob
    self_added = _events(owner_session, "tenancy.platform_admin.self_added", a.group)
    assert sorted(str(t) for t, _ in self_added) == sorted([str(a.tenant), str(b.tenant)])
    assert all(d["through_group_id"] == str(a.group) for _, d in self_added)
    lonely_id = UUID(lonely["id"])
    assert [t for t, _ in _events(owner_session, "identity.group.members_changed", lonely_id)] == [
        None
    ]
    assert _events(owner_session, "tenancy.platform_admin.self_added", lonely_id) == []


def test_disabling_a_group_is_audited_in_its_clients_and_ends_its_grants(
    settings: Settings, world: World, people: People, owner_session: Session, app_db: Database
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        assert _bind_group(c, headers, a.slug, a.tenant, a.group, viewer).status_code == 201
    staff = Principal(user_id=a.staff, email="staff@shi.test", display_name="Staff")
    with app_session(app_db, a.tenant) as s:
        assert authz.new_pdp(s, staff, a.tenant).effective_permissions(ResourceRef(a.tenant))
    with admin_client(settings) as c:
        headers = login(c, people.platform)
        version = c.get(f"/api/v1/platform/groups/{a.group}").json()["row_version"]
        r = c.patch(
            f"/api/v1/platform/groups/{a.group}",
            json={"status": "disabled", "row_version": version},
            headers=headers,
        )
        assert r.status_code == 200 and r.json()["status"] == "disabled", r.text
    updated = _events(owner_session, "identity.group.updated", a.group)
    assert sorted(str(t) for t, _ in updated) == sorted(["None", str(a.tenant)])
    assert b.tenant not in {t for t, _ in updated}
    with app_session(app_db, a.tenant) as s:
        assert not authz.new_pdp(s, staff, a.tenant).effective_permissions(ResourceRef(a.tenant))


def test_platform_group_writes_check_the_idp_mapping_and_the_members(
    settings: Settings, people: People
) -> None:
    tag = uuid7().hex[-12:]
    with admin_client(settings) as c:
        headers = login(c, people.platform)
        half = {"slug": f"h-{tag}", "name": "H", "external_id": "fdes"}
        assert c.post("/api/v1/platform/groups", json=half, headers=headers).status_code == 422
        unknown = {**half, "idp_config_id": str(uuid7())}
        r = c.post("/api/v1/platform/groups", json=unknown, headers=headers)
        assert r.status_code == 409 and r.json()["detail"] == "the group could not be created"
        made = c.post(
            "/api/v1/platform/groups", json={"slug": f"h-{tag}", "name": "H"}, headers=headers
        )
        assert made.status_code == 201
        taken = c.post(
            "/api/v1/platform/groups", json={"slug": f"h-{tag}", "name": "H"}, headers=headers
        )
        assert taken.status_code == 409 and taken.json()["detail"] == r.json()["detail"]
        stranger = {"user_ids": [str(uuid7())], "row_version": made.json()["row_version"]}
        r = c.put(
            f"/api/v1/platform/groups/{made.json()['id']}/members", json=stranger, headers=headers
        )
        assert r.status_code == 422


def test_only_the_owner_binds_a_group(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    viewer = _system_role(owner_session, "viewer")
    for email in (people.viewer_a, people.both, people.owner_b):
        with admin_client(settings) as c:
            headers = login(c, email)
            r = _bind_group(c, headers, a.slug, a.tenant, a.group, viewer)
            assert r.status_code in (403, 404), r.text  # owner_b: A is invisible
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.role_bindings WHERE subject_id = :g"), {"g": a.group}
        ).scalar_one()
        == 0
    )
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        platform_template = _system_role(owner_session, "platform_administrator")
        r = _bind_group(c, headers, a.slug, a.tenant, a.group, platform_template)
        assert r.status_code == 422
        assert _bind_group(c, headers, a.slug, a.tenant, a.group, viewer).status_code == 201


def test_the_pdp_grants_no_direct_binding_once_the_membership_is_suspended(
    world: World, people: People, owner_session: Session, app_db: Database
) -> None:
    a, b = world.a, world.b
    both = Principal(user_id=people.both_id, email=people.both, display_name=people.both)
    for tenant in (a.tenant, b.tenant):
        with app_session(app_db, tenant) as s:
            assert authz.new_pdp(s, both, tenant).effective_permissions(ResourceRef(tenant))
    owner_session.execute(
        text(
            "UPDATE shifde.memberships SET status = 'suspended' WHERE tenant_id = :t AND user_id = :u"
        ),
        {"t": a.tenant, "u": people.both_id},
    )
    owner_session.commit()
    with app_session(app_db, a.tenant) as s:
        pdp = authz.new_pdp(s, both, a.tenant)
        assert pdp.effective_permissions(ResourceRef(a.tenant)) == frozenset()
        assert not pdp.authorize("workspace:read", ResourceRef(a.tenant)).allowed
    with app_session(app_db, b.tenant) as s:  # B's membership is untouched
        assert authz.new_pdp(s, both, b.tenant).effective_permissions(ResourceRef(b.tenant))


def _fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for arg in [field.annotation, *getattr(field.annotation, "__args__", ())]:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _fields(arg, seen)
    return names


def test_client_shapes_carry_no_last_login_or_installation_updated_by(
    owner_session: Session, app_db: Database, world: World
) -> None:
    shapes = [m for m in vars(schemas).values() if isinstance(m, type) and issubclass(m, BaseModel)]
    names: set[str] = set()
    for m in shapes:
        names |= _fields(m, set())
    assert not names & {"last_login_at", "updated_by"}
    someone = uuid7()
    owner_session.execute(
        text("INSERT INTO shifde.users (id, primary_email, display_name) VALUES (:i, :e, 'x')"),
        {"i": someone, "e": f"{someone.hex}@shi.test"},
    )
    before = owner_session.execute(
        text("SELECT updated_by FROM shifde.installation_settings WHERE id = 1")
    ).scalar_one()
    owner_session.execute(
        text("UPDATE shifde.installation_settings SET updated_by = :u WHERE id = 1"),
        {"u": someone},
    )
    owner_session.commit()
    try:
        with app_session(app_db, world.a.tenant) as s:  # what tenancy hands to client settings
            settings = tenancy_repository.get_installation_settings(s)
    finally:
        owner_session.execute(
            text("UPDATE shifde.installation_settings SET updated_by = :u WHERE id = 1"),
            {"u": before},
        )
        owner_session.commit()
    assert "updated_by" not in settings and str(someone) not in json.dumps(settings, default=str)
