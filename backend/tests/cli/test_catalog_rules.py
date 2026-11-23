"""`shifde.cli.catalog_rules` is a copy of the schema tests' isolation rules (issue #197): the
restore check cannot import `tests`, and `tests/schema/*` is protected. This guard keeps the two
copies equal: the rule data must match, and on the migrated test database and on each probe below
(a rolled-back owner transaction) every ported function returns exactly what its test
counterpart returns. Each probe yields at least one problem, so no comparison is vacuous.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from shifde.cli import catalog_rules
from tests.schema import catalog_scan as scan_rules
from tests.schema import test_function_schema as fn_rules
from tests.schema import test_rls_schema as rls_rules

TENANT = rls_rules.TENANT
PROBE = "shifde.zz_rls_probe"
_SECURED = [
    f"CREATE TABLE {PROBE} (id int PRIMARY KEY, tenant_id uuid, user_id uuid, ref uuid)",
    f"ALTER TABLE {PROBE} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE {PROBE} FORCE ROW LEVEL SECURITY",
]
_TENANT_POLICY = f"CREATE POLICY tenant_isolation ON {PROBE} USING ({TENANT}) WITH CHECK ({TENANT})"
_OTHER = [
    "CREATE TABLE shifde.zz_rls_other (id int PRIMARY KEY, tenant_id uuid, user_id uuid, ref uuid)",
    "ALTER TABLE shifde.zz_rls_other ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE shifde.zz_rls_other FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY tenant_isolation ON shifde.zz_rls_other USING ({TENANT}) WITH CHECK ({TENANT})",
]
_DEFINER = (
    "CREATE FUNCTION shifde.zz_definer() RETURNS int LANGUAGE sql SECURITY DEFINER {extra}"
    " AS $$ SELECT 1 $$"
)
_REVOKE = "REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"
_PINNED = "SET search_path = pg_catalog, pg_temp"

PROBES: dict[str, list[str]] = {
    "rls not forced": [
        f"CREATE TABLE {PROBE} (id int PRIMARY KEY, tenant_id uuid)",
        f"ALTER TABLE {PROBE} ENABLE ROW LEVEL SECURITY",
        _TENANT_POLICY,
    ],
    "no policy": _SECURED,
    "using true": [*_SECURED, f"CREATE POLICY read_all ON {PROBE} FOR SELECT USING (true)"],
    "using true next to tenant": [
        *_SECURED,
        _TENANT_POLICY,
        f"CREATE POLICY read_all ON {PROBE} FOR SELECT USING (true)",
    ],
    "all without with check": [*_SECURED, f"CREATE POLICY t ON {PROBE} USING ({TENANT})"],
    "insert without with check": [
        *_SECURED,
        _TENANT_POLICY,
        f"CREATE POLICY ins ON {PROBE} FOR INSERT",
    ],
    # recognised forms on a table whose RLS is not forced: the only problem is the forcing
    **{
        f"write form {i}": [
            f"CREATE TABLE {PROBE} (id int PRIMARY KEY, tenant_id uuid, user_id uuid, ref uuid)",
            f"ALTER TABLE {PROBE} ENABLE ROW LEVEL SECURITY",
            f"CREATE POLICY p ON {PROBE} USING ({expr}) WITH CHECK ({expr})",
        ]
        for i, expr in enumerate(rls_rules.WRITE_FORMS)
    },
    **{
        f"exists bypass {i}": [
            *_SECURED,
            *_OTHER,
            f"CREATE POLICY p ON {PROBE} FOR SELECT USING ({using})",
        ]
        for i, using in enumerate(rls_rules.EXISTS_BYPASSES)
    },
    **{
        f"null tenant by a client {i}": [
            *_SECURED,
            _TENANT_POLICY,
            f"CREATE POLICY p ON {PROBE} FOR INSERT WITH CHECK ({check})",
        ]
        for i, check in enumerate(rls_rules.NULL_TENANT_BY_A_CLIENT)
    },
    "policy writes the context": [
        *_SECURED,
        _TENANT_POLICY,
        f"CREATE POLICY p ON {PROBE} FOR SELECT USING ({TENANT} AND {rls_rules.TENANT_B} IS NOT NULL)",
    ],
    "view without security_invoker": ["CREATE VIEW shifde.zz_rls_view AS SELECT 1 AS x"],
    "rewrite rule": [
        *_SECURED,
        _TENANT_POLICY,
        *_OTHER,
        f"CREATE RULE leak AS ON INSERT TO {PROBE} DO ALSO INSERT INTO shifde.zz_rls_other"
        " SELECT id, NEW.tenant_id, user_id, ref FROM shifde.zz_rls_other",
    ],
    "truncate grant": [f"GRANT TRUNCATE ON shifde.runs TO {fn_rules.APP_ROLE}"],
    "unlisted definer": [_DEFINER.format(extra=_PINNED), _REVOKE],
    "definer without search_path": [_DEFINER.format(extra=""), _REVOKE],
    "definer executable by public": [_DEFINER.format(extra=_PINNED)],
    "overload of an allowlisted definer": [
        "CREATE FUNCTION shifde.rate_card_versions_guard(x int) RETURNS int LANGUAGE sql"
        " SECURITY DEFINER SET search_path = shifde, pg_catalog, pg_temp AS $$ SELECT 1 $$",
        "REVOKE EXECUTE ON FUNCTION shifde.rate_card_versions_guard(int) FROM PUBLIC",
    ],
}


def _both(c: Connection) -> list[tuple[list[str], list[str]]]:
    """(ported, test) results of the four rule functions on `c`."""
    return [
        (
            catalog_rules.rls_violations(c),
            rls_rules.rls_violations(c, rls_rules.global_tables()),
        ),
        (
            catalog_rules.privilege_problems(c, fn_rules.APP_ROLE),
            rls_rules.privilege_problems(c),
        ),
        (
            catalog_rules.definer_violations(c, fn_rules.OWNER_ROLE, fn_rules.APP_ROLE),
            fn_rules.definer_violations(c, fn_rules.SECURITY_DEFINER_ALLOWLIST),
        ),
        (
            catalog_rules.context_write_violations(c),
            scan_rules.context_write_violations(c),
        ),
    ]


@pytest.fixture
def scratch(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is always rolled back."""
    with owner_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


