"""Runtime: the retention delete guard refuses a session of another client (issue #364).

Revision ID: runtime_0010
Revises: runtime_0009

A `shifde_app` session can change `app.tenant_id` inside a DELETE; the guard then reads no
retention policy of the row's client (item 5 of the isolation review of #351; B-4089). On the run
tables the guard's read of `shifde.runs` goes through row-level security too and finds no run, so
such a delete was already refused, as a run that had not ended; the schema owner, who bypasses
row-level security, was not refused. This revision replaces the body of
`shifde.retention_delete_guard()` (CREATE OR REPLACE: owner, EXECUTE revoked from PUBLIC and the
nine triggers unchanged) so that, after the parse of the cutoff and before any read of a policy,
it raises 42501 when the session's client is not the row's.

A BEFORE DELETE row trigger runs per row, after the row passed row-level security and the
statement's WHERE (and any CTE or subquery it reads) and before the row goes. The guard therefore
sees `app.tenant_id` as the DELETE itself left it: a `set_config('app.tenant_id', ...)` in the
WHERE or a CTE has run, and nothing of the caller's runs between the check and the delete. A
RETURNING list runs after the row's guard; what it switches is met by the guard of the rows a
cascade deletes, at the end of the statement.

The check is `shifde.current_tenant_id() IS DISTINCT FROM OLD.tenant_id`: a session of no client
(NULL) is refused, for every role, the schema owner included; the column is NOT NULL on every
guarded table. The legitimate caller, `shifde-admin maintenance delete`, sets the client of each
pass before it deletes.

The guard still runs with the caller's rights (SECURITY INVOKER), a fixed search_path and every
relation schema-qualified; it reads the setting through `shifde.current_tenant_id()` and writes
none. No deliberate difference from SETH is added: the function body is not compared.

Rollback: restores the runtime_0009 body exactly (`PREVIOUS_GUARD`, pinned against runtime_0009's
`GUARD` by a test).
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0010"
down_revision = "runtime_0009"
branch_labels = None
depends_on = None

# max(RETENTION_FLOORS[c] for c in shifde.cli.maintenance.RUN_CLASSES); pinned by
# tests/cli/test_maintenance_delete.py
FLOOR_DAYS = 1
# RETENTION_DEFAULTS[c] for c in shifde.cli.maintenance.RUN_CLASSES; pinned by the same tests
RUN_DEFAULT_DAYS = {"run_artifacts": 90, "run_steps_content": 90}

GUARD = f"""CREATE OR REPLACE FUNCTION shifde.retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
  v_run uuid;
  v_days integer;
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
  -- NULL (a session of no client) is distinct from every tenant_id, so it is refused
  IF shifde.current_tenant_id() IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'the rows of a client can be deleted only in a session of that client'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF v_cutoff > pg_catalog.now() - pg_catalog.make_interval(days => {FLOOR_DAYS}) THEN
    RAISE EXCEPTION 'the retention delete cutoff is later than the retention floor allows'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF EXISTS (SELECT 1 FROM shifde.retention_policies p
              WHERE p.tenant_id = OLD.tenant_id AND p.legal_hold) THEN
    RAISE EXCEPTION 'the rows of a client on legal hold cannot be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- maintenance.effective_days without --older-than (GREATEST and COALESCE are not functions)
  v_days := GREATEST({FLOOR_DAYS},
    COALESCE((SELECT p.retain_days FROM shifde.retention_policies p
               WHERE p.tenant_id = OLD.tenant_id AND p.data_class = 'run_artifacts'),
             {RUN_DEFAULT_DAYS["run_artifacts"]}),
    COALESCE((SELECT p.retain_days FROM shifde.retention_policies p
               WHERE p.tenant_id = OLD.tenant_id AND p.data_class = 'run_steps_content'),
             {RUN_DEFAULT_DAYS["run_steps_content"]}));
  IF v_cutoff > pg_catalog.now() - pg_catalog.make_interval(days => v_days) THEN
    RAISE EXCEPTION 'the retention delete cutoff is inside the client''s retention'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
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

# runtime_0009's GUARD, verbatim (a test pins it equal)
PREVIOUS_GUARD = """CREATE OR REPLACE FUNCTION shifde.retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
  v_run uuid;
  v_days integer;
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
  IF v_cutoff > pg_catalog.now() - pg_catalog.make_interval(days => 1) THEN
    RAISE EXCEPTION 'the retention delete cutoff is later than the retention floor allows'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF EXISTS (SELECT 1 FROM shifde.retention_policies p
              WHERE p.tenant_id = OLD.tenant_id AND p.legal_hold) THEN
    RAISE EXCEPTION 'the rows of a client on legal hold cannot be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- maintenance.effective_days without --older-than (GREATEST and COALESCE are not functions)
  v_days := GREATEST(1,
    COALESCE((SELECT p.retain_days FROM shifde.retention_policies p
               WHERE p.tenant_id = OLD.tenant_id AND p.data_class = 'run_artifacts'),
             90),
    COALESCE((SELECT p.retain_days FROM shifde.retention_policies p
               WHERE p.tenant_id = OLD.tenant_id AND p.data_class = 'run_steps_content'),
             90));
  IF v_cutoff > pg_catalog.now() - pg_catalog.make_interval(days => v_days) THEN
    RAISE EXCEPTION 'the retention delete cutoff is inside the client''s retention'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
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


def upgrade() -> None:
    op.get_bind().exec_driver_sql(GUARD)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(PREVIOUS_GUARD)
