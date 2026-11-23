"""Solution generator, part 1: app requests, their drafts and the appgen permission keys.

Revision ID: appgen_0001
Revises: base_0001
Depends on: packaging_0003 (`solution_versions`), evaluation_0001, connections_0003
(`connections`, `connection_discoveries`), models_admin_0004, runtime_0007 (`jobs`),
tenancy_0007 (`workspaces`, `projects`) and identity_0007; build_0002 is an ancestor of
packaging_0001 and is not named (a named ancestor of another named revision breaks
`alembic upgrade heads`, see delivery_0001).

SP5 design sections 4 and 5 (issue #255).

* `appgen_requests`: an FDE's request for a drafted app in one project: the read-only Databricks
  connection, the discovery whose tables were offered, 1 to 50 `catalog.schema.table` names, a
  brief of 1 to 4000 characters, the drafting model (no key: a shared SHI model has no client),
  the state (`queued`, `drafting`, `drafted`, `failed`, `accepting`, `accepted`, `discarded`) and
  its reason, the latest `appgen.draft` job (no key: the runtime reaps jobs), and the accept
  columns SP5-3 fills. `requested_by` / `accepted_by` hold principal ids without a key, as
  `delivery_plans.requested_by` (a service account is not a user).
* `shifde.appgen_requests_guard()`: a SECURITY INVOKER `BEFORE INSERT` trigger,
  `SET search_path = pg_catalog, pg_temp`, every object schema-qualified, EXECUTE revoked from
  PUBLIC. It refuses (`check_violation`) a request whose discovery, read under the writer's
  row-level security, is not a discovery of the request's own client and connection, whose
  project is not in its workspace, or whose connection is neither client-wide nor of its
  workspace or project.
  `connection_discoveries` (a ported SETH table) has no `(tenant_id, id)` unique key to name in a
  composite foreign key, and adding one would change a ported table, so this trigger keeps the
  reference inside the client instead.
* `appgen_drafts`: the numbered revisions of a request (`revision_no` unique per request), drafted
  by the model or edited by a person, each with its draft (at most 256 KiB), the draft's sha256
  and its diagnostics.
* `shifde.appgen_drafts_guard()`: a SECURITY INVOKER `BEFORE UPDATE` trigger with the same
  settings that refuses every update (`check_violation`), for the schema owner too.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); every key
between them and to `projects`, `connections` and `solution_versions` is composite
`(tenant_id, ...)`. Grants to `shifde_app`: requests SELECT, INSERT and UPDATE (status,
status_reason, job_id, accept_progress, accepted_revision_no, solution_version_id, accepted_by,
updated_at) only; drafts SELECT and INSERT only. No SECURITY DEFINER function.

Permissions: three keys, `appgen:request:read|write` and `appgen:draft:accept`, none privileged;
`tenant_administrator` and `developer` hold all three (six template grants); no other role.

Rollback: removes every `appgen:*` role grant (custom roles included) and permission, drops both
tables and both guard functions (data-destructive: every organization's requests and drafts are
lost).
"""

from __future__ import annotations

from alembic import op

revision = "appgen_0001"
down_revision = "base_0001"
branch_labels = ("appgen",)
depends_on = (
    "packaging_0003",
    "evaluation_0001",
    "connections_0003",
    "models_admin_0004",
    "runtime_0007",
    "tenancy_0007",
    "identity_0007",
)

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000001"
DEVELOPER = "01900000-0000-7000-8000-000000000004"
REQUESTS_GUARD_SIGNATURE = "appgen_requests_guard()"
DRAFTS_GUARD_SIGNATURE = "appgen_drafts_guard()"
REQUEST_UPDATE_COLUMNS = (
    "status, status_reason, job_id, accept_progress, accepted_revision_no, solution_version_id, "
    "accepted_by, updated_at"
)

# key, resource type, action, description, is_privileged
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    ("appgen:request:read", "appgen_request", "read", "View app requests and drafts", False),
    (
        "appgen:request:write",
        "appgen_request",
        "write",
        "Request, edit, discard and retry app drafts",
        False,
    ),
    ("appgen:draft:accept", "appgen_draft", "accept", "Accept app drafts", False),
]

