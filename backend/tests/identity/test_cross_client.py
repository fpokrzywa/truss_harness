"""Cross-client isolation of identity's tables, as `shifde_app` (each session asserts
`current_user`).

Client A cannot read, list or modify client B's memberships, service accounts, API keys or IdP
configurations, through the repository or raw SQL, and cannot create rows under B's id; a
person's own principal rows (credentials, sessions, IdP subjects) are invisible to everyone else;
with no context nothing is visible; constraints (which bypass row-level security) refuse a row
of A naming B's object. Also the authz binding trigger's subject branches that need only
identity's tables (carry_forward: "Authz binding cross-client tests must match the trigger's
specific messages").

Identity part 2a (service, definers; B-909..B-914) adds: no client context inserts a membership
or mints an invitation or setup credential (review 1 item 1); a session pinned to a client the
person has no active membership in does not resolve, and a pinned session is never an
administrator's (item 4); suspension reaches only the caller's client; login counters are the
person's own.

Identity part 2b (B-915..B-920) adds the routes of `identity.api` over the minimal app of
`tests/identity/harness.py`: every route is listed in `ROUTE_TESTS` (a new route without a
cross-client test fails `test_every_identity_route_has_a_cross_client_test`); a person of A sees
and changes only their own session and A's memberships; A's session or A's API key asking for B's
object by id gets the answer of a missing id, and B's objects are absent from A's lists;
`shifde_resolve_api_key` resolves a key only with a principal of the key's own client. OIDC routes
arrive with part 3, member and IdP administration with tenancy/admin (docs/build/carry_forward.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any  # Any: JSON response bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import Forbidden
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import api as identity_api
from shifde.identity import apikeys, repository, service
from tests.identity.conftest import World, app_session, owner_membership, owner_user
from tests.identity.harness import PASSWORD, client_for, login, person

ROW_SECURITY = "row-level security|row level security"


@pytest.fixture
def a_session(app_db: Database, world: World) -> Iterator[Session]:
    s = app_session(app_db, world.a.tenant, world.a.member)
    yield s
    s.close()


def _ids(s: Session, table: str, column: str = "id") -> set[UUID]:
    return {r[0] for r in s.execute(text(f"SELECT {column} FROM shifde.{table}"))}


def _refused(s: Session, sql: str, params: dict[str, object], match: str = ROW_SECURITY) -> None:
    with pytest.raises(DBAPIError, match=match):
        s.execute(text(sql), params)
    s.rollback()


# ------------------------------------------------------------------ memberships
def test_a_cannot_read_list_or_modify_b_memberships(world: World, a_session: Session) -> None:
    s, a, b = a_session, world.a, world.b
    assert b.membership not in _ids(s, "memberships")
    assert _ids(s, "memberships", "tenant_id") == {a.tenant}
    assert repository.get_membership(s, b.tenant, b.member) is None
    assert repository.get_membership(s, b.tenant, world.both) is None
    assert repository.list_members_admin(s, tenant_id=b.tenant, after=None, limit=50) == []
    assert repository.list_members(s, tenant_id=b.tenant, after=None, limit=50) == []
    assert repository.count_members(s, b.tenant) == 0
    assert not repository.is_member(s, b.tenant, b.member)
    assert repository.update_membership_status(s, b.tenant, b.member, 1, "suspended") is None
    raw = s.execute(
        text("UPDATE shifde.memberships SET status = 'suspended' WHERE id = :id"),
        {"id": b.membership},
    )
    assert raw.rowcount == 0  # type: ignore[attr-defined]  # CursorResult on DML
    s.rollback()
    _refused(
        s,
        "INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:id, :t, :u)",
        {"id": uuid7(), "t": b.tenant, "u": a.member},
    )


def test_b_membership_is_unchanged(
    world: World, a_session: Session, owner_session: Session
) -> None:
    repository.update_membership_status(a_session, world.b.tenant, world.b.member, 1, "suspended")
    a_session.commit()
    status = owner_session.execute(
        text("SELECT status FROM shifde.memberships WHERE id = :id"), {"id": world.b.membership}
    ).scalar_one()
    assert status == "active"


# ------------------------------------------------------------------ service accounts
def test_a_cannot_read_list_or_modify_b_service_accounts(
    world: World, a_session: Session, owner_session: Session
) -> None:
    s, a, b = a_session, world.a, world.b
    assert _ids(s, "service_accounts") == {a.service_account}
    assert repository.get_service_account(s, b.tenant, b.service_account) is None
    assert repository.get_service_account(s, a.tenant, b.service_account) is None
    assert repository.list_service_accounts(s, tenant_id=b.tenant, after=None, limit=50) == []
    updated = repository.update_service_account(
        s, b.tenant, b.service_account, None, {"status": "disabled"}
    )
    assert updated is None
    s.commit()
    _refused(
        s,
        "INSERT INTO shifde.service_accounts (id, tenant_id, name) VALUES (:id, :t, 'x')",
        {"id": uuid7(), "t": b.tenant},
    )
    assert (
        owner_session.execute(
            text("SELECT status FROM shifde.service_accounts WHERE id = :id"),
            {"id": b.service_account},
        ).scalar_one()
        == "active"
    )


# ------------------------------------------------------------------ API keys
def test_a_cannot_read_list_or_modify_b_api_keys(
    world: World, a_session: Session, owner_session: Session
) -> None:
    s, a, b = a_session, world.a, world.b
    assert _ids(s, "api_keys") == {a.api_key, a.user_key}
    for key in (b.api_key, b.user_key):
        assert repository.get_api_key(s, b.tenant, key) is None
        assert repository.get_api_key(s, a.tenant, key) is None
        assert not repository.revoke_api_key(s, key, a.member, utcnow())
        assert not repository.touch_api_key(s, key, utcnow(), 0)
        repository.shorten_api_key_expiry(s, key, utcnow())
    listed = repository.list_api_keys(
        s, tenant_id=b.tenant, principal_type=None, principal_id=None, after=None, limit=50
    )
    assert listed == []
    revoked = repository.revoke_keys_of_principal(
        s, b.tenant, "service_account", b.service_account, a.member, utcnow()
    )
    assert revoked == 0
    s.commit()
    rows = owner_session.execute(
        text(
            "SELECT revoked_at, last_used_at, expires_at > now() + interval '1 day' "
            "FROM shifde.api_keys WHERE tenant_id = :t"
        ),
        {"t": b.tenant},
    ).all()
    assert [tuple(r) for r in rows] == [(None, None, True), (None, None, True)]
    _refused(
        s,
        "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, prefix, "
        "key_hash, scopes, expires_at) VALUES (:id, :t, 'x', 'service_account', :p, 'zzzzzzzz', "
        "'\\x00', ARRAY['read'], now() + interval '1 day')",
        {"id": uuid7(), "t": b.tenant, "p": b.service_account},
    )


def test_a_key_cannot_name_b_key_as_predecessor_even_as_owner(
    world: World, owner_session: Session
) -> None:
    """Constraints bypass row-level security: the composite key refuses it for the owner too."""
    with pytest.raises(DBAPIError, match="fk_api_keys__rotated_from"):
        owner_session.execute(
            text(
                "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, "
                "prefix, key_hash, scopes, expires_at, rotated_from_id) VALUES (:id, :t, 'x', "
                "'service_account', :p, 'rotated1', '\\x00', ARRAY['read'], "
                "now() + interval '1 day', :r)"
            ),
            {
                "id": uuid7(),
                "t": world.a.tenant,
                "p": world.a.service_account,
                "r": world.b.api_key,
            },
        )
    owner_session.rollback()


# ------------------------------------------------------------------ IdP configurations
def test_a_cannot_read_list_or_modify_b_idp_configs(
    world: World, a_session: Session, owner_session: Session
) -> None:
    s, a, b = a_session, world.a, world.b
    assert _ids(s, "idp_configs") == {a.idp, world.installation_idp}
    assert repository.get_idp(s, b.tenant, b.idp) is None
    assert repository.get_idp(s, a.tenant, b.idp) is None
    assert repository.list_idps(s, tenant_id=b.tenant, after=None, limit=50) == []
    assert [i.id for i in repository.list_public_idps(s)] == [world.installation_idp]
    # neither B's configuration nor the installation one (platform administrator only)
    for idp in (b.idp, world.installation_idp):
        assert repository.update_idp(s, idp, 1, {"display_name": "taken"}) is None
    s.commit()
    names = set(
        owner_session.execute(
            text("SELECT display_name FROM shifde.idp_configs WHERE id IN (:b, :i)"),
            {"b": b.idp, "i": world.installation_idp},
        ).scalars()
    )
    assert "taken" not in names
    for tenant in (b.tenant, None):
        _refused(
            s,
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id) VALUES (:id, :t, 'x', 'https://evil.test', 'https://evil.test', 'c')",
            {"id": uuid7(), "t": tenant},
        )


def test_a_idp_cannot_name_b_secret_even_as_owner(world: World, owner_session: Session) -> None:
    secret = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (:id, :t, 'b-idp-secret', 'env', 'B_IDP_SECRET')"
        ),
        {"id": secret, "t": world.b.tenant},
    )
    owner_session.commit()
    with pytest.raises(DBAPIError, match="fk_idp_configs__client_secret"):
        owner_session.execute(
            text("UPDATE shifde.idp_configs SET client_secret_ref_id = :s WHERE id = :i"),
            {"s": secret, "i": world.a.idp},
        )
    owner_session.rollback()


# ------------------------------------------------------------------ users (staff directory)
def test_client_context_sees_only_its_members(world: World, a_session: Session) -> None:
    s, a, b = a_session, world.a, world.b
    assert _ids(s, "users") == {a.member, world.both}
    assert repository.get_user(s, b.member) is None
    assert repository.find_user_by_email(s, "member@b.test") is None
    assert repository.user_labels(s, [b.member, world.both]) == {world.both: "both@shi.test"}


def test_a_client_context_cannot_create_a_person(world: World, app_db: Database) -> None:
    """B-901: SETH let any client context create a platform-wide user."""
    with (
        app_session(app_db, world.a.tenant, world.a.member) as s,
        pytest.raises(DBAPIError, match=ROW_SECURITY),
    ):
        repository.insert_user_basic(s, uuid7(), "squat@shi.test", "Squatter", utcnow())


# ------------------------------------------------------------------ a person's own rows
@pytest.mark.parametrize("tenant", ["none", "a", "b"])
def test_a_person_sees_only_their_own_principal_rows(
    world: World, app_db: Database, tenant: str
) -> None:
    """In any client context (or none) the principal tables show the acting person's rows only."""
    tenants = {"none": None, "a": world.a.tenant, "b": world.b.tenant}
    with app_session(app_db, tenants[tenant], world.a.member) as s:
        for table in ("local_credentials", "sessions", "user_identities"):
            assert _ids(s, table, "user_id") == {world.a.member}, table
        assert repository.lock_credentials(s, world.b.member) is None
        repository.record_login_failure(
            s, world.b.member, now=utcnow(), threshold=1, base_seconds=60, max_seconds=60
        )
        repository.reset_login_state(s, world.b.member)
        s.commit()
        _refused(
            s,
            "INSERT INTO shifde.sessions (id, session_hash, user_id, csrf_hash, idle_expires_at, "
            "absolute_expires_at) VALUES (:id, 'x', :u, 'c', now(), now())",
            {"id": uuid7(), "u": world.b.member},
        )
        _refused(
            s,
            "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, idp_config_id) "
            "VALUES (:id, :u, 'i', 's', :c)",
            {"id": uuid7(), "u": world.b.member, "c": world.b.idp},
        )


