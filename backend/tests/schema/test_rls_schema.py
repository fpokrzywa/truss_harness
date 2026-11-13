"""Every relation that can hold client data is isolated by forced row-level security with
tenant-keyed policies (Review Focus item 3; isolation review 1, items 1, 2 and 11).

Scope: every relation in every non-system schema (SETH's tables live in `shifde`; `public` is
checked too so a relation created there by mistake cannot escape), except exactly
`shifde.alembic_version` and the relations listed, with a reason, in `global_tables.txt`.

Rules, per relation kind:

* Tables (`r`) and partitioned tables (`p`): row-level security enabled AND forced, at least one
  policy, and every PERMISSIVE policy tenant-keyed (below). RESTRICTIVE policies only narrow
  access, so they are not constrained.
* Views (`v`): `security_invoker` must be true. A view otherwise runs with its owner's rights, and
  `shifde_owner` has BYPASSRLS, so it would return every client's rows to `shifde_app`.
* Materialized views (`m`) and foreign tables (`f`): not allowed at all unless listed in
  `global_tables.txt`. Neither can carry row-level security.

A permissive policy is tenant-keyed when:

1. Its USING expression (commands ALL, SELECT, UPDATE, DELETE) and its WITH CHECK expression
   (commands ALL, INSERT, UPDATE) each reference `shifde.current_tenant_id()` or, for
   principal-owned rows, `shifde.current_principal_id()`.
2. WITH CHECK is present explicitly on every ALL, INSERT and UPDATE policy. PostgreSQL would use
   the USING expression as the check when WITH CHECK is absent (and an INSERT policy has no USING
   at all, so its writes would be unchecked); we are stricter and require the write rule to be
   written out, so it is visible in the migration and reviewed on its own.
3. Every expression is built only from recognised forms. Top-level `OR` needs every branch
   recognised, `AND` needs at least one recognised conjunct (the rest can only narrow). The
   recognised atoms are:
     - `<column> = shifde.current_tenant_id()` / `shifde.current_principal_id()` (either side),
     - `NOT (<column> IS DISTINCT FROM <helper>)` (either side),
     - `tenant_id IS NULL` (rows shared by every client, e.g. built-in roles),
     - `shifde.is_platform_admin_ctx()` (the platform-admin flag, fail closed when unset),
     - an `EXISTS (...)` subquery that compares a column with one of the two helpers (the
       subquery itself is not parsed further: isolation review reads every such policy).
   Anything else -- `true`, `1 = 1`, `current_user = ...`, `NOT (tenant_id = ...)`, a test of
   whether the context is set (`shifde.current_tenant_id() IS NOT NULL`), `deleted_at IS NULL`
   -- is rejected. A new form needs isolation review and a change to this test.

Expressions are read with `pg_get_expr` under `search_path = pg_catalog`, so the helpers print
schema-qualified and a look-alike function in another schema cannot pass for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

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
              pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS using_expr,
              pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
         FROM pg_catalog.pg_policy p
         JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        ORDER BY n.nspname, c.relname, p.polname"""
)

_CMD = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}
_HAS_USING = {"r", "w", "d", "*"}
_HAS_CHECK = {"a", "w", "*"}
_KIND = {"m": "materialized view", "f": "foreign table"}

# --------------------------------------------------------------------------- expression rules
_HELPER = r"shifde\.current_(?:tenant|principal)_id\(\)"
_COLUMN = r"(?:[a-z_][a-z0-9_$]*\.)?(?:[a-z_][a-z0-9_$]*|\"[^\"]+\")"
_KEY_RE = re.compile(_HELPER)
_ATOMS = (
    re.compile(rf"{_COLUMN} = {_HELPER}"),
    re.compile(rf"{_HELPER} = {_COLUMN}"),
    re.compile(rf"NOT \({_COLUMN} IS DISTINCT FROM {_HELPER}\)"),
    re.compile(rf"NOT \({_HELPER} IS DISTINCT FROM {_COLUMN}\)"),
    re.compile(r"tenant_id IS NULL"),
    re.compile(r"shifde\.is_platform_admin_ctx\(\)"),
)
_EXISTS_RE = re.compile(r"EXISTS \(.*", re.DOTALL)
_EXISTS_KEY_RE = re.compile(rf"{_COLUMN} = {_HELPER}|{_HELPER} = {_COLUMN}")


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


