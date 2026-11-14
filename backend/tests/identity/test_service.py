"""`shifde.identity.service` (identity part 2a) as `shifde_app`: local login, sessions, logout,
setup and invitation tokens, members, and the fixes of this part (DECISIONS B-909..B-914).

SETH 3ba0e06 has no test of the service alone: its tests drive it through the app `World` and the
routes (`test_api_auth.py`, `test_p2_review.py`, ...), which move with identity part 2b and
tenancy (docs/build/carry_forward.md). These tests use the owner-seeded identity `World`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import Database, platform_admin_context
from shifde.core.errors import (
    Conflict,
    CsrfFailed,
    Forbidden,
    InvalidCredentials,
    Unauthenticated,
)
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import repository, schemas, service
from shifde.identity.passwords import PasswordService
from tests.identity.conftest import World, app_session, owner_membership, owner_user

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def passwords() -> PasswordService:
    return PasswordService(fast=True)


@pytest.fixture
def svc_settings(app_url: str, owner_url: str) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
    )


@pytest.fixture
def s(app_db: Database) -> Iterator[Session]:
    session = app_session(app_db, None)
    yield session
    session.close()


def _person(owner: Session, passwords: PasswordService, email: str, *, admin: bool = False) -> UUID:
    uid = owner_user(owner, email, admin=admin)
    owner.execute(
        text("INSERT INTO shifde.local_credentials (user_id, password_hash) VALUES (:u, :h)"),
        {"u": uid, "h": passwords.hash(PASSWORD)},
    )
    owner.commit()
    return uid


def _ctx(ip: str = "10.0.0.1") -> RequestContext:
    return RequestContext(request_id="req", client_ip=ip, user_agent="pytest")


def _login(
    s: Session,
    settings: Settings,
    passwords: PasswordService,
    email: str,
    password: str = PASSWORD,
    ip: str = "10.0.0.1",
) -> service.LoginResult:
    return service.login_local(
        s, settings, passwords, _ctx(ip), email=email, password=password, previous_token=None
    )


def _events(owner: Session, action: str) -> list[UUID | None]:
    owner.rollback()
    return list(
        owner.execute(
            text("SELECT tenant_id FROM shifde.audit_events WHERE action = :a ORDER BY tenant_id"),
            {"a": action},
        ).scalars()
    )


# ------------------------------------------------------------------ login, sessions, logout
def test_login_opens_a_session_that_authenticates_and_is_audited_per_client(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    uid = _person(owner_session, passwords, "staff@shi.test")
    owner_membership(owner_session, world.a.tenant, uid)
    owner_session.commit()
    result = _login(s, svc_settings, passwords, " Staff@SHI.test ")
    auth = service.authenticate(
        s, svc_settings, _ctx(), token=result.session_token, method="GET", csrf_header=None
    )
    assert auth.principal.user_id == uid
    assert auth.principal.tenant_id is None and not auth.principal.is_platform_admin
    assert auth.csrf_token == result.auth.csrf_token
    assert _events(owner_session, "identity.login.succeeded") == [world.a.tenant]


def test_unknown_account_and_wrong_password_give_one_error(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    _person(owner_session, passwords, "staff@shi.test")
    errors = []
    for email, password in (("nobody@shi.test", PASSWORD), ("staff@shi.test", "wrong password!")):
        with pytest.raises(InvalidCredentials) as err:
            _login(s, svc_settings, passwords, email, password)
        errors.append(str(err.value))
    assert errors[0] == errors[1]
    # the unknown account is filed globally; the known one (no client) globally too
    assert _events(owner_session, "identity.login.failed") == [None, None]


def test_unsafe_methods_need_the_csrf_token_and_logout_ends_the_session(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    _person(owner_session, passwords, "staff@shi.test")
    result = _login(s, svc_settings, passwords, "staff@shi.test")
    with pytest.raises(CsrfFailed):
        service.authenticate(
            s, svc_settings, _ctx(), token=result.session_token, method="POST", csrf_header=None
        )
    auth = service.authenticate(
        s,
        svc_settings,
        _ctx(),
        token=result.session_token,
        method="POST",
        csrf_header=result.auth.csrf_token,
    )
    service.logout(s, _ctx(), auth)
    with pytest.raises(Unauthenticated):
        service.authenticate(
            s, svc_settings, _ctx(), token=result.session_token, method="GET", csrf_header=None
        )


def test_failures_from_one_source_do_not_lock_the_person_out_from_another(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    """SEC Minor-1 (B-911): knowing the e-mail is not enough to lock its owner out. SETH counted
    failures per account, so the victim's correct password was refused from every address."""
    _person(owner_session, passwords, "victim@shi.test")
    for _ in range(service.LOCKOUT_THRESHOLD):
        with pytest.raises(InvalidCredentials):
            _login(
                s, svc_settings, passwords, "victim@shi.test", "wrong password!", ip="203.0.113.9"
            )
    with pytest.raises(InvalidCredentials):  # the attacking source is locked, correct or not
        _login(s, svc_settings, passwords, "victim@shi.test", ip="203.0.113.9")
    assert _login(s, svc_settings, passwords, "victim@shi.test", ip="10.0.0.1").session_token


