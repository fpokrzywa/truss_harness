"""Evaluation, part 1: evaluation datasets, evaluation runs and their guards.

Revision ID: evaluation_0001
Revises: base_0001
Depends on: packaging_0002 (`releases`; `packaging_0001` seeded the `evaluation:*` permission keys)
and runtime_0004 (the runtime branch head; case runs are ordinary `runs` rows). Their ancestors
(`tenancy_0006` for `tenants`, `workspaces`, `projects`) are not named (a named ancestor of
another named revision breaks `alembic upgrade heads`, see delivery_0001).

Sub-project 3 design sections 4 and 6, settled calls 7, 16 to 22 (issue #80).

* `evaluation_datasets`: an immutable, numbered version of a named dataset of one project
  (workspace and project required; `version_no` unique per project and name,
  `uq_evaluation_datasets__name_version`). `cases` is a JSON array of 1 to 200 cases
  (`ck_evaluation_datasets__cases`) of at most 1 MiB in its text form
  (`ck_evaluation_datasets__size`); `digest` is the sha256 of the cases' canonical JSON.
* `shifde.evaluation_datasets_guard()`: a SECURITY INVOKER `BEFORE UPDATE OR DELETE` trigger,
  `SET search_path = pg_catalog, pg_temp`, EXECUTE revoked from PUBLIC: every update and delete
  is refused (`check_violation`), for the schema owner too. A change is a new version.
* `evaluation_runs`: one evaluation of a release against its manifest's dataset, keyed to
  `releases (tenant_id, id)` and `evaluation_datasets (tenant_id, id)`, both `ON DELETE RESTRICT`.
  It records the release's digest, the dataset's digest and the thresholds at request time, the
  case runs it started (`case_runs`, `[{case_id, run_id}]`), its status (`queued`, `running`,
  `passed`, `failed`, `error`) and the scored results.
* `shifde.evaluation_runs_guard()`: the same function rules, a `BEFORE UPDATE OR DELETE` trigger
  (settled call 20). `release_id`, `release_digest`, `dataset_id`, `dataset_digest`, `thresholds`,
  `requested_by` and `created_at` (and `tenant_id`, `id`) never change; `case_runs` changes only
  on the update that moves `queued` to `running`; a row with a terminal status (`passed`,
  `failed`, `error`) never changes; a delete is always refused. Every refusal is
  `check_violation`, for the schema owner too.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); every key
to `tenants`, `workspaces`, `projects`, `releases` and between them is composite
`(tenant_id, ...)`. Grants to `shifde_app`: datasets SELECT and INSERT only; runs SELECT, INSERT
and UPDATE of the status and result columns (`status`, `status_reason`, `case_runs`,
`accuracy`, `p95_latency_ms`, `total_cost_usd`, `results`, `started_at`, `finished_at`) only; no
DELETE. No SECURITY DEFINER function, no global table, no permission seeded.

Rollback: drops both triggers and guard functions, then both tables (data-destructive: every
organization's evaluation datasets and evaluation runs are lost).
"""

from __future__ import annotations

from alembic import op

revision = "evaluation_0001"
down_revision = "base_0001"
branch_labels = ("evaluation",)
depends_on = ("packaging_0002", "runtime_0004")

TENANT = "tenant_id = shifde.current_tenant_id()"
DATASETS_GUARD_SIGNATURE = "evaluation_datasets_guard()"
RUNS_GUARD_SIGNATURE = "evaluation_runs_guard()"

DATASETS_GUARD = """CREATE FUNCTION shifde.evaluation_datasets_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        RAISE EXCEPTION 'evaluation datasets are immutable' USING ERRCODE = 'check_violation';
      END $$"""