def test_the_rule_data_matches_the_schema_tests() -> None:
    assert catalog_rules.KEY_COLUMNS == rls_rules.KEY_COLUMNS
    assert catalog_rules.REVIEWED_EXPRESSIONS == rls_rules.REVIEWED_EXPRESSIONS
    assert catalog_rules.EXEMPT == rls_rules.EXEMPT
    assert catalog_rules.FORBIDDEN_PRIVILEGES == rls_rules._FORBIDDEN_PRIVILEGES
    assert catalog_rules.CONTEXT_HELPERS == scan_rules.CONTEXT_HELPERS
    assert catalog_rules.CONTEXT_WRITE_ALLOWLIST == scan_rules.CONTEXT_WRITE_ALLOWLIST
    assert set(rls_rules.PRINCIPAL_TABLES) == catalog_rules.PRINCIPAL_TABLES
    assert set(catalog_rules.GLOBAL_TABLES) == rls_rules.global_tables()
    assert all(reason.strip() for reason in catalog_rules.GLOBAL_TABLES.values())
    assert set(fn_rules.SECURITY_DEFINER_ALLOWLIST) == catalog_rules.SECURITY_DEFINER_ALLOWLIST


def _patterns(patterns: tuple[tuple[re.Pattern[str], str], ...]) -> list[tuple[str, int, str]]:
    return [(pattern.pattern, pattern.flags, why) for pattern, why in patterns]


def test_the_context_scan_data_matches_the_schema_tests() -> None:
    assert catalog_rules.ALL_KINDS == scan_rules.ALL_KINDS
    assert catalog_rules._SCANNED_LANGUAGES == scan_rules._SCANNED_LANGUAGES
    assert catalog_rules._EXPRESSIONS.keys() == scan_rules._EXPRESSIONS.keys()
    assert _patterns(catalog_rules._PATTERNS) == _patterns(scan_rules._PATTERNS)
    assert _patterns(catalog_rules._BODY_PATTERNS) == _patterns(scan_rules._BODY_PATTERNS)


def test_the_migrated_database_gives_the_same_empty_results(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        results = _both(c)
    for ported, expected in results:
        assert ported == expected == []


@pytest.mark.parametrize("statements", list(PROBES.values()), ids=list(PROBES))
def test_each_probe_gives_the_same_non_empty_results(
    scratch: Connection, statements: list[str]
) -> None:
    for statement in statements:
        scratch.execute(text(statement))
    results = _both(scratch)
    for ported, expected in results:
        assert ported == expected
    assert any(ported for ported, _ in results)


@pytest.mark.parametrize("expr", rls_rules.WRITE_FORMS)
def test_a_recognised_write_form_is_not_reported(scratch: Connection, expr: str) -> None:
    for statement in PROBES[f"write form {rls_rules.WRITE_FORMS.index(expr)}"]:
        scratch.execute(text(statement))
    assert catalog_rules.rls_violations(scratch) == [
        "shifde.zz_rls_probe: row level security not forced"
    ]


@pytest.mark.parametrize(("ddl", "labels"), fn_rules.CONTEXT_WRITERS)
def test_each_context_writer_gives_the_same_non_empty_scan(
    scratch: Connection, ddl: list[str], labels: set[str]
) -> None:
    for statement in ddl:
        scratch.execute(text(statement))
    ported = catalog_rules.context_write_violations(scratch)
    assert ported == scan_rules.context_write_violations(scratch)
    assert ported


def test_the_allowlist_argument_gives_the_same_scan(scratch: Connection) -> None:
    ddl, labels = fn_rules.CONTEXT_WRITERS[0]
    for statement in ddl:
        scratch.execute(text(statement))
    for allowlist in ({}, dict.fromkeys(labels, "probe")):
        ported = catalog_rules.context_write_violations(scratch, allowlist=allowlist)
        assert ported == scan_rules.context_write_violations(scratch, allowlist=allowlist)
    assert catalog_rules.context_write_violations(scratch, allowlist={}) != []


@pytest.mark.parametrize("body", [False, True])
@pytest.mark.parametrize(
    "config", [["app.platform_admin=on"], ["search_path=pg_catalog", "APP.Tenant_Id=x"]]
)
def test_a_set_clause_gives_the_same_reasons(config: list[str], body: bool) -> None:
    source = " ".join(config)
    ported = catalog_rules.context_reasons(source, body=body)
    assert ported == scan_rules.context_reasons(source, body=body)
    assert ported