REQUESTS_GUARD = """CREATE FUNCTION shifde.appgen_requests_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM shifde.connection_discoveries d
                        WHERE d.tenant_id = NEW.tenant_id AND d.id = NEW.discovery_id
                          AND d.connection_id = NEW.connection_id) THEN
          RAISE EXCEPTION 'the discovery must be one of the request''s connection'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM shifde.projects p
                        WHERE p.tenant_id = NEW.tenant_id AND p.id = NEW.project_id
                          AND p.workspace_id = NEW.workspace_id) THEN
          RAISE EXCEPTION 'the project must be in the request''s workspace'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM shifde.connections c
                        WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.connection_id
                          AND (c.workspace_id IS NULL
                               OR (c.workspace_id = NEW.workspace_id
                                   AND (c.project_id IS NULL
                                        OR c.project_id = NEW.project_id)))) THEN
          RAISE EXCEPTION 'the connection must be client-wide or of the request''s scope'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

DRAFTS_GUARD = """CREATE FUNCTION shifde.appgen_drafts_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        RAISE EXCEPTION 'app drafts are immutable' USING ERRCODE = 'check_violation';
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
    ]


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ requests
        """CREATE TABLE shifde.appgen_requests (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid NOT NULL,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            connection_id uuid NOT NULL,
            discovery_id uuid NOT NULL,
            tables jsonb NOT NULL,
            brief text NOT NULL CHECK (char_length(brief) BETWEEN 1 AND 4000),
            model_id uuid NOT NULL,
            status text NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'drafting', 'drafted', 'failed', 'accepting', 'accepted',
                'discarded')),
            status_reason text,
            job_id uuid,
            accept_progress jsonb NOT NULL DEFAULT '{}'::jsonb,
            accepted_revision_no int CHECK (accepted_revision_no >= 1),
            solution_version_id uuid,
            requested_by uuid NOT NULL,
            accepted_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_appgen_requests PRIMARY KEY (tenant_id, id),
            CONSTRAINT ck_appgen_requests__tables CHECK (
                jsonb_typeof(tables) = 'array'
                AND jsonb_array_length(tables) BETWEEN 1 AND 50),
            CONSTRAINT ck_appgen_requests__accept_progress
                CHECK (jsonb_typeof(accept_progress) = 'object'),
            CONSTRAINT fk_appgen_requests__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_appgen_requests__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_appgen_requests__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_appgen_requests__solution_version
                FOREIGN KEY (tenant_id, solution_version_id)
                REFERENCES shifde.solution_versions (tenant_id, id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_appgen_requests__project ON shifde.appgen_requests "
        "(tenant_id, project_id, id DESC)",
        "CREATE INDEX ix_appgen_requests__connection ON shifde.appgen_requests "
        "(tenant_id, connection_id)",
        "CREATE INDEX ix_appgen_requests__solution_version ON shifde.appgen_requests "
        "(tenant_id, solution_version_id)",
        *_tenant_table("appgen_requests"),
        REQUESTS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{REQUESTS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_appgen_requests_guard BEFORE INSERT ON shifde.appgen_requests "
        "FOR EACH ROW EXECUTE FUNCTION shifde.appgen_requests_guard()",
        "GRANT SELECT, INSERT ON shifde.appgen_requests TO shifde_app",
        f"GRANT UPDATE ({REQUEST_UPDATE_COLUMNS}) ON shifde.appgen_requests TO shifde_app",
        # ------------------------------------------------------------------ drafts
        """CREATE TABLE shifde.appgen_drafts (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid NOT NULL,
            request_id uuid NOT NULL,
            revision_no int NOT NULL CHECK (revision_no >= 1),
            source text NOT NULL CHECK (source IN ('model', 'edited')),
            draft jsonb NOT NULL,
            draft_hash text NOT NULL CHECK (draft_hash ~ '^[0-9a-f]{64}$'),
            validation jsonb NOT NULL CHECK (jsonb_typeof(validation) = 'array'),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT pk_appgen_drafts PRIMARY KEY (tenant_id, id),
            CONSTRAINT ck_appgen_drafts__size CHECK (octet_length(draft::text) <= 262144),
            CONSTRAINT uq_appgen_drafts__revision UNIQUE (tenant_id, request_id, revision_no),
            CONSTRAINT fk_appgen_drafts__request FOREIGN KEY (tenant_id, request_id)
                REFERENCES shifde.appgen_requests (tenant_id, id) ON DELETE CASCADE
        )""",
        DRAFTS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{DRAFTS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_appgen_drafts_guard BEFORE UPDATE ON shifde.appgen_drafts "
        "FOR EACH ROW EXECUTE FUNCTION shifde.appgen_drafts_guard()",
        *_tenant_table("appgen_drafts"),
        "GRANT SELECT, INSERT ON shifde.appgen_drafts TO shifde_app",
    )
    for key, resource_type, action, description, privileged in PERMISSIONS:
        _run(
            "INSERT INTO shifde.permissions (key, resource_type, action, description, "
            f"is_privileged) VALUES ('{key}', '{resource_type}', '{action}', '{description}', "
            f"{_sql_bool(privileged)})"
        )
    for role_id in (TENANT_ADMINISTRATOR, DEVELOPER):
        for key, *_ in PERMISSIONS:
            _run(
                "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
                f"VALUES ('{role_id}', '{key}', NULL)"
            )


def downgrade() -> None:
    _run(
        "DELETE FROM shifde.role_permissions WHERE starts_with(permission_key, 'appgen:')",
        "DELETE FROM shifde.permissions WHERE starts_with(key, 'appgen:')",
        "DROP TABLE shifde.appgen_drafts",
        "DROP TABLE shifde.appgen_requests",
        f"DROP FUNCTION shifde.{DRAFTS_GUARD_SIGNATURE}",
        f"DROP FUNCTION shifde.{REQUESTS_GUARD_SIGNATURE}",
    )
