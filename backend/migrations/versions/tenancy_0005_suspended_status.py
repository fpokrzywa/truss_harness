"""Tenancy: a suspended client status (findings Item 12, F-03).

Revision ID: tenancy_0005
Revises: tenancy_0004

* Findings Item 12, F-03 (B-2511): `shifde.tenants.status` admits `suspended` beside `active` and
  `archived`. Only the platform administrator suspends or unsuspends a client. No function
  changes: `shifde_resolve_session` already resolves a pinned session only for an `active`
  client, so a suspended client's pinned sessions are refused like an archived client's.

Rollback: restores the two-value CHECK under the same name; it fails while any client is
`suspended` (rows are never rewritten).
"""

from __future__ import annotations

from alembic import op

revision = "tenancy_0005"
down_revision = "tenancy_0004"
branch_labels = None
depends_on = None


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _status_check(values: str) -> tuple[str, str]:
    return (
        "ALTER TABLE shifde.tenants DROP CONSTRAINT tenants_status_check",
        "ALTER TABLE shifde.tenants ADD CONSTRAINT tenants_status_check"
        f" CHECK (status IN ({values}))",
    )


def upgrade() -> None:
    _run(*_status_check("'active', 'suspended', 'archived'"))


def downgrade() -> None:
    _run(*_status_check("'active', 'archived'"))
