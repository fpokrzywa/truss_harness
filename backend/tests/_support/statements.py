"""A statement log of one database role: every statement it sends and every row it reads back.

The `before_cursor_execute` listener of `tests/ontology/test_service.py`, lifted into a context
manager, plus the rows: psycopg's `Cursor.fetchone` / `fetchmany` / `fetchall` (which SQLAlchemy
calls for every result) are wrapped while the log is open, so a test can assert that no row
another client owns was ever returned to that role, whichever thread ran the query.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any  # Any: DBAPI rows and parameters

import psycopg
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass
class StatementLog:
    statements: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def values(self) -> set[str]:
        """Every value of every row read, as text."""
        return {str(v) for row in self.rows for v in row}


@contextmanager
def statement_log(role: str) -> Iterator[StatementLog]:
    log = StatementLog()
    patch = pytest.MonkeyPatch()

    def before(conn: Any, cursor: Any, statement: str, *_: Any) -> None:  # Any: DBAPI objects
        if cursor.connection.info.user == role:
            log.statements.append(statement)

    def wrap(name: str) -> None:
        original = getattr(psycopg.Cursor, name)

        def fetch(self: psycopg.Cursor[Any], *args: Any, **kwargs: Any) -> Any:
            out = original(self, *args, **kwargs)
            if self.connection.info.user == role and out is not None:
                log.rows.extend(out if isinstance(out, list) else [out])
            return out

        patch.setattr(psycopg.Cursor, name, fetch)

    for name in ("fetchone", "fetchmany", "fetchall"):
        wrap(name)
    event.listen(Engine, "before_cursor_execute", before)
    try:
        yield log
    finally:
        event.remove(Engine, "before_cursor_execute", before)
        patch.undo()
