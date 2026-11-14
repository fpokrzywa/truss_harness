"""The pool checkout hook runs DISCARD ALL (B-114), which drops server-side prepared statements.
psycopg prepares a statement after its fifth execution on a connection and kept using the dropped
one after a commit, so the first repeated statement of the next checkout failed with "prepared
statement does not exist" (found by the ledger port, DECISIONS B-603). The pool therefore never
prepares statements."""

from __future__ import annotations

from sqlalchemy import text

from shifde.core.db import Database


def test_a_repeated_statement_works_on_the_next_checkout_after_a_commit(app_url: str) -> None:
    db = Database(app_url, pool_size=1, max_overflow=0)  # one connection: reuse is guaranteed
    try:
        for _ in range(4):
            with db.session() as s:
                for i in range(8):  # past psycopg's default prepare threshold of 5
                    assert s.execute(text("SELECT :x"), {"x": i}).scalar_one() == i
                s.commit()
    finally:
        db.dispose()
