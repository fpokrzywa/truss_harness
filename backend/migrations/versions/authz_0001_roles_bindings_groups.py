"""Authz: permission catalog, roles, role bindings, groups, group members, policy decisions.

Revision ID: authz_0001
Revises: base_0001

SETH 3ba0e06's final schema for the authz tables, squashed from `p01_0001_tenancy_core`,
`p01_0002_hardening`, `p02_0001_identity_authz`, `p02_0003_identity_hardening` (groups keyed by
IdP), `p08_0001_governance` (`policy_decisions.policy_ids`, run index) and the catalog additions of
`p04_0002`, `p05_0002`, `p06_0002`, `p07_0002`, `p08_0002`. The seed data is SETH's final catalog
(92 permissions, 8 system role templates, 339 template grants), replayed from those migrations.

Isolation (DECISIONS B-401):
* `permissions` is the platform-wide permission catalog (key, resource type, action, description,
  privileged flag): no tenant or principal column, no client content, SELECT-only for the app role,
  written only by migrations. It keeps SETH's `read_all` policy and is listed in
  `tests/schema/global_tables.txt`.
* Every other table carries `tenant_id` and has forced RLS with tenant-keyed policies. `roles` and
  `role_permissions` expose the shared system templates (`tenant_id IS NULL`) only through their
  FOR SELECT policy; writes are tenant-only and never touch a system template.

Foreign keys to tables of later modules are NOT created here (DECISIONS B-402; listed in
`tests/schema/deliberate_changes.yaml` and `docs/build/carry_forward.md`): `tenant_id` ->
`tenants` (tenancy) on roles, role_permissions, role_bindings, groups, policy_decisions;
`groups.idp_config_id` -> `idp_configs` (identity); `group_members (tenant_id, user_id)` ->
`memberships` (identity). The integrity trigger function `check_role_binding()` is SETH's verbatim:
it reads `memberships`, `service_accounts`, `workspaces` and `projects`, so until identity and
tenancy create them a binding to a user or service account, or at workspace or project scope,
fails closed ("relation does not exist").

Rollback: drops every table and function created here (data-destructive: custom roles, bindings,
groups and the decision log are lost).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "authz_0001"
down_revision = "base_0001"
branch_labels = ("authz",)
depends_on = None

SLUG_RE = "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"

# (key, resource_type, action, description, is_privileged): SETH 3ba0e06's final catalog
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    (
        "tenant:admin",
        "tenant",
        "admin",
        "Administer the organization (settings, status, roles)",
        True,
    ),
    ("member:read", "member", "read", "List organization members", False),
    ("workspace:read", "workspace", "read", "View a workspace", False),
    ("workspace:create", "workspace", "create", "Create workspaces", False),
    ("workspace:manage", "workspace", "manage", "Update or archive a workspace", False),
    ("project:read", "project", "read", "View a project", False),
    ("project:create", "project", "create", "Create projects", False),
    ("project:manage", "project", "manage", "Update or archive a project", False),
    ("platform:admin", "platform", "admin", "Administer the installation (platform-only)", True),
    ("member:manage", "member", "manage", "Add, suspend and reactivate organization members", True),
    ("group:read", "group", "read", "View groups and their members", False),
    ("group:manage", "group", "manage", "Create groups and change membership", True),
    ("role:read", "role", "read", "View roles and role bindings", False),
    ("role:manage", "role", "manage", "Create custom roles and manage role bindings", True),
    ("serviceaccount:read", "serviceaccount", "read", "View service accounts", False),
    ("serviceaccount:manage", "serviceaccount", "manage", "Create, disable service accounts", True),
    ("apikey:read", "apikey", "read", "View API key metadata", False),
    ("apikey:manage", "apikey", "manage", "Create, rotate and revoke API keys", True),
    ("secret:read", "secret", "read", "View secret metadata (never values)", False),
    ("secret:manage", "secret", "manage", "Create, rotate and disable secrets", True),
    ("secret:use", "secret", "use", "Resolve secrets server-side for connections and runs", False),
    ("idp:manage", "idp", "manage", "Configure identity providers", True),
    ("audit:read", "audit", "read", "Read audit events and verify the chain", False),
    ("vendor:read", "vendor", "read", "View the vendor catalog", False),
    ("vendor:manage", "vendor", "manage", "Manage vendor catalog entries", False),
    ("model:read", "model", "read", "View model providers and models", False),
    ("model:manage", "model", "manage", "Manage model providers, models and routing", False),
    ("model:invoke", "model", "invoke", "Invoke models", False),
    ("connection:read", "connection", "read", "View connections", False),
    ("connection:manage", "connection", "manage", "Create and update connections", False),
    ("connection:test", "connection", "test", "Test connections", False),
    (
        "connection:use",
        "connection",
        "use",
        "Use connections from agents, tools and pipelines",
        False,
    ),
    ("connection:write", "connection", "write", "Write data through a connection", False),
    ("egress:manage", "egress", "manage", "Widen or restrict egress policies", True),
    ("adapter:verify", "adapter", "verify", "Mark adapter capabilities as verified", True),
    ("agent:read", "agent", "read", "View agents", False),
    ("agent:create", "agent", "create", "Create agents", False),
    ("agent:update", "agent", "update", "Edit agent drafts", False),
    ("agent:publish", "agent", "publish", "Publish agent versions", False),
    ("agent:execute", "agent", "execute", "Execute agents", False),
    ("agent:test", "agent", "test", "Test agents", False),
    ("tool:read", "tool", "read", "View tools", False),
    ("tool:invoke", "tool", "invoke", "Invoke tools", False),
    ("tool:manage", "tool", "manage", "Manage tools", False),
    ("prompt:read", "prompt", "read", "View prompts", False),
    ("prompt:create", "prompt", "create", "Create prompts", False),
    ("prompt:update", "prompt", "update", "Edit prompt drafts", False),
    ("prompt:publish", "prompt", "publish", "Publish prompt versions", False),
    ("prompt:test", "prompt", "test", "Test prompts", False),
    ("pipeline:read", "pipeline", "read", "View pipelines", False),
    ("pipeline:create", "pipeline", "create", "Create pipelines", False),
    ("pipeline:update", "pipeline", "update", "Edit pipeline drafts", False),
    ("pipeline:publish", "pipeline", "publish", "Publish pipeline versions", False),
    ("pipeline:execute", "pipeline", "execute", "Execute pipelines", False),
    ("ontology:read", "ontology", "read", "View ontologies", False),
    ("ontology:query", "ontology", "query", "Query ontologies", False),
    ("ontology:manage", "ontology", "manage", "Manage ontologies", False),
    ("data:export", "data", "export", "Export data", False),
    ("approval:decide", "approval", "decide", "Decide approvals", False),
    ("usage:read", "usage", "read", "View usage and cost", False),
    ("budget:manage", "budget", "manage", "Manage budgets and quotas", False),
    ("skill:read", "skill", "read", "View skills", False),
    ("skill:create", "skill", "create", "Create skills", False),
    ("skill:update", "skill", "update", "Edit skill drafts", False),
    ("skill:publish", "skill", "publish", "Publish skill versions", False),
    ("run:read", "run", "read", "View runs", False),
    ("run:cancel", "run", "cancel", "Cancel runs", False),
    ("tool:create", "tool", "create", "Create tools", False),
    ("tool:update", "tool", "update", "Edit tool drafts", False),
    ("tool:publish", "tool", "publish", "Publish tool versions", False),
    ("tool:test", "tool", "test", "Test tools", False),
    (
        "tool:test_side_effects",
        "tool",
        "test_side_effects",
        "Test tools that change external state",
        True,
    ),
    ("agent:activate", "agent", "activate", "Activate and deactivate agents", False),
    ("approval:read", "approval", "read", "View approvals and their exact arguments", False),
    ("orchestration:read", "orchestration", "read", "View orchestrations", False),
    ("orchestration:create", "orchestration", "create", "Create orchestrations", False),
    ("orchestration:update", "orchestration", "update", "Edit orchestration drafts", False),
    ("orchestration:publish", "orchestration", "publish", "Publish orchestration versions", False),
    ("orchestration:test", "orchestration", "test", "Test orchestrations", False),
    ("orchestration:execute", "orchestration", "execute", "Execute orchestrations", False),
    ("pipeline:test", "pipeline", "test", "Test pipelines with draft test runs", False),
    ("ontology:create", "ontology", "create", "Create ontologies", False),
    ("ontology:update", "ontology", "update", "Edit ontology drafts and source mappings", False),
    ("ontology:publish", "ontology", "publish", "Publish ontology versions", False),
    (
        "ontology:manage_policies",
        "ontology",
        "manage_policies",
        "Manage ontology access policies",
        True,
    ),
    ("pricing:read", "pricing", "read", "View rate cards", False),
    ("pricing:manage", "pricing", "manage", "Create rate cards and rate card versions", True),
    ("usage:manage", "usage", "manage", "Rebuild usage rollups", True),
    ("budget:read", "budget", "read", "View budgets and quotas", False),
    (
        "governance:read",
        "governance",
        "read",
        "View governance policies, decisions and retention",
        False,
    ),
    ("governance:manage", "governance", "manage", "Manage governance policies and retention", True),
    ("ops:read", "ops", "read", "View operational performance and platform health", False),
]

# (id, key, name, description): the system role templates (tenant_id NULL, is_system)
SYSTEM_ROLES: list[tuple[str, str, str, str]] = [
    (
        "01900000-0000-7000-8000-000000000001",
        "tenant_administrator",
        "Tenant Administrator",
        "Full administration of one organization",
    ),
    (
        "01900000-0000-7000-8000-000000000002",
        "workspace_administrator",
        "Workspace Administrator",
        "Administers a workspace and its projects",
    ),
    (
        "01900000-0000-7000-8000-000000000003",
        "project_administrator",
        "Project Administrator",
        "Administers a single project",
    ),
    (
        "01900000-0000-7000-8000-000000000004",
        "developer",
        "Developer",
        "Reads workspaces and creates projects",
    ),
    (
        "01900000-0000-7000-8000-000000000005",
        "viewer",
        "Viewer",
        "Read-only access to workspaces and projects",
    ),
    (
        "01900000-0000-7000-8000-000000000006",
        "operator",
        "Operator",
        "Operates runs and connections; read access plus test/use",
    ),
    (
        "01900000-0000-7000-8000-000000000007",
        "auditor",
        "Auditor",
        "Read-only access including audit events and security metadata",
    ),
    (
        "01900000-0000-7000-8000-000000000008",
        "platform_administrator",
        "Platform Administrator",
        "Installation-wide administration; no tenant data-plane access (platform-only)",
    ),
]

# role id -> permission keys of the system role templates
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "01900000-0000-7000-8000-000000000001": [  # tenant_administrator
        "tenant:admin",
        "member:read",
        "workspace:read",
        "workspace:create",
        "workspace:manage",
        "project:read",
        "project:create",
        "project:manage",
        "member:manage",
        "group:read",
        "group:manage",
        "role:read",
        "role:manage",
        "serviceaccount:read",
        "serviceaccount:manage",
        "apikey:read",
        "apikey:manage",
        "secret:read",
        "secret:manage",
        "secret:use",
        "idp:manage",
        "audit:read",
        "vendor:read",
        "vendor:manage",
        "model:read",
        "model:manage",
        "model:invoke",
        "connection:read",
        "connection:manage",
        "connection:test",
        "connection:use",
        "connection:write",
        "egress:manage",
        "adapter:verify",
        "agent:read",
        "agent:create",
        "agent:update",
        "agent:publish",
        "agent:execute",
        "agent:test",
        "tool:read",
        "tool:invoke",
        "tool:manage",
        "prompt:read",
        "prompt:create",
        "prompt:update",
        "prompt:publish",
        "prompt:test",
        "pipeline:read",
        "pipeline:create",
        "pipeline:update",
        "pipeline:publish",
        "pipeline:execute",
        "ontology:read",
        "ontology:query",
        "ontology:manage",
        "data:export",
        "approval:decide",
        "usage:read",
        "budget:manage",
        "skill:read",
        "skill:create",
        "skill:update",
        "skill:publish",
        "run:read",
        "run:cancel",
        "tool:create",
        "tool:update",
        "tool:publish",
        "tool:test",
        "tool:test_side_effects",
        "agent:activate",
        "approval:read",
        "orchestration:read",
        "orchestration:create",
        "orchestration:update",
        "orchestration:publish",
        "orchestration:test",
        "orchestration:execute",
        "pipeline:test",
        "ontology:create",
        "ontology:update",
        "ontology:publish",
        "ontology:manage_policies",
        "pricing:read",
        "pricing:manage",
        "usage:manage",
        "budget:read",
        "governance:read",
        "governance:manage",
        "ops:read",
    ],
    "01900000-0000-7000-8000-000000000002": [  # workspace_administrator
        "member:read",
        "workspace:read",
        "workspace:manage",
        "project:read",
        "project:create",
        "project:manage",
        "group:read",
        "role:read",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "agent:create",
        "agent:update",
        "agent:publish",
        "agent:execute",
        "agent:test",
        "prompt:create",
        "prompt:update",
        "prompt:publish",
        "prompt:test",
        "tool:invoke",
        "pipeline:create",
        "pipeline:update",
        "pipeline:publish",
        "pipeline:execute",
        "skill:create",
        "skill:update",
        "skill:publish",
        "ontology:query",
        "tool:manage",
        "ontology:manage",
        "model:invoke",
        "connection:manage",
        "connection:test",
        "connection:use",
        "connection:write",
        "secret:use",
        "run:cancel",
        "approval:decide",
        "tool:create",
        "tool:update",
        "tool:publish",
        "tool:test",
        "tool:test_side_effects",
        "agent:activate",
        "approval:read",
        "orchestration:read",
        "orchestration:create",
        "orchestration:update",
        "orchestration:publish",
        "orchestration:test",
        "orchestration:execute",
        "pipeline:test",
        "ontology:create",
        "ontology:update",
        "ontology:publish",
        "ontology:manage_policies",
        "pricing:read",
        "budget:read",
        "governance:read",
        "ops:read",
    ],
    "01900000-0000-7000-8000-000000000003": [  # project_administrator
        "project:read",
        "project:manage",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "agent:create",
        "agent:update",
        "agent:publish",
        "agent:execute",
        "agent:test",
        "prompt:create",
        "prompt:update",
        "prompt:publish",
        "prompt:test",
        "tool:invoke",
        "pipeline:create",
        "pipeline:update",
        "pipeline:publish",
        "pipeline:execute",
        "skill:create",
        "skill:update",
        "skill:publish",
        "ontology:query",
        "tool:manage",
        "ontology:manage",
        "model:invoke",
        "connection:manage",
        "connection:test",
        "connection:use",
        "connection:write",
        "secret:use",
        "run:cancel",
        "approval:decide",
        "tool:create",
        "tool:update",
        "tool:publish",
        "tool:test",
        "tool:test_side_effects",
        "agent:activate",
        "approval:read",
        "orchestration:read",
        "orchestration:create",
        "orchestration:update",
        "orchestration:publish",
        "orchestration:test",
        "orchestration:execute",
        "pipeline:test",
        "ontology:create",
        "ontology:update",
        "ontology:publish",
        "ontology:manage_policies",
        "pricing:read",
        "budget:read",
        "governance:read",
        "ops:read",
    ],
    "01900000-0000-7000-8000-000000000004": [  # developer
        "workspace:read",
        "project:read",
        "project:create",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "agent:create",
        "agent:update",
        "agent:publish",
        "agent:execute",
        "agent:test",
        "prompt:create",
        "prompt:update",
        "prompt:publish",
        "prompt:test",
        "tool:invoke",
        "pipeline:create",
        "pipeline:update",
        "pipeline:publish",
        "pipeline:execute",
        "skill:create",
        "skill:update",
        "skill:publish",
        "ontology:query",
        "model:invoke",
        "connection:use",
        "connection:test",
        "tool:create",
        "tool:update",
        "tool:publish",
        "tool:test",
        "approval:read",
        "orchestration:read",
        "orchestration:create",
        "orchestration:update",
        "orchestration:publish",
        "orchestration:test",
        "orchestration:execute",
        "pipeline:test",
        "ontology:create",
        "ontology:update",
        "ontology:publish",
    ],
    "01900000-0000-7000-8000-000000000005": [  # viewer
        "workspace:read",
        "project:read",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "orchestration:read",
    ],
    "01900000-0000-7000-8000-000000000006": [  # operator
        "workspace:read",
        "project:read",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "connection:test",
        "connection:use",
        "run:cancel",
        "agent:execute",
        "pipeline:execute",
        "approval:decide",
        "approval:read",
        "orchestration:read",
        "orchestration:execute",
        "pricing:read",
        "budget:read",
        "ops:read",
    ],
    "01900000-0000-7000-8000-000000000007": [  # auditor
        "workspace:read",
        "project:read",
        "agent:read",
        "prompt:read",
        "tool:read",
        "skill:read",
        "pipeline:read",
        "ontology:read",
        "vendor:read",
        "model:read",
        "connection:read",
        "run:read",
        "usage:read",
        "member:read",
        "group:read",
        "role:read",
        "serviceaccount:read",
        "apikey:read",
        "secret:read",
        "audit:read",
        "approval:read",
        "orchestration:read",
        "pricing:read",
        "budget:read",
        "governance:read",
        "ops:read",
    ],
    "01900000-0000-7000-8000-000000000008": [  # platform_administrator
        "platform:admin",
    ],
}


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
    ]


def upgrade() -> None:
    # ------------------------------------------------------------------ catalog and roles
    _run(
        """CREATE TABLE shifde.permissions (
            key text PRIMARY KEY,
            resource_type text NOT NULL,
            action text NOT NULL,
            description text NOT NULL,
            is_privileged boolean NOT NULL DEFAULT false
        )""",
        """CREATE TABLE shifde.roles (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            key text NOT NULL,
            name text NOT NULL,
            description text,
            is_system boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'retired')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT ck_roles__system_has_no_tenant CHECK (NOT is_system OR tenant_id IS NULL)
        )""",
        "CREATE UNIQUE INDEX uq_roles__tenant_id_key ON shifde.roles "
        "(COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), key)",
        """CREATE TABLE shifde.role_permissions (
            role_id uuid NOT NULL REFERENCES shifde.roles (id) ON DELETE CASCADE,
            permission_key text NOT NULL REFERENCES shifde.permissions (key),
            tenant_id uuid,
            PRIMARY KEY (role_id, permission_key)
        )""",
        """CREATE TABLE shifde.role_bindings (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            subject_type text NOT NULL CHECK (subject_type IN ('user', 'group', 'service_account')),
            subject_id uuid NOT NULL,
            role_id uuid NOT NULL REFERENCES shifde.roles (id),
            scope_type text NOT NULL CHECK (scope_type IN ('tenant', 'workspace', 'project', 'resource')),
            scope_id uuid NOT NULL,
            resource_type text,
            expires_at timestamptz,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_role_bindings__tenant_scope CHECK (scope_type <> 'tenant' OR scope_id = tenant_id),
            CONSTRAINT uq_role_bindings__grant UNIQUE
                (tenant_id, subject_type, subject_id, role_id, scope_type, scope_id)
        )""",
        "CREATE INDEX ix_role_bindings__subject ON shifde.role_bindings (tenant_id, subject_type, subject_id)",
        "CREATE TRIGGER trg_roles_updated_at BEFORE UPDATE ON shifde.roles "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
    )

    # ------------------------------------------------------------------ groups
    _run(
        f"""CREATE TABLE shifde.groups (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            slug text NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            description text,
            source text NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'idp')),
            external_id text,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            idp_config_id uuid,
            CONSTRAINT uq_groups__tenant_id_slug UNIQUE (tenant_id, slug),
            CONSTRAINT uq_groups__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_groups__slug CHECK (slug ~ '{SLUG_RE}'),
            CONSTRAINT ck_groups__idp_external CHECK (source = 'local' OR external_id IS NOT NULL)
        )""",
        "CREATE UNIQUE INDEX uq_groups__idp_external ON shifde.groups "
        "(tenant_id, idp_config_id, external_id) WHERE source = 'idp'",
        """CREATE TABLE shifde.group_members (
            tenant_id uuid NOT NULL,
            group_id uuid NOT NULL,
            user_id uuid NOT NULL,
            source text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'idp_sync')),
            added_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, user_id),
            CONSTRAINT fk_group_members__group FOREIGN KEY (tenant_id, group_id)
                REFERENCES shifde.groups (tenant_id, id)
        )""",
        "CREATE INDEX ix_group_members__user ON shifde.group_members (tenant_id, user_id)",
        "CREATE TRIGGER trg_groups_updated_at BEFORE UPDATE ON shifde.groups "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
    )

    # ------------------------------------------------------------------ policy decisions
    _run(
        """CREATE TABLE shifde.policy_decisions (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            at timestamptz NOT NULL DEFAULT now(),
            principal_id uuid,
            principal_type text NOT NULL DEFAULT 'user',
            action text NOT NULL,
            resource_type text,
            resource_id text,
            decision text NOT NULL CHECK (decision IN ('allow', 'deny')),
            reason_codes text[] NOT NULL DEFAULT '{}',
            matched_binding_ids uuid[] NOT NULL DEFAULT '{}',
            run_id uuid,
            trace_id text,
            policy_ids uuid[] NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX ix_policy_decisions__tenant_at ON shifde.policy_decisions (tenant_id, at DESC)",
        "CREATE INDEX ix_policy_decisions__run ON shifde.policy_decisions (tenant_id, run_id) "
        "WHERE run_id IS NOT NULL",
        "CREATE TRIGGER trg_policy_decisions_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.policy_decisions FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
    )

    # ------------------------------------------------------------------ integrity triggers
    _run(
        """CREATE FUNCTION shifde.check_role_permission_tenant() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                               AND r.tenant_id IS NOT DISTINCT FROM NEW.tenant_id) THEN
                  RAISE EXCEPTION 'role_permissions.tenant_id must match the role tenant'
                    USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
              END $$""",
        "CREATE TRIGGER trg_role_permissions_tenant BEFORE INSERT ON shifde.role_permissions "
        "FOR EACH ROW EXECUTE FUNCTION shifde.check_role_permission_tenant()",
        """CREATE FUNCTION shifde.check_role_binding() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM shifde.roles r WHERE r.id = NEW.role_id
                               AND (r.tenant_id IS NULL OR r.tenant_id = NEW.tenant_id)) THEN
                  RAISE EXCEPTION 'role is not available to this tenant'
                    USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.subject_type = 'user' AND NOT EXISTS (
                     SELECT 1 FROM shifde.memberships m
                     WHERE m.tenant_id = NEW.tenant_id AND m.user_id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject user is not a member of this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.subject_type = 'group' AND NOT EXISTS (
                     SELECT 1 FROM shifde.groups g
                     WHERE g.tenant_id = NEW.tenant_id AND g.id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject group is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.subject_type = 'service_account' AND NOT EXISTS (
                     SELECT 1 FROM shifde.service_accounts s
                     WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.subject_id) THEN
                  RAISE EXCEPTION 'subject service account is not in this tenant'
                    USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.scope_type = 'workspace' AND NOT EXISTS (
                     SELECT 1 FROM shifde.workspaces w
                     WHERE w.tenant_id = NEW.tenant_id AND w.id = NEW.scope_id) THEN
                  RAISE EXCEPTION 'scope workspace is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.scope_type = 'project' AND NOT EXISTS (
                     SELECT 1 FROM shifde.projects p
                     WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.scope_id) THEN
                  RAISE EXCEPTION 'scope project is not in this tenant'
                    USING ERRCODE = 'check_violation';
                ELSIF NEW.scope_type = 'resource' AND NEW.resource_type IS NULL THEN
                  RAISE EXCEPTION 'resource bindings need a resource_type'
                    USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
              END $$""",
        "CREATE TRIGGER trg_role_bindings_integrity BEFORE INSERT ON shifde.role_bindings "
        "FOR EACH ROW EXECUTE FUNCTION shifde.check_role_binding()",
    )

    # ------------------------------------------------------------------ row-level security
    for table in ("permissions", "roles", "role_permissions"):
        _run(
            f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        )
    _run(
        # permissions: the platform catalog, read-only for every context (global_tables.txt).
        "CREATE POLICY read_all ON shifde.permissions FOR SELECT USING (true)",
        # roles / role_permissions: system templates (tenant_id NULL) readable by all; writes are
        # tenant-only and never touch a system template (managed by migrations).
        "CREATE POLICY read_global_or_own ON shifde.roles FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        "CREATE POLICY tenant_write ON shifde.roles FOR ALL "
        "USING (tenant_id = shifde.current_tenant_id() AND NOT is_system) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id() AND NOT is_system)",
        "CREATE POLICY read_global_or_own ON shifde.role_permissions FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = shifde.current_tenant_id())",
        "CREATE POLICY tenant_write ON shifde.role_permissions FOR ALL "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
        *_tenant_table("role_bindings"),
        *_tenant_table("groups"),
        *_tenant_table("group_members"),
        *_tenant_table("policy_decisions"),
    )

    # ------------------------------------------------------------------ grants (least privilege)
    _run(
        "GRANT SELECT ON shifde.permissions TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.roles TO shifde_app",
        "GRANT UPDATE (name, description, status, row_version) ON shifde.roles TO shifde_app",
        "GRANT SELECT, INSERT, DELETE ON shifde.role_permissions TO shifde_app",
        "GRANT SELECT, INSERT, DELETE ON shifde.role_bindings TO shifde_app",
        "GRANT SELECT, INSERT ON shifde.groups TO shifde_app",
        "GRANT UPDATE (name, description, status, row_version) ON shifde.groups TO shifde_app",
        "GRANT SELECT, INSERT, DELETE ON shifde.group_members TO shifde_app",
        # append-only decision log
        "GRANT SELECT, INSERT ON shifde.policy_decisions TO shifde_app",
    )

    # ------------------------------------------------------------------ seed data
    conn = op.get_bind()
    conn.execute(
        text(
            "INSERT INTO shifde.permissions (key, resource_type, action, description, is_privileged) "
            "VALUES (:key, :rtype, :action, :description, :privileged)"
        ),
        [
            {"key": k, "rtype": r, "action": a, "description": d, "privileged": p}
            for k, r, a, d, p in PERMISSIONS
        ],
    )
    conn.execute(
        text(
            "INSERT INTO shifde.roles (id, tenant_id, key, name, description, is_system) "
            "VALUES (CAST(:id AS uuid), NULL, :key, :name, :description, true)"
        ),
        [{"id": i, "key": k, "name": n, "description": d} for i, k, n, d in SYSTEM_ROLES],
    )
    conn.execute(
        text(
            "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
            "VALUES (CAST(:role_id AS uuid), :key, NULL)"
        ),
        [{"role_id": r, "key": k} for r, keys in ROLE_PERMISSIONS.items() for k in keys],
    )


def downgrade() -> None:
    _run(
        "DROP TABLE IF EXISTS shifde.policy_decisions",
        "DROP TABLE IF EXISTS shifde.group_members",
        "DROP TABLE IF EXISTS shifde.groups",
        "DROP TABLE IF EXISTS shifde.role_bindings",
        "DROP TABLE IF EXISTS shifde.role_permissions",
        "DROP TABLE IF EXISTS shifde.roles",
        "DROP TABLE IF EXISTS shifde.permissions",
        "DROP FUNCTION IF EXISTS shifde.check_role_binding()",
        "DROP FUNCTION IF EXISTS shifde.check_role_permission_tenant()",
    )
