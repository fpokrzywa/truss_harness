"""Functions cannot be used to step around row-level security (isolation review 1, items 7 and
8; review 2, items 4 and 6; review 3, items 1 and 5).

* The five baseline helpers (`base_0001`, DECISIONS B-104) are pinned, keyed by oid: each is the
  one zero-argument function of its name in `shifde`, and no other function in `shifde` shares
  the name (an overload with the pinned body could otherwise stand in for a replaced helper in
  the check). Each is owned by `shifde_owner`, not
  SECURITY DEFINER, no `SET` clause (so the SQL helpers stay inlinable), the expected language,
  volatility and body. Their bodies call builtins schema-qualified (`pg_catalog.current_setting`,
  `::pg_catalog.uuid`, `OPERATOR(pg_catalog.=)`, `pg_catalog.now()`) and use no construct that
  resolves an operator through the search path (`NULLIF` does: hence `CASE`), so nothing placed
  earlier in a caller's `search_path` can stand in for them.
* A SECURITY DEFINER function runs as its owner; owned by `shifde_owner` (BYPASSRLS) it ignores
  every policy. Each one, in any non-system schema (`public` included), must be listed in
  `SECURITY_DEFINER_ALLOWLIST` with a reason (adding one needs isolation review), pin
  `search_path` without `public` or `"$user"` and with `pg_temp` listed last (left out,
  `pg_temp` is searched first), and grant EXECUTE to nobody but its owner and `shifde_app`
  -- in particular not to PUBLIC. An entry is keyed by its full signature: `<schema>.<name>`
  names the one zero-argument function of that name, `<schema>.<name>(<argument types>)` (types
  as `pg_catalog.oidvectortypes` prints them) exactly that function. Any other function sharing
  an allowlisted name, definer or not, is refused, so an overload cannot inherit an entry
  (review 1, item 10; B-610), and an entry that names no SECURITY DEFINER function is stale.
* Nothing in the catalog writes the client context (`app.tenant_id`, `app.principal_id`,
  `app.platform_admin`): function bodies, SET clauses and argument defaults, views, policies,
  defaults, constraints, indexes, triggers and statistics in every non-system schema are scanned
  by `tests/schema/catalog_scan.py` (rules in its docstring); only the three context helpers are
  exempt, by oid.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine

from tests.schema.catalog_scan import (
    CONTEXT_WRITE_ALLOWLIST,
    context_reasons,
    context_write_violations,
)

APP_ROLE = "shifde_app"
OWNER_ROLE = "shifde_owner"

# <schema>.<name> (the zero-argument function) or <schema>.<name>(<argument types>) -> why it must
# run with its owner's rights. Adding one needs isolation review.
_IDENTITY_DEFINER = (
    "identity_0002 (B-909): owned by shifde_owner, search_path pg_catalog, pg_temp, every object "
    "schema-qualified, EXECUTE for shifde_app only, writes no app.* setting"
)
_OIDC_DEFINER = (
    "identity_0004 (B-922..B-924): owned by shifde_owner, search_path pg_catalog, pg_temp, every "
    "object schema-qualified, EXECUTE for shifde_app only, writes no app.* setting"
)
_GROUP_DEFINER = (
    "identity_0005 (B-928..B-932): owned by shifde_owner, search_path pg_catalog, pg_temp, every "
    "object schema-qualified, EXECUTE for shifde_app only, writes no app.* setting"
)
SECURITY_DEFINER_ALLOWLIST: dict[str, str] = {
    "shifde.shifde_resolve_local_login(text)": (
        f"{_IDENTITY_DEFINER}. Pre-tenant login lookup: users and local_credentials are not "
        "readable before the principal is known. Returns the one account of the normalised e-mail "
        "(hash, counters, status); the caller verifies the password."
    ),
    "shifde.shifde_resolve_session(bytea)": (
        f"{_IDENTITY_DEFINER}. Pre-tenant session lookup by cookie hash (sessions are own_row). "
        "Never reports platform administrator on a session pinned to a client, and returns no "
        "row for a pinned session without an active membership in that client (review 1 item 4)."
    ),
    "shifde.shifde_revoke_session(bytea)": (
        f"{_IDENTITY_DEFINER}. Login rotation revokes the presented session by cookie hash "
        "before the principal is set; it only sets revoked_at (never un-revokes)."
    ),
    "shifde.shifde_revoke_user_sessions(uuid)": (
        f"{_IDENTITY_DEFINER}. Suspending a member revokes that person's sessions (own_row "
        "rows of another person); refused unless the person is a member of the caller's current "
        "client, and touches only unpinned sessions and sessions pinned to that client."
    ),
    "shifde.shifde_create_invite_token(uuid, bytea, timestamp with time zone)": (
        f"{_IDENTITY_DEFINER}. The only app path that writes setup_tokens (no app grants). "
        "Trusts no membership row (review 1 item 1): platform administrator context and principal "
        "required, target an active non-administrator with no credential and no identity, "
        "expiry within 73 hours; one generic error."
    ),
    "shifde.shifde_redeem_setup_token(bytea, text, integer)": (
        f"{_IDENTITY_DEFINER}. Atomic single-use redemption of a setup token by its hash "
        "(setup_tokens is closed to the app); an invitation is refused for a person who already "
        "has a credential or identity. Writes only the token's own person's credential."
    ),
    "shifde.shifde_my_membership_tenants": (
        f"{_IDENTITY_DEFINER}. No arguments: the active membership tenant ids of "
        "current_principal_id() only (memberships are tenant-keyed, so a person cannot list "
        "their own clients before a client is chosen); used for the login audit fan-out."
    ),
    "shifde.shifde_resolve_api_key(text)": (
        "identity_0003 (B-915): owned by shifde_owner, search_path pg_catalog, pg_temp, every "
        "object schema-qualified, EXECUTE for shifde_app only, writes no app.* setting. "
        "Pre-tenant bearer lookup by the key's public prefix (api_keys is tenant-keyed). The "
        "client comes from the key row only, and a row is returned only when the key's principal "
        "belongs to that client (a service account of the client or a person with a membership "
        "there; review 1 item 4). The caller verifies the secret against key_hash."
    ),
    "shifde.shifde_resolve_idp(uuid)": (
        f"{_OIDC_DEFINER}. Pre-tenant OIDC: the sign-in start and callback resolve an active "
        "IdP by its id before any client context exists (no secret values). A client's IdP is "
        "returned only with jit off and a client secret of that same client, an installation IdP "
        "only with an installation secret (review 1 items 4, 7); the caller pins the session to "
        "the IdP's client and requires an active membership there."
    ),
    "shifde.shifde_resolve_identity(text, text, text, uuid)": (
        f"{_OIDC_DEFINER}. Maps a validated (IdP, issuer, subject) of an active IdP to its "
        "person (user_identities is own_row) and performs the setup-token link: only through an "
        "installation IdP or the token's own client's IdP, and for a client's IdP only an active "
        "member of that client who is not the platform administrator. One of the two paths that "
        "insert identities (review 1 item 2)."
    ),
    "shifde.shifde_jit_create_user(uuid, uuid, text, text, text, text)": (
        f"{_OIDC_DEFINER}. Just-in-time provisioning: only through an active installation IdP "
        "with jit on, for a subject not bound yet; creates the person and the identity together "
        "(the other path that inserts identities, review 1 item 2); refuses an existing e-mail."
    ),
    "shifde.shifde_begin_oidc_request(uuid, uuid, bytea, bytea, bytea, text, integer)": (
        f"{_OIDC_DEFINER}. Writes the hashed single-use flow state of an active IdP with the "
        "IdP's own client (oidc_auth_requests is closed to the app), so no client context is set "
        "from the unauthenticated request (review 3 item 5); bounded expiry, 32-byte hashes."
    ),
    "shifde.shifde_my_memberships": (
        "tenancy_0001 (B-1006): owned by shifde_owner, search_path pg_catalog, pg_temp, every "
        "object schema-qualified, EXECUTE for shifde_app only, writes no app.* setting. No "
        "arguments: the clients of current_principal_id() only (active membership and at least "
        "one live role binding there, direct or through an active platform group), with slug, "
        "name, status and the role names of that client's bindings of the person; memberships "
        "and tenants are tenant-keyed, so the client chooser needs it before a client is set."
    ),
    "shifde.shifde_may_bind_group(uuid, uuid)": (
        "tenancy_0002 (B-1010), tenancy_0003 (B-1015: false, never NULL, through "
        "shifde_is_client_owner): owned by shifde_owner, search_path pg_catalog, pg_temp, every "
        "object schema-qualified, EXECUTE for shifde_app only (check_role_binding runs with "
        "invoker rights), writes no app.* setting. True only when p_tenant is current_tenant_id(), "
        "p_group is an active platform group and current_principal_id() is that client's owner "
        "(active person, active membership, live direct tenant_administrator binding at "
        "organization scope); false alike otherwise, so it answers nothing about another client "
        "and nothing about a group to anyone but an owner."
    ),
    "shifde.shifde_is_client_owner(uuid)": (
        "tenancy_0003 (B-1015, B-1016): owned by shifde_owner, search_path pg_catalog, pg_temp, "
        "every object schema-qualified, EXECUTE for shifde_app only (check_role_binding runs "
        "with invoker rights and asks it before a person is bound to tenant_administrator at "
        "organization scope), writes no app.* setting. True only when p_tenant is "
        "current_tenant_id() and current_principal_id() is that client's owner (active person, "
        "active membership, live direct tenant_administrator binding at organization scope); "
        "false, never NULL, otherwise, so it answers nothing about another client."
    ),
    "shifde.shifde_bound_group_admin_ids(uuid)": (
        "tenancy_0004 (B-1407, admin isolation review 1 finding 1): owned by shifde_owner, "
        "search_path pg_catalog, pg_temp, every object schema-qualified, EXECUTE for shifde_app "
        "only, writes no app.* setting. Ids only (no e-mail, name or other member) of the "
        "platform administrators in p_group, and only when p_group holds a role binding in "
        "current_tenant_id(); empty without a client and for a group not bound there. Tenancy "
        "records each as a self-add in that client when a group binding takes effect or an "
        "administrator joins a bound group, which is exactly what that client's owner is shown "
        "(B-1012), so it answers nothing about another client."
    ),
    "shifde.shifde_bound_groups": (
        f"{_GROUP_DEFINER}. No arguments: id and name only of the platform groups holding a role "
        "binding in current_tenant_id() (groups are platform-level and a client context reads "
        "no group row); no members, slug, description or IdP mapping; empty without a client."
    ),
    "shifde.shifde_active_group_ids(uuid)": (
        f"{_GROUP_DEFINER}. The authz.GroupMembership the PDP uses: the person's active platform "
        "groups that hold a role binding in current_tenant_id() only (empty without a client), "
        "so a client learns no more than which of the groups it bound a person is in; nothing "
        "for a person whose membership there is not active (tenancy_0003, B-1018)."
    ),
    "shifde.shifde_sync_idp_groups(uuid, uuid, text[])": (
        f"{_GROUP_DEFINER}. IdP group sync after a validated sign-in: only for an active "
        "installation IdP with sync on and a person with an identity at that IdP, at most 200 "
        "names; touches only that person's idp_sync memberships of groups the platform "
        "administrator mapped to that IdP; never creates a group."
    ),
    "shifde.shifde_consume_oidc_request(bytea, uuid)": (
        f"{_OIDC_DEFINER}. Marks a live flow request used (single use, expiry checked in SQL) "
        "for the IdP named in the callback, only while that IdP is active and still in the "
        "request's client; returns only the request's own hashes and next path."
    ),
    "shifde.rate_card_versions_guard": (
        "ledger (B-601): the BEFORE INSERT guard of rate_card_versions closes the open-ended "
        "predecessor of a new version (UPDATE effective_to), which the application role may not "
        "do (INSERT/SELECT grants only; the immutability trigger refuses every other update). It "
        "runs as the owner, and because it fires before the INSERT's WITH CHECK its first statement "
        "refuses (42501) any NEW.tenant_id other than the caller's, and a global version unless "
        "the caller is the platform administrator, before any lock or lookup. It then reads and "
        "updates only rows of the NEW row's own rate_card_id after checking that the card "
        "belongs to NEW.tenant_id (review 1, item 1). It pins search_path "
        "(shifde, pg_catalog, pg_temp), and is executable by the owner only."
    ),
}

_CONTEXT = {"provolatile": "s", "lang": "sql", "rettype": None}
BASE_HELPERS: dict[str, dict[str, str | None]] = {
    "current_tenant_id": {
        **_CONTEXT,
        "rettype": "uuid",
        "body": "SELECT CASE WHEN pg_catalog.current_setting('app.tenant_id', true)"
        " OPERATOR(pg_catalog.=) '' THEN NULL"
        " ELSE pg_catalog.current_setting('app.tenant_id', true)::pg_catalog.uuid END",
    },
    "current_principal_id": {
        **_CONTEXT,
        "rettype": "uuid",
        "body": "SELECT CASE WHEN pg_catalog.current_setting('app.principal_id', true)"
        " OPERATOR(pg_catalog.=) '' THEN NULL"
        " ELSE pg_catalog.current_setting('app.principal_id', true)::pg_catalog.uuid END",
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
    r"""SELECT n.nspname, p.oid, p.proname, p.prosecdef, p.provolatile, l.lanname AS lang,
              pg_catalog.pg_get_function_identity_arguments(p.oid) AS args,
              pg_catalog.oidvectortypes(p.proargtypes) AS argtypes,
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
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
        ORDER BY n.nspname, p.proname, p.oid"""
)


def _squash(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()


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


def _allowlisted_names(allowlist: dict[str, str]) -> set[str]:
    return {key.partition("(")[0] for key in allowlist}


def definer_violations(conn: Connection, allowlist: dict[str, str]) -> list[str]:
    """An allowlist entry names exactly one function by full signature (`<schema>.<name>` is the
    zero-argument one, as the base helpers): any other function sharing an allowlisted name,
    definer or not, is refused, so an overload cannot inherit an entry (review 1, item 10)."""
    problems: list[str] = []
    names = _allowlisted_names(allowlist)
    for row in conn.execute(_FUNCTIONS):
        where = f"{row.nspname}.{row.proname}"
        listed = _signature(where, row.argtypes) in allowlist
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
        if row.owner != OWNER_ROLE:  # review 2 item 6: a definer runs with its owner's rights
            problems.append(f"{where}: owned by {row.owner}, not {OWNER_ROLE}")
        executors = ["PUBLIC"] if row.default_acl else list(row.executors)
        extra = [e for e in executors if e not in (row.owner, APP_ROLE)]
        if extra:
            problems.append(f"{where}: EXECUTE granted to {', '.join(extra)}")
    return problems


def helper_violations(conn: Connection) -> list[str]:
    """Keyed by oid: each helper is the one zero-argument function of its name in `shifde`, and
    no other function shares the name (an overload could stand in for a replaced helper)."""
    problems: list[str] = []
    rows = [row for row in conn.execute(_FUNCTIONS) if row.nspname == "shifde"]
    for name, want in BASE_HELPERS.items():
        named = [row for row in rows if row.proname == name]
        where = f"shifde.{name}"
        if len(named) > 1:
            problems.append(f"{where}: {len(named)} functions with this name (overloaded)")
        row = next((row for row in named if row.args == ""), None)
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


def stale_allowlist_entries(conn: Connection, allowlist: dict[str, str]) -> list[str]:
    """Entries that name no SECURITY DEFINER function in the database under test."""
    found = {
        _signature(f"{row.nspname}.{row.proname}", row.argtypes)
        for row in conn.execute(_FUNCTIONS)
        if row.prosecdef
    }
    return sorted(set(allowlist) - found)


def test_allowlist_has_no_stale_entries(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        assert stale_allowlist_entries(c, SECURITY_DEFINER_ALLOWLIST) == []


def test_identity_definers_are_on_the_allowlist_by_signature(owner_engine: Engine) -> None:
    """identity_0002..0005 (B-909, B-915, B-922, B-928) and tenancy_0001..0004 (B-1006, B-1010,
    B-1016, B-1407): each identity and tenancy definer is listed under its full signature."""
    with owner_engine.connect() as c:
        rows = [
            row
            for row in c.execute(_FUNCTIONS)
            if row.prosecdef and row.proname.startswith("shifde_")
        ]
    keys = {_signature(f"{row.nspname}.{row.proname}", row.argtypes) for row in rows}
    assert keys == {
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
        "shifde.shifde_consume_oidc_request(bytea, uuid)",
        "shifde.shifde_bound_groups",
        "shifde.shifde_active_group_ids(uuid)",
        "shifde.shifde_sync_idp_groups(uuid, uuid, text[])",
        "shifde.shifde_my_memberships",
        "shifde.shifde_may_bind_group(uuid, uuid)",
        "shifde.shifde_is_client_owner(uuid)",
        "shifde.shifde_bound_group_admin_ids(uuid)",
    }
    assert all(row.owner == "shifde_owner" for row in rows)
    assert all(_search_path(row.proconfig) == "pg_catalog, pg_temp" for row in rows)


# --------------------------------------------------------------------------- probes
@pytest.fixture
def scratch(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is always rolled back."""
    with owner_engine.connect() as c:
        tx = c.begin()
        try:
            # The probes assert the exact violation list of one scratch function: the real,
            # allowlisted definers (B-601) are dropped inside this rolled-back transaction.
            for key in SECURITY_DEFINER_ALLOWLIST:
                signature = key if "(" in key else f"{key}()"
                c.execute(text(f"DROP FUNCTION {signature} CASCADE"))
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


