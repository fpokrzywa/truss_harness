"""SHI-wide groups, part 1 (design 5.3; identity_0005; DECISIONS B-928..B-935).

A group exists once, at platform level, and holds staff. It reaches a client only through role
bindings stored in that client, at the bound scope; only the platform administrator creates,
changes, fills or binds a group (the client-owner path is tenancy's). A client context reads no
group or member row and sees only id and name of the groups bound in it. IdP group sync runs
only from installation IdPs into groups the platform administrator mapped to that IdP.

Every application assertion runs as `shifde_app` (asserted by `app_session`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.authz.service import ResourceRef, ScopeFilter, new_pdp
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.errors import Forbidden
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests.identity import oidc_flow
from tests.identity.conftest import World, app_session, owner_user

DENIED = "row-level security|insufficient_privilege|InsufficientPrivilege"


@contextmanager
def admin_session(app_db: Database, tenant: UUID | None) -> Iterator[Session]:
    """`shifde_app` with the platform-administrator context (and `tenant`'s, if given)."""
    with app_session(app_db, tenant) as s:
        db.set_platform_admin(s, True)
        yield s


def _group(s: Session, slug: str, *, idp: UUID | None = None, external: str | None = None) -> UUID:
    gid = uuid7()
    authz.insert_group(
        s,
        {
            "id": gid,
            "slug": slug,
            "name": slug.upper(),
            "source": "idp" if idp else "local",
            "idp_config_id": idp,
            "external_id": external,
            "created_at": utcnow(),
        },
    )
    return gid


def _bind_group(
    s: Session,
    tenant: UUID,
    group: UUID,
    role_key: str = "viewer",
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
) -> UUID:
    bid = uuid7()
    role = s.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": role_key}
    ).scalar_one()
    authz.insert_binding(
        s,
        {
            "id": bid,
            "tenant_id": tenant,
            "subject_type": "group",
            "subject_id": group,
            "role_id": role,
            "scope_type": scope_type,
            "scope_id": scope_id or tenant,
            "created_at": utcnow(),
        },
    )
    return bid


def _staff_group(app_db: Database, staff: UUID, bound_in: list[UUID]) -> UUID:
    """The platform administrator's way: create a group, add `staff`, bind it as Viewer at
    organization scope in each client of `bound_in`; commits."""
    with admin_session(app_db, None) as s:
        group = _group(s, f"fdes-{uuid7().hex[-8:]}")
        authz.add_group_members(s, group, [staff], "manual", utcnow())
        s.commit()
    for tenant in bound_in:
        with admin_session(app_db, tenant) as s:
            _bind_group(s, tenant, group)
            s.commit()
    return group


def _pdp(s: Session, user: UUID, tenant: UUID) -> authz.PDP:
    db.set_tenant(s, tenant)
    return new_pdp(s, Principal(user, "staff@shi.test", "Staff"), tenant)


def _staff(owner: Session, name: str) -> UUID:
    uid = owner_user(owner, f"{name}@shi.test")
    owner.commit()
    return uid


# ------------------------------------------------------------------ grants stay in their client
def test_a_group_bound_in_a_gives_no_permission_in_b(
    world: World, app_db: Database, owner_session: Session
) -> None:
    staff = _staff(owner_session, "fde")  # no membership anywhere: the group is the only path
    group = _staff_group(app_db, staff, [world.a.tenant])
    a, b = world.a.tenant, world.b.tenant
    with app_session(app_db, a) as s:
        pa = _pdp(s, staff, a)
        assert pa.authorize("workspace:read", ResourceRef(a, uuid7())).allowed
        assert "workspace:read" in pa.effective_permissions(ResourceRef(a))
        assert pa.visible_scopes("workspace:read").tenant_wide
        assert pa.authorize("workspace:read", ResourceRef(b, uuid7())).reason == "cross_tenant"
        assert authz.bound_groups(s) == [authz.BoundGroup(group, group_name(owner_session, group))]
    with app_session(app_db, b) as s:
        pb = _pdp(s, staff, b)
        assert pb.authorize("workspace:read", ResourceRef(b, uuid7())).reason == "no_binding"
        assert pb.effective_permissions(ResourceRef(b)) == set()
        assert pb.visible_scopes("workspace:read") == ScopeFilter()
        assert authz.platform_group_membership(s, tenant_id=b, user_id=staff) == frozenset()
        assert authz.bound_groups(s) == []
        roles, groups = authz.subject_ids_covering(
            s, tenant_id=b, principal=Principal(staff, "x", "x"), resource=ResourceRef(b)
        )
        assert roles == set() and groups == set()


