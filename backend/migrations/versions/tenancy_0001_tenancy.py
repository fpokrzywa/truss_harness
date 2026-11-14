"""Tenancy, part 1: tenants, workspaces, projects, installation_settings and the keys every
earlier module deferred to tenancy.

Revision ID: tenancy_0001
Revises: identity_0005, ledger_0001

* SETH 3ba0e06's final schema for `tenants`, `workspaces`, `projects` and
  `installation_settings` (p01_0001, p01_0002): columns, keys, checks, `updated_at` triggers,
  forced row-level security, SETH's policies and grants. `installation_settings` holds no client
  data and is listed in `tests/schema/global_tables.txt` (DECISIONS B-1002).
* The deferred `tenant_id -> tenants (id)` keys of ledger, authz, secrets and identity (SETH
  names `<table>_tenant_id_fkey`) and `sessions.active_tenant_id -> tenants (id)`.
* B-116 items 7 and 9 (constraints are checked as the owner, without row-level security):
  composite `(tenant_id, ...)` keys for `role_permissions -> roles`,
  `rate_card_versions -> rate_cards` and `oidc_auth_requests -> idp_configs`, with the
  `(tenant_id, id)` unique keys they need; SETH's plain keys stay for installation rows
  (tenant NULL), as B-801 and B-903 did.
* `secrets.scope_id` must name a workspace or project of the secret's own client
  (`trg_secrets_scope`, invoker rights; carry_forward secrets B-802).
* Grant sweep: `shifde_app` loses INSERT on `user_identities` and `oidc_auth_requests`, which
  RESTRICTIVE false policies already made useless (identity review 4 item 4).
* `shifde_my_memberships()` (SETH p01_0001), locked down as the identity definers (B-909): the
  caller's active memberships of clients where the caller holds a live role binding, directly
  or through an active platform group; role names only from that client's bindings.

Rollback: drops the functions, trigger, keys and the four tables (their rows are lost) and
restores the two INSERT grants.
"""

from __future__ import annotations

from alembic import op

revision = "tenancy_0001"
# A merge of the branches whose tables it alters, not `base_0001` + `depends_on`: Alembic's head
# bookkeeping deletes `base_0001` from the version table when every current head is a dependency
# (KeyError on upgrade from base; DECISIONS B-1001).
down_revision = ("identity_0005", "ledger_0001")
branch_labels = ("tenancy",)
depends_on = None

SLUG_RE = "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
TENANT = "tenant_id = shifde.current_tenant_id()"
_LOCKED = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"

TABLES = (
    """CREATE TABLE shifde.installation_settings (
        id int PRIMARY KEY CHECK (id = 1),
        branding jsonb NOT NULL DEFAULT '{}',
        feature_flags jsonb NOT NULL DEFAULT '{}',
        auth_modes jsonb NOT NULL DEFAULT '{}',
        default_limits jsonb NOT NULL DEFAULT '{}',
        profile_version int NOT NULL DEFAULT 1,
        updated_by uuid,
        updated_at timestamptz
    )""",
    "INSERT INTO shifde.installation_settings (id) VALUES (1)",
    f"""CREATE TABLE shifde.tenants (
        id uuid PRIMARY KEY,
        slug text NOT NULL,
        name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
        description text,
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        data_region text,
        settings jsonb NOT NULL DEFAULT '{{}}',
        row_version int NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz,
        created_by uuid,
        CONSTRAINT uq_tenants__slug UNIQUE (slug),
        CONSTRAINT ck_tenants__slug CHECK (slug ~ '{SLUG_RE}')
    )""",
    f"""CREATE TABLE shifde.workspaces (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
        slug text NOT NULL,
        name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
        description text,
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        settings jsonb NOT NULL DEFAULT '{{}}',
        row_version int NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz,
        created_by uuid,
        CONSTRAINT uq_workspaces__tenant_id_slug UNIQUE (tenant_id, slug),
        CONSTRAINT uq_workspaces__tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_workspaces__slug CHECK (slug ~ '{SLUG_RE}')
    )""",
    f"""CREATE TABLE shifde.projects (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,
        workspace_id uuid NOT NULL,
        slug text NOT NULL,
        name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
        description text,
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
        settings jsonb NOT NULL DEFAULT '{{}}',
        row_version int NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz,
        created_by uuid,
        CONSTRAINT uq_projects__tenant_id_workspace_id_slug UNIQUE (tenant_id, workspace_id, slug),
        CONSTRAINT uq_projects__tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT fk_projects__workspace FOREIGN KEY (tenant_id, workspace_id)
            REFERENCES shifde.workspaces (tenant_id, id),
        CONSTRAINT ck_projects__slug CHECK (slug ~ '{SLUG_RE}')
    )""",
)

