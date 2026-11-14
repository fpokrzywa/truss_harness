"""Identity, part 1 (data layer): users, memberships, credentials, sessions, setup tokens,
service accounts, API keys, user identities and identity-provider configurations.

Revision ID: identity_0001
Revises: base_0001
Depends on: authz_0001 (adds `groups.idp_config_id -> idp_configs`), secrets_0001
(`idp_configs.client_secret_ref_id -> secrets`).

SETH 3ba0e06's final schema for the nine identity tables, folded from p01_0001, p01_0002,
p01_0003, p02_0001, p02_0002, p02_0003, p02_0004 and p03_0002: columns, constraints, indexes,
triggers (and their three trigger functions), policies and `shifde_app` grants.

Not created here (DECISIONS B-901..B-905, docs/build/carry_forward.md):
* Foreign keys to `tenants` (memberships, sessions.active_tenant_id, setup_tokens,
  service_accounts, api_keys, idp_configs): tenancy is not ported; its migration adds them.
* SETH's SECURITY DEFINER functions (`shifde_resolve_local_login`, `shifde_resolve_session`,
  `shifde_my_memberships`, `shifde_revoke_session`, `shifde_revoke_user_sessions`,
  `shifde_create_invite_token`, `shifde_redeem_setup_token`, `shifde_resolve_identity`,
  `shifde_resolve_api_key`, `shifde_resolve_idp`): they take arguments, which
  `tests/schema/test_function_schema.py` refuses for a definer (an allowlist entry names one
  zero-argument function); identity part 2 (sessions, login, API keys) and part 3 (OIDC) settle
  them under isolation review. `shifde_my_memberships` also reads `tenants`.
* `group_members (tenant_id, user_id) -> memberships`: identity part 3 makes groups
  platform-level, so the key is decided there.

Deliberate differences from SETH (tests/schema/deliberate_changes.yaml):
* `users.users_insert` WITH CHECK is `id = current_principal_id() OR is_platform_admin_ctx()`
  (SETH: `... OR current_tenant_id() IS NOT NULL`, which let any client context create a
  platform-wide staff account; B-112, B-901).
* `setup_tokens` keeps SETH's default deny for the app role (no grants) but states it as a
  RESTRICTIVE `no_app_access` policy, so the table has a policy (B-112, B-902).
* `api_keys`: `uq_api_keys__tenant_id_id` and the composite `fk_api_keys__rotated_from
  (tenant_id, rotated_from_id) -> api_keys (tenant_id, id)` replace SETH's plain
  `api_keys_rotated_from_id_fkey` (constraints bypass row-level security; B-116 item 9, B-903).
* `idp_configs`: `fk_idp_configs__client_secret (tenant_id, client_secret_ref_id) ->
  secrets (tenant_id, id)` next to SETH's plain key, which still checks installation IdPs
  (tenant_id NULL) (B-116 item 9, B-903).

Rollback: drops the nine tables and the three trigger functions, and `groups_idp_config_id_fkey`
(data-destructive: every user, session, credential, API key and IdP configuration is lost).
Back up first.
"""

from __future__ import annotations

from alembic import op

revision = "identity_0001"
down_revision = "base_0001"
branch_labels = ("identity",)
depends_on = ("authz_0001", "secrets_0001")

TENANT_KEYED = "tenant_id = shifde.current_tenant_id()"
TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)
OWN_ROW = "user_id = shifde.current_principal_id()"

