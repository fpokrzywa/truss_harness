"""Identity, part 2b: the API-key resolution definer.

Revision ID: identity_0003
Revises: identity_0002

`shifde_resolve_api_key(text)` (SETH 3ba0e06 `p02_0001`, DECISIONS B-915): bearer authentication
looks a key up by its public prefix before any client context exists (`api_keys` is tenant-keyed
under row-level security). Changes from SETH (identity review 1 item 4):

* the client comes from the key row only (the caller passes nothing but the prefix);
* a row is returned only when the key's principal belongs to that client: a service account of
  the key's client, or a person with a membership in the key's client. A key whose principal was
  never (or is no longer) part of the client resolves to nothing, so its name and e-mail are not
  read from another client's rows;
* lockdown as the other identity definers (B-909): owned by `shifde_owner`,
  `SET search_path = pg_catalog, pg_temp`, every `shifde` object schema-qualified, EXECUTE revoked
  from PUBLIC and granted to `shifde_app` only, no write of the `app.*` context.

The secret is verified by the caller (HMAC with the pepper named by `pepper_id`, B-916).

Rollback: drops the function.
"""

from __future__ import annotations

from alembic import op

revision = "identity_0003"
down_revision = "identity_0002"
branch_labels = None
depends_on = None

SIGNATURE = "shifde_resolve_api_key(text)"

RESOLVE_API_KEY = """CREATE FUNCTION shifde.shifde_resolve_api_key(p_prefix text)
   RETURNS TABLE (key_id uuid, tenant_id uuid, principal_type text, principal_id uuid,
                  key_hash bytea, pepper_id text, scopes text[], resource_scope jsonb,
                  expires_at timestamptz, revoked_at timestamptz,
                  principal_active boolean, principal_name text, principal_email text)
   LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
   $$ SELECT k.id, k.tenant_id, k.principal_type, k.principal_id, k.key_hash, k.pepper_id,
             k.scopes, k.resource_scope, k.expires_at, k.revoked_at,
             CASE k.principal_type
               WHEN 'service_account' THEN COALESCE(sa.status = 'active', false)
               ELSE COALESCE(u.status = 'active' AND m.status = 'active', false)
             END,
             COALESCE(sa.name, u.display_name, ''), COALESCE(u.primary_email, '')
        FROM shifde.api_keys k
        LEFT JOIN shifde.service_accounts sa
          ON k.principal_type = 'service_account' AND sa.id = k.principal_id
             AND sa.tenant_id = k.tenant_id
        LEFT JOIN shifde.memberships m
          ON k.principal_type = 'user' AND m.tenant_id = k.tenant_id
             AND m.user_id = k.principal_id
        LEFT JOIN shifde.users u ON u.id = m.user_id
       WHERE k.prefix = p_prefix
         AND (sa.id IS NOT NULL OR m.id IS NOT NULL) $$"""


def upgrade() -> None:
    for statement in (
        RESOLVE_API_KEY,
        f"REVOKE ALL ON FUNCTION shifde.{SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION shifde.{SIGNATURE} TO shifde_app",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS shifde.{SIGNATURE}")
