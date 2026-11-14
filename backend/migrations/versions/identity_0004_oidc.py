"""Identity, part 3a: OpenID Connect sign-in (flow state, IdP and identity resolution).

Revision ID: identity_0004
Revises: identity_0003

* `oidc_auth_requests` (SETH 3ba0e06 `p02_0002`, final schema): hashed single-use state, nonce
  and browser binding with a short expiry. SETH's `flow_scope` policy and grants are kept, but the
  table gains a RESTRICTIVE `definer_only` policy (USING false, WITH CHECK false): the application
  role never reads or writes flow state itself. It is created and consumed only by two new
  definers, so no client context is set before an IdP has vouched for the person (identity
  review 3 item 5, DECISIONS B-922): `shifde_begin_oidc_request(...)` takes the client from the
  active IdP row, `shifde_consume_oidc_request(bytea, uuid)` marks a live request used (single
  use, expiry in SQL) only while its IdP is active and still belongs to the same client.
* `shifde_resolve_idp(uuid)` (SETH `p02_0002`): an active IdP by id, no secret values. Review 1
  item 4: the row is returned only if the client secret it names belongs to the IdP's own scope
  (a client's secret for that client's IdP, an installation secret for an installation IdP) and a
  client's IdP never provisions people (`jit = 'off'`); the caller pins the session to the IdP's
  client and requires an active membership there (`identity.oidc`).
* `shifde_resolve_identity(text, text, text, uuid)` (SETH `p02_0003`, final): identity by
  `(idp, issuer, subject)` of an active IdP; the "link the next OIDC login" path links only
  through an installation IdP or an IdP of the token's own client, and for a client's IdP only
  an active member of that client who is not the platform administrator.
* `shifde_jit_create_user(uuid, uuid, text, text, text, text)` (new): just-in-time provisioning
  through an active installation IdP with `jit` on creates the person and the identity together.
* Review 1 item 2 (MUST): `user_identities` gains a RESTRICTIVE `insert_by_definer_only` INSERT
  policy (WITH CHECK false): identities are inserted only inside the two definers above, so
  nobody can bind (squat) another person's subject at any IdP.
* Review 1 item 7: trigger `trg_idp_configs_secret_scope`: an installation IdP (tenant NULL) may
  name only an installation secret (the composite key already ties a client's IdP to its own
  client's secrets).
* Every definer: owner `shifde_owner`, `SET search_path = pg_catalog, pg_temp`, `shifde` objects
  schema-qualified, EXECUTE revoked from PUBLIC and granted to `shifde_app` only, no `app.*`
  write (B-909).

Rollback: drops the definers, the trigger, the two RESTRICTIVE policies and `oidc_auth_requests`
(pending sign-in flows are lost; identities are kept).
"""

from __future__ import annotations

from alembic import op

revision = "identity_0004"
down_revision = "identity_0003"
branch_labels = None
depends_on = None

_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"

