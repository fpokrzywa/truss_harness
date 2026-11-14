"""Identity, part 2a: the identity SECURITY DEFINER functions, the membership insert guard and
per-source login lockout.

Revision ID: identity_0002
Revises: identity_0001

* Seven SECURITY DEFINER functions (DECISIONS B-909), each owned by `shifde_owner`, with
  `SET search_path = pg_catalog, pg_temp`, every `shifde` object and builtin schema-qualified,
  EXECUTE revoked from PUBLIC and granted to `shifde_app` only, and no write of the `app.*`
  context (B-117). Six are SETH 3ba0e06's (`p01_0001`..`p02_0004`, final versions) with these
  changes:
  - `shifde_resolve_session(bytea)`: reports platform administrator only on a session not pinned
    to a client (SETH p02_0004) and returns no row for a session pinned to a client in which the
    user no longer has an active membership (identity review 1 item 4, B-116 review 5 item 3);
  - `shifde_create_invite_token(uuid, bytea, timestamptz)`: no longer trusts a membership row
    (identity review 1 item 1): only the platform administrator (flag and principal set) may
    issue an invitation, for an active non-administrator account that has no local credential
    and no identity, expiring within 73 hours;
  - `shifde_revoke_session(bytea)`, `shifde_revoke_user_sessions(uuid)`,
    `shifde_redeem_setup_token(bytea, text, int)`, `shifde_resolve_local_login(text)`: SETH's
    bodies, schema-qualified.
  The seventh, `shifde_my_membership_tenants()`, is new: the caller's own active membership
  tenant ids (identity tables only) for the login audit fan-out. SETH's
  `shifde_my_memberships()` reads `tenants` and moves to tenancy (carry_forward).
* `memberships` gains a RESTRICTIVE INSERT policy: a membership is inserted only under the
  platform-administrator context (identity review 1 item 1). A client context can no longer
  add another client's member, or any existing person, to itself.
* `login_lockouts`: failed local logins are counted per (person, source address) so that knowing
  an e-mail address is not enough to lock its owner out (SEC Minor-1, B-911). Per-person
  identity state, keyed to the person (`own_row`, a `PRINCIPAL_TABLES` entry).

Rollback: drops the functions, the policy and `login_lockouts` (its counters are lost).
"""

from __future__ import annotations

from alembic import op

revision = "identity_0002"
down_revision = "identity_0001"
branch_labels = None
depends_on = None

PATH = "SET search_path = pg_catalog, pg_temp"

RESOLVE_LOCAL_LOGIN = f"""CREATE FUNCTION shifde.shifde_resolve_local_login(p_email text)
   RETURNS TABLE (user_id uuid, password_hash text, failed_attempts int,
                  locked_until timestamptz, user_status text)
   LANGUAGE sql STABLE SECURITY DEFINER {PATH} AS
   $$ SELECT u.id, c.password_hash, c.failed_attempts, c.locked_until, u.status
        FROM shifde.users u JOIN shifde.local_credentials c ON c.user_id = u.id
       WHERE u.primary_email = pg_catalog.lower(pg_catalog.btrim(p_email)) $$"""

# A session pinned to a client is valid only while the person holds an active membership there,
# and it never carries platform-administrator power (review 1 item 4). Bindings, groups and the
# client's own status are added by tenancy (carry_forward).
RESOLVE_SESSION = f"""CREATE FUNCTION shifde.shifde_resolve_session(p_session_hash bytea)
   RETURNS TABLE (session_id uuid, user_id uuid, csrf_hash bytea, created_at timestamptz,
                  last_seen_at timestamptz, idle_expires_at timestamptz,
                  absolute_expires_at timestamptz, revoked_at timestamptz,
                  email text, display_name text, is_platform_admin boolean,
                  user_status text, active_tenant_id uuid)
   LANGUAGE sql STABLE SECURITY DEFINER {PATH} AS
   $$ SELECT s.id, s.user_id, s.csrf_hash, s.created_at, s.last_seen_at, s.idle_expires_at,
             s.absolute_expires_at, s.revoked_at, u.primary_email, u.display_name,
             (u.is_platform_admin AND s.active_tenant_id IS NULL), u.status, s.active_tenant_id
        FROM shifde.sessions s JOIN shifde.users u ON u.id = s.user_id
       WHERE s.session_hash = p_session_hash
         AND (s.active_tenant_id IS NULL OR EXISTS (
              SELECT 1 FROM shifde.memberships m
               WHERE m.tenant_id = s.active_tenant_id AND m.user_id = s.user_id
                 AND m.status = 'active')) $$"""

