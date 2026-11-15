"""models_admin repository behaviour beyond isolation: the SHI-provider switch and the ledger's
keys for shared models (DECISIONS B-1701, B-1705)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.ledger import pricing
from shifde.models_admin import repository
from tests.models_admin.conftest import Pair, make_shi, model_values, provider_values
from tests.tenancy.conftest import app_session


def test_the_shi_switch_is_versioned_and_records_who_changed_it(
    app_db: Database, pair: Pair
) -> None:
    """The author is the database's acting principal, never a caller-supplied value
    (models_admin review 1 item 5, B-1713)."""
    a, staff = pair.a.tenant, pair.a.c.staff
    with app_session(app_db, a, staff) as s, platform_admin_context(s):
        on = repository.set_shi_access(s, tenant_id=a, enabled=True, expected=None)
        assert on is not None and on["row_version"] == 1 and on["changed_by"] == staff
        stale = repository.set_shi_access(s, tenant_id=a, enabled=False, expected=5)
        assert stale is None
        off = repository.set_shi_access(s, tenant_id=a, enabled=False, expected=1)
        assert off is not None and off["enabled"] is False and off["row_version"] == 2
        assert off["changed_by"] == staff
        s.commit()
    with app_session(app_db, a) as s:
        assert repository.shi_enabled(s, tenant_id=a) is False


def test_a_switch_change_needs_an_acting_principal_who_is_its_author(
    app_db: Database, pair: Pair
) -> None:
    a, staff, other = pair.a.tenant, pair.a.c.staff, pair.a.c.member
    with app_session(app_db, a) as s, platform_admin_context(s):  # no principal
        with pytest.raises(DBAPIError) as err:
            repository.set_shi_access(s, tenant_id=a, enabled=True, expected=None)
        assert err.value.orig is not None and err.value.orig.sqlstate == "42501"
        s.rollback()
    with app_session(app_db, a, staff) as s, platform_admin_context(s):
        repository.set_shi_access(s, tenant_id=a, enabled=True, expected=None)
        s.commit()
        # naming someone else as the author is refused by the policies
        for stmt in (
            "UPDATE shifde.shi_provider_access SET changed_by = :o WHERE tenant_id = :a",
            "INSERT INTO shifde.shi_provider_access (tenant_id, enabled, changed_by) "
            "VALUES (:a, true, :o) ON CONFLICT (tenant_id) DO UPDATE SET changed_by = :o",
        ):
            with pytest.raises(DBAPIError) as err, s.begin_nested():
                s.execute(text(stmt), {"a": a, "o": other})
            assert err.value.orig is not None and err.value.orig.sqlstate == "42501", stmt
        s.rollback()
    with app_session(app_db, a) as s:
        row = repository.get_shi_access(s, tenant_id=a)
        assert row is not None and row["changed_by"] == staff


def test_ensure_shi_provider_is_idempotent_and_disabled(app_db: Database, pair: Pair) -> None:
    make_shi(app_db)
    with app_session(app_db, None) as s, platform_admin_context(s):
        row = repository.ensure_shi_provider(s, adapter_key="other", created_by=None)
        assert row["tenant_id"] is None and row["adapter_key"] == "fake"  # read back
        s.commit()
    with app_session(app_db, pair.a.tenant) as s:
        row = repository.get_provider(s, repository.SHI_PROVIDER_ID, tenant_id=pair.a.tenant)
        assert row is not None
        assert (row["tenant_id"], row["adapter_key"], row["status"]) == (None, "fake", "disabled")


def test_the_ledger_prices_own_and_shared_models_but_not_another_clients(
    app_db: Database, owner_session: Session, pair: Pair
) -> None:
    shared = make_shi(app_db)
    a, b = pair.a.tenant, pair.b.tenant
    with app_session(app_db, b) as s:
        prov = provider_values(b, pair.b.connection, adapter_key="openai")
        repository.insert_provider(s, prov)
        theirs = model_values(b, prov["id"], model_key="b-model")
        repository.insert_model(s, theirs)
        s.commit()
    # the ledger's own key holds without row-level security (schema owner)
    assert pricing.model_keys(owner_session, a, shared) == ("fake", "shi-model")
    assert pricing.model_keys(owner_session, b, theirs["id"]) == ("openai", "b-model")
    assert pricing.model_keys(owner_session, a, theirs["id"]) is None
    with app_session(app_db, a) as s:
        assert pricing.model_keys(s, a, shared) == ("fake", "shi-model")
        assert pricing.model_keys(s, a, theirs["id"]) is None
        assert pricing.model_keys(s, b, theirs["id"]) is None