FUNCTIONS: dict[str, str] = {
    "shifde_resolve_idp(uuid)": f"""CREATE FUNCTION shifde.shifde_resolve_idp(p_id uuid)
   RETURNS TABLE (id uuid, tenant_id uuid, display_name text, issuer text,
                  discovery_url text, client_id text, client_secret_ref_id uuid,
                  scopes text[], claim_mappings jsonb, jit text, group_sync_mode text)
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT c.id, c.tenant_id, c.display_name, c.issuer, c.discovery_url, c.client_id,
             c.client_secret_ref_id, c.scopes, c.claim_mappings, c.jit, c.group_sync_mode
        FROM shifde.idp_configs c
        LEFT JOIN shifde.secrets s ON s.id = c.client_secret_ref_id
       WHERE c.id = p_id AND c.status = 'active'
         AND (c.tenant_id IS NULL OR c.jit = 'off')
         AND (c.client_secret_ref_id IS NULL
              OR (s.id IS NOT NULL AND NOT (s.tenant_id IS DISTINCT FROM c.tenant_id))) $$""",
    "shifde_resolve_identity(text, text, text, uuid)": f"""CREATE FUNCTION
       shifde.shifde_resolve_identity(
       p_issuer text, p_subject text, p_email text, p_idp_config_id uuid)
   RETURNS TABLE (user_id uuid, user_status text, linked boolean)
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ DECLARE v_user uuid; v_status text; v_token uuid; v_tenant uuid;
      BEGIN
        SELECT c.tenant_id INTO v_tenant FROM shifde.idp_configs c
         WHERE c.id = p_idp_config_id AND c.status = 'active' AND c.issuer = p_issuer;
        IF NOT FOUND THEN
          RETURN;
        END IF;
        SELECT i.user_id, u.status INTO v_user, v_status
          FROM shifde.user_identities i JOIN shifde.users u ON u.id = i.user_id
         WHERE i.idp_config_id = p_idp_config_id AND i.issuer = p_issuer
           AND i.subject = p_subject;
        IF FOUND THEN
          RETURN QUERY SELECT v_user, v_status, false;
          RETURN;
        END IF;
        IF p_email IS NULL THEN
          RETURN;
        END IF;
        SELECT t.id, u.id, u.status INTO v_token, v_user, v_status
          FROM shifde.setup_tokens t JOIN shifde.users u ON u.id = t.user_id
         WHERE u.primary_email = lower(btrim(p_email))
           AND t.link_pending_until IS NOT NULL AND t.link_pending_until > now()
           AND (v_tenant IS NULL OR t.tenant_id = v_tenant)
         ORDER BY t.link_pending_until DESC LIMIT 1 FOR UPDATE OF t;
        IF NOT FOUND THEN
          RETURN;
        END IF;
        -- a client's IdP links only an active member of that client, never the administrator
        IF v_tenant IS NOT NULL AND NOT EXISTS (
             SELECT 1 FROM shifde.memberships m JOIN shifde.users u ON u.id = m.user_id
              WHERE m.tenant_id = v_tenant AND m.user_id = v_user AND m.status = 'active'
                AND NOT u.is_platform_admin) THEN
          RETURN;
        END IF;
        UPDATE shifde.setup_tokens SET link_pending_until = NULL WHERE id = v_token;
        INSERT INTO shifde.user_identities
          (id, user_id, issuer, subject, idp_config_id, email_at_login)
        VALUES (gen_random_uuid(), v_user, p_issuer, p_subject, p_idp_config_id,
                lower(btrim(p_email)));
        RETURN QUERY SELECT v_user, v_status, true;
      END $$""",
    "shifde_jit_create_user(uuid, uuid, text, text, text, text)": f"""CREATE FUNCTION
       shifde.shifde_jit_create_user(
       p_user uuid, p_idp_config_id uuid, p_issuer text, p_subject text, p_email text,
       p_display_name text)
   RETURNS boolean
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ BEGIN
        PERFORM 1 FROM shifde.idp_configs c
         WHERE c.id = p_idp_config_id AND c.tenant_id IS NULL AND c.status = 'active'
           AND c.jit <> 'off' AND c.issuer = p_issuer;
        IF NOT FOUND THEN
          RETURN false;
        END IF;
        PERFORM 1 FROM shifde.user_identities i
         WHERE i.idp_config_id = p_idp_config_id AND i.subject = p_subject;
        IF FOUND THEN
          RETURN false;
        END IF;
        INSERT INTO shifde.users (id, primary_email, display_name)
        VALUES (p_user, lower(btrim(p_email)), p_display_name) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN
          RETURN false;
        END IF;
        INSERT INTO shifde.user_identities
          (id, user_id, issuer, subject, idp_config_id, email_at_login)
        VALUES (gen_random_uuid(), p_user, p_issuer, p_subject, p_idp_config_id,
                lower(btrim(p_email)));
        RETURN true;
      END $$""",
    "shifde_begin_oidc_request(uuid, uuid, bytea, bytea, bytea, text, integer)": f"""CREATE
       FUNCTION shifde.shifde_begin_oidc_request(
       p_id uuid, p_idp_config_id uuid, p_state_hash bytea, p_binding_hash bytea,
       p_nonce_hash bytea, p_next_path text, p_ttl_seconds integer)
   RETURNS boolean
   LANGUAGE plpgsql VOLATILE {_LOCKED} AS
   $$ BEGIN
        IF p_ttl_seconds IS NULL OR p_ttl_seconds < 1 OR p_ttl_seconds > 3600
           OR octet_length(p_state_hash) <> 32 OR octet_length(p_binding_hash) <> 32
           OR octet_length(p_nonce_hash) <> 32 THEN
          RETURN false;
        END IF;
        INSERT INTO shifde.oidc_auth_requests
          (id, tenant_id, idp_config_id, state_hash, binding_hash, nonce_hash, next_path,
           expires_at)
        SELECT p_id, c.tenant_id, c.id, p_state_hash, p_binding_hash, p_nonce_hash,
               p_next_path, now() + make_interval(secs => p_ttl_seconds)
          FROM shifde.idp_configs c
         WHERE c.id = p_idp_config_id AND c.status = 'active';
        RETURN FOUND;
      END $$""",
    "shifde_consume_oidc_request(bytea, uuid)": f"""CREATE FUNCTION
       shifde.shifde_consume_oidc_request(p_state_hash bytea, p_idp_config_id uuid)
   RETURNS TABLE (id uuid, tenant_id uuid, binding_hash bytea, nonce_hash bytea,
                  next_path text)
   LANGUAGE sql VOLATILE {_LOCKED} AS
   $$ UPDATE shifde.oidc_auth_requests r SET used_at = now()
        FROM shifde.idp_configs c
       WHERE r.state_hash = p_state_hash AND r.idp_config_id = p_idp_config_id
         AND r.used_at IS NULL AND r.expires_at > now()
         AND c.id = r.idp_config_id AND c.status = 'active'
         AND NOT (c.tenant_id IS DISTINCT FROM r.tenant_id)
      RETURNING r.id, r.tenant_id, r.binding_hash, r.nonce_hash, r.next_path $$""",
}

