"""Catalog snapshot of the tables in schema `shifde`, for comparing this database with SETH's.

`snapshot(conn, tables=None)` returns a JSON-serialisable, deterministic dict:

    {"schema": "shifde",
     "tables": {<table>: {
        "columns":     {<name>: {"type", "nullable", "default"}},
        "constraints": {<name>: {"type": "p|f|u|c|x|t", "definition"}},
        "indexes":     {<name>: <CREATE INDEX definition>},
        "triggers":    {<name>: <CREATE TRIGGER definition>},
        "rls":         {"enabled": bool, "forced": bool},
        "policies":    {<name>: {"cmd", "permissive", "roles", "qual", "with_check"}},
        "privileges":  {"table": [<privilege>...], "columns": {<column>: [<privilege>...]}}
     }}}

`privileges` are those granted to `shifde_app` (directly, not through PUBLIC). The comparison
helpers below are pure functions so they can be unit-tested without a database.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, text

SCHEMA = "shifde"
APP_ROLE = "shifde_app"
TABLE_KEYS = ("columns", "constraints", "indexes", "triggers", "rls", "policies", "privileges")

_TABLES = text(
    """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') ORDER BY c.relname"""
)
_COLUMNS = text(
    """SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) AS type,
              NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS default
         FROM pg_attribute a
         JOIN pg_class c ON c.oid = a.attrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
         LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')
          AND a.attnum > 0 AND NOT a.attisdropped"""
)
_CONSTRAINTS = text(
    """SELECT c.relname, k.conname, k.contype::text AS type,
              pg_get_constraintdef(k.oid, true) AS definition
         FROM pg_constraint k
         JOIN pg_class c ON c.oid = k.conrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema"""
)
_INDEXES = text(
    """SELECT c.relname, i.relname AS name, pg_get_indexdef(x.indexrelid) AS definition
         FROM pg_index x
         JOIN pg_class c ON c.oid = x.indrelid
         JOIN pg_class i ON i.oid = x.indexrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema"""
)
_TRIGGERS = text(
    """SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid, true) AS definition
         FROM pg_trigger t
         JOIN pg_class c ON c.oid = t.tgrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND NOT t.tgisinternal"""
)
_RLS = text(
    """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')"""
)
_POLICIES = text(
    """SELECT tablename, policyname, permissive, cmd, roles::text[] AS roles, qual, with_check
         FROM pg_policies WHERE schemaname = :schema"""
)
_TABLE_PRIVS = text(
    """SELECT c.relname, acl.privilege_type
         FROM pg_class c
         JOIN pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN LATERAL aclexplode(c.relacl) acl
         JOIN pg_roles r ON r.oid = acl.grantee
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND r.rolname = :role"""
)
_COLUMN_PRIVS = text(
    """SELECT c.relname, a.attname, acl.privilege_type
         FROM pg_attribute a
         JOIN pg_class c ON c.oid = a.attrelid
         JOIN pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN LATERAL aclexplode(a.attacl) acl
         JOIN pg_roles r ON r.oid = acl.grantee
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND r.rolname = :role
          AND a.attnum > 0 AND NOT a.attisdropped"""
)


def _empty_table() -> dict[str, Any]:
    return {
        "columns": {},
        "constraints": {},
        "indexes": {},
        "triggers": {},
        "rls": {"enabled": False, "forced": False},
        "policies": {},
        "privileges": {"table": [], "columns": {}},
    }


def snapshot(conn: Connection, tables: set[str] | None = None) -> dict[str, Any]:
    """Snapshot of every table in schema `shifde` (or only `tables`)."""
    params = {"schema": SCHEMA, "role": APP_ROLE}
    names = [n for n in conn.execute(_TABLES, params).scalars() if tables is None or n in tables]
    out: dict[str, dict[str, Any]] = {name: _empty_table() for name in names}

    def rows(query: Any) -> Iterable[Any]:
        return (r for r in conn.execute(query, params) if r[0] in out)

    for r in rows(_COLUMNS):
        out[r.relname]["columns"][r.attname] = {
            "type": r.type,
            "nullable": bool(r.nullable),
            "default": r.default,
        }
    for r in rows(_CONSTRAINTS):
        out[r.relname]["constraints"][r.conname] = {"type": r.type, "definition": r.definition}
    for r in rows(_INDEXES):
        out[r.relname]["indexes"][r.name] = r.definition
    for r in rows(_TRIGGERS):
        out[r.relname]["triggers"][r.tgname] = r.definition
    for r in rows(_RLS):
        out[r.relname]["rls"] = {
            "enabled": bool(r.relrowsecurity),
            "forced": bool(r.relforcerowsecurity),
        }
    for r in rows(_POLICIES):
        out[r.tablename]["policies"][r.policyname] = {
            "cmd": r.cmd,
            "permissive": r.permissive,
            "roles": sorted(r.roles),
            "qual": r.qual,
            "with_check": r.with_check,
        }
    for r in rows(_TABLE_PRIVS):
        out[r.relname]["privileges"]["table"].append(r.privilege_type)
    for r in rows(_COLUMN_PRIVS):
        out[r.relname]["privileges"]["columns"].setdefault(r.attname, []).append(r.privilege_type)
    for table in out.values():
        table["privileges"]["table"].sort()
        for privs in table["privileges"]["columns"].values():
            privs.sort()
    return {"schema": SCHEMA, "tables": _sorted(out)}


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sorted(value[k]) for k in sorted(value)}
    return value


