"""Runtime: capture off scrubs an ended run's content (findings Item 14, REQ F-12).

Revision ID: runtime_0002
Revises: runtime_0001

With `capture_content` off the runtime replaces a run's stored content by the placeholder
`{"scrubbed": true}` in the transaction that ends the run (DECISIONS B-2514). `shifde_app` had
no way to do that:

* `run_artifacts` was append-only for every role (`trg_run_artifacts_append_only` ->
  `shifde.reject_mutation()`) and granted SELECT, INSERT only. The trigger keeps its name and
  now calls `shifde.run_artifacts_guard()`, which still refuses every DELETE and every UPDATE
  except one: `content` set to `{"scrubbed": true}` with every other column unchanged, on an
  artifact whose run (of the artifact's own client) is terminal. `shifde_app` gets
  `UPDATE (content)` only. The guard runs with the caller's rights (row-level security applies
  to its read of `runs`).
* `tool_invocations` gets `UPDATE (arguments)` for `shifde_app` (the scrub sets it to NULL;
  `settle_ended_run` already falls back to the approval's arguments when it is NULL).

Deliberate differences from SETH (`tests/schema/deliberate_changes.yaml`): the trigger
definition of `trg_run_artifacts_append_only`, the `content` column grant of `run_artifacts`
and the `arguments` column grant of `tool_invocations`.

Rollback: restores the append-only trigger and revokes both column grants (a scrubbed row stays
scrubbed).
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0002"
down_revision = "runtime_0001"
branch_labels = None
depends_on = None

GUARD = """CREATE FUNCTION shifde.run_artifacts_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.content = '{"scrubbed": true}'::jsonb
     AND to_jsonb(NEW) - 'content' = to_jsonb(OLD) - 'content'
     AND EXISTS (SELECT 1 FROM shifde.runs r
                  WHERE r.id = OLD.run_id AND r.tenant_id = OLD.tenant_id
                    AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out')) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'append-only table: only the content of an ended run''s artifact can be '
    'replaced by the scrubbed placeholder' USING ERRCODE = 'insufficient_privilege';
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        GUARD,
        "DROP TRIGGER trg_run_artifacts_append_only ON shifde.run_artifacts",
        "CREATE TRIGGER trg_run_artifacts_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.run_artifacts FOR EACH ROW EXECUTE FUNCTION shifde.run_artifacts_guard()",
        "GRANT UPDATE (content) ON shifde.run_artifacts TO shifde_app",
        "GRANT UPDATE (arguments) ON shifde.tool_invocations TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "REVOKE UPDATE (arguments) ON shifde.tool_invocations FROM shifde_app",
        "REVOKE UPDATE (content) ON shifde.run_artifacts FROM shifde_app",
        "DROP TRIGGER trg_run_artifacts_append_only ON shifde.run_artifacts",
        "CREATE TRIGGER trg_run_artifacts_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.run_artifacts FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        "DROP FUNCTION shifde.run_artifacts_guard()",
    )
