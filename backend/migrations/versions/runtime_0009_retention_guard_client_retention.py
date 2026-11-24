"""Runtime: the retention delete guard also enforces each client's own run retention (issue #347).

Revision ID: runtime_0009
Revises: runtime_0008

`runtime_0008` refuses a retention delete whose cutoff is later than `now()` minus the run
retention floor, but any cutoff older than that floor is admitted, so a `shifde_app` session can
set a cutoff inside a client's stored `retain_days` (or the 90-day default) and delete runs the
command would keep (issue #347, the follow-up from #174's isolation review; DECISIONS B-3993).
This revision replaces the body of `shifde.retention_delete_guard()` (CREATE OR REPLACE: owner,
EXECUTE revoked from PUBLIC and the nine triggers unchanged) so it keeps every runtime_0008 check,
in the same order, and right after the legal-hold check also raises 42501
(insufficient_privilege) when the cutoff is later than `now()` minus the client's effective run
retention: the largest of `FLOOR_DAYS` and, for `run_artifacts` and `run_steps_content`, the
client's stored `retain_days` in `shifde.retention_policies` or `RUN_DEFAULT_DAYS`. That is the
rule `maintenance.effective_days` applies without `--older-than` (tests pin the literals to
`RETENTION_FLOORS` and `RETENTION_DEFAULTS`).

The guard still runs with the caller's rights (SECURITY INVOKER), a fixed search_path and every
relation schema-qualified. Its read of `retention_policies` goes through row-level security:
every policy of the nine tables is `tenant_isolation`, so a `shifde_app` session reaches a row
only in the context of that row's client, where the client's own retention rows are visible; the
schema owner bypasses row-level security and sees them too.

No deliberate difference from SETH is added: the function body is not compared, and the nine
`*_retention_delete` triggers (listed since runtime_0006) are unchanged.

Rollback: restores the runtime_0008 body exactly (`PREVIOUS_GUARD`, pinned against
runtime_0008's `GUARD` by a test).
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0009"
down_revision = "runtime_0008"
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

# runtime_0008's GUARD, verbatim (a test pins it equal)
PREVIOUS_GUARD = """CREATE OR REPLACE FUNCTION shifde.retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
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
  IF v_cutoff > pg_catalog.now() - pg_catalog.make_interval(days => 1) THEN
    RAISE EXCEPTION 'the retention delete cutoff is later than the retention floor allows'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF EXISTS (SELECT 1 FROM shifde.retention_policies p
              WHERE p.tenant_id = OLD.tenant_id AND p.legal_hold) THEN
    RAISE EXCEPTION 'the rows of a client on legal hold cannot be deleted'
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