SECRET_SCOPE = """CREATE FUNCTION shifde.check_idp_secret_scope() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NEW.tenant_id IS NULL AND NEW.client_secret_ref_id IS NOT NULL AND NOT EXISTS (
             SELECT 1 FROM shifde.secrets s
              WHERE s.id = NEW.client_secret_ref_id AND s.tenant_id IS NULL) THEN
          RAISE EXCEPTION 'identity provider configuration refused'
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
        """CREATE TABLE shifde.oidc_auth_requests (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            idp_config_id uuid NOT NULL REFERENCES shifde.idp_configs (id),
            state_hash bytea NOT NULL,
            binding_hash bytea NOT NULL,
            nonce_hash bytea NOT NULL,
            next_path text NOT NULL DEFAULT '/',
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            CONSTRAINT uq_oidc_auth_requests__state UNIQUE (state_hash)
        )""",
        "ALTER TABLE shifde.oidc_auth_requests ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.oidc_auth_requests FORCE ROW LEVEL SECURITY",
        "CREATE POLICY flow_scope ON shifde.oidc_auth_requests "
        "USING (tenant_id IS NOT DISTINCT FROM shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id IS NOT DISTINCT FROM shifde.current_tenant_id())",
        # B-922: flow state only through the two definers (no client context before sign-in)
        "CREATE POLICY definer_only ON shifde.oidc_auth_requests AS RESTRICTIVE "
        "USING (false) WITH CHECK (false)",
        "GRANT SELECT, INSERT ON shifde.oidc_auth_requests TO shifde_app",
        "GRANT UPDATE (used_at) ON shifde.oidc_auth_requests TO shifde_app",
        # review 1 item 2: identities only inside the resolve / JIT definers
        "CREATE POLICY insert_by_definer_only ON shifde.user_identities AS RESTRICTIVE "
        "FOR INSERT WITH CHECK (false)",
        SECRET_SCOPE,
        "CREATE TRIGGER trg_idp_configs_secret_scope BEFORE INSERT OR UPDATE OF tenant_id, "
        "client_secret_ref_id ON shifde.idp_configs FOR EACH ROW "
        "EXECUTE FUNCTION shifde.check_idp_secret_scope()",
    )
    for signature, ddl in FUNCTIONS.items():
        _run(
            ddl,
            f"REVOKE ALL ON FUNCTION shifde.{signature} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION shifde.{signature} TO shifde_app",
        )


def downgrade() -> None:
    for signature in reversed(FUNCTIONS):
        _run(f"DROP FUNCTION IF EXISTS shifde.{signature}")
    _run(
        "DROP TRIGGER IF EXISTS trg_idp_configs_secret_scope ON shifde.idp_configs",
        "DROP FUNCTION IF EXISTS shifde.check_idp_secret_scope()",
        "DROP POLICY IF EXISTS insert_by_definer_only ON shifde.user_identities",
        "DROP TABLE IF EXISTS shifde.oidc_auth_requests",
    )
