"""OIDC authorization-code + PKCE flow against an in-process test OpenID provider (no network).

SETH 3ba0e06 `tests/test_p2_oidc.py`, the tests that need only identity and lower modules,
unchanged in assertion. Setup differs where SETH used later modules: IdPs are inserted as the
schema owner (SETH: the admin IdP routes, ported with admin), the app is
`tests/identity/harness.py` (SETH: `create_app`), `world` seeds SETH's four local accounts
without tenancy, and `service.my_memberships` is the B-920 stub. The fake IdP is
`tests/_support/fake_oidc.py` (SETH `p2util.FakeOidcProvider`). Deferred with carry_forward rows:
the tenant-IdP pinning test (tenancy org routes; its isolation is covered by
`test_oidc_cross_client.py`), the two group-sync tests (they use a client's IdP and admin's
group and binding routes; their behaviour on platform groups is pinned in
`test_platform_groups.py`, the route parts move with admin) and the IdP configuration API tests
(admin).
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.fake_oidc import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    FakeOidcProvider,
    resolver,
)
from tests._support.tenants import add_tenants
from tests.identity import harness
from tests.identity.conftest import owner_membership

DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
FAIL = "/login?error=oidc_failed"
ORIGIN = "http://127.0.0.1:3710"
SECRET_ENV = "SHIFDE_TEST_OIDC_CLIENT_SECRET"


@dataclass
class World:
    owner: Session
    tenants: dict[str, UUID]


@pytest.fixture
def world(owner_session: Session) -> World:
    """SETH's seed shape without tenancy: four people with a local password (the platform
    administrator, two in demo-a, one in demo-b) and no identities."""
    tenants = {"demo-a": uuid7(), "demo-b": uuid7()}
    add_tenants(owner_session, *tenants.values())
    harness.person(owner_session, "platform@shifde.local", admin=True)
    for email, slug in (
        ("admin@demo-a.local", "demo-a"),
        ("viewer@demo-a.local", "demo-a"),
        ("admin@demo-b.local", "demo-b"),
    ):
        owner_membership(owner_session, tenants[slug], harness.person(owner_session, email))
    owner_session.commit()
    return World(owner_session, tenants)


def create_idp(c: TestClient, world: World, *, tenant: str | None = None, **overrides: Any) -> str:
    """An active IdP with a client secret, inserted as the owner (SETH: the admin routes)."""
    owner = world.owner
    tenant_id = None if tenant is None else world.tenants[tenant]
    secret = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (:id, :t, :n, 'env', :ref)"
        ),
        {"id": secret, "t": tenant_id, "n": f"idp-{secret}", "ref": SECRET_ENV},
    )
    values = {
        "display_name": "Test IdP",
        "issuer": ISSUER,
        "discovery_url": DISCOVERY,
        "client_id": CLIENT_ID,
        "jit": "create_user" if tenant is None else "off",  # tenant IdPs never provision
        **overrides,
    }
    idp = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id, client_secret_ref_id, jit) VALUES (:id, :t, :display_name, :issuer, "
            ":discovery_url, :client_id, :s, :jit)"
        ),
        {"id": idp, "t": tenant_id, "s": secret, **values},
    )
    owner.commit()
    c.cookies.clear()
    return str(idp)


def owner_invite(owner: Session, user_id: Any, tenant: Any = None, minutes: int = 60) -> str:
    """Insert an invitation setup token as the DB owner (tests only); returns the plaintext."""
    token = _secrets.token_urlsafe(32)
    owner.execute(
        text(
            "INSERT INTO shifde.setup_tokens (id, user_id, token_hash, purpose, expires_at, "
            "tenant_id) VALUES (gen_random_uuid(), :u, :h, 'invite', "
            "now() + make_interval(mins => :m), :t)"
        ),
        {"u": user_id, "h": hashlib.sha256(token.encode()).digest(), "m": minutes, "t": tenant},
    )
    owner.commit()
    return token


@pytest.fixture
def provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest.fixture
def env(
    settings: Settings,
    world: World,
    provider: FakeOidcProvider,
    stub_memberships: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """App wired to the fake IdP (no network); JWKS refresh is unthrottled."""
    monkeypatch.setenv(SECRET_ENV, CLIENT_SECRET)
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    with harness.client_for(s, http_transport=provider.transport(), resolver=resolver) as c:
        yield c


@pytest.fixture
def client(settings: Settings, world: World, stub_memberships: None) -> Iterator[TestClient]:
    with harness.client_for(settings) as c:
        yield c


def start(c: TestClient, provider: FakeOidcProvider, idp_id: str, next_path: str | None = None):
    params = {"next": next_path} if next_path else {}
    r = c.get(f"/api/auth/oidc/{idp_id}/login", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{ISSUER}/authorize?")
    return provider.parse_authorization(r.headers["location"])


def finish(c: TestClient, idp_id: str, code: str, state: str):
    return c.get(
        f"/api/auth/oidc/{idp_id}/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def ok_login(c: TestClient, provider: FakeOidcProvider, idp_id: str, **claims: Any):
    auth = start(c, provider, idp_id)
    code = provider.issue_code(auth, provider.claims_for(auth, **claims))
    return auth, finish(c, idp_id, code, auth.state)


def failure_reasons(owner: Session) -> list[str]:
    rows = owner.execute(
        text(
            "SELECT details->>'reason' FROM shifde.audit_events "
            "WHERE action = 'identity.login.failed' ORDER BY id"
        )
    )
    owner.rollback()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- happy path


def test_login_redirect_carries_state_nonce_and_s256_challenge(
    env: TestClient, world: World, provider: FakeOidcProvider
) -> None:
    idp = create_idp(env, world)
    r = env.get(f"/api/auth/oidc/{idp}/login?next=/o/demo-a/projects", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == [CLIENT_ID] and q["scope"][0].split()[0] == "openid"
    assert (
        len(q["state"][0]) >= 40 and len(q["nonce"][0]) >= 40 and len(q["code_challenge"][0]) == 43
    )
    assert q["redirect_uri"] == [f"http://127.0.0.1:3710/api/auth/oidc/{idp}/callback"]
    assert (
        "__Host-shifde_oidc_bind" in r.headers["set-cookie"]
        and "HttpOnly" in r.headers["set-cookie"]
    )


def test_full_login_creates_user_session_and_audit(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp, "/o/demo-a/projects")
    code = provider.issue_code(auth, provider.claims_for(auth))
    r = finish(env, idp, code, auth.state)
    assert r.status_code == 302 and r.headers["location"] == "/o/demo-a/projects"
    session = env.get("/api/auth/session")
    assert session.status_code == 200 and session.json()["user"]["email"] == "newuser@example.com"
    assert provider.token_calls[-1]["client_secret"] == CLIENT_SECRET  # resolved server-side
    ident = owner_session.execute(text("SELECT issuer, subject FROM shifde.user_identities")).one()
    assert tuple(ident) == (ISSUER, "sub-123")
    ev = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events "
            "WHERE action = 'identity.login.succeeded' AND auth_method = 'oidc'"
        )
    ).scalar_one()
    assert ev == 1
    # a second login maps by (iss, sub), not by email: an email change does not create a user
    env.cookies.clear()
    ok_login(env, provider, idp, email="renamed@example.com")
    assert env.get("/api/auth/session").json()["user"]["email"] == "newuser@example.com"
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.users WHERE primary_email LIKE '%example.com'")
        ).scalar_one()
        == 1
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://evil.example/x", "/"),
        ("//evil.example", "/"),
        ("/ok/path?a=1", "/ok/path?a=1"),
        ("javascript:1", "/"),
    ],
)
def test_next_must_be_a_same_origin_relative_path(
    env: TestClient, world: World, provider: FakeOidcProvider, given: str, expected: str
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp, given)
    code = provider.issue_code(auth, provider.claims_for(auth))
    assert finish(env, idp, code, auth.state).headers["location"] == expected


# ---------------------------------------------------------------- token validation failures
def _expect_failure(
    env: TestClient,
    provider: FakeOidcProvider,
    idp: str,
    claims_or_raw: Any,
    owner: Session,
    reason: str,
) -> None:
    auth = start(env, provider, idp)
    claims = claims_or_raw(auth) if callable(claims_or_raw) else claims_or_raw
    code = provider.issue_code(auth, claims)
    r = finish(env, idp, code, auth.state)
    assert r.status_code == 302 and r.headers["location"] == FAIL
    assert env.get("/api/auth/session").status_code == 401  # no session was created
    assert failure_reasons(owner)[-1] == reason


def test_bad_signature_is_rejected(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    other = RSAKey.generate_key(2048, parameters={"kid": provider.kid})  # same kid, wrong key
    _expect_failure(
        env,
        provider,
        idp,
        lambda a: provider.mint(provider.claims_for(a), key=other),
        owner_session,
        "token_signature_invalid",
    )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"aud": "someone-else"}, "token_audience_mismatch"),
        ({"iss": "https://other-idp.test"}, "token_issuer_mismatch"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "token_expired"),
        ({"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200}, "token_iat_in_future"),
        ({"nbf": int(time.time()) + 3600}, "token_not_yet_valid"),
        ({"nonce": "not-the-nonce"}, "token_nonce_mismatch"),
        ({"nonce": None}, "token_nonce_mismatch"),
        ({"sub": ""}, "token_sub_invalid"),
        ({"aud": [CLIENT_ID, "x"]}, "token_azp_mismatch"),
    ],
)
def test_claim_validation_failures(
    env: TestClient,
    world: World,
    provider: FakeOidcProvider,
    owner_session: Session,
    override: dict[str, Any],
    reason: str,
) -> None:
    idp = create_idp(env, world)
    _expect_failure(
        env, provider, idp, lambda a: provider.claims_for(a, **override), owner_session, reason
    )


def test_expiry_within_sixty_seconds_skew_is_tolerated(
    env: TestClient, world: World, provider: FakeOidcProvider
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    now = int(time.time())
    code = provider.issue_code(auth, provider.claims_for(auth, exp=now - 30, iat=now - 90))
    assert finish(env, idp, code, auth.state).headers["location"] == "/"


def test_none_and_hmac_algorithms_are_rejected(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    import base64
    import json

    idp = create_idp(env, world)

    def forged(a: Any, alg: str) -> str:
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
        return f"{enc({'alg': alg, 'kid': provider.kid})}.{enc(provider.claims_for(a))}."

    _expect_failure(
        env,
        provider,
        idp,
        lambda a: {"__raw__": forged(a, "none")},
        owner_session,
        "token_alg_rejected",
    )
    _expect_failure(
        env,
        provider,
        idp,
        lambda a: {"__raw__": forged(a, "HS256") + "AAAA"},
        owner_session,
        "token_alg_rejected",
    )


def test_nonce_replay_and_state_reuse_are_rejected(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth, first = ok_login(env, provider, idp)
    assert first.headers["location"] == "/"
    env.cookies.clear()
    # replay the very same callback (same state) -> the single-use state is gone
    code = provider.issue_code(auth, provider.claims_for(auth), code="again")
    r = finish(env, idp, code, auth.state)
    assert (
        r.headers["location"] == FAIL
        and failure_reasons(owner_session)[-1] == "state_invalid_or_replayed"
    )
    # a token carrying the old nonce under a fresh state fails the nonce binding
    auth2 = start(env, provider, idp)
    stale = provider.claims_for(auth, nonce=auth.nonce)
    code2 = provider.issue_code(auth2, stale, code="stale")
    assert finish(env, idp, code2, auth2.state).headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "token_nonce_mismatch"


def test_state_mismatch_and_missing_binding_cookie(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    code = provider.issue_code(auth, provider.claims_for(auth))
    assert finish(env, idp, code, "forged-state-value").headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "state_invalid_or_replayed"
    # login-CSRF: the right state but a browser that never started the flow (no binding cookie)
    env.cookies.clear()
    assert finish(env, idp, code, auth.state).headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "binding_mismatch"
    for params in (
        {},
        {"code": "x"},
        {"state": "x"},
        {"error": "access_denied", "state": "x", "code": "y"},
    ):
        r = env.get(f"/api/auth/oidc/{idp}/callback", params=params, follow_redirects=False)
        assert r.headers["location"] == FAIL
    assert (
        env.get(
            "/api/auth/oidc/not-a-uuid/callback?code=a&state=b", follow_redirects=False
        ).headers["location"]
        == FAIL
    )


def test_expired_state_is_rejected(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    owner_session.execute(
        text("UPDATE shifde.oidc_auth_requests SET expires_at = now() - interval '1 second'")
    )
    owner_session.commit()
    code = provider.issue_code(auth, provider.claims_for(auth))
    assert finish(env, idp, code, auth.state).headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "state_invalid_or_replayed"


def test_state_nonce_are_stored_only_as_hashes(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    row = owner_session.execute(
        text("SELECT to_jsonb(r) FROM shifde.oidc_auth_requests r")
    ).scalar_one()
    blob = str(row)
    assert auth.state not in blob and auth.nonce not in blob
    assert len(bytes.fromhex(row["state_hash"][2:])) == 32


def test_unknown_kid_triggers_a_jwks_refresh(
    env: TestClient, world: World, provider: FakeOidcProvider
) -> None:
    idp = create_idp(env, world)
    ok_login(env, provider, idp)  # primes the JWKS cache with key-1
    env.cookies.clear()
    calls = provider.jwks_calls
    provider.rotate_key("key-2")  # the IdP rotated; the cache only knows key-1
    _, r = ok_login(env, provider, idp, sub="sub-123")
    assert r.headers["location"] == "/"
    assert provider.jwks_calls == calls + 1  # exactly one refresh on the kid miss


def test_unknown_kid_that_stays_unknown_is_rejected(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    rogue = RSAKey.generate_key(2048, parameters={"kid": "rogue"})
    code = provider.issue_code(
        auth, provider.mint(provider.claims_for(auth), key=rogue, kid="rogue")
    )
    assert finish(env, idp, code, auth.state).headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "token_signature_invalid"


def test_discovery_issuer_mismatch_and_wrong_pkce_verifier_fail(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    auth = start(env, provider, idp)
    auth.challenge = "x" * 43  # the provider will see a verifier that does not match
    code = provider.issue_code(auth, provider.claims_for(auth))
    assert finish(env, idp, code, auth.state).headers["location"].startswith("/login")
    assert failure_reasons(owner_session)[-1].startswith("token_exchange_failed")
    provider.discovery_issuer = "https://impostor.test"
    env.app.state.oidc._discovery.clear()  # force a fresh discovery fetch
    other = create_idp(env, world, display_name="Second", issuer="https://second.test")
    assert (
        env.get(f"/api/auth/oidc/{other}/login", follow_redirects=False).headers["location"] == FAIL
    )


# ---------------------------------------------------------------- JIT, identity mapping, groups
def test_jit_off_rejects_unknown_identities(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world, jit="off")
    _expect_failure(
        env,
        provider,
        idp,
        lambda a: provider.claims_for(a),
        owner_session,
        "unknown_identity_jit_off",
    )


def test_jit_requires_verified_email_and_refuses_existing_accounts(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world)
    _expect_failure(
        env,
        provider,
        idp,
        lambda a: provider.claims_for(a, email_verified=False),
        owner_session,
        "jit_requires_verified_email",
    )
    # an existing local account is never auto-linked by e-mail (takeover protection)
    _expect_failure(
        env,
        provider,
        idp,
        lambda a: provider.claims_for(a, email="viewer@demo-a.local"),
        owner_session,
        "jit_email_already_registered",
    )
    assert (
        owner_session.execute(text("SELECT count(*) FROM shifde.user_identities")).scalar_one() == 0
    )


# ---------------------------------------------------------------- setup-token link
def test_setup_token_links_the_next_verified_oidc_login_when_local_auth_is_off(
    settings: Settings,
    world: World,
    provider: FakeOidcProvider,
    owner_session: Session,
    stub_memberships: None,
) -> None:
    from shifde.identity import service as identity

    s = settings.model_copy(
        update={"auth_local_enabled": False, "oidc_jwks_min_refresh_seconds": 0}
    )
    uid = identity.create_user(owner_session, email="invitee@example.com", display_name="Invitee")
    token = owner_invite(owner_session, uid)
    provider.require_secret = False  # this IdP row is inserted without a client secret
    with harness.client_for(s, http_transport=provider.transport(), resolver=resolver) as c:
        # with local auth off a password is refused, and the token arms OIDC linking
        assert (
            c.post(
                "/api/auth/setup/redeem", json={"token": token, "password": "a-long-password-1"}
            ).status_code
            == 403
        )
        arm = c.post("/api/auth/setup/redeem", json={"token": token})
        assert arm.status_code == 200 and arm.json() == {"status": "pending_oidc_link"}
        assert (
            c.post("/api/auth/setup/redeem", json={"token": token}).status_code == 401
        )  # single use
        owner_session.execute(
            text(
                "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, client_id) VALUES (gen_random_uuid(), NULL, 'x', :i, :d, :c)"
            ),
            {"i": ISSUER, "d": DISCOVERY, "c": CLIENT_ID},
        )
        owner_session.commit()
        idp = str(owner_session.execute(text("SELECT id FROM shifde.idp_configs")).scalar_one())
        # unverified e-mail must not link
        auth = start(c, provider, idp)
        code = provider.issue_code(
            auth,
            provider.claims_for(auth, email="invitee@example.com", email_verified=False, sub="u-1"),
        )
        assert finish(c, idp, code, auth.state).headers["location"] == FAIL
        c.cookies.clear()
        auth = start(c, provider, idp)
        code = provider.issue_code(
            auth, provider.claims_for(auth, email="invitee@example.com", sub="u-1"), code="verified"
        )
        done = finish(c, idp, code, auth.state)
        assert done.headers["location"] == "/"
        assert c.get("/api/auth/session").json()["user"]["email"] == "invitee@example.com"
        linked = owner_session.execute(
            text("SELECT subject FROM shifde.user_identities WHERE user_id = :u"), {"u": uid}
        ).scalar_one()
        assert linked == "u-1"
        # the link is consumed: a different subject with the same e-mail cannot take over
        c.cookies.clear()
        auth = start(c, provider, idp)
        code = provider.issue_code(
            auth, provider.claims_for(auth, email="invitee@example.com", sub="u-2"), code="second"
        )
        assert finish(c, idp, code, auth.state).headers["location"] == FAIL


# ---------------------------------------------------------------- setup tokens
def _token(owner: Session, email: str = "tok@example.com", minutes: int = 60) -> str:
    from shifde.identity import service as identity

    uid = identity.create_user(owner, email=email, display_name="Tok")
    return owner_invite(owner, uid, minutes=minutes)


def test_setup_token_sets_password_opens_session_and_is_single_use(
    client: TestClient, world: World, owner_session: Session
) -> None:
    token = _token(owner_session)
    assert (
        client.post(
            "/api/auth/setup/redeem", json={"token": token, "password": "short"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/auth/setup/redeem", json={"token": token}).status_code == 422
    )  # password required
    ok = client.post(
        "/api/auth/setup/redeem", json={"token": token, "password": "a-long-password-1"}
    )
    assert (
        ok.status_code == 200
        and ok.json()["user"]["email"] == "tok@example.com"
        and ok.json()["csrf_token"]
    )
    assert client.cookies.get("__Host-shifde_session")
    again = client.post(
        "/api/auth/setup/redeem", json={"token": token, "password": "another-password-2"}
    )
    assert again.status_code == 401 and again.json()["code"] == "invalid_credentials"
    client.cookies.clear()
    login_ok = client.post(
        "/api/auth/local/login", json={"email": "tok@example.com", "password": "a-long-password-1"}
    )
    assert login_ok.status_code == 200


def test_expired_unknown_and_garbage_setup_tokens_are_rejected(
    client: TestClient, world: World, owner_session: Session
) -> None:
    expired = _token(owner_session, "old@example.com", minutes=-5)
    for tok in (expired, "x" * 43, "short", "A" * 400):
        r = client.post(
            "/api/auth/setup/redeem", json={"token": tok, "password": "a-long-password-1"}
        )
        assert r.status_code in (401, 422), tok
    r = client.post(
        "/api/auth/setup/redeem", json={"token": expired, "password": "a-long-password-1"}
    )
    assert r.status_code == 401
    rows = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events WHERE action = 'identity.setup_token.rejected'"
        )
    ).scalar_one()
    assert rows >= 2
    assert (
        owner_session.execute(text("SELECT count(*) FROM shifde.local_credentials")).scalar_one()
        == 4
    )  # seed users only
