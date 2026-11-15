"""Cross-client isolation of the models_admin data layer (DECISIONS B-1701..B-1704).

SQL as `shifde_app` (`app_session` asserts the role) and through `shifde.models_admin.repository`
and `limiter`: client A cannot read, list, update or delete client B's providers, models,
allowlists, routes or limiter state, cannot tell B's ids from ids that do not exist, and cannot
reference B's connection, provider, model, allowlist or private vendor. The SHI-wide provider and
its models (tenant NULL) are readable by every client and writable by none; the per-client switch
for it is readable by its own client only and written only in the platform administrator context.
The keys hold on their own, without the guard trigger and without row-level security (owner).

Routes arrive with part 2 (`service.py`, `api.py`); their cross-client tests arrive with them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any  # Any: repository row values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.models_admin import limiter, repository
from tests.models_admin.conftest import (
    MODULE_TABLES,
    Pair,
    make_shi,
    model_values,
    provider_values,
    route_values,
)
from tests.tenancy.conftest import app_session

SHI = repository.SHI_PROVIDER_ID
SHARED = ("model_providers", "models")


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


def _refused(s: Session, fn: Callable[[], object]) -> tuple[str | None, str]:
    """The SQLSTATE and first message line of a refused statement; rolls back."""
    with pytest.raises(DBAPIError) as err:
        fn()
        s.flush()
    s.rollback()
    return _sqlstate(err), str(err.value.orig).splitlines()[0]


def _same_refusal(s: Session, make: Callable[[UUID], object], foreign: UUID) -> None:
    """Naming `foreign` (another client's id) is refused exactly as naming an unknown id."""
    theirs = _refused(s, lambda: make(foreign))
    missing = _refused(s, lambda: make(uuid7()))
    assert theirs == missing, (theirs, missing)
    assert theirs[0] is not None


def _seed(app_db: Database, pair: Pair) -> dict[str, dict[str, Any]]:
    """Per client, through the repository and limiter as `shifde_app`: a provider, a model, an
    allowlist naming the model, a route, a request window and a concurrency lease."""
    out: dict[str, dict[str, Any]] = {}
    for label, side in (("a", pair.a), ("b", pair.b)):
        t = side.tenant
        with app_session(app_db, t) as s:
            prov = provider_values(t, side.connection, vendor_id=side.vendor)
            repository.insert_provider(s, prov)
            model = model_values(t, prov["id"])
            repository.insert_model(s, model)
            allow = repository.upsert_allowlist(s, t, uuid7(), "tenant", t, True)
            repository.replace_allowlist_models(s, t, allow, [model["id"]])
            route = route_values(t, model["id"])
            repository.insert_route(s, route)
            limiter.take_request(s, t, "bucket", 10)
            s.commit()
            lease = limiter.acquire_lease(s, t, "bucket", 5)
        out[label] = {
            "provider": prov["id"],
            "model": model["id"],
            "allowlist": allow,
            "route": route["id"],
            "lease": lease,
        }
    return out


def _snapshot(owner_db: Database, tenant: UUID) -> dict[str, list[Any]]:
    """Every row of the module's tables that belongs to `tenant`, as the owner."""
    with owner_db.session() as o:
        return {
            table: [
                tuple(r)
                for r in o.execute(
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY 1, 2"),
                    {"t": tenant},
                )
            ]
            for table in MODULE_TABLES
        }


# ------------------------------------------------------------------ read / detect
def test_a_reads_and_lists_only_its_own_rows_and_the_shared_ones(
    app_db: Database, pair: Pair
) -> None:
    ids = _seed(app_db, pair)
    shared_model = make_shi(app_db)
    a, b = pair.a.tenant, pair.b.tenant
    with app_session(app_db, a) as s:
        own = ids["a"]
        assert repository.get_provider(s, own["provider"], tenant_id=a) is not None
        assert repository.get_model(s, own["model"], tenant_id=a) is not None
        assert repository.get_route(s, own["route"], tenant_id=a) is not None
        for tenant in (a, b):  # B's ids are invisible whichever client the key names
            assert repository.get_provider(s, ids["b"]["provider"], tenant_id=tenant) is None
            assert repository.get_model(s, ids["b"]["model"], tenant_id=tenant) is None
            assert repository.get_models(s, [ids["b"]["model"]], tenant_id=tenant) == []
            assert repository.get_route(s, ids["b"]["route"], tenant_id=tenant) is None
            assert repository.allowlist_model_ids(s, ids["b"]["allowlist"], tenant_id=tenant) == []
        assert repository.get_allowlist(s, "tenant", b, tenant_id=a) is None
        assert repository.get_allowlist(s, "tenant", b, tenant_id=b) is None
        providers = repository.list_providers(s, None, 500, tenant_id=a)
        assert sorted(r["id"] for r in providers) == sorted([own["provider"], SHI])
        models = repository.list_models(
            s, tenant_id=a, provider_id=None, lifecycle=None, after=None, limit=500
        )
        assert sorted(r["id"] for r in models) == sorted([own["model"], shared_model])
        assert [r["id"] for r in repository.list_routes(s, None, 500, tenant_id=a)] == [
            own["route"]
        ]
        # keyed to B under A's row-level security: only the shared rows
        assert [r["id"] for r in repository.list_providers(s, None, 500, tenant_id=b)] == [SHI]
        assert repository.list_routes(s, None, 500, tenant_id=b) == []
        assert (
            repository.list_models(
                s,
                tenant_id=b,
                provider_id=ids["b"]["provider"],
                lifecycle=None,
                after=None,
                limit=500,
            )
            == []
        )
        for table in MODULE_TABLES:  # raw SQL: only A's rows, and the shared ones
            tenants = set(
                s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}")).scalars()
            )
            assert tenants <= ({a, None} if table in SHARED else {a}), table
            assert a in tenants or table == "shi_provider_access", table


