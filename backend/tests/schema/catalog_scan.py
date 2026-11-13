"""Nothing in the catalog may write the client context (isolation review 3, items 1 and 2).

Row-level security trusts three transaction settings: `app.tenant_id`, `app.principal_id` and
`app.platform_admin`, read only through the pinned helpers `shifde.current_tenant_id()`,
`shifde.current_principal_id()` and `shifde.is_platform_admin_ctx()`. Any code a query can reach
that writes them -- a plain function, a view, a policy (permissive or restrictive), a default --
can hand one client another client's rows for the rest of the transaction.

`context_write_violations` reads, in every non-system schema (`pg_catalog`,
`information_schema`, `pg_toast*` and `pg_temp_*` excluded):

* functions and procedures: the body (`prosrc`, or the SQL-standard body), the `SET` clauses
  (`proconfig`) and the argument defaults (they run in the caller);
* views and materialized views (`pg_get_viewdef`);
* every policy expression, USING and WITH CHECK, permissive and restrictive;
* column defaults and generation expressions; domain defaults;
* every constraint (CHECK, including domain CHECKs); index expressions and predicates; trigger
  definitions (the WHEN clause and the arguments); extended statistics expressions;
* aggregates: the schema-qualified names of every support function (transition, final,
  combine, serial, deserial and their moving-aggregate forms) and the initial states
  (`agginitval`, `aggminitval`) -- `SFUNC = pg_catalog.set_config` with `INITCOND =
  'app.platform_admin'` writes the context (isolation review 4, item 2);

and reports each object whose text

* mentions `set_config`, `pg_settings` (written through `set_config` by a rule), or one of the
  three setting names (`app . tenant_id` with any spacing or quoting: a `SET [LOCAL] ...` form
  needs the name), case-insensitive, also after comments and double quotes are removed;
* calls a builtin that runs a query given as text (`query_to_xml*`, `ts_stat`, `ts_rewrite`): the
  query can be assembled so that no name appears;

and, in function bodies (raw source, where the text is not normalised by PostgreSQL), also

* uses `EXECUTE` (dynamic SQL or a prepared statement: the scan cannot see what it runs) or a
  `U&` escape (it can spell any name);
* is written in a language other than `sql`, `plpgsql`, `c` or `internal` (not scanned).

Exempt: exactly the three context helpers, by oid (`to_regprocedure('shifde.<name>()')`), whose
bodies read the settings; an overload with the same name is not exempt. Anything else needs an
entry in `CONTEXT_WRITE_ALLOWLIST` (`"<label>"` as reported, with a reason; adding one needs
isolation review). The allowlist is empty.

Deparsed text (`pg_get_viewdef`, `pg_get_expr`, ...) is read under `search_path = pg_catalog`.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping

from sqlalchemy import Connection, text

# "<label>" (as reported, e.g. "function shifde.f(x integer)") -> why it may write the context.
# Adding one needs isolation review. Empty: nothing in the schema writes the context.
CONTEXT_WRITE_ALLOWLIST: dict[str, str] = {}

CONTEXT_HELPERS = ("current_tenant_id", "current_principal_id", "is_platform_admin_ctx")

ALL_KINDS = frozenset(
    {
        "function",
        "view",
        "policy",
        "default",
        "domain default",
        "constraint",
        "index",
        "trigger",
        "statistics",
        "aggregate",
    }
)

_SCANNED_LANGUAGES = frozenset({"sql", "plpgsql", "c", "internal"})

_SYSTEM = r"""{ns} NOT IN ('pg_catalog', 'information_schema')
          AND {ns} NOT LIKE 'pg\_toast%' AND {ns} NOT LIKE 'pg\_temp\_%'"""


def _system(column: str) -> str:
    return _SYSTEM.format(ns=column)


_EXEMPT = text(
    "SELECT pg_catalog.to_regprocedure(n)::oid FROM pg_catalog.unnest(CAST(:names AS text[])) n"
)

_FUNCTIONS = text(
    f"""SELECT p.oid,
              n.nspname || '.' || p.proname
                || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS label,
              l.lanname, p.prosrc,
              CASE WHEN p.prosqlbody IS NOT NULL
                   THEN pg_catalog.pg_get_function_sqlbody(p.oid) END AS sqlbody,
              pg_catalog.array_to_string(p.proconfig, ' ') AS config,
              pg_catalog.pg_get_function_arguments(p.oid) AS args
         FROM pg_catalog.pg_proc p
         JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         JOIN pg_catalog.pg_language l ON l.oid = p.prolang
        WHERE {_system("n.nspname")}"""
)

_EXPRESSIONS: dict[str, str] = {
    # An aggregate's pg_proc row has no body (`aggregate_dummy`): read its support functions
    # (schema-qualified names) and initial states from pg_aggregate.
    "aggregate": f"""SELECT n.nspname || '.' || p.proname
                || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')',
              pg_catalog.concat_ws(' ', a.agginitval, a.aggminitval,
                (SELECT pg_catalog.string_agg(fn.pronamespace::pg_catalog.regnamespace::text
                                              || '.' || fn.proname, ' ')
                   FROM pg_catalog.pg_proc fn
                  WHERE fn.oid IN (a.aggtransfn, a.aggfinalfn, a.aggcombinefn, a.aggserialfn,
                                   a.aggdeserialfn, a.aggmtransfn, a.aggminvtransfn,
                                   a.aggmfinalfn)))
         FROM pg_catalog.pg_aggregate a
         JOIN pg_catalog.pg_proc p ON p.oid = a.aggfnoid
         JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE {_system("n.nspname")}""",
    "view": f"""SELECT n.nspname || '.' || c.relname, pg_catalog.pg_get_viewdef(c.oid)
         FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('v', 'm') AND {_system("n.nspname")}""",
    "policy": f"""SELECT p.polname || ' on ' || n.nspname || '.' || c.relname || ' ' || x.clause,
              x.expr
         FROM pg_catalog.pg_policy p
         JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN LATERAL (VALUES
             ('USING', pg_catalog.pg_get_expr(p.polqual, p.polrelid)),
             ('WITH CHECK', pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid))) x(clause, expr)
        WHERE x.expr IS NOT NULL AND {_system("n.nspname")}""",
    "default": f"""SELECT n.nspname || '.' || c.relname || '.' || a.attname,
              pg_catalog.pg_get_expr(d.adbin, d.adrelid)
         FROM pg_catalog.pg_attrdef d
         JOIN pg_catalog.pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
         JOIN pg_catalog.pg_class c ON c.oid = d.adrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE {_system("n.nspname")}""",
    "domain default": f"""SELECT n.nspname || '.' || t.typname,
              pg_catalog.pg_get_expr(t.typdefaultbin, 0)
         FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typdefaultbin IS NOT NULL AND {_system("n.nspname")}""",
    "constraint": f"""SELECT n.nspname || '.' || k.conname || ' on '
                || CASE WHEN k.conrelid <> 0 THEN k.conrelid::pg_catalog.regclass::text
                        ELSE k.contypid::pg_catalog.regtype::text END,
              pg_catalog.pg_get_constraintdef(k.oid)
         FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_namespace n ON n.oid = k.connamespace
        WHERE {_system("n.nspname")}""",
    "index": f"""SELECT n.nspname || '.' || c.relname, pg_catalog.pg_get_indexdef(i.indexrelid)
         FROM pg_catalog.pg_index i
         JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE {_system("n.nspname")}""",
    "trigger": f"""SELECT t.tgname || ' on ' || n.nspname || '.' || c.relname,
              pg_catalog.pg_get_triggerdef(t.oid)
         FROM pg_catalog.pg_trigger t
         JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE {_system("n.nspname")}""",
    "statistics": f"""SELECT n.nspname || '.' || s.stxname,
              pg_catalog.pg_get_statisticsobjdef(s.oid)
         FROM pg_catalog.pg_statistic_ext s
         JOIN pg_catalog.pg_namespace n ON n.oid = s.stxnamespace
        WHERE {_system("n.nspname")}""",
}

_FLAGS = re.IGNORECASE
_PATTERNS = (
    (re.compile(r"\bset_config\b", _FLAGS), "set_config"),
    (re.compile(r"\bpg_settings\b", _FLAGS), "pg_settings"),
    (
        re.compile(r"\bapp\s*\.\s*(tenant_id|principal_id|platform_admin)\b", _FLAGS),
        "names app.<context setting>",
    ),
    (
        re.compile(r"\b(query_to_xml\w*|ts_stat|ts_rewrite)\b", _FLAGS),
        "runs a query given as text",
    ),
)
_BODY_PATTERNS = (
    (re.compile(r"\bEXECUTE\b", _FLAGS), "EXECUTE (dynamic SQL)"),
    (re.compile(r"\bU&", _FLAGS), "U& escape"),
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _variants(source: str) -> list[str]:
    """`source` as written and with comments and double quotes removed (either form can hide a
    name: a comment or quotes may split it, and stripping a comment may eat code)."""
    stripped = _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", source))
    return [source, stripped.replace('"', "")]


def context_reasons(source: str, *, body: bool = False) -> list[str]:
    """Why `source` may write the client context (empty if it cannot be seen to)."""
    patterns = _PATTERNS + _BODY_PATTERNS if body else _PATTERNS
    variants = _variants(source)
    return [why for pattern, why in patterns if any(pattern.search(v) for v in variants)]


def _function_findings(conn: Connection, exempt: set[int]) -> Iterable[tuple[str, list[str]]]:
    for row in conn.execute(_FUNCTIONS):
        if row.oid in exempt:
            continue
        reasons: list[str] = []
        if row.lanname not in _SCANNED_LANGUAGES:
            reasons.append(f"language {row.lanname} is not scanned")
        reasons += context_reasons(row.sqlbody or row.prosrc or "", body=True)
        reasons += [f"{why} in SET clause" for why in context_reasons(row.config or "")]
        reasons += [f"{why} in an argument default" for why in context_reasons(row.args or "")]
        yield f"function {row.label}", reasons


def context_write_violations(
    conn: Connection,
    *,
    kinds: Collection[str] = ALL_KINDS,
    allowlist: Mapping[str, str] | None = None,
) -> list[str]:
    """`<kind> <object>: <reasons>` for every object of `kinds` that may write the context."""
    allowed = CONTEXT_WRITE_ALLOWLIST if allowlist is None else allowlist
    # Pin the search path for the deparsers (transaction-local; the caller's transaction ends it).
    conn.execute(text("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)"))
    exempt = {
        oid
        for oid in conn.execute(
            _EXEMPT, {"names": [f"shifde.{name}()" for name in CONTEXT_HELPERS]}
        ).scalars()
        if oid is not None
    }
    found: list[tuple[str, list[str]]] = []
    if "function" in kinds:
        found += _function_findings(conn, exempt)
    for kind, query in _EXPRESSIONS.items():
        if kind in kinds:
            found += [
                (f"{kind} {label}", context_reasons(source or ""))
                for label, source in conn.execute(text(query))
            ]
    return sorted(
        f"{label}: {', '.join(reasons)}"
        for label, reasons in found
        if reasons and label not in allowed
    )
