"""API keys and bearer authentication (identity part 2b): SETH 3ba0e06 `test_p2_credentials.py`
tests that need only identity (keys minted through `apikeys.mint` instead of the admin routes,
which move with `admin`), and the fixes of this part: pepper rotation (findings Item 4, B-916),
the failed-attempt limiter (Item 5b, B-919), audited rejections (Item 5c, B-917), mint refusals
and prefix retries (review 1 item 3), and `shifde_resolve_api_key` (review 1 item 4, B-915).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any  # Any: SQL row values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import Conflict
from shifde.core.settings import Settings
from shifde.identity import apikeys
from tests.identity.conftest import World, app_session
from tests.identity.harness import client_for

WHOAMI = "/api/v1/probe/whoami"
KEY_RE = re.compile(r"^shf_test_([0-9A-Za-z]{8})_[0-9A-Za-z]{43}$")
OTHER_PEPPER = "q" * 40


def mint(
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    principal_type: str,
    principal: UUID,
    **extra: Any,  # Any: mint keyword arguments
) -> tuple[UUID, str]:
    s = app_session(app_db, tenant)
    try:
        row, full = apikeys.mint(
            s,
            settings,
            tenant_id=tenant,
            name="key",
            principal_type=principal_type,
            principal_id=principal,
            scopes=["read"],
            resource_scope=None,
            expires_at=extra.pop("expires_at", utcnow() + timedelta(days=30)),
            # a personal key is minted by its own person (B-921) unless a test says otherwise
            created_by=extra.pop("created_by", principal if principal_type == "user" else None),
            **extra,
        )
        s.commit()
        return row.id, full
    finally:
        s.close()


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _audit(owner: Session, action: str = apikeys.REJECTED_ACTION) -> list[Any]:
    return list(
        owner.execute(
            text(
                "SELECT tenant_id, target_id, details->>'reason' AS reason, actor_id "
                "FROM shifde.audit_events WHERE action = :a ORDER BY occurred_at"
            ),
            {"a": action},
        )
    )


# ------------------------------------------------------------------ SETH test_p2_credentials
def test_key_format_hashed_at_rest_and_shown_once(
    app_db: Database, settings: Settings, world: World, owner_session: Session
) -> None:
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    match = KEY_RE.match(full)
    assert match
    row = owner_session.execute(
        text("SELECT prefix, key_hash, pepper_id FROM shifde.api_keys WHERE id = :i"),
        {"i": key_id},
    ).one()
    assert row.prefix == match.group(1)
    assert full.encode() not in bytes(row.key_hash) and len(bytes(row.key_hash)) == 32
    assert bytes(row.key_hash) == apikeys.hash_key(settings.pepper, full)
    assert row.pepper_id == settings.api_key_pepper_id


def test_bearer_needs_no_csrf_and_never_reads_the_session_cookie(
    auth_client: TestClient, app_db: Database, settings: Settings, world: World
) -> None:
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    auth_client.cookies.set("__Host-shifde_session", "not-a-session")
    r = auth_client.get(WHOAMI, headers=bearer(full))
    assert r.status_code == 200
    assert r.json()["credential"] == "api_key"
    assert r.json()["principal_type"] == "service_account"
    # a mutation needs no CSRF header with a bearer key
    url = f"/api/v1/probe/orgs/{world.a.tenant}/api-keys/{key_id}"
    assert auth_client.delete(url, headers=bearer(full)).status_code == 204


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        "shf_test_short_x",
        "shf_prod_abcdefgh_" + "a" * 43,  # foreign environment
        "shf_test_ZZZZZZZZ_" + "a" * 43,  # unknown prefix
    ],
)
def test_malformed_unknown_and_foreign_env_keys_are_invalid_credentials(
    auth_client: TestClient, token: str
) -> None:
    r = auth_client.get(WHOAMI, headers=bearer(token))
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_wrong_secret_with_valid_prefix_is_invalid(
    auth_client: TestClient, app_db: Database, settings: Settings, world: World
) -> None:
    _, full = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    forged = full[:-1] + ("A" if full[-1] != "A" else "B")
    r = auth_client.get(WHOAMI, headers=bearer(forged))
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_http_basic_is_rejected_everywhere(auth_client: TestClient) -> None:
    for method, url in (("GET", WHOAMI), ("GET", "/api/auth/config"), ("GET", "/api/auth/session")):
        r = auth_client.request(method, url, headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert r.status_code == 401 and r.json()["code"] == "unsupported_auth_scheme"


def test_revoke_expire_flow(
    auth_client: TestClient,
    app_db: Database,
    settings: Settings,
    world: World,
    owner_session: Session,
) -> None:
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    assert auth_client.get(WHOAMI, headers=bearer(full)).status_code == 200
    owner_session.execute(
        text("UPDATE shifde.api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"),
        {"i": key_id},
    )
    owner_session.commit()
    r = auth_client.get(WHOAMI, headers=bearer(full))
    assert r.status_code == 401 and r.json()["code"] == "credential_expired"
    owner_session.execute(
        text("UPDATE shifde.api_keys SET revoked_at = now() WHERE id = :i"), {"i": key_id}
    )
    owner_session.commit()
    r = auth_client.get(WHOAMI, headers=bearer(full))
    assert r.status_code == 401 and r.json()["code"] == "credential_revoked"


def test_last_used_is_recorded_and_throttled(
    auth_client: TestClient,
    app_db: Database,
    settings: Settings,
    world: World,
    owner_session: Session,
) -> None:
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    assert auth_client.get(WHOAMI, headers=bearer(full)).status_code == 200
    first = owner_session.execute(
        text("SELECT last_used_at FROM shifde.api_keys WHERE id = :i"), {"i": key_id}
    ).scalar_one()
    owner_session.commit()
    assert first is not None
    assert auth_client.get(WHOAMI, headers=bearer(full)).status_code == 200
    second = owner_session.execute(
        text("SELECT last_used_at FROM shifde.api_keys WHERE id = :i"), {"i": key_id}
    ).scalar_one()
    assert second == first  # at most one write per key per minute


def test_suspending_the_principal_stops_its_keys(
    auth_client: TestClient,
    app_db: Database,
    settings: Settings,
    world: World,
    owner_session: Session,
) -> None:
    """SETH `test_disabling_a_service_account_revokes_its_keys` / suspension, identity part: a
    key of an inactive principal is refused (the admin routes that revoke move with `admin`)."""
    _, sa_key = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    _, user_key = mint(app_db, settings, world.a.tenant, "user", world.a.member)
    owner_session.execute(
        text("UPDATE shifde.service_accounts SET status = 'disabled' WHERE id = :i"),
        {"i": world.a.service_account},
    )
    owner_session.execute(
        text("UPDATE shifde.memberships SET status = 'suspended' WHERE id = :i"),
        {"i": world.a.membership},
    )
    owner_session.commit()
    for key in (sa_key, user_key):
        r = auth_client.get(WHOAMI, headers=bearer(key))
        assert r.status_code == 401 and r.json()["code"] == "credential_revoked"


# ------------------------------------------------------------------ review 1 items 3 and 4
def test_a_key_is_minted_only_for_a_principal_of_its_own_client(
    app_db: Database, settings: Settings, world: World, owner_session: Session
) -> None:
    """Review 1 item 3 / carry_forward (no foreign key on `principal_id`): B's service account,
    B's member, a person in no client and an unknown id are one generic refusal."""
    before = owner_session.execute(text("SELECT count(*) FROM shifde.api_keys")).scalar_one()
    for ptype, pid in (
        ("service_account", world.b.service_account),
        ("user", world.b.member),
        ("user", world.a.service_account),
        ("bogus", world.a.member),
    ):
        with pytest.raises(Conflict) as err:
            mint(app_db, settings, world.a.tenant, ptype, pid)
        assert str(err.value) == apikeys.MINT_REFUSED
    owner_session.rollback()
    after = owner_session.execute(text("SELECT count(*) FROM shifde.api_keys")).scalar_one()
    assert after == before