def test_another_person_cannot_change_b_credentials_or_sessions(
    world: World, app_db: Database, owner_session: Session
) -> None:
    sid = owner_session.execute(
        text("SELECT id FROM shifde.sessions WHERE user_id = :u"), {"u": world.b.member}
    ).scalar_one()
    with app_session(app_db, world.b.tenant, world.a.member) as s:
        repository.revoke_session_by_id(s, sid, utcnow())
        repository.touch_session(s, sid, now=utcnow(), idle_expires_at=utcnow())
        repository.record_login_failure(
            s, world.b.member, now=utcnow(), threshold=1, base_seconds=60, max_seconds=60
        )
        s.commit()
    row = owner_session.execute(
        text(
            "SELECT s.revoked_at, c.failed_attempts, c.locked_until FROM shifde.sessions s "
            "JOIN shifde.local_credentials c ON c.user_id = s.user_id WHERE s.id = :id"
        ),
        {"id": sid},
    ).one()
    assert tuple(row) == (None, 0, None)


# ------------------------------------------------------------------ no context
def test_nothing_is_visible_without_a_context(world: World, app_db: Database) -> None:
    with app_session(app_db, None) as s:
        for table in (
            "users",
            "memberships",
            "local_credentials",
            "sessions",
            "service_accounts",
            "api_keys",
            "user_identities",
        ):
            assert _ids(s, table, "user_id" if table == "local_credentials" else "id") == set()
        # only the installation-wide IdP list is public (SETH: sign-in page before any login)
        assert _ids(s, "idp_configs") == {world.installation_idp}


