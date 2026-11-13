"""Every relation that can hold client data is isolated by forced row-level security with
tenant-keyed policies and no rewrite rules (Review Focus item 3; isolation review 1, items
1, 2 and 11; isolation review 2, items 1, 2, 3 and 11; isolation review 3, items 2, 3, 4, 6).

Scope: every relation in every non-system schema (SETH's tables live in `shifde`; `public` is
checked too so a relation created there by mistake cannot escape), except exactly
`shifde.alembic_version` and the relations listed, with a reason, in `global_tables.txt`.

Rules, per relation kind:

* Tables (`r`) and partitioned tables (`p`): row-level security enabled AND forced, at least one
  policy, and every PERMISSIVE policy tenant-keyed (below).
* Every policy expression, PERMISSIVE and RESTRICTIVE, USING and WITH CHECK, on any relation
  (listed or not), passes the context-write scan of `tests/schema/catalog_scan.py`: it must not
  write `app.tenant_id`, `app.principal_id` or `app.platform_admin` (`set_config(...)` in a
  policy, even in a conjunct next to a tenant key, changes the context for the rest of the
  query). Beyond that scan a RESTRICTIVE policy is not constrained: it is ANDed with the
  permissive ones, so it can only narrow what they allow and need not be tenant-keyed.
* Views (`v`): `security_invoker` must be true. A view otherwise runs with its owner's rights, and
  `shifde_owner` has BYPASSRLS, so it would return every client's rows to `shifde_app`.
* Materialized views (`m`) and foreign tables (`f`): not allowed at all unless listed in
  `global_tables.txt`. Neither can carry row-level security.
* Rewrite rules: none on any relation (listed or not), apart from the `_RETURN` rule that is a
  view's or materialized view's definition. A rule's actions run with the table owner's rights,
  and `shifde_owner` has BYPASSRLS, so `ON INSERT ... DO ALSO INSERT INTO x SELECT ... FROM y`
  copies every client's rows.

A permissive policy is tenant-keyed when:

1. Its USING expression (commands ALL, SELECT, UPDATE, DELETE) and its WITH CHECK expression
   (commands ALL, INSERT, UPDATE) each reference `shifde.current_tenant_id()` or, for
   principal-owned rows, `shifde.current_principal_id()`.
2. WITH CHECK is present explicitly on every ALL, INSERT and UPDATE policy. PostgreSQL would use
   the USING expression as the check when WITH CHECK is absent (and an INSERT policy has no USING
   at all, so its writes would be unchecked); we are stricter and require the write rule to be
   written out, so it is visible in the migration and reviewed on its own.
3. Every expression is built only from recognised forms. Top-level `OR` needs every branch
   recognised, `AND` needs at least one recognised conjunct (the rest can only narrow; the scan
   above keeps them from writing the context). The recognised atoms are:
     - `<key column> = <helper>` (either side) and `NOT (<key column> IS DISTINCT FROM <helper>)`
       (either side), where the key column is one `KEY_COLUMNS` lists for that helper:
       `tenant_id` for `shifde.current_tenant_id()`, `user_id` for
       `shifde.current_principal_id()`, plus the reviewed `<schema>.<table>.<column>` entries
       (`shifde.tenants.id`, `shifde.users.id`) that hold only on that table;
     - `shifde.is_platform_admin_ctx()` (the platform-admin flag, fail closed when unset);
     - in the USING of a FOR SELECT policy only, `tenant_id IS NULL` (rows shared by every
       client, e.g. built-in roles).
   On a relation that has a `tenant_id` column only the tenant atoms count, in USING and WITH
   CHECK alike: `tenant_id = shifde.current_tenant_id()` (and its NOT IS DISTINCT FROM form),
   `shifde.is_platform_admin_ctx()`, and the shared-rows atom above. A principal key
   (`user_id = shifde.current_principal_id()`) may narrow such a policy in an `AND` but cannot
   satisfy it: alone it lets a user write rows with `tenant_id` NULL or another client's, and
   read their own rows filed under another client (isolation review 4, item 1). Principal keys
   count only on relations without `tenant_id`.
   Everywhere else -- every WITH CHECK, and the USING of ALL, UPDATE and DELETE policies, where
   a client could create, take over or delete a row every client sees -- each occurrence of
   `tenant_id IS NULL` must sit in an `AND` that has `shifde.is_platform_admin_ctx()` as one of
   its direct conjuncts (`tenant_id IS NULL AND shifde.is_platform_admin_ctx()`), however deeply
   that AND is nested; another recognised conjunct (say `user_id = current_principal_id()`)
   does not carry it.
   Anything else -- `true`, `1 = 1`, `current_user = ...`, `NOT (tenant_id = ...)`, a test of
   whether the context is set (`shifde.current_tenant_id() IS NOT NULL`), `deleted_at IS NULL`,
   `user_id = shifde.current_tenant_id()`, any `EXISTS (...)` subquery -- is rejected.
4. An expression the forms above cannot express (for example SETH's membership `EXISTS` on
   `users`) passes only if `REVIEWED_EXPRESSIONS` holds its exact `pg_get_expr` text under
   (relation, policy, command, PERMISSIVE/RESTRICTIVE, roles, clause); any change to the text,
   the command or the roles fails until it is reviewed again. Adding an entry or a key column
   needs isolation review.

Expressions are read with `pg_get_expr` under `search_path = pg_catalog`, so the helpers print
schema-qualified and a look-alike function in another schema cannot pass for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from tests.schema.catalog_scan import context_write_violations
from tests.schema.snapshot import load_lines

GLOBAL_TABLES_FILE = Path(__file__).with_name("global_tables.txt")
EXEMPT = frozenset({("shifde", "alembic_version")})

_RELATIONS = text(
    r"""SELECT n.nspname, c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
              (SELECT o.option_value::boolean
                 FROM pg_catalog.pg_options_to_table(c.reloptions) o
                WHERE o.option_name = 'security_invoker') AS security_invoker
         FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
        ORDER BY n.nspname, c.relname"""
)

_POLICIES = text(
    """SELECT n.nspname, c.relname, p.polname, p.polcmd, p.polpermissive,
              ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE pg_catalog.pg_get_userbyid(r) END
                      FROM pg_catalog.unnest(p.polroles) r ORDER BY 1) AS roles,
              EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                       WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                         AND a.attnum > 0 AND NOT a.attisdropped) AS tenant_column,
              pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS using_expr,
              pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
         FROM pg_catalog.pg_policy p
         JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        ORDER BY n.nspname, c.relname, p.polname"""
)

_RULES = text(
    r"""SELECT n.nspname, c.relname, r.rulename
         FROM pg_catalog.pg_rewrite r
         JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
          AND NOT (r.rulename = '_RETURN' AND c.relkind IN ('v', 'm'))
        ORDER BY n.nspname, c.relname, r.rulename"""
)

_CMD = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}
_HAS_USING = {"r", "w", "d", "*"}
_HAS_CHECK = {"a", "w", "*"}
_KIND = {"m": "materialized view", "f": "foreign table"}

# --------------------------------------------------------------------------- expression rules
# helper -> the columns it may be compared with: a bare name holds on every table, a
# `<schema>.<table>.<column>` entry only on that table. Changing this needs isolation review.
KEY_COLUMNS: dict[str, frozenset[str]] = {
    "shifde.current_tenant_id()": frozenset({"tenant_id", "shifde.tenants.id"}),
    "shifde.current_principal_id()": frozenset({"user_id", "shifde.users.id"}),
}

# (relation, policy, command, "PERMISSIVE" | "RESTRICTIVE", roles, "USING" | "WITH CHECK")
# -> exact pg_get_expr text, reviewed by isolation review because the recognised forms cannot
# express it. The command is SELECT, INSERT, UPDATE, DELETE or ALL; roles are sorted, `public`
# for PUBLIC. Adding an entry needs isolation review.
ReviewedKey = tuple[str, str, str, str, tuple[str, ...], str]
REVIEWED_EXPRESSIONS: dict[ReviewedKey, str] = {}

_HELPER = r"shifde\.current_(?:tenant|principal)_id\(\)"
_IDENT = r"[a-z_][a-z0-9_$]*"
_KEY_RE = re.compile(_HELPER)
_KEYED = (
    re.compile(rf"(?P<col>{_IDENT}) = (?P<helper>{_HELPER})"),
    re.compile(rf"(?P<helper>{_HELPER}) = (?P<col>{_IDENT})"),
    re.compile(rf"NOT \((?P<col>{_IDENT}) IS DISTINCT FROM (?P<helper>{_HELPER})\)"),
    re.compile(rf"NOT \((?P<helper>{_HELPER}) IS DISTINCT FROM (?P<col>{_IDENT})\)"),
)
_ADMIN = "shifde.is_platform_admin_ctx()"
_TENANT_HELPER = "shifde.current_tenant_id()"
_GLOBAL_ROWS = "tenant_id IS NULL"
_GLOBAL_ROWS_RE = re.compile(r"\btenant_id IS NULL\b")


@dataclass(frozen=True)
class _Clause:
    relation: str  # schema.table
    shared_rows: bool  # bare `tenant_id IS NULL` allowed: the USING of a FOR SELECT policy only
    keys: Mapping[str, frozenset[str]]
    tenant_column: bool = False  # the relation has tenant_id: only tenant atoms count


def _strip_parens(expr: str) -> str:
    """`expr` without whitespace and without parentheses that enclose all of it."""
    expr = expr.strip()
    while expr.startswith("(") and _closing_paren(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr


def _closing_paren(expr: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    for i in range(start, len(expr)):
        ch = expr[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _word_at(expr: str, i: int, word: str) -> bool:
    """`word` (a keyword) starts at `expr[i]` and is not part of a longer identifier."""
    end = i + len(word)
    return (
        expr.startswith(word, i)
        and (i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] in "_$"))
        and (end == len(expr) or not (expr[end].isalnum() or expr[end] in "_$"))
    )


def _split_top(expr: str, word: str) -> list[str]:
    """Split `expr` on ` <word> ` outside parentheses, quotes and CASE ... END."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    last = 0
    i = 0
    sep = f" {word} "
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif _word_at(expr, i, "CASE"):
            depth += 1  # a CASE ... END nests like a parenthesis
        elif _word_at(expr, i, "END"):
            depth -= 1
        elif depth == 0 and expr.startswith(sep, i):
            parts.append(expr[last:i])
            last = i + len(sep)
            i = last
            continue
        i += 1
    parts.append(expr[last:])
    return [p.strip() for p in parts]


