"""Runtime: a run's end no longer releases a budget hold whose provider call may have started.

Revision ID: runtime_0004
Revises: runtime_0003
Depends on: ledger_0003 (the `provider_request_at` marker)

Sub-project 3 design 7.1 and 12.2 (issue #82). Step 3 of `shifde.settle_ended_run(uuid)` released
every `held` reservation of the ended run. The reaper calls it inside the database (through
`shifde_reap_expired_leases`) before any Python runs, so a hold whose model call may have reached
the provider was freed before the worker's orphan handler could charge it. The function is
replaced with the same signature, SECURITY INVOKER, `search_path`, owner, grants (CREATE OR REPLACE
keeps them) and body, except that step 3 releases only held holds with `provider_request_at IS
NULL`. A held hold that carries the marker stays `held` (still counting, never expiring) until the
runtime's orphan handler turns it into `settled_unknown` with its usage row. No SECURITY DEFINER
function is changed.

Rollback: restores the runtime_0001 body (every held hold of an ended run is released again).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "runtime_0004"
down_revision = "runtime_0003"
branch_labels = None
depends_on = ("ledger_0003",)

_ALL_HOLDS = (
    "  UPDATE shifde.budget_reservations SET status = 'released'\n"
    "   WHERE run_id = p_run AND tenant_id = r.tenant_id AND status = 'held';\n"
)
_UNSTARTED_HOLDS = (
    "  UPDATE shifde.budget_reservations SET status = 'released'\n"
    "   WHERE run_id = p_run AND tenant_id = r.tenant_id AND status = 'held'\n"
    "     AND provider_request_at IS NULL;\n"
)


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


def _settle(unstarted_only: bool) -> str:
    settle: str = _previous("runtime_0001_runtime").SETTLE
    if _ALL_HOLDS not in settle:  # pragma: no cover - the runtime_0001 body is fixed
        raise RuntimeError("runtime_0001 settle body not recognised")
    if unstarted_only:
        settle = settle.replace(_ALL_HOLDS, _UNSTARTED_HOLDS, 1)
    return settle.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    _run(_settle(unstarted_only=True))


def downgrade() -> None:
    _run(_settle(unstarted_only=False))
