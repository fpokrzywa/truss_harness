"""`connections.service.integrity_error` on real database refusals (issue #205, B-3121).

Each refusal is a real `IntegrityError` from `repository.insert_connection` on the schema owner's
session (BYPASSRLS, so the guard and the plain CHECK constraints answer, not row-level security),
rolled back after each. A `connections_guard` refusal (23514) gets the code of the guard its fixed
RAISE text names; any other 23514 is `validation_error`; the message is always the service's own
constant, never ids, values, or table or constraint names.
"""

from __future__ import annotations

from typing import Any  # Any: connection column values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.connections import repository
from shifde.connections.service import integrity_error
from shifde.core.errors import (
    AppError,
    EgressPolicyMismatch,
    SecretScopeMismatch,
    ValidationFailed,
    VendorNotVisible,
)
from shifde.core.ids import uuid7
from tests.connections.conftest import Pair, connection_values
from tests.connections.routes import SECRET_PREFIX
from tests.tenancy.conftest import World


def _workspace_secret(o: Session, tenant: UUID, ws: UUID) -> UUID:
    sid = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref, "
            "scope_type, scope_id) VALUES (:i, :t, :n, 'env', 'SHIFDE_TEST_UNUSED', "
            "'workspace', :w)"
        ),
        {"i": sid, "t": tenant, "n": f"{SECRET_PREFIX}{sid.hex}", "w": ws},
    )
    return sid


def _mapped(o: Session, values: dict[str, Any]) -> AppError:
    with pytest.raises(IntegrityError) as err:
        repository.insert_connection(o, values)
    o.rollback()
    assert getattr(err.value.orig, "sqlstate", None) == "23514"
    mapped = integrity_error(err.value)
    assert mapped is not None
    return mapped


def _assert(err: AppError, cls: type[AppError], code: str, message: str) -> None:
    assert type(err) is cls
    assert (err.status, err.code, err.detail) == (422, code, message)


def test_bs_secret_is_secret_scope_mismatch(pair: Pair, owner_session: Session) -> None:
    values = connection_values(pair.a.tenant, secret_ref_id=pair.b.secret, auth_method="bearer")
    _assert(
        _mapped(owner_session, values),
        SecretScopeMismatch,
        "secret_scope_mismatch",
        "secret must belong to the connection tenant",
    )


def test_a_workspace_secret_on_a_client_connection_is_secret_scope_mismatch(
    pair: Pair, world: World, owner_session: Session
) -> None:
    o = owner_session
    sid = _workspace_secret(o, pair.a.tenant, world.a.ws)
    o.commit()
    values = connection_values(pair.a.tenant, secret_ref_id=sid, auth_method="bearer")
    _assert(
        _mapped(o, values),
        SecretScopeMismatch,
        "secret_scope_mismatch",
        "secret scope must match the connection scope",
    )


def test_bs_egress_policy_is_egress_policy_mismatch(pair: Pair, owner_session: Session) -> None:
    values = connection_values(pair.a.tenant, egress_policy_id=pair.b.policy)
    _assert(
        _mapped(owner_session, values),
        EgressPolicyMismatch,
        "egress_policy_mismatch",
        "egress policy must belong to the connection tenant",
    )


def test_bs_private_vendor_is_vendor_not_visible(pair: Pair, owner_session: Session) -> None:
    values = connection_values(pair.a.tenant, vendor_id=pair.b.vendor)
    _assert(
        _mapped(owner_session, values),
        VendorNotVisible,
        "vendor_not_visible",
        "vendor is not visible to the connection tenant",
    )


def test_a_plain_check_is_validation_error(pair: Pair, owner_session: Session) -> None:
    values = connection_values(pair.a.tenant, name="")
    _assert(
        _mapped(owner_session, values),
        ValidationFailed,
        "validation_error",
        "the connection does not meet a database check",
    )
