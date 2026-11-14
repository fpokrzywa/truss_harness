"""OIDC fixes of identity part 3a (DECISIONS B-922..B-927), each failing first on SETH 3ba0e06's
code or schema: PKCE from `Settings.pkce_key` (findings Item 4), the callback limiter (Item 5b),
identities only through the definers (review 1 item 2), installation IdPs name only installation
secrets (review 1 item 7), single-use expiring flow state in the definers, a client's IdP links
only that client's members, and IdP group sync refused until part 3b.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import repository
from shifde.identity.oidc import pkce_challenge, pkce_verifier
from tests.identity import oidc_flow
from tests.identity.conftest import World, app_session, owner_membership, owner_user


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def test_the_pkce_verifier_comes_from_the_pkce_key_not_the_pepper(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_oidc_idp(owner_session, None, oidc_flow.sso("install"))
    owner_session.commit()
    fake = oidc_flow.provider()
    with oidc_flow.oidc_client(settings, fake) as c:
        auth = oidc_flow.start(c, fake, idp)
    assert settings.pkce_key != settings.pepper
    assert auth.challenge == pkce_challenge(pkce_verifier(settings.pkce_key, auth.state))
    assert auth.challenge != pkce_challenge(pkce_verifier(settings.pepper, auth.state))


def test_oidc_starts_and_callbacks_are_limited_per_source(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_oidc_idp(owner_session, None, oidc_flow.sso("install"))
    owner_session.commit()
    fake = oidc_flow.provider()
    limited = settings.model_copy(update={"oidc_rate_per_minute": 3})
    with oidc_flow.oidc_client(limited, fake) as c:
        for _ in range(3):
            r = oidc_flow.finish(c, idp, "code", "forged-state")
            assert r.headers["location"] == oidc_flow.FAIL
        refused = oidc_flow.finish(c, idp, "code", "forged-state")
        assert refused.status_code == 429
        oidc_flow.start(c, fake, idp)  # starts count on their own key
    rows = oidc_flow.failures(owner_session)
    assert len(rows) == 3  # the refused callback did no work and wrote nothing


def test_identities_are_inserted_only_by_the_definers(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Review 1 item 2: nobody binds a subject at another client's (or the installation) IdP,
    not even for themself; the JIT definer provisions only through an installation IdP with
    JIT on, for an unbound subject and a new e-mail address."""
    jit_idp = oidc_flow.owner_oidc_idp(owner_session, None, "https://jit.test", jit="create_user")
    me = owner_user(owner_session, "me@a.test")
    owner_session.commit()
    for tenant, idp in ((world.a.tenant, world.b.idp), (None, world.installation_idp)):
        with app_session(app_db, tenant, me) as s:
            with pytest.raises(DBAPIError, match="row-level security|row level security"):
                s.execute(
                    text(
                        "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, "
                        "idp_config_id) VALUES (:id, :u, 'https://idp.b.test', 'b', :c)"
                    ),
                    {"id": uuid7(), "u": me, "c": idp},
                )
            s.rollback()

    def jit(idp: UUID, issuer: str, subject: str, email: str) -> bool:
        with app_session(app_db, None) as s:
            ok = repository.jit_create_user(
                s,
                user_id=uuid7(),
                idp_id=idp,
                issuer=issuer,
                subject=subject,
                email=email,
                display_name="New",
            )
            s.commit()
            return ok

    assert not jit(world.b.idp, "https://idp.b.test", "fresh", "fresh@b.test")  # client IdP
    assert not jit(world.installation_idp, "https://login.shi.test", "fresh", "x@shi.test")
    assert not jit(jit_idp, "https://other.test", "fresh", "fresh@shi.test")  # wrong issuer
    assert jit(jit_idp, "https://jit.test", "fresh", "fresh@shi.test")
    assert not jit(jit_idp, "https://jit.test", "fresh", "again@shi.test")  # subject bound
    assert not jit(jit_idp, "https://jit.test", "other", "me@a.test")  # existing e-mail
    rows = owner_session.execute(
        text("SELECT subject FROM shifde.user_identities WHERE idp_config_id = :i"),
        {"i": jit_idp},
    ).scalars()
    assert list(rows) == ["fresh"]


def _secret(owner: Session, tenant: UUID | None) -> UUID:
    sid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (:id, :t, :n, 'env', 'SHIFDE_UNUSED')"
        ),
        {"id": sid, "t": tenant, "n": f"s-{sid}"},
    )
    return sid


def test_an_installation_idp_names_only_an_installation_secret(
    world: World, owner_session: Session
) -> None:
    """Review 1 item 7, even as the schema owner."""
    a_secret, own = _secret(owner_session, world.a.tenant), _secret(owner_session, None)
    owner_session.commit()
    refused = "identity provider configuration refused"
    with pytest.raises(DBAPIError, match=refused):
        owner_session.execute(
            text("UPDATE shifde.idp_configs SET client_secret_ref_id = :s WHERE id = :i"),
            {"s": a_secret, "i": world.installation_idp},
        )
    owner_session.rollback()
    with pytest.raises(DBAPIError, match=refused):
        owner_session.execute(
            text(
                "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, "
                "discovery_url, client_id, client_secret_ref_id) VALUES (:id, NULL, 'x', "
                "'https://x.test', 'https://x.test/d', 'c', :s)"
            ),
            {"id": uuid7(), "s": a_secret},
        )
    owner_session.rollback()
    owner_session.execute(
        text("UPDATE shifde.idp_configs SET client_secret_ref_id = :s WHERE id = :i"),
        {"s": own, "i": world.installation_idp},
    )
    owner_session.commit()


