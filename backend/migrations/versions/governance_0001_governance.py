"""Governance: policies and retention settings.

Revision ID: governance_0001
Revises: base_0001
Depends on: tenancy_0001 (`tenants`)

Taken from SETH 3ba0e06 `p08_0001_governance` (`governance_policies`, `retention_policies`): columns,
checks, the index, forced row-level security (tenant-keyed USING and WITH CHECK) and the grants are
SETH's.

Not here (DECISIONS B-1802):

* the other tables of `p08_0001` belong to the ledger (`ledger_0001`: rate cards, rollups, alerts,
  quotas); `policy_decisions.policy_ids` and its run index are in `authz_0001`;
* the permission catalog of `p08_0002` is in `authz_0001`;
* `workers.last_reap_at` belongs to the runtime migration;
* `shifde_ops_stats(int)` (a SECURITY DEFINER aggregate over `jobs`, `runs`, `usage_events` and
  `policy_decisions`) comes with the governance part that serves the performance and ops routes.

Rollback: drops the two tables (data-destructive: every client's policies and retention
settings); back up first.
"""

from __future__ import annotations

from alembic import op

revision = "governance_0001"
down_revision = "base_0001"
branch_labels = ("governance",)
depends_on = ("tenancy_0001",)


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
        """CREATE TABLE shifde.governance_policies (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            kind text NOT NULL CHECK (kind IN
                ('model_policy', 'approval_rule', 'data_export', 'lifecycle', 'cost_threshold')),
            name text NOT NULL DEFAULT '' CHECK (length(name) <= 200),
            scope_type text CHECK (scope_type IN
                ('tenant', 'workspace', 'project', 'agent', 'pipeline', 'principal')),
            scope_id uuid,
            definition jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            version int NOT NULL DEFAULT 1,
            row_version int NOT NULL DEFAULT 1,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_governance_policies__scope CHECK
                ((scope_type IS NULL) = (scope_id IS NULL))
        )""",
        "CREATE INDEX ix_governance_policies__kind ON shifde.governance_policies "
        "(tenant_id, kind, status)",
        *_tenant_table("governance_policies"),
        "GRANT SELECT, INSERT ON shifde.governance_policies TO shifde_app",
        "GRANT UPDATE (name, scope_type, scope_id, definition, status, version, row_version, "
        "updated_at) ON shifde.governance_policies TO shifde_app",
        """CREATE TABLE shifde.retention_policies (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            data_class text NOT NULL CHECK (data_class IN
                ('run_artifacts', 'run_steps_content', 'audit_events', 'usage_events',
                 'policy_decisions')),
            retain_days int NOT NULL CHECK (retain_days >= 1),
            legal_hold boolean NOT NULL DEFAULT false,
            updated_by uuid,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, data_class)
        )""",
        *_tenant_table("retention_policies"),
        "GRANT SELECT, INSERT ON shifde.retention_policies TO shifde_app",
        "GRANT UPDATE (retain_days, legal_hold, updated_by, updated_at) "
        "ON shifde.retention_policies TO shifde_app",
    )


def downgrade() -> None:
    _run(
        "DROP TABLE shifde.retention_policies",
        "DROP TABLE shifde.governance_policies",
    )
