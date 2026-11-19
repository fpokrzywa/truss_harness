"""Delivery, part 2: executor identities, their credentials and the executor resolver definer.

Revision ID: delivery_0002
Revises: delivery_0001

Design 2026-11-17 sections 3 and 5, section 9 items 3, 4, 12, 13 and 15 (DECISIONS B-2500,
B-2501, B-2664..B-2667).

* `delivery_executors`: an organization's executor identity. At most one `active` executor per
  organization (`uq_delivery_executors__one_active`, partial unique index on `tenant_id`); a
  disabled one stays as history and may be re-activated only while no other is active.
* `delivery_executor_credentials`: bearer tokens `shx_<prefix8>_<secret43>` of an executor. Only
  the prefix and `HMAC-SHA256(pepper, full token)` (`identity.apikeys.hash_key`) are stored, never
  the secret or the token. The prefix is unique per `(tenant_id, prefix)`, not globally (a unique
  key on a client table must include `tenant_id`, B-2665); the resolver matches prefix and hash.
* `shifde.delivery_resolve_executor(text, bytea)`: executor authentication happens before any
  client context exists. A SECURITY DEFINER owned by `shifde_owner` (BYPASSRLS),
  `SET search_path = pg_catalog, pg_temp`, every `shifde` object schema-qualified, EXECUTE revoked
  from PUBLIC and granted to `shifde_app` only, no write of the `app.*` context. It returns
  `(tenant_id, executor_id)` only for exactly one credential matching prefix and hash that is
  unrevoked and unexpired, of an active executor of an active organization; otherwise no row, and
  never a name, status or reason. The caller tries each pepper of the ring.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); the key from
credentials to executors is composite `(tenant_id, executor_id)`. Grants to `shifde_app`:
executors SELECT, INSERT and UPDATE (status, last_seen_at, version) only; credentials SELECT on
every column except `secret_hash` (only the definer reads the hash), INSERT and UPDATE
(revoked_at) only.

Rollback: drops the definer and both tables, credentials first (data-destructive: every
organization's executors and credentials are lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0002"
down_revision = "delivery_0001"
branch_labels = None
depends_on = None

TENANT = "tenant_id = shifde.current_tenant_id()"
RESOLVE_SIGNATURE = "delivery_resolve_executor(text, bytea)"
CREDENTIAL_READ_COLUMNS = (
    "tenant_id, id, executor_id, prefix, created_by, expires_at, revoked_at, created_at, updated_at"
)

RESOLVE_EXECUTOR = """CREATE FUNCTION shifde.delivery_resolve_executor(
       p_prefix text, p_hash bytea)
   RETURNS TABLE (tenant_id uuid, executor_id uuid)
   LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS
   $$ SELECT c.tenant_id, c.executor_id
        FROM shifde.delivery_executor_credentials c
        JOIN shifde.delivery_executors e ON e.tenant_id = c.tenant_id AND e.id = c.executor_id
        JOIN shifde.tenants t ON t.id = c.tenant_id
       WHERE c.prefix = p_prefix AND c.secret_hash = p_hash
         AND c.revoked_at IS NULL AND c.expires_at > pg_catalog.now()
         AND e.status = 'active' AND t.status = 'active'
         AND (SELECT pg_catalog.count(*) FROM shifde.delivery_executor_credentials x
               WHERE x.prefix = p_prefix AND x.secret_hash = p_hash) = 1 $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON shifde.{table} "
        "FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} FOR ALL USING ({TENANT}) "
        f"WITH CHECK ({TENANT})",
    ]


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ executors
        """CREATE TABLE shifde.delivery_executors (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            last_seen_at timestamptz,
            version text CHECK (length(version) <= 64),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_delivery_executors__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        "CREATE UNIQUE INDEX uq_delivery_executors__one_active ON shifde.delivery_executors "
        "(tenant_id) WHERE status = 'active'",
        *_tenant_table("delivery_executors"),
        "GRANT SELECT, INSERT ON shifde.delivery_executors TO shifde_app",
        "GRANT UPDATE (status, last_seen_at, version) ON shifde.delivery_executors TO shifde_app",
        # ------------------------------------------------------------------ credentials
        """CREATE TABLE shifde.delivery_executor_credentials (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            executor_id uuid NOT NULL,
            prefix text NOT NULL CHECK (prefix ~ '^[0-9A-Za-z]{8}$'),
            secret_hash bytea NOT NULL CHECK (length(secret_hash) = 32),
            created_by uuid NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_delivery_executor_credentials__tenant_prefix UNIQUE (tenant_id, prefix),
            CONSTRAINT uq_delivery_executor_credentials__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_delivery_executor_credentials__executor FOREIGN KEY
                (tenant_id, executor_id) REFERENCES shifde.delivery_executors (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_executor_credentials__prefix "
        "ON shifde.delivery_executor_credentials (prefix)",
        "CREATE INDEX ix_delivery_executor_credentials__executor "
        "ON shifde.delivery_executor_credentials (tenant_id, executor_id)",
        *_tenant_table("delivery_executor_credentials"),
        f"GRANT SELECT ({CREDENTIAL_READ_COLUMNS}) ON shifde.delivery_executor_credentials "
        "TO shifde_app",
        "GRANT INSERT ON shifde.delivery_executor_credentials TO shifde_app",
        "GRANT UPDATE (revoked_at) ON shifde.delivery_executor_credentials TO shifde_app",
        # ------------------------------------------------------------------ resolver
        RESOLVE_EXECUTOR,
        f"REVOKE ALL ON FUNCTION shifde.{RESOLVE_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION shifde.{RESOLVE_SIGNATURE} TO shifde_app",
    )


def downgrade() -> None:
    _run(
        f"DROP FUNCTION shifde.{RESOLVE_SIGNATURE}",
        "DROP TABLE shifde.delivery_executor_credentials",
        "DROP TABLE shifde.delivery_executors",
    )
