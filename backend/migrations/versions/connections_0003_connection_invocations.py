"""Connections: `connection_invocations`, the result of a connection invoke run on the worker queue.

Revision ID: connections_0003
Revises: connections_0002
Depends on: tenancy_0001 (`tenants`)

Issue #190 (split from #144, B-2823; DECISIONS B-2996, B-2997). Moving connection `invoke` onto
the worker queue needs a place for its result, and audit event details must not hold customer
payloads. This revision adds the table only; the factory job for #190 queues the invoke, writes
and reads the rows.

`connection_invocations` (one row per queued invoke):

* `tenant_id`, `id`; `job_id`, the `jobs` row that runs it (one invocation per job:
  `uq_connection_invocations__job` on `(tenant_id, job_id)`); `connection_id`; `requested_by`,
  the principal that asked (only the requester reads the status).
* `status` `queued`, `running`, `succeeded` or `failed`; `finished_at` is set exactly when the
  status is terminal; `result` (scrubbed, jsonb) only on `succeeded`; `error_code` only on
  `failed`.
* `created_at`, `updated_at` (`set_updated_at` trigger), `finished_at`.
* `ix_connection_invocations__requester` `(tenant_id, requested_by, created_at DESC)` serves the
  requester's status reads; `ix_connection_invocations__connection` `(tenant_id, connection_id)`
  the composite key.

Isolation: client-owned, forced row-level security, one `tenant_isolation` policy (USING and WITH
CHECK on `tenant_id = current_tenant_id()`), like `connection_tests`. The key to `connections` is
composite, `fk_connection_invocations__connection (tenant_id, connection_id) -> connections
(tenant_id, id)`, so a row can name only its own client's connection (constraints are checked as
the owner, without row-level security; B-116 item 9). `job_id` has no foreign key (B-2996):
`jobs` has no `(tenant_id, id)` key and the retention delete removes old jobs. `shifde_app` gets
SELECT, INSERT and UPDATE of `status`, `result`, `error_code` and `finished_at` only: no DELETE,
no change of tenant, job, connection or requester. No function, no SECURITY DEFINER, no global
table. Owned by the schema owner (the migration role).

Rollback: drops the table (data-destructive: every client's invocation results are lost).
"""

from __future__ import annotations

from alembic import op

revision = "connections_0003"
down_revision = "connections_0002"
branch_labels = None
depends_on = ("tenancy_0001",)

TENANT = "tenant_id = shifde.current_tenant_id()"
TABLE = "connection_invocations"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        f"""CREATE TABLE shifde.{TABLE} (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            job_id uuid NOT NULL,
            connection_id uuid NOT NULL,
            requested_by uuid NOT NULL,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            result jsonb,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            finished_at timestamptz,
            CONSTRAINT uq_connection_invocations__job UNIQUE (tenant_id, job_id),
            CONSTRAINT fk_connection_invocations__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id),
            CONSTRAINT ck_connection_invocations__finished
                CHECK ((status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)),
            CONSTRAINT ck_connection_invocations__result
                CHECK (result IS NULL OR status = 'succeeded'),
            CONSTRAINT ck_connection_invocations__error_code
                CHECK (error_code IS NULL OR status = 'failed')
        )""",
        f"CREATE INDEX ix_connection_invocations__requester ON shifde.{TABLE} "
        "(tenant_id, requested_by, created_at DESC)",
        f"CREATE INDEX ix_connection_invocations__connection ON shifde.{TABLE} "
        "(tenant_id, connection_id)",
        f"CREATE TRIGGER trg_{TABLE}_updated_at BEFORE UPDATE ON shifde.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        f"ALTER TABLE shifde.{TABLE} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{TABLE} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{TABLE} FOR ALL USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
        f"GRANT SELECT, INSERT ON shifde.{TABLE} TO shifde_app",
        f"GRANT UPDATE (status, result, error_code, finished_at) ON shifde.{TABLE} TO shifde_app",
    )


def downgrade() -> None:
    _run(f"DROP TABLE shifde.{TABLE}")
