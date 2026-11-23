"""Audit: index `audit_events` on (tenant_id, correlation_id).

Revision ID: audit_0002
Revises: audit_0001

Issue #183 (lead isolation review of PR #180, item 4; DECISIONS B-2993). The model-provider job
result lookup `audit.repository.find_event` has no time bound since #133 (B-2861): it filters one
client's events on action, target and correlation id. The `(tenant_id, occurred_at)` index only
narrowed it to the client, so every job status read and every worker run scanned that client's
whole audit history. `ix_audit_events__tenant_id_correlation_id` leads with `tenant_id` like every
other index of the table, so the lookup stays inside one client's rows.

Isolation: an index only; no policy, grant, function or trigger changes. Row-level security on
`audit_events` is unchanged (forced, `read_global_or_own` / `write_own`).

Rollback: drops the index (no data is lost; the lookup falls back to the tenant index).
"""

from __future__ import annotations

from alembic import op

revision = "audit_0002"
down_revision = "audit_0001"
branch_labels = None
depends_on = None

INDEX = "ix_audit_events__tenant_id_correlation_id"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"CREATE INDEX {INDEX} ON shifde.audit_events (tenant_id, correlation_id)"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(f"DROP INDEX shifde.{INDEX}")
