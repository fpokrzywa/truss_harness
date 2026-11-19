"""Delivery, part 3: the delivery job queue, the step journal, the lease definer and the sweep.

Revision ID: delivery_0003
Revises: delivery_0002

Design 2026-11-17 sections 3, 4 and 5, section 9 items 4, 5, 7, 8, 9, 13 and 16 (DECISIONS
B-2500, B-2501, B-2520, B-2530, B-2669..B-2671).

* `delivery_jobs`: the executor's work queue (`plan`, `apply`, `observe`, `verify`, `reconcile`).
  A leased job carries its executor, `lease_id`, `fence` (raised on every lease) and
  `lease_until`; `checkpoint_seq` is the last accepted checkpoint sequence. An `apply` job has
  `max_attempts = 1` (`ck_delivery_jobs__apply_once`): a platform write is never retried.
  `target_id` is an addition to design section 3 (the credential grant needs the target before
  plans and deployments exist); `plan_id` and `deployment_id` get their keys in 0004/0005.
* `delivery_journal`: one row per `(tenant_id, deployment_id, step_key)`, the intent recorded
  before a platform write and the receipt after it, with the job and fence that wrote it.
* `shifde.delivery_lease_job(uuid, uuid, text[])`: leases at most one due job of the session's
  organization (`current_tenant_id()` only; no argument names an organization) to an active
  executor of it, `FOR UPDATE SKIP LOCKED`. A repeated request id answers the same lease while it
  is still held by that executor and unexpired, otherwise nothing. A SECURITY DEFINER owned by
  `shifde_owner`, `SET search_path = pg_catalog, pg_temp`, every object schema-qualified, EXECUTE
  revoked from PUBLIC and granted to `shifde_app` only, no write of the `app.*` context. It exists
  because `shifde_app` holds no grant on the lease columns (`executor_id`, `lease_id`,
  `lease_request_id`, `fence`, `attempts`), so the application cannot forge a lease or a fence.
* `shifde.delivery_sweep_lost_jobs()`: SECURITY INVOKER (runs as `shifde_app` under row-level
  security), touches only the session organization's expired leases: an `apply` job becomes
  `lost` (outcome `unknown_outcome` when its deployment journal holds an intent, else `failed`),
  another kind is re-queued while `attempts < max_attempts`, else `lost`. Returns the count.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); keys to
targets, executors and jobs are composite `(tenant_id, ...)`. Grants to `shifde_app`: jobs SELECT,
INSERT (tenant_id, id, kind, plan_id, deployment_id, target_id, run_after, deadline, max_attempts)
and UPDATE (status, run_after, lease_until, heartbeat_at, checkpoint_seq, result); journal SELECT,
INSERT and UPDATE (state, receipt, recorded_at). No DELETE on either.

Rollback: drops both functions, then the journal, then the jobs (data-destructive: every
organization's queued jobs and journal rows are lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0003"
down_revision = "delivery_0002"
branch_labels = None
depends_on = None

TENANT = "tenant_id = shifde.current_tenant_id()"
LEASE_SIGNATURE = "delivery_lease_job(uuid, uuid, text[])"
SWEEP_SIGNATURE = "delivery_sweep_lost_jobs()"
JOB_INSERT_COLUMNS = (
    "tenant_id, id, kind, plan_id, deployment_id, target_id, run_after, deadline, max_attempts"
)
JOB_UPDATE_COLUMNS = "status, run_after, lease_until, heartbeat_at, checkpoint_seq, result"

LEASE_JOB = """CREATE FUNCTION shifde.delivery_lease_job(
       p_executor_id uuid, p_request_id uuid, p_kinds text[])
   RETURNS TABLE (job_id uuid, lease_id uuid, fence bigint, lease_until timestamptz)
   LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE
        v_tenant uuid := shifde.current_tenant_id();
        v_job uuid;
      BEGIN
        IF v_tenant IS NULL OR NOT EXISTS (
            SELECT 1 FROM shifde.delivery_executors e
             WHERE e.tenant_id = v_tenant AND e.id = p_executor_id AND e.status = 'active') THEN
          RAISE EXCEPTION 'job lease refused' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF EXISTS (SELECT 1 FROM shifde.delivery_jobs j
                    WHERE j.tenant_id = v_tenant AND j.lease_request_id = p_request_id) THEN
          RETURN QUERY
            SELECT j.id, j.lease_id, j.fence, j.lease_until FROM shifde.delivery_jobs j
             WHERE j.tenant_id = v_tenant AND j.lease_request_id = p_request_id
               AND j.status = 'leased' AND j.executor_id = p_executor_id
               AND j.lease_until > pg_catalog.now()
             ORDER BY j.id LIMIT 1;
          RETURN;
        END IF;
        SELECT j.id INTO v_job FROM shifde.delivery_jobs j
         WHERE j.tenant_id = v_tenant AND j.status = 'queued'
           AND j.run_after <= pg_catalog.now() AND j.kind = ANY (p_kinds)
           AND j.attempts < j.max_attempts
           AND (j.deadline IS NULL OR j.deadline > pg_catalog.now())
         ORDER BY j.run_after, j.id
           FOR UPDATE SKIP LOCKED LIMIT 1;
        IF v_job IS NULL THEN
          RETURN;
        END IF;
        UPDATE shifde.delivery_jobs j
           SET status = 'leased', executor_id = p_executor_id,
               lease_id = pg_catalog.gen_random_uuid(), lease_request_id = p_request_id,
               fence = j.fence + 1,
               lease_until = pg_catalog.now() + pg_catalog.make_interval(secs => 45),
               heartbeat_at = pg_catalog.now(), attempts = j.attempts + 1, checkpoint_seq = 0
         WHERE j.tenant_id = v_tenant AND j.id = v_job
        RETURNING j.id, j.lease_id, j.fence, j.lease_until
             INTO job_id, lease_id, fence, lease_until;
        RETURN NEXT;
      END $$"""

SWEEP_LOST_JOBS = """CREATE FUNCTION shifde.delivery_sweep_lost_jobs()
   RETURNS integer LANGUAGE sql VOLATILE SECURITY INVOKER
   SET search_path = pg_catalog, pg_temp AS
   $$ WITH swept AS (
        UPDATE shifde.delivery_jobs j
           SET status = CASE WHEN j.kind <> 'apply' AND j.attempts < j.max_attempts
                             THEN 'queued' ELSE 'lost' END,
               run_after = CASE WHEN j.kind <> 'apply' AND j.attempts < j.max_attempts
                                THEN pg_catalog.now() ELSE j.run_after END,
               result = CASE
                 WHEN j.kind = 'apply' AND EXISTS (
                     SELECT 1 FROM shifde.delivery_journal r
                      WHERE r.tenant_id = j.tenant_id AND r.deployment_id = j.deployment_id
                        AND r.state = 'intent')
                   THEN CAST('{"outcome": "unknown_outcome"}' AS jsonb)
                 WHEN j.kind = 'apply'
                   THEN CAST('{"outcome": "failed", "reason": "no platform write was attempted"}'
                             AS jsonb)
                 WHEN j.attempts < j.max_attempts THEN j.result
                 ELSE CAST('{"outcome": "failed", "reason": "executor lost"}' AS jsonb) END
         WHERE j.tenant_id = shifde.current_tenant_id() AND j.status = 'leased'
           AND j.lease_until <= pg_catalog.now()
        RETURNING 1)
      SELECT CAST(pg_catalog.count(*) AS integer) FROM swept $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} FOR ALL USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
    ]


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ jobs
        """CREATE TABLE shifde.delivery_jobs (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            kind text NOT NULL
                CHECK (kind IN ('plan', 'apply', 'observe', 'verify', 'reconcile')),
            plan_id uuid,
            deployment_id uuid,
            target_id uuid,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'leased', 'done', 'lost', 'cancelled')),
            run_after timestamptz NOT NULL DEFAULT now(),
            deadline timestamptz,
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
            executor_id uuid,
            lease_id uuid,
            lease_request_id uuid,
            fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
            lease_until timestamptz,
            heartbeat_at timestamptz,
            checkpoint_seq bigint NOT NULL DEFAULT 0,
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_jobs__apply_once CHECK (kind <> 'apply' OR max_attempts = 1),
            CONSTRAINT ck_delivery_jobs__lease CHECK (status <> 'leased' OR (
                executor_id IS NOT NULL AND lease_id IS NOT NULL AND lease_until IS NOT NULL
                AND fence >= 1)),
            CONSTRAINT uq_delivery_jobs__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_delivery_jobs__target FOREIGN KEY (tenant_id, target_id)
                REFERENCES shifde.delivery_targets (tenant_id, id),
            CONSTRAINT fk_delivery_jobs__executor FOREIGN KEY (tenant_id, executor_id)
                REFERENCES shifde.delivery_executors (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_jobs__due ON shifde.delivery_jobs (tenant_id, status, run_after)",
        "CREATE INDEX ix_delivery_jobs__lease_request ON shifde.delivery_jobs "
        "(tenant_id, lease_request_id)",
        *_tenant_table("delivery_jobs"),
        "GRANT SELECT ON shifde.delivery_jobs TO shifde_app",
        f"GRANT INSERT ({JOB_INSERT_COLUMNS}) ON shifde.delivery_jobs TO shifde_app",
        f"GRANT UPDATE ({JOB_UPDATE_COLUMNS}) ON shifde.delivery_jobs TO shifde_app",
        # ------------------------------------------------------------------ journal
        """CREATE TABLE shifde.delivery_journal (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            deployment_id uuid NOT NULL,
            step_key text NOT NULL CHECK (step_key ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'),
            input_digest text NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
            state text NOT NULL CHECK (state IN ('intent', 'complete')),
            receipt jsonb,
            job_id uuid NOT NULL,
            fence bigint NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_journal__receipt CHECK (
                (state = 'intent' AND receipt IS NULL)
                OR (state = 'complete' AND receipt IS NOT NULL)),
            CONSTRAINT pk_delivery_journal PRIMARY KEY (tenant_id, deployment_id, step_key),
            CONSTRAINT fk_delivery_journal__job FOREIGN KEY (tenant_id, job_id)
                REFERENCES shifde.delivery_jobs (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_journal__job ON shifde.delivery_journal (tenant_id, job_id)",
        *_tenant_table("delivery_journal"),
        "GRANT SELECT, INSERT ON shifde.delivery_journal TO shifde_app",
        "GRANT UPDATE (state, receipt, recorded_at) ON shifde.delivery_journal TO shifde_app",
        # ------------------------------------------------------------------ lease and sweep
        LEASE_JOB,
        f"REVOKE ALL ON FUNCTION shifde.{LEASE_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION shifde.{LEASE_SIGNATURE} TO shifde_app",
        SWEEP_LOST_JOBS,
        f"REVOKE ALL ON FUNCTION shifde.{SWEEP_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION shifde.{SWEEP_SIGNATURE} TO shifde_app",
    )


def downgrade() -> None:
    _run(
        f"DROP FUNCTION shifde.{SWEEP_SIGNATURE}",
        f"DROP FUNCTION shifde.{LEASE_SIGNATURE}",
        "DROP TABLE shifde.delivery_journal",
        "DROP TABLE shifde.delivery_jobs",
    )
