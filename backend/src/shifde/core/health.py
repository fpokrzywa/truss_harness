"""Database / migration state probes shared by /api/ready and /api/v1/platform/status."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shifde.core.db import read_db_revisions
from shifde.core.migrations import code_heads


@dataclass(frozen=True)
class MigrationState:
    status: str  # ok | pending | error
    revision: str | None
    head: str


def database_reachable(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


def migration_state(session: Session) -> MigrationState:
    """`revision` and `head` list every branch head, comma-separated and sorted (B-111)."""
    heads = code_heads()
    head = ",".join(sorted(heads))
    try:
        revisions = read_db_revisions(session)
    except SQLAlchemyError:
        session.rollback()
        return MigrationState("error", None, head)
    revision = ",".join(sorted(revisions)) or None
    return MigrationState("ok" if revisions == heads else "pending", revision, head)
