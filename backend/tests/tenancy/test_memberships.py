"""`shifde_my_memberships()` (tenancy_0001, B-1006) through `identity.repository.my_memberships`,
as `shifde_app`: the caller's own active memberships of clients where the caller holds a live
binding (direct or through an active platform group), with role names of that client only.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.identity import repository as identity_repository
from tests.tenancy.conftest import World, app_session, owner_member


def _bind(owner: Session, tenant: UUID, subject_type: str, subject: UUID, role_key: str) -> None:
    """Owner-side binding (the trigger still checks subject and scope); expired with `expired`."""
    owner.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :id, :t, :st, :sid, r.id, 'tenant', :t "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = :k"
        ),
        {"id": uuid7(), "t": tenant, "st": subject_type, "sid": subject, "k": role_key},
    )


def _mine(
    app_db: Database, principal: UUID | None, tenant: UUID | None = None
) -> dict[UUID, list[str]]:
    with app_session(app_db, tenant, principal) as s:
        return {m.tenant_id: m.role_names for m in identity_repository.my_memberships(s)}


def test_only_clients_with_a_live_binding_are_listed_with_their_own_role_names(
    world: World, app_db: Database, owner_db: Database
) -> None:
    a, b = world.a, world.b
    with owner_db.session() as owner:
        owner_member(owner, b.tenant, a.member)  # A's member is also a member of B
        _bind(owner, a.tenant, "user", a.member, "auditor")
        owner.commit()
    # a membership without a binding does not make B visible
    assert _mine(app_db, a.member) == {a.tenant: ["Auditor"]}
    with owner_db.session() as owner:
        _bind(owner, b.tenant, "user", a.member, "tenant_administrator")
        owner.commit()
    mine = _mine(app_db, a.member)
    assert mine[a.tenant] == ["Auditor"]
    assert set(mine) == {a.tenant, b.tenant}
    assert "Auditor" not in mine[b.tenant]
    # the same answer in any client context, and nothing without a principal
    assert _mine(app_db, a.member, b.tenant) == mine
    assert _mine(app_db, None, a.tenant) == {}
    # B's own member sees only B
    assert set(_mine(app_db, b.member)) <= {b.tenant}


def test_group_held_bindings_count_and_inactive_or_expired_ones_do_not(
    world: World, app_db: Database, owner_db: Database
) -> None:
    a, b = world.a, world.b
    with owner_db.session() as owner:
        owner_member(owner, a.tenant, a.staff)
        owner.commit()
    assert _mine(app_db, a.staff) == {}
    with app_session(app_db, a.tenant) as s:
        with platform_admin_context(s):
            s.execute(
                text(
                    "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                    "role_id, scope_type, scope_id) SELECT :id, :t, 'group', :g, r.id, "
                    "'workspace', :w FROM shifde.roles r WHERE r.tenant_id IS NULL "
                    "AND r.key = 'auditor'"
                ),
                {"id": uuid7(), "t": a.tenant, "g": a.group, "w": a.ws},
            )
        s.commit()
    assert _mine(app_db, a.staff) == {a.tenant: ["Auditor"]}
    assert b.tenant not in _mine(app_db, a.staff)
    with owner_db.session() as owner:
        owner.execute(
            text("UPDATE shifde.groups SET status = 'disabled' WHERE id = :g"), {"g": a.group}
        )
        owner.commit()
    assert _mine(app_db, a.staff) == {}
    with owner_db.session() as owner:
        owner.execute(
            text("UPDATE shifde.groups SET status = 'active' WHERE id = :g"), {"g": a.group}
        )
        owner.execute(
            text(
                "UPDATE shifde.role_bindings SET expires_at = now() - interval '1 minute' "
                "WHERE subject_id = :g"
            ),
            {"g": a.group},
        )
        owner.commit()
    assert _mine(app_db, a.staff) == {}


def test_an_inactive_membership_hides_the_client(
    world: World, app_db: Database, owner_db: Database
) -> None:
    a = world.a
    with owner_db.session() as owner:
        _bind(owner, a.tenant, "user", a.member, "auditor")
        owner.execute(
            text("UPDATE shifde.memberships SET status = 'suspended' WHERE user_id = :u"),
            {"u": a.member},
        )
        owner.commit()
    assert _mine(app_db, a.member) == {}