def test_a_personal_key_is_minted_only_for_oneself(
    app_db: Database, settings: Settings, world: World, owner_session: Session
) -> None:
    """Isolation review 3 item 4 (B-921): a user key acts as its person under principal-keyed
    policies, so `mint` refuses one whose creator is not that person (another member of the same
    client, or no creator), with the same generic refusal."""
    before = owner_session.execute(text("SELECT count(*) FROM shifde.api_keys")).scalar_one()
    owner_session.commit()
    for creator in (world.both, world.a.service_account, None):
        with pytest.raises(Conflict) as err:
            mint(app_db, settings, world.a.tenant, "user", world.a.member, created_by=creator)
        assert str(err.value) == apikeys.MINT_REFUSED
    after = owner_session.execute(text("SELECT count(*) FROM shifde.api_keys")).scalar_one()
    owner_session.commit()
    assert after == before
    key_id, _ = mint(
        app_db, settings, world.a.tenant, "user", world.a.member, created_by=world.a.member
    )
    assert key_id


def test_a_prefix_collision_is_retried_and_other_refusals_are_generic(
    app_db: Database,
    settings: Settings,
    world: World,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = owner_session.execute(
        text("SELECT prefix FROM shifde.api_keys WHERE id = :i"), {"i": world.b.api_key}
    ).scalar_one()
    owner_session.commit()
    draws = iter([taken, "s" * 43, "Fresh123", "t" * 43])  # B's prefix first, then a free one
    monkeypatch.setattr(apikeys, "_rand", lambda n: next(draws))
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    assert full == f"shf_test_Fresh123_{'t' * 43}"
    monkeypatch.setattr(apikeys, "_rand", lambda n: taken if n == apikeys.PREFIX_LEN else "u" * n)
    with pytest.raises(Conflict) as err:  # every draw collides: generic, not the constraint
        mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    assert str(err.value) == apikeys.MINT_REFUSED
    monkeypatch.undo()
    with pytest.raises(Conflict) as err:  # another constraint (rotated_from names B's key)
        mint(
            app_db,
            settings,
            world.a.tenant,
            "service_account",
            world.a.service_account,
            rotated_from_id=world.b.api_key,
        )
    assert str(err.value) == apikeys.MINT_REFUSED
    assert key_id


def test_resolve_api_key_needs_the_principal_in_the_keys_client(
    auth_client: TestClient,
    settings: Settings,
    world: World,
    owner_session: Session,
    app_db: Database,
) -> None:
    """Review 1 item 4: a key row of A naming B's service account or B's member (written by the
    owner: the app cannot) resolves to nothing; the definer reads nothing of B."""
    keys = []
    for n, (ptype, pid) in enumerate(
        (("service_account", world.b.service_account), ("user", world.b.member))
    ):
        full = f"shf_test_Forged0{n}_{'f' * 43}"
        owner_session.execute(
            text(
                "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, "
                "prefix, key_hash, scopes, expires_at) VALUES (gen_random_uuid(), :t, 'k', :pt, "
                ":p, :pre, :h, ARRAY['read'], now() + interval '1 day')"
            ),
            {
                "t": world.a.tenant,
                "pt": ptype,
                "p": pid,
                "pre": f"Forged0{n}",
                "h": apikeys.hash_key(settings.pepper, full),
            },
        )
        keys.append((f"Forged0{n}", full))
    owner_session.commit()
    s = app_session(app_db, None)
    try:
        for prefix, _ in keys:
            got = s.execute(
                text("SELECT * FROM shifde.shifde_resolve_api_key(:p)"), {"p": prefix}
            ).all()
            assert got == []
        own = s.execute(
            text("SELECT tenant_id FROM shifde.shifde_resolve_api_key(:p)"), {"p": "asa_key0"}
        ).all()
        assert [r[0] for r in own] == [world.a.tenant]
    finally:
        s.close()
    for _, full in keys:
        r = auth_client.get(WHOAMI, headers=bearer(full))
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


# ------------------------------------------------------------------ Item 4: pepper rotation
def test_a_new_pepper_mints_new_keys_while_old_keys_still_verify(
    app_db: Database, settings: Settings, world: World
) -> None:
    _, old = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    rotated = settings.model_copy(
        update={
            "api_key_pepper": SecretStr(OTHER_PEPPER),
            "api_key_pepper_id": "p2",
            "api_key_retired_peppers": SecretStr(f"p1:{settings.pepper.decode()}"),
        }
    )
    new_id, new = mint(app_db, rotated, world.a.tenant, "service_account", world.a.service_account)
    with client_for(rotated) as c:
        assert c.get(WHOAMI, headers=bearer(old)).status_code == 200
        assert c.get(WHOAMI, headers=bearer(new)).status_code == 200
    dropped = rotated.model_copy(update={"api_key_retired_peppers": None})
    with client_for(dropped) as c:  # the retired pepper withdrawn: its keys stop verifying
        r = c.get(WHOAMI, headers=bearer(old))
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
        assert c.get(WHOAMI, headers=bearer(new)).status_code == 200
    with client_for(settings) as c:  # a key of a pepper the server does not know is invalid
        assert c.get(WHOAMI, headers=bearer(new)).status_code == 401
    s = app_session(app_db, world.a.tenant)
    try:
        assert (
            s.execute(
                text("SELECT pepper_id FROM shifde.api_keys WHERE id = :i"), {"i": new_id}
            ).scalar_one()
            == "p2"
        )
    finally:
        s.close()


@pytest.mark.parametrize(
    "retired",
    [
        "p1",
        "p2:" + "x" * 40,
        "p1:short",
        "bad id:" + "x" * 40,
        "p3:" + "x" * 40 + ",p3:" + "y" * 40,
    ],
)
def test_a_malformed_pepper_ring_is_refused_at_startup(
    app_url: str, owner_url: str, retired: str
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            env="test",
            database_url=app_url,
            migration_database_url=owner_url,
            api_key_pepper="p" * 40,
            api_key_pepper_id="p2",
            api_key_retired_peppers=retired,
        )


def test_pkce_does_not_derive_from_the_api_key_pepper(settings: Settings) -> None:
    """Item 4: the OIDC PKCE key (used by identity part 3) is not the pepper and does not move
    when the pepper rotates."""
    rotated = settings.model_copy(update={"api_key_pepper": SecretStr(OTHER_PEPPER)})
    assert settings.pkce_key != settings.pepper
    assert rotated.pkce_key == settings.pkce_key
    assert len(settings.pkce_key) == 32


# ------------------------------------------------------------------ Item 5b: failure limiter
def test_failed_bearer_attempts_are_limited_per_source(
    settings: Settings, app_db: Database, world: World
) -> None:
    limited = settings.model_copy(update={"api_key_failures_per_minute": 3})
    _, good = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    garbage = bearer("shf_test_ZZZZZZZZ_" + "a" * 43)
    with client_for(limited, client=("203.0.113.7", 5000)) as c:
        assert c.get(WHOAMI, headers=bearer(good)).status_code == 200  # successes are not counted
        for _ in range(3):
            assert c.get(WHOAMI, headers=garbage).status_code == 401
        for headers in (garbage, bearer(good)):  # a full window refuses even a correct key
            r = c.get(WHOAMI, headers=headers)
            assert r.status_code == 429 and r.json()["code"] == "rate_limited"
        app = c.app
    with TestClient(
        app, base_url="https://testserver", client=("2001:db8:1:2::9", 5000)
    ) as other:  # another source is not affected
        assert other.get(WHOAMI, headers=bearer(good)).status_code == 200


# ------------------------------------------------------------------ Item 5c: audited rejections
def test_a_rejected_credential_is_audited_where_its_client_is_proven(
    auth_client: TestClient,
    app_db: Database,
    settings: Settings,
    world: World,
    owner_session: Session,
) -> None:
    key_id, full = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    auth_client.get(WHOAMI, headers=bearer("shf_test_ZZZZZZZZ_" + "a" * 43))  # unknown
    auth_client.get(WHOAMI, headers=bearer(full[:-1] + ("A" if full[-1] != "A" else "B")))
    owner_session.execute(
        text("UPDATE shifde.api_keys SET revoked_at = now() WHERE id = :i"), {"i": key_id}
    )
    owner_session.commit()
    auth_client.get(WHOAMI, headers=bearer(full))
    rows = _audit(owner_session)
    assert [(r.tenant_id, r.target_id, r.reason, r.actor_id) for r in rows] == [
        (None, None, "invalid", None),  # no client proven: installation level
        (None, None, "invalid", None),  # wrong secret: A is not proven either
        (world.a.tenant, str(key_id), "revoked", None),  # secret verified: A's own log
    ]
    dumped = str(owner_session.execute(text("SELECT details FROM shifde.audit_events")).all())
    assert full not in dumped and full[-43:] not in dumped
    s = app_session(app_db, world.b.tenant)
    try:
        seen = s.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = :a"),
            {"a": apikeys.REJECTED_ACTION},
        ).scalar_one()
        assert seen == 0  # B sees neither A's row nor the installation rows
    finally:
        s.close()