# --------------------------------------------------------------------------- comparison
@dataclass(frozen=True)
class DeliberateChange:
    """One reviewed difference from SETH: `path` inside the table's snapshot (e.g.
    `["columns", "name", "type"]` or `["policies", "tenant_isolation"]`) may differ."""

    table: str
    path: tuple[str, ...]
    reason: str
    migration: str


def load_lines(path: Path) -> list[str]:
    """Entries of a `name  # comment` list file: blank lines and comment lines skipped."""
    entries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def load_deliberate_changes(path: Path) -> list[DeliberateChange]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of entries")
    changes: list[DeliberateChange] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, Mapping):
            raise ValueError(f"{path}: entry {i} is not a mapping")
        missing = [k for k in ("table", "path", "reason", "migration") if not entry.get(k)]
        if missing:
            raise ValueError(f"{path}: entry {i} lacks {', '.join(missing)}")
        raw_path = entry["path"]
        parts = raw_path.split("/") if isinstance(raw_path, str) else list(raw_path)
        if not parts or parts[0] not in TABLE_KEYS or not all(isinstance(p, str) for p in parts):
            raise ValueError(f"{path}: entry {i} has an invalid path {raw_path!r}")
        changes.append(
            DeliberateChange(
                table=str(entry["table"]),
                path=tuple(parts),
                reason=str(entry["reason"]),
                migration=str(entry["migration"]),
            )
        )
    return changes


_MISSING = object()


def _pop_path(tree: Any, path: Sequence[str]) -> Any:
    node = tree
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    if not isinstance(node, dict):
        return _MISSING
    return node.pop(path[-1], _MISSING)


def _diff(a: Any, b: Any, where: str, out: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{where}/{key}"
            if key not in a:
                out.append(f"{sub}: only in the new database: {b[key]!r}")
            elif key not in b:
                out.append(f"{sub}: missing from the new database (SETH: {a[key]!r})")
            else:
                _diff(a[key], b[key], sub, out)
    elif a != b:
        out.append(f"{where}: SETH {a!r} != new {b!r}")


def compare_table(
    table: str,
    seth: Mapping[str, Any],
    new: Mapping[str, Any],
    changes: Iterable[DeliberateChange] = (),
) -> list[str]:
    """Differences between SETH's and the new snapshot of `table`, after removing the paths of
    its deliberate changes. A deliberate change whose path does not actually differ is reported
    too (stale entry)."""
    a, b = copy.deepcopy(dict(seth)), copy.deepcopy(dict(new))
    problems: list[str] = []
    for change in changes:
        if change.table != table:
            continue
        before, after = _pop_path(a, change.path), _pop_path(b, change.path)
        if before == after:
            problems.append(
                f"{table}/{'/'.join(change.path)}: listed in deliberate_changes.yaml but "
                "identical to SETH (remove the entry)"
            )
    diffs: list[str] = []
    _diff(a, b, table, diffs)
    return problems + diffs


def compare_ported(
    ported: Iterable[str],
    seth: Mapping[str, Any],
    new: Mapping[str, Any],
    changes: Sequence[DeliberateChange] = (),
) -> list[str]:
    """Problems for every ported table: unknown to SETH, missing from the new database, or
    different from SETH beyond its deliberate changes. `seth`/`new` are full snapshots."""
    seth_tables, new_tables = seth["tables"], new["tables"]
    problems: list[str] = []
    ported = list(ported)
    for table in ported:
        if table not in seth_tables:
            problems.append(f"{table}: listed in ported_tables.txt but not a SETH 3ba0e06 table")
        elif table not in new_tables:
            problems.append(f"{table}: listed in ported_tables.txt but missing from the database")
        else:
            problems.extend(compare_table(table, seth_tables[table], new_tables[table], changes))
    for change in changes:
        if change.table not in ported:
            problems.append(
                f"{change.table}: has a deliberate change but is not in ported_tables.txt"
            )
    return problems
