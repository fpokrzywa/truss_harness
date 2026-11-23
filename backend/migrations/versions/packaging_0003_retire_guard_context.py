"""Packaging, part 3: the retire guard refuses a caller outside the version's client context.

Revision ID: packaging_0003
Revises: packaging_0002

Issue #158 (follow-up of job 065 / issue #93, item 2; B-2742; DECISIONS B-2994, B-2995).

`shifde.artifact_versions_release_guard()` (packaging_0002) reads `releases` under the caller's
forced row-level security. A caller with no client context (for example the schema owner on a
cluster where it is not BYPASSRLS) saw no releases and could retire a version a sealed release
pins. The replaced body keeps invoker rights (no SECURITY DEFINER), `SET search_path =
pg_catalog, pg_temp` and every object schema-qualified, and refuses a change to `retired`
(`check_violation`, 23514) when:

1. a sealed release of the version's client, read under the caller's row-level security, names
   the version as its manifest's entrypoint or among its pins ("a sealed release pins this
   version"): the packaging_0002 check, unchanged; or
2. `shifde.current_tenant_id()` is unset or is not `NEW.tenant_id` ("retiring a version needs
   its client context").

Both refusals are 23514 and the union is what the issue asks for: a retire outside the version's
client context is always refused, and one inside it is refused exactly when packaging_0002
refused it. The pinned check runs first so a caller that can see the pinning release (a BYPASSRLS
owner) keeps the packaging_0002 message (B-2995); a caller that cannot see it gets the context
refusal. Neither message names another client's release or version.

The trigger `trg_artifact_versions_release_guard` (BEFORE UPDATE OF state, only on a change to
`retired`) is unchanged. `CREATE OR REPLACE` keeps the function's owner and ACL; EXECUTE is
revoked from PUBLIC again.

Rollback: restores the packaging_0002 body (a caller without a client context can again retire
an unpinned version, and a pinned one when row-level security hides the release).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "packaging_0003"
down_revision = "packaging_0002"
branch_labels = None
depends_on = None

RETIRE_SIGNATURE = "artifact_versions_release_guard()"

RETIRE_GUARD = """CREATE OR REPLACE FUNCTION shifde.artifact_versions_release_guard()
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
        IF shifde.current_tenant_id() IS DISTINCT FROM NEW.tenant_id THEN
          RAISE EXCEPTION 'retiring a version needs its client context'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END $$"""


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
    guard: str = _previous("packaging_0002_releases").RETIRE_GUARD
    if not guard.startswith("CREATE FUNCTION"):  # pragma: no cover - the packaging_0002 body
        raise RuntimeError("packaging_0002 retire guard body not recognised")
    return guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    _run(RETIRE_GUARD, f"REVOKE ALL ON FUNCTION shifde.{RETIRE_SIGNATURE} FROM PUBLIC")


def downgrade() -> None:
    _run(_old_guard(), f"REVOKE ALL ON FUNCTION shifde.{RETIRE_SIGNATURE} FROM PUBLIC")