@pytest.mark.parametrize("tenant", ["none", "a"])
def test_setup_tokens_are_never_readable_by_the_app(
    world: World, app_db: Database, owner_session: Session, tenant: str
) -> None:
    owner_session.execute(
        text(
            "INSERT INTO shifde.setup_tokens (id, user_id, token_hash, purpose, expires_at, "
            "tenant_id) VALUES (:id, :u, 'tok', 'invite', :e, :t)"
        ),
        {
            "id": uuid7(),
            "u": world.a.member,
            "e": utcnow() + timedelta(days=1),
            "t": world.a.tenant,
        },
    )
    owner_session.commit()
    with app_session(app_db, world.a.tenant if tenant == "a" else None, world.a.member) as s:
        _refused(s, "SELECT * FROM shifde.setup_tokens", {}, match="permission denied")


# ------------------------------------------------------------------ authz binding trigger
def _bind(s: Session, tenant: UUID, subject_type: str, subject: UUID) -> None:
    role = s.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = 'auditor'")
    ).scalar_one()
    s.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) VALUES (:id, :t, :st, :sid, :r, 'tenant', :t)"
        ),
        {"id": uuid7(), "t": tenant, "st": subject_type, "sid": subject, "r": role},
    )


@pytest.mark.parametrize(
    ("subject_type", "subject", "message"),
    [
        ("service_account", "b_sa", "subject service account is not in this tenant"),
        ("user", "b_member", "subject user is not a member of this tenant"),
    ],
)
def test_binding_another_clients_subject_is_refused_with_the_triggers_message(
    world: World, a_session: Session, subject_type: str, subject: str, message: str
) -> None:
    subjects = {"b_sa": world.b.service_account, "b_member": world.b.member}
    with pytest.raises(DBAPIError, match=message):
        _bind(a_session, world.a.tenant, subject_type, subjects[subject])
    a_session.rollback()


