"""Every table holding client data has row-level security enabled AND forced, and a policy.

Applies to every table in every non-system schema (SETH's tables live in `shifde`; `public` is
checked too so a table created there by mistake cannot escape), except Alembic's version table
and the tables listed, with a reason, in `global_tables.txt` (Review Focus item 3).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from tests.schema.snapshot import load_lines

GLOBAL_TABLES_FILE = Path(__file__).with_name("global_tables.txt")
EXEMPT = frozenset({"alembic_version"})

_TABLES = text(
    r"""SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
              (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
        ORDER BY n.nspname, c.relname"""
)


def global_tables() -> set[str]:
    entries: set[str] = set()
    for raw in GLOBAL_TABLES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, reason = line.partition("#")
        if not reason.strip():
            raise AssertionError(f"global_tables.txt: {name.strip()!r} has no reason")
        entries.add(name.strip())
    return entries


def rls_violations(conn: Connection, allowed: set[str]) -> list[str]:
    """`schema.table: problem` for every table that breaks the rule."""
    problems: list[str] = []
    for schema, table, enabled, forced, policies in conn.execute(_TABLES):
        if table in EXEMPT or (schema == "shifde" and table in allowed):
            continue
        missing = [
            what
            for what, ok in (
                ("row level security not enabled", enabled),
                ("row level security not forced", forced),
                ("no policy", policies > 0),
            )
            if not ok
        ]
        if missing:
            problems.append(f"{schema}.{table}: {', '.join(missing)}")
    return problems


def test_every_client_table_has_forced_rls_and_a_policy(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        problems = rls_violations(c, global_tables())
    assert not problems, "tables without forced row-level security and a policy:\n" + "\n".join(
        problems
    )


def test_global_tables_exist_and_have_reasons(owner_engine: Engine) -> None:
    listed = global_tables()  # raises on an entry without a reason
    assert listed == set(load_lines(GLOBAL_TABLES_FILE))
    with owner_engine.connect() as c:
        present = set(
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'shifde'")).scalars()
        )
    assert not listed - present, f"global_tables.txt lists unknown tables: {listed - present}"


@pytest.fixture
def scratch(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is always rolled back."""
    with owner_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


def test_table_without_policy_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_probe (id int PRIMARY KEY)"))
    problems = rls_violations(scratch, set())
    assert problems == [
        "shifde.zz_rls_probe: row level security not enabled, row level security not forced, "
        "no policy"
    ]


def test_enabled_but_not_forced_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_probe (id int PRIMARY KEY)"))
    scratch.execute(text("ALTER TABLE shifde.zz_rls_probe ENABLE ROW LEVEL SECURITY"))
    scratch.execute(text("CREATE POLICY p ON shifde.zz_rls_probe USING (true)"))
    assert rls_violations(scratch, set()) == ["shifde.zz_rls_probe: row level security not forced"]


def test_forced_without_policy_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE public.zz_rls_probe (id int PRIMARY KEY)"))
    scratch.execute(text("ALTER TABLE public.zz_rls_probe ENABLE ROW LEVEL SECURITY"))
    scratch.execute(text("ALTER TABLE public.zz_rls_probe FORCE ROW LEVEL SECURITY"))
    assert rls_violations(scratch, set()) == ["public.zz_rls_probe: no policy"]


def test_compliant_or_allowlisted_table_passes(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_ok (id int PRIMARY KEY)"))
    scratch.execute(text("ALTER TABLE shifde.zz_rls_ok ENABLE ROW LEVEL SECURITY"))
    scratch.execute(text("ALTER TABLE shifde.zz_rls_ok FORCE ROW LEVEL SECURITY"))
    scratch.execute(text("CREATE POLICY p ON shifde.zz_rls_ok USING (true)"))
    scratch.execute(text("CREATE TABLE shifde.zz_rls_global (id int PRIMARY KEY)"))
    assert rls_violations(scratch, {"zz_rls_global"}) == []
