"""Delivery, part 7: Snowflake platform and target kinds.

Revision ID: delivery_0007
Revises: delivery_0006

Sub-project 6 design (2026-11-23) sections 5 ("Migration") and 6.4 ("Targets and executor"),
issue #270 (SP6-3).

* `delivery_platform_claims.platform_kind` and `delivery_targets.platform_kind` accept
  `databricks` or `snowflake` (`ck_delivery_platform_claims__platform_kind`,
  `ck_delivery_targets__platform_kind`).
* `delivery_artifacts.target_kind` accepts `databricks.app/v1` or `snowflake.native-app/v1`
  (`ck_delivery_artifacts__target_kind`).
* The three inline CHECKs of delivery_0001 and delivery_0004 carry generated names; they are
  found in the catalog (the one single-column CHECK on that column) and dropped by name.

Isolation: only CHECK constraints change. No table, column, policy, grant, function or definer
changes; the tables keep their forced row-level security and `tenant_isolation` policies, and
`shifde.delivery_claim_platform` is unchanged (it keys a claim on platform kind and address key,
so a Databricks and a Snowflake claim never collide). `shifde_app` still owns nothing.

Grants: unchanged.

Rollback: refused while any organization has a `snowflake` claim or target or a
`snowflake.native-app/v1` artifact, counted per client (the per-client loop of
connections_0002's PRECHECK: a plain count under forced row-level security sees no rows); the
refusal names only the count. Otherwise the three named CHECKs are dropped and the original
single-value CHECKs are restored under their original generated names.
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0007"
down_revision = "delivery_0006"
branch_labels = None
depends_on = None

# table, column, new constraint name, widened values, original value
CHECKS = (
    (
        "delivery_platform_claims",
        "platform_kind",
        "ck_delivery_platform_claims__platform_kind",
        ("databricks", "snowflake"),
        "databricks",
    ),
    (
        "delivery_targets",
        "platform_kind",
        "ck_delivery_targets__platform_kind",
        ("databricks", "snowflake"),
        "databricks",
    ),
    (
        "delivery_artifacts",
        "target_kind",
        "ck_delivery_artifacts__target_kind",
        ("databricks.app/v1", "snowflake.native-app/v1"),
        "databricks.app/v1",
    ),
)

# The single-column CHECK constraints on one column of one shifde table.
CHECK_NAMES = (
    "SELECT k.conname FROM pg_catalog.pg_constraint k "
    "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.conkey[1] "
    "WHERE n.nspname = 'shifde' AND k.contype = 'c' AND cardinality(k.conkey) = 1 "
    "AND c.relname = %s AND a.attname = %s"
)

# Every client's Snowflake rows. Runs as the schema owner, which forced row-level security also
# binds: the client list is read as platform administrator, then each client's rows under its own
# `app.tenant_id`.
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
    SELECT (SELECT count(*) FROM shifde.delivery_platform_claims
             WHERE tenant_id = t AND platform_kind = 'snowflake')
         + (SELECT count(*) FROM shifde.delivery_targets
             WHERE tenant_id = t AND platform_kind = 'snowflake')
         + (SELECT count(*) FROM shifde.delivery_artifacts
             WHERE tenant_id = t AND target_kind = 'snowflake.native-app/v1')
      INTO k;
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION 'delivery_0007: %% Snowflake claim, target or artifact row(s) exist', n;
  END IF;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, name, widened, _ in CHECKS:
        found = conn.exec_driver_sql(CHECK_NAMES, (table, column)).scalars().all()
        if len(found) != 1:
            raise RuntimeError(f"delivery_0007: expected one CHECK on {table}.{column}")
        _run(
            f'ALTER TABLE shifde.{table} DROP CONSTRAINT "{found[0]}"',
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT {name} "
            f"CHECK ({column} IN ({_values(widened)}))",
        )


def downgrade() -> None:
    _run(PRECHECK)
    for table, column, name, _, original in CHECKS:
        _run(
            f"ALTER TABLE shifde.{table} DROP CONSTRAINT {name}",
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT {table}_{column}_check "
            f"CHECK ({column} = '{original}')",
        )
