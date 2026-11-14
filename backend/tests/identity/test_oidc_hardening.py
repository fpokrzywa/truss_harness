"""OIDC fixes of identity part 3a (DECISIONS B-922..B-927), each failing first on SETH 3ba0e06's
code or schema: PKCE from `Settings.pkce_key` (findings Item 4), the callback limiter (Item 5b),
identities only through the definers (review 1 item 2), installation IdPs name only installation
secrets (review 1 item 7), single-use expiring flow state in the definers, a client's IdP links
only that client's members, and IdP group sync refused for a client's IdP (identity_0005: only
installation IdPs sync platform groups, B-931). Identity review 4 items 1, 2, 6 (part 3b): the
link paths and refusals pinned, SETH's client-IdP refusal test moved, unknown IdPs audited.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import repository
from shifde.identity.oidc import OidcFailure, _require_supported, pkce_challenge, pkce_verifier
from shifde.identity.repository import ResolvedIdp
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


def test_a_clients_idp_never_syncs_groups(world: World, owner_session: Session) -> None:
    """Replaces part 3a's `test_an_idp_that_syncs_groups_signs_nobody_in_until_part_3b`: groups
    are platform-level and hold SHI staff, so a client's IdP with sync on cannot exist
    (`ck_idp_configs__group_sync_installation_only`, B-931), and `identity.oidc` still refuses
    such a row (B-925); an installation IdP may sync (tests/identity/test_platform_groups.py)."""
    iss = oidc_flow.sso("a")
    with (
        pytest.raises(DBAPIError, match="ck_idp_configs__group_sync_installation_only"),
        owner_session.begin_nested(),
    ):
        oidc_flow.owner_oidc_idp(owner_session, world.a.tenant, iss, group_sync_mode="sync")
    row = ResolvedIdp(
        uuid7(), world.a.tenant, "a", iss, f"{iss}/x", "c", None, [], {}, "off", "sync"
    )
    with pytest.raises(OidcFailure) as refused:
        _require_supported(row)
    assert refused.value.reason == "group_sync_unavailable"
    _require_supported(replace(row, tenant_id=None))


def test_link_paths_installation_idp_and_refusals_at_a_clients_idp(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Review 4 item 1: a link armed in A completes through the installation IdP; at A's IdP a
    link armed with no client, a suspended member's link and a link armed in A for a person
    who is a member of B only are refused, and no identity is written for them."""
    a, b = world.a.tenant, world.b.tenant
    inst_iss, a_iss = oidc_flow.sso("inst"), oidc_flow.sso("a")
    inst = oidc_flow.owner_oidc_idp(owner_session, None, inst_iss)
    a_idp = oidc_flow.owner_oidc_idp(owner_session, a, a_iss)
    via_inst = owner_user(owner_session, "via-inst@a.test")
    owner_membership(owner_session, a, via_inst)
    no_client = owner_user(owner_session, "no-client@shi.test")
    suspended = owner_user(owner_session, "suspended@a.test")
    owner_membership(owner_session, a, suspended)
    owner_session.execute(
        text("UPDATE shifde.memberships SET status = 'suspended' WHERE user_id = :u"),
        {"u": suspended},
    )
    b_only = owner_user(owner_session, "b-only@b.test")
    owner_membership(owner_session, b, b_only)
    _arm_link(owner_session, via_inst, a)
    _arm_link(owner_session, no_client, None)
    _arm_link(owner_session, suspended, a)
    _arm_link(owner_session, b_only, a)
    owner_session.commit()

    def resolve(issuer: str, subject: str, email: str, idp: UUID) -> object:
        with app_session(app_db, None) as s:
            row = repository.resolve_identity(s, issuer, subject, email, idp)
            s.commit()
            return row

    assert resolve(a_iss, "x-1", "no-client@shi.test", a_idp) is None
    assert resolve(a_iss, "x-2", "suspended@a.test", a_idp) is None
    assert resolve(a_iss, "x-3", "b-only@b.test", a_idp) is None
    linked = resolve(inst_iss, "x-4", "via-inst@a.test", inst)
    assert linked is not None and getattr(linked, "user_id", None) == via_inst
    rows = owner_session.execute(
        text("SELECT user_id, idp_config_id FROM shifde.user_identities WHERE subject LIKE 'x-%'")
    ).all()
    assert rows == [(via_inst, inst)]


def test_tenant_idp_refuses_platform_admins_non_members_and_suspended_members(
    settings: Settings, world: World, owner_session: Session, stub_memberships: None
) -> None:
    """SETH `test_p2_review.py` (review 4 item 2), moved: the IdP is owner-inserted instead of
    created through the admin route, and identities are owner-inserted instead of prelinked;
    the assertions are SETH's (failure reason, no session, three failures filed in A)."""
    a = world.a.tenant
    iss = oidc_flow.sso("a")
    idp = oidc_flow.owner_oidc_idp(owner_session, a, iss)
    root = owner_user(owner_session, "root@example.com", admin=True)
    owner_membership(owner_session, a, root)
    outsider = owner_user(owner_session, "outsider@example.com")
    susp = owner_user(owner_session, "susp@example.com")
    owner_membership(owner_session, a, susp)
    owner_session.execute(
        text("UPDATE shifde.memberships SET status = 'suspended' WHERE user_id = :u"), {"u": susp}
    )
    for person, sub in ((root, "sub-root"), (outsider, "sub-out"), (susp, "sub-susp")):
        oidc_flow.owner_identity(owner_session, person, idp, iss, sub)
    owner_session.commit()
    cases = {
        "sub-root": "tenant_idp_platform_admin_refused",
        "sub-out": "tenant_idp_not_an_active_member",
        "sub-susp": "tenant_idp_not_an_active_member",
    }
    fake = oidc_flow.provider()
    with oidc_flow.oidc_client(settings, fake) as c:
        for sub, reason in cases.items():
            c.cookies.clear()
            r = oidc_flow.sign_in(c, fake, idp, iss, code=sub, sub=sub)
            assert r.headers["location"] == oidc_flow.FAIL, sub
            assert c.get("/api/auth/session").status_code == 401
            assert oidc_flow.failures(owner_session)[-1] == (reason, a)
    n = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events "
            "WHERE action = 'identity.login.failed' AND tenant_id = :t"
        ),
        {"t": a},
    ).scalar_one()
    assert n == 3


def test_a_callback_for_an_unknown_or_disabled_idp_is_audited(
    settings: Settings, world: World, owner_session: Session
) -> None:
    """Review 4 item 6: `complete_login` audits `idp_not_found` (installation-level: nothing
    proves a client) before answering with the generic failure."""
    off = oidc_flow.owner_oidc_idp(owner_session, None, oidc_flow.sso("off"), status="disabled")
    owner_session.commit()
    fake = oidc_flow.provider()
    with oidc_flow.oidc_client(settings, fake) as c:
        for idp in (uuid7(), off):
            r = oidc_flow.finish(c, idp, "code", "forged-state")
            assert r.headers["location"] == oidc_flow.FAIL
    assert oidc_flow.failures(owner_session) == [("idp_not_found", None)] * 2
