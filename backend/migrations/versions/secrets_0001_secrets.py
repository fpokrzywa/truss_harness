"""Secrets: envelope-encrypted client credentials and their access log.

Revision ID: secrets_0001
Revises: base_0001

SETH 3ba0e06's final schema for `secrets` and `secret_access_log`, from
`p02_0002_secrets_idp_audit` (no later SETH migration alters either table; `p03_0001` only adds
`connections.secret_ref_id`, which the connections port owns). Policy `tenant_or_platform` is
SETH's: a client's rows in its own context, installation-level rows (tenant_id NULL, e.g. an
installation IdP client secret) only under the platform-administrator context. Grants are SETH's:
SELECT, INSERT and UPDATE of the rotation/status columns only (no DELETE: SEC Minor-3, a known
defect; no UPDATE of tenant_id, name or scope).

Deliberate differences from SETH (DECISIONS B-801, tests/schema/deliberate_changes.yaml):
* `uq_secrets__tenant_id_id UNIQUE (tenant_id, id)` and the composite foreign key
  `fk_secret_access_log__secret (tenant_id, secret_id) -> secrets (tenant_id, id)`: constraints
  are checked without row-level security, and SETH's plain `secret_access_log_secret_id_fkey`
  alone accepted a client's access-log row naming another client's secret (B-116 item 9). SETH's
  plain key is kept: the composite one does not check installation-level rows (tenant_id NULL).
* `tenant_id -> tenants(id)` on both tables is not created: tenancy is not ported yet; its
  migration adds them (docs/build/carry_forward.md).

Rollback: drops both tables (data-destructive: every stored secret is lost). Back up first.
"""

from __future__ import annotations

from alembic import op

revision = "secrets_0001"
down_revision = "base_0001"
branch_labels = ("secrets",)
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
        """CREATE TABLE shifde.secrets (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            kind text NOT NULL DEFAULT 'generic',
            provider_key text NOT NULL DEFAULT 'db_encrypted' CHECK (provider_key IN ('db_encrypted', 'env')),
            external_ref text,
            scope_type text NOT NULL DEFAULT 'tenant' CHECK (scope_type IN ('tenant', 'workspace', 'project')),
            scope_id uuid,
            ciphertext bytea,
            wrapped_dek bytea,
            nonce bytea,
            kek_id text,
            secret_version int NOT NULL DEFAULT 1,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            expires_at timestamptz,
            rotated_at timestamptz,
            last_used_at timestamptz,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT ck_secrets__scope CHECK ((scope_type = 'tenant') = (scope_id IS NULL)),
            CONSTRAINT ck_secrets__material CHECK (
                (provider_key = 'db_encrypted' AND ciphertext IS NOT NULL AND wrapped_dek IS NOT NULL
                 AND nonce IS NOT NULL AND kek_id IS NOT NULL)
                OR (provider_key = 'env' AND external_ref IS NOT NULL)),
            CONSTRAINT uq_secrets__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_secrets__tenant_name ON shifde.secrets "
        "(COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), name)",
        "CREATE TRIGGER trg_secrets_updated_at BEFORE UPDATE ON shifde.secrets "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        "ALTER TABLE shifde.secrets ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.secrets FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_or_platform ON shifde.secrets USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
        "GRANT SELECT, INSERT ON shifde.secrets TO shifde_app",
        "GRANT UPDATE (ciphertext, wrapped_dek, nonce, kek_id, secret_version, rotated_at, "
        "last_used_at, status, row_version) ON shifde.secrets TO shifde_app",
        """CREATE TABLE shifde.secret_access_log (
            id uuid PRIMARY KEY,
            tenant_id uuid,
            secret_id uuid NOT NULL REFERENCES shifde.secrets (id),
            at timestamptz NOT NULL DEFAULT now(),
            principal_id uuid,
            purpose text NOT NULL,
            run_id uuid,
            outcome text NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
            CONSTRAINT fk_secret_access_log__secret FOREIGN KEY (tenant_id, secret_id)
                REFERENCES shifde.secrets (tenant_id, id)
        )""",
        "CREATE INDEX ix_secret_access_log__secret ON shifde.secret_access_log (secret_id, at DESC)",
        "ALTER TABLE shifde.secret_access_log ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE shifde.secret_access_log FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_or_platform ON shifde.secret_access_log USING {TENANT_OR_PLATFORM} "
        f"WITH CHECK {TENANT_OR_PLATFORM}",
        "GRANT SELECT, INSERT ON shifde.secret_access_log TO shifde_app",
        "CREATE TRIGGER trg_secret_access_log_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.secret_access_log FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
    )


def downgrade() -> None:
    _run(
        "DROP TABLE shifde.secret_access_log",
        "DROP TABLE shifde.secrets",
    )