TABLES = (
    "users",
    "idp_configs",
    "memberships",
    "local_credentials",
    "sessions",
    "setup_tokens",
    "service_accounts",
    "api_keys",
    "user_identities",
)


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    # ------------------------------------------------------------------ tables
    _run(
        """CREATE TABLE shifde.users (
            id uuid PRIMARY KEY,
            primary_email text NOT NULL,
            display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            is_platform_admin boolean NOT NULL DEFAULT false,
            last_login_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_users__primary_email UNIQUE (primary_email),
            CONSTRAINT ck_users__email_normalized CHECK (primary_email = lower(btrim(primary_email)))
        )""",
        """CREATE TABLE shifde.idp_configs (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
            issuer text NOT NULL,
            discovery_url text NOT NULL,
            client_id text NOT NULL,
            client_secret_ref_id uuid REFERENCES shifde.secrets (id),
            scopes text[] NOT NULL DEFAULT '{openid,email,profile}',
            claim_mappings jsonb NOT NULL DEFAULT '{"email": "email", "groups": "groups"}',
            jit text NOT NULL DEFAULT 'off' CHECK (jit IN ('off', 'create_user', 'create_and_join')),
            group_sync_mode text NOT NULL DEFAULT 'off' CHECK (group_sync_mode IN ('off', 'sync')),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT ck_idp_configs__tenant_no_jit CHECK (tenant_id IS NULL OR jit = 'off'),
            CONSTRAINT fk_idp_configs__client_secret FOREIGN KEY (tenant_id, client_secret_ref_id)
                REFERENCES shifde.secrets (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_idp_configs__scope_issuer ON shifde.idp_configs "
        "(COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), issuer)",
        """CREATE TABLE shifde.memberships (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES shifde.users (id),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('invited', 'active', 'suspended')),
            joined_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_memberships__tenant_id_user_id UNIQUE (tenant_id, user_id),
            CONSTRAINT uq_memberships__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        "CREATE INDEX ix_memberships__user_id ON shifde.memberships (user_id)",
        """CREATE TABLE shifde.local_credentials (
            user_id uuid PRIMARY KEY REFERENCES shifde.users (id),
            password_hash text NOT NULL,
            password_changed_at timestamptz NOT NULL DEFAULT now(),
            failed_attempts int NOT NULL DEFAULT 0,
            locked_until timestamptz,
            mfa_secret_ref_id uuid
        )""",
        """CREATE TABLE shifde.sessions (
            id uuid PRIMARY KEY,
            session_hash bytea NOT NULL,
            user_id uuid NOT NULL REFERENCES shifde.users (id),
            active_tenant_id uuid,
            csrf_hash bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            idle_expires_at timestamptz NOT NULL,
            absolute_expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            ip text,
            user_agent_hash bytea,
            CONSTRAINT uq_sessions__session_hash UNIQUE (session_hash)
        )""",
        "CREATE INDEX ix_sessions__user_id ON shifde.sessions (user_id)",
        """CREATE TABLE shifde.setup_tokens (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES shifde.users (id),
            token_hash bytea NOT NULL,
            purpose text NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            link_pending_until timestamptz,
            created_by uuid,
            tenant_id uuid,
            CONSTRAINT uq_setup_tokens__token_hash UNIQUE (token_hash),
            CONSTRAINT setup_tokens_purpose_check CHECK (purpose IN ('bootstrap', 'invite'))
        )""",
        """CREATE TABLE shifde.service_accounts (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            description text,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            owner_principal_id uuid,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_service_accounts__tenant_id_name UNIQUE (tenant_id, name),
            CONSTRAINT uq_service_accounts__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        """CREATE TABLE shifde.api_keys (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            principal_type text NOT NULL CHECK (principal_type IN ('user', 'service_account')),
            principal_id uuid NOT NULL,
            prefix text NOT NULL CHECK (length(prefix) = 8),
            key_hash bytea NOT NULL,
            pepper_id text NOT NULL DEFAULT 'p1',
            scopes text[] NOT NULL,
            resource_scope jsonb,
            expires_at timestamptz NOT NULL,
            last_used_at timestamptz,
            revoked_at timestamptz,
            revoked_by uuid,
            rotated_from_id uuid,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_api_keys__prefix UNIQUE (prefix),
            CONSTRAINT uq_api_keys__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_api_keys__rotated_from FOREIGN KEY (tenant_id, rotated_from_id)
                REFERENCES shifde.api_keys (tenant_id, id)
        )""",
        "CREATE INDEX ix_api_keys__principal ON shifde.api_keys "
        "(tenant_id, principal_type, principal_id)",
        """CREATE TABLE shifde.user_identities (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES shifde.users (id),
            issuer text NOT NULL,
            subject text NOT NULL,
            idp_config_id uuid NOT NULL,
            email_at_login text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_user_identities__idp FOREIGN KEY (idp_config_id)
                REFERENCES shifde.idp_configs (id)
        )""",
        "CREATE INDEX ix_user_identities__user_id ON shifde.user_identities (user_id)",
        "CREATE UNIQUE INDEX uq_user_identities__idp_subject ON shifde.user_identities "
        "(idp_config_id, subject) WHERE idp_config_id IS NOT NULL",
        # authz deferred this key to identity (B-402).
        "ALTER TABLE shifde.groups ADD CONSTRAINT groups_idp_config_id_fkey "
        "FOREIGN KEY (idp_config_id) REFERENCES shifde.idp_configs (id)",
    )

    # ------------------------------------------------------------------ triggers
    _run(
        # an API key's revocation is irreversible and its expiry can only shrink (p02_0001)
        """CREATE FUNCTION shifde.guard_api_key_update() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                  RAISE EXCEPTION 'a revoked API key cannot be changed'
                    USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF NEW.expires_at > OLD.expires_at THEN
                  RAISE EXCEPTION 'API key expiry can only be shortened'
                    USING ERRCODE = 'insufficient_privilege';
                END IF;
                RETURN NEW;
              END $$""",
        # a revoked session stays revoked (p01_0003)
        """CREATE FUNCTION shifde.reject_session_unrevoke() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                RAISE EXCEPTION 'a revoked session cannot be reinstated'
                  USING ERRCODE = 'insufficient_privilege';
              END $$""",
        # promoting a user to platform administrator ends their sessions (p02_0004); runs as the
        # invoker, and the app role cannot update is_platform_admin
        """CREATE FUNCTION shifde.revoke_sessions_on_admin_promotion() RETURNS trigger
           LANGUAGE plpgsql AS
           $$ BEGIN
                UPDATE shifde.sessions SET revoked_at = now()
                 WHERE user_id = NEW.id AND revoked_at IS NULL;
                RETURN NEW;
              END $$""",
        "CREATE TRIGGER trg_api_keys_guard BEFORE UPDATE ON shifde.api_keys "
        "FOR EACH ROW EXECUTE FUNCTION shifde.guard_api_key_update()",
        "CREATE TRIGGER trg_sessions_no_unrevoke BEFORE UPDATE ON shifde.sessions "
        "FOR EACH ROW WHEN (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL) "
        "EXECUTE FUNCTION shifde.reject_session_unrevoke()",
        "CREATE TRIGGER trg_users_admin_promotion AFTER UPDATE OF is_platform_admin "
        "ON shifde.users FOR EACH ROW "
        "WHEN (NEW.is_platform_admin AND NOT OLD.is_platform_admin) "
        "EXECUTE FUNCTION shifde.revoke_sessions_on_admin_promotion()",
    )
    for table in ("users", "memberships", "service_accounts", "idp_configs"):
        _run(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
            "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()"
        )

    # ------------------------------------------------------------------ row-level security
    for table in TABLES:
        _run(
            f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        )
    for table in ("memberships", "service_accounts", "api_keys"):
        _run(
            f"CREATE POLICY tenant_isolation ON shifde.{table} "
            f"USING ({TENANT_KEYED}) WITH CHECK ({TENANT_KEYED})"
        )
    for table in ("local_credentials", "sessions", "user_identities"):
        _run(f"CREATE POLICY own_row ON shifde.{table} USING ({OWN_ROW}) WITH CHECK ({OWN_ROW})")
    _run(
        # users (SHI staff, one row per person): self, the platform administrator, or a member
        # of the current client. The membership EXISTS is reviewed text (B-112, B-901).
        "CREATE POLICY users_select ON shifde.users FOR SELECT USING ("
        "id = shifde.current_principal_id() OR shifde.is_platform_admin_ctx() OR EXISTS ("
        "SELECT 1 FROM shifde.memberships m WHERE m.user_id = users.id "
        "AND m.tenant_id = shifde.current_tenant_id()))",
        "CREATE POLICY users_update_self ON shifde.users FOR UPDATE "
        "USING (id = shifde.current_principal_id()) WITH CHECK (id = shifde.current_principal_id())",
        # a person creates only themself (JIT sign-in acts as the new user); every other user is
        # created by the platform administrator (deliberate: SETH also allowed any client context)
        "CREATE POLICY users_insert ON shifde.users FOR INSERT "
        "WITH CHECK (id = shifde.current_principal_id() OR shifde.is_platform_admin_ctx())",
        # setup tokens: the app role never reads or writes them directly (SETH: no policy)
        "CREATE POLICY no_app_access ON shifde.setup_tokens AS RESTRICTIVE "
        "USING (false) WITH CHECK (false)",
        # identity providers: installation-wide (tenant_id NULL) readable by all, a client's own
        "CREATE POLICY read_global_or_own ON shifde.idp_configs FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        f"CREATE POLICY write_own ON shifde.idp_configs FOR ALL USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
    )

    # ------------------------------------------------------------------ grants (no DELETE)
    _run(
        "GRANT SELECT ON shifde.users TO shifde_app",
        "GRANT INSERT (id, primary_email, display_name, status, created_at) ON shifde.users "
        "TO shifde_app",
        "GRANT UPDATE (last_login_at) ON shifde.users TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.memberships TO shifde_app",
        "GRANT UPDATE (status, row_version) ON shifde.memberships TO shifde_app",
        "GRANT SELECT ON shifde.local_credentials TO shifde_app",
        "GRANT UPDATE (failed_attempts, locked_until) ON shifde.local_credentials TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.sessions TO shifde_app",
        "GRANT UPDATE (revoked_at, last_seen_at, idle_expires_at) ON shifde.sessions TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.service_accounts TO shifde_app",
        "GRANT UPDATE (name, description, status, row_version) ON shifde.service_accounts "
        "TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.api_keys TO shifde_app",
        "GRANT UPDATE (last_used_at, revoked_at, revoked_by, expires_at) ON shifde.api_keys "
        "TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.user_identities TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.idp_configs TO shifde_app",
        "GRANT UPDATE (display_name, issuer, discovery_url, client_id, client_secret_ref_id, "
        "scopes, claim_mappings, jit, group_sync_mode, status, row_version) "
        "ON shifde.idp_configs TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "ALTER TABLE shifde.groups DROP CONSTRAINT IF EXISTS groups_idp_config_id_fkey",
        # users_select reads memberships: drop it before the tables
        "DROP POLICY IF EXISTS users_select ON shifde.users",
    )
    for table in reversed(TABLES):
        _run(f"DROP TABLE IF EXISTS shifde.{table}")
    _run(
        "DROP FUNCTION IF EXISTS shifde.revoke_sessions_on_admin_promotion()",
        "DROP FUNCTION IF EXISTS shifde.reject_session_unrevoke()",
        "DROP FUNCTION IF EXISTS shifde.guard_api_key_update()",
    )
