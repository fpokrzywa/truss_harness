"""Packaging, part 4: the Snowflake Native App target kind.

Revision ID: packaging_0004
Revises: packaging_0003

Sub-project 6 design (2026-11-23) sections 4 (`packaging`) and 5 ("Migration"), issue #271
(SP6-4).

* `solutions.target_kind` accepts `databricks.app/v1` or `snowflake.native-app/v1`
  (`ck_solutions__target_kind`). The design names `solution_versions`, but `solutions` holds the
  only packaging `target_kind` column.
* The inline CHECK of packaging_0001 carries a generated name; it is found in the catalog (the
  one single-column CHECK on that column) and dropped by name.

Isolation: only a CHECK constraint changes. No table, column, policy, grant, function or definer
changes; `solutions` keeps its forced row-level security and `tenant_isolation` policy.
`shifde_app` still owns nothing.

Grants: unchanged.

Rollback: refused while any organization has a `snowflake.native-app/v1` solution, counted per
client (the per-client loop of delivery_0007's PRECHECK: a plain count under forced row-level
security sees no rows); the refusal names only the count. Otherwise the named CHECK is dropped and
the original single-value CHECK is restored under its original generated name.
"""

from __future__ import annotations

from alembic import op

revision = "packaging_0004"
down_revision = "packaging_0003"
branch_labels = None
depends_on = None

TABLE, COLUMN = "solutions", "target_kind"
NAME = "ck_solutions__target_kind"
WIDENED = ("databricks.app/v1", "snowflake.native-app/v1")
ORIGINAL = "databricks.app/v1"

# The single-column CHECK constraints on one column of one shifde table.
CHECK_NAMES = (
    "SELECT k.conname FROM pg_catalog.pg_constraint k "
    "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.conkey[1] "
    "WHERE n.nspname = 'shifde' AND k.contype = 'c' AND cardinality(k.conkey) = 1 "
    "AND c.relname = %s AND a.attname = %s"
)

# Every client's Snowflake solutions. Runs as the schema owner, which forced row-level security
# also binds: the client list is read as platform administrator, then each client's rows under
# its own `app.tenant_id`.
PRECHECK = """DO $$
DECLARE
  clients uuid[];
  t uuid;
  n bigint := 0;
  k bigint;
BEGIN
  PERFORM pg_catalog.set_config('app.platform_admin', 'on', true);
  clients := ARRAY(SELECT id FROM shifde.tenants);
  FOREACH t IN ARRAY clients LOOP
    PERFORM pg_catalog.set_config('app.platform_admin', '', true);
    PERFORM pg_catalog.set_config('app.tenant_id', t::text, true);
    SELECT count(*) FROM shifde.solutions
      WHERE tenant_id = t AND target_kind = 'snowflake.native-app/v1'
      INTO k;
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION 'packaging_0004: %% Snowflake solution row(s) exist', n;
  END IF;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    conn = op.get_bind()
    found = conn.exec_driver_sql(CHECK_NAMES, (TABLE, COLUMN)).scalars().all()
    if len(found) != 1:
        raise RuntimeError(f"packaging_0004: expected one CHECK on {TABLE}.{COLUMN}")
    values = ", ".join(f"'{value}'" for value in WIDENED)
    _run(
        f'ALTER TABLE shifde.{TABLE} DROP CONSTRAINT "{found[0]}"',
        f"ALTER TABLE shifde.{TABLE} ADD CONSTRAINT {NAME} CHECK ({COLUMN} IN ({values}))",
    )


def downgrade() -> None:
    _run(
        PRECHECK,
        f"ALTER TABLE shifde.{TABLE} DROP CONSTRAINT {NAME}",
        f"ALTER TABLE shifde.{TABLE} ADD CONSTRAINT {TABLE}_{COLUMN}_check "
        f"CHECK ({COLUMN} = '{ORIGINAL}')",
    )
