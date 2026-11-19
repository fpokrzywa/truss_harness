"""Delivery, part 5: deployments, native-resource ownership and the deployment keys of jobs and
the journal.

Revision ID: delivery_0005
Revises: delivery_0004

Design 2026-11-17 sections 3 and 4, section 9 items 4, 6, 7, 8, 10, 11 and 18 (DECISIONS B-2500,
B-2501, B-2673, B-2681..B-2687).

* `delivery_deployments`: one deployment of an approved plan (`uq_delivery_deployments__tenant_plan
  (tenant_id, plan_id)`; design: UNIQUE (`plan_id`), `tenant_id` joins it as in B-2665), on the
  plan's target and `application_key`, with `status` `queued -> leased -> applying -> observing ->
  verifying -> verified`, or `expired`, `cancelled`, `failed`, `unknown_outcome`,
  `resolved_failed`. It holds the apply `receipt`, the `verification` report, the observe
  deadline (`observe_deadline`), the last reconcile evidence (`reconcile_evidence`) and, once a
  person marks an unknown outcome failed, `resolved_by`, `resolved_note` (at most 2000
  characters) and `resolved_at` (`ck_delivery_deployments__resolved`). `observe_deadline`,
  `reconcile_evidence` and `resolved_at` are additions to design section 3 (B-2682).
* `delivery_native_resources`: which application (`application_key`) owns a native resource
  `(resource_type, native_id)` on a target, and the deployment that wrote it; one row per
  `(tenant_id, target_id, resource_type, native_id)`. Written once, never changed.
* `fk_delivery_jobs__deployment` and `fk_delivery_journal__deployment`: `delivery_jobs (tenant_id,
  deployment_id)` and `delivery_journal (tenant_id, deployment_id)` now reference
  `delivery_deployments (tenant_id, id)` (B-2673), so a job or a journal step can name only its
  own organization's deployment.
* `shifde.delivery_deployments_status_guard()`: a SECURITY INVOKER `BEFORE UPDATE` trigger on
  `delivery_deployments`. Every status change is one of `queued -> leased | expired | cancelled`,
  `leased -> applying | observing | failed | unknown_outcome`, `applying -> observing | failed |
  unknown_outcome`, `observing -> verifying | failed`, `verifying -> verified | failed`,
  `unknown_outcome -> resolved_failed` (42501 otherwise); `verified`, `failed`, `expired`,
  `cancelled` and `resolved_failed` are final. `SET search_path = pg_catalog, pg_temp`, EXECUTE
  revoked from PUBLIC. No SECURITY DEFINER function is added, and `delivery_lease_job` and
  `delivery_sweep_lost_jobs` are unchanged.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); keys to
targets, plans and deployments are composite `(tenant_id, ...)`. Grants to `shifde_app`:
deployments SELECT, INSERT (tenant_id, id, plan_id, target_id, application_key) and UPDATE
(status, status_reason, receipt, verification, observe_deadline, reconcile_evidence, resolved_by,
resolved_note, resolved_at); native resources SELECT, INSERT. No DELETE on either.

Rollback: drops the jobs and journal keys and the jobs index, then native resources, then
deployments (with the guard trigger), then the guard function (data-destructive: every
organization's deployments and ownership rows are lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0005"
down_revision = "delivery_0004"
branch_labels = None
depends_on = None

TENANT = "tenant_id = shifde.current_tenant_id()"
STATUS_GUARD_SIGNATURE = "delivery_deployments_status_guard()"
DEPLOYMENT_INSERT_COLUMNS = "tenant_id, id, plan_id, target_id, application_key"
DEPLOYMENT_UPDATE_COLUMNS = (
    "status, status_reason, receipt, verification, observe_deadline, reconcile_evidence, "
    "resolved_by, resolved_note, resolved_at"
)

STATUS_GUARD = """CREATE FUNCTION shifde.delivery_deployments_status_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
            (OLD.status = 'queued' AND NEW.status IN ('leased', 'expired', 'cancelled'))
            OR (OLD.status = 'leased'
                AND NEW.status IN ('applying', 'observing', 'failed', 'unknown_outcome'))
            OR (OLD.status = 'applying'
                AND NEW.status IN ('observing', 'failed', 'unknown_outcome'))
            OR (OLD.status = 'observing' AND NEW.status IN ('verifying', 'failed'))
            OR (OLD.status = 'verifying' AND NEW.status IN ('verified', 'failed'))
            OR (OLD.status = 'unknown_outcome' AND NEW.status = 'resolved_failed'))
        THEN
          RAISE EXCEPTION 'the deployment status change is not allowed'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$"""


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
        # ------------------------------------------------------------------ deployments
        """CREATE TABLE shifde.delivery_deployments (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            plan_id uuid NOT NULL,
            target_id uuid NOT NULL,
            application_key text NOT NULL,
            status text NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'leased', 'applying', 'observing', 'verifying', 'verified', 'expired',
                'cancelled', 'failed', 'unknown_outcome', 'resolved_failed')),
            status_reason text,
            receipt jsonb,
            verification jsonb,
            observe_deadline timestamptz,
            reconcile_evidence jsonb,
            resolved_by uuid,
            resolved_note text CHECK (length(resolved_note) <= 2000),
            resolved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_deployments__resolved CHECK (
                status <> 'resolved_failed' OR (
                    resolved_by IS NOT NULL AND resolved_note IS NOT NULL
                    AND resolved_at IS NOT NULL)),
            CONSTRAINT uq_delivery_deployments__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_delivery_deployments__tenant_plan UNIQUE (tenant_id, plan_id),
            CONSTRAINT fk_delivery_deployments__plan FOREIGN KEY (tenant_id, plan_id)
                REFERENCES shifde.delivery_plans (tenant_id, id),
            CONSTRAINT fk_delivery_deployments__target FOREIGN KEY (tenant_id, target_id)
                REFERENCES shifde.delivery_targets (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_deployments__target_status ON shifde.delivery_deployments "
        "(tenant_id, target_id, status)",
        *_tenant_table("delivery_deployments"),
        STATUS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{STATUS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_delivery_deployments_status_guard BEFORE UPDATE "
        "ON shifde.delivery_deployments "
        "FOR EACH ROW EXECUTE FUNCTION shifde.delivery_deployments_status_guard()",
        "GRANT SELECT ON shifde.delivery_deployments TO shifde_app",
        f"GRANT INSERT ({DEPLOYMENT_INSERT_COLUMNS}) ON shifde.delivery_deployments TO shifde_app",
        f"GRANT UPDATE ({DEPLOYMENT_UPDATE_COLUMNS}) ON shifde.delivery_deployments TO shifde_app",
        # ------------------------------------------------------------------ native resources
        """CREATE TABLE shifde.delivery_native_resources (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            target_id uuid NOT NULL,
            application_key text NOT NULL,
            resource_type text NOT NULL,
            native_id text NOT NULL,
            deployment_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_delivery_native_resources
                PRIMARY KEY (tenant_id, target_id, resource_type, native_id),
            CONSTRAINT fk_delivery_native_resources__target FOREIGN KEY (tenant_id, target_id)
                REFERENCES shifde.delivery_targets (tenant_id, id),
            CONSTRAINT fk_delivery_native_resources__deployment
                FOREIGN KEY (tenant_id, deployment_id)
                REFERENCES shifde.delivery_deployments (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_native_resources__deployment "
        "ON shifde.delivery_native_resources (tenant_id, deployment_id)",
        *_tenant_table("delivery_native_resources"),
        "GRANT SELECT, INSERT ON shifde.delivery_native_resources TO shifde_app",
        # ------------------------------------------------------------------ jobs, journal (B-2673)
        "ALTER TABLE shifde.delivery_jobs ADD CONSTRAINT fk_delivery_jobs__deployment "
        "FOREIGN KEY (tenant_id, deployment_id) "
        "REFERENCES shifde.delivery_deployments (tenant_id, id)",
        "CREATE INDEX ix_delivery_jobs__deployment ON shifde.delivery_jobs "
        "(tenant_id, deployment_id)",
        "ALTER TABLE shifde.delivery_journal ADD CONSTRAINT fk_delivery_journal__deployment "
        "FOREIGN KEY (tenant_id, deployment_id) "
        "REFERENCES shifde.delivery_deployments (tenant_id, id)",
    )


def downgrade() -> None:
    _run(
        "ALTER TABLE shifde.delivery_journal DROP CONSTRAINT fk_delivery_journal__deployment",
        "DROP INDEX shifde.ix_delivery_jobs__deployment",
        "ALTER TABLE shifde.delivery_jobs DROP CONSTRAINT fk_delivery_jobs__deployment",
        "DROP TABLE shifde.delivery_native_resources",
        "DROP TABLE shifde.delivery_deployments",
        f"DROP FUNCTION shifde.{STATUS_GUARD_SIGNATURE}",
    )
