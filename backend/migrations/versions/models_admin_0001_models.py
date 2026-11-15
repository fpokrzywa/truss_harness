"""Model registry: providers, models, allowlists, routes, the DB-backed limiter, and the per-client
switch for the SHI-wide model provider.

Revision ID: models_admin_0001
Revises: base_0001
Depends on: tenancy_0001 (`tenants`), vendors_0001 (`model_providers.vendor_id`),
connections_0001 (`model_providers.connection_id`)

Taken from SETH 3ba0e06 `p03_0001_vendors_models_conns` (the seven tables); no later SETH migration
alters them. Columns, checks, indexes, `updated_at` triggers, forced row-level security, SETH's
policies and grants are SETH's, apart from the changes below.

The SHI-wide provider (design 3 and 5.3, DECISIONS B-1701, B-1702):

* `model_providers` and `models` are client-owned tables with shared rows, like `egress_policies`
  (B-1501): the one platform-level SHI provider and its models have `tenant_id` NULL. Every client
  reads them (`read_global_or_own`); writes need `tenant_id = current_tenant_id()`, or
  `tenant_id IS NULL` together with the platform-administrator context (`write_own`).
* `ck_model_providers__platform`: a NULL-tenant provider has the fixed id `SHI_PROVIDER_ID` (so
  there is at most one) and no connection (`connection_id` is NULL exactly then; a client
  connection cannot serve a shared provider). `ck_models__platform`: a NULL-tenant model belongs
  to that provider, and `trg_models_guard` makes that row exist; client rows are checked by
  SETH's composite `fk_models__provider`. The row itself is created by the service (part 2), not
  here: test cleanup truncates tables that reference `connections`, which would remove a seed.
* `shi_provider_access` (new, not in SETH): one row per client, `enabled` false by default (no row
  is off too). The client reads its own row (the owner sees the switch); only the platform
  administrator context inserts or updates it (RESTRICTIVE policies); no DELETE grant.

B-116 item 9 (constraints are checked as the owner, without row-level security):
`model_providers_vendor_id_fkey` stays plain (a provider names a global catalog vendor, tenant
NULL, or its own client's; `vendors` has no `(tenant_id, id)` key); `trg_model_providers_guard`
(BEFORE INSERT/UPDATE OF vendor_id, the writer's rights under RLS) refuses another client's private
vendor with the message an unknown id gets, as `trg_connections_guard` does (B-1502, B-1703).

Rollback: drops the eight tables and the guard (data-destructive: every client's providers,
models, allowlists, routes, limiter state and SHI-provider switches are lost). Back up first.
"""

from __future__ import annotations

from alembic import op

revision = "models_admin_0001"
down_revision = "base_0001"
branch_labels = ("models_admin",)
depends_on = ("tenancy_0001", "vendors_0001", "connections_0001")

# shifde.models_admin.repository.SHI_PROVIDER_ID
SHI_PROVIDER_ID = "'00000000-0000-4000-8000-00000000051a'::uuid"
TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)
ADMIN = "shifde.is_platform_admin_ctx()"
ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _rls(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
    ]


def _tenant_table(table: str) -> list[str]:
    return [
        *_rls(table),
        f"CREATE POLICY tenant_isolation ON shifde.{table} USING ({TENANT}) WITH CHECK ({TENANT})",
    ]


def _shared_table(table: str) -> list[str]:
    """Client rows plus shared rows (tenant NULL) every client reads and only the platform
    administrator context writes."""
    return [
        *_rls(table),
        f"CREATE POLICY read_global_or_own ON shifde.{table} FOR SELECT "
        f"USING (tenant_id IS NULL OR {TENANT})",
        f"CREATE POLICY write_own ON shifde.{table} FOR ALL USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
    ]


def _touch(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()"
    )


# Same-client vendor in the database: a buggy service can never wire another client's private
# vendor into a provider. Invoker rights, so RLS applies; another client's id is refused exactly
# like an unknown one. The shared provider (tenant NULL) may name only a global vendor.
MODEL_PROVIDERS_GUARD = """CREATE FUNCTION shifde.model_providers_guard() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
     IF NEW.vendor_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.vendors v WHERE v.id = NEW.vendor_id
           AND (v.tenant_id IS NULL OR v.tenant_id = NEW.tenant_id))
     THEN RAISE EXCEPTION 'vendor is not visible to the provider tenant'
          USING ERRCODE = '23514'; END IF;
     RETURN NEW;
   END $$"""


