"""Build: versioned artifacts, pins, publish requests, prompt test runs, pipeline refs.

Revision ID: build_0001
Revises: base_0001
Depends on: tenancy_0001 (`tenants`, `workspaces`, `projects`), models_admin_0001 (`models`),
connections_0001 (`connections`), identity_0001 (`service_accounts`)

Taken from SETH 3ba0e06 `p04_0001_build_artifacts` and the build-owned tables of
`p06_0001_pipelines` (`pipeline_version_refs`, `pipeline_validation_reports`,
`pipeline_schedules`), in their final shape after `p05_0001`, `p06_0003` and `p07_0001`:

* `artifacts.kind` / `artifact_versions.kind` admit all seven kinds (prompts, agents, tools,
  skills, orchestrations, pipelines, ontologies); `artifact_version_pins.role` and
  `pipeline_version_refs.ref_type` carry the values of the last SETH migration that widened them;
* `uq_artifact_versions__tenant_id_id_artifact` (from `p06_0003`) is the target of the later
  `runs` foreign keys; the foreign keys from `runs`, `run_steps` and the ontology tables into
  these tables come with the runtime and ontology migrations.

Not here: the permission catalog of `p04_0002` / `p06_0002` / `p07_0002` is seeded by
`authz_0001`; the `runs`, `run_steps`, `approvals` and `usage_events` column and CHECK changes
of `p06_0001` / `p07_0001` belong to the runtime and ledger migrations; the ontology tables
belong to the ontology migration (DECISIONS B-1901).

Rollback: drops every build table (data-destructive: every client's artifacts); back up first.
"""

from __future__ import annotations

from alembic import op

revision = "build_0001"
down_revision = "base_0001"
branch_labels = ("build",)
depends_on = ("tenancy_0001", "models_admin_0001", "connections_0001", "identity_0001")

ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"
KINDS = "('prompts', 'agents', 'tools', 'skills', 'orchestrations', 'pipelines', 'ontologies')"

