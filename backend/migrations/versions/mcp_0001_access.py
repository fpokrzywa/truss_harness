"""MCP access, part 1: the per-client MCP settings and the MCP permission keys.

Revision ID: mcp_0001
Revises: base_0001
Depends on: tenancy_0007 (`tenants`) and identity_0007; authz_0002 (`permissions`, `roles`,
`role_permissions`) is an ancestor of identity_0007 through identity_0005. Naming it as well
makes this revision an alembic merge of three heads, and `alembic upgrade heads` from an empty
database then fails in identity_0001 for most hash seeds (KeyError 'base_0001'), as
delivery_0001's docstring records.

Design 2026-11-23 (sub-project 8) sections 3.2 to 3.5 and 10 items 22 to 24.

* `mcp_client_settings`: one row per organization, keyed by `tenant_id`. `enabled` (default
  false) switches MCP calls of the organization's API keys on; `rate_per_minute` (1..600, default
  60) is a key's tool calls per minute; `max_cost_per_call` (above 0, at most 25, default 1.00)
  caps the cost of one `start_run`; `allowed_tools` names the tools the organization's keys may
  call (NULL: the default set; an empty array: none), each element from the closed list of the
  v1 tools and the three tools of SP8-3. A missing row reads as the defaults.

Isolation: client-owned, forced row-level security with one `tenant_isolation` policy (USING and
WITH CHECK on `tenant_id = current_tenant_id()`). Grants to `shifde_app`: SELECT, INSERT and
UPDATE of `enabled`, `rate_per_minute`, `max_cost_per_call`, `allowed_tools`, `updated_by` and
`updated_at` only; no UPDATE of `tenant_id` and no DELETE.

Permissions: `mcp:access`, `mcp:settings:read` and `mcp:settings:write` (privileged);
`tenant_administrator` holds all three, `developer` `mcp:access` and `mcp:settings:read`; no
other role (`platform_administrator` included) holds any.

Rollback: removes every `mcp:*` role grant (custom roles included) and permission, then drops the
table (data-destructive: every organization's MCP settings are lost).
"""

from __future__ import annotations

from alembic import op

revision = "mcp_0001"
down_revision = "base_0001"
branch_labels = ("mcp",)
depends_on = ("tenancy_0007", "identity_0007")  # authz_0002 through identity_0005

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000001"
DEVELOPER = "01900000-0000-7000-8000-000000000004"
TOOL_NAMES = (
    "whoami",
    "list_projects",
    "list_agents",
    "start_run",
    "get_run",
    "list_releases",
    "get_release",
    "list_deployments",
    "get_deployment",
    "board_get",
    "assist_ask",
    "assist_get",
)

# key, resource type, action, description, is_privileged
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    ("mcp:access", "mcp", "access", "Call the MCP tools with an API key", False),
    ("mcp:settings:read", "mcp_settings", "read", "View the MCP settings", False),
    ("mcp:settings:write", "mcp_settings", "write", "Change the MCP settings", True),
]
DEVELOPER_KEYS = ("mcp:access", "mcp:settings:read")


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def upgrade() -> None:
    tools = ", ".join(f"'{name}'" for name in TOOL_NAMES)
    _run(
        f"""CREATE TABLE shifde.mcp_client_settings (
            tenant_id uuid PRIMARY KEY REFERENCES shifde.tenants (id),
            enabled boolean NOT NULL DEFAULT false,
            rate_per_minute int NOT NULL DEFAULT 60
                CONSTRAINT ck_mcp_client_settings__rate CHECK (rate_per_minute BETWEEN 1 AND 600),
            max_cost_per_call numeric(12, 4) NOT NULL DEFAULT 1.00
                CONSTRAINT ck_mcp_client_settings__max_cost
                CHECK (max_cost_per_call > 0 AND max_cost_per_call <= 25),
            allowed_tools text[]
                CONSTRAINT ck_mcp_client_settings__allowed_tools
                CHECK (allowed_tools <@ ARRAY[{tools}]::text[]),
            updated_by uuid,
            updated_at timestamptz
        )""",
        "ALTER TABLE shifde.mcp_client_settings ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.mcp_client_settings FORCE ROW LEVEL SECURITY",
        "CREATE POLICY tenant_isolation ON shifde.mcp_client_settings FOR ALL "
        f"USING ({TENANT}) WITH CHECK ({TENANT})",
        "GRANT SELECT, INSERT ON shifde.mcp_client_settings TO shifde_app",
        "GRANT UPDATE (enabled, rate_per_minute, max_cost_per_call, allowed_tools, updated_by, "
        "updated_at) ON shifde.mcp_client_settings TO shifde_app",
    )
    for key, resource_type, action, description, privileged in PERMISSIONS:
        _run(
            "INSERT INTO shifde.permissions (key, resource_type, action, description, "
            f"is_privileged) VALUES ('{key}', '{resource_type}', '{action}', '{description}', "
            f"{_sql_bool(privileged)})"
        )
    grants = [(TENANT_ADMINISTRATOR, key) for key, *_ in PERMISSIONS]
    grants += [(DEVELOPER, key) for key in DEVELOPER_KEYS]
    for role_id, key in grants:
        _run(
            "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
            f"VALUES ('{role_id}', '{key}', NULL)"
        )


def downgrade() -> None:
    _run(
        "DELETE FROM shifde.role_permissions WHERE starts_with(permission_key, 'mcp:')",
        "DELETE FROM shifde.permissions WHERE starts_with(key, 'mcp:')",
        "DROP TABLE shifde.mcp_client_settings",
    )
