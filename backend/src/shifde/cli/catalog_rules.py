"""The isolation rules of the schema tests, applied to a live catalog (restore check
`catalog rules`, issue #197).

This module mirrors, with the same semantics and the same problem strings:

* `tests/schema/test_rls_schema.py`: `rls_violations` (forced row-level security, at least one
  policy, tenant-keyed permissive policies with an explicit WITH CHECK, views with
  `security_invoker`, no materialized view or foreign table, no rewrite rule) and
  `privilege_problems` (no REFERENCES, TRIGGER or TRUNCATE for the app role);
* `tests/schema/test_function_schema.py`: `definer_violations` (every SECURITY DEFINER function
  allowlisted by full signature, search_path pinned without `public`/`$user` and `pg_temp` last,
  owned by the schema owner, EXECUTE for nobody but the owner and the app role);
* `tests/schema/catalog_scan.py`: the context-write scan of policy expressions.

The rules and their reasons are documented in those files. `tests/schema/*` is protected and
`src` never imports `tests`, so the rules are copied here; `tests/cli/test_catalog_rules.py`
compares the data and the results of both copies, so neither can drift without a failing test.
Changing any list here needs isolation review, as in the tests (docs/build/CHARTER.md
section 3). Everything is read from the catalog; the only write is the transaction-local
`search_path = pg_catalog` for the deparsers, which the caller's transaction ends.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import Connection, text

EXEMPT = frozenset({("shifde", "alembic_version")})

# Tables in schema `shifde` exempt from the row-level security rules: they hold no client-owned
# data. Mirrors tests/schema/global_tables.txt (reasons in full there).
GLOBAL_TABLES: dict[str, str] = {
    "adapter_conformance_runs": "platform-wide conformance evidence per adapter kind",
    "permissions": "platform-wide permission catalog (DECISIONS B-401)",
    "installation_settings": "one installation-wide row (DECISIONS B-1002)",
    "run_state_transitions": "the legal run status changes (DECISIONS B-2104)",
    "workers": "one heartbeat row per worker process (DECISIONS B-2104)",
}

# helper -> the columns it may be compared with: a bare name holds on every table, a
# `<schema>.<table>.<column>` entry only on that table.
KEY_COLUMNS: dict[str, frozenset[str]] = {
    "shifde.current_tenant_id()": frozenset({"tenant_id", "shifde.tenants.id"}),
    "shifde.current_principal_id()": frozenset({"user_id", "shifde.users.id"}),
}

# Relations whose rows are per-person identity state, never client content: principal keys on
# bare columns count only here (isolation review 5, item 1; reasons in the RLS schema test).
PRINCIPAL_TABLES = frozenset(
    {
        "shifde.sessions",
        "shifde.local_credentials",
        "shifde.user_identities",
        "shifde.login_lockouts",
    }
)

# (relation, policy, command, "PERMISSIVE" | "RESTRICTIVE", roles, "USING" | "WITH CHECK")
# -> exact pg_get_expr text reviewed by isolation review (reasons in the RLS schema test).
ReviewedKey = tuple[str, str, str, str, tuple[str, ...], str]
REVIEWED_EXPRESSIONS: dict[ReviewedKey, str] = {
    (
        "shifde.users",
        "users_select",
        "SELECT",
        "PERMISSIVE",
        ("public",),
        "USING",
    ): (
        "((id = shifde.current_principal_id()) OR shifde.is_platform_admin_ctx() OR (EXISTS "
        "( SELECT 1\n   FROM shifde.memberships m\n  WHERE ((m.user_id = users.id) AND "
        "(m.tenant_id = shifde.current_tenant_id())))))"
    ),
    **{
        (relation, "platform_admin_all", "ALL", "PERMISSIVE", ("public",), clause): (
            "shifde.is_platform_admin_ctx()"
        )
        for relation in ("shifde.groups", "shifde.group_members")
        for clause in ("USING", "WITH CHECK")
    },
    (
        "shifde.groups",
        "member_read",
        "SELECT",
        "PERMISSIVE",
        ("public",),
        "USING",
    ): (
        "((shifde.current_tenant_id() IS NULL) AND (EXISTS ( SELECT 1\n   FROM "
        "shifde.group_members gm\n  WHERE ((gm.group_id = groups.id) AND (gm.user_id = "
        "shifde.current_principal_id())))))"
    ),
    (
        "shifde.group_members",
        "own_row",
        "SELECT",
        "PERMISSIVE",
        ("public",),
        "USING",
    ): "((user_id = shifde.current_principal_id()) AND (shifde.current_tenant_id() IS NULL))",
}

# TRUNCATE ignores row-level security; TRIGGER and REFERENCES let the role run code or hold
# constraints over rows it cannot see. Checked on every relation, PUBLIC grants included.
FORBIDDEN_PRIVILEGES = ("REFERENCES", "TRIGGER", "TRUNCATE")

# `<schema>.<name>` (the zero-argument function) or `<schema>.<name>(<argument types>)` of every
# function that may be SECURITY DEFINER (reasons in the function schema test).
SECURITY_DEFINER_ALLOWLIST = frozenset(
    {
        "shifde.shifde_resolve_local_login(text)",
        "shifde.shifde_resolve_session(bytea)",
        "shifde.shifde_revoke_session(bytea)",
        "shifde.shifde_revoke_user_sessions(uuid)",
        "shifde.shifde_create_invite_token(uuid, bytea, timestamp with time zone)",
        "shifde.shifde_redeem_setup_token(bytea, text, integer)",
        "shifde.shifde_my_membership_tenants",
        "shifde.shifde_resolve_api_key(text)",
        "shifde.shifde_resolve_idp(uuid)",
        "shifde.shifde_resolve_identity(text, text, text, uuid)",
        "shifde.shifde_jit_create_user(uuid, uuid, text, text, text, text)",
        "shifde.shifde_begin_oidc_request(uuid, uuid, bytea, bytea, bytea, text, integer)",
        "shifde.shifde_my_memberships",
        "shifde.shifde_may_bind_group(uuid, uuid)",
        "shifde.shifde_is_client_owner(uuid)",
        "shifde.shifde_bound_group_admin_ids(uuid)",
        "shifde.shifde_bound_groups",
        "shifde.shifde_active_group_ids(uuid)",
        "shifde.shifde_sync_idp_groups(uuid, uuid, text[])",
        "shifde.shifde_installation_idp_secret(uuid)",
        "shifde.shifde_consume_oidc_request(bytea, uuid)",
        "shifde.rate_card_versions_guard",
        "shifde.shifde_claim_jobs(text, integer, integer, integer)",
        "shifde.shifde_reap_expired_leases",
        "shifde.shifde_ops_stats(integer)",
        "shifde.delivery_claim_platform(text, text)",
        "shifde.delivery_resolve_executor(text, bytea)",
        "shifde.delivery_lease_job(uuid, uuid, text[])",
    }
)

# The context helpers, exempt by oid from the full-catalog scan of the tests, and the labels
# allowed to write the client context (empty: nothing may).
CONTEXT_HELPERS = ("current_tenant_id", "current_principal_id", "is_platform_admin_ctx")
CONTEXT_WRITE_ALLOWLIST: dict[str, str] = {}

_PIN_SEARCH_PATH = text("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)")


# --------------------------------------------------------------------------- row-level security
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
    principal_table: bool = False  # listed in PRINCIPAL_TABLES: bare principal keys count


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
            principal = match["helper"] != _TENANT_HELPER
            if clause.tenant_column and principal:
                return False  # a principal key may narrow (in an AND) but not satisfy
            allowed = clause.keys.get(match["helper"], frozenset())
            column = match["col"]
            if f"{clause.relation}.{column}" in allowed:
                return True  # a table-specific key, reviewed for this table
            return column in allowed and (clause.principal_table or not principal)
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


def _expression_problem(expr: str | None, clause: _Clause, reviewed: str | None) -> str | None:
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
        if not clause.principal_table and _recognised(expr, replace(clause, principal_table=True)):
            return (
                "keyed to shifde.current_principal_id() on a relation not listed in"
                " PRINCIPAL_TABLES"
            )
        return "not built from recognised tenant-keyed forms"
    return None


def _reviewed_key(
    relation: str, name: str, cmd: str, permissive: bool, roles: list[str], clause: str
) -> ReviewedKey:
    kind = "PERMISSIVE" if permissive else "RESTRICTIVE"
    return (relation, name, _CMD[cmd], kind, tuple(sorted(roles)), clause)


def _policy_problems(conn: Connection) -> dict[tuple[str, str], list[str]]:
    conn.execute(_PIN_SEARCH_PATH)
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
            clause = _Clause(
                relation,
                kind == "USING" and cmd == "r",
                KEY_COLUMNS,
                tenant_column,
                relation in PRINCIPAL_TABLES,
            )
            key = _reviewed_key(relation, name, cmd, permissive, roles, kind)
            why = _expression_problem(expr, clause, REVIEWED_EXPRESSIONS.get(key))
            if why:
                problems.append(f"{label} {kind} {why}: {expr}")
    return found


def rls_violations(conn: Connection) -> list[str]:
    """`schema.relation: problem` for every relation that breaks the row-level security rules."""
    relations = conn.execute(_RELATIONS).all()
    policy_problems = _policy_problems(conn)
    policy_count: dict[tuple[str, str], int] = {}
    for schema, table, *_ in conn.execute(_POLICIES):
        policy_count[(schema, table)] = policy_count.get((schema, table), 0) + 1
    problems: list[str] = []
    for schema, table, kind, enabled, forced, invoker in relations:
        if (schema, table) in EXEMPT or (schema == "shifde" and table in GLOBAL_TABLES):
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
    problems += _policy_context_writes(conn)
    return sorted(problems)


# --------------------------------------------------------------------------- context writes
_POLICY_EXPRESSIONS = text(
    r"""SELECT p.polname || ' on ' || n.nspname || '.' || c.relname || ' ' || x.clause,
              x.expr
         FROM pg_catalog.pg_policy p
         JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN LATERAL (VALUES
             ('USING', pg_catalog.pg_get_expr(p.polqual, p.polrelid)),
             ('WITH CHECK', pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid))) x(clause, expr)
        WHERE x.expr IS NOT NULL AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'"""
)

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
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _context_reasons(source: str) -> list[str]:
    """Why `source` may write the client context, read as written and with comments and double
    quotes removed (empty if it cannot be seen to)."""
    stripped = _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", source))
    variants = [source, stripped.replace('"', "")]
    return [why for pattern, why in _PATTERNS if any(pattern.search(v) for v in variants)]


