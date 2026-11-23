"""Runtime: an ended connection invoke job may drop its queued request from the payload (issue #224).

Revision ID: runtime_0007
Revises: runtime_0006

A queued connection invoke (`jobs.kind = 'connection.invoke'`, issue #190) keeps the parsed
request (query, body, the remaining headers) and the write flag in `jobs.payload` after the
invocation ends, and `shifde_app` had no UPDATE on `payload`. This revision:

* grants `UPDATE (payload)` on `shifde.jobs` to `shifde_app` (forced row-level security and the
  `tenant_isolation` policy are unchanged, so a client reaches only its own jobs);
* adds `shifde.jobs_payload_guard()` and the `BEFORE UPDATE ... FOR EACH ROW` trigger
  `trg_jobs_payload_guard`. An update that leaves `payload` unchanged passes untouched. A change
  of `payload` is refused with SQLSTATE 23514 unless the job's kind is `connection.invoke`
  (old and new row), its new status is terminal (`done` or `dead`), the old payload is a JSON
  object and the new payload is exactly the old one without the keys `request` and `write`
  (`OLD.payload - 'request' - 'write'`), so the ids and `request_hash` stay for replays. It
  fires for every role, the schema owner included; the guard runs with the caller's rights (not
  SECURITY DEFINER), reads no table, has a fixed search_path and EXECUTE revoked from PUBLIC.

Deliberate differences from SETH (`tests/schema/deliberate_changes.yaml`): the `payload` column
grant of `jobs` and the new trigger `trg_jobs_payload_guard`.

Rollback: drops the trigger and the guard and revokes the column grant (a scrubbed payload stays
scrubbed).
"""

from __future__ import annotations

from alembic import op

revision = "runtime_0007"
down_revision = "runtime_0006"
branch_labels = None
depends_on = None

INVOKE_KIND = "connection.invoke"  # shifde.connections.service.INVOKE_JOB_KIND
TERMINAL_JOB = "('done', 'dead')"  # the terminal values of jobs.status (runtime_0001)

GUARD = f"""CREATE FUNCTION shifde.jobs_payload_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF NEW.payload IS NOT DISTINCT FROM OLD.payload THEN
    RETURN NEW;
  END IF;
  IF OLD.kind = '{INVOKE_KIND}' AND NEW.kind = '{INVOKE_KIND}'
     AND NEW.status IN {TERMINAL_JOB}
     AND pg_catalog.jsonb_typeof(OLD.payload) = 'object'
     AND NEW.payload = OLD.payload - 'request'::text - 'write'::text THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'a job payload changes only by dropping an ended invoke''s request'
    USING ERRCODE = '23514';
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        GUARD,
        "REVOKE ALL ON FUNCTION shifde.jobs_payload_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_jobs_payload_guard BEFORE UPDATE ON shifde.jobs "
        "FOR EACH ROW EXECUTE FUNCTION shifde.jobs_payload_guard()",
        "GRANT UPDATE (payload) ON shifde.jobs TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "REVOKE UPDATE (payload) ON shifde.jobs FROM shifde_app",
        "DROP TRIGGER trg_jobs_payload_guard ON shifde.jobs",
        "DROP FUNCTION shifde.jobs_payload_guard()",
    )
