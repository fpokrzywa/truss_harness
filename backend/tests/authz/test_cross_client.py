"""Cross-client isolation for authz (design 5.3; plan per-module recipe step 5).

authz has no routes. Its entry points are the PDP (`authorize`, `require`,
`effective_permissions`, `missing_permissions`, `visible_scopes`, `ancestor_workspace_ids`),
`subject_ids_covering`, the decision log hook, and the data operations other modules call through
`authz.service`. For each: a principal bound in organization A gets nothing in organization B, and
B's rows are absent. At the database: with A's context set, as `shifde_app`, B's authz rows are
invisible and cannot be written. `execution_principal` reads users, memberships, service accounts
and API keys (identity/tenancy); its cross-client test (SETH `test_p2_execution.py`
`test_a_run_naming_another_tenant_is_rejected`) moves with identity (carry_forward.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.authz.service import PDP, ResourceRef, ScopeFilter, new_pdp
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.errors import Forbidden, NotFound
from shifde.core.hooks import run_close_hooks
from shifde.core.ids import uuid7
from tests.authz.conftest import owner_insert_bindings, owner_insert_group

WHO = text("SELECT current_user")
ALL_ACTIONS = ("tenant:admin", "workspace:read", "project:manage", "agent:create", "role:manage")


@dataclass
class Org:
    tenant: UUID
    ws: UUID
    proj: UUID
    admin: UUID  # bound directly: tenant_administrator at org level
    member: UUID  # bound through a group: workspace_administrator on `ws`
    role: UUID  # a custom role
    group: UUID
    bindings: list[UUID]

    def ref(self) -> ResourceRef:
        return ResourceRef(self.tenant, self.ws, self.proj)


def _org(owner: Session, seed: Engine, slug: str) -> Org:
    tenant, ws, proj, admin, member, role = (uuid7() for _ in range(6))
    owner.execute(
        text("INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:id, :t, :k, :k)"),
        {"id": role, "t": tenant, "k": f"{slug}-role"},
    )
    owner.execute(
        text(
            "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
            "VALUES (:id, 'workspace:read', :t)"
        ),
        {"id": role, "t": tenant},
    )
    owner.execute(
        text(
            "INSERT INTO shifde.policy_decisions (id, tenant_id, action, decision) "
            "VALUES (:id, :t, 'x:y', 'deny')"
        ),
        {"id": uuid7(), "t": tenant},
    )
    owner.commit()
    group = owner_insert_group(owner, tenant, f"{slug}-fdes", [member])
    bindings = owner_insert_bindings(
        seed,
        [
            {
                "tenant_id": tenant,
                "subject_id": admin,
                "role_key": "tenant_administrator",
                "scope_type": "tenant",
                "scope_id": tenant,
            },
            {
                "tenant_id": tenant,
                "subject_type": "group",
                "subject_id": group,
                "role_key": "workspace_administrator",
                "scope_type": "workspace",
                "scope_id": ws,
            },
            {
                "tenant_id": tenant,
                "subject_id": admin,
                "role_id": role,
                "scope_type": "project",
                "scope_id": proj,
            },
        ],
    )
    return Org(tenant, ws, proj, admin, member, role, group, bindings)


@pytest.fixture
def orgs(owner_session: Session, seed_engine: Engine) -> tuple[Org, Org]:
    return _org(owner_session, seed_engine, "a"), _org(owner_session, seed_engine, "b")


@pytest.fixture
def s(app_db: Database) -> Iterator[Session]:
    with app_db.session() as session:
        assert session.execute(WHO).scalar_one() == "shifde_app"
        yield session


def _pdp(s: Session, uid: UUID, tenant: UUID, admin: bool = False) -> PDP:
    db.set_tenant(s, tenant)
    return new_pdp(s, Principal(uid, "u@x", "U", admin), tenant)


# ------------------------------------------------------------------ PDP entry points
def test_bound_in_a_means_nothing_in_b_for_every_pdp_entry_point(
    s: Session, orgs: tuple[Org, Org]
) -> None:
    a, b = orgs
    for uid in (a.admin, a.member):  # directly and through a group
        assert _pdp(s, uid, a.tenant).effective_permissions(a.ref())  # holds something in A
        pa = _pdp(s, uid, a.tenant)
        for action in ALL_ACTIONS:
            d = pa.authorize(action, b.ref())
            assert not d.allowed and d.reason == "cross_tenant"
            assert not pa.authorize(action, ResourceRef(b.tenant)).allowed
        assert pa.effective_permissions(b.ref()) == set()
        assert pa.missing_permissions(set(ALL_ACTIONS), b.ref()) == set(ALL_ACTIONS)
        with pytest.raises(NotFound):
            pa.require("workspace:manage", b.ref(), hide_unless="workspace:read")
        with pytest.raises(Forbidden):
            pa.require("workspace:read", b.ref())

        pb = _pdp(s, uid, b.tenant)  # the same principal, asking inside B
        assert s.execute(WHO).scalar_one() == "shifde_app"
        for action in ALL_ACTIONS:
            d = pb.authorize(action, b.ref())
            assert not d.allowed and d.reason == "no_binding"
        assert pb.effective_permissions(b.ref()) == set()
        assert pb.effective_permissions(ResourceRef(b.tenant)) == set()
        assert pb.missing_permissions(set(ALL_ACTIONS), b.ref()) == set(ALL_ACTIONS)
        for action in ALL_ACTIONS:
            assert pb.visible_scopes(action) == ScopeFilter()
        assert pb.ancestor_workspace_ids() == frozenset()
        with pytest.raises(NotFound):
            pb.require("workspace:manage", b.ref(), hide_unless="workspace:read")


def test_a_pdp_never_counts_bindings_of_a_tenant_other_than_the_database_context(
    s: Session, orgs: tuple[Org, Org]
) -> None:
    """Defence in depth: the PDP filters on its tenant and RLS on the transaction's tenant; a
    mismatch between the two yields nothing, never the other client's grants."""
    a, b = orgs
    db.set_tenant(s, a.tenant)  # context A, PDP for B
    pdp = new_pdp(s, Principal(b.admin, "u@x", "U"), b.tenant)
    assert pdp.effective_permissions(ResourceRef(b.tenant)) == set()
    assert pdp.visible_scopes("workspace:read") == ScopeFilter()
    db.set_tenant(s, b.tenant)  # context B, PDP for A
    pdp = new_pdp(s, Principal(a.admin, "u@x", "U"), a.tenant)
    assert pdp.effective_permissions(ResourceRef(a.tenant)) == set()
    assert not pdp.authorize("tenant:admin", ResourceRef(a.tenant)).allowed