def _policy_context_writes(conn: Connection) -> list[str]:
    """`policy <name> on <relation> <clause>: <reasons>` for every policy expression that may
    write the client context."""
    conn.execute(_PIN_SEARCH_PATH)
    found = [
        (f"policy {label}", _context_reasons(source or ""))
        for label, source in conn.execute(_POLICY_EXPRESSIONS)
    ]
    return sorted(
        f"{label}: {', '.join(reasons)}"
        for label, reasons in found
        if reasons and label not in CONTEXT_WRITE_ALLOWLIST
    )


# --------------------------------------------------------------------------- grants
_PRIVILEGED_RELATIONS = text(
    r"""SELECT n.nspname || '.' || c.relname AS rel, p.priv
         FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN pg_catalog.unnest(CAST(:privs AS text[])) AS p(priv)
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
          AND (pg_catalog.has_table_privilege(:role, c.oid, p.priv)
               OR (p.priv = 'REFERENCES'
                   AND pg_catalog.has_any_column_privilege(:role, c.oid, 'REFERENCES')))
        ORDER BY 1, 2"""
)


def privilege_problems(conn: Connection, role: str) -> list[str]:
    """`relation: <role> holds <privilege>` for every forbidden privilege `role` holds."""
    rows = conn.execute(_PRIVILEGED_RELATIONS, {"role": role, "privs": list(FORBIDDEN_PRIVILEGES)})
    return [f"{rel}: {role} holds {priv}" for rel, priv in rows]