# SETH names; the deferred keys of earlier modules (carry_forward, deliberate_changes.yaml)
TENANT_KEYS = (
    "usage_events",
    "usage_rollups_daily",
    "rate_cards",
    "rate_card_versions",
    "budgets",
    "budget_reservations",
    "budget_alerts",
    "quotas",
    "roles",
    "role_permissions",
    "role_bindings",
    "policy_decisions",
    "secrets",
    "secret_access_log",
    "memberships",
    "setup_tokens",
    "service_accounts",
    "api_keys",
    "idp_configs",
    "oidc_auth_requests",
)

# B-116 items 7, 9: (table, unique key, columns) targets and (table, key, definition) composites
UNIQUE_KEYS = (
    ("roles", "uq_roles__tenant_id_id", "tenant_id, id"),
    ("rate_cards", "uq_rate_cards__tenant_id_id", "tenant_id, id"),
    ("idp_configs", "uq_idp_configs__tenant_id_id", "tenant_id, id"),
)
COMPOSITE_KEYS = (
    (
        "role_permissions",
        "fk_role_permissions__role",
        "FOREIGN KEY (tenant_id, role_id) REFERENCES shifde.roles (tenant_id, id) "
        "ON DELETE CASCADE",
    ),
    (
        "rate_card_versions",
        "fk_rate_card_versions__rate_card",
        "FOREIGN KEY (tenant_id, rate_card_id) REFERENCES shifde.rate_cards (tenant_id, id)",
    ),
    (
        "oidc_auth_requests",
        "fk_oidc_auth_requests__idp",
        "FOREIGN KEY (tenant_id, idp_config_id) REFERENCES shifde.idp_configs (tenant_id, id)",
    ),
)

# INSERT made useless by a RESTRICTIVE false policy (identity review 4 item 4)
REVOKED_INSERT = ("user_identities", "oidc_auth_requests")

CHECK_SECRET_SCOPE = """CREATE FUNCTION shifde.check_secret_scope() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
        IF NEW.scope_type = 'workspace' THEN
          IF NEW.tenant_id IS NULL OR NOT EXISTS (
               SELECT 1 FROM shifde.workspaces w
                WHERE w.tenant_id = NEW.tenant_id AND w.id = NEW.scope_id) THEN
            RAISE EXCEPTION 'secret scope is not in this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        ELSIF NEW.scope_type = 'project' THEN
          IF NEW.tenant_id IS NULL OR NOT EXISTS (
               SELECT 1 FROM shifde.projects p
                WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.scope_id) THEN
            RAISE EXCEPTION 'secret scope is not in this tenant'
              USING ERRCODE = 'check_violation';
          END IF;
        END IF;
        RETURN NEW;
      END $$"""

# Role names of live bindings of the person in membership `m`'s client: direct, or through an
# active platform group the person belongs to.
_ROLE_NAMES = """ARRAY(SELECT DISTINCT r.name
              FROM shifde.role_bindings rb JOIN shifde.roles r ON r.id = rb.role_id
             WHERE rb.tenant_id = m.tenant_id
               AND (rb.expires_at IS NULL OR rb.expires_at > pg_catalog.now())
               AND ((rb.subject_type = 'user' AND rb.subject_id = m.user_id)
                    OR (rb.subject_type = 'group' AND rb.subject_id IN (
                          SELECT gm.group_id
                            FROM shifde.group_members gm
                            JOIN shifde.groups g ON g.id = gm.group_id
                           WHERE gm.user_id = m.user_id AND g.status = 'active')))
             ORDER BY 1)"""

