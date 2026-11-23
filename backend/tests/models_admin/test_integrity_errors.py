"""`models_admin.service.provider_integrity_error` on real database refusals (issue #205, B-3121).

Each refusal is a real `IntegrityError` from `repository.insert_provider` on the schema owner's
session (BYPASSRLS, so the guards and the plain CHECK constraints answer, not row-level security),
rolled back after each. A `model_providers` guard refusal (23514) gets the code of the guard its
fixed RAISE text names; any other 23514 is `validation_error`; the message is always the service's
own constant, never ids, values, or table or constraint names.
"""

from __future__ import annotations

from typing import Any  # Any: provider column values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.core.errors import AppError, ValidationFailed, VendorNotVisible
from shifde.core.ids import uuid7
from shifde.models_admin import repository
from shifde.models_admin.service import PROVIDER_CONNECTION_SCOPE, provider_integrity_error
from tests.models_admin.conftest import Pair, provider_values
from tests.tenancy.conftest import World


def _workspace_connection(o: Session, tenant: UUID, ws: UUID) -> UUID:
    cid = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, workspace_id, name, connection_type, "
            "adapter_key) VALUES (:i, :t, :w, :n, 'model_api', 'fake')"
        ),
        {"i": cid, "t": tenant, "w": ws, "n": f"conn-{cid.hex}"},
    )
    return cid


def _mapped(o: Session, values: dict[str, Any]) -> AppError:
    with pytest.raises(IntegrityError) as err:
        repository.insert_provider(o, values)
    o.rollback()
    assert getattr(err.value.orig, "sqlstate", None) == "23514"
    mapped = provider_integrity_error(err.value)
    assert mapped is not None
    return mapped


def _assert(err: AppError, cls: type[AppError], code: str, message: str) -> None:
    assert type(err) is cls
    assert (err.status, err.code, err.detail) == (422, code, message)


def test_bs_private_vendor_is_vendor_not_visible(pair: Pair, owner_session: Session) -> None:
    values = provider_values(pair.a.tenant, pair.a.connection, vendor_id=pair.b.vendor)
    _assert(
        _mapped(owner_session, values),
        VendorNotVisible,
        "vendor_not_visible",
        "vendor is not visible to the provider tenant",
    )


def test_a_workspace_connection_is_the_connection_scope_error(
    pair: Pair, world: World, owner_session: Session
) -> None:
    o = owner_session
    cid = _workspace_connection(o, pair.a.tenant, world.a.ws)
    o.commit()
    _assert(
        _mapped(o, provider_values(pair.a.tenant, cid)),
        ValidationFailed,
        "validation_error",
        PROVIDER_CONNECTION_SCOPE,
    )


def test_a_plain_check_is_validation_error(pair: Pair, owner_session: Session) -> None:
    values = provider_values(pair.a.tenant, pair.a.connection, name="")
    _assert(
        _mapped(owner_session, values),
        ValidationFailed,
        "validation_error",
        "the model provider does not meet a database check",
    )
