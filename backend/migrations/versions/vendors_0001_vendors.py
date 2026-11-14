"""Vendors: the vendor catalog (`vendors`), seeded with the declared global entries.

Revision ID: vendors_0001
Revises: base_0001
Depends on: tenancy_0001 (`vendors.tenant_id` references `tenants`)

Taken from SETH 3ba0e06 `p03_0001_vendors_models_conns` (the only SETH migration that creates or
alters the table; `p09_0001` changes rows only) with SETH's final schema unchanged.

Isolation (DECISIONS B-1301): `vendors` is a client-owned table with shared rows. Global catalog
rows have `tenant_id` NULL; a client adds private ones. Forced RLS: SELECT shows the global rows
and the client's own (`tenant_id IS NULL OR tenant_id = current_tenant_id()`); every write needs
`tenant_id = current_tenant_id()`, or `tenant_id IS NULL` together with the platform-administrator
context. The app role has SELECT, INSERT and column-wise UPDATE (no DELETE, no change of
`tenant_id`, `key` or `category`).

`adapter_conformance_runs` stays with the adapters migration. `connections` and `model_providers`
add their foreign keys to `vendors` in their own migrations.

Rollback: drops the table (data-destructive: private vendors of every client are lost; the global
seeds are recreated by upgrade). Back up first.
"""

from __future__ import annotations

import json

from alembic import op

revision = "vendors_0001"
down_revision = "base_0001"
branch_labels = ("vendors",)
depends_on = "tenancy_0001"

TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)
ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _seed_vendor(
    key: str,
    name: str,
    category: str,
    description: str,
    caps: list[tuple[str, str | None]],
    requirements: list[str],
    adapters: list[str],
) -> str:
    capabilities = json.dumps([{"capability": c, "adapter_key": a} for c, a in caps])
    adapter_array = "ARRAY[" + ", ".join(f"'{a}'" for a in adapters) + "]::text[]"
    return (
        "INSERT INTO shifde.vendors (id, tenant_id, key, name, category, description, version, "
        "configuration_requirements, adapter_keys, capabilities) VALUES "
        f"(gen_random_uuid(), NULL, '{key}', '{name}', '{category}', '{description}', '1', "
        f"'{json.dumps(requirements)}'::jsonb, {adapter_array}, '{capabilities}'::jsonb)"
    )


def upgrade() -> None:
    _run(
        """CREATE TABLE shifde.vendors (
            id uuid PRIMARY KEY,
            tenant_id uuid REFERENCES shifde.tenants (id),
            key text NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$'),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            category text NOT NULL CHECK (category IN
                ('data_platform', 'model_provider', 'saas_api', 'mcp', 'other')),
            description text,
            version text,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'retired')),
            configuration_requirements jsonb NOT NULL DEFAULT '[]',
            adapter_keys text[] NOT NULL DEFAULT '{}',
            capabilities jsonb NOT NULL DEFAULT '[]',
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid
        )""",
        "CREATE UNIQUE INDEX uq_vendors__scope_key ON shifde.vendors "
        f"(COALESCE(tenant_id, {ZERO}), key)",
        "CREATE TRIGGER trg_vendors_updated_at BEFORE UPDATE ON shifde.vendors "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        "ALTER TABLE shifde.vendors ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.vendors FORCE ROW LEVEL SECURITY",
        "CREATE POLICY read_global_or_own ON shifde.vendors FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        f"CREATE POLICY write_own ON shifde.vendors FOR ALL USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
        "GRANT SELECT, INSERT ON shifde.vendors TO shifde_app",
        "GRANT UPDATE (name, description, version, status, configuration_requirements, "
        "adapter_keys, capabilities, row_version) ON shifde.vendors TO shifde_app",
    )
    planned: list[tuple[str, str | None]] = [
        ("query.sql", None),
        ("query.pushdown_join", None),
        ("discover.schema", None),
    ]
    declared = "Declared only: no adapter is implemented or validated."
    _run(
        _seed_vendor(
            "databricks",
            "Databricks",
            "data_platform",
            declared,
            planned,
            ["workspace host", "OAuth M2M or personal access token"],
            [],
        ),
        _seed_vendor(
            "snowflake",
            "Snowflake",
            "data_platform",
            declared,
            planned,
            ["account identifier", "key-pair or OAuth credentials"],
            [],
        ),
        _seed_vendor(
            "microsoft-fabric",
            "Microsoft Fabric",
            "data_platform",
            declared,
            planned,
            ["workspace and warehouse endpoint", "Entra ID service principal"],
            [],
        ),
        _seed_vendor(
            "postgresql",
            "PostgreSQL",
            "data_platform",
            "PostgreSQL via the postgres adapter (read-only by default).",
            [
                ("query.sql", "postgres"),
                ("discover.schema", "postgres"),
                ("query.pushdown_join", None),
            ],
            ["host, port, database", "database role (read-only recommended)"],
            ["postgres"],
        ),
        _seed_vendor(
            "generic-rest",
            "Generic REST API",
            "saas_api",
            "Any HTTP/JSON API via the rest.http adapter.",
            [("rest.call", "rest.http"), ("discover.openapi", "rest.http")],
            ["base URL", "optional API key or bearer token"],
            ["rest.http"],
        ),
        _seed_vendor(
            "generic-mcp",
            "Generic MCP Server",
            "mcp",
            "Model Context Protocol server over streamable HTTP (stdio is not supported).",
            [("mcp.tools", "mcp_http")],
            ["server URL", "optional bearer token"],
            ["mcp_http"],
        ),
        _seed_vendor(
            "openai-compatible",
            "OpenAI-compatible endpoint",
            "model_provider",
            "Any endpoint implementing the OpenAI chat-completions API.",
            [
                ("model.chat", "openai_compatible"),
                ("model.list", "openai_compatible"),
                ("model.health", "openai_compatible"),
                ("model.stream", None),
                ("model.tools", None),
            ],
            ["base URL", "optional API key"],
            ["openai_compatible"],
        ),
        _seed_vendor(
            "anthropic",
            "Anthropic",
            "model_provider",
            "Anthropic Messages API (experimental: no recorded live evidence).",
            [
                ("model.chat", "anthropic"),
                ("model.list", "anthropic"),
                ("model.health", "anthropic"),
                ("model.stream", None),
                ("model.tools", None),
            ],
            ["API key"],
            ["anthropic"],
        ),
    )


def downgrade() -> None:
    _run("DROP TABLE shifde.vendors")
