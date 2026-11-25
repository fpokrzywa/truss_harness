"""Issue #373: no engine of the product puts bound parameters in an exception's text.

`Database` and the engine of `check_database_url` are tried against PostgreSQL; a scan of the
source holds every `create_engine` call (the migrations' engine too) to the literal
`hide_parameters=True`, and refuses the other ways to build an engine."""

from __future__ import annotations

import ast
import traceback
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from shifde.core import dbrole
from shifde.core.db import Database
from tests._support.dberrors import fail_with, new_marker

BACKEND = Path(__file__).resolve().parents[2]
SOURCES = [*sorted((BACKEND / "src" / "shifde").rglob("*.py")), BACKEND / "migrations" / "env.py"]
KNOWN = {"src/shifde/core/db.py", "src/shifde/core/dbrole.py", "migrations/env.py"}
CREATE = "create_engine"
OTHERS = {"engine_from_config", "create_async_engine"}


def _texts(exc: BaseException) -> list[str]:
    return [str(exc), repr(exc), "".join(traceback.format_exception(exc))]


def test_a_failed_statement_of_a_database_does_not_show_its_parameters(app_url: str) -> None:
    marker = new_marker()
    db = Database(app_url)
    try:
        with db.session() as s, pytest.raises(DBAPIError) as failed:
            fail_with(s, marker)
    finally:
        db.dispose()
    assert failed.value.params == {"marker": marker}  # it was bound
    for shown in _texts(failed.value):
        assert "division by zero" in shown and marker not in shown


def test_a_failed_executemany_of_a_database_does_not_show_its_parameters(app_url: str) -> None:
    markers = [new_marker(), new_marker()]
    db = Database(app_url)
    try:
        with db.session() as s, pytest.raises(DBAPIError) as failed:
            s.execute(
                text("INSERT INTO shifde.no_such_table (v) VALUES (:marker)"),
                [{"marker": m} for m in markers],
            )
    finally:
        db.dispose()
    assert failed.value.params == [{"marker": m} for m in markers]
    for shown in _texts(failed.value):
        assert "no_such_table" in shown
        assert not any(m in shown for m in markers)


def test_a_failed_statement_of_the_role_check_does_not_show_its_parameters(
    app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = new_marker()

    def broken(engine: Engine) -> None:
        with engine.connect() as c:
            fail_with(c, marker)

    monkeypatch.setattr(dbrole, "check_app_role", broken)
    with pytest.raises(DBAPIError) as failed:
        dbrole.check_database_url(app_url)
    assert failed.value.params == {"marker": marker}
    for shown in _texts(failed.value):
        assert "division by zero" in shown and marker not in shown


def _hides(call: ast.Call) -> bool:
    return any(
        k.arg == "hide_parameters" and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in call.keywords
    )


def _scan(source: str) -> tuple[int, list[int]]:
    """(the number of `create_engine` calls, the lines that build an engine in another way than
    such a call with the literal `hide_parameters=True`)."""
    tree = ast.parse(source)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name | ast.Attribute)
        and (n.func.id if isinstance(n.func, ast.Name) else n.func.attr) == CREATE
    ]
    called = {id(c.func) for c in calls}
    bad = [c.lineno for c in calls if not _hides(c)]
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            renamed = node.name == CREATE and node.asname not in (None, CREATE)
            if renamed or node.name in OTHERS:
                bad.append(node.lineno)
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in OTHERS or (name == CREATE and id(node) not in called):
                bad.append(node.lineno)
    return len(calls), bad


def test_every_engine_of_the_product_hides_parameters() -> None:
    found: set[str] = set()
    bad: list[str] = []
    for path in SOURCES:
        name = path.relative_to(BACKEND).as_posix()
        calls, lines = _scan(path.read_text(encoding="utf-8"))
        if calls:
            found.add(name)
        bad += [f"{name}:{line}" for line in lines]
    assert bad == []
    assert found >= KNOWN


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("create_engine(u, hide_parameters=True)", (1, [])),
        ("sqlalchemy.create_engine(u, hide_parameters=True)", (1, [])),
        ("create_engine(u)", (1, [1])),
        ("sqlalchemy.create_engine(u)", (1, [1])),
        ("create_engine(u, hide_parameters=False)", (1, [1])),
        ("create_engine(u, hide_parameters=flag)", (1, [1])),
        ("create_engine(u, **options)", (1, [1])),
        ("partial(create_engine, u)", (0, [1])),
        ("from sqlalchemy import create_engine as ce", (0, [1])),
        ("from sqlalchemy import engine_from_config", (0, [1])),
        ("sqlalchemy.engine_from_config(c)", (0, [1])),
        ("aio.create_async_engine(u, hide_parameters=True)", (0, [1])),
    ],
)
def test_the_scan_finds_an_engine_that_shows_parameters(
    source: str, expected: tuple[int, list[int]]
) -> None:
    assert _scan(source) == expected