# --------------------------------------------------------------------------- SECURITY DEFINER
_FUNCTIONS = text(
    r"""SELECT n.nspname, p.proname, p.prosecdef,
              pg_catalog.pg_get_function_identity_arguments(p.oid) AS args,
              pg_catalog.oidvectortypes(p.proargtypes) AS argtypes,
              pg_catalog.pg_get_userbyid(p.proowner) AS owner, p.proconfig,
              p.proacl IS NULL AS default_acl,
              ARRAY(SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                ELSE pg_catalog.pg_get_userbyid(a.grantee) END
                      FROM pg_catalog.aclexplode(p.proacl) a
                     WHERE a.privilege_type = 'EXECUTE'
                     ORDER BY 1) AS executors
         FROM pg_catalog.pg_proc p
         JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
        ORDER BY n.nspname, p.proname, p.oid"""
)


def _search_path(config: list[str] | None) -> str | None:
    for entry in config or []:
        name, _, value = entry.partition("=")
        if name.lower() == "search_path":
            return value
    return None


def _signature(where: str, argtypes: str) -> str:
    """The allowlist key of a function: `<schema>.<name>` with no arguments, else
    `<schema>.<name>(<argument types>)`."""
    return where if argtypes == "" else f"{where}({argtypes})"


def definer_violations(conn: Connection, owner_role: str, app_role: str) -> list[str]:
    """`<schema>.<name>: problem` for every SECURITY DEFINER function that is not allowlisted by
    full signature or not locked down, and for every overload of an allowlisted name."""
    problems: list[str] = []
    names = {key.partition("(")[0] for key in SECURITY_DEFINER_ALLOWLIST}
    for row in conn.execute(_FUNCTIONS):
        where = f"{row.nspname}.{row.proname}"
        listed = _signature(where, row.argtypes) in SECURITY_DEFINER_ALLOWLIST
        if where in names and not listed:
            problems.append(f"{where}({row.args}): overload of an allowlisted function")
        if not row.prosecdef:
            continue
        if not listed:
            problems.append(f"{where}: SECURITY DEFINER but not on the allowlist")
        path = _search_path(row.proconfig)
        if path is None:
            problems.append(f"{where}: search_path not pinned")
        elif re.search(r"\bpublic\b|\$user", path):
            problems.append(f"{where}: search_path {path} includes public or $user")
        elif [p.strip().strip('"') for p in path.split(",")][-1] != "pg_temp":
            problems.append(f"{where}: search_path {path} must list pg_temp last")
        if row.owner != owner_role:
            problems.append(f"{where}: owned by {row.owner}, not {owner_role}")
        executors = ["PUBLIC"] if row.default_acl else list(row.executors)
        extra = [e for e in executors if e not in (row.owner, app_role)]
        if extra:
            problems.append(f"{where}: EXECUTE granted to {', '.join(extra)}")
    return problems
