"""Delivery, part 6: a plan names the sealed release it deploys and the evaluation that passed it.

Revision ID: delivery_0006
Revises: delivery_0005
Depends on: evaluation_0001 (`evaluation_runs`; `releases` comes with its dependency
packaging_0002). packaging_0002 is an ancestor of evaluation_0001 and is not named (a named
ancestor of another named revision breaks `alembic upgrade heads`, see delivery_0001).

Sub-project 3 design sections 4 ("Delivery migration") and 6 ("Gate"), section 12 settled calls
3 to 6 (issue #81).

* `delivery_plans.release_id`, `release_digest` and `evaluation_run_id`, all nullable: the
  release the plan deploys, its digest at request time and the passing evaluation run the gate
  found. `ck_delivery_plans__release`: all three are NULL (a plan requested before this
  revision) or all three are set. `release_digest` is a lowercase sha256.
* `fk_delivery_plans__release` to `releases (tenant_id, id)` and
  `fk_delivery_plans__evaluation_run` to `evaluation_runs (tenant_id, id)`, both
  `ON DELETE RESTRICT`; `ix_delivery_plans__release (tenant_id, release_id)`.

Isolation: `delivery_plans` keeps its forced row-level security, its `tenant_isolation` policy
and `delivery_plans_seal_guard()` unchanged; both new keys are composite `(tenant_id, ...)`, so a
plan can name only its own organization's release and run. Grants to `shifde_app`: INSERT on the
three new columns only; no UPDATE of them. No SECURITY DEFINER function, no global table.

Rollback: drops the index, both keys, the check and the three columns (data-destructive: every
plan's release and evaluation run reference is lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0006"
down_revision = "delivery_0005"
branch_labels = None
depends_on = ("evaluation_0001",)

RELEASE_COLUMNS = "release_id, release_digest, evaluation_run_id"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        "ALTER TABLE shifde.delivery_plans ADD COLUMN release_id uuid, "
        "ADD COLUMN release_digest text CHECK (release_digest ~ '^[a-f0-9]{64}$'), "
        "ADD COLUMN evaluation_run_id uuid",
        "ALTER TABLE shifde.delivery_plans ADD CONSTRAINT ck_delivery_plans__release CHECK ("
        "(release_id IS NULL AND release_digest IS NULL AND evaluation_run_id IS NULL) OR "
        "(release_id IS NOT NULL AND release_digest IS NOT NULL "
        "AND evaluation_run_id IS NOT NULL))",
        "ALTER TABLE shifde.delivery_plans ADD CONSTRAINT fk_delivery_plans__release "
        "FOREIGN KEY (tenant_id, release_id) REFERENCES shifde.releases (tenant_id, id) "
        "ON DELETE RESTRICT",
        "ALTER TABLE shifde.delivery_plans ADD CONSTRAINT fk_delivery_plans__evaluation_run "
        "FOREIGN KEY (tenant_id, evaluation_run_id) "
        "REFERENCES shifde.evaluation_runs (tenant_id, id) ON DELETE RESTRICT",
        "CREATE INDEX ix_delivery_plans__release ON shifde.delivery_plans (tenant_id, release_id)",
        f"GRANT INSERT ({RELEASE_COLUMNS}) ON shifde.delivery_plans TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP INDEX shifde.ix_delivery_plans__release",
        "ALTER TABLE shifde.delivery_plans DROP CONSTRAINT fk_delivery_plans__evaluation_run",
        "ALTER TABLE shifde.delivery_plans DROP CONSTRAINT fk_delivery_plans__release",
        "ALTER TABLE shifde.delivery_plans DROP CONSTRAINT ck_delivery_plans__release",
        "ALTER TABLE shifde.delivery_plans DROP COLUMN evaluation_run_id, "
        "DROP COLUMN release_digest, DROP COLUMN release_id",
    )
