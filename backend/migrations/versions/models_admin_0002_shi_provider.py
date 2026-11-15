"""The SHI-wide model provider, part 2: its reserved id, client-safe shared columns, the switch's
author, and pinned search paths for the guard functions.

Revision ID: models_admin_0002
Revises: models_admin_0001
Depends on: connections_0001 (`connections_guard`)

models_admin isolation review 1 (DECISIONS B-1711..B-1714):

* Item 1: `ck_model_providers__platform` reserves `SHI_PROVIDER_ID` for the NULL-tenant row
  (`(tenant_id IS NULL) = (id = SHI_PROVIDER_ID)`): before, a client row could take the id while
  no SHI row existed, and `ensure_shi_provider`'s `ON CONFLICT DO NOTHING` then hid it.
* Item 4: `ck_model_providers__shared_client_safe`: every client reads the shared row, so its
  `settings` stay `{}` and its `health_detail` NULL. SHI's endpoint, deployment and key reference
  are installation configuration, never a client-readable column (B-1712).
* Item 5: `shi_provider_access.changed_by` is NOT NULL and the RESTRICTIVE insert / update
  policies require it to be `current_principal_id()`, so the author of a switch change is the
  database's acting principal, not a caller-supplied value.
* Item 8: `model_providers_guard`, `models_guard` and `connections_guard` (invoker rights, tables
  already schema-qualified) pin `search_path = pg_catalog, shifde, pg_temp`.

Rollback: restores the part-1 check, drops the client-safe check, makes `changed_by` nullable
again with part 1's policies and resets the three functions' search path. Not data-destructive.
"""

from __future__ import annotations

from alembic import op

revision = "models_admin_0002"
down_revision = "models_admin_0001"
branch_labels = None
depends_on = ("connections_0001",)

# shifde.models_admin.repository.SHI_PROVIDER_ID
SHI_PROVIDER_ID = "'00000000-0000-4000-8000-00000000051a'::uuid"
ADMIN = "shifde.is_platform_admin_ctx()"
AUTHOR = "changed_by = shifde.current_principal_id()"
GUARDS = ("model_providers_guard", "models_guard", "connections_guard")


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        "ALTER TABLE shifde.model_providers DROP CONSTRAINT ck_model_providers__platform",
        "ALTER TABLE shifde.model_providers ADD CONSTRAINT ck_model_providers__platform CHECK ("
        "(tenant_id IS NULL) = (connection_id IS NULL) "
        f"AND (tenant_id IS NULL) = (id = {SHI_PROVIDER_ID}))",
        "ALTER TABLE shifde.model_providers ADD CONSTRAINT ck_model_providers__shared_client_safe "
        "CHECK (tenant_id IS NOT NULL OR (settings = '{}'::jsonb AND health_detail IS NULL))",
        # no route wrote a switch before this revision; a row without an author fails here
        "ALTER TABLE shifde.shi_provider_access ALTER COLUMN changed_by SET NOT NULL",
        "DROP POLICY insert_by_platform_admin ON shifde.shi_provider_access",
        "DROP POLICY update_by_platform_admin ON shifde.shi_provider_access",
        "CREATE POLICY insert_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK ({ADMIN} AND {AUTHOR})",
        "CREATE POLICY update_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR UPDATE USING ({ADMIN}) WITH CHECK ({ADMIN} AND {AUTHOR})",
        *(
            f"ALTER FUNCTION shifde.{name}() SET search_path = pg_catalog, shifde, pg_temp"
            for name in GUARDS
        ),
    )


def downgrade() -> None:
    _run(
        *(f"ALTER FUNCTION shifde.{name}() RESET search_path" for name in GUARDS),
        "DROP POLICY update_by_platform_admin ON shifde.shi_provider_access",
        "DROP POLICY insert_by_platform_admin ON shifde.shi_provider_access",
        "CREATE POLICY insert_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK ({ADMIN})",
        "CREATE POLICY update_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR UPDATE USING ({ADMIN}) WITH CHECK ({ADMIN})",
        "ALTER TABLE shifde.shi_provider_access ALTER COLUMN changed_by DROP NOT NULL",
        "ALTER TABLE shifde.model_providers DROP CONSTRAINT ck_model_providers__shared_client_safe",
        "ALTER TABLE shifde.model_providers DROP CONSTRAINT ck_model_providers__platform",
        "ALTER TABLE shifde.model_providers ADD CONSTRAINT ck_model_providers__platform CHECK ("
        "(tenant_id IS NULL) = (connection_id IS NULL) "
        f"AND (tenant_id IS NOT NULL OR id = {SHI_PROVIDER_ID}))",
    )