def _recognised(expr: str, clause: _Clause) -> bool:
    expr = _strip_parens(expr)
    branches = _split_top(expr, "OR")
    if len(branches) > 1:
        return all(_recognised(b, clause) for b in branches)
    conjuncts = _split_top(expr, "AND")
    if len(conjuncts) > 1:
        return any(_recognised(c, clause) for c in conjuncts)
    if expr == _ADMIN:
        return True
    if expr == _GLOBAL_ROWS:
        return clause.shared_rows
    for atom in _KEYED:
        if match := atom.fullmatch(expr):
            if clause.tenant_column and match["helper"] != _TENANT_HELPER:
                return False  # a principal key may narrow (in an AND) but not satisfy
            allowed = clause.keys.get(match["helper"], frozenset())
            column = match["col"]
            return column in allowed or f"{clause.relation}.{column}" in allowed
    return False


def _admits_shared_rows(expr: str) -> bool:
    """`tenant_id IS NULL` occurs outside every AND that has `shifde.is_platform_admin_ctx()` as
    a direct conjunct."""
    expr = _strip_parens(expr)
    branches = _split_top(expr, "OR")
    if len(branches) > 1:
        return any(_admits_shared_rows(b) for b in branches)
    conjuncts = _split_top(expr, "AND")
    if len(conjuncts) > 1:
        if any(_strip_parens(c) == _ADMIN for c in conjuncts):
            return False
        return any(_admits_shared_rows(c) for c in conjuncts)
    return _GLOBAL_ROWS_RE.search(expr) is not None