DROP_ORDER = [
    "pipeline_schedules",
    "pipeline_validation_reports",
    "pipeline_version_refs",
    "prompt_test_runs",
    "publish_requests",
    "artifact_version_pins",
    "artifact_versions",
    "artifacts",
]


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
    _run(
        # ------------------------------------------------------------------ artifacts
        f"""CREATE TABLE shifde.artifacts (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            kind text NOT NULL CHECK (kind IN {KINDS}),
            workspace_id uuid,
            project_id uuid,
            key text NOT NULL CHECK (key ~ '^[a-z0-9]([a-z0-9._-]{{0,98}}[a-z0-9])?$'),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            description text,
            owner_principal_id uuid NOT NULL,
            visibility text NOT NULL DEFAULT 'scope'
                CHECK (visibility IN ('private', 'scope', 'tenant')),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'retired')),
            latest_published_version_id uuid,
            row_version int NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            created_by uuid,
            CONSTRAINT ck_artifacts__scope CHECK (project_id IS NULL OR workspace_id IS NOT NULL),
            CONSTRAINT uq_artifacts__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_artifacts__tenant_id_id_kind UNIQUE (tenant_id, id, kind),
            CONSTRAINT fk_artifacts__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_artifacts__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_artifacts__scope_key ON shifde.artifacts "
        f"(tenant_id, kind, COALESCE(workspace_id, {ZERO}), COALESCE(project_id, {ZERO}), key)",
        "CREATE INDEX ix_artifacts__tenant_kind ON shifde.artifacts (tenant_id, kind, id)",
        "CREATE TRIGGER trg_artifacts_updated_at BEFORE UPDATE ON shifde.artifacts "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        *_tenant_table("artifacts"),
        "GRANT SELECT, INSERT ON shifde.artifacts TO shifde_app",
        "GRANT UPDATE (name, description, visibility, status, latest_published_version_id, "
        "row_version) ON shifde.artifacts TO shifde_app",
        # ------------------------------------------------------------------ versions
        f"""CREATE TABLE shifde.artifact_versions (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            artifact_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN {KINDS}),
            version_no int NOT NULL CHECK (version_no > 0),
            state text NOT NULL CHECK (state IN ('draft', 'published', 'deprecated', 'retired')),
            spec jsonb NOT NULL DEFAULT '{{}}',
            spec_schema_version int NOT NULL DEFAULT 1,
            spec_hash text,
            change_note text,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            published_by uuid,
            published_at timestamptz,
            approved_by uuid,
            approval_id uuid,
            based_on_version_id uuid,
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT ck_artifact_versions__published CHECK
                (state = 'draft' OR (spec_hash IS NOT NULL AND published_at IS NOT NULL)),
            CONSTRAINT ck_artifact_versions__size CHECK (octet_length(spec::text) <= 2097152),
            CONSTRAINT uq_artifact_versions__no UNIQUE (tenant_id, artifact_id, version_no),
            CONSTRAINT uq_artifact_versions__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_artifact_versions__tenant_id_id_artifact
                UNIQUE (tenant_id, id, artifact_id),
            CONSTRAINT fk_artifact_versions__artifact FOREIGN KEY (tenant_id, artifact_id, kind)
                REFERENCES shifde.artifacts (tenant_id, id, kind),
            CONSTRAINT fk_artifact_versions__based_on FOREIGN KEY (tenant_id, based_on_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_artifact_versions__one_draft ON shifde.artifact_versions "
        "(tenant_id, artifact_id) WHERE state = 'draft'",
        "CREATE INDEX ix_artifact_versions__artifact ON shifde.artifact_versions "
        "(tenant_id, artifact_id, version_no DESC)",
        "ALTER TABLE shifde.artifacts ADD CONSTRAINT fk_artifacts__latest FOREIGN KEY "
        "(tenant_id, latest_published_version_id) REFERENCES shifde.artifact_versions "
        "(tenant_id, id)",
        # Immutability in the database (invoker rights; applies to the owner too).
        """CREATE FUNCTION shifde.artifact_versions_guard() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
             IF TG_OP = 'DELETE' THEN
               IF OLD.state <> 'draft' THEN
                 RAISE EXCEPTION 'published artifact versions cannot be deleted'
                   USING ERRCODE = '23514';
               END IF;
               RETURN OLD;
             END IF;
             IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id
                OR NEW.artifact_id <> OLD.artifact_id OR NEW.kind <> OLD.kind
                OR NEW.version_no <> OLD.version_no OR NEW.created_at <> OLD.created_at THEN
               RAISE EXCEPTION 'artifact version identity columns are immutable'
                 USING ERRCODE = '23514';
             END IF;
             IF OLD.state = 'draft' THEN
               IF NEW.state NOT IN ('draft', 'published') THEN
                 RAISE EXCEPTION 'a draft can only be published' USING ERRCODE = '23514';
               END IF;
               RETURN NEW;
             END IF;
             IF NEW.spec IS DISTINCT FROM OLD.spec
                OR NEW.spec_hash IS DISTINCT FROM OLD.spec_hash
                OR NEW.spec_schema_version IS DISTINCT FROM OLD.spec_schema_version
                OR NEW.change_note IS DISTINCT FROM OLD.change_note
                OR NEW.based_on_version_id IS DISTINCT FROM OLD.based_on_version_id
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.published_by IS DISTINCT FROM OLD.published_by
                OR NEW.published_at IS DISTINCT FROM OLD.published_at
                OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
                OR NEW.approval_id IS DISTINCT FROM OLD.approval_id THEN
               RAISE EXCEPTION 'published artifact versions are immutable'
                 USING ERRCODE = '23514';
             END IF;
             IF NEW.state <> OLD.state AND NOT
                ((OLD.state = 'published' AND NEW.state = 'deprecated')
                 OR (OLD.state = 'deprecated' AND NEW.state = 'retired')) THEN
               RAISE EXCEPTION 'illegal version state transition %% -> %%', OLD.state, NEW.state
                 USING ERRCODE = '23514';
             END IF;
             RETURN NEW;
           END $$""",
        "CREATE TRIGGER trg_artifact_versions_guard BEFORE UPDATE OR DELETE "
        "ON shifde.artifact_versions FOR EACH ROW EXECUTE FUNCTION shifde.artifact_versions_guard()",
        *_tenant_table("artifact_versions"),
        "GRANT SELECT, INSERT ON shifde.artifact_versions TO shifde_app",
        "GRANT UPDATE (spec, spec_schema_version, spec_hash, state, change_note, published_by, "
        "published_at, approved_by, approval_id, based_on_version_id, row_version) "
        "ON shifde.artifact_versions TO shifde_app",
        # ------------------------------------------------------------------ pins
        """CREATE TABLE shifde.artifact_version_pins (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            version_id uuid NOT NULL,
            pinned_version_id uuid NOT NULL,
            role text NOT NULL CHECK (role IN
                ('prompt', 'tool', 'skill', 'composed_prompt', 'skill_dependency', 'agent',
                 'ontology', 'action_tool')),
            PRIMARY KEY (tenant_id, version_id, pinned_version_id, role),
            CONSTRAINT fk_pins__version FOREIGN KEY (tenant_id, version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_pins__pinned FOREIGN KEY (tenant_id, pinned_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE INDEX ix_pins__pinned ON shifde.artifact_version_pins (tenant_id, pinned_version_id)",
        *_tenant_table("artifact_version_pins"),
        "GRANT SELECT, INSERT ON shifde.artifact_version_pins TO shifde_app",
        # ------------------------------------------------------------------ publish requests
        """CREATE TABLE shifde.publish_requests (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            artifact_id uuid NOT NULL,
            kind text NOT NULL,
            version_id uuid NOT NULL,
            version_no int NOT NULL,
            change_note text,
            spec_hash text NOT NULL,
            requested_by uuid NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
            decided_by uuid,
            decided_at timestamptz,
            comment text,
            CONSTRAINT ck_publish_requests__separation CHECK
                (decided_by IS NULL OR decided_by <> requested_by),
            CONSTRAINT ck_publish_requests__decided CHECK
                ((status = 'pending') = (decided_by IS NULL)),
            CONSTRAINT fk_publish_requests__artifact FOREIGN KEY (tenant_id, artifact_id, kind)
                REFERENCES shifde.artifacts (tenant_id, id, kind),
            CONSTRAINT fk_publish_requests__version FOREIGN KEY (tenant_id, version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_publish_requests__one_pending ON shifde.publish_requests "
        "(tenant_id, version_id) WHERE status = 'pending'",
        "CREATE INDEX ix_publish_requests__status ON shifde.publish_requests (tenant_id, status, id)",
        """CREATE FUNCTION shifde.publish_requests_guard() RETURNS trigger LANGUAGE plpgsql AS
           $$ BEGIN
             IF OLD.status <> 'pending' THEN
               RAISE EXCEPTION 'a decided publish request is final' USING ERRCODE = '23514';
             END IF;
             IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id
                OR NEW.version_id <> OLD.version_id OR NEW.requested_by <> OLD.requested_by
                OR NEW.spec_hash <> OLD.spec_hash THEN
               RAISE EXCEPTION 'publish request identity columns are immutable'
                 USING ERRCODE = '23514';
             END IF;
             RETURN NEW;
           END $$""",
        "CREATE TRIGGER trg_publish_requests_guard BEFORE UPDATE ON shifde.publish_requests "
        "FOR EACH ROW EXECUTE FUNCTION shifde.publish_requests_guard()",
        *_tenant_table("publish_requests"),
        "GRANT SELECT, INSERT ON shifde.publish_requests TO shifde_app",
        "GRANT UPDATE (status, decided_by, decided_at, comment) "
        "ON shifde.publish_requests TO shifde_app",
        # ------------------------------------------------------------------ prompt test runs
        """CREATE TABLE shifde.prompt_test_runs (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            artifact_id uuid NOT NULL,
            version_id uuid NOT NULL,
            model_id uuid NOT NULL,
            inputs jsonb NOT NULL DEFAULT '{}',
            output text NOT NULL,
            usage jsonb NOT NULL DEFAULT '{}',
            latency_ms int NOT NULL,
            evaluation jsonb NOT NULL DEFAULT '[]',
            verdict text NOT NULL CHECK (verdict IN ('pass', 'fail')),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_prompt_test_runs__version FOREIGN KEY (tenant_id, version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_prompt_test_runs__artifact FOREIGN KEY (tenant_id, artifact_id)
                REFERENCES shifde.artifacts (tenant_id, id),
            CONSTRAINT fk_prompt_test_runs__model FOREIGN KEY (tenant_id, model_id)
                REFERENCES shifde.models (tenant_id, id)
        )""",
        "CREATE INDEX ix_prompt_test_runs__artifact ON shifde.prompt_test_runs "
        "(tenant_id, artifact_id, id DESC)",
        "CREATE TRIGGER trg_prompt_test_runs_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.prompt_test_runs FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("prompt_test_runs"),
        "GRANT SELECT, INSERT ON shifde.prompt_test_runs TO shifde_app",
        # ------------------------------------------------------------------ pipeline refs
        """CREATE TABLE shifde.pipeline_version_refs (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            pipeline_version_id uuid NOT NULL,
            node_id text NOT NULL,
            ref_type text NOT NULL CHECK (ref_type IN
                ('connection', 'agent_version', 'prompt_version', 'tool_version', 'model',
                 'ontology_version')),
            ref_id uuid NOT NULL,
            connection_id uuid,
            ref_version_id uuid,
            model_id uuid,
            PRIMARY KEY (tenant_id, pipeline_version_id, node_id, ref_type, ref_id),
            CONSTRAINT ck_pipeline_refs__typed CHECK (
                (ref_type = 'connection' AND connection_id = ref_id
                    AND ref_version_id IS NULL AND model_id IS NULL)
                OR (ref_type = 'model' AND model_id = ref_id
                    AND ref_version_id IS NULL AND connection_id IS NULL)
                OR (ref_type IN ('agent_version', 'prompt_version', 'tool_version',
                                 'ontology_version')
                    AND ref_version_id = ref_id AND connection_id IS NULL AND model_id IS NULL)),
            CONSTRAINT fk_pipeline_refs__version FOREIGN KEY (tenant_id, pipeline_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_pipeline_refs__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id),
            CONSTRAINT fk_pipeline_refs__pinned FOREIGN KEY (tenant_id, ref_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_pipeline_refs__model FOREIGN KEY (tenant_id, model_id)
                REFERENCES shifde.models (tenant_id, id)
        )""",
        "CREATE INDEX ix_pipeline_refs__connection ON shifde.pipeline_version_refs "
        "(tenant_id, connection_id) WHERE connection_id IS NOT NULL",
        *_tenant_table("pipeline_version_refs"),
        "GRANT SELECT, INSERT ON shifde.pipeline_version_refs TO shifde_app",
        # ------------------------------------------------------------------ reports
        """CREATE TABLE shifde.pipeline_validation_reports (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            version_id uuid NOT NULL,
            report jsonb NOT NULL,
            topo_order jsonb NOT NULL DEFAULT '[]',
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, version_id),
            CONSTRAINT fk_pipeline_reports__version FOREIGN KEY (tenant_id, version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE TRIGGER trg_pipeline_reports_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.pipeline_validation_reports FOR EACH ROW "
        "EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("pipeline_validation_reports"),
        "GRANT SELECT, INSERT ON shifde.pipeline_validation_reports TO shifde_app",
        # ------------------------------------------------------------------ schedules
        """CREATE TABLE shifde.pipeline_schedules (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            pipeline_id uuid NOT NULL,
            kind text NOT NULL DEFAULT 'pipelines' CHECK (kind = 'pipelines'),
            version_policy text NOT NULL CHECK (version_policy IN ('pinned', 'latest_published')),
            pipeline_version_id uuid,
            cron text NOT NULL CHECK (length(cron) BETWEEN 9 AND 120),
            timezone text NOT NULL DEFAULT 'UTC',
            params jsonb NOT NULL DEFAULT '{}',
            run_as_service_account_id uuid NOT NULL,
            status text NOT NULL DEFAULT 'paused' CHECK (status IN ('active', 'paused')),
            next_run_at timestamptz,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT ck_pipeline_schedules__pinned CHECK
                ((version_policy = 'pinned') = (pipeline_version_id IS NOT NULL)),
            CONSTRAINT fk_pipeline_schedules__pipeline FOREIGN KEY (tenant_id, pipeline_id, kind)
                REFERENCES shifde.artifacts (tenant_id, id, kind),
            CONSTRAINT fk_pipeline_schedules__version FOREIGN KEY (tenant_id, pipeline_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_pipeline_schedules__sa FOREIGN KEY (tenant_id, run_as_service_account_id)
                REFERENCES shifde.service_accounts (tenant_id, id)
        )""",
        "CREATE INDEX ix_pipeline_schedules__due ON shifde.pipeline_schedules "
        "(next_run_at) WHERE status = 'active'",
        "CREATE INDEX ix_pipeline_schedules__pipeline ON shifde.pipeline_schedules "
        "(tenant_id, pipeline_id, id)",
        *_tenant_table("pipeline_schedules"),
        "GRANT SELECT, INSERT ON shifde.pipeline_schedules TO shifde_app",
        "GRANT UPDATE (cron, timezone, params, status, next_run_at, row_version) "
        "ON shifde.pipeline_schedules TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "ALTER TABLE shifde.artifacts DROP CONSTRAINT IF EXISTS fk_artifacts__latest",
        *[f"DROP TABLE IF EXISTS shifde.{t} CASCADE" for t in DROP_ORDER],
        "DROP FUNCTION IF EXISTS shifde.publish_requests_guard()",
        "DROP FUNCTION IF EXISTS shifde.artifact_versions_guard()",
    )