REVOKE_SESSION = f"""CREATE FUNCTION shifde.shifde_revoke_session(p_session_hash bytea)
   RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER {PATH} AS
   $$ UPDATE shifde.sessions SET revoked_at = pg_catalog.now()
       WHERE session_hash = p_session_hash AND revoked_at IS NULL $$"""

REVOKE_USER_SESSIONS = f"""CREATE FUNCTION shifde.shifde_revoke_user_sessions(p_user_id uuid)
   RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS
   $$ DECLARE v_count integer;
      BEGIN
        IF shifde.current_tenant_id() IS NULL OR NOT EXISTS (
             SELECT 1 FROM shifde.memberships m
              WHERE m.user_id = p_user_id AND m.tenant_id = shifde.current_tenant_id()) THEN
          RAISE EXCEPTION 'user is not a member of the current tenant'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        UPDATE shifde.sessions SET revoked_at = pg_catalog.now()
         WHERE user_id = p_user_id AND revoked_at IS NULL
           AND (active_tenant_id IS NULL OR active_tenant_id = shifde.current_tenant_id());
        GET DIAGNOSTICS v_count = ROW_COUNT;
        RETURN v_count;
      END $$"""

# Review 1 item 1: no membership row is trusted. One generic error for every refusal (item 3).
CREATE_INVITE_TOKEN = f"""CREATE FUNCTION shifde.shifde_create_invite_token(
       p_user_id uuid, p_token_hash bytea, p_expires_at timestamptz)
   RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS
   $$ BEGIN
        IF NOT shifde.is_platform_admin_ctx() OR shifde.current_principal_id() IS NULL
           OR p_expires_at IS NULL OR p_expires_at <= pg_catalog.now()
           OR p_expires_at > pg_catalog.now() + pg_catalog.make_interval(hours => 73)
           OR NOT EXISTS (
             SELECT 1 FROM shifde.users u
              WHERE u.id = p_user_id AND u.status = 'active' AND NOT u.is_platform_admin)
           OR EXISTS (SELECT 1 FROM shifde.local_credentials lc WHERE lc.user_id = p_user_id)
           OR EXISTS (SELECT 1 FROM shifde.user_identities ui WHERE ui.user_id = p_user_id) THEN
          RAISE EXCEPTION 'invitation refused' USING ERRCODE = 'insufficient_privilege';
        END IF;
        INSERT INTO shifde.setup_tokens
          (id, user_id, token_hash, purpose, expires_at, created_by, tenant_id)
        VALUES (pg_catalog.gen_random_uuid(), p_user_id, p_token_hash, 'invite', p_expires_at,
                shifde.current_principal_id(), shifde.current_tenant_id());
      END $$"""

