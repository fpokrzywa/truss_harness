"""Connections: egress policies, connections, their test and discovery history.

Revision ID: connections_0001
Revises: base_0001
Depends on: secrets_0001 (`connections.secret_ref_id`), tenancy_0001 (`tenants`, `workspaces`,
`projects`), vendors_0001 (`connections.vendor_id`)

Taken from SETH 3ba0e06 `p03_0001_vendors_models_conns` (the four tables) and
`p03_0003_conn_test_hash` (`connection_tests.config_hash`); no later SETH migration alters them.
Columns, checks, indexes, `updated_at` triggers, the same-tenant guard `connections_guard`, forced
row-level security, SETH's policies and grants and the strict installation egress default are
SETH's.

Isolation (DECISIONS B-1501, B-1502):

* `egress_policies` is a client-owned table with one shared row, like `vendors` (B-1301): the
  installation default has `tenant_id` NULL and is readable by every client (it bounds their
  effective policy); writes need `tenant_id = current_tenant_id()`, or `tenant_id IS NULL`
  together with the platform-administrator context. `shifde_app` keeps SETH's SELECT (the
  installation overview reads `allow_private`, B-1105), INSERT and column-wise UPDATE.
* `connections`, `connection_tests`, `connection_discoveries`: `tenant_isolation` on
  `tenant_id = current_tenant_id()`, USING and WITH CHECK.
* B-116 item 9 (constraints are checked as the owner, without row-level security): SETH's plain
  `connections_secret_ref_id_fkey` and `connections_egress_policy_id_fkey` become composite
  `(tenant_id, ...)` keys, `fk_connections__secret` and `fk_connections__egress_policy`, with the
  `uq_egress_policies__tenant_id_id` key they need. A connection never names an installation
  secret or the installation egress default (SETH's guard already refused both), so the composite
  keys check every row. `connections_vendor_id_fkey` stays plain: a connection names a global
  catalog vendor (tenant NULL) or its own client's; `trg_connections_guard` (BEFORE INSERT/UPDATE,
  the writer's rights under RLS) refuses another client's vendor with the message it gives an
  unknown id (listed in `PLAIN_FOREIGN_KEYS`, tests/schema/test_rls_schema.py).

`model_providers.connection_id` and the runtime / ontology keys to `connections` belong to their
modules' migrations.

Rollback: drops the four tables and the guard function (data-destructive: every client's
connections, egress policies, test and discovery history are lost; upgrade recreates the
installation default). Back up first.
"""

from __future__ import annotations

from alembic import op

revision = "connections_0001"
down_revision = "base_0001"
branch_labels = ("connections",)
depends_on = ("secrets_0001", "tenancy_0001", "vendors_0001")

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)
ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} USING ({TENANT}) WITH CHECK ({TENANT})",
    ]


def _touch(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()"
    )


