"""Audit: the append-only audit log.

Revision ID: audit_0001
Revises: base_0001

SETH 3ba0e06's final schema for `audit_events`, folded from `p01_0001_tenancy_core` (table,
indexes, append-only trigger, policies, grant) and `p02_0002_secrets_idp_audit` (`chain_seq`,
`uq_audit_events__chain`, `ix_audit_events__tenant_id_id`). No later SETH migration touches it.

Deliberate difference from SETH (DECISIONS B-701, finding ARCH m7): SETH's INSERT policy
`write_own` admitted `tenant_id IS NULL` for any caller, so any client could write a global
audit row. Here a NULL-tenant row needs the platform-administrator context
(`tenant_id IS NULL AND shifde.is_platform_admin_ctx()`); `audit.service.record` sets that context
for the one insert, and only for a closed list of installation-level actions.

Foreign keys not created here (owed by the owning modules, docs/build/carry_forward.md):
`tenant_id -> tenants(id)` (tenancy), `workspace_id -> workspaces(id)` and
`project_id -> projects(id)` (tenancy). SETH has none of the last two on this table; only the
tenant FK exists there, so only that one is listed in `deliberate_changes.yaml`.

Rollback: drops the table (data-destructive); back up first. The audit log is the record of what
happened: do not downgrade a database that holds events you must keep.
"""

from __future__ import annotations

from alembic import op

revision = "audit_0001"
down_revision = "base_0001"
branch_labels = ("audit",)
depends_on = None

TENANT_OR_PLATFORM = (
    "(tenant_id = shifde.current_tenant_id() "
    "OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx()))"
)


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        """CREATE TABLE shifde.audit_events (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            actor_type text NOT NULL CHECK (actor_type IN
                ('user', 'service_account', 'system', 'anonymous')),
            actor_id uuid,
            actor_ip text,
            auth_method text,
            action text NOT NULL,
            target_type text,
            target_id text,
            workspace_id uuid,
            project_id uuid,
            outcome text NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
            details jsonb NOT NULL DEFAULT '{}',
            trace_id text,
            correlation_id text,
            prev_hash bytea,
            hash bytea,
            chain_seq bigint
        )""",
        "CREATE INDEX ix_audit_events__tenant_id_occurred_at ON shifde.audit_events "
        "(tenant_id, occurred_at DESC)",
        "CREATE INDEX ix_audit_events__tenant_id_workspace_id ON shifde.audit_events "
        "(tenant_id, workspace_id, occurred_at DESC)",
        "CREATE UNIQUE INDEX uq_audit_events__chain ON shifde.audit_events (tenant_id, chain_seq) "
        "WHERE chain_seq IS NOT NULL",
        "CREATE INDEX ix_audit_events__tenant_id_id ON shifde.audit_events (tenant_id, id DESC)",
        "CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON "
        "shifde.audit_events FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        "ALTER TABLE shifde.audit_events ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.audit_events FORCE ROW LEVEL SECURITY",
        # tenant rows are visible in their own context; installation-level rows (tenant_id NULL)
        # only to the platform administrator
        f"CREATE POLICY read_global_or_own ON shifde.audit_events FOR SELECT USING {TENANT_OR_PLATFORM}",
        f"CREATE POLICY write_own ON shifde.audit_events FOR INSERT WITH CHECK {TENANT_OR_PLATFORM}",
        # append-only: INSERT + SELECT only
        "GRANT SELECT, INSERT ON shifde.audit_events TO shifde_app",
    )


def downgrade() -> None:
    _run("DROP TABLE shifde.audit_events")
