"""Delivery, part 1: deployment targets, platform claims, the claim definer and the delivery
permission keys.

Revision ID: delivery_0001
Revises: base_0001
Depends on: tenancy_0004 (`tenants`, `workspaces`, `projects`) and identity_0006; authz_0002
(`permissions`, `roles`, `role_permissions`) is an ancestor of both through identity_0005. Naming
it as well makes this revision an alembic merge of three heads, and `alembic upgrade heads` from
an empty database then fails in identity_0001 for most hash seeds (KeyError 'base_0001').

Design 2026-11-17 sections 3 and 9 (DECISIONS B-2500, B-2501), lead ruling B-2523.

* `delivery_platform_claims`: one row per (organization, platform kind, address key). The address
  key is the sha256 of the normalized platform address (`adapters.native.addresses`), so no
  address sits in an index or row another organization could read back. Its only unique key is
  `uq_delivery_platform_claims__tenant_address (tenant_id, platform_kind, address_key)`.
* `delivery_targets`: a Databricks deployment target of a workspace (optionally a project). An
  active target names its organization's claim (`ck_delivery_targets__claim`); an archived one
  names none. Several active targets of one organization may share a claim.
* `shifde.delivery_claim_platform(text, text)` (B-2523): the only path that writes a claim. A
  SECURITY DEFINER owned by `shifde_owner` (BYPASSRLS), `SET search_path = pg_catalog, pg_temp`,
  every `shifde` object schema-qualified, EXECUTE revoked from PUBLIC and granted to `shifde_app`
  only, no write of the `app.*` context. The organization and principal come from the session
  context only (refused, 42501, without both). Under `pg_advisory_xact_lock` on a hash of
  (platform kind, address key) it returns the caller's own claim (locked FOR UPDATE), or NULL when
  any other organization holds one (nothing about which), or inserts and returns a new claim for
  the caller. The lock is held to the end of the transaction, so two organizations racing for one
  address are serialized and exactly one gets the claim.

Isolation: both tables are client-owned with forced row-level security and one
`tenant_isolation` policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); keys between
them and to `workspaces` / `projects` are composite `(tenant_id, ...)`. Grants to `shifde_app`:
claims SELECT and DELETE (no INSERT: the definer writes claims; no UPDATE); targets SELECT, INSERT
and UPDATE (status, claim_id, revision) only. Archiving the last active target of a claim deletes
the claim under the caller's row-level security, after taking the same advisory lock.

Permissions: nine `delivery:*` keys; `tenant_administrator` holds all nine, `developer` the
target read/write, plan read/request, deployment read and executor read keys; no other role.

Rollback: removes every `delivery:*` role grant (custom roles included) and permission, drops the
definer and both tables (data-destructive: every organization's targets and claims are lost).
"""

from __future__ import annotations

from alembic import op

revision = "delivery_0001"
down_revision = "base_0001"
branch_labels = ("delivery",)
depends_on = ("tenancy_0004", "identity_0006")  # authz_0002 through identity_0005

TENANT = "tenant_id = shifde.current_tenant_id()"
TENANT_ADMINISTRATOR = "01900000-0000-7000-8000-000000000001"
DEVELOPER = "01900000-0000-7000-8000-000000000004"
CLAIM_SIGNATURE = "delivery_claim_platform(text, text)"

# key, resource type, action, description, is_privileged
PERMISSIONS: list[tuple[str, str, str, str, bool]] = [
    ("delivery:target:read", "delivery_target", "read", "View deployment targets", False),
    ("delivery:target:write", "delivery_target", "write", "Register and archive targets", False),
    ("delivery:plan:read", "delivery_plan", "read", "View deployment plans", False),
    ("delivery:plan:request", "delivery_plan", "request", "Request deployment plans", False),
    ("delivery:plan:approve", "delivery_plan", "approve", "Approve deployment plans", True),
    ("delivery:deployment:read", "delivery_deployment", "read", "View deployments", False),
    (
        "delivery:deployment:resolve",
        "delivery_deployment",
        "resolve",
        "Resolve deployments that need attention",
        True,
    ),
    ("delivery:executor:read", "delivery_executor", "read", "View delivery executors", False),
    ("delivery:executor:manage", "delivery_executor", "manage", "Manage delivery executors", True),
]
DEVELOPER_KEYS = (
    "delivery:target:read",
    "delivery:target:write",
    "delivery:plan:read",
    "delivery:plan:request",
    "delivery:deployment:read",
    "delivery:executor:read",
)