def group_name(owner: Session, group: UUID) -> str:
    return str(
        owner.execute(text("SELECT name FROM shifde.groups WHERE id = :g"), {"g": group}).scalar()
    )


def test_a_member_of_a_group_bound_only_in_b_cannot_see_a(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Nothing in A's PDP reveals A to a person whose only group is bound in B: no visible
    scope, no permission, no group (organization listing from bindings is tenancy's)."""
    staff = _staff(owner_session, "b-fde")
    _staff_group(app_db, staff, [world.b.tenant])
    a = world.a.tenant
    with app_session(app_db, a) as s:
        pdp = _pdp(s, staff, a)
        for action in ("workspace:read", "tenant:admin", "agent:read", "member:read"):
            assert pdp.visible_scopes(action) == ScopeFilter(), action
            assert not pdp.authorize(action, ResourceRef(a)).allowed
        assert pdp.effective_permissions(ResourceRef(a, uuid7(), uuid7())) == set()
        assert pdp.ancestor_workspace_ids() == frozenset()
        assert authz.platform_group_membership(s, tenant_id=a, user_id=staff) == frozenset()
    with app_session(app_db, world.b.tenant) as s:
        assert _pdp(s, staff, world.b.tenant).visible_scopes("workspace:read").tenant_wide


def test_disabling_a_group_or_removing_a_member_ends_the_grant(
    world: World, app_db: Database, owner_session: Session
) -> None:
    staff, other = _staff(owner_session, "s1"), _staff(owner_session, "s2")
    group = _staff_group(app_db, staff, [world.a.tenant])
    with admin_session(app_db, None) as s:
        authz.add_group_members(s, group, [other], "manual", utcnow())
        authz.remove_group_members(s, group, [staff])
        s.commit()
    with app_session(app_db, world.a.tenant) as s:
        assert (
            not _pdp(s, staff, world.a.tenant)
            .authorize("workspace:read", ResourceRef(world.a.tenant))
            .allowed
        )
        assert (
            _pdp(s, other, world.a.tenant)
            .authorize("workspace:read", ResourceRef(world.a.tenant))
            .allowed
        )
    with admin_session(app_db, None) as s:
        assert authz.update_group(s, group, 1, {"status": "disabled"}) is not None
        s.commit()
    with app_session(app_db, world.a.tenant) as s:
        assert (
            not _pdp(s, other, world.a.tenant)
            .authorize("workspace:read", ResourceRef(world.a.tenant))
            .allowed
        )


# ------------------------------------------------------------------ who manages groups
def test_a_client_context_cannot_create_change_fill_enumerate_or_bind_platform_groups(
    world: World, app_db: Database, owner_session: Session
) -> None:
    staff = _staff(owner_session, "fde")
    group = _staff_group(app_db, staff, [world.a.tenant])
    viewer = "(SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = 'viewer')"
    bind = (
        "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
        f"scope_type, scope_id) VALUES (gen_random_uuid(), :t, 'group', :g, {viewer}, 'tenant', :t)"
    )
    refused = [
        ("INSERT INTO shifde.groups (id, slug, name) VALUES (gen_random_uuid(), 'x', 'x')", {}),
        (
            "INSERT INTO shifde.group_members (group_id, user_id) VALUES (:g, :u)",
            {"g": group, "u": world.a.member},
        ),
        (bind, {"t": world.b.tenant, "g": group}),  # into another client
        (bind, {"t": world.a.tenant, "g": group}),  # into its own client: platform admin only
    ]
    for principal in (world.a.member, None):
        for sql, params in refused:
            with (
                app_session(app_db, world.a.tenant, principal) as s,
                pytest.raises(DBAPIError, match=DENIED + "|binding a group needs"),
            ):
                s.execute(text(sql), params)
        with app_session(app_db, world.a.tenant, principal) as s:
            for sql in (
                "UPDATE shifde.groups SET name = 'pwned'",
                "DELETE FROM shifde.group_members",
            ):
                assert s.execute(text(sql)).rowcount == 0
            for table in ("groups", "group_members"):
                assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() == 0
            assert authz.list_groups(s, after=None, limit=100) == []
            assert authz.get_group(s, group) is None
            for write in (
                lambda s=s: authz.update_group(s, group, None, {"name": "pwned"}),
                lambda s=s: authz.add_group_members(s, group, [world.a.member], "manual", utcnow()),
                lambda s=s: authz.remove_group_members(s, group, [staff]),
                lambda s=s: authz.insert_group(s, {"id": uuid7(), "slug": "y", "name": "y"}),
            ):
                with pytest.raises(Forbidden):
                    write()
            # what A sees of its bound group: id and name only
            row = s.execute(text("SELECT * FROM shifde.shifde_bound_groups()")).mappings().one()
            assert set(row) == {"id", "name"} and row["id"] == group
            s.commit()
    with admin_session(app_db, None) as s:
        assert authz.member_user_ids(s, group) == {staff: "manual"}
        found = authz.get_group(s, group)
        assert found is not None and found.name == group_name(owner_session, group)


def test_a_staff_member_cannot_write_group_memberships_with_or_without_a_client(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Identity isolation review 5, item 1 (B-936): a `group_members` row is a grant. As
    `shifde_app`, with no client context and then with B's, a staff member cannot join a group
    bound in B, put a colleague into their own group, or move or drop a membership."""
    staff, colleague = _staff(owner_session, "fde"), _staff(owner_session, "b-fde")
    mine = _staff_group(app_db, staff, [world.a.tenant])
    bs = _staff_group(app_db, colleague, [world.b.tenant])
    insert = "INSERT INTO shifde.group_members (group_id, user_id, source) VALUES (:g, :u, :s)"
    for tenant in (None, world.b.tenant):
        for group, person in ((bs, staff), (mine, colleague)):
            for source in ("manual", "idp_sync"):
                with (
                    app_session(app_db, tenant, staff) as s,
                    pytest.raises(DBAPIError, match="row-level security"),
                ):
                    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
                    s.execute(text(insert), {"g": group, "u": person, "s": source})
        for update in (
            "UPDATE shifde.group_members SET group_id = :bs WHERE user_id = :me",
            "UPDATE shifde.group_members SET user_id = :me WHERE group_id = :bs",
        ):
            with (
                app_session(app_db, tenant, staff) as s,
                pytest.raises(DBAPIError, match="permission denied for table group_members"),
            ):
                s.execute(text(update), {"bs": bs, "me": staff})
        with app_session(app_db, tenant, staff) as s:
            assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
            for delete in (
                "DELETE FROM shifde.group_members WHERE user_id = :me",
                "DELETE FROM shifde.group_members WHERE group_id = :bs",
                "DELETE FROM shifde.group_members",
            ):
                assert s.execute(text(delete), {"bs": bs, "me": staff}).rowcount == 0
            s.commit()
    with admin_session(app_db, None) as s:
        assert authz.member_user_ids(s, mine) == {staff: "manual"}
        assert authz.member_user_ids(s, bs) == {colleague: "manual"}
    with app_session(app_db, world.b.tenant) as s:
        assert authz.platform_group_membership(s, tenant_id=world.b.tenant, user_id=staff) == (
            frozenset()
        )


def test_the_platform_administrator_manages_and_binds_groups(
    world: World, app_db: Database, owner_session: Session
) -> None:
    staff = _staff(owner_session, "fde")
    with admin_session(app_db, None) as s:
        group = _group(s, "databricks-fdes")
        authz.add_group_members(s, group, [staff], "manual", utcnow())
        renamed = authz.update_group(s, group, 1, {"name": "Databricks FDEs"})
        assert renamed is not None and renamed.row_version == 2
        assert [g.id for g in authz.list_groups(s, after=None, limit=10)] == [group]
        assert authz.groups_of_users(s, [staff])[staff][0].id == group
        assert authz.member_counts(s, [group]) == {group: 1}
        s.commit()
    with admin_session(app_db, world.a.tenant) as s:
        _bind_group(s, world.a.tenant, group, "developer")
        with (
            pytest.raises(DBAPIError, match="subject group is not a platform group"),
            s.begin_nested(),
        ):
            _bind_group(s, world.a.tenant, uuid7())
        s.commit()
    # a staff member reads their own groups outside any client, never inside one
    with app_session(app_db, None, staff) as s:
        assert [g.id for g in authz.list_groups(s, after=None, limit=10)] == [group]
        assert authz.member_user_ids(s, group) == {staff: "manual"}
    with app_session(app_db, world.a.tenant, staff) as s:
        assert authz.list_groups(s, after=None, limit=10) == []
        assert authz.bound_groups(s) == [authz.BoundGroup(group, "Databricks FDEs")]
        assert (
            _pdp(s, staff, world.a.tenant)
            .authorize("agent:create", ResourceRef(world.a.tenant, uuid7(), uuid7()))
            .allowed
        )


def test_a_group_binding_below_the_organization_needs_a_scope_of_that_client(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Workspace and project scope checks read tenancy's tables: the platform administrator's
    binding at those scopes naming no workspace or project of the client is refused with the
    trigger's message, never stored unchecked (before tenancy_0001 it failed closed on the
    missing tables). Positive group bindings at those scopes, and that they grant only at and
    below the scope: `tests/tenancy/test_bindings.py`."""
    staff = _staff(owner_session, "fde")
    group = _staff_group(app_db, staff, [])
    for scope_type in ("workspace", "project"):
        with (
            admin_session(app_db, world.a.tenant) as s,
            pytest.raises(DBAPIError, match=f"scope {scope_type} is not in this tenant"),
        ):
            _bind_group(s, world.a.tenant, group, scope_type=scope_type, scope_id=uuid7())


# ------------------------------------------------------------------ IdP mapping and sync
def _sync_idp(owner: Session, name: str, *, sync: bool = True) -> tuple[UUID, str]:
    iss = oidc_flow.sso(name)
    idp = oidc_flow.owner_oidc_idp(owner, None, iss, group_sync_mode="sync" if sync else "off")
    return idp, iss


def test_a_group_is_mapped_only_to_an_installation_idp(
    world: World, app_db: Database, owner_session: Session
) -> None:
    inst, _ = _sync_idp(owner_session, "inst")
    owner_session.commit()
    with admin_session(app_db, None) as s:
        with pytest.raises(DBAPIError, match="only from an installation identity provider"):
            _group(s, "a-idp", idp=world.a.idp, external="Eng")
        s.rollback()
        db.set_platform_admin(s, True)
        _group(s, "inst-eng", idp=inst, external="Eng")
        s.commit()
    with pytest.raises(DBAPIError, match="stays installation-wide"):
        owner_session.execute(
            text("UPDATE shifde.idp_configs SET tenant_id = :t WHERE id = :i"),
            {"t": world.a.tenant, "i": inst},
        )
    owner_session.rollback()
    with pytest.raises(DBAPIError, match="ck_idp_configs__group_sync_installation_only"):
        owner_session.execute(
            text("UPDATE shifde.idp_configs SET group_sync_mode = 'sync' WHERE id = :i"),
            {"i": world.a.idp},
        )
    owner_session.rollback()


def test_the_sync_definer_refuses_client_idps_idps_without_sync_and_strangers(
    world: World, app_db: Database, owner_session: Session
) -> None:
    inst, iss = _sync_idp(owner_session, "inst")
    quiet, quiet_iss = _sync_idp(owner_session, "quiet", sync=False)
    staff = _staff(owner_session, "fde")
    oidc_flow.owner_identity(owner_session, staff, inst, iss, "fde")
    oidc_flow.owner_identity(owner_session, staff, quiet, quiet_iss, "fde")
    owner_session.commit()
    cases = [
        (world.a.idp, world.a.member, ["Eng"]),  # a client's IdP
        (quiet, staff, ["Eng"]),  # installation IdP, sync off
        (inst, world.a.member, ["Eng"]),  # no identity at this IdP
        (inst, staff, [f"g{i}" for i in range(201)]),  # too many names
    ]
    for tenant in (None, world.a.tenant):
        for idp, person, names in cases:
            with (
                app_session(app_db, tenant) as s,
                pytest.raises(DBAPIError, match="group sync refused"),
            ):
                authz.sync_idp_groups(s, idp, person, names)
    with app_session(app_db, None) as s:
        assert authz.sync_idp_groups(s, inst, staff, []) == 0


def test_the_sync_definer_refuses_inside_a_client_context(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Identity isolation review 5, item 2 (B-936): sync runs only with no client context, so
    code inside client A (say a later resync passing request names) cannot enrol a person into
    a group bound in B, nor drop their synced memberships; the same call outside works."""
    inst, iss = _sync_idp(owner_session, "inst")
    staff = _staff(owner_session, "fde")
    oidc_flow.owner_identity(owner_session, staff, inst, iss, "fde")
    owner_session.commit()
    with admin_session(app_db, None) as s:
        eng = _group(s, "inst-eng", idp=inst, external="Eng")
        s.commit()
    with admin_session(app_db, world.b.tenant) as s:
        _bind_group(s, world.b.tenant, eng)
        s.commit()
    for names in (["Eng"], []):
        for principal in (staff, None):
            with (
                app_session(app_db, world.a.tenant, principal) as s,
                pytest.raises(DBAPIError, match="group sync refused"),
            ):
                authz.sync_idp_groups(s, inst, staff, names)
        with (
            admin_session(app_db, world.a.tenant) as s,
            pytest.raises(DBAPIError, match="group sync refused"),
        ):
            authz.sync_idp_groups(s, inst, staff, names)
        with admin_session(app_db, None) as s:
            assert authz.member_user_ids(s, eng) == ({staff: "idp_sync"} if not names else {})
        if names:  # control: the same call with no client context enrols the person
            with app_session(app_db, None) as s:
                assert authz.sync_idp_groups(s, inst, staff, names) == 1
                s.commit()
    with app_session(app_db, world.b.tenant) as s:
        assert authz.platform_group_membership(s, tenant_id=world.b.tenant, user_id=staff) == {eng}


def test_synced_groups_are_keyed_by_idp_and_sync_touches_only_that_idps_memberships(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """SETH `test_p2_review.py` test of the same name, on platform groups: the groups are
    mapped by the platform administrator to installation IdPs (sync never creates one)."""
    (idp1, iss1), (idp2, iss2) = _sync_idp(owner_session, "one"), _sync_idp(owner_session, "two")
    staff = _staff(owner_session, "fde")
    oidc_flow.owner_identity(owner_session, staff, idp1, iss1, "fde")
    oidc_flow.owner_identity(owner_session, staff, idp2, iss2, "fde")
    owner_session.commit()
    with admin_session(app_db, None) as s:
        eng1 = _group(s, "one-eng", idp=idp1, external="Eng")
        eng2 = _group(s, "two-eng", idp=idp2, external="Eng")
        ops2 = _group(s, "two-ops", idp=idp2, external="Ops")
        manual = _group(s, "two-ops-manual")
        authz.add_group_members(s, manual, [staff], "manual", utcnow())
        s.commit()
    with app_session(app_db, None) as s:
        assert authz.sync_idp_groups(s, idp1, staff, ["Eng", "Unknown"]) == 1
        assert authz.sync_idp_groups(s, idp2, staff, ["Eng", "Ops"]) == 2
        s.commit()
    count = text("SELECT count(*) FROM shifde.groups")
    assert owner_session.execute(count).scalar_one() == 4  # "Unknown" created nothing
    with app_session(app_db, None) as s:
        assert authz.sync_idp_groups(s, idp1, staff, []) == 0  # IdP 1 drops the person from Eng
        s.commit()
    with admin_session(app_db, None) as s:
        assert authz.groups_of_users(s, [staff])[staff] and {
            g.id for g in authz.groups_of_users(s, [staff])[staff]
        } == {eng2, ops2, manual}
        assert authz.idp_synced_group_ids_of_user(s, staff, idp2) == {eng2, ops2}
        assert authz.member_user_ids(s, eng1) == {}
        assert authz.member_user_ids(s, manual) == {staff: "manual"}  # sync never touches it


def test_idp_groups_are_synced_on_sign_in_and_their_grants_follow_the_claim(
    settings: Settings,
    world: World,
    app_db: Database,
    owner_session: Session,
    stub_memberships: None,
) -> None:
    """SETH `test_p2_oidc.py` `test_idp_groups_are_synced_on_login_and_removed_when_the_claim_
    shrinks` and `test_idp_group_grants_flow_through_to_permissions`, on platform groups
    synced from an installation IdP (the route parts, admin's group and binding APIs, stay
    deferred to admin): grants arrive and leave with the claim, only in the bound client."""
    idp, iss = _sync_idp(owner_session, "inst")
    staff = _staff(owner_session, "fde")
    oidc_flow.owner_identity(owner_session, staff, idp, iss, "fde")
    owner_session.commit()
    with admin_session(app_db, None) as s:
        readers = _group(s, "readers", idp=idp, external="Readers")
        _group(s, "data-eng", idp=idp, external="Data Eng")
        s.commit()
    with admin_session(app_db, world.a.tenant) as s:
        _bind_group(s, world.a.tenant, readers)
        s.commit()
    a = world.a.tenant
    fake = oidc_flow.provider()

    def sign_in(code: str, groups: list[str]) -> None:
        with oidc_flow.oidc_client(settings, fake) as c:
            r = oidc_flow.sign_in(c, fake, idp, iss, code=code, sub="fde", groups=groups)
            assert r.headers["location"] == "/", r.headers

    def synced() -> list[str]:
        rows = owner_session.execute(
            text(
                "SELECT g.external_id FROM shifde.group_members gm JOIN shifde.groups g "
                "ON g.id = gm.group_id WHERE gm.user_id = :u AND gm.source = 'idp_sync' "
                "ORDER BY 1"
            ),
            {"u": staff},
        ).scalars()
        owner_session.rollback()
        return list(rows)

    def can_read(tenant: UUID) -> bool:
        with app_session(app_db, tenant) as s:
            return _pdp(s, staff, tenant).authorize("workspace:read", ResourceRef(tenant)).allowed

    sign_in("c1", ["Readers", "Data Eng", "Not Mapped"])
    assert synced() == ["Data Eng", "Readers"]
    assert can_read(a) and not can_read(world.b.tenant)
    sign_in("c2", ["Data Eng"])
    assert synced() == ["Data Eng"]
    assert not can_read(a)  # removed at the IdP: access ends at the next sign-in
    assert owner_session.execute(text("SELECT count(*) FROM shifde.groups")).scalar_one() == 2
