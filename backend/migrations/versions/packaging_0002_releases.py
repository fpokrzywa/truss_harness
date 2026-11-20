"""Packaging, part 2: sealed releases, their guard, and the retire guard on pinned build versions.

Revision ID: packaging_0002
Revises: packaging_0001

Sub-project 3 design sections 4 and 5, settled calls 11 and 13 (issue #79).

* `releases`: a sealed, immutable release of one solution version (composite key
  `fk_releases__solution_version` to `solution_versions (tenant_id, id)`, `ON DELETE RESTRICT`).
  It stores the release manifest, the compatibility report it passed and the manifest's sha256
  `digest`, unique per client (`uq_releases__digest`: sealing the same manifest again finds the
  existing row). `status` is `sealed` or `withdrawn`.
* `shifde.releases_guard()`: a SECURITY INVOKER `BEFORE INSERT OR UPDATE OR DELETE` trigger,
  `SET search_path = pg_catalog, pg_temp`, every object schema-qualified, EXECUTE revoked from
  PUBLIC. An insert must be `sealed`; the only update is `sealed -> withdrawn` with every other
  column but `updated_at` unchanged; a delete is always refused. Every refusal is
  `check_violation`, for the schema owner too.
* `shifde.artifact_versions_release_guard()`: the same function rules, a `BEFORE UPDATE OF state`
  trigger on `artifact_versions` that runs only when a version becomes `retired`. It refuses
  (`check_violation`, "a sealed release pins this version") while a sealed release of the
  version's client, read under the caller's row-level security, names the version as its
  manifest's entrypoint or among its manifest's pins.

Isolation: `releases` is client-owned with forced row-level security and one `tenant_isolation`
policy (USING and WITH CHECK on `tenant_id = current_tenant_id()`); its key to
`solution_versions` is composite `(tenant_id, ...)`. Grants to `shifde_app`: SELECT, INSERT and
UPDATE (status) only; no DELETE. No SECURITY DEFINER function, no global table.

Rollback: drops the retire trigger and its function, the releases trigger and its function, then
the table (data-destructive: every organization's releases are lost).
"""

from __future__ import annotations

from alembic import op

revision = "packaging_0002"
down_revision = "packaging_0001"
branch_labels = None
depends_on = None

TENANT = "tenant_id = shifde.current_tenant_id()"
GUARD_SIGNATURE = "releases_guard()"
RETIRE_SIGNATURE = "artifact_versions_release_guard()"

GUARD = """CREATE FUNCTION shifde.releases_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'releases cannot be deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF TG_OP = 'INSERT' THEN
          IF NEW.status IS DISTINCT FROM 'sealed' THEN
            RAISE EXCEPTION 'a release is written sealed' USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END IF;
        IF OLD.status <> 'sealed' OR NEW.status IS DISTINCT FROM 'withdrawn'
           OR (NEW.tenant_id, NEW.id, NEW.solution_version_id, NEW.digest, NEW.manifest,
               NEW.compatibility, NEW.sealed_by, NEW.sealed_at, NEW.created_at)
              IS DISTINCT FROM
              (OLD.tenant_id, OLD.id, OLD.solution_version_id, OLD.digest, OLD.manifest,
               OLD.compatibility, OLD.sealed_by, OLD.sealed_at, OLD.created_at) THEN
          RAISE EXCEPTION 'a sealed release can only be withdrawn'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""

RETIRE_GUARD = """CREATE FUNCTION shifde.artifact_versions_release_guard()
   RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
   $$ BEGIN
        IF EXISTS (
          SELECT 1 FROM shifde.releases r
           WHERE r.tenant_id = NEW.tenant_id AND r.status = 'sealed'
             AND (r.manifest -> 'entrypoint' ->> 'version_id' = NEW.id::text
                  OR r.manifest -> 'pins' @> pg_catalog.jsonb_build_array(
                       pg_catalog.jsonb_build_object('version_id', NEW.id::text)))) THEN
          RAISE EXCEPTION 'a sealed release pins this version' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
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


def upgrade() -> None:
    _run(
        """CREATE TABLE shifde.releases (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            id uuid PRIMARY KEY,
            solution_version_id uuid NOT NULL,
            digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
            manifest jsonb NOT NULL,
            compatibility jsonb NOT NULL,
            status text NOT NULL DEFAULT 'sealed' CHECK (status IN ('sealed', 'withdrawn')),
            sealed_by uuid NOT NULL,
            sealed_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            CONSTRAINT uq_releases__tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_releases__digest UNIQUE (tenant_id, digest),
            CONSTRAINT fk_releases__solution_version FOREIGN KEY (tenant_id, solution_version_id)
                REFERENCES shifde.solution_versions (tenant_id, id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX ix_releases__solution_version ON shifde.releases "
        "(tenant_id, solution_version_id)",
        GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{GUARD_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_releases_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON shifde.releases FOR EACH ROW EXECUTE FUNCTION shifde.releases_guard()",
        *_tenant_table("releases"),
        "GRANT SELECT, INSERT ON shifde.releases TO shifde_app",
        "GRANT UPDATE (status) ON shifde.releases TO shifde_app",
        RETIRE_GUARD,
        f"REVOKE ALL ON FUNCTION shifde.{RETIRE_SIGNATURE} FROM PUBLIC",
        "CREATE TRIGGER trg_artifact_versions_release_guard BEFORE UPDATE OF state "
        "ON shifde.artifact_versions FOR EACH ROW "
        "WHEN (NEW.state = 'retired' AND OLD.state IS DISTINCT FROM 'retired') "
        "EXECUTE FUNCTION shifde.artifact_versions_release_guard()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER trg_artifact_versions_release_guard ON shifde.artifact_versions",
        f"DROP FUNCTION shifde.{RETIRE_SIGNATURE}",
        "DROP TRIGGER trg_releases_guard ON shifde.releases",
        f"DROP FUNCTION shifde.{GUARD_SIGNATURE}",
        "DROP TABLE shifde.releases",
    )
