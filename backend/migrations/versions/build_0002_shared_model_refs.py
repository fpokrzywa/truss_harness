"""Build: prompt test runs and pipeline refs may name a shared SHI model.

Revision ID: build_0002
Revises: build_0001

`build_0001` keyed `prompt_test_runs.model_id` and `pipeline_version_refs.model_id` to
`models (tenant_id, id)`. A shared SHI model has `models.tenant_id` NULL (design 5.3), which a
composite key never matches, so the row of a prompt test that used a shared model could not be
stored (the SHI call was made, then the route answered 500; DECISIONS B-1938) and a pipeline
could not pin one (B-1940).

Both tables keep `model_id` (no key on it) and gain `own_model_id uuid` with
`CHECK (own_model_id IS NULL OR own_model_id = model_id)` and the composite key
`(tenant_id, own_model_id) -> models (tenant_id, id)` under the old key's name (MATCH SIMPLE: NULL
is not checked). `trg_build_model_ref_guard` (BEFORE INSERT OR UPDATE OF model_id, own_model_id,
invoker rights, so it applies to the schema owner too: B-116 item 9) always sets `own_model_id`:
NULL for a NULL `model_id`, the model id for the client's own model, NULL for a shared model, and
refuses with 23503 'model not found' when no `models` row with that id is the client's own or
shared. Another client's model and an unknown id give the same error, and the trigger runs before
the key is checked, so there is no lock wait on another client's uncommitted `models` row. The
client's own models keep full referential integrity; a shared model is guarded by the trigger only
(the platform administrator can delete it: B-1940). Whether the caller's client may use a shared
model is the service's decision at call time (the SHI switch, B-1732).

Rollback: drops the column, check, trigger and function and restores the composite keys on
`(tenant_id, model_id)`. It fails (23503) while a row names a shared model; delete those rows first.
"""

from __future__ import annotations

from alembic import op

revision = "build_0002"
down_revision = "build_0001"
branch_labels = None
depends_on = None

TABLES = ("prompt_test_runs", "pipeline_version_refs")
KEYS = {
    "prompt_test_runs": "fk_prompt_test_runs__model",
    "pipeline_version_refs": "fk_pipeline_refs__model",
}
TRIGGER = "trg_build_model_ref_guard"

GUARD = """CREATE FUNCTION shifde.build_model_ref_guard() RETURNS trigger
   LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS
   $$ DECLARE owner_tenant uuid; found boolean;
   BEGIN
     IF NEW.model_id IS NULL THEN NEW.own_model_id := NULL; RETURN NEW; END IF;
     SELECT true, m.tenant_id INTO found, owner_tenant FROM shifde.models m
       WHERE m.id = NEW.model_id AND (m.tenant_id IS NULL OR m.tenant_id = NEW.tenant_id);
     IF found IS NOT TRUE THEN
       RAISE EXCEPTION 'model not found' USING ERRCODE = '23503';
     END IF;
     NEW.own_model_id := CASE WHEN owner_tenant IS NULL THEN NULL ELSE NEW.model_id END;
     RETURN NEW;
   END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    statements = [GUARD, "REVOKE ALL ON FUNCTION shifde.build_model_ref_guard() FROM PUBLIC"]
    for table in TABLES:
        statements += [
            f"ALTER TABLE shifde.{table} DROP CONSTRAINT {KEYS[table]}",
            f"ALTER TABLE shifde.{table} ADD COLUMN own_model_id uuid",
            f"UPDATE shifde.{table} SET own_model_id = model_id",
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT ck_{table}__own_model "
            "CHECK (own_model_id IS NULL OR own_model_id = model_id)",
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT {KEYS[table]} "
            "FOREIGN KEY (tenant_id, own_model_id) REFERENCES shifde.models (tenant_id, id)",
            f"CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE OF model_id, own_model_id "
            f"ON shifde.{table} FOR EACH ROW EXECUTE FUNCTION shifde.build_model_ref_guard()",
        ]
    _run(*statements)


def downgrade() -> None:
    statements = []
    for table in TABLES:
        statements += [
            f"DROP TRIGGER {TRIGGER} ON shifde.{table}",
            f"ALTER TABLE shifde.{table} DROP CONSTRAINT {KEYS[table]}",
            f"ALTER TABLE shifde.{table} DROP CONSTRAINT ck_{table}__own_model",
            f"ALTER TABLE shifde.{table} DROP COLUMN own_model_id",
            f"ALTER TABLE shifde.{table} ADD CONSTRAINT {KEYS[table]} "
            "FOREIGN KEY (tenant_id, model_id) REFERENCES shifde.models (tenant_id, id)",
        ]
    statements.append("DROP FUNCTION shifde.build_model_ref_guard()")
    _run(*statements)
