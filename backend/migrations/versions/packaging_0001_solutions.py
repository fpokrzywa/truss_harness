"""Packaging, part 1: solutions, solution versions, their guard and the packaging and evaluation
permission keys.

Revision ID: packaging_0001
Revises: base_0001
Depends on: build_0002 (`artifact_versions`), models_admin_0003 and tenancy_0006 (`tenants`,
`workspaces`, `projects`); connections_0001 is an ancestor of build_0002 and models_admin_0003, and
authz_0002 (`permissions`, `role_permissions`) reaches tenancy_0006 through identity_0005, so
neither is named (a named ancestor of another named revision breaks `alembic upgrade heads`, see
delivery_0001).

Sub-project 3 design sections 3, 4 and 5, settled calls 7 to 12 and 31 (DECISIONS B-2701).

* `solutions`: a deliverable of one project (workspace and project required), named uniquely in
  its project (`uq_solutions__project_name`), with one target kind (`databricks.app/v1`) and a
  status (`active` / `archived`).
* `solution_versions`: an immutable, numbered version of a solution (`version_no` unique per
  solution). It names one published agent or pipeline version as its entrypoint through the
  composite key `fk_solution_versions__entrypoint` to `artifact_versions (tenant_id, id)`,
  `ON DELETE RESTRICT`, and stores its spec (data reads, reserved screens, evaluation contract;
  at most 256 KiB) with the spec's sha256 `spec_hash`.
* `shifde.solution_versions_guard()`: a SECURITY INVOKER `BEFORE INSERT OR UPDATE OR DELETE`
  trigger, `SET search_path = pg_catalog, pg_temp`, every object schema-qualified, EXECUTE revoked
  from PUBLIC. An insert is refused (`check_violation`) unless the entrypoint version, read under
  the caller's row-level security, is `published` and of the kind the row names (`agents` for
  `agent`, `pipelines` for `pipeline`); every update and delete is refused (`check_violation`),
  for the schema owner too.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); every key
between them and to `workspaces`, `projects` and `artifact_versions` is composite
`(tenant_id, ...)`. Grants to `shifde_app`: solutions SELECT, INSERT and UPDATE (status) only;
solution versions SELECT and INSERT only. No SECURITY DEFINER function.

Permissions: eight keys, `packaging:solution:read|write`, `packaging:release:seal|withdraw`,
`evaluation:dataset:read|write` and `evaluation:run:read|request`; only
`packaging:release:withdraw` is privileged. `tenant_administrator` holds all eight, `developer`
all but `packaging:release:withdraw` (fifteen template grants); no other role.

Rollback: removes every `packaging:*` and `evaluation:*` role grant (custom roles included) and
permission, drops both tables and the guard function (data-destructive: every organization's
solutions and solution versions are lost).
"""

from __future__ import annotations

from alembic import op

revision = "packaging_0001"
down_revision = "base_0001"
branch_labels = ("packaging",)
depends_on = ("build_0002", "models_admin_0003", "tenancy_0006")

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000001"
DEVELOPER = "01900000-0000-7000-8000-000000000004"
GUARD_SIGNATURE = "solution_versions_guard()"

# key, resource type, action, description, is_privileged
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    ("packaging:solution:read", "packaging_solution", "read", "View solutions", False),
    (
        "packaging:solution:write",
        "packaging_solution",
        "write",
        "Create and archive solutions and their versions",
        False,
    ),
    ("packaging:release:seal", "packaging_release", "seal", "Seal releases", False),
    ("packaging:release:withdraw", "packaging_release", "withdraw", "Withdraw releases", True),
    ("evaluation:dataset:read", "evaluation_dataset", "read", "View evaluation datasets", False),
    (
        "evaluation:dataset:write",
        "evaluation_dataset",
        "write",
        "Create evaluation datasets",
        False,
    ),
    ("evaluation:run:read", "evaluation_run", "read", "View evaluation runs", False),
    ("evaluation:run:request", "evaluation_run", "request", "Request evaluation runs", False),
]
DEVELOPER_KEYS = tuple(key for key, *_ in PERMISSIONS if key != "packaging:release:withdraw")

GUARD = """CREATE FUNCTION shifde.solution_versions_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE
        v_entry record;
        v_kind text;
      BEGIN
        IF TG_OP <> 'INSERT' THEN
          RAISE EXCEPTION 'solution versions are immutable' USING ERRCODE = 'check_violation';
        END IF;
        v_kind := CASE NEW.entrypoint_kind WHEN 'agent' THEN 'agents'
                                           WHEN 'pipeline' THEN 'pipelines' END;
        SELECT v.kind, v.state INTO v_entry FROM shifde.artifact_versions v
         WHERE v.tenant_id = NEW.tenant_id AND v.id = NEW.entrypoint_version_id;
        IF NOT FOUND OR v_entry.state <> 'published' OR v_entry.kind IS DISTINCT FROM v_kind THEN
          RAISE EXCEPTION 'the entrypoint must be a published version of its kind'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str, *, updated_at: bool = True) -> list[str]:
    trigger = (
        [
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
            "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()"
        ]
        if updated_at
        else []
    )
    return [
        *trigger,
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} FOR ALL USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
    ]


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ solutions
        """CREATE TABLE shifde.solutions (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            target_kind text NOT NULL CHECK (target_kind = 'databricks.app/v1'),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_solutions__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_solutions__project_name UNIQUE (tenant_id, project_id, name),
            CONSTRAINT fk_solutions__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_solutions__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id)
        )""",
        *_tenant_table("solutions"),
        "GRANT SELECT, INSERT ON shifde.solutions TO shifde_app",
        "GRANT UPDATE (status) ON shifde.solutions TO shifde_app",
        # ------------------------------------------------------------------ solution versions
        """CREATE TABLE shifde.solution_versions (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            solution_id uuid NOT NULL,
            version_no int NOT NULL CHECK (version_no > 0),
            entrypoint_kind text NOT NULL CHECK (entrypoint_kind IN ('agent', 'pipeline')),
            entrypoint_version_id uuid NOT NULL,
            spec jsonb NOT NULL,
            spec_hash text NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_solution_versions__size CHECK (octet_length(spec::text) <= 262144),
            CONSTRAINT uq_solution_versions__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_solution_versions__no UNIQUE (tenant_id, solution_id, version_no),
            CONSTRAINT fk_solution_versions__solution FOREIGN KEY (tenant_id, solution_id)
                REFERENCES shifde.solutions (tenant_id, id),
            CONSTRAINT fk_solution_versions__entrypoint
                FOREIGN KEY (tenant_id, entrypoint_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_solution_versions__entrypoint ON shifde.solution_versions "
        "(tenant_id, entrypoint_version_id)",
        GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_solution_versions_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON shifde.solution_versions FOR EACH ROW "
        "EXECUTE FUNCTION shifde.solution_versions_guard()",
        *_tenant_table("solution_versions", updated_at=False),
        "GRANT SELECT, INSERT ON shifde.solution_versions TO shifde_app",
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
        "DELETE FROM shifde.role_permissions WHERE starts_with(permission_key, 'packaging:') "
        "OR starts_with(permission_key, 'evaluation:')",
        "DELETE FROM shifde.permissions WHERE starts_with(key, 'packaging:') "
        "OR starts_with(key, 'evaluation:')",
        "DROP TABLE shifde.solution_versions",
        f"DROP FUNCTION shifde.{GUARD_SIGNATURE}",
        "DROP TABLE shifde.solutions",
    )
