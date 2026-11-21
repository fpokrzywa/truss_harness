"""Runtime: capture off also scrubs the tool arguments copied into a run's approvals (issue #33).

Revision ID: runtime_0005
Revises: runtime_0004

With `capture_content` off the runtime scrubs an ended run family's artifacts and tool invocation
arguments (runtime_0002, DECISIONS B-2514), but the copy of the arguments in `approvals.arguments`
stayed. `shifde_app` gets `UPDATE (arguments)` on `shifde.approvals`, and the body of
`shifde.approvals_guard()` is replaced (CREATE OR REPLACE keeps the trigger `trg_approvals_guard`,
the owner and the revoked PUBLIC execute). The guard still refuses every update of a non-pending
approval except one: `arguments` set to `{"scrubbed": true}` with every other column unchanged, on
an approval whose run (matched on the row's own tenant_id, run_id) is terminal. A pending approval
behaves as before: its id, tenant_id, run_id and arguments cannot change. The guard keeps the
caller's rights (row-level security applies to its read of `runs`), its fixed search_path and its
SQLSTATE 23514.

Deliberate difference from SETH (`tests/schema/deliberate_changes.yaml`): the `arguments` column
grant of `approvals`.

Rollback: revokes the column grant and restores the runtime_0001 guard body (a scrubbed row stays
scrubbed).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "runtime_0005"
down_revision = "runtime_0004"
branch_labels = None
depends_on = None

GUARD = """CREATE OR REPLACE FUNCTION shifde.approvals_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS
$$ BEGIN
  IF OLD.status <> 'pending' THEN
    IF NEW.arguments = '{"scrubbed": true}'::jsonb
       AND to_jsonb(NEW) - 'arguments' = to_jsonb(OLD) - 'arguments'
       AND EXISTS (SELECT 1 FROM shifde.runs r
                    WHERE r.id = OLD.run_id AND r.tenant_id = OLD.tenant_id
                      AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out')) THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'a decided approval is final' USING ERRCODE = '23514';
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.run_id <> OLD.run_id
     OR NEW.arguments IS DISTINCT FROM OLD.arguments THEN
    RAISE EXCEPTION 'approval identity columns are immutable' USING ERRCODE = '23514';
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


def upgrade() -> None:
    _run(
        "GRANT UPDATE (arguments) ON shifde.approvals TO shifde_app",
        GUARD,
    )


def downgrade() -> None:
    guard: str = _previous("runtime_0001_runtime").APPROVALS_GUARD
    _run(
        "REVOKE UPDATE (arguments) ON shifde.approvals FROM shifde_app",
        guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1),
    )
