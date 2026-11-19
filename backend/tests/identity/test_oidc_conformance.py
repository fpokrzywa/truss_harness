"""REQ F-22 conformance suite against a local test IdP: the whole OIDC sign-in flow through the
real `/api/auth/oidc/{idp}/login` and `/callback` routes (`tests/identity/harness.py` as
`shifde_app`), against the in-process standards-compliant provider in
`tests/_support/fake_oidc.py` (discovery, JWKS, RS256 ID tokens, S256-checking token endpoint).

Each test names the OIDC Core 1.0 or RFC rule it checks: the authorization request, ID token
validation, signing key rotation, state, authorization code replay and PKCE. It restates the
rules as one conformance flow; `test_oidc.py`, `test_oidc_hardening.py` and
`test_oidc_cross_client.py` keep their own cases.

It covers no live IdP: live Entra sign-in stays open, waiting on owner input (sub-project 3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from typing import Any  # Any: JWT claims
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fake_oidc import CLIENT_ID, ISSUER, Authorization, FakeOidcProvider
from tests.identity import oidc_flow

FAIL = oidc_flow.FAIL
UNRESERVED = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")  # RFC 7636 4.1 code_verifier
BASE64URL_43 = re.compile(r"^[A-Za-z0-9\-_]{43}$")


@pytest.fixture
def fake() -> FakeOidcProvider:
    return oidc_flow.provider()


@pytest.fixture
def idp(owner_session: Session) -> UUID:
    """An installation IdP at the fake provider that provisions new people (JIT)."""
    iid = oidc_flow.owner_oidc_idp(owner_session, None, ISSUER, jit="create_user")
    owner_session.commit()
    return iid


@pytest.fixture
def c(settings: Settings, fake: FakeOidcProvider, idp: UUID) -> Iterator[TestClient]:
    with oidc_flow.oidc_client(settings, fake) as client:
        yield client


def login(c: TestClient, fake: FakeOidcProvider, idp: UUID) -> Authorization:
    return oidc_flow.start(c, fake, idp)


def complete(
    c: TestClient,
    fake: FakeOidcProvider,
    idp: UUID,
    token: Callable[[Authorization], str],
    code: str = "c1",
) -> tuple[Authorization, httpx.Response]:
    """One login whose token endpoint answers the ID token `token(auth)`."""
    auth = login(c, fake, idp)
    fake.issue_code(auth, token(auth), code=code)
    return auth, oidc_flow.finish(c, idp, code, auth.state)


def signed(fake: FakeOidcProvider, key: RSAKey, kid: str) -> Callable[[Authorization], str]:
    return lambda auth: fake.mint(fake.claims_for(auth), key=key, kid=kid)


def signs_in(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, key: RSAKey, kid: str, code: str
) -> bool:
    """A login whose ID token is signed with `key` under `kid` lands on `/`."""
    c.cookies.clear()
    return complete(c, fake, idp, signed(fake, key, kid), code)[1].headers["location"] == "/"


def last_reason(owner: Session) -> str:
    return oidc_flow.failures(owner)[-1][0]


def assert_failed(c: TestClient, r: httpx.Response) -> None:
    assert r.status_code == 302 and r.headers["location"] == FAIL
    assert c.get("/api/auth/session").status_code == 401  # no session was opened


def unsigned(alg: str, fake: FakeOidcProvider, auth: Authorization) -> str:
    """A JWT with header `alg` and an empty (or junk) signature: never verified with a key."""

    def enc(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    head = enc({"alg": alg, "kid": fake.kid})
    return f"{head}.{enc(fake.claims_for(auth))}." + ("" if alg == "none" else "AAAA")


# ---------------------------------------------------------------- authorization request


def test_authorization_request_oidc_core_3_1_2_1_and_rfc_7636_4_2(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, settings: Settings
) -> None:
    """OIDC Core 3.1.2.1 (authorization request) and RFC 7636 4.2-4.3 (S256 code challenge):
    the login redirect goes to the discovered `authorization_endpoint` with the code flow's
    parameters, a fresh state and nonce, and an S256 challenge."""
    queries = []
    for _ in range(2):
        r = c.get(f"/api/auth/oidc/{idp}/login", follow_redirects=False)
        assert r.status_code == 302
        url = urlsplit(r.headers["location"])
        assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
        queries.append(parse_qs(url.query))
    for q in queries:
        assert q["response_type"] == ["code"]
        assert q["client_id"] == [CLIENT_ID]
        assert q["redirect_uri"] == [f"{settings.oidc_base_url}/api/auth/oidc/{idp}/callback"]
        assert "openid" in q["scope"][0].split()
        assert q["state"][0] and q["nonce"][0] and q["state"][0] != q["nonce"][0]
        assert BASE64URL_43.match(q["code_challenge"][0])
        assert q["code_challenge_method"] == ["S256"]
    first, second = queries
    assert first["state"] != second["state"] and first["nonce"] != second["nonce"]


# ---------------------------------------------------------------- ID token validation


def test_valid_id_token_signs_in_oidc_core_3_1_3_7(
    c: TestClient, fake: FakeOidcProvider, idp: UUID
) -> None:
    """OIDC Core 3.1.3.7 (ID token validation): an RS256 ID token whose `iss`, `aud`, `exp`,
    `iat` and `nonce` match completes sign-in and opens a session."""
    r = c.get(f"/api/auth/oidc/{idp}/login", params={"next": "/projects"}, follow_redirects=False)
    auth = fake.parse_authorization(r.headers["location"])
    claims = fake.claims_for(auth)
    assert claims["iss"] == ISSUER and claims["aud"] == CLIENT_ID
    assert claims["nonce"] == auth.nonce and claims["iat"] < claims["exp"]
    fake.issue_code(auth, claims)
    done = oidc_flow.finish(c, idp, "c1", auth.state)
    assert done.status_code == 302 and done.headers["location"] == "/projects"
    session = c.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["user"]["email"] == "newuser@example.com"


def _other_key(fake: FakeOidcProvider, auth: Authorization) -> str:
    other = RSAKey.generate_key(2048, parameters={"kid": fake.kid})  # same kid, wrong key
    return fake.mint(fake.claims_for(auth), key=other)


REJECTIONS: list[tuple[str, Callable[[FakeOidcProvider, Authorization], Any], str]] = [
    ("wrong_signature", _other_key, "token_signature_invalid"),
    (
        "wrong_iss",
        lambda f, a: f.claims_for(a, iss="https://other-idp.test"),
        "token_issuer_mismatch",
    ),
    ("wrong_aud", lambda f, a: f.claims_for(a, aud="someone-else"), "token_audience_mismatch"),
    ("wrong_nonce", lambda f, a: f.claims_for(a, nonce="not-the-nonce"), "token_nonce_mismatch"),
    ("missing_nonce", lambda f, a: f.claims_for(a, nonce=None), "token_nonce_mismatch"),
    (
        "expired",
        lambda f, a: f.claims_for(a, exp=int(time.time()) - 120, iat=int(time.time()) - 600),
        "token_expired",
    ),
    ("alg_none", lambda f, a: unsigned("none", f, a), "token_alg_rejected"),
    ("alg_hs256", lambda f, a: unsigned("HS256", f, a), "token_alg_rejected"),
]


@pytest.mark.parametrize(
    ("build", "reason"), [(b, r) for _, b, r in REJECTIONS], ids=[n for n, _, _ in REJECTIONS]
)
def test_id_token_rejected_oidc_core_3_1_3_7_and_rfc_8725_3_1(
    c: TestClient,
    fake: FakeOidcProvider,
    idp: UUID,
    owner_session: Session,
    build: Callable[[FakeOidcProvider, Authorization], Any],
    reason: str,
) -> None:
    """OIDC Core 3.1.3.7 steps 2, 3, 6, 9, 11 (issuer, audience, signature, expiry, nonce) and
    RFC 8725 3.1 (algorithm verification: no `none`, no HMAC with a public key): a token that
    fails one check ends in the failure redirect, no session and the audited reason."""
    auth = login(c, fake, idp)
    fake.issue_code(auth, build(fake, auth))
    assert_failed(c, oidc_flow.finish(c, idp, "c1", auth.state))
    assert last_reason(owner_session) == reason


# ---------------------------------------------------------------- signing key rotation


def test_signing_key_rotation_oidc_core_10_1_1(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, owner_session: Session
) -> None:
    """OIDC Core 10.1.1 (rotation of asymmetric signing keys): the RP refetches the JWKS on an
    unknown `kid`, accepts every published key during an overlap, and rejects a retired key
    once a refresh no longer lists it."""
    # (a) a sign-in with key-1 primes the cache
    assert signs_in(c, fake, idp, fake.key, "key-1", "a")
    assert fake.jwks_calls == 1
    key1 = fake.key

    # (b) overlap: the IdP publishes key-1 and key-2 together
    key2 = RSAKey.generate_key(2048, parameters={"kid": "key-2"})
    fake.extra_keys.append(key2)
    assert signs_in(c, fake, idp, key2, "key-2", "b2")
    assert fake.jwks_calls == 2  # exactly one refresh on the kid miss
    assert signs_in(c, fake, idp, key1, "key-1", "b1")
    assert fake.jwks_calls == 2  # still published and cached: no refetch

    # (c) key-1 and key-2 retired: the IdP publishes only key-3
    fake.rotate_key("key-3")
    fake.extra_keys.clear()
    assert signs_in(c, fake, idp, fake.key, "key-3", "c3")
    assert fake.jwks_calls == 3
    c.cookies.clear()
    _, r = complete(c, fake, idp, signed(fake, key1, "key-1"), code="c1")
    assert_failed(c, r)
    assert fake.jwks_calls == 4  # the refresh no longer lists key-1
    assert last_reason(owner_session) == "token_signature_invalid"


# ---------------------------------------------------------------- state


def test_state_missing_mismatched_or_replayed_rfc_6749_10_12_oidc_core_3_1_2_7(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, owner_session: Session
) -> None:
    """RFC 6749 10.12 (CSRF protection through `state`) and OIDC Core 3.1.2.7 (the RP checks
    the response's `state`): a callback without `state`, with a state the RP never issued, or
    with a state an earlier callback already consumed opens no session."""
    auth = login(c, fake, idp)
    fake.issue_code(auth, fake.claims_for(auth))
    # no state: refused before any token exchange (the route audits missing parameters)
    missing = c.get(f"/api/auth/oidc/{idp}/callback", params={"code": "c1"}, follow_redirects=False)
    assert_failed(c, missing)
    assert last_reason(owner_session) == "idp_error_or_missing_parameters"
    assert fake.token_calls == []
    # a state the RP did not issue
    assert_failed(c, oidc_flow.finish(c, idp, "c1", "forged-state-value"))
    assert last_reason(owner_session) == "state_invalid_or_replayed"
    # a completed callback's state replayed with a fresh code
    auth = login(c, fake, idp)
    fake.issue_code(auth, fake.claims_for(auth), code="first")
    assert oidc_flow.finish(c, idp, "first", auth.state).headers["location"] == "/"
    c.cookies.clear()
    fake.issue_code(auth, fake.claims_for(auth), code="fresh")
    assert_failed(c, oidc_flow.finish(c, idp, "fresh", auth.state))
    assert last_reason(owner_session) == "state_invalid_or_replayed"


# ---------------------------------------------------------------- authorization code replay


def test_authorization_code_replay_is_refused_rfc_6749_4_1_2(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, owner_session: Session
) -> None:
    """RFC 6749 4.1.2 (the authorization code is single use) and 10.5: replaying an exchanged
    code opens no session, with its spent state or with a fresh one (the IdP answers
    `invalid_grant`)."""
    auth, done = complete(c, fake, idp, signed(fake, fake.key, fake.kid), code="once")
    assert done.headers["location"] == "/"
    c.cookies.clear()
    # the whole callback replayed: the state is already consumed
    assert_failed(c, oidc_flow.finish(c, idp, "once", auth.state))
    assert last_reason(owner_session) == "state_invalid_or_replayed"
    # the spent code under a fresh state: the IdP refuses it
    calls = len(fake.token_calls)
    fresh = login(c, fake, idp)
    assert_failed(c, oidc_flow.finish(c, idp, "once", fresh.state))
    assert len(fake.token_calls) == calls + 1 and fake.token_calls[-1]["code"] == "once"
    assert last_reason(owner_session).startswith("token_exchange_failed")


# ---------------------------------------------------------------- PKCE


def test_pkce_verifier_matches_the_s256_challenge_rfc_7636_4_5_4_6(
    c: TestClient, fake: FakeOidcProvider, idp: UUID
) -> None:
    """RFC 7636 4.1 and 4.5-4.6 (code verifier sent with the token request, checked against
    the S256 challenge): the verifier is 43 to 128 unreserved characters and its base64url
    SHA-256 is the challenge of the authorization request."""
    auth, done = complete(c, fake, idp, signed(fake, fake.key, fake.kid))
    assert done.headers["location"] == "/"
    verifier = fake.token_calls[-1]["code_verifier"]
    assert UNRESERVED.match(verifier)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == auth.challenge


def test_pkce_challenge_mismatch_fails_the_exchange_rfc_7636_4_6(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, owner_session: Session
) -> None:
    """RFC 7636 4.6: when the challenge the IdP holds does not match the verifier, the token
    request fails and no session is opened."""
    auth = login(c, fake, idp)
    auth.challenge = "x" * 43  # the IdP holds a challenge the verifier does not match
    fake.issue_code(auth, fake.claims_for(auth))
    assert_failed(c, oidc_flow.finish(c, idp, "c1", auth.state))
    assert last_reason(owner_session).startswith("token_exchange_failed")