@pytest.mark.parametrize("subject_type", ["service_account", "user"])
def test_binding_own_subject_passes_the_subject_check_and_fails_closed_until_tenancy(
    world: World, a_session: Session, subject_type: str
) -> None:
    """A's own service account and member get past the subject branch; the scope branch then
    needs `workspaces` (tenancy), so every binding still fails closed (B-402)."""
    subject = world.a.service_account if subject_type == "service_account" else world.a.member
    with pytest.raises(DBAPIError, match='relation "shifde.workspaces" does not exist'):
        _bind(a_session, world.a.tenant, subject_type, subject)
    a_session.rollback()


# ------------------------------------------------------------------ identity part 2a (B-909..)
INSUFFICIENT = "insufficient_privilege|InsufficientPrivilege|invitation refused"


def _owner_person(owner: Session, email: str) -> UUID:
    uid = uuid7()
    owner.execute(
        text("INSERT INTO shifde.users (id, primary_email, display_name) VALUES (:i, :e, :e)"),
        {"i": uid, "e": email},
    )
    return uid


@pytest.mark.parametrize("target", ["b_member", "both", "fresh"])
def test_a_cannot_add_another_clients_member_or_any_person_to_itself(
    world: World, app_db: Database, owner_session: Session, target: str
) -> None:
    """Review 1 item 1 (MUST): client A's context could insert a membership for B's member (then
    read their e-mail through users_select and, with SETH's invite definer, mint their first
    credential). Only the platform administrator inserts memberships now."""
    fresh = _owner_person(owner_session, "fresh@shi.test")
    owner_session.commit()
    person = {"b_member": world.b.member, "both": world.both, "fresh": fresh}[target]
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        if target != "both":  # `both` is already A's member: the unique key would answer first
            _refused(
                s,
                "INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:id, :t, :u)",
                {"id": uuid7(), "t": world.a.tenant, "u": person},
            )
        with pytest.raises(Forbidden):
            service.add_member(s, tenant_id=world.a.tenant, user_id=person)
        assert repository.find_user_by_email(s, "member@b.test") is None
    count = owner_session.execute(
        text("SELECT count(*) FROM shifde.memberships WHERE tenant_id = :t AND user_id = :u"),
        {"t": world.a.tenant, "u": person},
    ).scalar_one()
    assert count == (1 if target == "both" else 0)


