"""models_admin repository behaviour beyond isolation: the SHI-provider switch and the ledger's
keys for shared models (DECISIONS B-1701, B-1705)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.ledger import pricing
from shifde.models_admin import repository
from tests.models_admin.conftest import Pair, make_shi, model_values, provider_values
from tests.tenancy.conftest import app_session


def test_the_shi_switch_is_versioned_and_records_who_changed_it(
    app_db: Database, pair: Pair
) -> None:
    a = pair.a.tenant
    with app_session(app_db, a) as s, platform_admin_context(s):
        on = repository.set_shi_access(
            s, tenant_id=a, enabled=True, changed_by=pair.a.c.staff, expected=None
        )
        assert on is not None and on["row_version"] == 1 and on["changed_by"] == pair.a.c.staff
        stale = repository.set_shi_access(
            s, tenant_id=a, enabled=False, changed_by=None, expected=5
        )
        assert stale is None
        off = repository.set_shi_access(s, tenant_id=a, enabled=False, changed_by=None, expected=1)
        assert off is not None and off["enabled"] is False and off["row_version"] == 2
        s.commit()
    with app_session(app_db, a) as s:
        assert repository.shi_enabled(s, tenant_id=a) is False


def test_ensure_shi_provider_is_idempotent_and_disabled(app_db: Database, pair: Pair) -> None:
    make_shi(app_db)
    with app_session(app_db, None) as s, platform_admin_context(s):
        repository.ensure_shi_provider(s, adapter_key="other", settings={}, created_by=None)
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
