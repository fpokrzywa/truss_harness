"""Role bindings at organization, workspace and project scope now that `workspaces` and
`projects` exist (carry_forward: authz binding cross-client tests, B-402, B-933).

As `shifde_app` in A's context the application binds A's users and service accounts (and, in the
platform-administrator context, platform groups) at every scope of A; `check_role_binding()`
refuses another client's user, service account, workspace or project, and an id that is no
platform group, each with its specific message, and it does so for the schema owner too (SETH
`test_p2_isolation.py::test_database_integrity_triggers_reject_cross_tenant_references_even_for_the_owner`).
A group binding grants only at and below its scope, through the real platform membership.
"""

from __future__ import annotations

from contextlib import nullcontext
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.authz.service import ResourceRef, new_pdp
from shifde.core.context import Principal
from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from tests.tenancy.conftest import Client, World, app_session


def _bind(
    s: Session,
    tenant: UUID,
    subject_type: str,
    subject: UUID,
    scope_type: str,
    scope_id: UUID,
    role_key: str = "auditor",
) -> UUID:
    binding = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :id, :t, :st, :sid, r.id, :sc, :scid "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = :k"
        ),
        {
            "id": binding,
            "t": tenant,
            "st": subject_type,
            "sid": subject,
            "sc": scope_type,
            "scid": scope_id,
            "k": role_key,
        },
    )
    return binding


def _scopes(c: Client) -> list[tuple[str, UUID]]:
    return [("tenant", c.tenant), ("workspace", c.ws), ("project", c.project)]


def _stored(s: Session, binding: UUID) -> UUID | None:
    found: UUID | None = s.execute(
        text("SELECT tenant_id FROM shifde.role_bindings WHERE id = :i"), {"i": binding}
    ).scalar_one_or_none()
    return found


@pytest.mark.parametrize("subject_type", ["user", "service_account"])
def test_the_app_binds_its_own_users_and_service_accounts_at_every_scope(
    world: World, app_db: Database, subject_type: str
) -> None:
    a = world.a
    subject = a.member if subject_type == "user" else a.service_account
    with app_session(app_db, a.tenant) as s:
        made = [_bind(s, a.tenant, subject_type, subject, st, sid) for st, sid in _scopes(a)]
        assert [_stored(s, b) for b in made] == [a.tenant] * 3
        s.commit()
    with app_session(app_db, world.b.tenant) as s:
        assert [_stored(s, b) for b in made] == [None] * 3


def test_the_platform_administrator_binds_a_group_at_every_scope_of_the_client(
    world: World, app_db: Database
) -> None:
    a = world.a
    with app_session(app_db, a.tenant) as s:
        with platform_admin_context(s):
            made = [_bind(s, a.tenant, "group", a.group, st, sid) for st, sid in _scopes(a)]
        assert [_stored(s, b) for b in made] == [a.tenant] * 3
        s.commit()


def _refusals(world: World) -> list[tuple[str, str, UUID, str, UUID, str]]:
    a, b = world.a, world.b
    return [
        ("user", "b member", b.member, "tenant", a.tenant, "subject user is not a member"),
        (
            "service_account",
            "b sa",
            b.service_account,
            "tenant",
            a.tenant,
            "service account is not in",
        ),
        (
            "user",
            "b workspace",
            a.member,
            "workspace",
            b.ws,
            "scope workspace is not in this tenant",
        ),
        (
            "user",
            "b project",
            a.member,
            "project",
            b.project,
            "scope project is not in this tenant",
        ),
        (
            "group",
            "b workspace",
            a.group,
            "workspace",
            b.ws,
            "scope workspace is not in this tenant",
        ),
        (
            "group",
            "b project",
            a.group,
            "project",
            b.project,
            "scope project is not in this tenant",
        ),
        ("group", "no group", uuid7(), "tenant", a.tenant, "subject group is not a platform group"),
    ]


def test_another_clients_subject_or_scope_is_refused_with_the_triggers_message(
    world: World, app_db: Database
) -> None:
    a = world.a
    for subject_type, case, subject, scope_type, scope_id, message in _refusals(world):
        with app_session(app_db, a.tenant) as s:
            admin = platform_admin_context(s) if subject_type == "group" else nullcontext()
            with admin, pytest.raises(DBAPIError, match=message):
                _bind(s, a.tenant, subject_type, subject, scope_type, scope_id)
            s.rollback()
        with app_session(app_db, a.tenant) as s:
            stored = s.execute(
                text("SELECT count(*) FROM shifde.role_bindings WHERE subject_id = :s"),
                {"s": subject},
            ).scalar_one()
            assert stored == 0, case


def test_the_trigger_refuses_cross_client_references_even_for_the_owner(
    world: World, owner_db: Database
) -> None:
    a = world.a
    for subject_type, _case, subject, scope_type, scope_id, message in _refusals(world):
        if subject_type == "group":
            continue  # the group branch needs the admin flag first (B-933)
        with owner_db.session() as owner:
            assert owner.execute(text("SELECT current_user")).scalar_one() == "shifde_owner"
            with pytest.raises(DBAPIError, match=message):
                _bind(owner, a.tenant, subject_type, subject, scope_type, scope_id)


def test_a_group_binding_without_the_admin_flag_is_refused(world: World, app_db: Database) -> None:
    with (
        app_session(app_db, world.a.tenant) as s,
        pytest.raises(DBAPIError, match="binding a group needs the platform administrator"),
    ):
        _bind(s, world.a.tenant, "group", world.a.group, "workspace", world.a.ws)


def test_a_group_bound_at_a_workspace_grants_there_and_below_only(
    world: World, app_db: Database
) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        with platform_admin_context(s):
            _bind(s, a.tenant, "group", a.group, "workspace", a.ws)
        s.commit()
    staff = Principal(a.staff, "staff@shi.test", "Staff")
    with app_session(app_db, a.tenant, a.staff) as s:
        pdp = new_pdp(s, staff, a.tenant)
        assert "workspace:read" in pdp.effective_permissions(ResourceRef(a.tenant, a.ws))
        assert "workspace:read" in pdp.effective_permissions(ResourceRef(a.tenant, a.ws, a.project))
        assert pdp.effective_permissions(ResourceRef(a.tenant, a.ws2)) == set()
        assert pdp.effective_permissions(ResourceRef(a.tenant)) == set()
    with app_session(app_db, b.tenant, a.staff) as s:
        pdp = new_pdp(s, staff, b.tenant)
        assert pdp.effective_permissions(ResourceRef(b.tenant, b.ws)) == set()


def test_a_project_grant_makes_its_workspace_an_ancestor_only(
    world: World, app_db: Database
) -> None:
    """`PDP.ancestor_workspace_ids` reads `projects` (carry_forward authz row)."""
    a = world.a
    with app_session(app_db, a.tenant) as s:
        _bind(s, a.tenant, "user", a.member, "project", a.project)
        s.commit()
    with app_session(app_db, a.tenant, a.member) as s:
        pdp = new_pdp(s, Principal(a.member, "m@shi.test", "M"), a.tenant)
        assert pdp.ancestor_workspace_ids() == frozenset({a.ws})
        assert pdp.effective_permissions(ResourceRef(a.tenant, a.ws2)) == set()
