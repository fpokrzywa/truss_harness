"""Cross-client isolation of identity's tables, as `shifde_app` (each session asserts
`current_user`).

Client A cannot read, list or modify client B's memberships, service accounts, API keys or IdP
configurations, through the repository or raw SQL, and cannot create rows under B's id; a
person's own principal rows (credentials, sessions, IdP subjects) are invisible to everyone else;
with no context nothing is visible; constraints (which bypass row-level security) refuse a row
of A naming B's object. Also the authz binding trigger's subject branches that need only
identity's tables (carry_forward: "Authz binding cross-client tests must match the trigger's
specific messages").

Identity part 1 has no routes: the login, session and API-key routes arrive with part 2, OIDC
with part 3, member and IdP administration with tenancy/admin; their cross-client tests arrive
with them (docs/build/carry_forward.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.identity import repository
from tests.identity.conftest import World, app_session

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