CLAIM_PLATFORM = """CREATE FUNCTION shifde.delivery_claim_platform(
       p_platform_kind text, p_address_key text)
   RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
   SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE
        v_tenant uuid := shifde.current_tenant_id();
        v_principal uuid := shifde.current_principal_id();
        v_claim uuid;
      BEGIN
        IF v_tenant IS NULL OR v_principal IS NULL THEN
          RAISE EXCEPTION 'platform claim refused' USING ERRCODE = 'insufficient_privilege';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
          p_platform_kind || ':' || p_address_key, 0));
        SELECT c.id INTO v_claim FROM shifde.delivery_platform_claims c
         WHERE c.tenant_id = v_tenant AND c.platform_kind = p_platform_kind
           AND c.address_key = p_address_key
           FOR UPDATE;
        IF FOUND THEN
          RETURN v_claim;
        END IF;
        IF EXISTS (SELECT 1 FROM shifde.delivery_platform_claims c
                    WHERE c.platform_kind = p_platform_kind
                      AND c.address_key = p_address_key) THEN
          RETURN NULL;
        END IF;
        v_claim := pg_catalog.gen_random_uuid();
        INSERT INTO shifde.delivery_platform_claims
          (id, tenant_id, platform_kind, address_key, created_by)
        VALUES (v_claim, v_tenant, p_platform_kind, p_address_key, v_principal);
        RETURN v_claim;
      END $$"""


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


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ claims
        """CREATE TABLE shifde.delivery_platform_claims (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            platform_kind text NOT NULL CHECK (platform_kind = 'databricks'),
            address_key text NOT NULL CHECK (address_key ~ '^[0-9a-f]{64}$'),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_delivery_platform_claims__tenant_address
                UNIQUE (tenant_id, platform_kind, address_key),
            CONSTRAINT uq_delivery_platform_claims__tenant_id_id UNIQUE (tenant_id, id)
        )""",
        *_tenant_table("delivery_platform_claims"),
        "GRANT SELECT, DELETE ON shifde.delivery_platform_claims TO shifde_app",
        CLAIM_PLATFORM,
        f"REVOKE ALL ON FUNCTION shifde.{CLAIM_SIGNATURE} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION shifde.{CLAIM_SIGNATURE} TO shifde_app",
        # ------------------------------------------------------------------ targets
        """CREATE TABLE shifde.delivery_targets (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            project_id uuid,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            platform_kind text NOT NULL CHECK (platform_kind = 'databricks'),
            address text NOT NULL,
            claim_id uuid,
            environment text NOT NULL CHECK (environment IN ('dev', 'test', 'prod')),
            bindings jsonb NOT NULL,
            credential_ref text NOT NULL
                CHECK (credential_ref ~ '^[A-Za-z][A-Za-z0-9_.-]{0,127}$'),
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            revision int NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT ck_delivery_targets__claim
                CHECK ((status = 'active') = (claim_id IS NOT NULL)),
            CONSTRAINT uq_delivery_targets__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_delivery_targets__workspace FOREIGN KEY (tenant_id, workspace_id)
                REFERENCES shifde.workspaces (tenant_id, id),
            CONSTRAINT fk_delivery_targets__project FOREIGN KEY (tenant_id, project_id)
                REFERENCES shifde.projects (tenant_id, id),
            CONSTRAINT fk_delivery_targets__claim FOREIGN KEY (tenant_id, claim_id)
                REFERENCES shifde.delivery_platform_claims (tenant_id, id)
        )""",
        "CREATE INDEX ix_delivery_targets__claim ON shifde.delivery_targets (tenant_id, claim_id)",
        *_tenant_table("delivery_targets"),
        "GRANT SELECT, INSERT ON shifde.delivery_targets TO shifde_app",
        "GRANT UPDATE (status, claim_id, revision) ON shifde.delivery_targets TO shifde_app",
    )
    for key, resource_type, action, description, privileged in PERMISSIONS:
        _run(
            "INSERT INTO shifde.permissions (key, resource_type, action, description, "
            f"is_privileged) VALUES ('{key}', '{resource_type}', '{action}', '{description}', "
            f"{_sql_bool(privileged)})"
        )
    grants = [(TENANT_ADMINISTRATOR, key) for key, *_ in PERMISSIONS]
    grants += [(DEVELOPER, key) for key in DEVELOPER_KEYS]
    for role_id, key in grants:
        _run(
            "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
            f"VALUES ('{role_id}', '{key}', NULL)"
        )


def downgrade() -> None:
    _run(
        "DELETE FROM shifde.role_permissions WHERE starts_with(permission_key, 'delivery:')",
        "DELETE FROM shifde.permissions WHERE starts_with(key, 'delivery:')",
        "DROP TABLE shifde.delivery_targets",
        f"DROP FUNCTION shifde.{CLAIM_SIGNATURE}",
        "DROP TABLE shifde.delivery_platform_claims",
    )
