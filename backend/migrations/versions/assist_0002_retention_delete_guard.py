"""Assist, part 2: the assist delete guard also enforces legal hold, the retention floor and each
client's own run retention (issue #351).

Revision ID: assist_0002
Revises: assist_0001

`assist_0001` admits a DELETE on `assist_requests` and `assist_proposals` once the transaction
sets `shifde.retention_delete_cutoff` to a time after the row's `created_at`, and any
`shifde_app` session can set that setting; the legal hold, the retention floor and the client's
run retention were applied only by `shifde-admin maintenance delete` (issue #351, the follow-up
of #280 and #347; DECISIONS B-4010). This revision replaces the body of
`shifde.assist_retention_delete_guard()` (CREATE OR REPLACE: owner, EXECUTE revoked from PUBLIC
and the two triggers unchanged) so it keeps every assist_0001 check and, between the parse of the
setting and the `created_at` check, also raises 42501 (insufficient_privilege), with the
statements of runtime_0009 verbatim:

* when the cutoff is later than `now()` minus the run retention floor, `FLOOR_DAYS` days;
* when the row's client (`OLD.tenant_id`) has a legal hold on any data class in
  `shifde.retention_policies`, the rule `maintenance._delete_pass` skips a client by;
* when the cutoff is later than `now()` minus the client's effective run retention: the largest
  of `FLOOR_DAYS` and, for `run_artifacts` and `run_steps_content`, the client's stored
  `retain_days` in `shifde.retention_policies` or `RUN_DEFAULT_DAYS`. That is the rule
  `maintenance.effective_days` applies without `--older-than`; the command deletes assist rows
  with the run cutoff (Call S7-J), so the run data classes are the ones that apply (tests pin the
  literals to `RETENTION_FLOORS` and `RETENTION_DEFAULTS`).

The guard still runs with the caller's rights (SECURITY INVOKER), a fixed search_path and every
relation schema-qualified. Its read of `retention_policies` goes through row-level security:
every policy of the two tables is `tenant_isolation`, so a `shifde_app` session reaches a row
only in the context of that row's client, where the client's own retention rows are visible; the
schema owner (a direct delete, or the cascade from a request to its proposals) bypasses row-level
security and sees them too.

No deliberate difference from SETH is added: the assist tables are not SETH tables and the
function body is not compared.

Rollback: restores the assist_0001 body exactly (`PREVIOUS_GUARD`, pinned against assist_0001's
`GUARD` by a test).
"""

from __future__ import annotations

from alembic import op

revision = "assist_0002"
down_revision = "assist_0001"
branch_labels = None
depends_on = None

# max(RETENTION_FLOORS[c] for c in shifde.cli.maintenance.RUN_CLASSES); pinned by
# tests/cli/test_maintenance_delete.py
FLOOR_DAYS = 1
# RETENTION_DEFAULTS[c] for c in shifde.cli.maintenance.RUN_CLASSES; pinned by the same tests
RUN_DEFAULT_DAYS = {"run_artifacts": 90, "run_steps_content": 90}

GUARD = f"""CREATE OR REPLACE FUNCTION shifde.assist_retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
  v_days integer;
BEGIN
  IF v_setting IS NULL OR v_setting = '' THEN
    RAISE EXCEPTION 'assist rows are deleted only by the retention delete'
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
  IF OLD.created_at IS NULL OR OLD.created_at >= v_cutoff THEN
    RAISE EXCEPTION 'only assist rows created before the retention cutoff can be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN OLD;
END $$"""

# assist_0001's GUARD, verbatim (a test pins it equal)
PREVIOUS_GUARD = """CREATE FUNCTION shifde.assist_retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
BEGIN
  IF v_setting IS NULL OR v_setting = '' THEN
    RAISE EXCEPTION 'assist rows are deleted only by the retention delete'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  BEGIN
    v_cutoff := v_setting::timestamptz;
  EXCEPTION WHEN data_exception THEN
    RAISE EXCEPTION 'the retention delete cutoff is not a timestamp'
      USING ERRCODE = 'insufficient_privilege';
  END;
  IF OLD.created_at IS NULL OR OLD.created_at >= v_cutoff THEN
    RAISE EXCEPTION 'only assist rows created before the retention cutoff can be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN OLD;
END $$"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(GUARD)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        PREVIOUS_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    )
