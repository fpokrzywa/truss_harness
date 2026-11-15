"""Ontology: source mappings, version info, access policies, principal attributes, lineage,
materializations.

Revision ID: ontology_0001
Revises: base_0001
Depends on: build_0002 (`artifacts`, `artifact_versions`, and through it `tenants`),
connections_0001 (`connections`)

Taken from SETH 3ba0e06 `p07_0001_ontology` in its final shape (no later migration, p08 to p11,
changes these tables; the permission keys of `p07_0002` are seeded by `authz_0001`). The
`ontologies` artifact kind, the `ontology` / `action_tool` pin roles and the `ontology_version`
reference type are already in `build_0001`.

Differences from SETH, each listed in `tests/schema/deliberate_changes.yaml`:

* `uq_ontology_mappings__target` and `uq_ontology_materializations__type` start with `tenant_id`,
  and the primary key of `ontology_version_info` is `(tenant_id, version_id)` (DECISIONS B-1905,
  B-2001): with a key on the version id alone, an `INSERT ... ON CONFLICT` under client A's
  context could conflict with client B's row (a silent outcome where an unknown id gives
  23503) and wait on B's uncommitted insert.
* `ontology_query_lineage` has no foreign key to `shifde.runs` yet: `runs` belongs to the runtime
  module, whose migration adds `fk_ontology_lineage__run`.

`ontology_mappings_guard` pins `search_path` and has EXECUTE revoked from PUBLIC (trigger
function, invoker rights, every object schema-qualified).

Not here: the `runs` columns and foreign keys of `p07_0001` (`ontology_id`, `ontology_version_id`,
`action_key`, `fk_runs__ontology_of_version`, `ix_runs__ontology`, the widened `runs` CHECKs)
belong to the runtime migration.

Rollback: drops the six ontology tables and the guard function (data-destructive: every
client's mappings, policies and lineage); back up first.
"""

from __future__ import annotations

from alembic import op

revision = "ontology_0001"
down_revision = "base_0001"
branch_labels = ("ontology",)
depends_on = ("build_0002", "connections_0001")


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _tenant_table(table: str) -> list[str]:
    return [
        f"ALTER TABLE shifde.{table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE shifde.{table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON shifde.{table} "
        "USING (tenant_id = shifde.current_tenant_id()) "
        "WITH CHECK (tenant_id = shifde.current_tenant_id())",
    ]


MAPPING_GUARD = """CREATE FUNCTION shifde.ontology_mappings_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS
$$
DECLARE
  v_state text;
  v_kind text;
BEGIN
  SELECT v.state, v.kind INTO v_state, v_kind FROM shifde.artifact_versions v
   WHERE v.tenant_id = COALESCE(NEW.tenant_id, OLD.tenant_id)
     AND v.id = COALESCE(NEW.ontology_version_id, OLD.ontology_version_id);
  IF v_kind IS DISTINCT FROM 'ontologies' THEN
    RAISE EXCEPTION 'mappings belong to ontology versions' USING ERRCODE = '23514';
  END IF;
  IF v_state IS DISTINCT FROM 'draft' THEN
    RAISE EXCEPTION 'the mappings of a published ontology version are frozen'
      USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id
     OR NEW.ontology_version_id <> OLD.ontology_version_id
     OR NEW.target_kind <> OLD.target_kind OR NEW.target_key <> OLD.target_key) THEN
    RAISE EXCEPTION 'mapping identity columns are immutable' USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$"""

DROP_ORDER = [
    "ontology_materializations",
    "ontology_query_lineage",
    "ontology_principal_attributes",
    "ontology_access_policies",
    "ontology_version_info",
    "ontology_source_mappings",
]