def test_a_cannot_update_or_delete_bs_rows(
    app_db: Database, owner_db: Database, pair: Pair
) -> None:
    ids = _seed(app_db, pair)
    a, b = pair.a.tenant, pair.b.tenant
    before = _snapshot(owner_db, b)
    with app_session(app_db, a) as s:
        for tenant in (a, b):
            assert (
                repository.update_provider(
                    s, ids["b"]["provider"], None, {"name": "x"}, tenant_id=tenant
                )
                is None
            )
            assert (
                repository.update_model(
                    s, ids["b"]["model"], None, {"display_name": "x"}, tenant_id=tenant
                )
                is None
            )
            assert (
                repository.update_route(s, ids["b"]["route"], None, {"name": "x"}, tenant_id=tenant)
                is None
            )
        repository.replace_allowlist_models(s, b, ids["b"]["allowlist"], [])
        for stmt in (
            "UPDATE shifde.model_providers SET name = 'x' WHERE tenant_id = :b",
            "UPDATE shifde.models SET display_name = 'x' WHERE tenant_id = :b",
            "UPDATE shifde.model_allowlists SET enabled = false WHERE tenant_id = :b",
            "UPDATE shifde.model_routes SET name = 'x' WHERE tenant_id = :b",
            "UPDATE shifde.rate_limit_windows SET requests = 0 WHERE tenant_id = :b",
            "DELETE FROM shifde.model_allowlist_models WHERE tenant_id = :b",
            "DELETE FROM shifde.rate_limit_windows WHERE tenant_id = :b",
            "DELETE FROM shifde.concurrency_leases WHERE tenant_id = :b",
        ):
            assert s.execute(text(stmt), {"b": b}).rowcount == 0, stmt
        s.commit()
        limiter.release_lease(s, a, ids["b"]["lease"])
        limiter.release_lease(s, b, ids["b"]["lease"])
        # B's request window does not count against A's limit
        limiter.take_request(s, a, "bucket", 2)
        s.rollback()
    assert _snapshot(owner_db, b) == before


