"""Runtime: retention deletes old ended runs, client by client (issue #126, SP4-8, REQ F-11).

Revision ID: runtime_0006
Revises: runtime_0005

`shifde-admin maintenance delete` removes a client's old, ended run families as `shifde_app`
(DECISIONS B-2783). `shifde_app` had no DELETE on any run table, and three of them refused DELETE
for every role. This revision:

* grants DELETE to `shifde_app` on `tool_invocations`, `run_artifacts`, `run_events`,
  `approvals`, `run_steps`, `jobs`, `ontology_query_lineage`, `budget_reservations` and `runs`
  (nothing else);
* recreates `trg_run_events_append_only`, `trg_run_artifacts_append_only` and
  `trg_ontology_lineage_append_only` as `BEFORE UPDATE` only, with their functions unchanged, so
  every UPDATE is refused exactly as before (`reject_mutation()`; `run_artifacts_guard()` still
  admits only the scrub placeholder);
* adds `shifde.retention_delete_guard()` and a `BEFORE DELETE ... FOR EACH ROW` trigger
  `trg_<table>_retention_delete` (`trg_ontology_lineage_retention_delete` on the lineage) on each
  of the nine tables. It fires for every role, the schema owner included, and raises 42501
  (insufficient_privilege) unless the transaction-local setting `shifde.retention_delete_cutoff`
  is set, not empty and a timestamptz; the row's run (`runs`: the row itself; elsewhere the run
  `(OLD.tenant_id, OLD.run_id)` names, a NULL run_id refused) is terminal with `finished_at`
  before that cutoff; an approval is not pending; a job is done or dead; and a budget reservation
  is not held. The guard runs with the caller's rights (row-level security applies to its read of
  `runs`), a fixed search_path, every relation schema-qualified and EXECUTE revoked from PUBLIC.
  The command writes the setting with `set_config(..., true)` inside each client's transaction;
  nothing in the catalog writes it, and it is not an `app.*` name.

Retention floors are not enforced here: the command applies them when it picks the cutoff.

Deliberate differences from SETH (`tests/schema/deliberate_changes.yaml`): the table privileges of
the nine tables (DELETE), the definitions of `trg_run_events_append_only` and
`trg_ontology_lineage_append_only` (UPDATE only; `trg_run_artifacts_append_only` already differs
since runtime_0002) and the nine new `*_retention_delete` triggers.

Rollback: drops the nine guard triggers and the guard, restores the three append-only triggers
as `BEFORE UPDATE OR DELETE` with their runtime_0002 / runtime_0001 / ontology_0001 functions and
revokes the nine DELETE grants (deleted rows stay deleted).
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0006"
down_revision = "runtime_0005"
branch_labels = None
depends_on = None

# table -> its new delete trigger, in the command's delete order
TABLES = {
    "run_artifacts": "trg_run_artifacts_retention_delete",
    "run_events": "trg_run_events_retention_delete",
    "approvals": "trg_approvals_retention_delete",
    "run_steps": "trg_run_steps_retention_delete",
    "tool_invocations": "trg_tool_invocations_retention_delete",
    "jobs": "trg_jobs_retention_delete",
    "ontology_query_lineage": "trg_ontology_lineage_retention_delete",
    "budget_reservations": "trg_budget_reservations_retention_delete",
    "runs": "trg_runs_retention_delete",
}
# the append-only triggers that refused DELETE: (table, trigger, function)
APPEND_ONLY = (
    ("run_events", "trg_run_events_append_only", "shifde.reject_mutation()"),
    ("run_artifacts", "trg_run_artifacts_append_only", "shifde.run_artifacts_guard()"),
    ("ontology_query_lineage", "trg_ontology_lineage_append_only", "shifde.reject_mutation()"),
)

GUARD = """CREATE FUNCTION shifde.retention_delete_guard() RETURNS trigger LANGUAGE plpgsql
SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
  v_run uuid;
BEGIN
  IF v_setting IS NULL OR v_setting = '' THEN
    RAISE EXCEPTION 'run rows are deleted only by the retention delete'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  BEGIN
    v_cutoff := v_setting::timestamptz;
  EXCEPTION WHEN data_exception THEN
    RAISE EXCEPTION 'the retention delete cutoff is not a timestamp'
      USING ERRCODE = 'insufficient_privilege';
  END;
  IF TG_TABLE_NAME = 'runs' THEN
    v_run := OLD.id;
  ELSE
    v_run := OLD.run_id;
  END IF;
  IF v_run IS NULL OR NOT EXISTS (
       SELECT 1 FROM shifde.runs r
        WHERE r.id = v_run AND r.tenant_id = OLD.tenant_id
          AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out')
          AND r.finished_at IS NOT NULL AND r.finished_at < v_cutoff) THEN
    RAISE EXCEPTION 'only the rows of a run that ended before the retention cutoff can be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- one IF per table: OLD.status exists only on some of them
  IF TG_TABLE_NAME = 'approvals' THEN
    IF OLD.status = 'pending' THEN
      RAISE EXCEPTION 'a pending approval cannot be deleted'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  ELSIF TG_TABLE_NAME = 'jobs' THEN
    IF OLD.status NOT IN ('done', 'dead') THEN
      RAISE EXCEPTION 'a queued or leased job cannot be deleted'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  ELSIF TG_TABLE_NAME = 'budget_reservations' THEN
    IF OLD.status = 'held' THEN
      RAISE EXCEPTION 'a held budget reservation cannot be deleted'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END IF;
  RETURN OLD;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        *[
            stmt
            for table, trigger, function in APPEND_ONLY
            for stmt in (
                f"DROP TRIGGER {trigger} ON shifde.{table}",
                f"CREATE TRIGGER {trigger} BEFORE UPDATE ON shifde.{table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function}",
            )
        ],
        GUARD,
        "REVOKE ALL ON FUNCTION shifde.retention_delete_guard() FROM PUBLIC",
        *[
            f"CREATE TRIGGER {trigger} BEFORE DELETE ON shifde.{table} "
            "FOR EACH ROW EXECUTE FUNCTION shifde.retention_delete_guard()"
            for table, trigger in TABLES.items()
        ],
        *[f"GRANT DELETE ON shifde.{table} TO shifde_app" for table in TABLES],
    )


def downgrade() -> None:
    _run(
        *[f"REVOKE DELETE ON shifde.{table} FROM shifde_app" for table in reversed(TABLES)],
        *[f"DROP TRIGGER {TABLES[table]} ON shifde.{table}" for table in reversed(TABLES)],
        "DROP FUNCTION shifde.retention_delete_guard()",
        *[
            stmt
            for table, trigger, function in reversed(APPEND_ONLY)
            for stmt in (
                f"DROP TRIGGER {trigger} ON shifde.{table}",
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON shifde.{table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function}",
            )
        ],
    )