@pytest.mark.parametrize("target", ["b_member", "fresh_member_of_a"])
def test_a_cannot_mint_an_invite_or_setup_credential_for_a_person(
    world: World, app_db: Database, owner_session: Session, target: str
) -> None:
    """Review 1 item 1: invitation and setup credentials are not minted on the strength of a
    membership row. A client context is refused through the service, the definer and raw SQL,
    even for a person with no credential who is a member of A."""
    fresh = _owner_person(owner_session, "fresh@shi.test")
    owner_session.execute(
        text("INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:id, :t, :u)"),
        {"id": uuid7(), "t": world.a.tenant, "u": fresh},
    )
    owner_session.commit()
    person = world.b.member if target == "b_member" else fresh
    expires = utcnow() + timedelta(hours=1)
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(Forbidden):
            service.create_invite_token(s, person)
        with pytest.raises(DBAPIError, match=INSUFFICIENT):
            repository.create_invite_token(s, person, b"forged-invite", expires)
        s.rollback()
        _refused(
            s,
            "INSERT INTO shifde.setup_tokens (id, user_id, token_hash, purpose, expires_at) "
            "VALUES (:id, :u, 'forged', 'bootstrap', :e)",
            {"id": uuid7(), "u": person, "e": expires},
            match="permission denied",
        )
    tokens = owner_session.execute(
        text("SELECT count(*) FROM shifde.setup_tokens WHERE user_id = :u"), {"u": person}
    ).scalar_one()
    assert tokens == 0


def test_a_session_pinned_to_another_client_does_not_resolve(
    world: World, app_db: Database
) -> None:
    """Review 1 item 4 / B-116 review 5 item 3: `sessions` is own_row, so a person can insert a
    session of their own pinned to any client id; `shifde_resolve_session` returns nothing for a
    pin the person has no active membership in (A's member pinned to B)."""
    now = utcnow()
    with app_session(app_db, None, world.a.member) as s:
        repository.insert_session(
            s,
            {
                "id": uuid7(),
                "session_hash": b"pinned-to-b",
                "user_id": world.a.member,
                "active_tenant_id": world.b.tenant,
                "csrf_hash": b"csrf",
                "idle_expires_at": now + timedelta(hours=1),
                "absolute_expires_at": now + timedelta(hours=8),
            },
        )
        s.commit()
    with app_session(app_db, None) as s:
        assert repository.resolve_session(s, b"pinned-to-b") is None
        own = repository.resolve_session(s, world.a.session_hash)
        assert own is not None and own.user_id == world.a.member


def test_a_cannot_revoke_b_members_sessions(world: World, app_db: Database) -> None:
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(DBAPIError, match="not a member of the current tenant"):
            repository.revoke_user_sessions(s, world.b.member)
        s.rollback()
    with app_session(app_db, None) as s:
        row = repository.resolve_session(s, world.b.session_hash)
        assert row is not None and row.revoked_at is None


def test_resolve_session_never_reports_admin_on_a_pinned_session(
    world: World, app_db: Database, owner_session: Session
) -> None:
    owner_session.execute(
        text("UPDATE shifde.users SET is_platform_admin = true WHERE id = :u"), {"u": world.both}
    )
    now = utcnow()
    for tenant, digest in ((world.a.tenant, b"admin-pinned"), (None, b"admin-free")):
        owner_session.execute(
            text(
                "INSERT INTO shifde.sessions (id, session_hash, user_id, active_tenant_id, "
                "csrf_hash, idle_expires_at, absolute_expires_at) VALUES (:id, :h, :u, :t, 'c', "
                ":i, :a)"
            ),
            {
                "id": uuid7(),
                "h": digest,
                "u": world.both,
                "t": tenant,
                "i": now + timedelta(hours=1),
                "a": now + timedelta(hours=8),
            },
        )
    owner_session.commit()
    with app_session(app_db, None) as s:
        pinned = repository.resolve_session(s, b"admin-pinned")
        free = repository.resolve_session(s, b"admin-free")
    assert pinned is not None and pinned.is_platform_admin is False
    assert free is not None and free.is_platform_admin is True


