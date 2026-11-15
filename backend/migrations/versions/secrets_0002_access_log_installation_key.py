"""Secrets: the access log's plain secret key applies to installation rows only.

Revision ID: secrets_0002
Revises: secrets_0001

Secrets isolation review 1 item 1 (DECISIONS B-1645). `secret_access_log_secret_id_fkey`
(`secret_id -> secrets (id)`, SETH's) fired before the composite `fk_secret_access_log__secret`
(`(tenant_id, secret_id) -> secrets (tenant_id, id)`, B-801): a client's raw INSERT naming
another client's secret id failed on the composite key, an unknown id on the plain one, which
told the two apart (constraints are checked without row-level security). The plain key is
replaced by one that checks installation rows (tenant_id NULL) only, against installation
secrets only, through two stored generated columns that are the id on installation rows and
NULL otherwise:

* `secrets.installation_secret_id` (unique, `uq_secrets__installation_secret_id`);
* `secret_access_log.installation_secret_id` with
  `fk_secret_access_log__installation_secret -> secrets (installation_secret_id)`.

A client's row (tenant_id set) is now checked by the composite key alone, so B's id and an
unknown id give the same constraint. No grant changes: generated columns are never written and
the table-level SELECT covers them.

Rollback: drops the new key and columns and restores SETH's plain key (non-destructive).
"""

from __future__ import annotations

from alembic import op

revision = "secrets_0002"
down_revision = "secrets_0001"
branch_labels = None
depends_on = None


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        "ALTER TABLE shifde.secrets ADD COLUMN installation_secret_id uuid "
        "GENERATED ALWAYS AS (CASE WHEN tenant_id IS NULL THEN id END) STORED",
        "ALTER TABLE shifde.secrets ADD CONSTRAINT uq_secrets__installation_secret_id "
        "UNIQUE (installation_secret_id)",
        "ALTER TABLE shifde.secret_access_log ADD COLUMN installation_secret_id uuid "
        "GENERATED ALWAYS AS (CASE WHEN tenant_id IS NULL THEN secret_id END) STORED",
        "ALTER TABLE shifde.secret_access_log DROP CONSTRAINT secret_access_log_secret_id_fkey",
        "ALTER TABLE shifde.secret_access_log ADD CONSTRAINT "
        "fk_secret_access_log__installation_secret FOREIGN KEY (installation_secret_id) "
        "REFERENCES shifde.secrets (installation_secret_id)",
    )


def downgrade() -> None:
    _run(
        "ALTER TABLE shifde.secret_access_log DROP CONSTRAINT "
        "fk_secret_access_log__installation_secret",
        "ALTER TABLE shifde.secret_access_log ADD CONSTRAINT secret_access_log_secret_id_fkey "
        "FOREIGN KEY (secret_id) REFERENCES shifde.secrets (id)",
        "ALTER TABLE shifde.secret_access_log DROP COLUMN installation_secret_id",
        "ALTER TABLE shifde.secrets DROP CONSTRAINT uq_secrets__installation_secret_id",
        "ALTER TABLE shifde.secrets DROP COLUMN installation_secret_id",
    )
