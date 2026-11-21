"""Tenancy: the app role may change only `capture_content` in a client's settings (issue #39).

Revision ID: tenancy_0007
Revises: tenancy_0006

* Issue #39 (isolation review of PR #37, findings 1-2): the column grant `UPDATE (settings)` of
  tenancy_0006 cannot be limited to one key. The new trigger `trg_tenants_settings_guard`
  (`BEFORE UPDATE OF settings`) calls `shifde.tenants_settings_guard()`, which refuses with 42501
  (`insufficient_privilege`) any update that adds, removes or changes a settings key other than
  `capture_content`, or makes `settings` a non-object, unless the platform-administrator context
  is on. Superusers and BYPASSRLS roles (the schema owner) are exempt: they already bypass every
  policy. The guard runs with the caller's rights and a fixed search_path.

Deliberate difference from SETH (`tests/schema/deliberate_changes.yaml`): the `tenants` trigger
`trg_tenants_settings_guard`.

Rollback: drops the trigger and its function.
"""

from __future__ import annotations

from alembic import op

revision = "tenancy_0007"
down_revision = "tenancy_0006"
branch_labels = None
depends_on = None

GUARD = """CREATE FUNCTION shifde.tenants_settings_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF shifde.is_platform_admin_ctx() THEN
    RETURN NEW;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
              WHERE r.rolname = CURRENT_USER AND (r.rolsuper OR r.rolbypassrls)) THEN
    RETURN NEW;
  END IF;
  IF pg_catalog.jsonb_typeof(OLD.settings) IS DISTINCT FROM 'object'
     OR pg_catalog.jsonb_typeof(NEW.settings) IS DISTINCT FROM 'object'
     OR (OLD.settings - 'capture_content') IS DISTINCT FROM (NEW.settings - 'capture_content') THEN
    RAISE EXCEPTION 'only capture_content may be changed in a client''s settings'
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
        "REVOKE ALL ON FUNCTION shifde.tenants_settings_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_tenants_settings_guard BEFORE UPDATE OF settings ON shifde.tenants "
        "FOR EACH ROW EXECUTE FUNCTION shifde.tenants_settings_guard()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER trg_tenants_settings_guard ON shifde.tenants",
        "DROP FUNCTION shifde.tenants_settings_guard()",
    )
