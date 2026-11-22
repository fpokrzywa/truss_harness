"""Cross-client isolation of the connections data layer (DECISIONS B-1501..B-1503).

SQL as `shifde_app` (`app_session` asserts the role) and through `shifde.connections.repository`:
client A cannot read, list, insert, update or delete client B's connections, test history,
discoveries or egress policies, cannot tell B's ids from ids that do not exist, and cannot bind
B's secret, egress policy or private vendor (nor an installation secret or the installation
egress default) to a connection. The composite keys hold on their own, without the guard trigger
and without row-level security (schema owner).

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

from shifde.connections import repository
from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from tests.connections.conftest import (
    Pair,
    check_values,
    connection_values,
    discovery_values,
)
from tests.tenancy.conftest import app_session

TABLES = ("connections", "connection_tests", "connection_discoveries", "egress_policies")


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


def _seed(app_db: Database, pair: Pair) -> dict[str, dict[str, UUID]]:
    """Per client: a connection bound to its own secret, policy and private vendor, one test and
    one discovery, all written through the repository as `shifde_app`."""
    out: dict[str, dict[str, UUID]] = {}
    for label, side in (("a", pair.a), ("b", pair.b)):
        with app_session(app_db, side.tenant) as s:
            values = connection_values(
                side.tenant,
                secret_ref_id=side.secret,
                egress_policy_id=side.policy,
                vendor_id=side.vendor,
                auth_method="bearer",
            )
            repository.insert_connection(s, values)
            test = check_values(side.tenant, values["id"])
            repository.insert_test(s, test)
            disc = discovery_values(side.tenant, values["id"])
            repository.insert_discovery(s, disc)
            s.commit()
        out[label] = {"connection": values["id"], "test": test["id"], "discovery": disc["id"]}
    return out


def _list(s: Session, tenant: UUID) -> list[Any]:
    return repository.list_connections(
        s,
        tenant_id=tenant,
        connection_type=None,
        status=None,
        workspace_id=None,
        project_id=None,
        include_shared=False,
        tenant_wide=True,
        visible_workspaces=[],
        visible_projects=[],
        after=None,
        limit=500,
    )


# ------------------------------------------------------------------ read / detect
def test_a_reads_and_lists_only_its_own_rows(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    a, b = pair.a.tenant, pair.b.tenant
    bc = ids["b"]["connection"]
    with app_session(app_db, a) as s:
        assert repository.get_connection(s, ids["a"]["connection"], tenant_id=a) is not None
        # B's id is invisible whichever client the key names
        assert repository.get_connection(s, bc, tenant_id=a) is None
        assert repository.get_connection(s, bc, tenant_id=b) is None
        assert [r["id"] for r in _list(s, a)] == [ids["a"]["connection"]]
        assert _list(s, b) == []
        for tenant in (a, b):
            assert repository.list_tests(s, bc, None, 50, tenant_id=tenant) == []
            assert repository.latest_test(s, bc, tenant_id=tenant) is None
            assert repository.latest_discovery(s, bc, tenant_id=tenant) is None
            assert repository.get_egress_policy(s, pair.b.policy, tenant_id=tenant) is None
            assert repository.list_egress_policies(s, b, None, 50) == []
        own = repository.latest_test(s, ids["a"]["connection"], tenant_id=a)
        assert own is not None and own["id"] == ids["a"]["test"]
        for table in TABLES:  # raw SQL: only A's rows (and the installation egress default)
            tenants = set(
                s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}")).scalars()
            )
            assert tenants <= {a, None}, table
            assert a in tenants, table


def test_the_service_key_holds_without_row_level_security(
    app_db: Database, pair: Pair, owner_session: Session
) -> None:
    """An owner/maintenance session (BYPASSRLS) still gets nothing for B's ids keyed to A."""
    ids = _seed(app_db, pair)
    a, bc = pair.a.tenant, ids["b"]["connection"]
    o = owner_session
    b_name = o.execute(
        text("SELECT name FROM shifde.connections WHERE id = :i"), {"i": bc}
    ).scalar_one()
    assert repository.get_connection(o, bc, tenant_id=a) is None
    assert repository.latest_test(o, bc, tenant_id=a) is None
    assert repository.list_tests(o, bc, None, 50, tenant_id=a) == []
    assert repository.latest_discovery(o, bc, tenant_id=a) is None
    assert repository.get_egress_policy(o, pair.b.policy, tenant_id=a) is None
    assert repository.get_egress_policy(o, pair.b.policy, tenant_id=None) is None
    assert repository.get_default_policy(o, a) is None
    # own rows first (review 1 item 2): the same calls keyed to A change A's rows
    ac = ids["a"]["connection"]
    own = repository.update_connection(o, ac, None, {"name": f"own-{ac.hex}"}, tenant_id=a)
    assert own is not None and own["name"] == f"own-{ac.hex}"
    own_pol = repository.update_egress_policy(
        o, pair.a.policy, None, {"name": f"own-{pair.a.policy.hex}"}, tenant_id=a
    )
    assert own_pol is not None and own_pol["name"] == f"own-{pair.a.policy.hex}"
    repository.record_test_outcome(
        o, ac, tenant_id=a, at=_now(o), health_status="healthy", status=None
    )
    own_health = o.execute(
        text("SELECT health_status FROM shifde.connections WHERE id = :i"), {"i": ac}
    ).scalar_one()
    assert own_health == "healthy"
    assert repository.update_connection(o, bc, None, {"name": "taken"}, tenant_id=a) is None
    assert (
        repository.update_egress_policy(o, pair.b.policy, None, {"name": "x"}, tenant_id=a) is None
    )
    assert (
        repository.update_egress_policy(o, pair.b.policy, None, {"name": "x"}, tenant_id=None)
        is None
    )
    repository.record_test_outcome(
        o, bc, tenant_id=a, at=_now(o), health_status="unhealthy", status="degraded"
    )
    assert [r["id"] for r in _list(o, a)] == [ids["a"]["connection"]]
    row = o.execute(
        text("SELECT name, status, health_status FROM shifde.connections WHERE id = :i"), {"i": bc}
    ).one()
    assert tuple(row) == (b_name, "draft", "unknown")
    o.rollback()


