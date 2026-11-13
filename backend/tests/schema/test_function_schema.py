"""Functions in schema `shifde` cannot be used to step around row-level security
(isolation review 1, items 7 and 8).

* The five baseline helpers (`base_0001`, DECISIONS B-104) are pinned: owner `shifde_owner`, not
  SECURITY DEFINER, no `SET` clause (so the SQL helpers stay inlinable), the expected language,
  volatility and body. Their bodies call builtins schema-qualified (`pg_catalog.current_setting`,
  `::pg_catalog.uuid`, `OPERATOR(pg_catalog.=)`, `pg_catalog.now()`), so nothing placed earlier
  in a caller's `search_path` can stand in for them.
* A SECURITY DEFINER function runs as its owner; owned by `shifde_owner` (BYPASSRLS) it ignores
  every policy. Each one must be listed in `SECURITY_DEFINER_ALLOWLIST` with a reason (adding one
  needs isolation review), pin `search_path` (without `public` or `"$user"`), and grant EXECUTE to
  nobody but its owner and `shifde_app` -- in particular not to PUBLIC.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

APP_ROLE = "shifde_app"

# name -> why it must run with its owner's rights. Adding an entry needs isolation review.
SECURITY_DEFINER_ALLOWLIST: dict[str, str] = {}

_CONTEXT = {"provolatile": "s", "lang": "sql", "rettype": None}
BASE_HELPERS: dict[str, dict[str, str | None]] = {
    "current_tenant_id": {
        **_CONTEXT,
        "rettype": "uuid",
        "body": "SELECT NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')"
        " ::pg_catalog.uuid",
    },
    "current_principal_id": {
        **_CONTEXT,
        "rettype": "uuid",
        "body": "SELECT NULLIF(pg_catalog.current_setting('app.principal_id', true), '')"
        " ::pg_catalog.uuid",
    },
    "is_platform_admin_ctx": {
        **_CONTEXT,
        "rettype": "boolean",
        "body": "SELECT COALESCE(pg_catalog.current_setting('app.platform_admin', true), '')"
        " OPERATOR(pg_catalog.=) 'on'",
    },
    "set_updated_at": {
        "provolatile": "v",
        "lang": "plpgsql",
        "rettype": "trigger",
        "body": "BEGIN NEW.updated_at = pg_catalog.now(); RETURN NEW; END",
    },
    "reject_mutation": {
        "provolatile": "v",
        "lang": "plpgsql",
        "rettype": "trigger",
        "body": "BEGIN RAISE EXCEPTION 'append-only table: UPDATE and DELETE are not permitted'"
        " USING ERRCODE = 'insufficient_privilege'; END",
    },
}

_FUNCTIONS = text(
    """SELECT p.oid, p.proname, p.prosecdef, p.provolatile, l.lanname AS lang,
              pg_catalog.format_type(p.prorettype, NULL) AS rettype,
              pg_catalog.pg_get_userbyid(p.proowner) AS owner, p.proconfig, p.prosrc,
              p.proacl IS NULL AS default_acl,
              ARRAY(SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                ELSE pg_catalog.pg_get_userbyid(a.grantee) END
                      FROM pg_catalog.aclexplode(p.proacl) a
                     WHERE a.privilege_type = 'EXECUTE'
                     ORDER BY 1) AS executors
         FROM pg_catalog.pg_proc p
         JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         JOIN pg_catalog.pg_language l ON l.oid = p.prolang
        WHERE n.nspname = 'shifde'
        ORDER BY p.proname, p.oid"""
)


def _squash(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()


def _search_path(config: list[str] | None) -> str | None:
    for entry in config or []:
        name, _, value = entry.partition("=")
        if name.lower() == "search_path":
            return value
    return None


def definer_violations(conn: Connection, allowlist: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for row in conn.execute(_FUNCTIONS):
        if not row.prosecdef:
            continue
        where = f"shifde.{row.proname}"
        if row.proname not in allowlist:
            problems.append(f"{where}: SECURITY DEFINER but not on the allowlist")
        path = _search_path(row.proconfig)
        if path is None:
            problems.append(f"{where}: search_path not pinned")
        elif re.search(r"\bpublic\b|\$user", path):
            problems.append(f"{where}: search_path {path} includes public or $user")
        executors = ["PUBLIC"] if row.default_acl else list(row.executors)
        extra = [e for e in executors if e not in (row.owner, APP_ROLE)]
        if extra:
            problems.append(f"{where}: EXECUTE granted to {', '.join(extra)}")
    return problems


def helper_violations(conn: Connection) -> list[str]:
    problems: list[str] = []
    rows = {row.proname: row for row in conn.execute(_FUNCTIONS)}
    for name, want in BASE_HELPERS.items():
        row = rows.get(name)
        where = f"shifde.{name}"
        if row is None:
            problems.append(f"{where}: missing")
            continue
        got = {
            "owner": row.owner,
            "security definer": row.prosecdef,
            "SET clause": row.proconfig,
            "volatility": row.provolatile,
            "language": row.lang,
            "returns": row.rettype,
            "body": _squash(row.prosrc),
        }
        expected = {
            "owner": "shifde_owner",
            "security definer": False,
            "SET clause": None,
            "volatility": want["provolatile"],
            "language": want["lang"],
            "returns": want["rettype"],
            "body": want["body"],
        }
        problems += [
            f"{where}: {key} is {got[key]!r}, expected {expected[key]!r}"
            for key in expected
            if got[key] != expected[key]
        ]
    return problems


def test_base_helpers_are_pinned(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        problems = helper_violations(c)
    assert not problems, "\n".join(problems)


def test_security_definer_functions_are_allowlisted_and_locked_down(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        problems = definer_violations(c, SECURITY_DEFINER_ALLOWLIST)
    assert not problems, "\n".join(problems)


def test_allowlist_entries_have_reasons() -> None:
    assert all(reason.strip() for reason in SECURITY_DEFINER_ALLOWLIST.values())


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


def _definer(c: Connection, extra: str = "") -> None:
    c.execute(
        text(
            "CREATE FUNCTION shifde.zz_definer() RETURNS int LANGUAGE sql SECURITY DEFINER"
            f" {extra} AS $$ SELECT 1 $$"
        )
    )


def test_unlisted_security_definer_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {}) == [
        "shifde.zz_definer: SECURITY DEFINER but not on the allowlist"
    ]


def test_security_definer_without_pinned_search_path_is_reported(scratch: Connection) -> None:
    _definer(scratch)
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {"zz_definer": "probe"}) == [
        "shifde.zz_definer: search_path not pinned"
    ]


def test_security_definer_with_public_in_search_path_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = public, pg_catalog")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {"zz_definer": "probe"}) == [
        "shifde.zz_definer: search_path public, pg_catalog includes public or $user"
    ]


def test_security_definer_executable_by_public_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog")  # default ACL: EXECUTE to PUBLIC
    assert definer_violations(scratch, {"zz_definer": "probe"}) == [
        "shifde.zz_definer: EXECUTE granted to PUBLIC"
    ]


def test_locked_down_security_definer_passes(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog, pg_temp")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    scratch.execute(text(f"GRANT EXECUTE ON FUNCTION shifde.zz_definer() TO {APP_ROLE}"))
    assert definer_violations(scratch, {"zz_definer": "probe"}) == []


def test_baseline_helper_turned_definer_is_reported(scratch: Connection) -> None:
    scratch.execute(text("ALTER FUNCTION shifde.current_tenant_id() SECURITY DEFINER"))
    problems = helper_violations(scratch)
    assert "shifde.current_tenant_id: security definer is True, expected False" in problems
    assert "shifde.current_tenant_id: SECURITY DEFINER but not on the allowlist" in (
        definer_violations(scratch, {})
    )


def test_unqualified_helper_body_is_reported(scratch: Connection) -> None:
    scratch.execute(
        text(
            "CREATE OR REPLACE FUNCTION shifde.current_tenant_id() RETURNS uuid LANGUAGE sql"
            " STABLE AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$"
        )
    )
    (problem,) = helper_violations(scratch)
    assert problem.startswith("shifde.current_tenant_id: body is ")