def expression_problem(
    expr: str | None, clause: _Clause, reviewed: str | None = None
) -> str | None:
    """Why `expr` is not a tenant-keyed policy expression, or None if it is."""
    if expr is None:
        return "missing"
    if reviewed is not None:
        return None if expr == reviewed else "differs from its reviewed text"
    if _strip_parens(expr).lower() == "true":
        return "constant true"
    if not _KEY_RE.search(expr):
        return "does not reference shifde.current_tenant_id() or shifde.current_principal_id()"
    if not clause.shared_rows and _admits_shared_rows(expr):
        return f"admits {_GLOBAL_ROWS} without {_ADMIN}"
    if not _recognised(expr, clause):
        return "not built from recognised tenant-keyed forms"
    return None


# --------------------------------------------------------------------------- the check
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


def _reviewed_key(
    relation: str, name: str, cmd: str, permissive: bool, roles: list[str], clause: str
) -> ReviewedKey:
    kind = "PERMISSIVE" if permissive else "RESTRICTIVE"
    return (relation, name, _CMD[cmd], kind, tuple(sorted(roles)), clause)


def _policy_problems(
    conn: Connection,
    reviewed: Mapping[ReviewedKey, str],
    keys: Mapping[str, frozenset[str]],
) -> dict[tuple[str, str], list[str]]:
    # Pin the search path for pg_get_expr (transaction-local; the caller's transaction ends it).
    conn.execute(text("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)"))
    found: dict[tuple[str, str], list[str]] = {}
    for schema, table, name, cmd, permissive, roles, tenant_column, using, check in conn.execute(
        _POLICIES
    ):
        if not permissive:
            continue
        label = f"policy {name} ({_CMD[cmd]})"
        problems = found.setdefault((schema, table), [])
        relation = f"{schema}.{table}"
        for applies, kind, expr in (
            (cmd in _HAS_USING, "USING", using),
            (cmd in _HAS_CHECK, "WITH CHECK", check),
        ):
            if not applies:
                continue
            clause = _Clause(relation, kind == "USING" and cmd == "r", keys, tenant_column)
            key = _reviewed_key(relation, name, cmd, permissive, roles, kind)
            why = expression_problem(expr, clause, reviewed.get(key))
            if why:
                problems.append(f"{label} {kind} {why}: {expr}")
    return found