# A shared model (tenant NULL) belongs to the SHI provider row, which must exist; the composite
# fk_models__provider does not check a row whose tenant_id is NULL. A plain key on provider_id
# would answer differently for another client's provider id than for an unknown one, so the check
# is here and only for shared rows (B-1702). `provider_id` is not updatable.
MODELS_GUARD = """CREATE FUNCTION shifde.models_guard() RETURNS trigger LANGUAGE plpgsql AS
   $$ BEGIN
     IF NEW.tenant_id IS NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.model_providers p
         WHERE p.id = NEW.provider_id AND p.tenant_id IS NULL)
     THEN RAISE EXCEPTION 'a shared model belongs to the shared provider'
          USING ERRCODE = '23514'; END IF;
     RETURN NEW;
   END $$"""


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ model registry
        f"""CREATE TABLE shifde.model_providers (
            id uuid PRIMARY KEY,
            tenant_id uuid REFERENCES shifde.tenants (id),
            vendor_id uuid REFERENCES shifde.vendors (id),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            adapter_key text NOT NULL,
            connection_id uuid,
            settings jsonb NOT NULL DEFAULT '{{}}',
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN
                ('unknown', 'healthy', 'degraded', 'unavailable')),
            health_checked_at timestamptz,
            health_detail text,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT uq_model_providers__tenant_name UNIQUE (tenant_id, name),
            CONSTRAINT uq_model_providers__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_model_providers__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id),
            CONSTRAINT ck_model_providers__platform CHECK (
                (tenant_id IS NULL) = (connection_id IS NULL)
                AND (tenant_id IS NOT NULL OR id = {SHI_PROVIDER_ID}))
        )""",
        _touch("model_providers"),
        MODEL_PROVIDERS_GUARD,
        "REVOKE ALL ON FUNCTION shifde.model_providers_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_model_providers_guard BEFORE INSERT OR UPDATE OF vendor_id "
        "ON shifde.model_providers FOR EACH ROW EXECUTE FUNCTION shifde.model_providers_guard()",
        *_shared_table("model_providers"),
        "GRANT SELECT, INSERT ON shifde.model_providers TO shifde_app",
        "GRANT UPDATE (name, settings, status, health_status, health_checked_at, health_detail, "
        "row_version) ON shifde.model_providers TO shifde_app",
        f"""CREATE TABLE shifde.models (
            id uuid PRIMARY KEY,
            tenant_id uuid REFERENCES shifde.tenants (id),
            provider_id uuid NOT NULL,
            model_key text NOT NULL CHECK (length(model_key) BETWEEN 1 AND 300),
            display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
            family text,
            version_label text,
            capabilities text[] NOT NULL DEFAULT '{{}}',
            context_window int CHECK (context_window IS NULL OR context_window > 0),
            max_output_tokens int CHECK (max_output_tokens IS NULL OR max_output_tokens > 0),
            lifecycle text NOT NULL DEFAULT 'preview' CHECK (lifecycle IN
                ('preview', 'active', 'deprecated', 'retired')),
            deprecation_date date,
            rate_limits jsonb NOT NULL DEFAULT '{{}}',
            pricing_ref text,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT uq_models__provider_key UNIQUE (tenant_id, provider_id, model_key),
            CONSTRAINT uq_models__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_models__provider FOREIGN KEY (tenant_id, provider_id)
                REFERENCES shifde.model_providers (tenant_id, id),
            CONSTRAINT ck_models__platform CHECK (
                tenant_id IS NOT NULL OR provider_id = {SHI_PROVIDER_ID})
        )""",
        # shared models have tenant NULL, which the unique key above does not compare
        "CREATE UNIQUE INDEX uq_models__platform_key ON shifde.models "
        f"(COALESCE(tenant_id, {ZERO}), provider_id, model_key) WHERE tenant_id IS NULL",
        _touch("models"),
        MODELS_GUARD,
        "REVOKE ALL ON FUNCTION shifde.models_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_models_guard BEFORE INSERT ON shifde.models "
        "FOR EACH ROW EXECUTE FUNCTION shifde.models_guard()",
        *_shared_table("models"),
        "GRANT SELECT, INSERT ON shifde.models TO shifde_app",
        "GRANT UPDATE (display_name, family, version_label, capabilities, context_window, "
        "max_output_tokens, lifecycle, deprecation_date, rate_limits, pricing_ref, row_version) "
        "ON shifde.models TO shifde_app",
        """CREATE TABLE shifde.model_allowlists (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            scope_type text NOT NULL CHECK (scope_type IN ('tenant', 'workspace', 'project')),
            scope_id uuid NOT NULL,
            enabled boolean NOT NULL DEFAULT false,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_model_allowlists__scope UNIQUE (tenant_id, scope_type, scope_id),
            CONSTRAINT uq_model_allowlists__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        _touch("model_allowlists"),
        *_tenant_table("model_allowlists"),
        "GRANT SELECT, INSERT ON shifde.model_allowlists TO shifde_app",
        "GRANT UPDATE (enabled, row_version) ON shifde.model_allowlists TO shifde_app",
        """CREATE TABLE shifde.model_allowlist_models (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            allowlist_id uuid NOT NULL,
            model_id uuid NOT NULL,
            PRIMARY KEY (allowlist_id, model_id),
            CONSTRAINT fk_allowlist_models__allowlist FOREIGN KEY (tenant_id, allowlist_id)
                REFERENCES shifde.model_allowlists (tenant_id, id),
            CONSTRAINT fk_allowlist_models__model FOREIGN KEY (tenant_id, model_id)
                REFERENCES shifde.models (tenant_id, id)
        )""",
        *_tenant_table("model_allowlist_models"),
        "GRANT SELECT, INSERT, DELETE ON shifde.model_allowlist_models TO shifde_app",
        """CREATE TABLE shifde.model_routes (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            scope_type text NOT NULL CHECK (scope_type IN ('tenant', 'workspace', 'project')),
            scope_id uuid NOT NULL,
            primary_model_id uuid NOT NULL,
            fallback_model_ids uuid[] NOT NULL DEFAULT '{}',
            conditions jsonb NOT NULL DEFAULT '{}',
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT uq_model_routes__tenant_name UNIQUE (tenant_id, name),
            CONSTRAINT fk_model_routes__primary FOREIGN KEY (tenant_id, primary_model_id)
                REFERENCES shifde.models (tenant_id, id)
        )""",
        _touch("model_routes"),
        *_tenant_table("model_routes"),
        "GRANT SELECT, INSERT ON shifde.model_routes TO shifde_app",
        "GRANT UPDATE (name, primary_model_id, fallback_model_ids, conditions, row_version) "
        "ON shifde.model_routes TO shifde_app",
        # ------------------------------------------------------------------ DB-backed limiter
        """CREATE TABLE shifde.rate_limit_windows (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            bucket_key text NOT NULL,
            window_start timestamptz NOT NULL,
            requests bigint NOT NULL DEFAULT 0,
            tokens bigint NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, bucket_key, window_start)
        )""",
        *_tenant_table("rate_limit_windows"),
        "GRANT SELECT, INSERT, DELETE ON shifde.rate_limit_windows TO shifde_app",
        "GRANT UPDATE (requests, tokens) ON shifde.rate_limit_windows TO shifde_app",
        """CREATE TABLE shifde.concurrency_leases (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            bucket_key text NOT NULL,
            acquired_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        )""",
        "CREATE INDEX ix_concurrency_leases__bucket ON shifde.concurrency_leases "
        "(tenant_id, bucket_key, expires_at)",
        *_tenant_table("concurrency_leases"),
        "GRANT SELECT, INSERT, DELETE ON shifde.concurrency_leases TO shifde_app",
        # ------------------------------------------------------------------ SHI provider switch
        """CREATE TABLE shifde.shi_provider_access (
            tenant_id uuid PRIMARY KEY REFERENCES shifde.tenants (id),
            enabled boolean NOT NULL DEFAULT false,
            changed_by uuid,
            changed_at timestamptz NOT NULL DEFAULT now(),
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz
        )""",
        _touch("shi_provider_access"),
        *_tenant_table("shi_provider_access"),
        # a client reads its own switch but never sets it: the platform administrator does
        "CREATE POLICY insert_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR INSERT WITH CHECK ({ADMIN})",
        "CREATE POLICY update_by_platform_admin ON shifde.shi_provider_access AS RESTRICTIVE "
        f"FOR UPDATE USING ({ADMIN}) WITH CHECK ({ADMIN})",
        "GRANT SELECT, INSERT ON shifde.shi_provider_access TO shifde_app",
        "GRANT UPDATE (enabled, changed_by, changed_at, row_version) "
        "ON shifde.shi_provider_access TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP TABLE shifde.shi_provider_access",
        "DROP TABLE shifde.concurrency_leases",
        "DROP TABLE shifde.rate_limit_windows",
        "DROP TABLE shifde.model_routes",
        "DROP TABLE shifde.model_allowlist_models",
        "DROP TABLE shifde.model_allowlists",
        "DROP TABLE shifde.models",
        "DROP TABLE shifde.model_providers",
        "DROP FUNCTION shifde.models_guard()",
        "DROP FUNCTION shifde.model_providers_guard()",
    )
