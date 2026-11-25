"""Delivery, part 9: related principals of a plan decision, API key creators included.

Revision ID: delivery_0009
Revises: delivery_0008

Issue #352 (B-2733, B-2911; DECISIONS B-4113..B-4115).

* `delivery_plans.requested_api_key_id` and `delivery_approvals.decided_api_key_id`, both nullable:
  the API key the plan was requested with and the one it was decided with; NULL for a session, and
  for every row written before this revision. `fk_delivery_plans__requested_api_key` and
  `fk_delivery_approvals__decided_api_key` to `api_keys (tenant_id, id)`, `ON DELETE RESTRICT`.
  Both ids are claims of the app role, as `requested_by` and `decided_by` are: a raw `shifde_app`
  session can leave them NULL, and the guard refuses a key that is not the named principal's but
  cannot prove which key was used.
* `shifde.delivery_plans_seal_guard()` (CREATE OR REPLACE): delivery_0004's body, and
  `requested_api_key_id` joins the columns that never change on a sealed plan.
* `shifde.delivery_approvals_guard()` (CREATE OR REPLACE) refuses (42501) a decision in a session
  of no client or of another client; by the requester (as before); by a service account the
  requester owns; by the owner of the requesting service account; by a service account with the
  same owner; by the creator of the requesting key or a service account that creator owns; with a
  key the requester, or the owner of the requesting service account, created; and when a key named
  by the plan or the decision is not a key of its principal in this client. An unowned account and
  a key without a creator (NULL) relate nobody. The sealed, digest and expiry checks
  (`check_violation`) stay last. Two keys created by the same third person stay allowed (#395).
  Both functions keep their owner, their triggers and EXECUTE revoked from PUBLIC; SECURITY
  INVOKER, `SET search_path = pg_catalog, pg_temp`, every object schema-qualified.

Isolation: no table, policy, trigger or function is added and no SECURITY DEFINER function. Both
keys are composite `(tenant_id, ...)`, so a plan and a decision can name only a key of their own
organization. The guard reads `delivery_plans`, `service_accounts` and `api_keys` under the
caller's row-level security with the tenant in every WHERE; the session is the row's client, so a
row it does not see does not exist in that client. `shifde_app` can update neither
`service_accounts.owner_principal_id` nor `api_keys.created_by`.

Grants to `shifde_app`: INSERT on `delivery_plans.requested_api_key_id`; no UPDATE of either column
(`delivery_approvals` has a table-level INSERT and no UPDATE).

Rollback: restores both delivery_0004 bodies (`PREVIOUS_SEAL_GUARD`, `PREVIOUS_APPROVALS_GUARD`,
pinned against delivery_0004 by a test) and drops both keys and columns. Not refused when values
exist: every recorded key id and the related-principal rules in the database are lost.
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0009"
down_revision = "delivery_0008"
branch_labels = None
depends_on = None

SEAL_GUARD = """CREATE OR REPLACE FUNCTION shifde.delivery_plans_seal_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF OLD.sealed_at IS NOT NULL AND (
            NEW.tenant_id, NEW.id, NEW.target_id, NEW.target_revision, NEW.artifact_id,
            NEW.artifact_digest, NEW.application_key, NEW.display_name, NEW.requested_by,
            NEW.requested_api_key_id,
            NEW.steps, NEW.snapshot, NEW.adapter_version, NEW.toolchain_digest, NEW.digest,
            NEW.sealed_at, NEW.expires_at, NEW.created_at)
          IS DISTINCT FROM (
            OLD.tenant_id, OLD.id, OLD.target_id, OLD.target_revision, OLD.artifact_id,
            OLD.artifact_digest, OLD.application_key, OLD.display_name, OLD.requested_by,
            OLD.requested_api_key_id,
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

APPROVALS_GUARD = """CREATE OR REPLACE FUNCTION shifde.delivery_approvals_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE
        v_plan record;
        v_decider_owner uuid;
        v_requester_owner uuid;
        v_requesting_key_creator uuid;
        v_deciding_key_creator uuid;
      BEGIN
        -- NULL (a session of no client) is distinct from every tenant_id, so it is refused
        IF shifde.current_tenant_id() IS DISTINCT FROM NEW.tenant_id THEN
          RAISE EXCEPTION 'a plan is decided only in a session of its client'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        SELECT p.requested_by, p.requested_api_key_id, p.status, p.digest, p.expires_at
          INTO v_plan
          FROM shifde.delivery_plans p
         WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.plan_id;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'plan not found' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.decided_by = v_plan.requested_by THEN
          RAISE EXCEPTION 'a plan is decided by a person other than its requester'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- no row: the principal is not a service account of this client
        SELECT s.owner_principal_id INTO v_decider_owner
          FROM shifde.service_accounts s
         WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.decided_by;
        SELECT s.owner_principal_id INTO v_requester_owner
          FROM shifde.service_accounts s
         WHERE s.tenant_id = NEW.tenant_id AND s.id = v_plan.requested_by;
        -- an unowned account (NULL) equals nothing, so it is related to nobody
        IF v_decider_owner = v_plan.requested_by THEN
          RAISE EXCEPTION 'a plan is not decided by a service account its requester owns'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF v_requester_owner = NEW.decided_by THEN
          RAISE EXCEPTION 'a plan is not decided by the owner of the requesting service account'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF v_decider_owner = v_requester_owner THEN
          RAISE EXCEPTION 'a plan is not decided by a service account with the same owner'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF v_plan.requested_api_key_id IS NOT NULL THEN
          SELECT k.created_by INTO v_requesting_key_creator
            FROM shifde.api_keys k
           WHERE k.tenant_id = NEW.tenant_id AND k.id = v_plan.requested_api_key_id
             AND k.principal_id = v_plan.requested_by;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'the API key of the plan request is not found'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF v_requesting_key_creator = NEW.decided_by THEN
            RAISE EXCEPTION 'a plan is not decided by the creator of the requesting API key'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF v_requesting_key_creator = v_decider_owner THEN
            RAISE EXCEPTION 'a plan is not decided by a service account the creator of the requesting API key owns'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
        END IF;
        IF NEW.decided_api_key_id IS NOT NULL THEN
          SELECT k.created_by INTO v_deciding_key_creator
            FROM shifde.api_keys k
           WHERE k.tenant_id = NEW.tenant_id AND k.id = NEW.decided_api_key_id
             AND k.principal_id = NEW.decided_by;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'the API key of the decision is not found'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF v_deciding_key_creator = v_plan.requested_by THEN
            RAISE EXCEPTION 'a plan is not decided with an API key its requester created'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF v_deciding_key_creator = v_requester_owner THEN
            RAISE EXCEPTION 'a plan is not decided with an API key the owner of the requesting service account created'
              USING ERRCODE = 'insufficient_privilege';
          END IF;
        END IF;
        IF v_plan.status <> 'sealed' OR NEW.plan_digest IS DISTINCT FROM v_plan.digest
           OR pg_catalog.now() >= v_plan.expires_at THEN
          RAISE EXCEPTION 'the plan is not sealed, its digest differs or it has expired'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

# delivery_0004's SEAL_GUARD and APPROVALS_GUARD, verbatim but for OR REPLACE (a test pins them)
PREVIOUS_SEAL_GUARD = """CREATE OR REPLACE FUNCTION shifde.delivery_plans_seal_guard()
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

PREVIOUS_APPROVALS_GUARD = """CREATE OR REPLACE FUNCTION shifde.delivery_approvals_guard()
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


def upgrade() -> None:
    _run(
        "ALTER TABLE shifde.delivery_plans ADD COLUMN requested_api_key_id uuid",
        "ALTER TABLE shifde.delivery_plans ADD CONSTRAINT fk_delivery_plans__requested_api_key "
        "FOREIGN KEY (tenant_id, requested_api_key_id) "
        "REFERENCES shifde.api_keys (tenant_id, id) ON DELETE RESTRICT",
        "GRANT INSERT (requested_api_key_id) ON shifde.delivery_plans TO shifde_app",
        "ALTER TABLE shifde.delivery_approvals ADD COLUMN decided_api_key_id uuid",
        "ALTER TABLE shifde.delivery_approvals "
        "ADD CONSTRAINT fk_delivery_approvals__decided_api_key "
        "FOREIGN KEY (tenant_id, decided_api_key_id) "
        "REFERENCES shifde.api_keys (tenant_id, id) ON DELETE RESTRICT",
        SEAL_GUARD,
        APPROVALS_GUARD,
    )


def downgrade() -> None:
    _run(
        PREVIOUS_SEAL_GUARD,
        PREVIOUS_APPROVALS_GUARD,
        "ALTER TABLE shifde.delivery_approvals "
        "DROP CONSTRAINT fk_delivery_approvals__decided_api_key",
        "ALTER TABLE shifde.delivery_approvals DROP COLUMN decided_api_key_id",
        "ALTER TABLE shifde.delivery_plans DROP CONSTRAINT fk_delivery_plans__requested_api_key",
        "ALTER TABLE shifde.delivery_plans DROP COLUMN requested_api_key_id",
    )