def rls_violations(
    conn: Connection,
    allowed: set[str],
    *,
    reviewed: Mapping[ReviewedKey, str] = REVIEWED_EXPRESSIONS,
    key_columns: Mapping[str, frozenset[str]] = KEY_COLUMNS,
) -> list[str]:
    """`schema.relation: problem` for every relation that breaks the rules in the docstring."""
    relations = conn.execute(_RELATIONS).all()
    policy_problems = _policy_problems(conn, reviewed, key_columns)
    policy_count: dict[tuple[str, str], int] = {}
    for schema, table, *_ in conn.execute(_POLICIES):
        policy_count[(schema, table)] = policy_count.get((schema, table), 0) + 1
    problems: list[str] = []
    for schema, table, kind, enabled, forced, invoker in relations:
        if (schema, table) in EXEMPT or (schema == "shifde" and table in allowed):
            continue
        if kind in _KIND:
            problems.append(f"{schema}.{table}: {_KIND[kind]} not listed in global_tables.txt")
            continue
        if kind == "v":
            if not invoker:
                problems.append(f"{schema}.{table}: view without security_invoker=true")
            continue
        missing = [
            what
            for what, ok in (
                ("row level security not enabled", enabled),
                ("row level security not forced", forced),
                ("no policy", policy_count.get((schema, table), 0) > 0),
            )
            if not ok
        ]
        missing += policy_problems.get((schema, table), [])
        if missing:
            problems.append(f"{schema}.{table}: {', '.join(missing)}")
    problems += [
        f"{schema}.{table}: rewrite rule {rule}" for schema, table, rule in conn.execute(_RULES)
    ]
    problems += context_write_violations(conn, kinds={"policy"})
    return sorted(problems)


def test_every_client_table_has_forced_rls_and_tenant_keyed_policies(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as c:
        problems = rls_violations(c, global_tables())
    assert not problems, "relations not isolated by row-level security:\n" + "\n".join(problems)


def test_reviewed_expressions_still_exist(owner_engine: Engine) -> None:
    """A stale entry would silently approve a future policy that reuses the name."""
    with owner_engine.connect() as c:
        c.execute(text("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)"))
        present = {
            _reviewed_key(f"{schema}.{table}", name, cmd, permissive, roles, kind): expr
            for schema, table, name, cmd, permissive, roles, _t, using, check in c.execute(
                _POLICIES
            )
            for kind, expr in (("USING", using), ("WITH CHECK", check))
            if expr is not None
        }
    stale = [key for key in REVIEWED_EXPRESSIONS if key not in present]
    assert not stale, f"REVIEWED_EXPRESSIONS entries with no such policy clause: {stale}"


def test_global_tables_exist_and_have_reasons(owner_engine: Engine) -> None:
    listed = global_tables()  # raises on an entry without a reason
    assert listed == set(load_lines(GLOBAL_TABLES_FILE))
    with owner_engine.connect() as c:
        present = set(
            c.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'shifde' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
                )
            ).scalars()
        )
    assert not listed - present, f"global_tables.txt lists unknown tables: {listed - present}"