REDEEM_SETUP_TOKEN = f"""CREATE FUNCTION shifde.shifde_redeem_setup_token(
       p_token_hash bytea, p_password_hash text, p_link_seconds int)
   RETURNS TABLE (user_id uuid, mode text)
   LANGUAGE plpgsql VOLATILE SECURITY DEFINER {PATH} AS
   $$ DECLARE v_user uuid;
      BEGIN
        UPDATE shifde.setup_tokens t
           SET used_at = pg_catalog.now(),
               link_pending_until = CASE WHEN p_password_hash IS NULL
                 THEN pg_catalog.now() + pg_catalog.make_interval(secs => p_link_seconds) END
          FROM shifde.users u
         WHERE t.token_hash = p_token_hash AND t.used_at IS NULL
           AND t.expires_at > pg_catalog.now()
           AND u.id = t.user_id AND u.status = 'active'
           AND NOT (t.purpose = 'invite'
                    AND (EXISTS (SELECT 1 FROM shifde.local_credentials lc
                                  WHERE lc.user_id = t.user_id)
                         OR EXISTS (SELECT 1 FROM shifde.user_identities ui
                                     WHERE ui.user_id = t.user_id)))
        RETURNING t.user_id INTO v_user;
        IF NOT FOUND THEN
          RETURN;
        END IF;
        IF p_password_hash IS NOT NULL THEN
          INSERT INTO shifde.local_credentials (user_id, password_hash, password_changed_at)
          VALUES (v_user, p_password_hash, pg_catalog.now())
          ON CONFLICT ON CONSTRAINT local_credentials_pkey DO UPDATE
            SET password_hash = EXCLUDED.password_hash, password_changed_at = pg_catalog.now(),
                failed_attempts = 0, locked_until = NULL;
          RETURN QUERY SELECT v_user, 'password'::pg_catalog.text;
        ELSE
          RETURN QUERY SELECT v_user, 'oidc_link'::pg_catalog.text;
        END IF;
      END $$"""

MY_MEMBERSHIP_TENANTS = f"""CREATE FUNCTION shifde.shifde_my_membership_tenants()
   RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER {PATH} AS
   $$ SELECT m.tenant_id FROM shifde.memberships m
       WHERE m.user_id = shifde.current_principal_id() AND m.status = 'active'
       ORDER BY 1 $$"""

FUNCTIONS: dict[str, str] = {
    "shifde_resolve_local_login(text)": RESOLVE_LOCAL_LOGIN,
    "shifde_resolve_session(bytea)": RESOLVE_SESSION,
    "shifde_revoke_session(bytea)": REVOKE_SESSION,
    "shifde_revoke_user_sessions(uuid)": REVOKE_USER_SESSIONS,
    "shifde_create_invite_token(uuid, bytea, timestamptz)": CREATE_INVITE_TOKEN,
    "shifde_redeem_setup_token(bytea, text, int)": REDEEM_SETUP_TOKEN,
    "shifde_my_membership_tenants()": MY_MEMBERSHIP_TENANTS,
}

OWN_ROW = "user_id = shifde.current_principal_id()"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    for signature, ddl in FUNCTIONS.items():
        _run(
            ddl,
            f"REVOKE ALL ON FUNCTION shifde.{signature} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION shifde.{signature} TO shifde_app",
        )
    _run(
        # review 1 item 1: a client context cannot add anyone to itself; the platform
        # administrator adds members (a client owner path arrives with tenancy, carry_forward)
        "CREATE POLICY insert_by_platform_admin ON shifde.memberships AS RESTRICTIVE FOR INSERT "
        "WITH CHECK (shifde.is_platform_admin_ctx())",
        """CREATE TABLE shifde.login_lockouts (
            user_id uuid NOT NULL REFERENCES shifde.users (id),
            source_hash bytea NOT NULL,
            failed_attempts int NOT NULL DEFAULT 0,
            locked_until timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_login_lockouts PRIMARY KEY (user_id, source_hash)
        )""",
        "ALTER TABLE shifde.login_lockouts ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.login_lockouts FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY own_row ON shifde.login_lockouts USING ({OWN_ROW}) WITH CHECK ({OWN_ROW})",
        "GRANT SELECT, INSERT ON shifde.login_lockouts TO shifde_app",
        "GRANT UPDATE (failed_attempts, locked_until, updated_at) ON shifde.login_lockouts "
        "TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP TABLE IF EXISTS shifde.login_lockouts",
        "DROP POLICY IF EXISTS insert_by_platform_admin ON shifde.memberships",
    )
    for signature in reversed(FUNCTIONS):
        _run(f"DROP FUNCTION IF EXISTS shifde.{signature}")
