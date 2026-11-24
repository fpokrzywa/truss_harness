"""Assist, part 1: the organization's assist settings, the per-project specialists, the requests and
proposals tables and the assist permission keys.

Revision ID: assist_0001
Revises: base_0001
Depends on: appgen_0001 alone. The tables reference authz_0002 (`permissions`, `roles`,
`role_permissions`), identity_0007 (`users`), tenancy_0007 (`tenants`, `workspaces`, `projects`),
build_0002 (`artifacts`, `artifact_versions`), runtime_0007 and models_admin_0004; every one of
them is an ancestor of appgen_0001, and naming an ancestor of another named revision makes this
revision an alembic merge that breaks `alembic upgrade heads` from an empty database (see
delivery_0001 and mcp_0001).

SP7 design sections 4, 5.5, 6 and 8 (issue #278).

* `assist_settings`: one row per organization, keyed by `tenant_id`. `assist_enabled` (default
  false) is the organization switch; `catalog_version` the catalog version pinned when assist was
  enabled (NULL until then); `model_routing_enabled` (default false) and `router_model_id` (no
  key: a shared SHI model has no client) the model fallback of routing; `specialist_max_cost`
  (above 0, default 1.0000) the cost cap of a specialist run. A missing row reads as the
  defaults.
* `assist_specialists`: one row per (project, catalog key): the project-level agent install
  built (`agent_id`, `agent_version_id`, both NULL while `installing`, both set otherwise), the
  catalog version it was built from, the ordered `install_progress` steps and the status
  (`installing`, `enabled`, `disabled`).
* `assist_requests`: a person's request in one project, its routing state (`queued`,
  `classifying`, `handing_over`, `routed`, `refused`, `failed`) and outcome; `job_id` and
  `run_id` have no key (the runtime reaps jobs; retention deletes runs).
* `assist_proposals`: what a specialist run proposed (`appgen_request`, `evaluation_run`,
  `delivery_plan`), one per (run, step), its payload (at most 64 KiB) and the person's decision.
* `shifde.assist_retention_delete_guard()`: a SECURITY INVOKER `BEFORE DELETE ... FOR EACH ROW`
  trigger function on `assist_requests` and `assist_proposals`, `SET search_path = pg_catalog,
  pg_temp`, every object schema-qualified, EXECUTE revoked from PUBLIC. It fires for every role,
  the schema owner included, and raises 42501 (insufficient_privilege) unless the
  transaction-local setting `shifde.retention_delete_cutoff` is set, not empty and a timestamptz,
  and the row's `created_at` is before it (Call S7-J).

Isolation: the four tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); every key
between them and to `workspaces`, `projects`, `artifacts` and `artifact_versions` is composite
`(tenant_id, ...)`. Grants to `shifde_app`: SELECT and INSERT on all four; UPDATE of
settings (assist_enabled, catalog_version, model_routing_enabled, router_model_id,
specialist_max_cost, updated_by, updated_at), specialists (agent_id, agent_version_id,
catalog_version, install_progress, status, updated_at; `agent_id` too because the row exists
before its agent), requests (status, status_reason, specialist_key, route_method, rule_id,
router_version, job_id, run_id, text, context, updated_at) and proposals (status, status_reason,
result_ref, decided_by, decided_at, updated_at) only; DELETE on requests and proposals only,
behind the guard. No function runs with its owner's rights and no table is global.

Permissions: `assist:request:read`, `assist:request:write`, `assist:proposal:accept` and
`assist:install` (project scope) and `assist:admin` (tenant scope); `assist:install` and
`assist:admin` are privileged. `tenant_administrator` holds all five; `workspace_administrator`
and `project_administrator` all but `assist:admin`; `developer` request read and write and
proposal accept; `viewer`, `operator` and `auditor` request read; `platform_administrator` none.

Retention: `shifde_app` deletes requests and proposals only under the guard; settings and
specialists are never deleted by the app.

Rollback: removes every `assist:*` role grant (custom roles included) and permission, drops the
two guard triggers, the guard and the four tables (data-destructive: every organization's assist
settings, specialists, requests and proposals are lost; the installed agents stay in `build`).
"""

from __future__ import annotations

from alembic import op