def test_platform_admin_flag_gives_no_client_permission(s: Session, orgs: tuple[Org, Org]) -> None:
    """Design 5.3: administering SETH does not by itself grant sight of client content."""
    _, b = orgs
    pdp = _pdp(s, uuid7(), b.tenant, admin=True)
    assert pdp.authorize("platform:admin").allowed
    for action in ALL_ACTIONS:
        assert pdp.authorize(action, b.ref()).reason == "no_binding"
    assert pdp.effective_permissions(b.ref()) == {"platform:admin"}
    assert pdp.visible_scopes("workspace:read") == ScopeFilter()


def test_subject_ids_covering_is_empty_in_the_other_client(
    s: Session, orgs: tuple[Org, Org]
) -> None:
    a, b = orgs
    db.set_tenant(s, a.tenant)
    member = Principal(a.member, "u@x", "U")
    roles, groups = authz.subject_ids_covering(
        s, tenant_id=a.tenant, principal=member, resource=a.ref()
    )
    assert roles and groups == {a.group}
    db.set_tenant(s, b.tenant)
    assert authz.subject_ids_covering(
        s, tenant_id=b.tenant, principal=member, resource=b.ref()
    ) == (
        set(),
        set(),
    )
    db.set_tenant(s, a.tenant)  # A's context, B named as the tenant
    assert authz.subject_ids_covering(
        s, tenant_id=b.tenant, principal=member, resource=b.ref()
    ) == (
        set(),
        set(),
    )


def test_decisions_are_logged_under_the_deciding_client_only(
    s: Session, orgs: tuple[Org, Org], owner_session: Session
) -> None:
    a, b = orgs
    pa = _pdp(s, a.admin, a.tenant)
    with pytest.raises(Forbidden):
        pa.require("workspace:read", b.ref())
    s.rollback()
    run_close_hooks(s)
    rows = owner_session.execute(
        text(
            "SELECT tenant_id, resource_id, reason_codes FROM shifde.policy_decisions "
            "WHERE action = 'workspace:read'"
        )
    ).all()
    assert [(r[0], r[2]) for r in rows] == [(a.tenant, ["cross_tenant"])]
    db.set_tenant(s, b.tenant)
    assert (
        s.execute(
            text("SELECT count(*) FROM shifde.policy_decisions WHERE action = 'workspace:read'")
        ).scalar_one()
        == 0
    )


