"""Packaging, part 5: the Fabric, Palantir and Docker target kinds.

Revision ID: packaging_0005
Revises: packaging_0004

Sub-project 8 design (2026-11-23) sections 2 and 8 item 1, issue #286 (SP8-1).

* `solutions.target_kind` accepts `databricks.app/v1`, `snowflake.native-app/v1`,
  `fabric.solution/v1`, `palantir.marketplace/v1` or `docker.compose/v1`
  (`ck_solutions__target_kind`). The design names `solution_versions`, but `solutions` holds the
  only packaging `target_kind` column (as in packaging_0004).
* packaging_0004's named CHECK is dropped and re-added under the same name.

Isolation: only a CHECK constraint changes. No table, column, policy, grant, function or definer
changes; `solutions` keeps its forced row-level security and `tenant_isolation` policy.
`shifde_app` still owns nothing.

Grants: unchanged.

Rollback: refused while any organization has a `fabric.solution/v1`, `palantir.marketplace/v1` or
`docker.compose/v1` solution, counted per client (delivery_0007's per-client PRECHECK loop: a
plain count under forced row-level security sees no rows); the refusal names only the count.
Otherwise the named CHECK is restored to packaging_0004's two values.
"""

from __future__ import annotations

from alembic import op

revision = "packaging_0005"
down_revision = "packaging_0004"
branch_labels = None
depends_on = None

TABLE, COLUMN = "solutions", "target_kind"
NAME = "ck_solutions__target_kind"
WIDENED = (
    "databricks.app/v1",
    "snowflake.native-app/v1",
    "fabric.solution/v1",
    "palantir.marketplace/v1",
    "docker.compose/v1",
)
PREVIOUS = ("databricks.app/v1", "snowflake.native-app/v1")

# Every client's solutions of a new kind. Runs as the schema owner, which forced row-level
# security also binds: the client list is read as platform administrator, then each client's rows
# under its own `app.tenant_id`.
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
      WHERE tenant_id = t AND target_kind IN
        ('fabric.solution/v1', 'palantir.marketplace/v1', 'docker.compose/v1')
      INTO k;
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION 'packaging_0005: %% Fabric, Palantir or Docker solution row(s) exist', n;
  END IF;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _replace(values: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{value}'" for value in values)
    _run(
        f"ALTER TABLE shifde.{TABLE} DROP CONSTRAINT {NAME}",
        f"ALTER TABLE shifde.{TABLE} ADD CONSTRAINT {NAME} CHECK ({COLUMN} IN ({listed}))",
    )


def upgrade() -> None:
    _replace(WIDENED)


def downgrade() -> None:
    _run(PRECHECK)
    _replace(PREVIOUS)