def test_my_membership_tenants_lists_only_the_callers_own(world: World, app_db: Database) -> None:
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        assert repository.my_membership_tenants(s) == [world.a.tenant]
    with app_session(app_db, None, world.both) as s:
        assert set(repository.my_membership_tenants(s)) == {world.a.tenant, world.b.tenant}
    with app_session(app_db, None) as s:
        assert repository.my_membership_tenants(s) == []


def test_login_lockouts_are_private_to_their_person(
    world: World, app_db: Database, owner_session: Session
) -> None:
    owner_session.execute(
        text(
            "INSERT INTO shifde.login_lockouts (user_id, source_hash, failed_attempts) "
            "VALUES (:u, 'src', 3)"
        ),
        {"u": world.b.member},
    )
    owner_session.commit()
    with app_session(app_db, world.b.tenant, world.a.member) as s:
        assert s.execute(text("SELECT count(*) FROM shifde.login_lockouts")).scalar_one() == 0
        repository.reset_login_source(s, world.b.member, b"src", utcnow())
        s.commit()
        _refused(
            s,
            "INSERT INTO shifde.login_lockouts (user_id, source_hash) VALUES (:u, 'x')",
            {"u": world.b.member},
        )
    stored = owner_session.execute(
        text("SELECT failed_attempts FROM shifde.login_lockouts WHERE user_id = :u"),
        {"u": world.b.member},
    ).scalar_one()
    assert stored == 3


# ------------------------------------------------------------------ routes (identity part 2b)
ROUTE_TESTS = {
    ("GET", "/api/auth/config"): "test_auth_config_lists_no_clients_idp",
    ("POST", "/api/auth/local/login"): "test_login_shows_and_audits_only_the_persons_own_clients",
    ("GET", "/api/auth/session"): "test_a_session_shows_only_its_own_person_and_clients",
    ("POST", "/api/auth/logout"): "test_logout_ends_only_the_callers_own_session",
    ("POST", "/api/auth/setup/redeem"): "test_redeeming_a_token_signs_in_only_its_own_person",
}


def test_every_identity_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in identity_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


@pytest.fixture
def people(owner_session: Session, world: World) -> tuple[str, str]:
    """A person with a password in A only and one in B only (e-mails)."""
    a, b = "route-a@a.test", "route-b@b.test"
    owner_membership(owner_session, world.a.tenant, person(owner_session, a))
    owner_membership(owner_session, world.b.tenant, person(owner_session, b))
    owner_session.commit()
    return a, b


def _org_ids(body: dict[str, Any]) -> set[str]:  # Any: JSON response body
    return {m["org"]["id"] for m in body["memberships"]}


def test_auth_config_lists_no_clients_idp(auth_client: TestClient, world: World) -> None:
    providers = auth_client.get("/api/auth/config").json()["oidc_providers"]
    assert [p["id"] for p in providers] == [str(world.installation_idp)]


def test_login_shows_and_audits_only_the_persons_own_clients(
    auth_client: TestClient, world: World, people: tuple[str, str], owner_session: Session
) -> None:
    a, _ = people
    r = auth_client.post("/api/auth/local/login", json={"email": a, "password": PASSWORD})
    assert r.status_code == 200 and _org_ids(r.json()) == {str(world.a.tenant)}
    failed = auth_client.post("/api/auth/local/login", json={"email": a, "password": "wrong"})
    assert failed.status_code == 401
    rows = owner_session.execute(
        text("SELECT action, tenant_id FROM shifde.audit_events WHERE action LIKE 'identity.%'")
    ).all()
    assert rows and all(t == world.a.tenant for _, t in rows)  # nothing in B, nothing global


def test_a_session_shows_only_its_own_person_and_clients(
    settings: Settings, world: World, people: tuple[str, str], stub_memberships: None
) -> None:
    a, b = people
    with client_for(settings) as ca, client_for(settings) as cb:
        login(ca, a)
        login(cb, b)
        sa, sb = ca.get("/api/auth/session").json(), cb.get("/api/auth/session").json()
    assert sa["user"]["email"] == a and _org_ids(sa) == {str(world.a.tenant)}
    assert sb["user"]["email"] == b and _org_ids(sb) == {str(world.b.tenant)}


