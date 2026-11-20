"""Ledger: a budget hold records whether its provider call may have started; direct invocations
hold too.

Revision ID: ledger_0003
Revises: ledger_0002

Sub-project 3 design 12.1 and 12.2 (issue #82). A hold whose model call may have reached the
provider must never be silently freed: when its worker dies it becomes `settled_unknown` at the
full held amount (with a usage row marked as unknown usage), and only a hold whose call provably
never started is released or expires after its TTL. Direct model invocations (`models_admin`
invoke, prompt and agent tests) now hold through the same table, keyed by an invocation instead of
a run step.

`shifde.budget_reservations`:
* `run_id` is nullable and `invocation_id uuid` is new; `ck_budget_reservations__owner` requires
  exactly one of them. A direct invocation's hold has no run.
* `provider_request_at timestamptz` is the provider-request marker: NULL means no provider request
  was started. `shifde_app` gets `UPDATE (provider_request_at)`; every earlier grant is kept, and
  there is still no DELETE grant.
* `uq_budget_reservations__step UNIQUE (tenant_id, run_id, step_key)` is replaced by the partial
  unique indexes `uq_budget_reservations__run_step (tenant_id, run_id, step_key) WHERE run_id IS
  NOT NULL` and `uq_budget_reservations__invocation (tenant_id, invocation_id) WHERE invocation_id
  IS NOT NULL`.
* The status CHECK also admits `settled_unknown`.
Forced row-level security and the `tenant_isolation` policy are unchanged.

Rollback: `settled_unknown` becomes `settled`; the direct-invocation holds (`run_id IS NULL`) are
DELETED (data-destructive: back up first); the original unique constraint, status CHECK and
`run_id NOT NULL` are restored and the new columns dropped.
"""

from __future__ import annotations

from alembic import op

revision = "ledger_0003"
down_revision = "ledger_0002"
branch_labels = None
depends_on = None

TABLE = "shifde.budget_reservations"
STATUSES = "('held', 'settled', 'released', 'expired')"
OUTCOME_STATUSES = "('held', 'settled', 'released', 'expired', 'settled_unknown')"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        f"ALTER TABLE {TABLE} ALTER COLUMN run_id DROP NOT NULL",
        f"ALTER TABLE {TABLE} ADD COLUMN invocation_id uuid",
        f"ALTER TABLE {TABLE} ADD COLUMN provider_request_at timestamptz",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_budget_reservations__owner "
        "CHECK ((run_id IS NULL) <> (invocation_id IS NULL))",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT uq_budget_reservations__step",
        f"CREATE UNIQUE INDEX uq_budget_reservations__run_step ON {TABLE} "
        "(tenant_id, run_id, step_key) WHERE run_id IS NOT NULL",
        f"CREATE UNIQUE INDEX uq_budget_reservations__invocation ON {TABLE} "
        "(tenant_id, invocation_id) WHERE invocation_id IS NOT NULL",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT budget_reservations_status_check",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT budget_reservations_status_check "
        f"CHECK (status IN {OUTCOME_STATUSES})",
        f"GRANT UPDATE (provider_request_at) ON {TABLE} TO shifde_app",
    )


def downgrade() -> None:
    _run(
        f"UPDATE {TABLE} SET status = 'settled' WHERE status = 'settled_unknown'",
        f"DELETE FROM {TABLE} WHERE run_id IS NULL",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT budget_reservations_status_check",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT budget_reservations_status_check "
        f"CHECK (status IN {STATUSES})",
        "DROP INDEX shifde.uq_budget_reservations__invocation",
        "DROP INDEX shifde.uq_budget_reservations__run_step",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT uq_budget_reservations__step "
        "UNIQUE (tenant_id, run_id, step_key)",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_budget_reservations__owner",
        f"ALTER TABLE {TABLE} DROP COLUMN provider_request_at",
        f"ALTER TABLE {TABLE} DROP COLUMN invocation_id",
        f"ALTER TABLE {TABLE} ALTER COLUMN run_id SET NOT NULL",
    )