def test_nothing_is_visible_without_a_client_context(app_db: Database, pair: Pair) -> None:
    _seed(app_db, pair)
    with app_session(app_db, None) as s:
        for table in ("connections", "connection_tests", "connection_discoveries"):
            assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() == 0
        # only the installation default: the installation overview reads it (B-1105)
        rows = s.execute(text("SELECT tenant_id, is_default FROM shifde.egress_policies")).all()
        assert [tuple(r) for r in rows] == [(None, True)]


# ------------------------------------------------------------------ insert
def test_a_cannot_insert_rows_for_b_or_name_bs_connection(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    a, b = pair.a.tenant, pair.b.tenant
    bc = ids["b"]["connection"]
    with app_session(app_db, a) as s:
        for make in (
            lambda: repository.insert_connection(s, connection_values(b)),
            lambda: repository.insert_test(s, check_values(b, bc)),
            lambda: repository.insert_discovery(s, discovery_values(b, bc)),
            lambda: repository.insert_egress_policy(s, _policy(b)),
            lambda: repository.insert_egress_policy(s, _policy(None)),
        ):
            assert _refused(s, make)[0] == "42501"  # row-level security WITH CHECK
        # A's own row naming B's connection: refused exactly like a connection that does not exist
        _same_refusal(s, lambda c: repository.insert_test(s, check_values(a, c)), bc)
        _same_refusal(s, lambda c: repository.insert_discovery(s, discovery_values(a, c)), bc)


def test_names_are_unique_per_client_only(app_db: Database, pair: Pair) -> None:
    """A can use the name of B's connection and egress policy: no duplicate-key hint about B."""
    ids = _seed(app_db, pair)
    with app_session(app_db, pair.b.tenant) as s:
        b_conn = repository.get_connection(s, ids["b"]["connection"], tenant_id=pair.b.tenant)
        b_pol = repository.get_egress_policy(s, pair.b.policy, tenant_id=pair.b.tenant)
        assert b_conn is not None and b_pol is not None
        names = (b_conn["name"], b_pol["name"])
    with app_session(app_db, pair.a.tenant) as s:
        repository.insert_connection(s, connection_values(pair.a.tenant, name=names[0]))
        repository.insert_egress_policy(s, {**_policy(pair.a.tenant), "name": names[1]})
        repository.insert_egress_policy(s, {**_policy(pair.a.tenant), "is_default": True})
        s.commit()  # also a client default beside B's and the installation one


# ------------------------------------------------------------------ update / delete
def test_a_cannot_update_or_delete_bs_rows(
    app_db: Database, pair: Pair, owner_session: Session
) -> None:
    ids = _seed(app_db, pair)
    a, b = pair.a.tenant, pair.b.tenant
    bc, ac = ids["b"]["connection"], ids["a"]["connection"]
    with app_session(app_db, a) as s:
        # own rows first (review 1 item 2): A's update, test outcome and policy change land
        own = repository.update_connection(s, ac, None, {"name": f"own-{ac.hex}"}, tenant_id=a)
        assert own is not None and own["name"] == f"own-{ac.hex}" and own["row_version"] == 2
        repository.record_test_outcome(
            s, ac, tenant_id=a, at=_now(s), health_status="unhealthy", status="disabled"
        )
        own_row = repository.get_connection(s, ac, tenant_id=a)
        assert own_row is not None
        assert (own_row["status"], own_row["health_status"]) == ("disabled", "unhealthy")
        own_pol = repository.update_egress_policy(
            s, pair.a.policy, None, {"allow_private": True}, tenant_id=a
        )
        assert own_pol is not None and own_pol["allow_private"] is True
        for tenant in (a, b):
            assert (
                repository.update_connection(s, bc, None, {"name": "x"}, tenant_id=tenant) is None
            )
            assert (
                repository.update_egress_policy(
                    s, pair.b.policy, None, {"name": "x"}, tenant_id=tenant
                )
                is None
            )
            repository.record_test_outcome(
                s, bc, tenant_id=tenant, at=_now(s), health_status="unhealthy", status="disabled"
            )
        assert (
            repository.update_egress_policy(
                s, pair.installation_policy, None, {"allow_private": True}, tenant_id=None
            )
            is None
        )
        s.commit()
        for stmt in (
            "UPDATE shifde.connections SET tenant_id = :a WHERE id = :b",
            "UPDATE shifde.connection_tests SET outcome = 'fail' WHERE connection_id = :b",
            "UPDATE shifde.connection_discoveries SET payload = '{}' WHERE connection_id = :b",
            "UPDATE shifde.egress_policies SET tenant_id = :a WHERE id = :b",
            "DELETE FROM shifde.connections WHERE id = :b",
            "DELETE FROM shifde.connection_tests WHERE connection_id = :b",
            "DELETE FROM shifde.connection_discoveries WHERE connection_id = :b",
            "DELETE FROM shifde.egress_policies WHERE id = :b",
            "TRUNCATE shifde.connections",
        ):
            with pytest.raises(DBAPIError) as err:
                s.execute(text(stmt), {"a": a, "b": bc})
            assert _sqlstate(err) == "42501", stmt
            s.rollback()
    o = owner_session
    b_row = o.execute(
        text(
            "SELECT name, status, health_status, row_version FROM shifde.connections WHERE id = :i"
        ),
        {"i": bc},
    ).one()
    assert (b_row.status, b_row.health_status, b_row.row_version) == ("draft", "unknown", 1)
    assert not b_row.name.startswith("x")
    pol = o.execute(
        text("SELECT allow_private, row_version FROM shifde.egress_policies WHERE id = :i"),
        {"i": pair.installation_policy},
    ).one()
    assert tuple(pol) == (False, 1)


def test_the_platform_admin_context_changes_only_the_installation_default(
    app_db: Database, pair: Pair
) -> None:
    with app_session(app_db, None) as s, platform_admin_context(s):
        assert (
            repository.update_egress_policy(
                s, pair.b.policy, None, {"name": "x"}, tenant_id=pair.b.tenant
            )
            is None
        )
        row = repository.update_egress_policy(
            s, pair.installation_policy, None, {"timeout_seconds": 9.0}, tenant_id=None
        )
        assert row is not None and row["tenant_id"] is None
        s.rollback()


# ------------------------------------------------------------------ bindings
def _bind(s: Session, tenant: UUID, column: str) -> Callable[[UUID], object]:
    return lambda ref: repository.insert_connection(s, connection_values(tenant, **{column: ref}))


def _rebind(s: Session, tenant: UUID, conn: UUID, column: str) -> Callable[[UUID], object]:
    return lambda ref: repository.update_connection(s, conn, None, {column: ref}, tenant_id=tenant)


@pytest.mark.parametrize("column", ["secret_ref_id", "egress_policy_id", "vendor_id"])
def test_a_cannot_bind_bs_secret_policy_or_vendor(
    app_db: Database, pair: Pair, column: str
) -> None:
    """Insert and repoint alike: B's secret / egress policy / private vendor is refused exactly as
    an unknown id; so are an installation secret and the installation egress default."""
    ids = _seed(app_db, pair)
    a = pair.a.tenant
    theirs = {
        "secret_ref_id": pair.b.secret,
        "egress_policy_id": pair.b.policy,
        "vendor_id": pair.b.vendor,
    }[column]
    with app_session(app_db, a) as s:
        _same_refusal(s, _bind(s, a, column), theirs)
        _same_refusal(s, _rebind(s, a, ids["a"]["connection"], column), theirs)
        installation = {
            "secret_ref_id": pair.installation_secret,
            "egress_policy_id": pair.installation_policy,
        }.get(column)
        if installation is not None:
            _same_refusal(s, _bind(s, a, column), installation)
            _same_refusal(s, _rebind(s, a, ids["a"]["connection"], column), installation)
        else:  # a global catalog vendor is shared and may be named
            repository.insert_connection(s, connection_values(a, vendor_id=pair.global_vendor))
            s.commit()
    with app_session(app_db, a) as s:
        row = repository.get_connection(s, ids["a"]["connection"], tenant_id=a)
        own = {
            "secret_ref_id": pair.a.secret,
            "egress_policy_id": pair.a.policy,
            "vendor_id": pair.a.vendor,
        }[column]
        assert row is not None and row[column] == own


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("secret_ref_id", "fk_connections__secret"),
        ("egress_policy_id", "fk_connections__egress_policy"),
    ],
)
def test_the_composite_keys_hold_without_the_guard_or_rls(
    pair: Pair, owner_session: Session, column: str, constraint: str
) -> None:
    """With `trg_connections_guard` disabled, as the schema owner (BYPASSRLS), A's connection
    still cannot name B's secret or egress policy (B-116 item 9, B-1502)."""
    o = owner_session
    theirs = {"secret_ref_id": pair.b.secret, "egress_policy_id": pair.b.policy}[column]
    o.execute(text("ALTER TABLE shifde.connections DISABLE TRIGGER trg_connections_guard"))
    try:
        with pytest.raises(DBAPIError) as err:
            repository.insert_connection(o, connection_values(pair.a.tenant, **{column: theirs}))
        assert _sqlstate(err) == "23503"
        diag = getattr(err.value.orig, "diag", None)
        assert getattr(diag, "constraint_name", None) == constraint
    finally:
        o.rollback()  # also re-enables the trigger (the ALTER was in this transaction)
    assert (
        o.execute(
            text(
                "SELECT tgenabled FROM pg_catalog.pg_trigger WHERE tgname = 'trg_connections_guard'"
            )
        ).scalar_one()
        == "O"
    )


def test_the_guard_refuses_bs_vendor_as_the_owner(pair: Pair, owner_session: Session) -> None:
    """The vendor key stays plain (global vendors have tenant NULL); the guard alone refuses B's
    private vendor, even for a session that bypasses row-level security."""
    o = owner_session
    with pytest.raises(DBAPIError) as err:
        repository.insert_connection(o, connection_values(pair.a.tenant, vendor_id=pair.b.vendor))
    assert _sqlstate(err) == "23514"
    o.rollback()


# ------------------------------------------------------------------ helpers
def _now(s: Session) -> Any:
    return s.execute(text("SELECT now()")).scalar_one()


def _policy(tenant: UUID | None) -> dict[str, Any]:
    pid = uuid7()
    return {
        "id": pid,
        "tenant_id": tenant,
        "name": f"p-{pid.hex}",
        "is_default": False,
        "allowed_schemes": ["https"],
        "allowed_ports": [443],
        "allowed_hosts": [],
        "allowed_cidrs": [],
        "allow_private": False,
        "allow_redirects": False,
        "max_response_bytes": 1000,
        "timeout_seconds": 5.0,
        "created_by": None,
    }