# ------------------------------------------------------------------ service data operations
def test_service_data_operations_return_nothing_of_another_client(
    s: Session, orgs: tuple[Org, Org]
) -> None:
    a, b = orgs
    db.set_tenant(s, a.tenant)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    listed = {r.id for r in authz.list_roles(s, tenant_id=b.tenant, after=None, limit=500)}
    assert b.role not in listed and listed <= {
        r.id for r in authz.list_roles(s, tenant_id=a.tenant, after=None, limit=500)
    }
    assert authz.get_role(s, b.tenant, b.role) is None
    assert authz.get_role(s, a.tenant, b.role) is None
    assert not authz.role_key_exists(s, b.tenant, "b-role")
    assert authz.permissions_by_role(s, [b.role]) == {b.role: []}
    assert authz.update_role(s, b.role, None, {"name": "pwned"}) is None
    assert (
        authz.list_bindings(
            s,
            tenant_id=b.tenant,
            subject_type=None,
            subject_id=None,
            scope_type=None,
            scope_id=None,
            tenant_wide=True,
            workspace_ids=[],
            project_ids=[],
            after=None,
            limit=500,
        )
        == []
    )
    for bid in b.bindings:
        assert authz.get_binding(s, b.tenant, bid) is None
        assert authz.get_binding(s, a.tenant, bid) is None
        assert authz.delete_binding(s, b.tenant, bid) is False
    assert authz.bindings_of_subject(s, b.tenant, "user", [b.admin]) == []
    assert authz.delete_bindings_of_subject(s, b.tenant, "user", b.admin) == 0
    assert authz.get_group(s, b.tenant, b.group) is None
    assert authz.get_group_by_slug(s, b.tenant, "b-fdes") is None
    assert authz.list_groups(s, tenant_id=b.tenant, after=None, limit=500) == []
    assert authz.update_group(s, b.tenant, b.group, None, {"name": "pwned"}) is None
    assert authz.member_counts(s, b.tenant, [b.group]) == {}
    assert authz.member_user_ids(s, b.tenant, b.group) == {}
    assert authz.groups_of_users(s, b.tenant, [b.member]) == {}
    assert authz.idp_synced_group_ids_of_user(s, b.tenant, b.member, uuid7()) == set()
    assert authz.tenant_group_membership(s, tenant_id=b.tenant, user_id=b.member) == frozenset()
    authz.remove_group_members(s, b.tenant, b.group, [b.member])
    s.commit()
    # B is intact
    db.set_tenant(s, b.tenant)
    assert authz.get_role(s, b.tenant, b.role) is not None
    assert len(authz.bindings_of_subject(s, b.tenant, "user", [b.admin])) == 2
    assert authz.member_user_ids(s, b.tenant, b.group) == {b.member: "manual"}


# ------------------------------------------------------------------ database: RLS as shifde_app
TABLES = (
    "roles",
    "role_permissions",
    "role_bindings",
    "groups",
    "group_members",
    "policy_decisions",
)


@pytest.mark.parametrize("table", TABLES)
def test_with_a_set_b_rows_are_invisible_as_shifde_app(
    s: Session, orgs: tuple[Org, Org], table: str
) -> None:
    a, b = orgs
    db.set_tenant(s, a.tenant)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    seen = set(s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}")).scalars())
    assert a.tenant in seen and b.tenant not in seen
    assert seen <= {a.tenant, None}  # shared system templates only on roles/role_permissions
    s.rollback()
    db.set_tenant(s, None)
    seen = set(s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}")).scalars())
    assert seen <= {None}  # no context: no client row at all


