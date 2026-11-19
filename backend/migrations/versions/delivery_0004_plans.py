"""Delivery, part 4: template artifacts, deployment plans, approvals and the job-to-plan key.

Revision ID: delivery_0004
Revises: delivery_0003

Design 2026-11-17 sections 3 and 4, section 9 items 1, 5, 9, 11, 12 and 17 (DECISIONS B-2500,
B-2501, B-2665, B-2673, B-2675..B-2679).

* `delivery_artifacts`: the template archive a plan request builds (`databricks.app/v1`), its
  sha256 `digest` and `size`; `content` is the archive itself, capped at 5 MB
  (`octet_length(content) = size`, `size BETWEEN 1 AND 5242880`). Written once, never changed.
* `delivery_plans`: one plan of an artifact for a target at the target's `revision`
  (`target_revision`), with `status` `requested -> planning -> sealed -> approved | rejected |
  expired`, or `plan_failed`. A plan in `sealed`, `approved`, `rejected` or `expired` holds its
  digest, steps, snapshot, adapter version and toolchain digest (`ck_delivery_plans__sealed`), and
  `expires_at = sealed_at + 15 minutes` (`ck_delivery_plans__expiry`).
* `delivery_approvals`: the one decision (`approve` or `reject`) of a plan, by whom, on which
  digest; `uq_delivery_approvals__tenant_plan (tenant_id, plan_id)` (design: UNIQUE (`plan_id`);
  `tenant_id` joins it as in B-2665).
* `fk_delivery_jobs__plan`: `delivery_jobs (tenant_id, plan_id)` now references
  `delivery_plans (tenant_id, id)` (B-2673), so a job can name only its own organization's plan.
* `shifde.delivery_plans_seal_guard()`: a SECURITY INVOKER `BEFORE UPDATE` trigger on
  `delivery_plans`. Once a plan has `sealed_at`, only `status`, `status_reason` and `updated_at`
  may change; no status leaves `approved`, `rejected`, `expired` or `plan_failed`; every other
  status change is one of `requested -> planning`, `requested | planning -> sealed | plan_failed`,
  `sealed -> approved | rejected | expired` (42501 otherwise).
* `shifde.delivery_approvals_guard()`: a SECURITY INVOKER `BEFORE INSERT` trigger on
  `delivery_approvals`. It reads the plan under the caller's row-level security and refuses
  (42501) a decision by the plan's requester or on a plan it cannot see, and (`check_violation`)
  one on a plan that is not `sealed`, on another digest than the sealed one, or at or after
  `expires_at`.
  Both functions: `SET search_path = pg_catalog, pg_temp`, every object schema-qualified, EXECUTE
  revoked from PUBLIC. No SECURITY DEFINER function is added.

Isolation: the three tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); keys to
targets, artifacts and plans are composite `(tenant_id, ...)`. Grants to `shifde_app`: artifacts
SELECT, INSERT; plans SELECT, INSERT (the request columns) and UPDATE (status, status_reason,
steps, snapshot, adapter_version, toolchain_digest, digest, sealed_at, expires_at); approvals
SELECT, INSERT. No DELETE on any.

Rollback: drops the job-to-plan key, then approvals, plans and artifacts, then both trigger
functions (data-destructive: every organization's plans, approvals and artifacts are lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0004"
down_revision = "delivery_0003"
branch_labels = None
depends_on = None

TENANT = "tenant_id = shifde.current_tenant_id()"
SEAL_GUARD_SIGNATURE = "delivery_plans_seal_guard()"
APPROVALS_GUARD_SIGNATURE = "delivery_approvals_guard()"
PLAN_INSERT_COLUMNS = (
    "tenant_id, id, target_id, target_revision, artifact_id, artifact_digest, application_key, "
    "display_name, requested_by"
)
PLAN_UPDATE_COLUMNS = (
    "status, status_reason, steps, snapshot, adapter_version, toolchain_digest, digest, "
    "sealed_at, expires_at"
)

SEAL_GUARD = """CREATE FUNCTION shifde.delivery_plans_seal_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF OLD.sealed_at IS NOT NULL AND (
            NEW.tenant_id, NEW.id, NEW.target_id, NEW.target_revision, NEW.artifact_id,
            NEW.artifact_digest, NEW.application_key, NEW.display_name, NEW.requested_by,
            NEW.steps, NEW.snapshot, NEW.adapter_version, NEW.toolchain_digest, NEW.digest,
            NEW.sealed_at, NEW.expires_at, NEW.created_at)
          IS DISTINCT FROM (
            OLD.tenant_id, OLD.id, OLD.target_id, OLD.target_revision, OLD.artifact_id,
            OLD.artifact_digest, OLD.application_key, OLD.display_name, OLD.requested_by,
            OLD.steps, OLD.snapshot, OLD.adapter_version, OLD.toolchain_digest, OLD.digest,
            OLD.sealed_at, OLD.expires_at, OLD.created_at)
        THEN
          RAISE EXCEPTION 'a sealed plan is immutable' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
            (OLD.status = 'requested' AND NEW.status IN ('planning', 'sealed', 'plan_failed'))
            OR (OLD.status = 'planning' AND NEW.status IN ('sealed', 'plan_failed'))
            OR (OLD.status = 'sealed' AND NEW.status IN ('approved', 'rejected', 'expired')))
        THEN
          RAISE EXCEPTION 'the plan status change is not allowed'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$"""

APPROVALS_GUARD = """CREATE FUNCTION shifde.delivery_approvals_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE
        v_plan record;
      BEGIN
        SELECT p.requested_by, p.status, p.digest, p.expires_at INTO v_plan
          FROM shifde.delivery_plans p
         WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.plan_id;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'plan not found' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.decided_by = v_plan.requested_by THEN
          RAISE EXCEPTION 'a plan is decided by a person other than its requester'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF v_plan.status <> 'sealed' OR NEW.plan_digest IS DISTINCT FROM v_plan.digest
           OR pg_catalog.now() >= v_plan.expires_at THEN
          RAISE EXCEPTION 'the plan is not sealed, its digest differs or it has expired'
            USING ERRCODE = 'check_violation';
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
        # ------------------------------------------------------------------ artifacts
        """CREATE TABLE shifde.delivery_artifacts (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            target_kind text NOT NULL CHECK (target_kind = 'databricks.app/v1'),
            digest text NOT NULL CHECK (digest ~ '^[a-f0-9]{64}$'),
            size integer NOT NULL CHECK (size BETWEEN 1 AND 5242880),
            content bytea NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_artifacts__size CHECK (octet_length(content) = size),
            CONSTRAINT uq_delivery_artifacts__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        *_tenant_table("delivery_artifacts"),
        "GRANT SELECT, INSERT ON shifde.delivery_artifacts TO shifde_app",
        # ------------------------------------------------------------------ plans
        """CREATE TABLE shifde.delivery_plans (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            target_id uuid NOT NULL,
            target_revision integer NOT NULL CHECK (target_revision >= 1),
            artifact_id uuid NOT NULL,
            artifact_digest text NOT NULL CHECK (artifact_digest ~ '^[a-f0-9]{64}$'),
            application_key text NOT NULL CHECK (application_key ~ '^[a-z][a-z0-9-]{0,62}$'),
            display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
            status text NOT NULL DEFAULT 'requested' CHECK (status IN (
                'requested', 'planning', 'sealed', 'approved', 'rejected', 'expired',
                'plan_failed')),
            status_reason text,
            requested_by uuid NOT NULL,
            steps jsonb,
            snapshot jsonb,
            adapter_version text,
            toolchain_digest text CHECK (toolchain_digest ~ '^[a-f0-9]{64}$'),
            digest text CHECK (digest ~ '^[a-f0-9]{64}$'),
            sealed_at timestamptz,
            expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_plans__sealed CHECK (
                status NOT IN ('sealed', 'approved', 'rejected', 'expired') OR (
                    digest IS NOT NULL AND sealed_at IS NOT NULL AND expires_at IS NOT NULL
                    AND steps IS NOT NULL AND snapshot IS NOT NULL
                    AND adapter_version IS NOT NULL AND toolchain_digest IS NOT NULL)),
            CONSTRAINT ck_delivery_plans__expiry CHECK (
                sealed_at IS NULL OR expires_at = sealed_at + interval '15 minutes'),
            CONSTRAINT uq_delivery_plans__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_delivery_plans__target FOREIGN KEY (tenant_id, target_id)
                REFERENCES shifde.delivery_targets (tenant_id, id),
            CONSTRAINT fk_delivery_plans__artifact FOREIGN KEY (tenant_id, artifact_id)
                REFERENCES shifde.delivery_artifacts (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_plans__target ON shifde.delivery_plans (tenant_id, target_id)",
        "CREATE INDEX ix_delivery_plans__artifact ON shifde.delivery_plans "
        "(tenant_id, artifact_id)",
        *_tenant_table("delivery_plans"),
        SEAL_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{SEAL_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_delivery_plans_seal_guard BEFORE UPDATE ON shifde.delivery_plans "
        "FOR EACH ROW EXECUTE FUNCTION shifde.delivery_plans_seal_guard()",
        "GRANT SELECT ON shifde.delivery_plans TO shifde_app",
        f"GRANT INSERT ({PLAN_INSERT_COLUMNS}) ON shifde.delivery_plans TO shifde_app",
        f"GRANT UPDATE ({PLAN_UPDATE_COLUMNS}) ON shifde.delivery_plans TO shifde_app",
        # ------------------------------------------------------------------ approvals
        """CREATE TABLE shifde.delivery_approvals (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            plan_id uuid NOT NULL,
            plan_digest text NOT NULL CHECK (plan_digest ~ '^[a-f0-9]{64}$'),
            decided_by uuid NOT NULL,
            decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
            note text CHECK (length(note) <= 2000),
            decided_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_delivery_approvals__tenant_plan UNIQUE (tenant_id, plan_id),
            CONSTRAINT fk_delivery_approvals__plan FOREIGN KEY (tenant_id, plan_id)
                REFERENCES shifde.delivery_plans (tenant_id, id)
        )""",
        *_tenant_table("delivery_approvals"),
        APPROVALS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{APPROVALS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_delivery_approvals_guard BEFORE INSERT ON shifde.delivery_approvals "
        "FOR EACH ROW EXECUTE FUNCTION shifde.delivery_approvals_guard()",
        "GRANT SELECT, INSERT ON shifde.delivery_approvals TO shifde_app",
        # ------------------------------------------------------------------ jobs -> plans (B-2673)
        "ALTER TABLE shifde.delivery_jobs ADD CONSTRAINT fk_delivery_jobs__plan "
        "FOREIGN KEY (tenant_id, plan_id) REFERENCES shifde.delivery_plans (tenant_id, id)",
        "CREATE INDEX ix_delivery_jobs__plan ON shifde.delivery_jobs (tenant_id, plan_id)",
    )


def downgrade() -> None:
    _run(
        "DROP INDEX shifde.ix_delivery_jobs__plan",
        "ALTER TABLE shifde.delivery_jobs DROP CONSTRAINT fk_delivery_jobs__plan",
        "DROP TABLE shifde.delivery_approvals",
        "DROP TABLE shifde.delivery_plans",
        "DROP TABLE shifde.delivery_artifacts",
        f"DROP FUNCTION shifde.{APPROVALS_GUARD_SIGNATURE}",
        f"DROP FUNCTION shifde.{SEAL_GUARD_SIGNATURE}",
    )
