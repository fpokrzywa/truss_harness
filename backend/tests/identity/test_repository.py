"""Direct tests of `shifde.identity.repository` (part 1) as `shifde_app` in the caller's own
context, and of the identity tables' guards and grants.

The repository functions that call SETH's SECURITY DEFINER functions (`resolve_local_login`,
`resolve_session`, `my_memberships`, `revoke_session_by_hash`, `create_invite_token`,
`revoke_user_sessions`, `redeem_setup_token`, `resolve_identity`, `resolve_api_key`,
`resolve_idp`) are tested with their functions in identity part 2/3 (DECISIONS B-904).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.identity import repository
from tests.identity.conftest import World, app_session, owner_login_session, owner_user


def test_login_failures_lock_and_reset(world: World, app_db: Database) -> None:
    a = world.a
    now = utcnow()
    with app_session(app_db, None, a.member) as s:
        row = repository.lock_credentials(s, a.member)
        assert row is not None and row.password_hash == "hash-a" and row.failed_attempts == 0
        for _ in range(3):
            repository.record_login_failure(
                s, a.member, now=now, threshold=3, base_seconds=60, max_seconds=600
            )
        row = repository.lock_credentials(s, a.member)
        assert row is not None and row.failed_attempts == 3
        assert row.locked_until is not None and row.locked_until > now
        repository.reset_login_state(s, a.member)
        row = repository.lock_credentials(s, a.member)
        assert row is not None and (row.failed_attempts, row.locked_until) == (0, None)
        repository.touch_last_login(s, a.member, now)
        user = repository.get_user(s, a.member)
        assert user is not None and user.last_login_at is not None
        s.commit()


def test_sessions_insert_touch_and_revoke(world: World, app_db: Database) -> None:
    a = world.a
    now = utcnow()
    sid = uuid7()
    with app_session(app_db, None, a.member) as s:
        repository.insert_session(
            s,
            {
                "id": sid,
                "session_hash": b"new-session",
                "user_id": a.member,
                "csrf_hash": b"csrf",
                "idle_expires_at": now + timedelta(minutes=30),
                "absolute_expires_at": now + timedelta(hours=8),
                "active_tenant_id": a.tenant,
            },
        )
        repository.touch_session(s, sid, now=now, idle_expires_at=now + timedelta(hours=1))
        repository.revoke_session_by_id(s, sid, now)
        s.commit()
        with pytest.raises(DBAPIError, match="a revoked session cannot be reinstated"):
            s.execute(
                text("UPDATE shifde.sessions SET revoked_at = NULL WHERE id = :id"), {"id": sid}
            )
        s.rollback()


def test_promotion_to_platform_admin_revokes_sessions(world: World, owner_session: Session) -> None:
    owner_session.execute(
        text("UPDATE shifde.users SET is_platform_admin = true WHERE id = :u"),
        {"u": world.a.member},
    )
    owner_session.commit()
    revoked = owner_session.execute(
        text("SELECT revoked_at IS NOT NULL FROM shifde.sessions WHERE user_id = :u"),
        {"u": world.a.member},
    ).scalar_one()
    assert revoked is True
    assert repository.platform_admin_exists(owner_session)


def test_members_listing_and_status(world: World, app_db: Database) -> None:
    a = world.a
    with app_session(app_db, a.tenant, a.member) as s:
        members = repository.list_members(s, tenant_id=a.tenant, after=None, limit=10)
        assert {m.user_id for m in members} == {a.member, world.both}
        assert repository.count_members(s, a.tenant) == 2
        admin_rows = repository.list_members_admin(s, tenant_id=a.tenant, after=None, limit=1)
        assert len(admin_rows) == 1
        after = repository.list_members_admin(
            s, tenant_id=a.tenant, after=admin_rows[0].membership_id, limit=10
        )
        assert len(after) == 1 and after[0].membership_id != admin_rows[0].membership_id
        assert repository.is_member(s, a.tenant, world.both)
        updated = repository.update_membership_status(s, a.tenant, world.both, 1, "suspended")
        assert updated is not None and (updated.status, updated.row_version) == ("suspended", 2)
        assert repository.update_membership_status(s, a.tenant, world.both, 1, "active") is None
        s.commit()


def test_people_are_created_by_themselves_or_the_platform_administrator(
    world: World, app_db: Database
) -> None:
    """B-901: JIT sign-in acts as the new user; every other user needs the admin flag."""
    me = uuid7()
    with app_session(app_db, None, me) as s:
        repository.insert_user_basic(s, me, "jit@shi.test", "JIT", utcnow())
        s.commit()
    other = uuid7()
    with app_session(app_db, None, world.a.member) as s:
        with pytest.raises(DBAPIError, match="row-level security"):
            repository.insert_user_basic(s, other, "other@shi.test", "Other", utcnow())
        s.rollback()
        with platform_admin_context(s):
            repository.insert_user_basic(s, other, "other@shi.test", "Other", utcnow())
        s.commit()
        assert repository.find_user_by_email(s, "jit@shi.test") is None  # not a member anywhere


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE shifde.users SET is_platform_admin = true WHERE id = :u",
        "UPDATE shifde.users SET status = 'disabled' WHERE id = :u",
        "UPDATE shifde.local_credentials SET password_hash = 'x' WHERE user_id = :u",
        "UPDATE shifde.sessions SET user_id = user_id WHERE user_id = :u",
        "INSERT INTO shifde.users (id, primary_email, display_name, is_platform_admin) "
        "VALUES (:u, 'self@shi.test', 'Self', true)",
        "INSERT INTO shifde.local_credentials (user_id, password_hash) VALUES (:u, 'x')",
        "DELETE FROM shifde.sessions WHERE user_id = :u",
    ],
)
def test_privilege_columns_are_not_app_writable(
    world: World, app_db: Database, statement: str
) -> None:
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            s.execute(text(statement), {"u": world.a.member})
        s.rollback()


def test_service_accounts_and_keys_in_own_client(world: World, app_db: Database) -> None:
    a = world.a
    now = utcnow()
    with app_session(app_db, a.tenant, a.member) as s:
        sa = uuid7()
        repository.insert_service_account(s, {"id": sa, "tenant_id": a.tenant, "name": "ci"})
        got = repository.update_service_account(s, a.tenant, sa, 1, {"description": "ci bot"})
        assert got is not None and (got.description, got.row_version) == ("ci bot", 2)
        assert repository.update_service_account(s, a.tenant, sa, 1, {"name": "x"}) is None
        names = [
            x.name
            for x in repository.list_service_accounts(s, tenant_id=a.tenant, after=None, limit=10)
        ]
        assert sorted(names) == ["ci", "same-name"]
        key = uuid7()
        repository.insert_api_key(
            s,
            {
                "id": key,
                "tenant_id": a.tenant,
                "name": "ci key",
                "principal_type": "service_account",
                "principal_id": sa,
                "prefix": "cikey001",
                "key_hash": b"h",
                "scopes": ["read"],
                "expires_at": now + timedelta(days=1),
                "rotated_from_id": a.api_key,
            },
        )
        listed = repository.list_api_keys(
            s,
            tenant_id=a.tenant,
            principal_type="service_account",
            principal_id=sa,
            after=None,
            limit=10,
        )
        assert [k.id for k in listed] == [key]
        assert repository.touch_api_key(s, key, now, 60)
        assert not repository.touch_api_key(s, key, now, 60)  # throttled
        repository.shorten_api_key_expiry(s, key, now + timedelta(hours=1))
        s.commit()  # keep the key across the refused statement's rollback
        with pytest.raises(DBAPIError, match="API key expiry can only be shortened"):
            s.execute(
                text(
                    "UPDATE shifde.api_keys SET expires_at = now() + interval '9 days' WHERE id = :k"
                ),
                {"k": key},
            )
        s.rollback()
        assert repository.revoke_api_key(s, key, a.member, now)
        assert not repository.revoke_api_key(s, key, a.member, now)
        s.commit()
        with pytest.raises(DBAPIError, match="a revoked API key cannot be changed"):
            s.execute(
                text("UPDATE shifde.api_keys SET revoked_at = now() WHERE id = :k"), {"k": key}
            )
        s.rollback()
        assert repository.revoke_keys_of_principal(s, a.tenant, "user", a.member, None, now) == 1
        s.commit()


def test_idp_configs_in_own_client_and_installation(world: World, app_db: Database) -> None:
    a = world.a
    with app_session(app_db, a.tenant, a.member) as s:
        idp = repository.get_idp(s, a.tenant, a.idp)
        assert idp is not None
        updated = repository.update_idp(s, a.idp, idp.row_version, {"display_name": "A login"})
        assert updated is not None and updated.display_name == "A login"
        assert repository.get_idp(s, None, world.installation_idp) is not None
        assert [i.id for i in repository.list_idps(s, tenant_id=None, after=None, limit=5)] == [
            world.installation_idp
        ]
        with pytest.raises(DBAPIError, match="ck_idp_configs__tenant_no_jit"):
            repository.update_idp(s, a.idp, 2, {"jit": "create_user"})
        s.rollback()
        new = uuid7()
        repository.insert_idp(
            s,
            {
                "id": new,
                "tenant_id": a.tenant,
                "display_name": "second",
                "issuer": "https://second.a.test",
                "discovery_url": "https://second.a.test/.well-known",
                "client_id": "c",
                "scopes": ["openid"],
                "claim_mappings": {"email": "email"},
            },
        )
        s.commit()
    with app_session(app_db, None, None) as s, platform_admin_context(s):
        got = repository.update_idp(s, world.installation_idp, 1, {"status": "disabled"})
        assert got is not None and got.status == "disabled"
        s.commit()


def test_identity_rows_for_oneself(world: World, app_db: Database, owner_session: Session) -> None:
    me = owner_user(owner_session, "linked@shi.test")
    owner_login_session(owner_session, me, b"linked-session")
    owner_session.commit()
    with app_session(app_db, None, me) as s:
        repository.insert_identity(
            s,
            {
                "id": uuid7(),
                "user_id": me,
                "issuer": "https://login.shi.test",
                "subject": "linked",
                "idp_config_id": world.installation_idp,
            },
        )
        s.commit()
        with pytest.raises(DBAPIError, match="uq_user_identities__idp_subject"):
            repository.insert_identity(
                s,
                {
                    "id": uuid7(),
                    "user_id": me,
                    "issuer": "https://login.shi.test",
                    "subject": "linked",
                    "idp_config_id": world.installation_idp,
                },
            )
        s.rollback()


def test_owner_maintenance_paths(owner_session: Session) -> None:
    """`insert_user`, `upsert_credential`, `insert_setup_token`: the CLI runs them as the owner."""
    uid = uuid7()
    now = utcnow()
    repository.insert_user(
        owner_session,
        {"id": uid, "primary_email": "boot@shi.test", "display_name": "Boot", "created_at": now},
    )
    repository.upsert_credential(owner_session, uid, "first", now)
    repository.upsert_credential(owner_session, uid, "second", now)
    repository.insert_setup_token(
        owner_session,
        {
            "id": uuid7(),
            "user_id": uid,
            "token_hash": b"boot",
            "purpose": "bootstrap",
            "expires_at": now + timedelta(hours=1),
            "created_at": now,
        },
    )
    owner_session.commit()
    stored = owner_session.execute(
        text("SELECT password_hash FROM shifde.local_credentials WHERE user_id = :u"), {"u": uid}
    ).scalar_one()
    assert stored == "second"
    assert not repository.platform_admin_exists(owner_session)