# Same-tenant integrity in the database (SETH): a buggy service can never wire another client's
# secret, egress policy or vendor into a connection. Runs as invoker, so RLS applies; another
# client's id is refused exactly like an unknown one. BEFORE triggers run before the key checks.
CONNECTIONS_GUARD = """CREATE FUNCTION shifde.connections_guard() RETURNS trigger LANGUAGE plpgsql AS
   $$ BEGIN
     IF NEW.secret_ref_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.secrets s
         WHERE s.id = NEW.secret_ref_id AND s.tenant_id = NEW.tenant_id)
     THEN RAISE EXCEPTION 'secret must belong to the connection tenant'
          USING ERRCODE = '23514'; END IF;
     IF NEW.egress_policy_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.egress_policies p
         WHERE p.id = NEW.egress_policy_id AND p.tenant_id = NEW.tenant_id)
     THEN RAISE EXCEPTION 'egress policy must belong to the connection tenant'
          USING ERRCODE = '23514'; END IF;
     IF NEW.vendor_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.vendors v WHERE v.id = NEW.vendor_id
           AND (v.tenant_id IS NULL OR v.tenant_id = NEW.tenant_id))
     THEN RAISE EXCEPTION 'vendor is not visible to the connection tenant'
          USING ERRCODE = '23514'; END IF;
     RETURN NEW;
   END $$"""


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ egress policies
        """CREATE TABLE shifde.egress_policies (
            id uuid PRIMARY KEY,
            tenant_id uuid REFERENCES shifde.tenants (id),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            is_default boolean NOT NULL DEFAULT false,
            allowed_schemes text[] NOT NULL DEFAULT '{https}',
            allowed_ports int[] NOT NULL DEFAULT '{443}',
            allowed_hosts text[] NOT NULL DEFAULT '{}',
            allowed_cidrs text[] NOT NULL DEFAULT '{}',
            allow_private boolean NOT NULL DEFAULT false,
            allow_redirects boolean NOT NULL DEFAULT false,
            max_response_bytes int NOT NULL DEFAULT 5000000
                CHECK (max_response_bytes BETWEEN 1 AND 1000000000),
            timeout_seconds double precision NOT NULL DEFAULT 10
                CHECK (timeout_seconds > 0 AND timeout_seconds <= 600),
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT uq_egress_policies__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_egress_policies__scope_name ON shifde.egress_policies "
        f"(COALESCE(tenant_id, {ZERO}), name)",
        "CREATE UNIQUE INDEX uq_egress_policies__one_default ON shifde.egress_policies "
        f"(COALESCE(tenant_id, {ZERO})) WHERE is_default",
        _touch("egress_policies"),
        "ALTER TABLE shifde.egress_policies ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.egress_policies FORCE ROW LEVEL SECURITY",
        # The installation default (tenant_id NULL) is readable by every client (it bounds their
        # effective policy); writes: own client rows, or platform-admin context for the default.
        "CREATE POLICY read_global_or_own ON shifde.egress_policies FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        f"CREATE POLICY write_own ON shifde.egress_policies FOR ALL USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
        "GRANT SELECT, INSERT ON shifde.egress_policies TO shifde_app",
        "GRANT UPDATE (name, is_default, allowed_schemes, allowed_ports, allowed_hosts, "
        "allowed_cidrs, allow_private, allow_redirects, max_response_bytes, timeout_seconds, "
        "row_version) ON shifde.egress_policies TO shifde_app",
        # Strict installation default: https on 443, no private ranges, no redirects. Development
        # and test profiles widen it in code (loopback only), never in this row.
        "INSERT INTO shifde.egress_policies (id, tenant_id, name, is_default) "
        "VALUES (gen_random_uuid(), NULL, 'installation-default', true)",
        # ------------------------------------------------------------------ connections
        """CREATE TABLE shifde.connections (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            workspace_id uuid,
            project_id uuid,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            connection_type text NOT NULL CHECK (connection_type IN
                ('rest', 'mcp', 'database', 'data_platform', 'model_api')),
            adapter_key text NOT NULL,
            vendor_id uuid REFERENCES shifde.vendors (id),
            config jsonb NOT NULL DEFAULT '{}',
            auth_method text NOT NULL DEFAULT 'none' CHECK (auth_method IN
                ('none', 'api_key', 'bearer', 'oauth2_client_credentials', 'basic', 'key_pair',
                 'mtls')),
            secret_ref_id uuid,
            egress_policy_id uuid,
            access_mode text NOT NULL DEFAULT 'read' CHECK (access_mode IN ('read', 'read_write')),
            status text NOT NULL DEFAULT 'draft' CHECK (status IN
                ('draft', 'active', 'degraded', 'disabled', 'retired')),
            health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN
                ('unknown', 'healthy', 'degraded', 'unhealthy')),
            last_tested_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT ck_connections__scope CHECK (project_id IS NULL OR workspace_id IS NOT NULL),
            CONSTRAINT uq_connections__tenant_name UNIQUE (tenant_id, name),
            CONSTRAINT uq_connections__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_connections__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_connections__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id),
            CONSTRAINT fk_connections__secret FOREIGN KEY (tenant_id, secret_ref_id)
                REFERENCES shifde.secrets (tenant_id, id),
            CONSTRAINT fk_connections__egress_policy FOREIGN KEY (tenant_id, egress_policy_id)
                REFERENCES shifde.egress_policies (tenant_id, id)
        )""",
        "CREATE INDEX ix_connections__tenant_scope ON shifde.connections "
        "(tenant_id, workspace_id, project_id)",
        _touch("connections"),
        CONNECTIONS_GUARD,
        "REVOKE ALL ON FUNCTION shifde.connections_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF secret_ref_id, "
        "egress_policy_id, vendor_id ON shifde.connections FOR EACH ROW "
        "EXECUTE FUNCTION shifde.connections_guard()",
        *_tenant_table("connections"),
        "GRANT SELECT, INSERT ON shifde.connections TO shifde_app",
        "GRANT UPDATE (name, vendor_id, config, auth_method, secret_ref_id, egress_policy_id, "
        "access_mode, status, health_status, last_tested_at, row_version) "
        "ON shifde.connections TO shifde_app",
        # ------------------------------------------------------------------ test history
        """CREATE TABLE shifde.connection_tests (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            connection_id uuid NOT NULL,
            started_at timestamptz NOT NULL,
            finished_at timestamptz NOT NULL,
            duration_ms int NOT NULL,
            outcome text NOT NULL CHECK (outcome IN ('pass', 'fail')),
            checks jsonb NOT NULL DEFAULT '[]',
            error_class text,
            redacted_message text,
            config_version int NOT NULL DEFAULT 1,
            tested_by uuid,
            config_hash text,
            CONSTRAINT fk_connection_tests__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id)
        )""",
        "CREATE INDEX ix_connection_tests__connection ON shifde.connection_tests "
        "(connection_id, started_at DESC)",
        *_tenant_table("connection_tests"),
        "GRANT SELECT, INSERT ON shifde.connection_tests TO shifde_app",
        # ------------------------------------------------------------------ discoveries
        """CREATE TABLE shifde.connection_discoveries (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            connection_id uuid NOT NULL,
            discovered_at timestamptz NOT NULL,
            kind text NOT NULL CHECK (kind IN ('schema', 'mcp_tools', 'openapi')),
            payload jsonb NOT NULL,
            payload_hash text NOT NULL,
            discovered_by uuid,
            CONSTRAINT fk_connection_discoveries__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id)
        )""",
        "CREATE INDEX ix_connection_discoveries__connection ON shifde.connection_discoveries "
        "(connection_id, discovered_at DESC)",
        *_tenant_table("connection_discoveries"),
        "GRANT SELECT, INSERT ON shifde.connection_discoveries TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP TABLE shifde.connection_discoveries",
        "DROP TABLE shifde.connection_tests",
        "DROP TABLE shifde.connections",
        "DROP FUNCTION shifde.connections_guard()",
        "DROP TABLE shifde.egress_policies",
    )
