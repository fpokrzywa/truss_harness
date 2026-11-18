"""Conformance evidence: the only input to adapter maturity (`adapter_conformance_runs`).

Reads work with the app role (global read-only table). `record_run` needs the owner role: the app
role has no INSERT grant, so no API path can fabricate evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any  # Any: JSON check lists / environment documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7


@dataclass(frozen=True)
class ConformanceRun:
    adapter_key: str
    adapter_version: str
    suite_version: str
    started_at: datetime
    finished_at: datetime
    outcome: str  # pass | fail
    validated_capabilities: tuple[str, ...] = ()
    checks: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid7)


_COLUMNS = (
    "id, adapter_key, adapter_version, suite_version, started_at, finished_at, outcome, "
    "validated_capabilities, checks, environment"
)


def _row(r: Any) -> ConformanceRun:  # Any: SQLAlchemy Row
    return ConformanceRun(
        id=r[0],
        adapter_key=r[1],
        adapter_version=r[2],
        suite_version=r[3],
        started_at=r[4],
        finished_at=r[5],
        outcome=r[6],
        validated_capabilities=tuple(r[7] or ()),
        checks=list(r[8] or []),
        environment=dict(r[9] or {}),
    )


def latest_runs(session: Session) -> dict[str, ConformanceRun]:
    """The most recent run (any outcome) per adapter."""
    rows = session.execute(
        text(
            # columns are module constants; values are bound
            f"SELECT DISTINCT ON (adapter_key) {_COLUMNS} FROM "  # nosec B608
            "shifde.adapter_conformance_runs "
            "ORDER BY adapter_key, finished_at DESC, id DESC"
        )
    ).all()
    return {r[1]: _row(r) for r in rows}


def list_runs(session: Session, adapter_key: str, limit: int = 50) -> list[ConformanceRun]:
    rows = session.execute(
        text(
            # columns are module constants; values are bound
            f"SELECT {_COLUMNS} FROM shifde.adapter_conformance_runs WHERE "  # nosec B608
            "adapter_key = :k "
            "ORDER BY finished_at DESC, id DESC LIMIT :n"
        ),
        {"k": adapter_key, "n": limit},
    ).all()
    return [_row(r) for r in rows]


def record_run(session: Session, run: ConformanceRun) -> None:
    """Append evidence (owner role only)."""
    import json

    session.execute(
        text(
            "INSERT INTO shifde.adapter_conformance_runs "
            "(id, adapter_key, adapter_version, suite_version, started_at, finished_at, outcome, "
            "validated_capabilities, checks, environment) VALUES (:id, :k, :av, :sv, :s, :f, :o, "
            ":caps, CAST(:checks AS jsonb), CAST(:env AS jsonb))"
        ),
        {
            "id": run.id,
            "k": run.adapter_key,
            "av": run.adapter_version,
            "sv": run.suite_version,
            "s": run.started_at,
            "f": run.finished_at,
            "o": run.outcome,
            "caps": list(run.validated_capabilities),
            "checks": json.dumps(run.checks),
            "env": json.dumps(run.environment),
        },
    )