revision = "assist_0001"
down_revision = "base_0001"
branch_labels = ("assist",)
depends_on = ("appgen_0001",)  # authz, identity, tenancy, build, runtime, models_admin through it

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000001"
WORKSPACE_ADMINISTRATOR = "01900000-0000-7000-8000-000000000002"
PROJECT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000003"
DEVELOPER = "01900000-0000-7000-8000-000000000004"
VIEWER = "01900000-0000-7000-8000-000000000005"
OPERATOR = "01900000-0000-7000-8000-000000000006"
AUDITOR = "01900000-0000-7000-8000-000000000007"
KEYS = "'research', 'solution_design', 'build', 'evaluation', 'cost', 'deployment'"
GUARD_SIGNATURE = "assist_retention_delete_guard()"
GUARDED = ("assist_requests", "assist_proposals")
UPDATE_COLUMNS = {
    "assist_settings": (
        "assist_enabled, catalog_version, model_routing_enabled, router_model_id, "
        "specialist_max_cost, updated_by, updated_at"
    ),
    "assist_specialists": (
        "agent_id, agent_version_id, catalog_version, install_progress, status, updated_at"
    ),
    "assist_requests": (
        "status, status_reason, specialist_key, route_method, rule_id, router_version, job_id, "
        "run_id, text, context, updated_at"
    ),
    "assist_proposals": "status, status_reason, result_ref, decided_by, decided_at, updated_at",
}

# key, resource type, action, description, is_privileged
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    ("assist:request:read", "assist_request", "read", "View assist requests", False),
    ("assist:request:write", "assist_request", "write", "Send assist requests", False),
    ("assist:proposal:accept", "assist_proposal", "accept", "Accept assist proposals", False),
    ("assist:install", "assist_specialist", "install", "Install assist specialists", True),
    ("assist:admin", "assist_settings", "admin", "Administer assist", True),
]
ALL_BUT_ADMIN = (
    "assist:request:read",
    "assist:request:write",
    "assist:proposal:accept",
    "assist:install",
)
GRANTS: dict[str, tuple[str, ...]] = {
    TENANT_ADMINISTRATOR: tuple(key for key, *_ in PERMISSIONS),
    WORKSPACE_ADMINISTRATOR: ALL_BUT_ADMIN,
    PROJECT_ADMINISTRATOR: ALL_BUT_ADMIN,
    DEVELOPER: ("assist:request:read", "assist:request:write", "assist:proposal:accept"),
    VIEWER: ("assist:request:read",),
    OPERATOR: ("assist:request:read",),
    AUDITOR: ("assist:request:read",),
}

