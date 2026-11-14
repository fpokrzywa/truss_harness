"""Platform-only permissions never come from a role binding (DECISIONS B-406, authz isolation
review 1, follow-up 1).

``platform:admin`` (every key in ``PLATFORM_ONLY_ACTIONS``) administers the whole installation,
every client included. It comes only from ``users.is_platform_admin`` (``PDP._is_platform_admin``),
never from a role. Two layers:

* database (`authz_0002`): a client role (`role_permissions.tenant_id` NOT NULL) cannot hold a
  platform-only permission, for `shifde_app` and for the schema owner alike;
* PDP: grants loaded through role bindings drop platform-only permissions, so `effective_permissions`,
  `missing_permissions`, `visible_scopes` and `ancestor_workspace_ids` agree with `authorize` for a
  binding to the shared `platform_administrator` system template (which the check does not cover:
  its `tenant_id` is NULL) and for a client role row that predates the check (simulated by
  stubbing `repository.load_grants`, since the check cannot be bypassed once it exists).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.authz import repository
from shifde.authz.pdp import PLATFORM_ONLY_ACTIONS
from shifde.authz.service import PDP, GrantRow, ResourceRef, ScopeFilter, new_pdp
from shifde.core import db
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.errors import Forbidden
from shifde.core.ids import uuid7
from tests._support.tenants import add_tenants
from tests.authz.conftest import owner_insert_bindings

WHO = text("SELECT current_user")
CHECK_NAME = "ck_role_permissions__no_platform_only_in_client_role"
PLATFORM_ONLY = sorted(PLATFORM_ONLY_ACTIONS)


@pytest.fixture
def s(app_db: Database) -> Iterator[Session]:
    with app_db.session() as session:
        assert session.execute(WHO).scalar_one() == "shifde_app"
        yield session


def _check_violation(exc: IntegrityError) -> tuple[str | None, str | None]:
    diag = getattr(exc.orig, "diag", None)
    return getattr(exc.orig, "sqlstate", None), getattr(diag, "constraint_name", None)


# ------------------------------------------------------------------ database
@pytest.mark.parametrize("key", PLATFORM_ONLY)
def test_a_client_role_cannot_hold_a_platform_only_permission_as_shifde_app(
    s: Session, owner_db: Database, key: str
) -> None:
    tenant_a, role = uuid7(), uuid7()
    with owner_db.session() as owner:
        add_tenants(owner, tenant_a)
        owner.commit()
    db.set_tenant(s, tenant_a)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    s.execute(
        text("INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:id, :t, 'c', 'c')"),
        {"id": role, "t": tenant_a},
    )
    insert = text(
        "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
        "VALUES (:r, :k, :t)"
    )
    s.execute(insert, {"r": role, "k": "workspace:read", "t": tenant_a})  # control: allowed
    with pytest.raises(IntegrityError) as caught, s.begin_nested():
        s.execute(insert, {"r": role, "k": key, "t": tenant_a})
    assert _check_violation(caught.value) == ("23514", CHECK_NAME)
    held = s.execute(
        text("SELECT permission_key FROM shifde.role_permissions WHERE role_id = :r"), {"r": role}
    ).scalars()
    assert set(held) == {"workspace:read"}
    s.rollback()


@pytest.mark.parametrize("key", PLATFORM_ONLY)
def test_the_schema_owner_cannot_put_a_platform_only_permission_in_a_client_role(
    owner_session: Session, key: str
) -> None:
    tenant_a, role = uuid7(), uuid7()
    add_tenants(owner_session, tenant_a)
    owner_session.execute(
        text("INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:id, :t, 'c', 'c')"),
        {"id": role, "t": tenant_a},
    )
    with pytest.raises(IntegrityError) as caught:
        owner_session.execute(
            text(
                "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
                "VALUES (:r, :k, :t)"
            ),
            {"r": role, "k": key, "t": tenant_a},
        )
    assert _check_violation(caught.value) == ("23514", CHECK_NAME)
    owner_session.rollback()


def test_the_check_is_validated_and_system_templates_keep_platform_admin(
    owner_session: Session,
) -> None:
    validated = owner_session.execute(
        text(
            "SELECT convalidated FROM pg_constraint WHERE conname = :n "
            "AND conrelid = 'shifde.role_permissions'::regclass"
        ),
        {"n": CHECK_NAME},
    ).scalar_one()
    assert validated is True  # every existing row, system templates included, satisfies it
    holders = owner_session.execute(
        text(
            "SELECT r.key, rp.tenant_id FROM shifde.role_permissions rp "
            "JOIN shifde.roles r ON r.id = rp.role_id WHERE rp.permission_key = 'platform:admin'"
        )
    ).all()
    assert [(k, t) for k, t in holders if t is None] == [("platform_administrator", None)]
    assert all(t is None for _, t in holders)


# ------------------------------------------------------------------ PDP
def _refs(tenant: UUID, ws: UUID, proj: UUID) -> list[ResourceRef]:
    return [ResourceRef(tenant), ResourceRef(tenant, ws), ResourceRef(tenant, ws, proj)]


def _assert_no_platform_only(pdp: PDP, tenant: UUID, ws: UUID, proj: UUID) -> None:
    for ref in _refs(tenant, ws, proj):
        held = pdp.effective_permissions(ref)
        assert not held & PLATFORM_ONLY_ACTIONS, held
        assert pdp.missing_permissions(PLATFORM_ONLY_ACTIONS, ref) == set(PLATFORM_ONLY_ACTIONS)
    for action in PLATFORM_ONLY_ACTIONS:
        d = pdp.authorize(action)
        assert not d.allowed and d.reason == "not_platform_admin"
        with pytest.raises(Forbidden):
            pdp.require(action)
        assert pdp.visible_scopes(action) == ScopeFilter()


def test_a_binding_to_the_platform_administrator_template_grants_nothing_platform_only(
    s: Session, seed_engine: Engine
) -> None:
    """The shared template holds `platform:admin` with tenant_id NULL, so the check does not
    stop a client binding it; the PDP must still never report it for a non-admin."""
    tenant, ws, proj, uid = uuid7(), uuid7(), uuid7(), uuid7()
    owner_insert_bindings(
        seed_engine,
        [
            {
                "tenant_id": tenant,
                "subject_id": uid,
                "role_key": "platform_administrator",
                "scope_type": scope,
                "scope_id": scope_id,
            }
            for scope, scope_id in (("tenant", tenant), ("workspace", ws), ("project", proj))
        ],
    )
    db.set_tenant(s, tenant)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    pdp = new_pdp(s, Principal(uid, "u@x", "U"), tenant)
    _assert_no_platform_only(pdp, tenant, ws, proj)
    assert pdp.ancestor_workspace_ids() == frozenset()  # platform-only grants are not grants
    # an API key whose scopes name platform:admin gains nothing either
    key = Principal(
        uid, "u@x", "U", credential="api_key", tenant_id=tenant, key_scopes=PLATFORM_ONLY_ACTIONS
    )
    _assert_no_platform_only(new_pdp(s, key, tenant), tenant, ws, proj)


def test_a_client_role_holding_platform_admin_from_before_the_check_grants_nothing(
    s: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client-A custom role row granting every platform-only key (as it could before
    authz_0002), bound at every scope, next to an ordinary grant; the check makes such a row
    impossible now, so the grants are stubbed at the repository."""
    tenant, ws, proj, uid, binding = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    rows = [
        GrantRow(binding, scope, scope_id, key, None)
        for scope, scope_id in (("tenant", tenant), ("workspace", ws), ("project", proj))
        for key in (*PLATFORM_ONLY, "workspace:read")
    ]
    monkeypatch.setattr(repository, "load_grants", lambda *_a, **_k: list(rows))
    db.set_tenant(s, tenant)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    for admin_flag_on_a_key in (False, True):
        principal = (
            Principal(
                uid, "u@x", "U", True, credential="api_key", tenant_id=tenant, key_scopes=None
            )
            if admin_flag_on_a_key  # an API key never carries the admin flag (authorize agrees)
            else Principal(uid, "u@x", "U")
        )
        pdp = new_pdp(s, principal, tenant)
        _assert_no_platform_only(pdp, tenant, ws, proj)
        for ref in _refs(tenant, ws, proj):  # the ordinary grant is unaffected
            assert pdp.effective_permissions(ref) == {"workspace:read"}
            assert pdp.authorize("workspace:read", ref).allowed
        assert pdp.visible_scopes("workspace:read").tenant_wide