def test_flow_state_is_single_use_bounded_and_bound_to_its_idp(
    world: World, app_db: Database, owner_session: Session
) -> None:
    disabled = oidc_flow.owner_oidc_idp(owner_session, None, "https://off.test", status="disabled")
    owner_session.commit()
    state, other = _hash("state"), _hash("other")

    def begin(idp: UUID, state_hash: bytes, ttl: int = 600) -> bool:
        with app_session(app_db, None) as s:
            ok = repository.begin_oidc_request(
                s,
                request_id=uuid7(),
                idp_id=idp,
                state_hash=state_hash,
                binding_hash=_hash("binding"),
                nonce_hash=_hash("nonce"),
                next_path="/",
                ttl_seconds=ttl,
            )
            s.commit()
            return ok

    assert not begin(disabled, state)
    assert not begin(world.b.idp, b"short", 600)
    assert not begin(world.b.idp, state, 0) and not begin(world.b.idp, state, 86400)
    assert begin(world.b.idp, state) and begin(world.b.idp, other)
    owner_session.execute(
        text("UPDATE shifde.oidc_auth_requests SET expires_at = now() WHERE state_hash = :h"),
        {"h": other},
    )
    owner_session.commit()
    with app_session(app_db, world.a.tenant) as s:
        assert repository.consume_oidc_request(s, state, world.a.idp) is None  # not its IdP
        got = repository.consume_oidc_request(s, state, world.b.idp)
        assert got is not None and got.tenant_id == world.b.tenant
        assert repository.consume_oidc_request(s, state, world.b.idp) is None  # single use
        assert repository.consume_oidc_request(s, other, world.b.idp) is None  # expired
        s.commit()


def _arm_link(owner: Session, user: UUID, tenant: UUID | None) -> None:
    owner.execute(
        text(
            "INSERT INTO shifde.setup_tokens (id, user_id, token_hash, purpose, expires_at, "
            "tenant_id, link_pending_until) VALUES (gen_random_uuid(), :u, :h, 'invite', "
            "now() + interval '1 hour', :t, now() + interval '10 minutes')"
        ),
        {"u": user, "h": _hash(f"link-{user}"), "t": tenant},
    )


def test_a_link_armed_in_one_client_completes_only_through_its_own_or_the_installation_idp(
    world: World, app_db: Database, owner_session: Session
) -> None:
    a_iss, b_iss = oidc_flow.sso("a"), oidc_flow.sso("b")
    a_idp = oidc_flow.owner_oidc_idp(owner_session, world.a.tenant, a_iss)
    b_idp = oidc_flow.owner_oidc_idp(owner_session, world.b.tenant, b_iss)
    newcomer = owner_user(owner_session, "newcomer@a.test")
    owner_membership(owner_session, world.a.tenant, newcomer)
    admin = owner_user(owner_session, "root@shi.test", admin=True)
    owner_membership(owner_session, world.a.tenant, admin)
    _arm_link(owner_session, newcomer, world.a.tenant)
    _arm_link(owner_session, admin, world.a.tenant)
    owner_session.commit()

    def resolve(issuer: str, subject: str, email: str, idp: UUID) -> object:
        with app_session(app_db, None) as s:
            row = repository.resolve_identity(s, issuer, subject, email, idp)
            s.commit()
            return row

    assert resolve(b_iss, "n-1", "newcomer@a.test", b_idp) is None  # B's IdP: not A's link
    assert resolve(a_iss, "root", "root@shi.test", a_idp) is None  # never the administrator
    linked = resolve(a_iss, "n-1", "newcomer@a.test", a_idp)
    assert linked is not None and getattr(linked, "user_id", None) == newcomer
    assert resolve(a_iss, "n-2", "newcomer@a.test", a_idp) is None  # the link is consumed
    rows = owner_session.execute(
        text("SELECT user_id, idp_config_id FROM shifde.user_identities WHERE subject LIKE 'n-%'")
    ).all()
    assert rows == [(newcomer, a_idp)]


def test_an_idp_that_syncs_groups_signs_nobody_in_until_part_3b(
    settings: Settings, world: World, owner_session: Session
) -> None:
    iss = oidc_flow.sso("a")
    idp = oidc_flow.owner_oidc_idp(owner_session, world.a.tenant, iss, group_sync_mode="sync")
    oidc_flow.owner_identity(owner_session, world.a.member, idp, iss, "a")
    owner_session.commit()
    fake = oidc_flow.provider()
    with oidc_flow.oidc_client(settings, fake) as c:
        r = c.get(f"/api/auth/oidc/{idp}/login", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == oidc_flow.FAIL
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.sessions WHERE user_id = :u"), {"u": world.a.member}
        ).scalar_one()
        == 1
    )  # the seeded one
