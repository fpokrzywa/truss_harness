"""Secrets: an installation IdP's client secret through a narrow definer (identity review 4 item 5).

Revision ID: secrets_0003
Revises: secrets_0002
Depends on: identity_0001 (`idp_configs`)

DECISIONS B-2260. The OpenID Connect callback is unauthenticated; for an installation IdP it
used to raise the platform-administrator context to read the IdP's client secret
(`identity.oidc._client_secret`), which made every installation row and every client's row
visible to that block. New definer `shifde_installation_idp_secret(uuid)`:

* refuses (42501) whenever a client context is set, before any lookup;
* answers only for an active installation IdP (`tenant_id` NULL) whose `client_secret_ref_id`
  names an active installation secret (`tenant_id` NULL); a client's IdP, a disabled IdP, an IdP
  without a secret and an unknown id all answer no row;
* returns the envelope only (decryption stays in Python under the key ring) and records the use
  as SETH's `resolve_system` did: one `secret_access_log` row (no principal, purpose
  `oidc.token_exchange`, outcome `success`) and the secret's `last_used_at`.

It writes no `app.*` setting. EXECUTE for `shifde_app` only.

Rollback: drops the definer.
"""

from __future__ import annotations

from alembic import op

revision = "secrets_0003"
down_revision = "secrets_0002"
branch_labels = None
depends_on = ("identity_0001",)

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
SIGNATURE = "shifde.shifde_installation_idp_secret(uuid)"

INSTALLATION_IDP_SECRET = f"""CREATE FUNCTION shifde.shifde_installation_idp_secret(p_idp uuid)
   RETURNS TABLE (secret_id uuid, secret_name text, provider_key text, external_ref text,
                  ciphertext bytea, wrapped_dek bytea, nonce bytea, kek_id text,
                  secret_version integer)
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$
   DECLARE
     v_id uuid;
   BEGIN
     IF shifde.current_tenant_id() IS NOT NULL THEN
       RAISE EXCEPTION 'not available in a client context' USING ERRCODE = '42501';
     END IF;
     SELECT s.id INTO v_id
       FROM shifde.idp_configs c
       JOIN shifde.secrets s ON s.id = c.client_secret_ref_id
      WHERE c.id = p_idp AND c.tenant_id IS NULL AND c.status = 'active'
        AND s.tenant_id IS NULL AND s.status = 'active';
     IF v_id IS NULL THEN
       RETURN;
     END IF;
     INSERT INTO shifde.secret_access_log (id, tenant_id, secret_id, at, principal_id, purpose,
                                           run_id, outcome)
     VALUES (pg_catalog.gen_random_uuid(), NULL, v_id, pg_catalog.now(), NULL,
             'oidc.token_exchange', NULL, 'success');
     UPDATE shifde.secrets s SET last_used_at = pg_catalog.now()
      WHERE s.id = v_id AND s.tenant_id IS NULL;
     RETURN QUERY
       SELECT s.id, s.name, s.provider_key, s.external_ref, s.ciphertext, s.wrapped_dek,
              s.nonce, s.kek_id, s.secret_version
         FROM shifde.secrets s
        WHERE s.id = v_id AND s.tenant_id IS NULL;
   END
   $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        INSTALLATION_IDP_SECRET,
        f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO shifde_app",
    )


def downgrade() -> None:
    _run(f"DROP FUNCTION {SIGNATURE}")
