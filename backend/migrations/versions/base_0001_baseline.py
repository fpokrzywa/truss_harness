"""Baseline: schema privileges for the application role and the shared SQL helpers.

Revision ID: base_0001
Revises: (none)

Every module branch (`branch_labels=("<module>",)`) descends from this revision (DECISIONS B-004).
It creates nothing client-owned. Contents, taken from SETH 3ba0e06 `p01_0001_tenancy_core`:

* `USAGE` on schema `shifde` for `shifde_app` (the schema itself is created by `env.py`, because
  the Alembic version table lives in it); `PUBLIC` gets nothing on the schema.
* `SELECT` on `shifde.alembic_version` for `shifde_app` (readiness check).
* The per-transaction context helpers every row-level security policy reads. Each fails closed:
  an unset or empty setting yields NULL / false, which matches no tenant row.
    - `shifde.current_tenant_id()`    <- `app.tenant_id`
    - `shifde.current_principal_id()` <- `app.principal_id`
    - `shifde.is_platform_admin_ctx()` <- `app.platform_admin` = 'on'
* The shared trigger helpers `shifde.set_updated_at()` and `shifde.reject_mutation()`.
* Unlike SETH, every builtin the helpers use is schema-qualified (`pg_catalog.current_setting`,
  `::pg_catalog.uuid`, `OPERATOR(pg_catalog.=)`, `pg_catalog.now()`), and `CASE` replaces
  SETH's `NULLIF` (which resolves `=` through the search path), so no object earlier in a
  caller's `search_path` can stand in for it (isolation review 1, item 7; review 2, item 6).
  Unset or empty still yields NULL: `CASE WHEN NULL = ''` falls through to `NULL::uuid`. No `SET search_path`
  clause: it would stop PostgreSQL inlining the SQL helpers into every policy.

No `ALTER DEFAULT PRIVILEGES`: SETH grants each table explicitly and minimally (no DELETE,
column-level UPDATE), and a default grant would widen that (DECISIONS B-105).

Rollback: data-preserving. `downgrade()` drops the helpers and the grants; it fails if a module
branch still depends on them (downgrade the module branches first).
"""

from __future__ import annotations

from alembic import op

revision = "base_0001"
down_revision = None
branch_labels = ("base",)
depends_on = None


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        "CREATE SCHEMA IF NOT EXISTS shifde",
        "REVOKE ALL ON SCHEMA shifde FROM PUBLIC",
        "GRANT USAGE ON SCHEMA shifde TO shifde_app",
        "GRANT SELECT ON shifde.alembic_version TO shifde_app",
        # ---- context helpers (fail closed on unset GUC) ----
        """CREATE FUNCTION shifde.current_tenant_id() RETURNS uuid LANGUAGE sql STABLE AS
           $$ SELECT CASE WHEN pg_catalog.current_setting('app.tenant_id', true)
                               OPERATOR(pg_catalog.=) '' THEN NULL
                          ELSE pg_catalog.current_setting('app.tenant_id', true)::pg_catalog.uuid
                     END $$""",
        """CREATE FUNCTION shifde.current_principal_id() RETURNS uuid LANGUAGE sql STABLE AS
           $$ SELECT CASE WHEN pg_catalog.current_setting('app.principal_id', true)
                               OPERATOR(pg_catalog.=) '' THEN NULL
                          ELSE pg_catalog.current_setting('app.principal_id', true)::pg_catalog.uuid
                     END $$""",
        """CREATE FUNCTION shifde.is_platform_admin_ctx() RETURNS boolean LANGUAGE sql STABLE AS
           $$ SELECT COALESCE(pg_catalog.current_setting('app.platform_admin', true), '')
                OPERATOR(pg_catalog.=) 'on' $$""",
        """CREATE FUNCTION shifde.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN NEW.updated_at = pg_catalog.now(); RETURN NEW; END $$""",
        """CREATE FUNCTION shifde.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                RAISE EXCEPTION 'append-only table: UPDATE and DELETE are not permitted'
                  USING ERRCODE = 'insufficient_privilege';
              END $$""",
    )


def downgrade() -> None:
    _run(
        "DROP FUNCTION shifde.reject_mutation()",
        "DROP FUNCTION shifde.set_updated_at()",
        "DROP FUNCTION shifde.is_platform_admin_ctx()",
        "DROP FUNCTION shifde.current_principal_id()",
        "DROP FUNCTION shifde.current_tenant_id()",
        "REVOKE SELECT ON shifde.alembic_version FROM shifde_app",
        "REVOKE USAGE ON SCHEMA shifde FROM shifde_app",
    )