# ------------------------------------------------------------------ reference
def test_a_cannot_reference_bs_rows(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    shared_model = make_shi(app_db)
    a = pair.a.tenant
    own_conn = pair.a.connection
    with app_session(app_db, a) as s:
        _same_refusal(
            s, lambda c: repository.insert_provider(s, provider_values(a, c)), pair.b.connection
        )
        _same_refusal(
            s,
            lambda v: repository.insert_provider(s, provider_values(a, own_conn, vendor_id=v)),
            pair.b.vendor,
        )
        _same_refusal(
            s, lambda p: repository.insert_model(s, model_values(a, p)), ids["b"]["provider"]
        )
        # a client model cannot hang under the shared provider either
        _same_refusal(s, lambda p: repository.insert_model(s, model_values(a, p)), SHI)
        allow = ids["a"]["allowlist"]
        _same_refusal(
            s, lambda m: repository.replace_allowlist_models(s, a, allow, [m]), ids["b"]["model"]
        )
        _same_refusal(
            s,
            lambda al: repository.replace_allowlist_models(s, a, al, [ids["a"]["model"]]),
            ids["b"]["allowlist"],
        )
        _same_refusal(
            s, lambda m: repository.insert_route(s, route_values(a, m)), ids["b"]["model"]
        )
        _same_refusal(
            s,
            lambda m: repository.update_route(
                s, ids["a"]["route"], None, {"primary_model_id": m}, tenant_id=a
            ),
            ids["b"]["model"],
        )
        # composite keys: a client's allowlist or route cannot name a shared model (part 2 decides
        # how a client routes to the SHI provider, B-1702)
        _same_refusal(s, lambda m: repository.insert_route(s, route_values(a, m)), shared_model)
        # a global catalog vendor is fine
        repository.insert_provider(s, provider_values(a, own_conn, vendor_id=pair.global_vendor))
        s.rollback()
        # rows filed under B (or under no client) from A's context: row-level security
        b_conn = pair.b.connection
        for make in (
            lambda: repository.insert_provider(s, provider_values(pair.b.tenant, b_conn)),
            lambda: repository.insert_model(s, model_values(pair.b.tenant, ids["b"]["provider"])),
            lambda: repository.insert_route(s, route_values(pair.b.tenant, ids["b"]["model"])),
            lambda: repository.upsert_allowlist(s, pair.b.tenant, uuid7(), "tenant", a, True),
            lambda: limiter.take_request(s, pair.b.tenant, "bucket", 10),
            lambda: limiter.add_tokens(s, pair.b.tenant, "bucket", 5),
        ):
            assert _refused(s, make)[0] == "42501"


# ------------------------------------------------------------------ the SHI-wide provider
def test_a_client_cannot_create_or_change_the_shared_provider(
    app_db: Database, owner_db: Database, pair: Pair
) -> None:
    a = pair.a.tenant
    with app_session(app_db, a) as s:
        shi = dict(adapter_key="fake", settings={}, created_by=None)
        assert _refused(s, lambda: repository.ensure_shi_provider(s, **shi))[0] == "42501"
        stray = provider_values(None, None)
        assert _refused(s, lambda: repository.insert_provider(s, stray))[0] == "42501"
    shared_model = make_shi(app_db, a)  # the platform administrator, in A's context
    with owner_db.session() as o:
        before = [tuple(r) for r in o.execute(text("SELECT * FROM shifde.model_providers"))]
        models_before = [tuple(r) for r in o.execute(text("SELECT * FROM shifde.models"))]
    with app_session(app_db, a) as s:
        assert repository.get_provider(s, SHI, tenant_id=a) is not None  # readable
        assert repository.get_model(s, shared_model, tenant_id=a) is not None
        assert (
            repository.update_provider(s, SHI, None, {"status": "active"}, tenant_id=None) is None
        )
        assert (
            repository.update_model(s, shared_model, None, {"lifecycle": "retired"}, tenant_id=None)
            is None
        )
        for stmt in (
            "UPDATE shifde.model_providers SET status = 'active' WHERE tenant_id IS NULL",
            "UPDATE shifde.models SET lifecycle = 'retired' WHERE tenant_id IS NULL",
        ):
            assert s.execute(text(stmt)).rowcount == 0, stmt
        more = model_values(None, SHI)
        assert _refused(s, lambda: repository.insert_model(s, more))[0] == "42501"
        # claiming the shared row's id for a client row collides only on the server-minted id
        clash = provider_values(a, pair.a.connection, id=SHI)
        assert _refused(s, lambda: repository.insert_provider(s, clash))[0] == "23505"
        s.commit()
    with owner_db.session() as o:
        assert [tuple(r) for r in o.execute(text("SELECT * FROM shifde.model_providers"))] == before
        assert [tuple(r) for r in o.execute(text("SELECT * FROM shifde.models"))] == models_before


def test_only_the_platform_administrator_switches_the_shared_provider_for_a_client(
    app_db: Database, pair: Pair
) -> None:
    a, b = pair.a.tenant, pair.b.tenant
    with app_session(app_db, a) as s:
        assert repository.shi_enabled(s, tenant_id=a) is False  # off by default
        on = dict(tenant_id=a, enabled=True, changed_by=None, expected=None)
        assert _refused(s, lambda: repository.set_shi_access(s, **on))[0] == "42501"
    with app_session(app_db, a) as s, platform_admin_context(s):
        row = repository.set_shi_access(
            s, tenant_id=a, enabled=True, changed_by=None, expected=None
        )
        assert row is not None and row["enabled"] is True
        s.commit()
        # even the platform administrator writes a client's switch only in that client's context
        other = dict(tenant_id=b, enabled=True, changed_by=None, expected=None)
        assert _refused(s, lambda: repository.set_shi_access(s, **other))[0] == "42501"
    with app_session(app_db, a) as s:
        assert repository.shi_enabled(s, tenant_id=a) is True  # the owner sees the switch
        off = dict(tenant_id=a, enabled=False, changed_by=None, expected=None)
        assert _refused(s, lambda: repository.set_shi_access(s, **off))[0] == "42501"
        stmt = "UPDATE shifde.shi_provider_access SET enabled = false WHERE tenant_id = :a"
        assert s.execute(text(stmt), {"a": a}).rowcount == 0
        delete = text("DELETE FROM shifde.shi_provider_access")
        assert _refused(s, lambda: s.execute(delete))[0] == "42501"  # no DELETE grant
    with app_session(app_db, b) as s:
        assert repository.get_shi_access(s, tenant_id=a) is None
        assert repository.shi_enabled(s, tenant_id=a) is False
        assert repository.shi_enabled(s, tenant_id=b) is False
        assert s.execute(text("SELECT count(*) FROM shifde.shi_provider_access")).scalar_one() == 0
    with app_session(app_db, a) as s:
        assert repository.shi_enabled(s, tenant_id=a) is True


# ------------------------------------------------------------------ keys without RLS or guard
def test_the_keys_hold_without_the_guard_or_rls(
    app_db: Database, owner_db: Database, pair: Pair
) -> None:
    ids = _seed(app_db, pair)
    a = pair.a.tenant
    with owner_db.session() as o:
        o.execute(
            text("ALTER TABLE shifde.model_providers DISABLE TRIGGER trg_model_providers_guard")
        )
        cases: list[tuple[str, Callable[[], object]]] = [
            ("23503", lambda: repository.insert_provider(o, provider_values(a, pair.b.connection))),
            ("23503", lambda: repository.insert_model(o, model_values(a, ids["b"]["provider"]))),
            ("23503", lambda: repository.insert_model(o, model_values(a, SHI))),
            ("23503", lambda: repository.insert_route(o, route_values(a, ids["b"]["model"]))),
            (
                "23503",
                lambda: repository.replace_allowlist_models(
                    o, a, ids["a"]["allowlist"], [ids["b"]["model"]]
                ),
            ),
            # shared rows: only the SHI id, never with a connection, models only under it
            ("23514", lambda: repository.insert_provider(o, provider_values(None, None))),
            (
                "23514",
                lambda: repository.insert_provider(
                    o, provider_values(None, pair.a.connection, id=SHI)
                ),
            ),
            ("23514", lambda: repository.insert_provider(o, provider_values(a, None))),
            ("23514", lambda: repository.insert_model(o, model_values(None, ids["a"]["provider"]))),
            ("23514", lambda: repository.insert_model(o, model_values(None, SHI))),  # no SHI row
        ]
        for state, make in cases:
            with pytest.raises(DBAPIError) as err, o.begin_nested():
                make()
            assert _sqlstate(err) == state
        o.rollback()


def test_the_guard_refuses_bs_vendor_as_the_owner(owner_db: Database, pair: Pair) -> None:
    a = pair.a.tenant
    with owner_db.session() as o:
        values = provider_values(a, pair.a.connection, vendor_id=pair.b.vendor)
        with pytest.raises(DBAPIError) as err:
            repository.insert_provider(o, values)
        assert _sqlstate(err) == "23514"
        o.rollback()
        shared = provider_values(None, None, id=SHI, vendor_id=pair.a.vendor)
        with pytest.raises(DBAPIError) as err:
            repository.insert_provider(o, shared)
        assert _sqlstate(err) == "23514"
