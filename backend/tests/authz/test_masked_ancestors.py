"""`PDP.ancestor_workspace_ids` of a masked principal that holds only organization-level grants
(issue #359, DECISIONS B-4040): with `workspace:read` at organization level the result has the
mask's workspaces and the workspaces of the mask's projects; every other case is unchanged.

Client A has workspace `ws` with projects `p1` and `p2`, and workspace `ws2` with project `p3`.
Every PDP runs as `shifde_app`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any  # Any: Principal fields
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shifde.authz.service import PDP, ResourceRef, ScopeFilter, new_pdp
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, ResourceScope
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.tenants import add_project, add_tenants, add_workspace
from tests.authz.conftest import owner_insert_group, seed_bindings

NOTHING: frozenset[UUID] = frozenset()
KEY_SCOPES = frozenset({"workspace:read", "project:read", "mcp:access"})


@dataclass(frozen=True)
class Org:
    tenant: UUID
    ws: UUID
    p1: UUID
    p2: UUID
    ws2: UUID
    p3: UUID
    uid: UUID

    @property
    def mask(self) -> ResourceScope:
        return ResourceScope(project_ids=frozenset({self.p1}))


def _org(owner_session: Session) -> Org:
    tenant = uuid7()
    add_tenants(owner_session, tenant)
    ws, ws2 = add_workspace(owner_session, tenant), add_workspace(owner_session, tenant)
    p1, p2 = (add_project(owner_session, tenant, ws) for _ in range(2))
    p3 = add_project(owner_session, tenant, ws2)
    owner_session.commit()
    return Org(tenant, ws, p1, p2, ws2, p3, uuid7())


@pytest.fixture
def org(owner_session: Session) -> Org:
    return _org(owner_session)


@pytest.fixture
def s(app_db: Database) -> Iterator[Session]:
    with app_db.session() as session:
        assert session.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        yield session


def _bind(
    seed: Engine,
    org: Org,
    role_key: str | None = "viewer",
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
    *,
    role_id: UUID | None = None,
    subject_type: str = "user",
    subject_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> None:
    seed_bindings(
        seed,
        [
            {
                "tenant_id": org.tenant,
                "subject_type": subject_type,
                "subject_id": subject_id or org.uid,
                "role_key": role_key,
                "role_id": role_id,
                "scope_type": scope_type,
                "scope_id": scope_id or org.tenant,
                "expires_at": expires_at,
            }
        ],
    )


def _custom_role(owner: Session, org: Org, *permissions: str) -> UUID:
    role = uuid7()
    owner.execute(
        text("INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:i, :t, :k, :k)"),
        {"i": role, "t": org.tenant, "k": f"r-{role.hex}"},
    )
    for key in permissions:
        owner.execute(
            text(
                "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
                "VALUES (:r, :p, :t)"
            ),
            {"r": role, "p": key, "t": org.tenant},
        )
    owner.commit()
    return role


def _pdp(s: Session, org: Org, mask: ResourceScope | None, **over: Any) -> PDP:
    """The PDP of a key of `org.uid` in its client, with `over` replacing Principal fields."""
    db.set_tenant(s, org.tenant)
    fields: dict[str, Any] = {
        "tenant_id": org.tenant,
        "credential": "api_key",
        "key_scopes": KEY_SCOPES,
        "resource_scope": mask,
        **over,
    }
    return new_pdp(s, Principal(org.uid, "u@x", "U", **fields), org.tenant)


# ------------------------------------------------------------------ the rule
def test_a_masked_key_gets_the_workspaces_of_its_mask(
    s: Session, org: Org, owner_session: Session, seed_engine: Engine
) -> None:
    _bind(seed_engine, org)
    scopes = owner_session.execute(
        text("SELECT scope_type FROM shifde.role_bindings WHERE subject_id = :u"), {"u": org.uid}
    ).scalars()
    assert list(scopes) == ["tenant"]
    owner_session.rollback()
    pdp = _pdp(s, org, org.mask)
    assert pdp.ancestor_workspace_ids() == {org.ws}
    assert pdp.granted_workspace_ids() == NOTHING  # what it was before (the dashboard's activity)
    both = ResourceScope(frozenset({org.ws2}), frozenset({org.p1}))
    assert _pdp(s, org, both).ancestor_workspace_ids() == {org.ws, org.ws2}
    assert _pdp(s, org, ResourceScope()).ancestor_workspace_ids() == NOTHING
    unknown = ResourceScope(project_ids=frozenset({uuid7()}))
    assert _pdp(s, org, unknown).ancestor_workspace_ids() == NOTHING


@pytest.mark.parametrize(
    ("principal_type", "credential"),
    [("service_account", "api_key"), ("service_account", "execution"), ("user", "execution")],
)
def test_every_masked_principal_follows_the_same_rule(
    s: Session, org: Org, seed_engine: Engine, principal_type: str, credential: str
) -> None:
    _bind(seed_engine, org, subject_type=principal_type)
    who = {"principal_type": principal_type, "credential": credential}
    assert _pdp(s, org, org.mask, **who).ancestor_workspace_ids() == {org.ws}
    assert _pdp(s, org, None, **who).ancestor_workspace_ids() == NOTHING


def test_a_bound_platform_group_counts_as_a_direct_binding(
    s: Session, org: Org, owner_session: Session, seed_engine: Engine
) -> None:
    group = owner_insert_group(owner_session, f"g-{uuid7().hex}", [org.uid])
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == NOTHING  # membership grants nothing
    _bind(seed_engine, org, subject_type="group", subject_id=group)
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == {org.ws}


# ------------------------------------------------------------------ another client
def test_a_mask_naming_another_clients_project_gives_none_of_its_workspaces(
    s: Session, org: Org, owner_session: Session, seed_engine: Engine
) -> None:
    b = _org(owner_session)
    _bind(seed_engine, org)

    def ancestors(tenant: UUID, *projects: UUID) -> frozenset[UUID]:
        db.set_tenant(s, tenant)
        mask = ResourceScope(project_ids=frozenset(projects))
        key = Principal(org.uid, "u@x", "U", credential="api_key", resource_scope=mask)
        return new_pdp(s, key, tenant).ancestor_workspace_ids()

    assert ancestors(org.tenant, org.p1) == {org.ws}
    assert ancestors(org.tenant, b.p1) == NOTHING
    assert ancestors(org.tenant, org.p1, b.p1) == {org.ws}
    for projects in ((org.p1,), (b.p1,), (org.p1, b.p1)):  # the same principal inside B
        assert ancestors(b.tenant, *projects) == NOTHING


# ------------------------------------------------------------------ unchanged in every other case
def test_without_a_mask_an_organization_grant_adds_nothing(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    _bind(seed_engine, org)
    assert _pdp(s, org, None).ancestor_workspace_ids() == NOTHING  # an unmasked key
    session = _pdp(s, org, None, credential="session", key_scopes=None, tenant_id=None)
    assert session.ancestor_workspace_ids() == NOTHING
    no_client = new_pdp(s, Principal(org.uid, "u@x", "U", resource_scope=org.mask), None)
    assert no_client.ancestor_workspace_ids() == NOTHING


def test_a_workspace_grant_outside_the_mask_gives_nothing_of_it(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    _bind(seed_engine, org, scope_type="workspace", scope_id=org.ws2)
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == NOTHING
    assert _pdp(s, org, None).ancestor_workspace_ids() == {org.ws2}
    _bind(seed_engine, org)  # with the organization-level grant too
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == {org.ws}
    assert _pdp(s, org, None).ancestor_workspace_ids() == {org.ws2}


def test_an_expired_binding_gives_nothing(s: Session, org: Org, seed_engine: Engine) -> None:
    _bind(seed_engine, org, expires_at=utcnow() - timedelta(minutes=1))
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == NOTHING


@pytest.mark.parametrize("permission", ["mcp:access", "project:read"])
def test_an_organization_grant_of_another_permission_gives_nothing(
    s: Session, org: Org, owner_session: Session, seed_engine: Engine, permission: str
) -> None:
    role = _custom_role(owner_session, org, permission)
    _bind(seed_engine, org, None, role_id=role)
    for scopes in (KEY_SCOPES, None):
        pdp = _pdp(s, org, org.mask, key_scopes=scopes)
        assert permission in pdp.effective_permissions(ResourceRef(org.tenant, org.ws, org.p1))
        assert pdp.ancestor_workspace_ids() == NOTHING


def test_project_administrator_at_organization_level_gives_nothing(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    _bind(seed_engine, org, "project_administrator")
    for scopes in (KEY_SCOPES, None):
        pdp = _pdp(s, org, org.mask, key_scopes=scopes)
        assert "project:read" in pdp.effective_permissions(ResourceRef(org.tenant, org.ws, org.p1))
        assert pdp.ancestor_workspace_ids() == NOTHING


def test_a_key_without_workspace_read_in_its_scopes_gets_nothing(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    _bind(seed_engine, org, "tenant_administrator")
    without = _pdp(s, org, org.mask, key_scopes=KEY_SCOPES - {"workspace:read"})
    assert without.ancestor_workspace_ids() == NOTHING
    assert _pdp(s, org, org.mask).ancestor_workspace_ids() == {org.ws}  # the control


def test_workspace_read_at_a_project_is_no_organization_grant_of_it(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    """A project grant of `workspace:read` next to an organization-level grant without it gives
    that project's workspace, as before, and no other workspace of the mask."""
    _bind(seed_engine, org, "project_administrator")
    _bind(seed_engine, org, scope_type="project", scope_id=org.p1)
    mask = ResourceScope(project_ids=frozenset({org.p1, org.p3}))
    assert _pdp(s, org, mask).ancestor_workspace_ids() == {org.ws}


def test_the_other_answers_still_stop_at_the_mask(
    s: Session, org: Org, seed_engine: Engine
) -> None:
    _bind(seed_engine, org, "tenant_administrator")
    pdp = _pdp(s, org, org.mask)
    assert pdp.ancestor_workspace_ids() == {org.ws}
    for ref in (
        ResourceRef(org.tenant, org.ws, org.p2),
        ResourceRef(org.tenant, org.ws),
        ResourceRef(org.tenant, org.ws2),
        ResourceRef(org.tenant, org.ws2, org.p3),
        ResourceRef(org.tenant),
    ):
        for action in sorted(KEY_SCOPES):
            d = pdp.authorize(action, ref)
            assert not d.allowed and d.reason == "resource_scope", (action, ref)
        assert pdp.effective_permissions(ref) == set()
        assert pdp.missing_permissions(KEY_SCOPES, ref) == KEY_SCOPES
    in_p1 = ResourceRef(org.tenant, org.ws, org.p1)
    assert pdp.effective_permissions(in_p1) == KEY_SCOPES
    for action in sorted(KEY_SCOPES):
        assert pdp.visible_scopes(action) == ScopeFilter(False, NOTHING, frozenset({org.p1}))
