"""Model providers: a client's provider uses a client-wide connection.

Revision ID: models_admin_0004
Revises: models_admin_0003
Depends on: connections_0001 (`connections.workspace_id`)

Connection management space (#156, design 2026-11-22 section 5, DECISIONS B-2743, B-2754): a
client-level model provider cannot carry a workspace's or project's credential. The new trigger
`trg_model_providers_connection_scope` (BEFORE INSERT OR UPDATE OF connection_id, tenant_id)
runs `model_providers_connection_guard()` (invoker rights, so row-level security applies inside
it; never SECURITY DEFINER), which refuses with 23514 a provider whose connection is a workspace
or project connection (`workspace_id IS NOT NULL`) of the provider's own client. Like the other
guards (models_admin_0002, B-1714) it pins `search_path = pg_catalog, shifde, pg_temp`. No route
updates `connection_id` today; the trigger covers any future PATCH. The existing
`trg_model_providers_guard` (vendor check) is unchanged.

The guard refuses a connection it finds scoped rather than requiring that it find a client-wide
one (the design's wording): the composite key `fk_model_providers__connection` already requires
the connection to exist in the provider's client, so both forms admit exactly the same rows, and
this one leaves an unknown id or another client's connection to the refusals they had before
(42501 from row-level security, 23503 from the key; tests/models_admin/test_cross_client.py)
instead of turning them into this trigger's 23514.

Precheck (B-2743): `tenants` and every client table are under forced row-level security, so a
count taken with no client context would pass vacuously. The upgrade lists every client under the
platform-administrator context, then per client sets `app.tenant_id` (transaction-local
`set_config`) and counts the providers over a workspace or project connection. If any exist the
upgrade is refused naming only the count (never ids or names).

Rollback: drops the trigger, then the function.
"""

from __future__ import annotations

from alembic import op

revision = "models_admin_0004"
down_revision = "models_admin_0003"
branch_labels = None
depends_on = ("connections_0001",)

PRECHECK = """DO $$
DECLARE
  clients uuid[];
  t uuid;
  n bigint := 0;
  k bigint;
BEGIN
  PERFORM pg_catalog.set_config('app.platform_admin', 'on', true);
  clients := ARRAY(SELECT id FROM shifde.tenants);
  FOREACH t IN ARRAY clients LOOP
    PERFORM pg_catalog.set_config('app.platform_admin', '', true);
    PERFORM pg_catalog.set_config('app.tenant_id', t::text, true);
    SELECT count(*) INTO k
      FROM shifde.model_providers p
      JOIN shifde.connections c ON c.tenant_id = p.tenant_id AND c.id = p.connection_id
     WHERE p.tenant_id = t AND p.connection_id IS NOT NULL AND c.workspace_id IS NOT NULL;
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION
      'models_admin_0004: %% model provider(s) use a workspace or project connection', n;
  END IF;
END $$"""

GUARD = """CREATE FUNCTION shifde.model_providers_connection_guard() RETURNS trigger
   LANGUAGE plpgsql AS
   $$ BEGIN
     IF NEW.connection_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM shifde.connections c
         WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.connection_id
           AND c.workspace_id IS NOT NULL)
     THEN RAISE EXCEPTION 'a model provider needs a client-wide connection'
          USING ERRCODE = '23514'; END IF;
     RETURN NEW;
   END $$"""


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def upgrade() -> None:
    _run(
        PRECHECK,
        GUARD,
        "ALTER FUNCTION shifde.model_providers_connection_guard() "
        "SET search_path = pg_catalog, shifde, pg_temp",
        "REVOKE ALL ON FUNCTION shifde.model_providers_connection_guard() FROM PUBLIC",
        "CREATE TRIGGER trg_model_providers_connection_scope BEFORE INSERT OR UPDATE OF "
        "connection_id, tenant_id ON shifde.model_providers FOR EACH ROW "
        "EXECUTE FUNCTION shifde.model_providers_connection_guard()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER trg_model_providers_connection_scope ON shifde.model_providers",
        "DROP FUNCTION shifde.model_providers_connection_guard()",
    )