def test_the_platform_admin_flag_still_grants_platform_admin_and_nothing_else(
    s: Session, seed_engine: Engine
) -> None:
    tenant, ws, proj, uid = uuid7(), uuid7(), uuid7(), uuid7()
    owner_insert_bindings(
        seed_engine,
        [
            {
                "tenant_id": tenant,
                "subject_id": uid,
                "role_key": "platform_administrator",
                "scope_type": "tenant",
                "scope_id": tenant,
            }
        ],
    )
    db.set_tenant(s, tenant)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    pdp = new_pdp(s, Principal(uid, "u@x", "U", True), tenant)
    assert pdp.authorize("platform:admin").allowed
    for ref in _refs(tenant, ws, proj):
        assert pdp.effective_permissions(ref) == {"platform:admin"}
        assert pdp.missing_permissions(PLATFORM_ONLY_ACTIONS, ref) == set()
    # a platform-only action is not a client scope: no list predicate for anyone
    assert pdp.visible_scopes("platform:admin") == ScopeFilter()
    assert pdp.ancestor_workspace_ids() == frozenset()
    # the admin flag does not reach another client through this PDP
    assert pdp.effective_permissions(ResourceRef(uuid7())) == set()


def test_a_client_a_role_cannot_make_a_non_admin_platform_admin_in_client_b(
    s: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-client: a platform-only grant stubbed into client A's role yields nothing when the
    same principal asks inside client B, nor about B's resources from A."""
    a, b, ws, proj, uid = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    rows = [GrantRow(uuid7(), "tenant", a, k, None) for k in PLATFORM_ONLY]
    monkeypatch.setattr(
        repository,
        "load_grants",
        lambda *_a, tenant_id, **_k: list(rows) if tenant_id == a else [],
    )
    db.set_tenant(s, a)
    assert s.execute(WHO).scalar_one() == "shifde_app"
    pa = new_pdp(s, Principal(uid, "u@x", "U"), a)
    for ref in _refs(b, ws, proj):
        assert pa.effective_permissions(ref) == set()
        assert pa.missing_permissions(PLATFORM_ONLY_ACTIONS, ref) == set(PLATFORM_ONLY_ACTIONS)
    _assert_no_platform_only(pa, a, ws, proj)
    db.set_tenant(s, b)
    pb = new_pdp(s, Principal(uid, "u@x", "U"), b)
    _assert_no_platform_only(pb, b, ws, proj)
