"""Allowlist members keyed by their client.

Revision ID: models_admin_0003
Revises: models_admin_0002

models_admin isolation review 1 item 7 (DECISIONS B-1735): `model_allowlist_models` had
`PRIMARY KEY (allowlist_id, model_id)` without `tenant_id`, so a writer holding two of another
client's ids could tell them apart (23505 against 23503). The key is now
`(tenant_id, allowlist_id, model_id)`; the composite keys to `model_allowlists` and `models` are
unchanged.

Rollback: restores the part-1 key (every row stays unique under it: allowlist ids are global).
"""

from __future__ import annotations

from alembic import op

revision = "models_admin_0003"
down_revision = "models_admin_0002"
branch_labels = None
depends_on = None


def _key(columns: str) -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE shifde.model_allowlist_models DROP CONSTRAINT model_allowlist_models_pkey, "
        f"ADD CONSTRAINT model_allowlist_models_pkey PRIMARY KEY ({columns})"
    )


def upgrade() -> None:
    _key("tenant_id, allowlist_id, model_id")


def downgrade() -> None:
    _key("allowlist_id, model_id")