def test_a_session_pinned_to_a_client_has_no_admin_power_and_lapses_with_the_membership(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    """Review 1 item 4: a pinned session never reports platform administrator, and its pin is
    rechecked on every request against the person's current membership."""
    admin = _person(owner_session, passwords, "admin@shi.test", admin=True)
    membership = owner_membership(owner_session, world.a.tenant, admin)
    owner_session.commit()
    pinned = service.establish_session(
        s,
        svc_settings,
        _ctx(),
        user_id=admin,
        previous_token=None,
        auth_method="oidc",
        active_tenant_id=world.a.tenant,
    )
    assert not pinned.auth.principal.is_platform_admin
    auth = service.authenticate(
        s, svc_settings, _ctx(), token=pinned.session_token, method="GET", csrf_header=None
    )
    assert auth.principal.tenant_id == world.a.tenant and not auth.principal.is_platform_admin
    unpinned = _login(s, svc_settings, passwords, "admin@shi.test")
    assert unpinned.auth.principal.is_platform_admin
    owner_session.execute(
        text("UPDATE shifde.memberships SET status = 'suspended' WHERE id = :id"),
        {"id": membership},
    )
    owner_session.commit()
    s.rollback()
    with pytest.raises(Unauthenticated):
        service.authenticate(
            s, svc_settings, _ctx(), token=pinned.session_token, method="GET", csrf_header=None
        )


# ------------------------------------------------------------------ setup and invitation tokens
def test_a_bootstrap_token_is_redeemed_once(
    world: World,
    owner_session: Session,
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    uid = owner_user(owner_session, "boot@shi.test", admin=True)
    token, _ = service.create_setup_token(owner_session, uid)
    owner_session.commit()
    result = service.redeem_setup_token(
        s, svc_settings, passwords, _ctx(), token=token, password=PASSWORD, previous_token=None
    )
    assert result is not None and result.auth.principal.user_id == uid
    with pytest.raises(InvalidCredentials):
        service.redeem_setup_token(
            s, svc_settings, passwords, _ctx(), token=token, password=PASSWORD, previous_token=None
        )
    assert _events(owner_session, "identity.setup_token.rejected") == [None]
    assert _events(owner_session, "identity.setup_token.redeemed") == [None]


def test_only_the_platform_administrator_invites_a_person_without_credentials(
    world: World,
    owner_session: Session,
    app_db: Database,
    svc_settings: Settings,
    passwords: PasswordService,
) -> None:
    admin = _person(owner_session, passwords, "admin@shi.test", admin=True)
    new = owner_user(owner_session, "new@shi.test")
    owner_membership(owner_session, world.a.tenant, new)
    owner_session.commit()
    # a client context (even with the person a member there) cannot invite: review 1 item 1
    a = app_session(app_db, world.a.tenant, world.a.member)
    with pytest.raises(Forbidden):
        service.create_invite_token(a, new)
    a.close()
    s = app_session(app_db, world.a.tenant, admin)
    with platform_admin_context(s):
        refused = []
        for target in (world.b.member, admin):  # has a credential; a platform administrator
            with pytest.raises(Forbidden) as err:
                service.create_invite_token(s, target)
            refused.append(str(err.value))
        assert len(set(refused)) == 1  # one generic error
        token, expires = service.create_invite_token(s, new)
    s.commit()
    assert expires > utcnow() + timedelta(hours=71)
    s.close()
    s = app_session(app_db, None)
    result = service.redeem_setup_token(
        s, svc_settings, passwords, _ctx(), token=token, password=PASSWORD, previous_token=None
    )
    assert result is not None and result.auth.principal.user_id == new
    s.close()


# ------------------------------------------------------------------ members
def test_only_the_platform_administrator_adds_members_with_one_generic_error(
    world: World, owner_session: Session, app_db: Database
) -> None:
    person = owner_user(owner_session, "person@shi.test")
    owner_session.commit()
    a = app_session(app_db, world.a.tenant, world.a.member)
    with pytest.raises(Forbidden):
        service.add_member(a, tenant_id=world.a.tenant, user_id=person)
    with pytest.raises(Forbidden):
        service.create_member_user(
            a, tenant_id=world.a.tenant, email="fresh@shi.test", display_name="Fresh"
        )
    with platform_admin_context(a):
        service.add_member(a, tenant_id=world.a.tenant, user_id=person)
        messages = []
        for user in (person, uuid7()):  # already a member; no such person
            with pytest.raises(Conflict) as err:
                service.add_member(a, tenant_id=world.a.tenant, user_id=user)
            messages.append(str(err.value))
        assert messages[0] == messages[1]
        created = service.create_member_user(
            a, tenant_id=world.a.tenant, email="Fresh@SHI.test", display_name="Fresh"
        )
    a.commit()
    assert service.is_member(a, world.a.tenant, person)
    assert service.is_member(a, world.a.tenant, created)
    a.close()


def test_suspending_a_member_revokes_their_sessions_in_that_client(
    world: World, owner_session: Session, app_db: Database
) -> None:
    a = app_session(app_db, world.a.tenant, world.a.member)
    _, sessions_revoked, keys_revoked = service.set_member_status(
        a,
        tenant_id=world.a.tenant,
        user_id=world.both,
        status="suspended",
        expected_version=1,
        actor_id=world.a.member,
    )
    a.commit()
    a.close()
    assert (sessions_revoked, keys_revoked) == (0, 0)
    with pytest.raises(Conflict):
        a = app_session(app_db, world.a.tenant, world.a.member)
        try:
            service.set_member_status(
                a,
                tenant_id=world.a.tenant,
                user_id=world.both,
                status="active",
                expected_version=1,
                actor_id=world.a.member,
            )
        finally:
            a.close()


# ------------------------------------------------------------------ review 1 items 5, 6
def test_platform_admin_exists_is_owner_only(owner_session: Session, s: Session) -> None:
    """Review 1 item 5: under `shifde_app` the count saw no rows and returned False."""
    owner_user(owner_session, "admin@shi.test", admin=True)
    owner_session.commit()
    with pytest.raises(repository.OwnerOnly):
        service.platform_admin_exists(s)
    assert service.platform_admin_exists(owner_session)


def test_last_login_at_is_not_in_a_client_facing_shape() -> None:
    """Review 1 item 6: `users.last_login_at` is readable by every client the person belongs to;
    no identity DTO or member row carries it."""
    shapes = [
        cls
        for cls in vars(schemas).values()
        if isinstance(cls, type) and hasattr(cls, "model_fields")
    ]
    assert shapes
    for shape in shapes:
        assert "last_login_at" not in shape.model_fields, shape
    for row in (repository.MemberRow, repository.AdminMemberRow, repository.SessionRow):
        assert "last_login_at" not in row.__dataclass_fields__, row
