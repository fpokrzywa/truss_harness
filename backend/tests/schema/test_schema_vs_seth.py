"""Ported tables match SETH 3ba0e06 exactly, apart from reviewed deliberate changes.

`seth_3ba0e06.json` is produced by `scripts/make_seth_schema_snapshot.py` from SETH's own
migrations (DECISIONS B-005). Tables to compare are listed in `docs/build/ported_tables.txt`;
allowed differences in `deliberate_changes.yaml`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from tests.schema.snapshot import (
    TABLE_KEYS,
    DeliberateChange,
    compare_ported,
    compare_table,
    load_deliberate_changes,
    load_lines,
    snapshot,
)

HERE = Path(__file__).resolve().parent
SETH_SNAPSHOT = HERE / "seth_3ba0e06.json"
CHANGES_FILE = HERE / "deliberate_changes.yaml"
PORTED_FILE = HERE.parents[2] / "docs" / "build" / "ported_tables.txt"


@pytest.fixture(scope="module")
def seth() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SETH_SNAPSHOT.read_text(encoding="utf-8"))
    return data


def test_seth_snapshot_shape(seth: dict[str, Any]) -> None:
    assert seth["schema"] == "shifde"
    tables = seth["tables"]
    # SETH 3ba0e06 has the P1-P11 tables; the core ones must be there.
    assert {"tenants", "users", "role_bindings", "audit_events", "runs"} <= set(tables)
    for name, table in tables.items():
        assert set(table) == set(TABLE_KEYS), name
        assert set(table["rls"]) == {"enabled", "forced"}, name
        assert set(table["privileges"]) == {"table", "columns"}, name
        for column in table["columns"].values():
            assert set(column) == {"type", "nullable", "default"}, name
    assert tables["tenants"]["rls"] == {"enabled": True, "forced": True}


def test_list_files_load() -> None:
    changes = load_deliberate_changes(CHANGES_FILE)
    assert all(isinstance(c, DeliberateChange) for c in changes)
    ported = load_lines(PORTED_FILE)
    assert len(ported) == len(set(ported)), "ported_tables.txt lists a table twice"


def test_ported_tables_match_seth(seth: dict[str, Any], owner_engine: Engine) -> None:
    ported = load_lines(PORTED_FILE)
    changes = load_deliberate_changes(CHANGES_FILE)
    with owner_engine.connect() as c:
        new = snapshot(c, set(ported))
    problems = compare_ported(ported, seth, new, changes)
    assert not problems, "schema differs from SETH 3ba0e06:\n" + "\n".join(problems)


# --------------------------------------------------------------------------- comparison logic
def _table() -> dict[str, Any]:
    return {
        "columns": {"id": {"type": "uuid", "nullable": False, "default": None}},
        "constraints": {"t_pkey": {"type": "p", "definition": "PRIMARY KEY (id)"}},
        "indexes": {"t_pkey": "CREATE UNIQUE INDEX t_pkey ON shifde.t USING btree (id)"},
        "triggers": {},
        "rls": {"enabled": True, "forced": True},
        "policies": {
            "tenant_isolation": {
                "cmd": "ALL",
                "permissive": "PERMISSIVE",
                "roles": ["public"],
                "qual": "(tenant_id = shifde.current_tenant_id())",
                "with_check": None,
            }
        },
        "privileges": {"table": ["SELECT"], "columns": {}},
    }


def test_identical_tables_have_no_differences() -> None:
    assert compare_table("t", _table(), _table()) == []


def test_undeclared_difference_is_reported() -> None:
    new = _table()
    new["columns"]["id"]["type"] = "text"
    new["rls"]["forced"] = False
    assert compare_table("t", _table(), new) == [
        "t/columns/id/type: SETH 'uuid' != new 'text'",
        "t/rls/forced: SETH True != new False",
    ]


def test_declared_difference_is_accepted_and_others_still_reported() -> None:
    new = _table()
    new["columns"]["id"]["type"] = "text"
    new["privileges"]["table"] = ["DELETE", "SELECT"]
    change = DeliberateChange("t", ("columns", "id", "type"), "test", "x_0001")
    assert compare_table("t", _table(), new, [change]) == [
        "t/privileges/table: SETH ['SELECT'] != new ['DELETE', 'SELECT']"
    ]


def test_extra_and_missing_entries_are_reported() -> None:
    new = _table()
    del new["policies"]["tenant_isolation"]
    new["indexes"]["ix_extra"] = "CREATE INDEX ix_extra ON shifde.t USING btree (id)"
    problems = compare_table("t", _table(), new)
    assert len(problems) == 2
    assert problems[0].startswith("t/indexes/ix_extra: only in the new database")
    assert problems[1].startswith("t/policies/tenant_isolation: missing from the new database")


def test_stale_deliberate_change_is_reported() -> None:
    change = DeliberateChange("t", ("columns", "id", "type"), "test", "x_0001")
    assert compare_table("t", _table(), _table(), [change]) == [
        "t/columns/id/type: listed in deliberate_changes.yaml but identical to SETH "
        "(remove the entry)"
    ]


def test_ported_table_missing_from_database_fails() -> None:
    seth = {"schema": "shifde", "tables": {"t": _table()}}
    new: dict[str, Any] = {"schema": "shifde", "tables": {}}
    assert compare_ported(["t"], seth, new) == [
        "t: listed in ported_tables.txt but missing from the database"
    ]
    assert compare_ported(["nope"], seth, new) == [
        "nope: listed in ported_tables.txt but not a SETH 3ba0e06 table"
    ]


def test_change_for_unported_table_fails() -> None:
    seth = {"schema": "shifde", "tables": {"t": _table()}}
    change = DeliberateChange("u", ("rls",), "test", "x_0001")
    assert compare_ported([], seth, seth, [change]) == [
        "u: has a deliberate change but is not in ported_tables.txt"
    ]


def test_deliberate_changes_file_is_validated(tmp_path: Path) -> None:
    bad = tmp_path / "c.yaml"
    bad.write_text("- table: t\n  path: [columns]\n  reason: r\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lacks migration"):
        load_deliberate_changes(bad)
    bad.write_text("- {table: t, path: [nonsense, x], reason: r, migration: m}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid path"):
        load_deliberate_changes(bad)
    good = tmp_path / "g.yaml"
    good.write_text(
        "- {table: t, path: columns/id/type, reason: r, migration: m}\n", encoding="utf-8"
    )
    assert load_deliberate_changes(good) == [
        DeliberateChange("t", ("columns", "id", "type"), "r", "m")
    ]
