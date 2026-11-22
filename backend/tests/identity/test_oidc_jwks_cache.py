"""The JWKS cache lifetime (issue #62, DECISIONS B-2729, closes the B-2663 observation): a
fetched JWKS lives for the response's `Cache-Control: max-age` held between `JWKS_MIN_TTL` and
`oidc_jwks_max_cache_seconds` (else `oidc_jwks_cache_seconds`), a retired key stops being
trusted once that lifetime ends, and a failed refetch after expiry fails closed. The fake IdP is
reached through a real `OutboundHttp` (egress guard, fake resolver); the clock is injected.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any  # Any: JWT claims

import httpx
import pytest

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.core.ssrf import EgressViolation, OutboundHttp
from shifde.identity.oidc import (
    JWKS_MIN_TTL,
    Discovery,
    OidcClient,
    OidcFailure,
    cache_max_age,
)
from shifde.identity.repository import ResolvedIdp
from tests._support.fake_oidc import CLIENT_ID, ISSUER, FakeOidcProvider, resolver

JWKS_URI = f"{ISSUER}/jwks"
NONCE = "nonce-1"
DISC = Discovery(
    issuer=ISSUER,
    authorization_endpoint=f"{ISSUER}/authorize",
    token_endpoint=f"{ISSUER}/token",
    jwks_uri=JWKS_URI,
    auth_methods=("client_secret_post",),
)
IDP = ResolvedIdp(
    uuid7(),
    None,
    "idp",
    ISSUER,
    f"{ISSUER}/.well-known/openid-configuration",
    CLIENT_ID,
    None,
    [],
    {},
    "off",
    "off",
    "generic",
    None,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(**update: Any) -> Settings:
    base = Settings(_env_file=None, env="test", database_url="postgresql+psycopg://x")
    return base.model_copy(update=update)


def _client(fake: FakeOidcProvider, clock: Clock, settings: Settings | None = None) -> OidcClient:
    return _client_over(fake.transport(), clock, settings)


def _client_over(
    transport: httpx.BaseTransport, clock: Clock, settings: Settings | None = None
) -> OidcClient:
    http = OutboundHttp(allow_dev_loopback=False, resolver=resolver, transport=transport)
    return OidcClient(settings or _settings(), http, clock=clock)


def _token(fake: FakeOidcProvider) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "s", "nonce": NONCE, "iat": now}
    return fake.mint({**claims, "exp": now + 300})


def _validate(oidc: OidcClient, raw: str) -> dict[str, Any]:
    nonce_hash = hashlib.sha256(NONCE.encode()).digest()
    return oidc.validate_id_token(raw, idp=IDP, disc=DISC, nonce_hash=nonce_hash)


def test_a_retired_key_is_trusted_until_the_lifetime_ends_then_refused() -> None:
    fake = FakeOidcProvider()
    clock = Clock()
    oidc = _client(fake, clock)
    token_a = _token(fake)
    assert _validate(oidc, token_a)["sub"] == "s"
    assert fake.jwks_calls == 1
    fake.rotate_key("key-2")  # the IdP retires key A
    token_b = _token(fake)
    clock.now += 3600 - 1  # the bounded window: A is still in the cache, no refetch
    assert _validate(oidc, token_a)["sub"] == "s"
    assert fake.jwks_calls == 1
    clock.now += 1  # the lifetime (default 3600 s) has passed
    with pytest.raises(OidcFailure) as refused:
        _validate(oidc, token_a)
    assert refused.value.reason == "token_signature_invalid"
    assert fake.jwks_calls == 2
    assert _validate(oidc, token_b)["sub"] == "s"
    assert fake.jwks_calls == 2


@pytest.mark.parametrize(
    ("cache_control", "lifetime"),
    [
        ("public, max-age=86400", 1800),  # above the cap: the cap
        ("max-age=0", JWKS_MIN_TTL),  # below the floor: the floor
        ("max-age=300", 300),  # within the bounds: as given
        (None, 900),  # no header: the default
        ("max-age=soon", 900),  # unparsable: the default
        ("no-cache, s-maxage=120", 900),  # no max-age: the default
    ],
)
def test_the_jwks_lifetime_is_max_age_clamped_or_the_default(
    cache_control: str | None, lifetime: float
) -> None:
    fake = FakeOidcProvider(jwks_cache_control=cache_control)
    clock = Clock()
    settings = _settings(oidc_jwks_cache_seconds=900, oidc_jwks_max_cache_seconds=1800)
    oidc = _client(fake, clock, settings)
    start = clock.now
    oidc.signing_keys(JWKS_URI, fake.kid)
    assert fake.jwks_calls == 1
    clock.now = start + lifetime - 0.5
    oidc.signing_keys(JWKS_URI, fake.kid)
    assert fake.jwks_calls == 1
    clock.now = start + lifetime
    oidc.signing_keys(JWKS_URI, fake.kid)
    assert fake.jwks_calls == 2


def test_the_default_settings_cap_entras_max_age_at_an_hour() -> None:
    fake = FakeOidcProvider(jwks_cache_control="max-age=86400")
    clock = Clock()
    oidc = _client(fake, clock)
    oidc.signing_keys(JWKS_URI, fake.kid)
    clock.now += 3600
    oidc.signing_keys(JWKS_URI, fake.kid)
    assert fake.jwks_calls == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("max-age=300", 300),
        ("public, MAX-AGE=120", 120),
        (" Max-Age = 45 , must-revalidate", 45),
        ("s-maxage=10, max-age=20", 20),
        ("max-age=10, max-age=20", 10),  # the first max-age counts
        ("max-age=20, s-maxage=10", 20),
        ("no-store", None),
        ("max-age=", None),
        ("max-age=-1", None),
        ("max-age=1.5", None),
        ('max-age="60"', None),
        ("max-age=abc, max-age=30", None),  # the first max-age is unparsable: the default
    ],
)
def test_cache_max_age_parsing(header: str | None, expected: int | None) -> None:
    assert cache_max_age(header) == expected


def test_a_failed_refetch_after_expiry_fails_closed() -> None:
    fake = FakeOidcProvider()
    failing: list[httpx.Response] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jwks" and failing:
            fake.jwks_calls += 1
            return failing[0]
        return fake.transport().handle_request(request)

    clock = Clock()
    oidc = _client_over(httpx.MockTransport(handle), clock)
    token = _token(fake)
    assert _validate(oidc, token)["sub"] == "s"
    clock.now += 3600
    for response in (httpx.Response(503), httpx.Response(200, json={"keys": "nope"})):
        failing[:] = [response]
        with pytest.raises(OidcFailure) as unavailable:
            oidc.signing_keys(JWKS_URI, fake.kid)
        assert unavailable.value.reason == "jwks_unavailable"
        calls = fake.jwks_calls
        with pytest.raises(OidcFailure) as refused:  # the cached key is never used again
            _validate(oidc, token)
        assert refused.value.reason == "jwks_unavailable"
        assert fake.jwks_calls == calls + 1  # every later call tries to refetch
    failing.clear()  # the IdP recovers
    assert _validate(oidc, token)["sub"] == "s"


def test_get_json_with_headers_returns_the_document_and_headers() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"keys": []}, headers={"Cache-Control": "max-age=7"})
    )
    http = OutboundHttp(allow_dev_loopback=False, resolver=resolver, transport=transport)
    doc, headers = http.get_json_with_headers(JWKS_URI)
    assert doc == {"keys": []}
    assert headers["cache-control"] == "max-age=7"
    private = OutboundHttp(
        allow_dev_loopback=False, resolver=lambda h: ["10.0.0.9"], transport=transport
    )
    with pytest.raises(EgressViolation):
        private.get_json_with_headers(JWKS_URI)


def _run_checks(oidc: OidcClient) -> list[tuple[str, bool, str]]:
    return oidc.run_checks(IDP.discovery_url, ISSUER)


def test_the_idp_test_fetches_the_jwks_but_never_touches_the_shared_cache() -> None:
    # issue #140 (DECISIONS B-2891): the IdP `test` endpoint fetches fresh and stores nothing
    fake = FakeOidcProvider()
    clock = Clock()
    oidc = _client(fake, clock)
    oidc.signing_keys(JWKS_URI, fake.kid)
    before = oidc._jwks[JWKS_URI]
    assert oidc._last_forced == {}
    for n in range(1, 5):
        clock.now += 10
        checks = _run_checks(oidc)
        assert checks[-1] == ("jwks", True, "1 signing key(s) available")
        assert fake.jwks_calls == 1 + n  # every test call fetches
        assert oidc._jwks[JWKS_URI] == before
        assert oidc._jwks[JWKS_URI][2] is before[2]
        assert oidc._last_forced == {}


def test_the_idp_test_with_nothing_cached_leaves_no_cache_entry() -> None:
    fake = FakeOidcProvider()
    oidc = _client(fake, Clock())
    assert [c[:2] for c in _run_checks(oidc)] == [("discovery", True), ("jwks", True)]
    assert fake.jwks_calls == 1
    assert JWKS_URI not in oidc._jwks
    assert oidc._last_forced == {}


def test_the_idp_test_keeps_the_kid_miss_refresh_rate_limit() -> None:
    fake = FakeOidcProvider()
    clock = Clock()
    oidc = _client(fake, clock, _settings(oidc_jwks_min_refresh_seconds=60))
    oidc.signing_keys(JWKS_URI, fake.kid)
    clock.now += 100
    oidc.signing_keys(JWKS_URI, "unknown-kid")  # a kid miss: one rate-limited refresh
    assert fake.jwks_calls == 2
    entry, forced = oidc._jwks[JWKS_URI], dict(oidc._last_forced)
    assert forced == {JWKS_URI: clock.now}
    for _ in range(3):
        clock.now += 5
        assert _run_checks(oidc)[-1][:2] == ("jwks", True)
    assert fake.jwks_calls == 5
    assert oidc._jwks[JWKS_URI] == entry
    assert oidc._last_forced == forced
    clock.now += 5  # still inside the 60 s window: no refetch on another kid miss
    oidc.signing_keys(JWKS_URI, "other-kid")
    assert fake.jwks_calls == 5