RUNS_GUARD = """CREATE FUNCTION shifde.evaluation_runs_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'evaluation runs cannot be deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.status IN ('passed', 'failed', 'error') THEN
          RAISE EXCEPTION 'an ended evaluation run never changes'
            USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.tenant_id, NEW.id, NEW.release_id, NEW.release_digest, NEW.dataset_id,
            NEW.dataset_digest, NEW.thresholds, NEW.requested_by, NEW.created_at)
           IS DISTINCT FROM
           (OLD.tenant_id, OLD.id, OLD.release_id, OLD.release_digest, OLD.dataset_id,
            OLD.dataset_digest, OLD.thresholds, OLD.requested_by, OLD.created_at) THEN
          RAISE EXCEPTION 'what an evaluation run evaluates never changes'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.case_runs IS DISTINCT FROM OLD.case_runs
           AND NOT (OLD.status = 'queued' AND NEW.status = 'running') THEN
          RAISE EXCEPTION 'case runs are recorded only when the evaluation starts running'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
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


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ datasets
        """CREATE TABLE shifde.evaluation_datasets (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            project_id uuid NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            version_no int NOT NULL CHECK (version_no > 0),
            cases jsonb NOT NULL,
            digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_evaluation_datasets__cases CHECK (
                jsonb_typeof(cases) = 'array' AND jsonb_array_length(cases) BETWEEN 1 AND 200),
            CONSTRAINT ck_evaluation_datasets__size CHECK (octet_length(cases::text) <= 1048576),
            CONSTRAINT uq_evaluation_datasets__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_evaluation_datasets__name_version
                UNIQUE (tenant_id, project_id, name, version_no),
            CONSTRAINT fk_evaluation_datasets__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_evaluation_datasets__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id)
        )""",
        DATASETS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{DATASETS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_evaluation_datasets_guard BEFORE UPDATE OR DELETE "
        "ON shifde.evaluation_datasets FOR EACH ROW "
        "EXECUTE FUNCTION shifde.evaluation_datasets_guard()",
        *_tenant_table("evaluation_datasets"),
        "GRANT SELECT, INSERT ON shifde.evaluation_datasets TO shifde_app",
        # ------------------------------------------------------------------ runs
        """CREATE TABLE shifde.evaluation_runs (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            release_id uuid NOT NULL,
            release_digest text NOT NULL CHECK (release_digest ~ '^[0-9a-f]{64}$'),
            dataset_id uuid NOT NULL,
            dataset_digest text NOT NULL CHECK (dataset_digest ~ '^[0-9a-f]{64}$'),
            thresholds jsonb NOT NULL CHECK (jsonb_typeof(thresholds) = 'object'),
            case_runs jsonb NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(case_runs) = 'array'),
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'passed', 'failed', 'error')),
            status_reason text CHECK (length(status_reason) <= 2000),
            accuracy numeric(5,4) CHECK (accuracy BETWEEN 0 AND 1),
            p95_latency_ms int CHECK (p95_latency_ms >= 0),
            total_cost_usd text CHECK (total_cost_usd ~ '^[0-9]+(\\.[0-9]+)?$'),
            results jsonb,
            requested_by uuid NOT NULL,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_evaluation_runs__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_evaluation_runs__release FOREIGN KEY (tenant_id, release_id)
                REFERENCES shifde.releases (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_evaluation_runs__dataset FOREIGN KEY (tenant_id, dataset_id)
                REFERENCES shifde.evaluation_datasets (tenant_id, id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_evaluation_runs__release ON shifde.evaluation_runs "
        "(tenant_id, release_id)",
        RUNS_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{RUNS_GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_evaluation_runs_guard BEFORE UPDATE OR DELETE "
        "ON shifde.evaluation_runs FOR EACH ROW EXECUTE FUNCTION shifde.evaluation_runs_guard()",
        *_tenant_table("evaluation_runs"),
        "GRANT SELECT, INSERT ON shifde.evaluation_runs TO shifde_app",
        "GRANT UPDATE (status, status_reason, case_runs, accuracy, p95_latency_ms, "
        "total_cost_usd, results, started_at, finished_at) ON shifde.evaluation_runs "
        "TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER trg_evaluation_runs_guard ON shifde.evaluation_runs",
        f"DROP FUNCTION shifde.{RUNS_GUARD_SIGNATURE}",
        "DROP TABLE shifde.evaluation_runs",
        "DROP TRIGGER trg_evaluation_datasets_guard ON shifde.evaluation_datasets",
        f"DROP FUNCTION shifde.{DATASETS_GUARD_SIGNATURE}",
        "DROP TABLE shifde.evaluation_datasets",
    )