MY_MEMBERSHIPS = f"""CREATE FUNCTION shifde.shifde_my_memberships()
   RETURNS TABLE (tenant_id uuid, slug text, name text, status text, role_names text[])
   LANGUAGE sql STABLE {_LOCKED} AS
   $$ SELECT x.id, x.slug, x.name, x.status, x.role_names
        FROM (SELECT t.id, t.slug, t.name, t.status, {_ROLE_NAMES} AS role_names
                FROM shifde.memberships m JOIN shifde.tenants t ON t.id = m.tenant_id
               WHERE m.user_id = shifde.current_principal_id() AND m.status = 'active') x
       WHERE pg_catalog.cardinality(x.role_names) > 0
       ORDER BY x.slug $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(*TABLES)
    for table in ("tenants", "workspaces", "projects", "installation_settings"):
        _run(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
            "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
            f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        )
    _run(
        # tenants: own row; the platform-administrator context lists and creates clients (a
        # platform catalog, B-116 item 10 exception in tests/schema/test_rls_schema.py)
        "CREATE POLICY tenant_isolation ON shifde.tenants "
        "USING (id = shifde.current_tenant_id() OR shifde.is_platform_admin_ctx()) "
        "WITH CHECK (id = shifde.current_tenant_id() OR shifde.is_platform_admin_ctx())",
        f"CREATE POLICY tenant_isolation ON shifde.workspaces USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
        f"CREATE POLICY tenant_isolation ON shifde.projects USING ({TENANT}) WITH CHECK ({TENANT})",
        "CREATE POLICY read_all ON shifde.installation_settings FOR SELECT USING (true)",
        "CREATE POLICY write_platform_admin ON shifde.installation_settings FOR UPDATE "
        "USING (shifde.is_platform_admin_ctx()) WITH CHECK (shifde.is_platform_admin_ctx())",
        "GRANT SELECT ON shifde.installation_settings TO shifde_app",
    )
    for table in ("tenants", "workspaces", "projects"):
        _run(
            f"GRANT SELECT, INSERT ON shifde.{table} TO shifde_app",
            f"GRANT UPDATE (name, description, status, row_version) ON shifde.{table} "
            "TO shifde_app",
        )
    for table in TENANT_KEYS:
        _run(
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT {table}_tenant_id_fkey "
            "FOREIGN KEY (tenant_id) REFERENCES shifde.tenants (id)"
        )
    _run(
        "ALTER TABLE shifde.sessions ADD CONSTRAINT sessions_active_tenant_id_fkey "
        "FOREIGN KEY (active_tenant_id) REFERENCES shifde.tenants (id)"
    )
    for table, name, columns in UNIQUE_KEYS:
        _run(f"ALTER TABLE shifde.{table} ADD CONSTRAINT {name} UNIQUE ({columns})")
    for table, name, definition in COMPOSITE_KEYS:
        _run(f"ALTER TABLE shifde.{table} ADD CONSTRAINT {name} {definition}")
    for table in REVOKED_INSERT:
        _run(f"REVOKE INSERT ON shifde.{table} FROM shifde_app")
    _run(
        CHECK_SECRET_SCOPE,
        "REVOKE ALL ON FUNCTION shifde.check_secret_scope() FROM PUBLIC",
        "CREATE TRIGGER trg_secrets_scope BEFORE INSERT OR UPDATE OF tenant_id, scope_type, "
        "scope_id ON shifde.secrets FOR EACH ROW EXECUTE FUNCTION shifde.check_secret_scope()",
        MY_MEMBERSHIPS,
        "REVOKE ALL ON FUNCTION shifde.shifde_my_memberships() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION shifde.shifde_my_memberships() TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP FUNCTION shifde.shifde_my_memberships()",
        "DROP TRIGGER trg_secrets_scope ON shifde.secrets",
        "DROP FUNCTION shifde.check_secret_scope()",
    )
    for table in REVOKED_INSERT:
        _run(f"GRANT INSERT ON shifde.{table} TO shifde_app")
    for table, name, _definition in reversed(COMPOSITE_KEYS):
        _run(f"ALTER TABLE shifde.{table} DROP CONSTRAINT {name}")
    for table, name, _columns in reversed(UNIQUE_KEYS):
        _run(f"ALTER TABLE shifde.{table} DROP CONSTRAINT {name}")
    _run("ALTER TABLE shifde.sessions DROP CONSTRAINT sessions_active_tenant_id_fkey")
    for table in reversed(TENANT_KEYS):
        _run(f"ALTER TABLE shifde.{table} DROP CONSTRAINT {table}_tenant_id_fkey")
    _run(
        "DROP TABLE shifde.projects",
        "DROP TABLE shifde.workspaces",
        "DROP TABLE shifde.tenants",
        "DROP TABLE shifde.installation_settings",
    )