def _recognised(expr: str) -> bool:
    expr = _strip_parens(expr)
    branches = _split_top(expr, "OR")
    if len(branches) > 1:
        return all(_recognised(b) for b in branches)
    conjuncts = _split_top(expr, "AND")
    if len(conjuncts) > 1:
        return any(_recognised(c) for c in conjuncts)
    if _EXISTS_RE.fullmatch(expr):
        return bool(_EXISTS_KEY_RE.search(expr))
    return any(atom.fullmatch(expr) for atom in _ATOMS)


def expression_problem(expr: str | None) -> str | None:
    """Why `expr` is not a tenant-keyed policy expression, or None if it is."""
    if expr is None:
        return "missing"
    if _strip_parens(expr).lower() == "true":
        return "constant true"
    if not _KEY_RE.search(expr):
        return "does not reference shifde.current_tenant_id() or shifde.current_principal_id()"
    if not _recognised(expr):
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


def _policy_problems(conn: Connection) -> dict[tuple[str, str], list[str]]:
    # Pin the search path for pg_get_expr (transaction-local; the caller's transaction ends it).
    conn.execute(text("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)"))
    found: dict[tuple[str, str], list[str]] = {}
    for schema, table, name, cmd, permissive, using, check in conn.execute(_POLICIES):
        if not permissive:
            continue
        label = f"policy {name} ({_CMD[cmd]})"
        problems = found.setdefault((schema, table), [])
        if cmd in _HAS_USING and (why := expression_problem(using)):
            problems.append(f"{label} USING {why}: {using}")
        if cmd in _HAS_CHECK and (why := expression_problem(check)):
            problems.append(f"{label} WITH CHECK {why}: {check}")
    return found


def rls_violations(conn: Connection, allowed: set[str]) -> list[str]:
    """`schema.relation: problem` for every relation that breaks the rules in the docstring."""
    relations = conn.execute(_RELATIONS).all()
    policy_problems = _policy_problems(conn)
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
    return problems


def test_every_client_table_has_forced_rls_and_tenant_keyed_policies(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as c:
        problems = rls_violations(c, global_tables())
    assert not problems, "relations not isolated by row-level security:\n" + "\n".join(problems)


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


def _secured_table(c: Connection, name: str = "shifde.zz_rls_probe") -> None:
    c.execute(text(f"CREATE TABLE {name} (id int PRIMARY KEY, tenant_id uuid, user_id uuid)"))
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


@pytest.mark.parametrize(
    "using",
    [
        TENANT,
        "shifde.current_tenant_id() = tenant_id",
        "user_id = shifde.current_principal_id()",
        f"tenant_id IS NULL OR {TENANT}",
        f"{TENANT} OR (tenant_id IS NULL AND shifde.is_platform_admin_ctx())",
        "NOT (tenant_id IS DISTINCT FROM shifde.current_tenant_id())",
        f"{TENANT} AND id > 0",
        "id = 1 AND shifde.current_principal_id() = user_id",
        "EXISTS (SELECT 1 FROM shifde.zz_rls_probe m"
        " WHERE m.id = zz_rls_probe.id AND m.tenant_id = shifde.current_tenant_id())",
    ],
)
def test_recognised_tenant_keyed_forms_pass(scratch: Connection, using: str) -> None:
    _secured_table(scratch)
    scratch.execute(
        text(f"CREATE POLICY p ON shifde.zz_rls_probe USING ({using}) WITH CHECK ({using})")
    )
    assert rls_violations(scratch, set()) == []


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