def test_with_a_set_b_rows_cannot_be_written_as_shifde_app(
    s: Session, orgs: tuple[Org, Org]
) -> None:
    a, b = orgs
    now = utcnow()
    inserts = [
        "INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (gen_random_uuid(), :b, 'x', 'x')",
        # B's own role, or a system role, under B's tenant id
        "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) VALUES (:brole, 'project:read', :b)",
        "INSERT INTO shifde.groups (id, tenant_id, slug, name) VALUES (gen_random_uuid(), :b, 'x', 'x')",
        "INSERT INTO shifde.group_members (tenant_id, group_id, user_id) VALUES (:b, :bgroup, gen_random_uuid())",
        "INSERT INTO shifde.policy_decisions (id, tenant_id, action, decision) VALUES (gen_random_uuid(), :b, 'x:y', 'allow')",
        "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, scope_type, scope_id, created_at) VALUES (gen_random_uuid(), :b, 'group', :bgroup, :brole, 'tenant', :b, :now)",
        # A's tenant id, but B's role or group
        "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) VALUES (:brole, 'project:read', :a)",
        "INSERT INTO shifde.group_members (tenant_id, group_id, user_id) VALUES (:a, :bgroup, gen_random_uuid())",
        "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, scope_type, scope_id, created_at) VALUES (gen_random_uuid(), :a, 'group', :agroup, :brole, 'tenant', :a, :now)",
    ]
    params = {"a": a.tenant, "b": b.tenant, "brole": b.role, "bgroup": b.group, "agroup": a.group}
    for sql in inserts:
        db.set_tenant(s, a.tenant)
        assert s.execute(WHO).scalar_one() == "shifde_app"
        with pytest.raises(DBAPIError):
            s.execute(text(sql), {**params, "now": now})
        s.rollback()
    db.set_tenant(s, a.tenant)
    writes = [
        "UPDATE shifde.roles SET name = 'pwned' WHERE tenant_id = :b",
        "UPDATE shifde.groups SET name = 'pwned' WHERE tenant_id = :b",
        "DELETE FROM shifde.role_permissions WHERE tenant_id = :b",
        "DELETE FROM shifde.role_bindings WHERE tenant_id = :b",
        "DELETE FROM shifde.group_members WHERE tenant_id = :b",
        # system templates are never writable by a client either
        "UPDATE shifde.roles SET name = 'pwned' WHERE tenant_id IS NULL",
        "DELETE FROM shifde.role_permissions WHERE tenant_id IS NULL",
    ]
    for sql in writes:
        assert s.execute(text(sql), params).rowcount == 0
    s.commit()


def test_role_bindings_rls_refuses_another_clients_row_even_without_the_trigger(
    seed_engine: Engine, orgs: tuple[Org, Org]
) -> None:
    """The integrity trigger refuses B's binding first; this pins the RLS policy beneath it:
    as `shifde_app` with A's context and row triggers off, B's binding is still refused."""
    a, b = orgs
    with seed_engine.connect() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("SET LOCAL ROLE shifde_app"))
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a.tenant)})
        assert c.execute(WHO).scalar_one() == "shifde_app"
        with pytest.raises(DBAPIError, match="row-level security"):
            c.execute(
                text(
                    "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                    "role_id, scope_type, scope_id) VALUES (gen_random_uuid(), :b, 'user', "
                    "gen_random_uuid(), :r, 'tenant', :b)"
                ),
                {"b": b.tenant, "r": b.role},
            )
        c.rollback()


def test_policy_decisions_are_append_only_for_the_app_and_the_owner(
    s: Session, orgs: tuple[Org, Org], owner_session: Session
) -> None:
    a, _ = orgs
    for sql in (
        "UPDATE shifde.policy_decisions SET decision = 'allow'",
        "DELETE FROM shifde.policy_decisions",
    ):
        db.set_tenant(s, a.tenant)
        with pytest.raises(DBAPIError, match="permission denied"):
            s.execute(text(sql))
        s.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        owner_session.execute(text("UPDATE shifde.policy_decisions SET decision = 'allow'"))
    owner_session.rollback()


def test_integrity_triggers_reject_cross_client_references_even_for_the_owner(
    owner_session: Session, orgs: tuple[Org, Org]
) -> None:
    a, b = orgs
    """Subject checks of `check_role_binding()` (user, group, service account) and its
    workspace/project scope checks need identity's and tenancy's tables; they are pinned with
    those ports (carry_forward.md). The role check runs first and works now."""
    stmts = [
        # role of B bound inside A
        (
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, scope_type, scope_id) VALUES (gen_random_uuid(), :a, 'group', :agroup, :brole, 'tenant', :a)",
            "role is not available to this tenant",
        ),
        # a member row in A pointing at B's group (composite FK)
        (
            "INSERT INTO shifde.group_members (tenant_id, group_id, user_id) VALUES (:a, :bgroup, gen_random_uuid())",
            "fk_group_members__group",
        ),
        # role_permissions row whose tenant differs from its role's tenant
        (
            "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) VALUES (:brole, 'project:read', :a)",
            "must match the role tenant",
        ),
    ]
    params = {"a": a.tenant, "brole": b.role, "bgroup": b.group, "agroup": a.group}
    for sql, why in stmts:
        with pytest.raises(DBAPIError, match=why):
            owner_session.execute(text(sql), params)
        owner_session.rollback()
