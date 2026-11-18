"""Tenancy: the app role may update a client's settings (findings Item 13, J-M2).

Revision ID: tenancy_0006
Revises: tenancy_0005

* Findings Item 13, J-M2 (B-2550): `shifde_app` gets a column-level `UPDATE (settings)` on
  `shifde.tenants`, so `PUT /orgs/{org}/capture-content` can store a client's `capture_content`.
  Row-level security still confines the write to the client in context; who may write is decided
  in the tenancy service (`tenant:admin`, audited).

Rollback: revokes the column grant.
"""

from __future__ import annotations

from alembic import op

revision = "tenancy_0006"
down_revision = "tenancy_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("GRANT UPDATE (settings) ON shifde.tenants TO shifde_app")


def downgrade() -> None:
    op.get_bind().exec_driver_sql("REVOKE UPDATE (settings) ON shifde.tenants FROM shifde_app")