@pytest.fixture
def real(owner_engine: Engine) -> Iterator[Connection]:
    """Like `scratch` but leaves the real allowlisted definers in place."""
    with owner_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


@pytest.mark.parametrize("definer", [True, False])
def test_overload_of_an_allowlisted_definer_is_reported(real: Connection, definer: bool) -> None:
    """The allowlist was keyed by name: an overload of `rate_card_versions_guard` with a different
    body (review 1, item 10) inherited its entry. Keyed by the one zero-argument function."""
    assert definer_violations(real, SECURITY_DEFINER_ALLOWLIST) == []
    real.execute(
        text(
            "CREATE FUNCTION shifde.rate_card_versions_guard(x int) RETURNS int LANGUAGE sql"
            f" {'SECURITY DEFINER' if definer else ''} SET search_path = shifde, pg_catalog,"
            " pg_temp AS $$ SELECT 1 $$"
        )
    )
    real.execute(
        text("REVOKE EXECUTE ON FUNCTION shifde.rate_card_versions_guard(int) FROM PUBLIC")
    )
    problems = definer_violations(real, SECURITY_DEFINER_ALLOWLIST)
    assert "shifde.rate_card_versions_guard(x integer): overload of an allowlisted function" in (
        problems
    )
    if definer:
        assert "shifde.rate_card_versions_guard: SECURITY DEFINER but not on the allowlist" in (
            problems
        )


