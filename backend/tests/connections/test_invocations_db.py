"""`connection_invocations` in the database (connections_0003, issue #190, B-2996, B-2997).

As `shifde_app` (`app_session` asserts the role) under row-level security: client A cannot read,
insert, update or delete client B's invocations, cannot name B's connection (refused exactly as an
unknown id, by the composite key), and cannot write a row of B's tenant; the app role updates
only the outcome columns and never deletes. The state checks hold for every writer. The route,
the queue and their cross-client route tests arrive with the factory job for #190.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any  # Any: SQL parameter values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.connections.conftest import Pair, connection_values
from tests.tenancy.conftest import app_session

INSERT = text(
    "INSERT INTO shifde.connection_invocations "
    "(tenant_id, id, job_id, connection_id, requested_by, status, result, error_code, "
    "finished_at) VALUES (:tenant_id, :id, :job_id, :connection_id, :requested_by, :status, "
    "CAST(:result AS jsonb), :error_code, :finished_at)"
)


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


def _refused(s: Session, fn: Callable[[], object]) -> tuple[str | None, str]:
    """The SQLSTATE and first message line of a refused statement; rolls back."""
    with pytest.raises(DBAPIError) as err:
        fn()
    s.rollback()
    return _sqlstate(err), str(err.value.orig).splitlines()[0]


def _values(tenant: UUID, connection: UUID, requester: UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "id": uuid7(),
        "job_id": uuid7(),
        "connection_id": connection,
        "requested_by": requester,
        "status": "queued",
        "result": None,
        "error_code": None,
        "finished_at": None,
    }
    values.update(overrides)
    return values


def _connection(s: Session, tenant: UUID) -> UUID:
    values = connection_values(tenant)
    s.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, workspace_id, project_id, name, "
            "connection_type, adapter_key, vendor_id, config, auth_method, secret_ref_id, "
            "egress_policy_id, access_mode, created_by) VALUES (:id, :tenant_id, :workspace_id, "
            ":project_id, :name, :connection_type, :adapter_key, :vendor_id, "
            "CAST(:config AS jsonb), :auth_method, :secret_ref_id, :egress_policy_id, "
            ":access_mode, :created_by)"
        ),
        {**values, "config": '{"base_url": "https://api.example.com"}'},
    )
    cid: UUID = values["id"]
    return cid


def _seed(app_db: Database, pair: Pair) -> dict[str, dict[str, UUID]]:
    """Per client: a connection and one queued invocation of it, written as `shifde_app`."""
    out: dict[str, dict[str, UUID]] = {}
    for label, side in (("a", pair.a), ("b", pair.b)):
        with app_session(app_db, side.tenant) as s:
            connection = _connection(s, side.tenant)
            row = _values(side.tenant, connection, side.c.member)
            s.execute(INSERT, row)
            s.commit()
        out[label] = {"connection": connection, "invocation": row["id"], "job": row["job_id"]}
    return out


def test_a_reads_only_its_own_invocations(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    with app_session(app_db, pair.a.tenant) as s:
        rows = s.execute(text("SELECT id, tenant_id FROM shifde.connection_invocations")).all()
        assert [(r.id, r.tenant_id) for r in rows] == [(ids["a"]["invocation"], pair.a.tenant)]
        for where, value in (("id", ids["b"]["invocation"]), ("job_id", ids["b"]["job"])):
            found = s.execute(
                text(f"SELECT count(*) FROM shifde.connection_invocations WHERE {where} = :v"),
                {"v": value},
            ).scalar_one()
            assert found == 0
    with app_session(app_db, None) as s:  # no client context: nothing at all
        assert (
            s.execute(text("SELECT count(*) FROM shifde.connection_invocations")).scalar_one() == 0
        )


def test_a_cannot_name_bs_connection_or_write_bs_tenant(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    a, b = pair.a.tenant, pair.b.tenant
    with app_session(app_db, a) as s:
        # B's connection is refused exactly as an unknown connection (the composite key)
        theirs = _refused(
            s, lambda: s.execute(INSERT, _values(a, ids["b"]["connection"], pair.a.c.member))
        )
        missing = _refused(s, lambda: s.execute(INSERT, _values(a, uuid7(), pair.a.c.member)))
        assert theirs[0] == missing[0] == "23503"
        assert theirs[1].split("(")[0] == missing[1].split("(")[0]
        # a row of B's tenant is refused by the policy's WITH CHECK, whichever connection it names
        for connection in (ids["a"]["connection"], ids["b"]["connection"]):
            state, _ = _refused(
                s, lambda c=connection: s.execute(INSERT, _values(b, c, pair.a.c.member))
            )
            assert state == "42501"


def test_a_cannot_update_or_delete_bs_invocations(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    bid = ids["b"]["invocation"]
    with app_session(app_db, pair.a.tenant) as s:
        done = s.execute(
            text(
                "UPDATE shifde.connection_invocations SET status = 'failed', "
                "error_code = 'x', finished_at = now() WHERE id = :i"
            ),
            {"i": bid},
        )
        assert done.rowcount == 0
        s.rollback()
        state, _ = _refused(
            s,
            lambda: s.execute(
                text("DELETE FROM shifde.connection_invocations WHERE id = :i"), {"i": bid}
            ),
        )
        assert state == "42501"  # no DELETE grant at all
    with app_session(app_db, pair.b.tenant) as s:
        status = s.execute(
            text("SELECT status FROM shifde.connection_invocations WHERE id = :i"), {"i": bid}
        ).scalar_one()
        assert status == "queued"


@pytest.mark.parametrize(
    "column", ["tenant_id", "job_id", "connection_id", "requested_by", "created_at", "id"]
)
def test_the_app_role_cannot_change_identity_columns(
    app_db: Database, pair: Pair, column: str
) -> None:
    ids = _seed(app_db, pair)
    with app_session(app_db, pair.a.tenant) as s:
        state, _ = _refused(
            s,
            lambda: s.execute(
                text(f"UPDATE shifde.connection_invocations SET {column} = {column} WHERE id = :i"),
                {"i": ids["a"]["invocation"]},
            ),
        )
        assert state == "42501"


def test_the_requester_index_serves_the_status_read(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    with app_session(app_db, pair.a.tenant) as s:
        definitions = dict(
            s.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
                    "WHERE schemaname = 'shifde' AND tablename = 'connection_invocations'"
                )
            ).all()
        )
        assert definitions["ix_connection_invocations__requester"] == (
            "CREATE INDEX ix_connection_invocations__requester ON shifde.connection_invocations "
            "USING btree (tenant_id, requested_by, created_at DESC)"
        )
        own = s.execute(
            text(
                "SELECT id FROM shifde.connection_invocations "
                "WHERE tenant_id = :t AND requested_by = :r ORDER BY created_at DESC"
            ),
            {"t": pair.a.tenant, "r": pair.a.c.member},
        ).scalars()
        assert list(own) == [ids["a"]["invocation"]]


def test_the_outcome_columns_follow_the_status(app_db: Database, pair: Pair) -> None:
    ids = _seed(app_db, pair)
    a, member = pair.a.tenant, pair.a.c.member
    connection = ids["a"]["connection"]
    with app_session(app_db, a) as s:
        for bad in (
            {"status": "succeeded"},  # terminal without finished_at
            {"status": "running", "finished_at": "2026-11-22T00:00:00Z"},
            {"status": "failed", "finished_at": "2026-11-22T00:00:00Z", "result": '{"a": 1}'},
            {"status": "succeeded", "finished_at": "2026-11-22T00:00:00Z", "error_code": "x"},
            {"status": "done"},
        ):
            state, _ = _refused(
                s, lambda b=bad: s.execute(INSERT, _values(a, connection, member, **b))
            )
            assert state == "23514", bad
        # the worker's path: queued -> running -> succeeded with a scrubbed result
        iid = ids["a"]["invocation"]
        for sql in (
            "UPDATE shifde.connection_invocations SET status = 'running' WHERE id = :i",
            "UPDATE shifde.connection_invocations SET status = 'succeeded', "
            "result = '{\"rows\": 1}', finished_at = now() WHERE id = :i",
        ):
            assert s.execute(text(sql), {"i": iid}).rowcount == 1
        row = s.execute(
            text(
                "SELECT status, result, updated_at, finished_at "
                "FROM shifde.connection_invocations WHERE id = :i"
            ),
            {"i": iid},
        ).one()
        assert row.status == "succeeded" and row.result == {"rows": 1}
        assert row.updated_at is not None and row.finished_at is not None
        s.commit()
        # one invocation per job
        dup = _values(a, connection, member, job_id=ids["a"]["job"])
        state, _ = _refused(s, lambda: s.execute(INSERT, dup))
        assert state == "23505"
