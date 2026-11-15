"""Tenancy service (part 2a): who sees a client, who may grant, the session pin, as `shifde_app`.

Every test runs the application role under forced row-level security (`app_session` asserts
`current_user = 'shifde_app'`). Decisions B-1008..B-1014.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.authz import service as authz
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database
from shifde.core.errors import Forbidden, ValidationFailed
from shifde.core.ids import uuid7
from shifde.identity import repository as identity_repo
from shifde.identity import service as identity
from shifde.tenancy import service
from shifde.tenancy.schemas import OrgCreate, OrgSummaryOut
from shifde.tenancy.scope import TenantScope
from tests._support.tenants import bind_user
from tests.authz.conftest import owner_insert_bindings
from tests.tenancy.conftest import World, app_session, owner_member, owner_person

CTX = RequestContext(request_id="req", client_ip="10.0.0.1", user_agent="pytest")


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


def _p(uid: UUID, *, admin: bool = False) -> Principal:
    return Principal(
        user_id=uid, email=f"{uid}@shi.test", display_name="p", is_platform_admin=admin
    )


def _scope(s: Session, tenant: UUID, principal: Principal) -> TenantScope:
    return TenantScope(
        s, CTX, principal, tenant, "x", "x", "active", authz.new_pdp(s, principal, tenant)
    )


def _bind_group(owner: Session, tenant: UUID, group: UUID, role_key: str = "viewer") -> None:
    owner.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
    owner.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :i, :t, 'group', :g, r.id, 'tenant', :t "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = :k"
        ),
        {"i": uuid7(), "t": tenant, "g": group, "k": role_key},
    )
    owner.commit()


def _role_id(owner: Session, key: str) -> UUID:
    return owner.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": key}
    ).scalar_one()


def _admin(owner: Session) -> UUID:
    uid = owner_person(owner, "admin")
    owner.execute(
        text("UPDATE shifde.users SET is_platform_admin = true WHERE id = :u"), {"u": uid}
    )
    owner.commit()
    return uid


def _visible(app_db: Database, person: UUID) -> set[UUID]:
    with app_session(app_db, None, person) as s:
        return {o.id for o in service.list_orgs(s, _p(person), include_archived=True).items}


def _events(owner: Session, tenant: UUID, action: str) -> list[dict[str, object]]:
    rows = owner.execute(
        text("SELECT details FROM shifde.audit_events WHERE tenant_id = :t AND action = :a"),
        {"t": tenant, "a": action},
    )
    return [dict(r[0] or {}) for r in rows]


# ------------------------------------------------------------------ review 1 item 1
@pytest.mark.parametrize("admin_flag", [False, True])
def test_a_client_context_cannot_create_a_client(
    world: World, app_db: Database, admin_flag: bool
) -> None:
    ghost = uuid7()
    with app_session(app_db, ghost) as s:
        if admin_flag:  # the platform-administrator context may (create_org's path)
            db.set_platform_admin(s, True)
        insert = text("INSERT INTO shifde.tenants (id, slug, name) VALUES (:i, :s, 'g')")
        if admin_flag:
            s.execute(insert, {"i": ghost, "s": f"ghost-{ghost.hex[-12:]}"})
            s.rollback()
            return
        with pytest.raises(DBAPIError, match="row-level security"):
            s.execute(insert, {"i": ghost, "s": f"ghost-{ghost.hex[-12:]}"})
        s.rollback()
        set_slug = world.b.slug  # B's slug is not an existence oracle either: refused first
        with pytest.raises(DBAPIError, match="row-level security"):
            s.execute(insert, {"i": uuid7(), "s": set_slug})


# ------------------------------------------------------------------ visibility (B-1009)
def test_without_a_binding_a_person_sees_no_client(world: World, app_db: Database) -> None:
    assert _visible(app_db, world.a.member) == set()  # review 1 item 2: member, no binding
    assert _visible(app_db, world.a.staff) == set()  # group with no binding anywhere


def test_a_member_of_a_group_bound_only_in_b_lists_only_b_and_cannot_pin_a(
    world: World, app_db: Database, owner_session: Session
) -> None:
    _bind_group(owner_session, world.b.tenant, world.a.group)
    owner_member(owner_session, world.a.tenant, world.a.staff)  # member of A, no binding there
    owner_session.commit()
    assert _visible(app_db, world.a.staff) == {world.b.tenant}
    pin_a, pin_b = (
        _session(owner_session, world.a.staff, world.a.tenant),
        _session(owner_session, world.a.staff, world.b.tenant),
    )
    with app_session(app_db, None) as s:
        assert identity_repo.resolve_session(s, pin_a) is None
        assert identity_repo.resolve_session(s, pin_b) is not None


def test_a_suspended_membership_hides_a_client_the_group_is_bound_in(
    world: World, app_db: Database, owner_session: Session
) -> None:
    _bind_group(owner_session, world.b.tenant, world.a.group)
    owner_member(owner_session, world.b.tenant, world.a.staff, status="suspended")
    owner_session.commit()
    assert _visible(app_db, world.a.staff) == set()


def test_role_names_come_only_from_the_clients_own_roles(
    world: World, app_db: Database, owner_session: Session, worker_admin_url: str
) -> None:
    """Review 1 item 3: a binding in B naming A's own role (only a hostile owner could write it)
    does not lend A's role name to B's listing."""
    a_role = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:i, :t, 'a-only', 'A secret')"
        ),
        {"i": a_role, "t": world.a.tenant},
    )
    owner_session.commit()
    bind_user(owner_session, world.b.tenant, world.b.member)
    owner_session.commit()
    seed = create_engine(worker_admin_url, poolclass=NullPool)
    owner_insert_bindings(
        seed,
        [
            {
                "tenant_id": world.b.tenant,
                "subject_id": world.b.member,
                "role_id": a_role,
                "scope_type": "tenant",
                "scope_id": world.b.tenant,
            }
        ],
    )
    seed.dispose()
    with app_session(app_db, None, world.b.member) as s:
        [row] = identity.my_memberships(s)
    assert row.tenant_id == world.b.tenant and row.role_names == ["Viewer"]


def test_the_platform_admin_flag_lists_no_client_and_the_full_list_checks_the_principal(
    world: World, app_db: Database, owner_session: Session
) -> None:
    admin = _admin(owner_session)
    assert _visible(app_db, admin) == set()
    with app_session(app_db, None, admin) as s:
        db.set_platform_admin(s, True)  # the database flag alone is not enough
        with pytest.raises(Forbidden):
            service.list_all_orgs(s, _p(admin), include_archived=True, limit=500, cursor=None)
        # Follow every page: tenant rows from earlier tests stay in the worker's database
        # (tenants are not truncated) and ids sort oldest first, so A and B can be past page one.
        listed: dict[UUID, OrgSummaryOut] = {}
        cursor: str | None = None
        while True:
            page = service.list_all_orgs(
                s, _p(admin, admin=True), include_archived=True, limit=500, cursor=cursor
            )
            listed.update((o.id, o) for o in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    assert {world.a.tenant, world.b.tenant} <= set(listed)
    assert set(type(listed[world.a.tenant]).model_fields) == {"id", "slug", "name", "status"}
    with (
        app_session(app_db, world.a.tenant, world.a.member) as s,  # a client principal
        pytest.raises(Forbidden),
    ):
        service.list_all_orgs(s, _p(world.a.member), include_archived=True, limit=5, cursor=None)


# ------------------------------------------------------------------ session pin
def _session(owner: Session, user: UUID, tenant: UUID | None) -> bytes:
    digest, now = uuid7().bytes * 2, utcnow()
    owner.execute(
        text(
            "INSERT INTO shifde.sessions (id, session_hash, user_id, active_tenant_id, csrf_hash, "
            "idle_expires_at, absolute_expires_at) VALUES (:id, :h, :u, :t, 'c', :i, :a)"
        ),
        {
            "id": uuid7(),
            "h": digest,
            "u": user,
            "t": tenant,
            "i": now + timedelta(hours=1),
            "a": now + timedelta(hours=8),
        },
    )
    owner.commit()
    return digest


def test_a_pin_lapses_with_the_binding_and_with_an_archived_client(
    world: World, app_db: Database, owner_session: Session
) -> None:
    bid = bind_user(owner_session, world.a.tenant, world.a.member)
    bind_user(owner_session, world.b.tenant, world.b.member)
    owner_session.commit()
    pin_a, pin_b = (
        _session(owner_session, world.a.member, world.a.tenant),
        _session(owner_session, world.b.member, world.b.tenant),
    )
    with app_session(app_db, None) as s:
        assert identity_repo.resolve_session(s, pin_a) is not None
    owner_session.execute(
        text(
            "UPDATE shifde.role_bindings SET expires_at = now() - interval '1 second' WHERE id = :i"
        ),
        {"i": bid},
    )
    owner_session.commit()
    with app_session(app_db, None) as s:  # A's binding revoked: A's pin is gone, B's holds
        assert identity_repo.resolve_session(s, pin_a) is None
        assert identity_repo.resolve_session(s, pin_b) is not None
    owner_session.execute(
        text("UPDATE shifde.tenants SET status = 'archived' WHERE id = :t"), {"t": world.b.tenant}
    )
    owner_session.commit()
    with app_session(app_db, None) as s:  # findings Item 12 F-03
        assert identity_repo.resolve_session(s, pin_b) is None


def test_suspending_in_a_revokes_only_sessions_pinned_to_a_and_never_an_admin(
    world: World, app_db: Database, owner_session: Session
) -> None:
    both = owner_person(owner_session, "both")
    owner_member(owner_session, world.a.tenant, both)
    owner_member(owner_session, world.b.tenant, both)
    for t in (world.a.tenant, world.b.tenant):
        bind_user(owner_session, t, both)
    owner_session.commit()
    pins = {t: _session(owner_session, both, t) for t in (world.a.tenant, world.b.tenant, None)}
    with app_session(app_db, world.a.tenant) as s:
        assert identity_repo.revoke_user_sessions(s, both) == 1
        s.commit()
    with app_session(app_db, None) as s:
        live = {
            t
            for t, h in pins.items()
            if (r := identity_repo.resolve_session(s, h)) and r.revoked_at is None
        }
    assert live == {world.b.tenant, None}
    admin = _admin(owner_session)
    owner_member(owner_session, world.a.tenant, admin)
    owner_session.commit()
    with app_session(app_db, world.a.tenant) as s, pytest.raises(DBAPIError, match="not a member"):
        identity_repo.revoke_user_sessions(s, admin)


# ------------------------------------------------------------------ who may grant (B-1010)
def _owner_of(owner: Session, tenant: UUID, member: UUID) -> None:
    bind_user(owner, tenant, member, "tenant_administrator")
    owner.commit()


def test_the_owner_binds_a_group_in_their_own_client_audited_there_only(
    world: World, app_db: Database, owner_session: Session
) -> None:
    _owner_of(owner_session, world.a.tenant, world.a.member)
    viewer = _role_id(owner_session, "viewer")
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        p = _p(world.a.member)
        service.bind_group(
            _scope(s, world.a.tenant, p),
            group_id=world.b.group,
            role_id=viewer,
            scope_type="tenant",
            scope_id=world.a.tenant,
        )
    [event] = _events(owner_session, world.a.tenant, "authz.group.bound")
    assert event["group_id"] == str(world.b.group) and event["by_platform_administrator"] is False
    assert str(world.b.tenant) not in str(event)  # no other client named
    assert _events(owner_session, world.b.tenant, "authz.group.bound") == []
    assert _visible(app_db, world.b.staff) == {world.a.tenant}


def test_as_owner_cannot_bind_into_b_and_a_non_owner_administrator_cannot_bind_a_group(
    world: World, app_db: Database, owner_session: Session
) -> None:
    _owner_of(owner_session, world.a.tenant, world.a.member)
    admin_b = owner_person(owner_session, "ws-admin-b")
    owner_member(owner_session, world.b.tenant, admin_b)
    owner_session.commit()
    bind_user(owner_session, world.b.tenant, admin_b, "workspace_administrator")
    _bind_group(owner_session, world.b.tenant, world.a.group, "tenant_administrator")
    owner_session.commit()
    viewer = _role_id(owner_session, "viewer")
    cases = [
        (world.a.member, world.b.tenant),  # A's owner, B's context and binding
        (admin_b, world.b.tenant),  # B's workspace administrator (role:manage, not owner)
        (world.a.staff, world.b.tenant),  # tenant_administrator through a group only
    ]
    for person, tenant in cases:
        with app_session(app_db, tenant, person) as s:
            with pytest.raises(Forbidden):
                service.bind_group(
                    _scope(s, tenant, _p(person)),
                    group_id=world.b.group,
                    role_id=viewer,
                    scope_type="tenant",
                    scope_id=tenant,
                )
            s.rollback()
            with pytest.raises(DBAPIError, match="needs the platform administrator"):
                s.execute(
                    text(
                        "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                        "role_id, scope_type, scope_id) VALUES (:i, :t, 'group', :g, :r, 'tenant', :t)"
                    ),
                    {"i": uuid7(), "t": tenant, "g": world.b.group, "r": viewer},
                )
    with (
        app_session(app_db, world.a.tenant, world.a.member) as s,  # A's context, B's row
        pytest.raises(DBAPIError),
    ):
        s.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) VALUES (:i, :t, 'group', :g, :r, 'tenant', :t)"
            ),
            {"i": uuid7(), "t": world.b.tenant, "g": world.b.group, "r": viewer},
        )
    assert _visible(app_db, world.b.staff) == set()


def test_the_platform_administrator_template_is_never_bound_in_a_client(
    world: World, app_db: Database, owner_session: Session
) -> None:
    _owner_of(owner_session, world.a.tenant, world.a.member)
    pa_role = _role_id(owner_session, "platform_administrator")
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(ValidationFailed):
            authz.grant_role(
                s,
                tenant_id=world.a.tenant,
                user_id=world.a.member,
                role_key="platform_administrator",
                scope_type="tenant",
                scope_id=world.a.tenant,
                created_by=world.a.member,
            )
        with pytest.raises(Forbidden):
            service.bind_group(
                _scope(s, world.a.tenant, _p(world.a.member)),
                group_id=world.a.group,
                role_id=pa_role,
                scope_type="tenant",
                scope_id=world.a.tenant,
            )
    with pytest.raises(DBAPIError, match="role is not available"):  # even the schema owner
        bind_user(owner_session, world.a.tenant, world.a.member, "platform_administrator")
    owner_session.rollback()


def test_platform_admin_self_add_is_audited_in_that_client_only(
    world: World, app_db: Database, owner_session: Session
) -> None:
    admin = _admin(owner_session)
    with app_session(app_db, None, admin) as s:
        with pytest.raises(Forbidden):  # the flag on the principal alone (no PDP) is not enough
            service.add_platform_admin_to_client(s, CTX, _p(admin), world.a.tenant)
        service.add_platform_admin_to_client(s, CTX, _p(admin, admin=True), world.a.tenant)
        assert db.get_context(s).tenant_id is None and not db.get_context(s).platform_admin
    [event] = _events(owner_session, world.a.tenant, "tenancy.platform_admin.self_added")
    assert event["role"] == "tenant_administrator"
    assert _events(owner_session, world.b.tenant, "tenancy.platform_admin.self_added") == []
    assert _visible(app_db, admin) == {world.a.tenant}
    with app_session(app_db, world.a.tenant, world.a.member) as s:  # the owner reads it
        rows = s.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = :a"),
            {"a": "tenancy.platform_admin.self_added"},
        ).scalar_one()
    assert rows == 1


def test_an_owner_cannot_add_staff_as_members(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Memberships stay platform-administrator only (B-910, B-1014): an owner gives staff access
    by binding a platform group."""
    _owner_of(owner_session, world.a.tenant, world.a.member)
    with app_session(app_db, world.a.tenant, world.a.member) as s, pytest.raises(Forbidden):
        identity.add_member(s, tenant_id=world.a.tenant, user_id=world.a.staff)


def test_a_failed_create_org_leaves_no_client_context(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """B-116 item 6 for the tenancy service: a failure after the client context was set (inside
    the platform-administrator context and a savepoint) leaves neither the client nor the flag."""
    admin = _admin(owner_session)
    with app_session(app_db, None, admin) as s:
        body = OrgCreate(slug=f"n-{uuid7().hex[-12:]}", name="N", admin_email="nobody@nowhere.test")
        with pytest.raises(ValidationFailed):
            service.create_org(s, CTX, _p(admin, admin=True), body)
        ctx = db.get_context(s)
        assert ctx.tenant_id is None and not ctx.platform_admin
        row = s.execute(
            text("SELECT shifde.current_tenant_id(), shifde.is_platform_admin_ctx()")
        ).one()
        assert row[0] is None and row[1] is False
        out = service.create_org(
            s, CTX, _p(admin, admin=True), body.model_copy(update={"admin_email": None})
        )
        assert db.get_context(s).tenant_id is None
    assert out.slug == body.slug
