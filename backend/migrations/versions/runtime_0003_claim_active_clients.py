"""Runtime: the queue claim skips the jobs of a client that is not active (findings Item 12, F-03).

Revision ID: runtime_0003
Revises: runtime_0002

Lead isolation review of F-03 (DECISIONS B-2512): `shifde_claim_jobs` leased queued jobs of every
client, so a run queued before its client was suspended (or archived) still ran. The function is
replaced with the same signature, owner, grants (CREATE OR REPLACE keeps them), `search_path`,
client-context refusal and body, except that a candidate job must belong to a client whose
`tenants.status` is `active`. A job of a suspended or archived client stays `queued` and is
claimed again once the client is active; nothing about the job or its run is changed or failed.
The check names only `active`, so it needs no particular tenancy revision (`tenants` exists from
`tenancy_0001`, an ancestor of `runtime_0001`).

Rollback: restores the runtime_0001 body (jobs of every client are claimed again).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "runtime_0003"
down_revision = "runtime_0002"
branch_labels = None
depends_on = None

_ALL_CLIENTS = "     WHERE j.status = 'queued' AND j.run_after <= now()\n"
_ACTIVE_CLIENTS = (
    "     WHERE j.status = 'queued' AND j.run_after <= now()\n"
    "       AND EXISTS (SELECT 1 FROM shifde.tenants t\n"
    "                    WHERE t.id = j.tenant_id AND t.status = 'active')\n"
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


def _claim(active_only: bool) -> str:
    claim: str = _previous("runtime_0001_runtime").CLAIM
    if _ALL_CLIENTS not in claim:  # pragma: no cover - the runtime_0001 body is fixed
        raise RuntimeError("runtime_0001 claim body not recognised")
    if active_only:
        claim = claim.replace(_ALL_CLIENTS, _ACTIVE_CLIENTS, 1)
    return claim.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    _run(_claim(active_only=True))


def downgrade() -> None:
    _run(_claim(active_only=False))