def test_logout_ends_only_the_callers_own_session(
    settings: Settings, world: World, people: tuple[str, str], stub_memberships: None
) -> None:
    a, b = people
    with client_for(settings) as ca, client_for(settings) as cb:
        headers = login(ca, a)
        login(cb, b)
        assert ca.post("/api/auth/logout", headers=headers).status_code == 204
        assert ca.get("/api/v1/probe/whoami").status_code == 401
        assert cb.get("/api/v1/probe/whoami").status_code == 200


def test_redeeming_a_token_signs_in_only_its_own_person(
    auth_client: TestClient, world: World, owner_session: Session
) -> None:
    newcomer = owner_user(owner_session, "newcomer@a.test")
    owner_membership(owner_session, world.a.tenant, newcomer)
    token, _ = service.create_setup_token(owner_session, newcomer)
    owner_session.commit()
    body = {"token": token, "password": "a long new password 1"}
    r = auth_client.post("/api/auth/setup/redeem", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == str(newcomer)
    assert _org_ids(r.json()) == {str(world.a.tenant)}
    assert auth_client.post("/api/auth/setup/redeem", json=body).status_code == 401  # single use
    hashes = owner_session.execute(
        text("SELECT password_hash FROM shifde.local_credentials WHERE user_id IN (:a, :b)"),
        {"a": world.a.member, "b": world.b.member},
    ).scalars()
    assert sorted(hashes) == ["hash-a", "hash-b"]  # nobody else's credential was written


def _mint(app_db: Database, settings: Settings, tenant: UUID, sa: UUID) -> str:
    s = app_session(app_db, tenant)
    try:
        _, full = apikeys.mint(
            s,
            settings,
            tenant_id=tenant,
            name="probe",
            principal_type="service_account",
            principal_id=sa,
            scopes=["read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=None,
        )
        s.commit()
        return full
    finally:
        s.close()


@pytest.mark.parametrize("credential", ["session", "api_key"])
def test_a_cannot_reach_bs_objects_by_id_or_list(
    settings: Settings,
    world: World,
    people: tuple[str, str],
    app_db: Database,
    owner_session: Session,
    stub_memberships: None,
    credential: str,
) -> None:
    """Behind `require_auth` (A's session or A's API key): B's service account and API key by id
    are answered exactly like a missing id (GET and DELETE, under B's org and under A's), B's keys
    are absent from the lists, and B's key stays unrevoked."""
    a, _ = people
    key = _mint(app_db, settings, world.a.tenant, world.a.service_account)
    base = "/api/v1/probe/orgs"
    with client_for(settings) as c:
        if credential == "session":
            headers = login(c, a)
        else:
            headers = {"Authorization": f"Bearer {key}"}
            who = c.get("/api/v1/probe/whoami", headers=headers).json()
            assert who["tenant_id"] == who["db_tenant_id"] == str(world.a.tenant)
        own_sa = f"{base}/{world.a.tenant}/service-accounts/{world.a.service_account}"
        assert c.get(own_sa, headers=headers).status_code == (
            200 if credential == "api_key" else 404  # a session sets no client context
        )
        for org in (world.b.tenant, world.a.tenant):
            foreign = c.get(
                f"{base}/{org}/service-accounts/{world.b.service_account}", headers=headers
            )
            missing = c.get(f"{base}/{org}/service-accounts/{uuid7()}", headers=headers)
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json()["detail"] == missing.json()["detail"]
            gone = c.delete(f"{base}/{org}/api-keys/{world.b.api_key}", headers=headers)
            nothing = c.delete(f"{base}/{org}/api-keys/{uuid7()}", headers=headers)
            assert gone.status_code == nothing.status_code == 404
            assert gone.json()["detail"] == nothing.json()["detail"]
        listed = c.get(f"{base}/{world.b.tenant}/api-keys", headers=headers)
        assert listed.status_code == 200 and listed.json() == []
    revoked = owner_session.execute(
        text("SELECT revoked_at FROM shifde.api_keys WHERE id = :i"), {"i": world.b.api_key}
    ).scalar_one()
    assert revoked is None