# --------------------------------------------------------------------------- probes
@pytest.fixture
def scratch(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is always rolled back."""
    with owner_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


TENANT = "tenant_id = shifde.current_tenant_id()"


def _secured_table(
    c: Connection, name: str = "shifde.zz_rls_probe", *, tenant_column: bool = True
) -> None:
    tenant = "tenant_id uuid, " if tenant_column else ""
    c.execute(text(f"CREATE TABLE {name} (id int PRIMARY KEY, {tenant}user_id uuid, ref uuid)"))
    c.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
    c.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))


def _tenant_policy(c: Connection, name: str = "shifde.zz_rls_probe") -> None:
    c.execute(
        text(f"CREATE POLICY tenant_isolation ON {name} USING ({TENANT}) WITH CHECK ({TENANT})")
    )


def test_table_without_policy_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_probe (id int PRIMARY KEY)"))
    problems = rls_violations(scratch, set())
    assert problems == [
        "shifde.zz_rls_probe: row level security not enabled, row level security not forced, "
        "no policy"
    ]


def test_enabled_but_not_forced_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_probe (id int PRIMARY KEY, tenant_id uuid)"))
    scratch.execute(text("ALTER TABLE shifde.zz_rls_probe ENABLE ROW LEVEL SECURITY"))
    _tenant_policy(scratch)
    assert rls_violations(scratch, set()) == ["shifde.zz_rls_probe: row level security not forced"]


def test_forced_without_policy_is_reported(scratch: Connection) -> None:
    _secured_table(scratch, "public.zz_rls_probe")
    assert rls_violations(scratch, set()) == ["public.zz_rls_probe: no policy"]


def test_compliant_or_allowlisted_table_passes(scratch: Connection) -> None:
    _secured_table(scratch, "shifde.zz_rls_ok")
    _tenant_policy(scratch, "shifde.zz_rls_ok")
    scratch.execute(text("CREATE TABLE shifde.zz_rls_global (id int PRIMARY KEY)"))
    assert rls_violations(scratch, {"zz_rls_global"}) == []


def test_using_true_policy_is_reported(scratch: Connection) -> None:
    _secured_table(scratch)
    scratch.execute(text("CREATE POLICY read_all ON shifde.zz_rls_probe FOR SELECT USING (true)"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_probe: policy read_all (SELECT) USING constant true: true"
    ]


def test_permissive_true_next_to_tenant_policy_is_reported(scratch: Connection) -> None:
    _secured_table(scratch)
    _tenant_policy(scratch)
    scratch.execute(text("CREATE POLICY read_all ON shifde.zz_rls_probe FOR SELECT USING (true)"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_probe: policy read_all (SELECT) USING constant true: true"
    ]


def test_restrictive_policy_is_not_constrained(scratch: Connection) -> None:
    _secured_table(scratch)
    _tenant_policy(scratch)
    scratch.execute(
        text("CREATE POLICY narrow ON shifde.zz_rls_probe AS RESTRICTIVE FOR SELECT USING (id > 0)")
    )
    assert rls_violations(scratch, set()) == []


def test_missing_with_check_is_reported(scratch: Connection) -> None:
    _secured_table(scratch)
    scratch.execute(text(f"CREATE POLICY t ON shifde.zz_rls_probe USING ({TENANT})"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_probe: policy t (ALL) WITH CHECK missing: None"
    ]


def test_insert_policy_without_with_check_is_reported(scratch: Connection) -> None:
    _secured_table(scratch)
    _tenant_policy(scratch)
    scratch.execute(text("CREATE POLICY ins ON shifde.zz_rls_probe FOR INSERT WITH CHECK (true)"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_probe: policy ins (INSERT) WITH CHECK constant true: true"
    ]


def test_non_tenant_with_check_is_reported(scratch: Connection) -> None:
    _secured_table(scratch)
    scratch.execute(
        text(f"CREATE POLICY t ON shifde.zz_rls_probe USING ({TENANT}) WITH CHECK (id > 0)")
    )
    (problem,) = rls_violations(scratch, set())
    assert "policy t (ALL) WITH CHECK does not reference shifde.current_tenant_id()" in problem


@pytest.mark.parametrize(
    "using",
    [
        f"true OR {TENANT}",
        f"1 = 1 OR {TENANT}",
        f"current_user = 'shifde_app' OR {TENANT}",
        f"NOT ({TENANT})",
        "shifde.current_tenant_id() IS NOT NULL",
        f"deleted IS NULL OR {TENANT}",
        "tenant_id <> shifde.current_tenant_id()",
    ],
)
def test_disguised_open_policy_is_reported(scratch: Connection, using: str) -> None:
    _secured_table(scratch)
    scratch.execute(text("ALTER TABLE shifde.zz_rls_probe ADD COLUMN deleted timestamptz"))
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    (problem,) = rls_violations(scratch, set())
    assert problem.startswith("shifde.zz_rls_probe: policy p (SELECT) USING "), problem


WRITE_FORMS = [
    TENANT,
    "shifde.current_tenant_id() = tenant_id",
    f"{TENANT} OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx())",
    "NOT (tenant_id IS DISTINCT FROM shifde.current_tenant_id())",
    f"{TENANT} AND id > 0",
    f"{TENANT} AND user_id = shifde.current_principal_id()",
]

# Principal-owned rows: recognised only on a relation without a tenant_id column (review 4).
PRINCIPAL_FORMS = [
    "user_id = shifde.current_principal_id()",
    "id = 1 AND shifde.current_principal_id() = user_id",
    "NOT (user_id IS DISTINCT FROM shifde.current_principal_id())",
]


@pytest.mark.parametrize("expr", PRINCIPAL_FORMS)
def test_principal_keyed_forms_pass_without_a_tenant_column(scratch: Connection, expr: str) -> None:
    _secured_table(scratch, tenant_column=False)
    scratch.execute(
        text(f"CREATE POLICY p ON shifde.zz_rls_probe USING ({expr}) WITH CHECK ({expr})")
    )
    assert rls_violations(scratch, set()) == []


@pytest.mark.parametrize("expr", WRITE_FORMS)
def test_recognised_tenant_keyed_forms_pass(scratch: Connection, expr: str) -> None:
    _secured_table(scratch)
    scratch.execute(
        text(f"CREATE POLICY p ON shifde.zz_rls_probe USING ({expr}) WITH CHECK ({expr})")
    )
    assert rls_violations(scratch, set()) == []


@pytest.mark.parametrize("using", [*WRITE_FORMS, f"tenant_id IS NULL OR {TENANT}"])
def test_recognised_read_forms_pass(scratch: Connection, using: str) -> None:
    _secured_table(scratch)
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    assert rls_violations(scratch, set()) == []


# ---- isolation review 2
EXISTS_BYPASSES = [
    # the subquery never looks at the outer row
    "EXISTS (SELECT 1 WHERE shifde.current_tenant_id() = shifde.current_tenant_id())",
    # a subquery over another table that ignores the outer row
    "EXISTS (SELECT 1 FROM shifde.zz_rls_other o WHERE o.tenant_id = shifde.current_tenant_id())",
    # OR true inside the subquery
    "EXISTS (SELECT 1 FROM shifde.zz_rls_other o"
    " WHERE o.id = zz_rls_probe.id AND (o.tenant_id = shifde.current_tenant_id() OR true))",
    # a well-formed membership check is still not accepted unless reviewed
    "EXISTS (SELECT 1 FROM shifde.zz_rls_other o"
    " WHERE o.id = zz_rls_probe.id AND o.tenant_id = shifde.current_tenant_id())",
]


@pytest.mark.parametrize("using", EXISTS_BYPASSES)
def test_unreviewed_exists_policy_is_reported(scratch: Connection, using: str) -> None:
    _secured_table(scratch)
    _secured_table(scratch, "shifde.zz_rls_other")
    _tenant_policy(scratch, "shifde.zz_rls_other")
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    (problem,) = rls_violations(scratch, set())
    assert problem.startswith(
        "shifde.zz_rls_probe: policy p (SELECT) USING not built from recognised"
    ), problem


def test_reviewed_expression_passes_only_with_its_exact_text(scratch: Connection) -> None:
    _secured_table(scratch)
    _secured_table(scratch, "shifde.zz_rls_other")
    _tenant_policy(scratch, "shifde.zz_rls_other")
    using = EXISTS_BYPASSES[3]
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    printed = scratch.execute(
        text(
            "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy"
            " WHERE polrelid = 'shifde.zz_rls_probe'::regclass AND polname = 'p'"
        )
    ).scalar_one()
    key: ReviewedKey = ("shifde.zz_rls_probe", "p", "SELECT", "PERMISSIVE", ("public",), "USING")
    assert rls_violations(scratch, set(), reviewed={key: printed}) == []
    (problem,) = rls_violations(scratch, set(), reviewed={key: printed.replace("o.id", "o.id + 0")})
    assert "policy p (SELECT) USING differs from its reviewed text" in problem


def test_tenant_id_is_null_in_with_check_is_reported(scratch: Connection) -> None:
    """A write rule must not let a client create rows every client sees."""
    _secured_table(scratch)
    expr = f"tenant_id IS NULL OR {TENANT}"
    scratch.execute(
        text(f"CREATE POLICY p ON shifde.zz_rls_probe USING ({TENANT}) WITH CHECK ({expr})")
    )
    (problem,) = rls_violations(scratch, set())
    assert "policy p (ALL) WITH CHECK admits tenant_id IS NULL without" in problem
    assert "USING" not in problem.split("WITH CHECK")[0]


@pytest.mark.parametrize(
    "using",
    [
        "user_id = shifde.current_tenant_id()",
        "tenant_id = shifde.current_principal_id()",
        "ref = shifde.current_principal_id()",  # like users.id, which is listed for users only
        "ref = shifde.current_tenant_id()",  # like tenants.id, which is listed for tenants only
        "NOT (user_id IS DISTINCT FROM shifde.current_tenant_id())",
    ],
)
def test_helper_compared_with_the_wrong_column_is_reported(scratch: Connection, using: str) -> None:
    _secured_table(scratch)
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    (problem,) = rls_violations(scratch, set())
    assert "not built from recognised" in problem, problem


def test_reviewed_key_columns() -> None:
    assert {
        "shifde.current_tenant_id()": frozenset({"tenant_id", "shifde.tenants.id"}),
        "shifde.current_principal_id()": frozenset({"user_id", "shifde.users.id"}),
    } == KEY_COLUMNS


@pytest.mark.parametrize(
    "using", ["ref = shifde.current_tenant_id()", "ref = shifde.current_principal_id()"]
)
def test_table_specific_key_column_passes_on_its_table_only(
    scratch: Connection, using: str
) -> None:
    # A principal key counts only where there is no tenant_id column (review 4).
    tenant_column = "tenant" in using
    _secured_table(scratch, tenant_column=tenant_column)
    _secured_table(scratch, "shifde.zz_rls_other", tenant_column=tenant_column)
    for table in ("zz_rls_probe", "zz_rls_other"):
        scratch.execute(text(f"CREATE POLICY p ON shifde.{table} FOR SELECT USING ({using})"))
    keys = {
        "shifde.current_tenant_id()": frozenset({"tenant_id", "shifde.zz_rls_probe.ref"}),
        "shifde.current_principal_id()": frozenset({"user_id", "shifde.zz_rls_probe.ref"}),
    }
    (problem,) = rls_violations(scratch, set(), key_columns=keys)
    assert problem.startswith("shifde.zz_rls_other: policy p (SELECT) USING not built"), problem


def test_rewrite_rule_is_reported(scratch: Connection) -> None:
    """Rule actions run as the table owner (BYPASSRLS): a rule can copy any client's rows."""
    _secured_table(scratch)
    _tenant_policy(scratch)
    _secured_table(scratch, "shifde.zz_rls_other")
    _tenant_policy(scratch, "shifde.zz_rls_other")
    scratch.execute(
        text(
            "CREATE RULE leak AS ON INSERT TO shifde.zz_rls_probe DO ALSO"
            " INSERT INTO shifde.zz_rls_other SELECT id, NEW.tenant_id, user_id, ref"
            " FROM shifde.zz_rls_other"
        )
    )
    assert rls_violations(scratch, set()) == ["shifde.zz_rls_probe: rewrite rule leak"]


def test_rewrite_rule_on_a_global_table_or_view_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE shifde.zz_rls_global (id int PRIMARY KEY)"))
    scratch.execute(text("CREATE RULE r AS ON DELETE TO shifde.zz_rls_global DO INSTEAD NOTHING"))
    scratch.execute(text("CREATE VIEW public.zz_rls_view WITH (security_invoker) AS SELECT 1 AS x"))
    scratch.execute(text("CREATE RULE v AS ON INSERT TO public.zz_rls_view DO INSTEAD NOTHING"))
    assert rls_violations(scratch, {"zz_rls_global"}) == [
        "public.zz_rls_view: rewrite rule v",
        "shifde.zz_rls_global: rewrite rule r",
    ]


def test_lookalike_helper_in_another_schema_is_reported(scratch: Connection) -> None:
    scratch.execute(
        text(
            "CREATE FUNCTION public.current_tenant_id() RETURNS uuid LANGUAGE sql AS"
            " $$ SELECT NULL::uuid $$"
        )
    )
    _secured_table(scratch)
    scratch.execute(
        text(
            "CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT"
            " USING (tenant_id = public.current_tenant_id() OR true)"
        )
    )
    (problem,) = rls_violations(scratch, set())
    assert "does not reference shifde.current_tenant_id()" in problem


def test_view_without_security_invoker_is_reported(scratch: Connection) -> None:
    scratch.execute(text("CREATE VIEW shifde.zz_rls_view AS SELECT 1 AS x"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_view: view without security_invoker=true"
    ]


def test_view_with_security_invoker_passes(scratch: Connection) -> None:
    scratch.execute(
        text("CREATE VIEW shifde.zz_rls_view WITH (security_invoker = on) AS SELECT 1 AS x")
    )
    scratch.execute(
        text("CREATE VIEW public.zz_rls_view2 WITH (security_invoker) AS SELECT 1 AS x")
    )
    assert rls_violations(scratch, set()) == []


def test_view_with_security_invoker_false_is_reported(scratch: Connection) -> None:
    scratch.execute(
        text("CREATE VIEW shifde.zz_rls_view WITH (security_invoker = false) AS SELECT 1 AS x")
    )
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_view: view without security_invoker=true"
    ]


def test_materialized_view_is_reported_unless_listed(scratch: Connection) -> None:
    scratch.execute(text("CREATE MATERIALIZED VIEW shifde.zz_rls_mv AS SELECT 1 AS x"))
    assert rls_violations(scratch, set()) == [
        "shifde.zz_rls_mv: materialized view not listed in global_tables.txt"
    ]
    assert rls_violations(scratch, {"zz_rls_mv"}) == []


def test_alembic_version_is_exempt_only_in_schema_shifde(scratch: Connection) -> None:
    scratch.execute(text("CREATE TABLE public.alembic_version (version_num text)"))
    assert rls_violations(scratch, set()) == [
        "public.alembic_version: row level security not enabled, "
        "row level security not forced, no policy"
    ]


# ---- isolation review 3
ADMIN_ON = "pg_catalog.set_config('app.platform_admin', 'on', true)"
TENANT_B = "pg_catalog.set_config('app.tenant_id', '01900000-0000-7000-8000-0000000000bb', true)"


@pytest.mark.parametrize(
    ("policy", "label"),
    [
        # review 3, item 2: a restrictive policy that turns the admin flag on
        (
            f"narrow AS RESTRICTIVE FOR SELECT USING ({ADMIN_ON} IS NOT NULL)",
            "policy narrow on shifde.zz_rls_probe USING",
        ),
        (
            f"narrow AS RESTRICTIVE FOR INSERT WITH CHECK ({ADMIN_ON} IS NOT NULL)",
            "policy narrow on shifde.zz_rls_probe WITH CHECK",
        ),
        # review 3, item 2: a recognised conjunct carried a context-setting one
        (
            f"p FOR SELECT USING ({TENANT} AND {TENANT_B} IS NOT NULL)",
            "policy p on shifde.zz_rls_probe USING",
        ),
    ],
)
def test_policy_that_writes_the_client_context_is_reported(
    scratch: Connection, policy: str, label: str
) -> None:
    _secured_table(scratch)
    _tenant_policy(scratch)
    scratch.execute(text(f"CREATE POLICY {policy.replace(' ', ' ON shifde.zz_rls_probe ', 1)}"))
    problems = rls_violations(scratch, set())
    assert [p.split(": ")[0] for p in problems] == [label], problems


# ---- isolation review 4
USER_KEY = "user_id = shifde.current_principal_id()"


@pytest.mark.parametrize(
    "policy",
    [
        # SETH's own_row write rule: A's user wrote rows with tenant_id NULL and = B, B read them
        f"FOR INSERT WITH CHECK ({USER_KEY})",
        # A's user read their own row filed under B
        f"FOR SELECT USING ({USER_KEY})",
        f"FOR SELECT USING ({TENANT} OR {USER_KEY})",
        # evades the tenant_id IS NULL text match
        f"FOR INSERT WITH CHECK (NOT (tenant_id IS NOT NULL) AND {USER_KEY})",
    ],
)
def test_user_key_does_not_satisfy_a_policy_on_a_tenant_table(
    scratch: Connection, policy: str
) -> None:
    """Review 4, item 1: on a relation with tenant_id only tenant atoms count."""
    _secured_table(scratch)
    scratch.execute(
        text(
            "CREATE POLICY read_shared ON shifde.zz_rls_probe FOR SELECT"
            f" USING (tenant_id IS NULL OR {TENANT})"
        )
    )
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe {policy}"))
    (problem,) = rls_violations(scratch, set())
    assert problem.startswith("shifde.zz_rls_probe: policy p ("), problem
    assert "not built from recognised" in problem, problem


NULL_TENANT_BY_A_CLIENT = [
    # review 3, item 3: the recognised user_id conjunct carried the AND
    f"{TENANT} OR (tenant_id IS NULL AND user_id = shifde.current_principal_id())",
    "tenant_id IS NULL AND user_id = shifde.current_principal_id()",
    f"(tenant_id IS NULL OR {TENANT}) AND user_id = shifde.current_principal_id()",
    f"((tenant_id IS NULL AND id > 0) OR {TENANT}) AND user_id = shifde.current_principal_id()",
]


@pytest.mark.parametrize("check", NULL_TENANT_BY_A_CLIENT)
def test_with_check_admitting_a_tenantless_row_is_reported(scratch: Connection, check: str) -> None:
    _secured_table(scratch)
    _tenant_policy(scratch)
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR INSERT WITH CHECK ({check})"))
    (problem,) = rls_violations(scratch, set())
    assert (
        "policy p (INSERT) WITH CHECK admits tenant_id IS NULL without"
        " shifde.is_platform_admin_ctx()" in problem
    ), problem


@pytest.mark.parametrize(
    "cmd",
    [
        # review 3, item 4: A deleted (or could take over) the shared row
        f"ALL USING (tenant_id IS NULL OR {TENANT}) WITH CHECK ({TENANT})",
        f"UPDATE USING (tenant_id IS NULL OR {TENANT}) WITH CHECK ({TENANT})",
        f"DELETE USING (tenant_id IS NULL OR {TENANT})",
        f"DELETE USING ({TENANT} OR (tenant_id IS NULL AND user_id = shifde.current_principal_id()))",
    ],
)
def test_shared_rows_writable_by_a_client_are_reported(scratch: Connection, cmd: str) -> None:
    _secured_table(scratch)
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR {cmd}"))
    (problem,) = rls_violations(scratch, set())
    assert "USING admits tenant_id IS NULL without shifde.is_platform_admin_ctx()" in problem, (
        problem
    )
    assert "WITH CHECK" not in problem, problem


@pytest.mark.parametrize("cmd", ["ALL", "UPDATE", "DELETE", "INSERT", "SELECT"])
def test_shared_rows_guarded_by_the_admin_flag_pass(scratch: Connection, cmd: str) -> None:
    _secured_table(scratch)
    expr = f"(tenant_id IS NULL AND shifde.is_platform_admin_ctx() AND id > 0) OR {TENANT}"
    using = "" if cmd == "INSERT" else f" USING ({expr})"
    check = "" if cmd in ("SELECT", "DELETE") else f" WITH CHECK ({expr})"
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR {cmd}{using}{check}"))
    assert rls_violations(scratch, set()) == []


def _probe_key(
    clause: str, cmd: str = "SELECT", kind: str = "PERMISSIVE", roles: tuple[str, ...] = ("public",)
) -> ReviewedKey:
    return ("shifde.zz_rls_probe", "p", cmd, kind, roles, clause)


def test_reviewed_expression_is_keyed_by_command_and_roles(scratch: Connection) -> None:
    """Review 3, item 6: text reviewed for one command or role does not cover another."""
    _secured_table(scratch)
    _secured_table(scratch, "shifde.zz_rls_other")
    _tenant_policy(scratch, "shifde.zz_rls_other")
    using = EXISTS_BYPASSES[3]
    scratch.execute(text(f"CREATE POLICY p ON shifde.zz_rls_probe FOR SELECT USING ({using})"))
    printed = scratch.execute(
        text(
            "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy"
            " WHERE polrelid = 'shifde.zz_rls_probe'::regclass AND polname = 'p'"
        )
    ).scalar_one()
    assert rls_violations(scratch, set(), reviewed={_probe_key("USING"): printed}) == []
    for other in (
        _probe_key("USING", cmd="ALL"),
        _probe_key("USING", kind="RESTRICTIVE"),
        _probe_key("USING", roles=("shifde_app",)),
    ):
        (problem,) = rls_violations(scratch, set(), reviewed={other: printed})
        assert "not built from recognised" in problem, problem
    scratch.execute(text("ALTER POLICY p ON shifde.zz_rls_probe TO shifde_app"))
    (problem,) = rls_violations(scratch, set(), reviewed={_probe_key("USING"): printed})
    assert "not built from recognised" in problem, problem
    reviewed = {_probe_key("USING", roles=("shifde_app",)): printed}
    assert rls_violations(scratch, set(), reviewed=reviewed) == []


@pytest.mark.parametrize(
    ("expr", "branches"),
    [
        ("(a OR (b AND c))", ["a", "(b AND c)"]),
        ("(x = 'a OR b')", ["x = 'a OR b'"]),
        ("CASE WHEN a OR b THEN c END OR d", ["CASE WHEN a OR b THEN c END", "d"]),
    ],
)
def test_split_top_respects_nesting(expr: str, branches: list[str]) -> None:
    assert _split_top(_strip_parens(expr), "OR") == branches
