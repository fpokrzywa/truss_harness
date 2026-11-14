"""Role bindings held through groups (design 5.3; DECISIONS B-403).

A group grants exactly what its role bindings stored in a client grant, at the bound scope
(organization, workspace or project), and nothing in a client where it has no binding. The
`GroupMembership` seam answers which groups a user is in: SETH's per-client groups by default,
SHI-wide (platform-level) groups once identity makes them so. The SHI-wide tests below use a stub
membership that names the same group in every client.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.authz.service import PDP, ResourceRef, new_pdp
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.authz.conftest import owner_insert_group, seed_bindings


@dataclass
class Org:
    tenant: UUID
    ws: UUID
    proj: UUID


@pytest.fixture
def orgs() -> tuple[Org, Org]:
    return Org(uuid7(), uuid7(), uuid7()), Org(uuid7(), uuid7(), uuid7())


@pytest.fixture
def app_session(app_db: Database) -> Iterator[Session]:
    with app_db.session() as s:
        yield s


def _user(uid: UUID) -> Principal:
    return Principal(uid, "u@x", "U")


def _pdp(s: Session, uid: UUID, tenant: UUID, **kw: object) -> PDP:
    db.set_tenant(s, tenant)
    return new_pdp(s, _user(uid), tenant, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ SETH per-client groups
def test_a_group_binding_grants_its_members_only_at_the_bound_scope(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, _ = orgs
    member, outsider = uuid7(), uuid7()
    group = owner_insert_group(owner_session, "fdes", [member])
    [bid] = seed_bindings(
        seed_engine,
        [
            {
                "tenant_id": a.tenant,
                "subject_type": "group",
                "subject_id": group,
                "role_key": "viewer",
                "scope_type": "workspace",
                "scope_id": a.ws,
            }
        ],
    )
    pdp = _pdp(app_session, member, a.tenant)
    d = pdp.authorize("workspace:read", ResourceRef(a.tenant, a.ws, a.proj))
    assert d.allowed and d.matched_binding_ids == (bid,)
    assert not pdp.authorize("workspace:read", ResourceRef(a.tenant, uuid7())).allowed
    assert not pdp.authorize("workspace:read", ResourceRef(a.tenant)).allowed  # not org-wide
    assert pdp.visible_scopes("workspace:read").workspace_ids == {a.ws}
    assert pdp.ancestor_workspace_ids() == {a.ws}
    d = _pdp(app_session, outsider, a.tenant).authorize(
        "workspace:read", ResourceRef(a.tenant, a.ws)
    )
    assert not d.allowed and d.reason == "no_binding"


def test_membership_without_a_binding_grants_nothing(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, _ = orgs
    member = uuid7()
    owner_insert_group(owner_session, "unbound", [member])
    pdp = _pdp(app_session, member, a.tenant)
    d = pdp.authorize("workspace:read", ResourceRef(a.tenant, a.ws))
    assert not d.allowed and d.reason == "no_binding"
    assert pdp.effective_permissions(ResourceRef(a.tenant, a.ws, a.proj)) == set()
    assert pdp.visible_scopes("workspace:read") == authz.ScopeFilter()


def test_disabled_group_and_expired_group_binding_grant_nothing(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, _ = orgs
    member = uuid7()
    disabled = owner_insert_group(owner_session, "off", [member], status="disabled")
    expired = owner_insert_group(owner_session, "old", [member])
    seed_bindings(
        seed_engine,
        [
            {
                "tenant_id": a.tenant,
                "subject_type": "group",
                "subject_id": disabled,
                "role_key": "viewer",
                "scope_type": "tenant",
                "scope_id": a.tenant,
            },
            {
                "tenant_id": a.tenant,
                "subject_type": "group",
                "subject_id": expired,
                "role_key": "viewer",
                "scope_type": "tenant",
                "scope_id": a.tenant,
                "expires_at": utcnow() - timedelta(minutes=1),
            },
        ],
    )
    assert (
        not _pdp(app_session, member, a.tenant)
        .authorize("workspace:read", ResourceRef(a.tenant, a.ws))
        .allowed
    )


# ------------------------------------------------------------------ SHI-wide groups (seam)
def _bind_shi_group(seed: Engine, group: UUID, a: Org, b: Org) -> None:
    """One SHI-wide group, bound as Developer on a project of A and as Viewer on a workspace of B."""
    seed_bindings(
        seed,
        [
            {
                "tenant_id": a.tenant,
                "subject_type": "group",
                "subject_id": group,
                "role_key": "developer",
                "scope_type": "project",
                "scope_id": a.proj,
            },
            {
                "tenant_id": b.tenant,
                "subject_type": "group",
                "subject_id": group,
                "role_key": "viewer",
                "scope_type": "workspace",
                "scope_id": b.ws,
            },
        ],
    )


def test_a_shi_wide_group_gets_in_each_client_only_what_that_client_bound(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, b = orgs
    group, member = uuid7(), uuid7()
    _bind_shi_group(seed_engine, group, a, b)

    def shi_groups(session: Session, *, tenant_id: UUID, user_id: UUID) -> frozenset[UUID]:
        return frozenset({group}) if user_id == member else frozenset()

    pa = _pdp(app_session, member, a.tenant, group_membership=shi_groups)
    assert pa.authorize("agent:create", ResourceRef(a.tenant, a.ws, a.proj)).allowed
    assert not pa.authorize("agent:create", ResourceRef(a.tenant, a.ws, uuid7())).allowed
    assert not pa.authorize("workspace:read", ResourceRef(a.tenant, a.ws)).allowed
    assert pa.visible_scopes("agent:read") == authz.ScopeFilter(
        False, frozenset(), frozenset({a.proj})
    )
    # B's workspace, asked in A's PDP: refused, whatever B bound
    assert pa.authorize("workspace:read", ResourceRef(b.tenant, b.ws)).reason == "cross_tenant"

    pb = _pdp(app_session, member, b.tenant, group_membership=shi_groups)
    assert pb.authorize("workspace:read", ResourceRef(b.tenant, b.ws)).allowed
    assert not pb.authorize("agent:create", ResourceRef(b.tenant, b.ws, uuid7())).allowed
    assert pb.visible_scopes("agent:create") == authz.ScopeFilter()
    assert pb.ancestor_workspace_ids() == {b.ws}

    # a third client where the group has no binding: nothing
    c = uuid7()
    pc = _pdp(app_session, member, c, group_membership=shi_groups)
    assert pc.authorize("workspace:read", ResourceRef(c)).reason == "no_binding"
    assert pc.visible_scopes("workspace:read") == authz.ScopeFilter()
    # a non-member gets nothing anywhere
    other = _pdp(app_session, uuid7(), a.tenant, group_membership=shi_groups)
    assert not other.authorize("agent:create", ResourceRef(a.tenant, a.ws, a.proj)).allowed


def test_subject_ids_covering_counts_only_the_clients_own_group_bindings(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, b = orgs
    group, member = uuid7(), uuid7()
    _bind_shi_group(seed_engine, group, a, b)

    def shi_groups(session: Session, *, tenant_id: UUID, user_id: UUID) -> frozenset[UUID]:
        return frozenset({group})

    developer, viewer = (
        owner_session.execute(
            text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": k}
        ).scalar_one()
        for k in ("developer", "viewer")
    )
    db.set_tenant(app_session, a.tenant)
    roles, groups = authz.subject_ids_covering(
        app_session,
        tenant_id=a.tenant,
        principal=_user(member),
        resource=ResourceRef(a.tenant, a.ws, a.proj),
        group_membership=shi_groups,
    )
    assert roles == {developer} and groups == {group}
    roles, _ = authz.subject_ids_covering(
        app_session,
        tenant_id=a.tenant,
        principal=_user(member),
        resource=ResourceRef(a.tenant, b.ws),  # B's workspace id inside A: no A binding covers it
        group_membership=shi_groups,
    )
    assert roles == set()
    db.set_tenant(app_session, b.tenant)
    roles, _ = authz.subject_ids_covering(
        app_session,
        tenant_id=b.tenant,
        principal=_user(member),
        resource=ResourceRef(b.tenant, b.ws, uuid7()),
        group_membership=shi_groups,
    )
    assert roles == {viewer}


def test_groups_are_never_consulted_for_service_accounts(
    owner_session: Session,
    seed_engine: Engine,
    app_session: Session,
    orgs: tuple[Org, Org],
) -> None:
    a, b = orgs
    group = uuid7()
    _bind_shi_group(seed_engine, group, a, b)

    def refuse(session: Session, *, tenant_id: UUID, user_id: UUID) -> frozenset[UUID]:
        raise AssertionError("group membership asked for a service account")

    sa = Principal(group, "sa:x", "x", principal_type="service_account")  # same id as the group
    db.set_tenant(app_session, a.tenant)
    pdp = new_pdp(app_session, sa, a.tenant, group_membership=refuse)
    assert not pdp.authorize("agent:create", ResourceRef(a.tenant, a.ws, a.proj)).allowed
