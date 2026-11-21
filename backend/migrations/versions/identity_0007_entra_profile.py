"""Identity: the Entra profile of installation identity providers (SP3-6, issue #83).

Revision ID: identity_0007
Revises: identity_0006

* `idp_configs` gains `profile` (`generic` or `entra`, default `generic`) and `entra_tenant_id`
  (the Entra directory the IdP is pinned to). `ck_idp_configs__entra_installation`: an `entra`
  row names its tenant and is an installation IdP (`tenant_id` NULL); client IdPs stay generic.
  The application role may change both columns (`UPDATE (profile, entra_tenant_id)`).
* `shifde_resolve_idp(uuid)` (definer, same signature, same filter and lockdown as
  identity_0004) also returns `profile` and `entra_tenant_id`, so the sign-in callback checks
  `tid`, `ver` and `oid` against the row it already trusts (design SP3 section 8).

Rollback: restores the identity_0004 function, revokes the grant and drops both constraints and
both columns (every IdP is generic again).
"""

from __future__ import annotations

from alembic import op

revision = "identity_0007"
down_revision = "identity_0006"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
_SIG = "shifde.shifde_resolve_idp(uuid)"
_WHERE = """FROM shifde.idp_configs c
        LEFT JOIN shifde.secrets s ON s.id = c.client_secret_ref_id
       WHERE c.id = p_id AND c.status = 'active'
         AND (c.tenant_id IS NULL OR c.jit = 'off')
         AND (c.client_secret_ref_id IS NULL
              OR (s.id IS NOT NULL AND NOT (s.tenant_id IS DISTINCT FROM c.tenant_id)))"""

NEW = f"""CREATE FUNCTION shifde.shifde_resolve_idp(p_id uuid)
   RETURNS TABLE (id uuid, tenant_id uuid, display_name text, issuer text,
                  discovery_url text, client_id text, client_secret_ref_id uuid,
                  scopes text[], claim_mappings jsonb, jit text, group_sync_mode text,
                  profile text, entra_tenant_id uuid)
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT c.id, c.tenant_id, c.display_name, c.issuer, c.discovery_url, c.client_id,
             c.client_secret_ref_id, c.scopes, c.claim_mappings, c.jit, c.group_sync_mode,
             c.profile, c.entra_tenant_id
        {_WHERE} $$"""

OLD = f"""CREATE FUNCTION shifde.shifde_resolve_idp(p_id uuid)
   RETURNS TABLE (id uuid, tenant_id uuid, display_name text, issuer text,
                  discovery_url text, client_id text, client_secret_ref_id uuid,
                  scopes text[], claim_mappings jsonb, jit text, group_sync_mode text)
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT c.id, c.tenant_id, c.display_name, c.issuer, c.discovery_url, c.client_id,
             c.client_secret_ref_id, c.scopes, c.claim_mappings, c.jit, c.group_sync_mode
        {_WHERE} $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _replace(body: str) -> None:
    _run(
        f"DROP FUNCTION {_SIG}",
        body,
        f"REVOKE ALL ON FUNCTION {_SIG} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {_SIG} TO shifde_app",
    )


def upgrade() -> None:
    _run(
        "ALTER TABLE shifde.idp_configs ADD COLUMN profile text NOT NULL DEFAULT 'generic' "
        "CONSTRAINT ck_idp_configs__profile CHECK (profile IN ('generic', 'entra'))",
        "ALTER TABLE shifde.idp_configs ADD COLUMN entra_tenant_id uuid",
        "ALTER TABLE shifde.idp_configs ADD CONSTRAINT ck_idp_configs__entra_installation "
        "CHECK (profile <> 'entra' OR (entra_tenant_id IS NOT NULL AND tenant_id IS NULL))",
        "GRANT UPDATE (profile, entra_tenant_id) ON shifde.idp_configs TO shifde_app",
    )
    _replace(NEW)


def downgrade() -> None:
    _replace(OLD)
    _run(
        "REVOKE UPDATE (profile, entra_tenant_id) ON shifde.idp_configs FROM shifde_app",
        "ALTER TABLE shifde.idp_configs DROP CONSTRAINT ck_idp_configs__entra_installation",
        "ALTER TABLE shifde.idp_configs DROP CONSTRAINT ck_idp_configs__profile",
        "ALTER TABLE shifde.idp_configs DROP COLUMN entra_tenant_id",
        "ALTER TABLE shifde.idp_configs DROP COLUMN profile",
    )