def upgrade() -> None:
    _run(
        # ------------------------------------------------------------------ mappings
        """CREATE TABLE shifde.ontology_source_mappings (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            ontology_version_id uuid NOT NULL,
            target_kind text NOT NULL CHECK (target_kind IN ('object_type', 'relationship')),
            target_key text NOT NULL CHECK (length(target_key) BETWEEN 1 AND 200),
            connection_id uuid NOT NULL,
            relation_schema text NOT NULL DEFAULT '' CHECK (length(relation_schema) <= 128),
            relation_name text NOT NULL CHECK (length(relation_name) BETWEEN 1 AND 128),
            column_map jsonb NOT NULL DEFAULT '{}'
                CHECK (jsonb_typeof(column_map) = 'object'),
            key_map jsonb CHECK (key_map IS NULL OR jsonb_typeof(key_map) = 'object'),
            filters jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(filters) = 'array'),
            status text NOT NULL DEFAULT 'unchecked'
                CHECK (status IN ('unchecked', 'ok', 'invalid')),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT uq_ontology_mappings__target
                UNIQUE (tenant_id, ontology_version_id, target_kind, target_key),
            CONSTRAINT fk_ontology_mappings__version FOREIGN KEY (tenant_id, ontology_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id),
            CONSTRAINT fk_ontology_mappings__connection FOREIGN KEY (tenant_id, connection_id)
                REFERENCES shifde.connections (tenant_id, id)
        )""",
        "CREATE INDEX ix_ontology_mappings__connection ON shifde.ontology_source_mappings "
        "(tenant_id, connection_id)",
        MAPPING_GUARD,
        "REVOKE EXECUTE ON FUNCTION shifde.ontology_mappings_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_ontology_mappings_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON shifde.ontology_source_mappings FOR EACH ROW "
        "EXECUTE FUNCTION shifde.ontology_mappings_guard()",
        *_tenant_table("ontology_source_mappings"),
        "GRANT SELECT, INSERT, DELETE ON shifde.ontology_source_mappings TO shifde_app",
        "GRANT UPDATE (connection_id, relation_schema, relation_name, column_map, key_map, "
        "filters, status, updated_at, row_version) "
        "ON shifde.ontology_source_mappings TO shifde_app",
        # ------------------------------------------------------------------ version info
        """CREATE TABLE shifde.ontology_version_info (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            version_id uuid NOT NULL,
            mappings_hash text NOT NULL,
            report jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, version_id),
            CONSTRAINT fk_ontology_info__version FOREIGN KEY (tenant_id, version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE TRIGGER trg_ontology_info_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.ontology_version_info FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("ontology_version_info"),
        "GRANT SELECT, INSERT ON shifde.ontology_version_info TO shifde_app",
        # ------------------------------------------------------------------ policies
        """CREATE TABLE shifde.ontology_access_policies (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            ontology_id uuid NOT NULL,
            kind text NOT NULL DEFAULT 'ontologies' CHECK (kind = 'ontologies'),
            object_type_key text NOT NULL CHECK (length(object_type_key) BETWEEN 1 AND 200),
            property_keys text[] CHECK (property_keys IS NULL
                OR cardinality(property_keys) BETWEEN 1 AND 500),
            subject_type text NOT NULL CHECK (subject_type IN ('role', 'group', 'user')),
            subject_id uuid NOT NULL,
            effect text NOT NULL DEFAULT 'allow' CHECK (effect = 'allow'),
            row_filter text CHECK (row_filter IS NULL OR length(row_filter) BETWEEN 1 AND 1000),
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz,
            row_version int NOT NULL DEFAULT 1,
            CONSTRAINT fk_ontology_policies__ontology FOREIGN KEY (tenant_id, ontology_id, kind)
                REFERENCES shifde.artifacts (tenant_id, id, kind)
        )""",
        "CREATE INDEX ix_ontology_policies__ontology ON shifde.ontology_access_policies "
        "(tenant_id, ontology_id, object_type_key)",
        "CREATE TRIGGER trg_ontology_policies_updated_at BEFORE UPDATE "
        "ON shifde.ontology_access_policies FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        *_tenant_table("ontology_access_policies"),
        "GRANT SELECT, INSERT, DELETE ON shifde.ontology_access_policies TO shifde_app",
        "GRANT UPDATE (object_type_key, property_keys, subject_type, subject_id, row_filter, "
        "updated_at, row_version) ON shifde.ontology_access_policies TO shifde_app",
        # ------------------------------------------------------------------ principal attributes
        """CREATE TABLE shifde.ontology_principal_attributes (
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            principal_type text NOT NULL CHECK (principal_type IN ('user', 'service_account')),
            principal_id uuid NOT NULL,
            attributes jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'
                AND octet_length(attributes::text) <= 16384),
            updated_by uuid,
            updated_at timestamptz NOT NULL DEFAULT now(),
            row_version int NOT NULL DEFAULT 1,
            PRIMARY KEY (tenant_id, principal_type, principal_id)
        )""",
        *_tenant_table("ontology_principal_attributes"),
        "GRANT SELECT, INSERT ON shifde.ontology_principal_attributes TO shifde_app",
        "GRANT UPDATE (attributes, updated_by, updated_at, row_version) "
        "ON shifde.ontology_principal_attributes TO shifde_app",
        # ------------------------------------------------------------------ lineage
        # no foreign key to shifde.runs (runtime module): the runtime migration adds it
        """CREATE TABLE shifde.ontology_query_lineage (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            at timestamptz NOT NULL DEFAULT now(),
            principal_id uuid NOT NULL,
            principal_type text NOT NULL CHECK (principal_type IN ('user', 'service_account')),
            ontology_id uuid NOT NULL,
            ontology_version_id uuid NOT NULL,
            run_id uuid,
            channel text NOT NULL CHECK (channel IN ('api', 'pipeline', 'agent', 'action')),
            object_types text[] NOT NULL,
            connection_ids uuid[] NOT NULL,
            compiled_query_hashes text[] NOT NULL,
            row_counts jsonb NOT NULL DEFAULT '{}',
            duration_ms int NOT NULL CHECK (duration_ms >= 0),
            outcome text NOT NULL CHECK (outcome IN ('success', 'failed')),
            error_code text,
            CONSTRAINT fk_ontology_lineage__version FOREIGN KEY (tenant_id, ontology_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        "CREATE INDEX ix_ontology_lineage__ontology ON shifde.ontology_query_lineage "
        "(tenant_id, ontology_id, id DESC)",
        "CREATE INDEX ix_ontology_lineage__run ON shifde.ontology_query_lineage "
        "(tenant_id, run_id) WHERE run_id IS NOT NULL",
        "CREATE TRIGGER trg_ontology_lineage_append_only BEFORE UPDATE OR DELETE "
        "ON shifde.ontology_query_lineage FOR EACH ROW EXECUTE FUNCTION shifde.reject_mutation()",
        *_tenant_table("ontology_query_lineage"),
        "GRANT SELECT, INSERT ON shifde.ontology_query_lineage TO shifde_app",
        # ------------------------------------------------------------------ materializations
        """CREATE TABLE shifde.ontology_materializations (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES shifde.tenants (id),
            ontology_version_id uuid NOT NULL,
            object_type_key text NOT NULL,
            mode text NOT NULL DEFAULT 'off' CHECK (mode IN ('off', 'snapshot', 'cache')),
            schedule text,
            retention_days int CHECK (retention_days IS NULL OR retention_days > 0),
            approved_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_ontology_materializations__type
                UNIQUE (tenant_id, ontology_version_id, object_type_key),
            CONSTRAINT fk_ontology_materializations__version
                FOREIGN KEY (tenant_id, ontology_version_id)
                REFERENCES shifde.artifact_versions (tenant_id, id)
        )""",
        *_tenant_table("ontology_materializations"),
        "GRANT SELECT ON shifde.ontology_materializations TO shifde_app",
    )


def downgrade() -> None:
    _run(
        *(f"DROP TABLE IF EXISTS shifde.{table} CASCADE" for table in DROP_ORDER),
        "DROP FUNCTION IF EXISTS shifde.ontology_mappings_guard()",
    )
