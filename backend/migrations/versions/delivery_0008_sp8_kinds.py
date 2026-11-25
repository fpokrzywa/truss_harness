"""Delivery, part 8: Fabric, Palantir and Docker platform and target kinds.

Revision ID: delivery_0008
Revises: delivery_0007

Sub-project 8 design (2026-11-23) sections 2 and 8 item 1, issue #286 (SP8-1).

* `delivery_platform_claims.platform_kind` and `delivery_targets.platform_kind` accept
  `databricks`, `snowflake`, `fabric`, `palantir` or `docker`
  (`ck_delivery_platform_claims__platform_kind`, `ck_delivery_targets__platform_kind`).
* `delivery_artifacts.target_kind` accepts `databricks.app/v1`, `snowflake.native-app/v1`,
  `fabric.solution/v1`, `palantir.marketplace/v1` or `docker.compose/v1`
  (`ck_delivery_artifacts__target_kind`).
* The three named CHECKs of delivery_0007 are dropped and re-added under the same names.

Isolation: only CHECK constraints change. No table, column, policy, grant, function or definer
changes; the tables keep their forced row-level security and `tenant_isolation` policies, and
`shifde.delivery_claim_platform` is unchanged (it keys a claim on platform kind and address key,
so claims of different platform kinds never collide). `shifde_app` still owns nothing.

Grants: unchanged.

Rollback: refused while any organization has a `fabric`, `palantir` or `docker` claim or target
or a `fabric.solution/v1`, `palantir.marketplace/v1` or `docker.compose/v1` artifact, counted per
client (delivery_0007's per-client PRECHECK loop: a plain count under forced row-level security
sees no rows); the refusal names only the count. Otherwise the three named CHECKs are restored to
delivery_0007's two values.
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0008"
down_revision = "delivery_0007"
branch_labels = None
depends_on = None

# table, column, constraint name, widened values, delivery_0007 values
CHECKS = (
    (
        "delivery_platform_claims",
        "platform_kind",
        "ck_delivery_platform_claims__platform_kind",
        ("databricks", "snowflake", "fabric", "palantir", "docker"),
        ("databricks", "snowflake"),
    ),
    (
        "delivery_targets",
        "platform_kind",
        "ck_delivery_targets__platform_kind",
        ("databricks", "snowflake", "fabric", "palantir", "docker"),
        ("databricks", "snowflake"),
    ),
    (
        "delivery_artifacts",
        "target_kind",
        "ck_delivery_artifacts__target_kind",
        (
            "databricks.app/v1",
            "snowflake.native-app/v1",
            "fabric.solution/v1",
            "palantir.marketplace/v1",
            "docker.compose/v1",
        ),
        ("databricks.app/v1", "snowflake.native-app/v1"),
    ),
)

# Every client's rows of a new kind. Runs as the schema owner, which forced row-level security
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
    SELECT (SELECT count(*) FROM shifde.delivery_platform_claims
             WHERE tenant_id = t AND platform_kind IN ('fabric', 'palantir', 'docker'))
         + (SELECT count(*) FROM shifde.delivery_targets
             WHERE tenant_id = t AND platform_kind IN ('fabric', 'palantir', 'docker'))
         + (SELECT count(*) FROM shifde.delivery_artifacts
             WHERE tenant_id = t AND target_kind IN
               ('fabric.solution/v1', 'palantir.marketplace/v1', 'docker.compose/v1'))
      INTO k;
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION 'delivery_0008: %% Fabric, Palantir or Docker claim, target or artifact row(s) exist', n;
  END IF;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace(table: str, column: str, name: str, values: tuple[str, ...]) -> None:
    _run(
        f"ALTER TABLE shifde.{table} DROP CONSTRAINT {name}",
        f"ALTER TABLE shifde.{table} ADD CONSTRAINT {name} CHECK ({column} IN ({_values(values)}))",
    )


def upgrade() -> None:
    for table, column, name, widened, _ in CHECKS:
        _replace(table, column, name, widened)


def downgrade() -> None:
    _run(PRECHECK)
    for table, column, name, _, previous in CHECKS:
        _replace(table, column, name, previous)
