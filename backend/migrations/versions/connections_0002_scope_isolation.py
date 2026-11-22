"""Connections: names unique per scope; a connection's secret has the connection's scope.

Revision ID: connections_0002
Revises: connections_0001
Depends on: tenancy_0001 (`tenants`), secrets_0001 (`secrets.scope_type`, `secrets.scope_id`)

Connection management space (#156, design 2026-11-22 section 5, DECISIONS B-2743, B-2744):

* Names are unique within a connection's scope, not per client: `uq_connections__tenant_name`
  `UNIQUE (tenant_id, name)` is replaced by the unique index `uq_connections__scope_name` on
  `(tenant_id, workspace or zero, project or zero, name)`.
* `connections_guard()` (invoker rights, so row-level security applies inside it; never SECURITY
  DEFINER) keeps the three `connections_0001` checks unchanged and in the same order (another
  client's secret keeps its message), then requires the bound secret's scope to equal the
  connection's: a project connection takes a `project` secret of that project, a workspace
  connection a `workspace` secret of that workspace, a client connection a `tenant` secret
  (23514 'secret scope must match the connection scope'). The function's pinned
  `search_path` (models_admin_0002, B-1714) is re-applied after the body is replaced.
* `trg_connections_guard` also fires on `workspace_id` and `project_id` (defense in depth:
  `shifde_app` has no UPDATE on either). Grants are unchanged.

Precheck (B-2743): `tenants` and every client table are under forced row-level security, so a
count taken with no client context would pass vacuously. The upgrade lists every client under the
platform-administrator context, then per client sets `app.tenant_id` (transaction-local
`set_config`) and counts the connections that bind a secret of another scope. If any exist the
upgrade is refused naming only the count (never ids or names); fix or rebind them first.

Rollback: restores the exact `connections_0001` guard body, trigger and `UNIQUE (tenant_id, name)`
constraint. Re-adding the constraint fails if same-named connections now exist in two scopes of
one client; rename them first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "connections_0002"
down_revision = "connections_0001"
branch_labels = None
depends_on = ("tenancy_0001", "secrets_0001")

ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"

# Every client's connections whose bound secret's scope differs from the connection's. Runs inside
# the per-client loop of PRECHECK, with `app.tenant_id` set to that client.
_SCOPE_MATCH = (
    "((c.project_id IS NOT NULL AND s.scope_type = 'project' AND s.scope_id = c.project_id)"
    " OR (c.project_id IS NULL AND c.workspace_id IS NOT NULL"
    " AND s.scope_type = 'workspace' AND s.scope_id = c.workspace_id)"
    " OR (c.workspace_id IS NULL AND s.scope_type = 'tenant'))"
)

PRECHECK = f"""DO $$
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
      FROM shifde.connections c
      JOIN shifde.secrets s ON s.tenant_id = c.tenant_id AND s.id = c.secret_ref_id
     WHERE c.tenant_id = t AND c.secret_ref_id IS NOT NULL AND NOT {_SCOPE_MATCH};
    n := n + k;
  END LOOP;
  PERFORM pg_catalog.set_config('app.platform_admin', '', true);
  PERFORM pg_catalog.set_config('app.tenant_id', '', true);
  IF n > 0 THEN
    RAISE EXCEPTION 'connections_0002: %% connection(s) bind a secret of another scope', n;
  END IF;
END $$"""

_SCOPE_CHECK = """     IF NEW.secret_ref_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM shifde.secrets s
         WHERE s.id = NEW.secret_ref_id AND s.tenant_id = NEW.tenant_id
           AND ((NEW.project_id IS NOT NULL AND s.scope_type = 'project'
                 AND s.scope_id = NEW.project_id)
             OR (NEW.project_id IS NULL AND NEW.workspace_id IS NOT NULL
                 AND s.scope_type = 'workspace' AND s.scope_id = NEW.workspace_id)
             OR (NEW.workspace_id IS NULL AND s.scope_type = 'tenant')))
     THEN RAISE EXCEPTION 'secret scope must match the connection scope'
          USING ERRCODE = '23514'; END IF;
"""
_RETURN = "     RETURN NEW;\n"

_OLD_TRIGGER = (
    "CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF secret_ref_id, "
    "egress_policy_id, vendor_id ON shifde.connections FOR EACH ROW "
    "EXECUTE FUNCTION shifde.connections_guard()"
)
_NEW_TRIGGER = (
    "CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF secret_ref_id, "
    "egress_policy_id, vendor_id, workspace_id, project_id ON shifde.connections FOR EACH ROW "
    "EXECUTE FUNCTION shifde.connections_guard()"
)
_REVOKE = "REVOKE ALL ON FUNCTION shifde.connections_guard() FROM PUBLIC"


def _run(*statements: str) -> None:
    conn = op.get_bind()
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def _previous(module: str) -> ModuleType:
    """An earlier migration of this directory, for its function bodies."""
    spec = importlib.util.spec_from_file_location(
        f"_shifde_prev_{module}", Path(__file__).with_name(f"{module}.py")
    )
    if spec is None or spec.loader is None:  # pragma: no cover - file sits next to this one
        raise RuntimeError(f"cannot load {module}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _old_guard() -> str:
    guard: str = _previous("connections_0001_connections").CONNECTIONS_GUARD
    return guard


def _new_guard() -> str:
    guard = _old_guard()
    if guard.count(_RETURN) != 1:  # pragma: no cover - the connections_0001 body is fixed
        raise RuntimeError("connections_0001 guard body not recognised")
    guard = guard.replace(_RETURN, _SCOPE_CHECK + _RETURN, 1)
    return guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def _function_settings() -> list[str]:
    """`connections_guard`'s pinned settings as ALTER statements, to re-apply after the function is
    replaced (CREATE OR REPLACE and DROP/CREATE both reset them). models_admin_0002 pins
    `search_path = pg_catalog, shifde, pg_temp` (B-1714); it may run before or after this revision,
    so the settings are read from the catalog rather than assumed."""
    config = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT proconfig FROM pg_catalog.pg_proc "
            "WHERE oid = 'shifde.connections_guard()'::regprocedure"
        )
        .scalar_one()
    )
    statements = []
    for entry in config or ():
        key, _, value = str(entry).partition("=")
        if key != "search_path":  # pragma: no cover - only models_admin_0002 sets one
            raise RuntimeError(f"connections_guard setting {key!r} not recognised")
        statements.append(f"ALTER FUNCTION shifde.connections_guard() SET search_path = {value}")
    return statements


def upgrade() -> None:
    settings = _function_settings()
    _run(
        PRECHECK,
        "ALTER TABLE shifde.connections DROP CONSTRAINT uq_connections__tenant_name",
        "CREATE UNIQUE INDEX uq_connections__scope_name ON shifde.connections "
        f"(tenant_id, COALESCE(workspace_id, {ZERO}), COALESCE(project_id, {ZERO}), name)",
        _new_guard(),
        *settings,
        _REVOKE,
        "DROP TRIGGER trg_connections_guard ON shifde.connections",
        _NEW_TRIGGER,
    )


def downgrade() -> None:
    settings = _function_settings()
    _run(
        "DROP TRIGGER trg_connections_guard ON shifde.connections",
        "DROP FUNCTION shifde.connections_guard()",
        _old_guard(),
        *settings,
        _REVOKE,
        _OLD_TRIGGER,
        "DROP INDEX shifde.uq_connections__scope_name",
        "ALTER TABLE shifde.connections ADD CONSTRAINT uq_connections__tenant_name "
        "UNIQUE (tenant_id, name)",
    )
