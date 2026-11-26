"""Delivery, part 10: the app role may change a Palantir target's `version_id` binding, and nothing else.

Revision ID: delivery_0010
Revises: delivery_0009

Issue #397 (DECISIONS B-4192; supervisor ruling pending, S-075). The migration only; the audited
PATCH route of a Palantir target's `version_id` is built on top of it later.

* `GRANT UPDATE (bindings) ON shifde.delivery_targets TO shifde_app`. With delivery_0001's
  `UPDATE (status, claim_id, revision)` the app role may now update four columns. `updated_at` is
  set by `trg_delivery_targets_updated_at` (`shifde.set_updated_at()`, delivery_0001), so it gets
  no grant.
* `shifde.delivery_targets_bindings_guard()` (SECURITY INVOKER, `SET search_path = pg_catalog,
  pg_temp`, EXECUTE revoked from PUBLIC) and its trigger `trg_delivery_targets_bindings_guard`,
  `BEFORE UPDATE OF bindings ... FOR EACH ROW`: when `bindings` changes (IS DISTINCT FROM), it
  raises 42501 unless all of these hold: the target is `palantir` before and after; both values
  are JSON objects; `OLD.bindings - 'version_id' = NEW.bindings - 'version_id'` (no other key is
  added, removed or changed); and `NEW.bindings ->> 'version_id'` is a JSON string in the
  canonical lowercase hyphenated UUID form (how `PalantirBindings` stores it). The test is
  `IS NOT TRUE`, so a NULL anywhere is a refusal. An update of `status`, `claim_id` or `revision`
  alone does not fire the trigger; an update that sets `bindings` to its current value passes.
* The table's owner (the schema owner; a superuser is a member of every role) is not bound by the
  guard: it can drop the trigger, so binding it would protect nothing, and its data migrations and
  the test fixtures that set bindings as the schema owner keep working. Every other role is
  (`pg_has_role(current_user, <table owner>, 'MEMBER')`; `shifde_app` is not a member of
  `shifde_owner`). No SECURITY DEFINER function updates `delivery_targets`.

Isolation: no table, column, policy or SECURITY DEFINER function is added; forced row-level
security and `tenant_isolation` on `delivery_targets` are unchanged, so an UPDATE in one
client's context reaches 0 rows of another client. `host` (`address`), `claim_id`'s claim rules,
`credential_ref`, `platform_kind`, `name`, `environment`, `workspace_id` and `project_id` stay
ungranted; `frontend_origin` and every RID are bindings keys other than `version_id`, so the guard
refuses them. No data is rewritten.

Rollback: drops the trigger and the function and revokes `UPDATE (bindings)` from `shifde_app`;
delivery_0001's grant on `(status, claim_id, revision)` stays. Not refused when values exist: a
`version_id` changed through the grant stays as written.
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0010"
down_revision = "delivery_0009"
branch_labels = None
depends_on = None

GUARD_SIGNATURE = "delivery_targets_bindings_guard()"
TRIGGER = "trg_delivery_targets_bindings_guard"

GUARD = f"""CREATE FUNCTION shifde.{GUARD_SIGNATURE}
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF NEW.bindings IS NOT DISTINCT FROM OLD.bindings THEN
          RETURN NEW;
        END IF;
        -- the table's owner (or a superuser) can drop this trigger; binding it protects nothing
        IF pg_catalog.pg_has_role(
             current_user,
             (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID),
             'MEMBER') THEN
          RETURN NEW;
        END IF;
        IF (OLD.platform_kind = 'palantir' AND NEW.platform_kind = 'palantir'
            AND pg_catalog.jsonb_typeof(OLD.bindings) = 'object'
            AND pg_catalog.jsonb_typeof(NEW.bindings) = 'object'
            AND (OLD.bindings - 'version_id') = (NEW.bindings - 'version_id')
            AND pg_catalog.jsonb_typeof(NEW.bindings -> 'version_id') = 'string'
            AND (NEW.bindings ->> 'version_id')
                ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$')
           IS NOT TRUE THEN
          RAISE EXCEPTION 'only the version_id binding of a Palantir target can change'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{GUARD_SIGNATURE} FROM PUBLIC",
        f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE OF bindings ON shifde.delivery_targets "
        f"FOR EACH ROW EXECUTE FUNCTION shifde.{GUARD_SIGNATURE}",
        "GRANT UPDATE (bindings) ON shifde.delivery_targets TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "REVOKE UPDATE (bindings) ON shifde.delivery_targets FROM shifde_app",
        f"DROP TRIGGER {TRIGGER} ON shifde.delivery_targets",
        f"DROP FUNCTION shifde.{GUARD_SIGNATURE}",
    )
