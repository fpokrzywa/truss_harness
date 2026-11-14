"""Authz: a client role cannot hold a platform-only permission.

Revision ID: authz_0002
Revises: authz_0001

`platform:admin` administers the whole installation, every client included; it comes only from
`users.is_platform_admin`, never from a role (DECISIONS B-406, authz isolation review 1, follow-up
1). SETH 3ba0e06 let a client put it into its own custom role (`role_permissions.tenant_id` NOT
NULL); the PDP refused it in `authorize` but reported it from `effective_permissions`,
`missing_permissions` and `visible_scopes`. This CHECK refuses such a row for every role, the app
role and the schema owner alike. The shared system templates (`tenant_id` NULL; SETH's
`platform_administrator` holds `platform:admin`) are not affected.

The key list is frozen here (it equals `shifde.authz.pdp.PLATFORM_ONLY_ACTIONS` at this revision;
`tests/authz/test_platform_only.py` checks every key of that set against this constraint). Adding a
platform-only action later needs a migration replacing the constraint.

Upgrade fails (and changes nothing) if a client role already holds a platform-only permission:
such a row must be reviewed and removed by an operator, not silently dropped.

Rollback: drops the constraint (no data change).
"""

from __future__ import annotations

from alembic import op

revision = "authz_0002"
down_revision = "authz_0001"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_role_permissions__no_platform_only_in_client_role"
PLATFORM_ONLY_KEYS = ("platform:admin",)


def upgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in PLATFORM_ONLY_KEYS)
    op.get_bind().exec_driver_sql(
        f"ALTER TABLE shifde.role_permissions ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK (tenant_id IS NULL OR permission_key NOT IN ({keys}))"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"ALTER TABLE shifde.role_permissions DROP CONSTRAINT IF EXISTS {CONSTRAINT}"
    )