GUARD = """CREATE FUNCTION shifde.assist_retention_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
$$ DECLARE
  v_setting text := pg_catalog.current_setting('shifde.retention_delete_cutoff', true);
  v_cutoff timestamptz;
BEGIN
  IF v_setting IS NULL OR v_setting = '' THEN
    RAISE EXCEPTION 'assist rows are deleted only by the retention delete'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  BEGIN
    v_cutoff := v_setting::timestamptz;
  EXCEPTION WHEN data_exception THEN
    RAISE EXCEPTION 'the retention delete cutoff is not a timestamp'
      USING ERRCODE = 'insufficient_privilege';
  END;
  IF OLD.created_at IS NULL OR OLD.created_at >= v_cutoff THEN
    RAISE EXCEPTION 'only assist rows created before the retention cutoff can be deleted'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN OLD;
END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} FOR ALL USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
        f"GRANT SELECT, INSERT ON shifde.{table} TO shifde_app",
        f"GRANT UPDATE ({UPDATE_COLUMNS[table]}) ON shifde.{table} TO shifde_app",
    ]


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ settings
        """CREATE TABLE shifde.assist_settings (
            tenant_id uuid PRIMARY KEY REFERENCES shifde.tenants (id),
            assist_enabled boolean NOT NULL DEFAULT false,
            catalog_version text,
            model_routing_enabled boolean NOT NULL DEFAULT false,
            router_model_id uuid,
            specialist_max_cost numeric(12, 4) NOT NULL DEFAULT 1.0000
                CONSTRAINT ck_assist_settings__max_cost CHECK (specialist_max_cost > 0),
            updated_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz
        )""",
        *_tenant_table("assist_settings"),
        # ------------------------------------------------------------------ specialists
        f"""CREATE TABLE shifde.assist_specialists (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid NOT NULL,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            key text NOT NULL CONSTRAINT ck_assist_specialists__key CHECK (key IN ({KEYS})),
            agent_id uuid,
            agent_version_id uuid,
            catalog_version text NOT NULL,
            install_progress jsonb NOT NULL DEFAULT '[]'::jsonb,
            status text NOT NULL DEFAULT 'installing'
                CONSTRAINT ck_assist_specialists__status
                CHECK (status IN ('installing', 'enabled', 'disabled')),
            installed_by uuid NOT NULL REFERENCES shifde.users (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_assist_specialists PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_assist_specialists__key UNIQUE (tenant_id, project_id, key),
            CONSTRAINT ck_assist_specialists__install_progress
                CHECK (jsonb_typeof(install_progress) = 'array'),
            CONSTRAINT ck_assist_specialists__agent CHECK (
                status = 'installing' OR (agent_id IS NOT NULL AND agent_version_id IS NOT NULL)),
            CONSTRAINT fk_assist_specialists__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_assist_specialists__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_assist_specialists__agent FOREIGN KEY (tenant_id, agent_id)
                REFERENCES shifde.artifacts (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_assist_specialists__agent_version
                FOREIGN KEY (tenant_id, agent_version_id, agent_id)
                REFERENCES shifde.artifact_versions (tenant_id, id, artifact_id)
                ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_assist_specialists__agent ON shifde.assist_specialists "
        "(tenant_id, agent_id)",
        "CREATE INDEX ix_assist_specialists__agent_version ON shifde.assist_specialists "
        "(tenant_id, agent_version_id, agent_id)",
        *_tenant_table("assist_specialists"),
        # ------------------------------------------------------------------ requests
        f"""CREATE TABLE shifde.assist_requests (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid NOT NULL,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            requested_by uuid NOT NULL REFERENCES shifde.users (id),
            text text NOT NULL CONSTRAINT ck_assist_requests__text
                CHECK (char_length(text) BETWEEN 0 AND 4000),
            requested_specialist text,
            context jsonb,
            status text NOT NULL CONSTRAINT ck_assist_requests__status CHECK (status IN (
                'queued', 'classifying', 'handing_over', 'routed', 'refused', 'failed')),
            status_reason text,
            specialist_key text CONSTRAINT ck_assist_requests__specialist_key
                CHECK (specialist_key IN ({KEYS})),
            route_method text CONSTRAINT ck_assist_requests__route_method
                CHECK (route_method IN ('explicit', 'rule', 'model')),
            rule_id text,
            router_version text,
            job_id uuid,
            run_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_assist_requests PRIMARY KEY (tenant_id, id),
            CONSTRAINT fk_assist_requests__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_assist_requests__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_assist_requests__project ON shifde.assist_requests "
        "(tenant_id, project_id, id DESC)",
        *_tenant_table("assist_requests"),
        # ------------------------------------------------------------------ proposals
        """CREATE TABLE shifde.assist_proposals (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid NOT NULL,
            request_id uuid NOT NULL,
            run_id uuid NOT NULL,
            step_key text NOT NULL,
            kind text NOT NULL CONSTRAINT ck_assist_proposals__kind
                CHECK (kind IN ('appgen_request', 'evaluation_run', 'delivery_plan')),
            payload jsonb NOT NULL,
            status text NOT NULL DEFAULT 'open' CONSTRAINT ck_assist_proposals__status
                CHECK (status IN ('open', 'accepted', 'dismissed', 'failed')),
            status_reason text,
            result_ref jsonb,
            decided_by uuid,
            decided_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_assist_proposals PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_assist_proposals__step UNIQUE (tenant_id, run_id, step_key),
            CONSTRAINT ck_assist_proposals__payload_size
                CHECK (octet_length(payload::text) <= 65536),
            CONSTRAINT fk_assist_proposals__request FOREIGN KEY (tenant_id, request_id)
                REFERENCES shifde.assist_requests (tenant_id, id) ON DELETE CASCADE
        )""",
        "CREATE INDEX ix_assist_proposals__request ON shifde.assist_proposals "
        "(tenant_id, request_id)",
        *_tenant_table("assist_proposals"),
        # ------------------------------------------------------------------ retention
        GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{GUARD_SIGNATURE} FROM PUBLIC",
        *[
            f"CREATE TRIGGER trg_{table}_retention_delete BEFORE DELETE ON shifde.{table} "
            "FOR EACH ROW EXECUTE FUNCTION shifde.assist_retention_delete_guard()"
            for table in GUARDED
        ],
        *[f"GRANT DELETE ON shifde.{table} TO shifde_app" for table in GUARDED],
    )
    for key, resource_type, action, description, privileged in PERMISSIONS:
        _run(
            "INSERT INTO shifde.permissions (key, resource_type, action, description, "
            f"is_privileged) VALUES ('{key}', '{resource_type}', '{action}', '{description}', "
            f"{_sql_bool(privileged)})"
        )
    for role_id, keys in GRANTS.items():
        for key in keys:
            _run(
                "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
                f"VALUES ('{role_id}', '{key}', NULL)"
            )


def downgrade() -> None:
    _run(
        "DELETE FROM shifde.role_permissions WHERE starts_with(permission_key, 'assist:')",
        "DELETE FROM shifde.permissions WHERE starts_with(key, 'assist:')",
        *[f"DROP TRIGGER trg_{table}_retention_delete ON shifde.{table}" for table in GUARDED],
        "DROP TABLE shifde.assist_proposals",
        "DROP TABLE shifde.assist_requests",
        "DROP TABLE shifde.assist_specialists",
        "DROP TABLE shifde.assist_settings",
        f"DROP FUNCTION shifde.{GUARD_SIGNATURE}",
    )