def test_unlisted_security_definer_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog, pg_temp")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {}) == [
        "shifde.zz_definer: SECURITY DEFINER but not on the allowlist"
    ]


def test_security_definer_without_pinned_search_path_is_reported(scratch: Connection) -> None:
    _definer(scratch)
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {"shifde.zz_definer": "probe"}) == [
        "shifde.zz_definer: search_path not pinned"
    ]


def test_security_definer_with_public_in_search_path_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = public, pg_catalog, pg_temp")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    assert definer_violations(scratch, {"shifde.zz_definer": "probe"}) == [
        "shifde.zz_definer: search_path public, pg_catalog, pg_temp includes public or $user"
    ]


def test_security_definer_executable_by_public_is_reported(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog, pg_temp")  # default ACL: PUBLIC
    assert definer_violations(scratch, {"shifde.zz_definer": "probe"}) == [
        "shifde.zz_definer: EXECUTE granted to PUBLIC"
    ]


def test_locked_down_security_definer_passes(scratch: Connection) -> None:
    _definer(scratch, "SET search_path = pg_catalog, pg_temp")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    scratch.execute(text(f"GRANT EXECUTE ON FUNCTION shifde.zz_definer() TO {APP_ROLE}"))
    assert definer_violations(scratch, {"shifde.zz_definer": "probe"}) == []


def test_allowlisted_definer_owned_by_another_role_is_reported(worker_admin_url: str) -> None:
    """Review 2 item 6: every allowlisted definer must be owned by `shifde_owner` (a definer runs
    with its owner's rights). Probed as the cluster administrator in a rolled-back transaction:
    the schema owner cannot give a function away."""
    engine = create_engine(worker_admin_url)
    try:
        with engine.connect() as c:
            tx = c.begin()
            try:
                assert definer_violations(c, SECURITY_DEFINER_ALLOWLIST) == []
                c.execute(
                    text(f"ALTER FUNCTION shifde.shifde_resolve_api_key(text) OWNER TO {APP_ROLE}")
                )
                problems = definer_violations(c, SECURITY_DEFINER_ALLOWLIST)
            finally:
                tx.rollback()
    finally:
        engine.dispose()
    assert problems == [f"shifde.shifde_resolve_api_key: owned by {APP_ROLE}, not {OWNER_ROLE}"]


def _definer_with_args(c: Connection, args: str, *, definer: bool = True) -> None:
    c.execute(
        text(
            f"CREATE FUNCTION shifde.zz_definer({args}) RETURNS int LANGUAGE sql"
            f" {'SECURITY DEFINER' if definer else ''}"
            " SET search_path = pg_catalog, pg_temp AS $$ SELECT 1 $$"
        )
    )
    c.execute(text(f"REVOKE EXECUTE ON FUNCTION shifde.zz_definer({args}) FROM PUBLIC"))


def test_signature_keyed_entry_admits_exactly_that_signature(scratch: Connection) -> None:
    _definer_with_args(scratch, "a uuid, b bytea")
    assert definer_violations(scratch, {"shifde.zz_definer(uuid, bytea)": "probe"}) == []
    # the same name keyed for no arguments, or for other types, does not admit it
    for key in ("shifde.zz_definer", "shifde.zz_definer(bytea, uuid)"):
        assert definer_violations(scratch, {key: "probe"}) == [
            "shifde.zz_definer(a uuid, b bytea): overload of an allowlisted function",
            "shifde.zz_definer: SECURITY DEFINER but not on the allowlist",
        ]


@pytest.mark.parametrize("definer", [True, False])
def test_overload_of_a_signature_keyed_definer_is_reported(
    scratch: Connection, definer: bool
) -> None:
    """B-610 holds for signature-keyed entries: another function of the name is refused."""
    _definer_with_args(scratch, "a uuid")
    _definer_with_args(scratch, "a uuid, b int", definer=definer)
    problems = definer_violations(scratch, {"shifde.zz_definer(uuid)": "probe"})
    assert "shifde.zz_definer(a uuid, b integer): overload of an allowlisted function" in problems
    assert ("shifde.zz_definer: SECURITY DEFINER but not on the allowlist" in problems) is definer
    assert len(problems) == (2 if definer else 1)


def test_signature_keyed_entry_still_needs_the_lockdown(scratch: Connection) -> None:
    scratch.execute(
        text(
            "CREATE FUNCTION shifde.zz_definer(a uuid) RETURNS int LANGUAGE sql SECURITY DEFINER"
            " AS $$ SELECT 1 $$"
        )
    )
    assert definer_violations(scratch, {"shifde.zz_definer(uuid)": "probe"}) == [
        "shifde.zz_definer: search_path not pinned",
        "shifde.zz_definer: EXECUTE granted to PUBLIC",
    ]


def test_stale_allowlist_entry_is_reported(scratch: Connection) -> None:
    _definer_with_args(scratch, "a uuid")
    stale = stale_allowlist_entries(
        scratch, {"shifde.zz_definer(uuid)": "probe", "shifde.zz_definer(text)": "probe"}
    )
    assert stale == ["shifde.zz_definer(text)"]


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


# ---- isolation review 2
def test_security_definer_outside_schema_shifde_is_reported(scratch: Connection) -> None:
    """A definer function in `public` owned by shifde_owner reads every client's rows."""
    scratch.execute(
        text(
            "CREATE FUNCTION public.zz_dump() RETURNS SETOF text LANGUAGE sql SECURITY DEFINER"
            " SET search_path = pg_catalog, pg_temp AS $$ SELECT 'all rows'::text $$"
        )
    )
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION public.zz_dump() FROM PUBLIC"))
    scratch.execute(text(f"GRANT EXECUTE ON FUNCTION public.zz_dump() TO {APP_ROLE}"))
    assert definer_violations(scratch, {}) == [
        "public.zz_dump: SECURITY DEFINER but not on the allowlist"
    ]
    assert definer_violations(scratch, {"public.zz_dump": "probe"}) == []


@pytest.mark.parametrize(
    "path", ["pg_catalog", "pg_temp, pg_catalog", "pg_catalog, pg_temp, shifde"]
)
def test_security_definer_without_pg_temp_last_is_reported(scratch: Connection, path: str) -> None:
    """Unlisted, pg_temp is searched first: a temporary object could stand in for a builtin."""
    _definer(scratch, f"SET search_path = {path}")
    scratch.execute(text("REVOKE EXECUTE ON FUNCTION shifde.zz_definer() FROM PUBLIC"))
    (problem,) = definer_violations(scratch, {"shifde.zz_definer": "probe"})
    assert problem.startswith("shifde.zz_definer: search_path "), problem
    assert problem.endswith("must list pg_temp last"), problem


def test_helpers_ignore_a_hostile_operator_on_the_search_path(scratch: Connection) -> None:
    """The helpers resolve no operator through the caller's search_path (review 2, item 6)."""
    scratch.execute(
        text(
            "CREATE FUNCTION public.zz_hostile_eq(text, text) RETURNS boolean LANGUAGE plpgsql"
            " AS $$ BEGIN RAISE EXCEPTION 'hostile operator used'; END $$"
        )
    )
    scratch.execute(
        text(
            "CREATE OPERATOR public.= (LEFTARG = text, RIGHTARG = text, FUNCTION = public.zz_hostile_eq)"
        )
    )
    scratch.execute(text("SET LOCAL search_path = public, pg_catalog"))
    scratch.execute(
        text(
            "SELECT set_config('app.tenant_id', '01900000-0000-7000-8000-0000000000aa', true),"
            " set_config('app.principal_id', '', true),"
            " set_config('app.platform_admin', 'on', true)"
        )
    )
    row = scratch.execute(
        text(
            "SELECT shifde.current_tenant_id()::text, shifde.current_principal_id(),"
            " shifde.is_platform_admin_ctx()"
        )
    ).one()
    assert tuple(row) == ("01900000-0000-7000-8000-0000000000aa", None, True)


# ---- isolation review 3
ADMIN_ON = "pg_catalog.set_config('app.platform_admin', 'on', true)"
TENANT_B = "pg_catalog.set_config('app.tenant_id', '01900000-0000-7000-8000-0000000000bb', true)"
_T = "CREATE TABLE shifde.zz_ctx (id int, c text)"

CONTEXT_WRITERS = [
    # review 3, item 1: a plain plpgsql function in public
    (
        [
            "CREATE FUNCTION public.zz_peek2() RETURNS void LANGUAGE plpgsql"
            f" AS $$ BEGIN PERFORM {ADMIN_ON}; END $$"
        ],
        {"function public.zz_peek2()"},
    ),
    # review 3, item 1: a SQL function in shifde
    (
        [f"CREATE FUNCTION shifde.zz_all() RETURNS text LANGUAGE sql AS $$ SELECT {ADMIN_ON} $$"],
        {"function shifde.zz_all()"},
    ),
    # review 3, item 1: a security_invoker view whose FROM sets the flag for the transaction
    (
        [
            "CREATE VIEW shifde.zz_ctx_view WITH (security_invoker) AS"
            f" SELECT s FROM {ADMIN_ON} AS s"
        ],
        {"view shifde.zz_ctx_view"},
    ),
    (
        [f"CREATE MATERIALIZED VIEW shifde.zz_ctx_mv AS SELECT {TENANT_B} AS s"],
        {"view shifde.zz_ctx_mv"},
    ),
    # a SET statement, the name quoted and split by a comment
    (
        [
            "CREATE FUNCTION shifde.zz_set() RETURNS void LANGUAGE sql"
            ' AS $$ SET LOCAL "app" /* x */ . "platform_admin" TO \'on\' $$'
        ],
        {"function shifde.zz_set()"},
    ),
    # dynamic SQL hides every name
    (
        [
            "CREATE FUNCTION shifde.zz_dyn() RETURNS void LANGUAGE plpgsql AS $$ BEGIN"
            " EXECUTE 'SELECT pg_catalog.set_' || 'config(''ap' || 'p.platform_admin'', ''on'', true)';"
            " END $$"
        ],
        {"function shifde.zz_dyn()"},
    ),
    # a unicode-escaped identifier hides the name
    (
        [
            "CREATE FUNCTION shifde.zz_uni() RETURNS text LANGUAGE sql AS $$ SELECT"
            " pg_catalog.U&\"\\0073et_config\"('ap' || 'p.platform_admin', 'on', true) $$"
        ],
        {"function shifde.zz_uni()"},
    ),
    # pg_settings is written through set_config by a rule
    (
        [
            "CREATE FUNCTION shifde.zz_settings() RETURNS void LANGUAGE sql AS $$"
            " UPDATE pg_catalog.pg_settings SET setting = 'on' WHERE name = 'ap' || 'p.platform_admin'"
            " $$"
        ],
        {"function shifde.zz_settings()"},
    ),
    # a builtin that runs a query given as text
    (
        [
            "CREATE VIEW shifde.zz_ctx_view WITH (security_invoker) AS SELECT"
            " pg_catalog.query_to_xml('SELECT ' || 'now()', true, false, '') AS x"
        ],
        {"view shifde.zz_ctx_view"},
    ),
    # a SQL-standard body
    (
        [
            "CREATE FUNCTION shifde.zz_atomic() RETURNS text LANGUAGE sql"
            f" BEGIN ATOMIC SELECT {ADMIN_ON}; END"
        ],
        {"function shifde.zz_atomic()"},
    ),
    # an argument default runs in the caller
    (
        [
            f"CREATE FUNCTION shifde.zz_argdef(x text DEFAULT {ADMIN_ON}) RETURNS text"
            " LANGUAGE sql AS $$ SELECT x $$"
        ],
        {"function shifde.zz_argdef(x text)"},
    ),
    (
        [f"CREATE TABLE shifde.zz_ctx (id int, c text DEFAULT {ADMIN_ON})"],
        {"default shifde.zz_ctx.c"},
    ),
    (
        [
            "CREATE TABLE shifde.zz_ctx (id int, c text,"
            " g text GENERATED ALWAYS AS (c || 'app.tenant_id') STORED)"
        ],
        {"default shifde.zz_ctx.g"},
    ),
    (
        [_T, f"ALTER TABLE shifde.zz_ctx ADD CONSTRAINT zz_ck CHECK ({TENANT_B} IS NOT NULL)"],
        {"constraint shifde.zz_ck on shifde.zz_ctx"},
    ),
    (
        [
            f"CREATE DOMAIN shifde.zz_dom AS text DEFAULT {ADMIN_ON}"
            f" CONSTRAINT zz_dom_ck CHECK ({ADMIN_ON} IS NOT NULL)"
        ],
        {"domain default shifde.zz_dom", "constraint shifde.zz_dom_ck on shifde.zz_dom"},
    ),
    (
        [_T, "CREATE INDEX zz_ctx_ix ON shifde.zz_ctx ((c = 'app.principal_id'))"],
        {"index shifde.zz_ctx_ix"},
    ),
    (
        [_T, "CREATE INDEX zz_ctx_ix ON shifde.zz_ctx (id) WHERE c = 'app.principal_id'"],
        {"index shifde.zz_ctx_ix"},
    ),
    (
        [
            _T,
            "CREATE TRIGGER zz_ctx_tr BEFORE INSERT ON shifde.zz_ctx FOR EACH ROW"
            f" WHEN ({ADMIN_ON} IS NOT NULL) EXECUTE FUNCTION shifde.set_updated_at()",
        ],
        {"trigger zz_ctx_tr on shifde.zz_ctx"},
    ),
    (
        [_T, "CREATE STATISTICS shifde.zz_ctx_st ON (c || 'app.tenant_id'), id FROM shifde.zz_ctx"],
        {"statistics shifde.zz_ctx_st"},
    ),
    # an overload of a pinned helper is not the pinned helper (exemption is by oid)
    (
        [
            "CREATE FUNCTION shifde.current_tenant_id(x int) RETURNS uuid LANGUAGE sql STABLE"
            f" AS $$ {BASE_HELPERS['current_tenant_id']['body']} $$"
        ],
        {"function shifde.current_tenant_id(x integer)"},
    ),
    # review 4, item 2: an aggregate whose transition function is set_config
    (
        [
            "CREATE AGGREGATE shifde.zz_agg(text, boolean) (SFUNC = pg_catalog.set_config,"
            " STYPE = text, INITCOND = 'app.platform_admin')"
        ],
        {"aggregate shifde.zz_agg(text, boolean)"},
    ),
    (
        [
            "CREATE AGGREGATE shifde.zz_agg(text) (SFUNC = pg_catalog.textcat, STYPE = text,"
            " MSFUNC = pg_catalog.textcat, MINVFUNC = pg_catalog.textcat, MSTYPE = text,"
            " MINITCOND = 'app.tenant_id')"
        ],
        {"aggregate shifde.zz_agg(text)"},
    ),
]


def _labels(findings: list[str]) -> set[str]:
    return {finding.split(": ")[0] for finding in findings}


@pytest.mark.parametrize(("ddl", "labels"), CONTEXT_WRITERS)
def test_object_that_writes_the_client_context_is_reported(
    scratch: Connection, ddl: list[str], labels: set[str]
) -> None:
    """Review 3, item 1: code that sets app.* hands a client another client's rows."""
    for statement in ddl:
        scratch.execute(text(statement))
    assert _labels(context_write_violations(scratch)) == labels


def test_code_that_only_reads_the_context_through_the_helpers_passes(scratch: Connection) -> None:
    scratch.execute(text(_T))
    scratch.execute(
        text(
            "CREATE FUNCTION shifde.zz_reader() RETURNS uuid LANGUAGE sql STABLE"
            " AS $$ SELECT shifde.current_tenant_id() $$"
        )
    )
    scratch.execute(
        text(
            "CREATE VIEW shifde.zz_ctx_view WITH (security_invoker) AS"
            " SELECT id FROM shifde.zz_ctx WHERE c = 'settings' AND id > 0"
        )
    )
    scratch.execute(
        text(
            "CREATE FUNCTION shifde.zz_upd() RETURNS void LANGUAGE sql"
            " AS $$ UPDATE shifde.zz_ctx SET c = 'x' $$"
        )
    )
    scratch.execute(
        text(
            "CREATE AGGREGATE shifde.zz_sum(int) (SFUNC = pg_catalog.int4pl, STYPE = int,"
            " INITCOND = '0')"
        )
    )
    assert context_write_violations(scratch) == []


def test_context_writer_allowlist_is_exact(scratch: Connection) -> None:
    scratch.execute(
        text(
            f"CREATE FUNCTION shifde.zz_all() RETURNS text LANGUAGE sql AS $$ SELECT {ADMIN_ON} $$"
        )
    )
    assert context_write_violations(scratch, allowlist={"function shifde.zz_all()": "probe"}) == []
    assert _labels(
        context_write_violations(scratch, allowlist={"function shifde.zz_all": "probe"})
    ) == {"function shifde.zz_all()"}


@pytest.mark.parametrize(
    "config",
    [["app.platform_admin=on"], ["search_path=pg_catalog", "APP.Tenant_Id=x"]],
)
def test_function_set_clause_naming_the_context_is_reported(config: list[str]) -> None:
    """PostgreSQL refuses `SET app.* = ...` on a function to shifde_owner; checked anyway."""
    assert context_reasons(" ".join(config))


def test_nothing_in_the_catalog_writes_the_client_context(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        problems = context_write_violations(c)
    assert not problems, "\n".join(problems)


def test_context_writer_allowlist_has_reasons_and_no_stale_entries(owner_engine: Engine) -> None:
    assert all(reason.strip() for reason in CONTEXT_WRITE_ALLOWLIST.values())
    with owner_engine.connect() as c:
        found = _labels(context_write_violations(c, allowlist={}))
    stale = set(CONTEXT_WRITE_ALLOWLIST) - found
    assert not stale, f"CONTEXT_WRITE_ALLOWLIST entries that match nothing: {stale}"


def test_replaced_helper_hidden_by_an_overload_is_reported(scratch: Connection) -> None:
    """Review 3, item 5: keyed by name, the overload's pinned body hid the replaced helper."""
    scratch.execute(
        text(
            "CREATE OR REPLACE FUNCTION shifde.current_tenant_id() RETURNS uuid LANGUAGE sql STABLE"
            " AS $$ SELECT '01900000-0000-7000-8000-0000000000bb'::pg_catalog.uuid $$"
        )
    )
    scratch.execute(
        text(
            "CREATE FUNCTION shifde.current_tenant_id(x int) RETURNS uuid LANGUAGE sql STABLE"
            f" AS $$ {BASE_HELPERS['current_tenant_id']['body']} $$"
        )
    )
    problems = helper_violations(scratch)
    assert "shifde.current_tenant_id: 2 functions with this name (overloaded)" in problems
    assert any(p.startswith("shifde.current_tenant_id: body is ") for p in problems), problems


def test_overloaded_helper_is_reported(scratch: Connection) -> None:
    scratch.execute(
        text(
            "CREATE FUNCTION shifde.is_platform_admin_ctx(x int) RETURNS boolean LANGUAGE sql"
            " STABLE AS $$ SELECT false $$"
        )
    )
    assert helper_violations(scratch) == [
        "shifde.is_platform_admin_ctx: 2 functions with this name (overloaded)"
    ]
