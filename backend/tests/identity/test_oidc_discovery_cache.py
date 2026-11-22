"""The IdP test leaves the sign-in caches untouched (issue #189, DECISIONS B-3061): `run_checks`
fetches discovery and the JWKS live and validates them like sign-in does, but `_discovery`,
`_jwks` and `_last_forced` hold exactly what they held before, on success and on failure, so one
organization's test never forces a refetch of another's sign-in caches. The fake IdP is reached
through a real `OutboundHttp` (egress guard, fake resolver); the clock is injected.
"""

from __future__ import annotations

from typing import Any  # Any: cache snapshots

import httpx
import pytest

from shifde.identity.oidc import OidcClient
from tests._support.fake_oidc import ISSUER, FakeOidcProvider
from tests.identity.test_oidc_jwks_cache import JWKS_URI, Clock, _client, _client_over

DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
OK_CHECKS = [
    ("discovery", True, "discovery document fetched; issuer matches"),
    ("jwks", True, "1 signing key(s) available"),
]


class Counting:
    """The fake IdP behind a transport that counts discovery requests and can fail a path."""

    def __init__(self, fake: FakeOidcProvider) -> None:
        self.fake = fake
        self.discovery_calls = 0
        self.failing: dict[str, httpx.Response] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            self.discovery_calls += 1
        if path in self.failing:
            return self.failing[path]
        return self.fake.transport().handle_request(request)


def _snapshot(oidc: OidcClient) -> dict[str, Any]:
    return {
        "discovery": dict(oidc._discovery),
        "jwks": dict(oidc._jwks),
        "last_forced": dict(oidc._last_forced),
    }


def _assert_unchanged(oidc: OidcClient, before: dict[str, Any]) -> None:
    after = _snapshot(oidc)
    assert after == before
    # the very same stored tuples: same timestamps, same Discovery / KeySet objects
    for name in ("discovery", "jwks"):
        for key, entry in before[name].items():
            assert after[name][key][0] == entry[0]
            assert after[name][key][-1] is entry[-1]


def _populate(oidc: OidcClient, fake: FakeOidcProvider, clock: Clock) -> None:
    """Fill every cache through the sign-in paths, then let time pass so a rewrite would show."""
    oidc.discovery(DISCOVERY_URL, ISSUER)
    oidc.signing_keys(JWKS_URI, fake.kid)
    clock.now += 10
    oidc.signing_keys(JWKS_URI, "unknown-kid")  # the rate-limited kid-miss refresh
    assert oidc._discovery and oidc._jwks and oidc._last_forced
    clock.now += 10


def test_a_successful_test_on_empty_caches_stores_nothing() -> None:
    fake = FakeOidcProvider()
    oidc = _client(fake, Clock())
    assert oidc.run_checks(DISCOVERY_URL, ISSUER) == OK_CHECKS
    assert oidc._discovery == {}
    assert oidc._jwks == {}
    assert oidc._last_forced == {}
    assert fake.jwks_calls == 1


def test_a_successful_test_leaves_populated_caches_identical_and_still_fetches_live() -> None:
    fake = FakeOidcProvider()
    counting = Counting(fake)
    clock = Clock()
    oidc = _client_over(httpx.MockTransport(counting.handle), clock)
    _populate(oidc, fake, clock)
    before = _snapshot(oidc)
    discovery_calls, jwks_calls = counting.discovery_calls, fake.jwks_calls
    assert oidc.run_checks(DISCOVERY_URL, ISSUER) == OK_CHECKS
    _assert_unchanged(oidc, before)
    assert counting.discovery_calls == discovery_calls + 1  # live, not from the cache
    assert fake.jwks_calls == jwks_calls + 1


def test_sign_in_discovery_is_still_a_cache_hit_after_a_test() -> None:
    fake = FakeOidcProvider()
    counting = Counting(fake)
    clock = Clock()
    oidc = _client_over(httpx.MockTransport(counting.handle), clock)
    _populate(oidc, fake, clock)
    cached = oidc._discovery[DISCOVERY_URL][1]
    oidc.run_checks(DISCOVERY_URL, ISSUER)
    calls = counting.discovery_calls
    assert oidc.discovery(DISCOVERY_URL, ISSUER) is cached
    assert counting.discovery_calls == calls


@pytest.mark.parametrize("populated", [False, True])
def test_an_issuer_mismatch_fails_and_leaves_the_caches_unchanged(populated: bool) -> None:
    fake = FakeOidcProvider()
    clock = Clock()
    oidc = _client(fake, clock)
    if populated:
        _populate(oidc, fake, clock)
    before = _snapshot(oidc)
    fake.discovery_issuer = "https://other.test"
    assert oidc.run_checks(DISCOVERY_URL, ISSUER) == [
        ("discovery", False, "issuer_mismatch_discovery")
    ]
    _assert_unchanged(oidc, before)


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/.well-known/openid-configuration",
            [("discovery", False, "discovery_unreachable:unexpected status 503")],
        ),
        (
            "/jwks",
            [
                ("discovery", True, "discovery document fetched; issuer matches"),
                ("jwks", False, "jwks_unavailable"),
            ],
        ),
    ],
)
def test_an_unreachable_idp_fails_and_leaves_the_caches_unchanged(
    populated: bool, path: str, expected: list[tuple[str, bool, str]]
) -> None:
    fake = FakeOidcProvider()
    counting = Counting(fake)
    clock = Clock()
    oidc = _client_over(httpx.MockTransport(counting.handle), clock)
    if populated:
        _populate(oidc, fake, clock)
    before = _snapshot(oidc)
    counting.failing[path] = httpx.Response(503)
    assert oidc.run_checks(DISCOVERY_URL, ISSUER) == expected
    _assert_unchanged(oidc, before)


@pytest.mark.parametrize("populated", [False, True])
def test_an_egress_violation_fails_and_leaves_the_caches_unchanged(populated: bool) -> None:
    fake = FakeOidcProvider()
    clock = Clock()
    oidc = _client(fake, clock)
    if populated:
        _populate(oidc, fake, clock)
    before = _snapshot(oidc)
    checks = oidc.run_checks("http://idp.test/.well-known/openid-configuration", ISSUER)
    assert checks == [("discovery", False, "discovery_unreachable:https is required")]
    _assert_unchanged(oidc, before)
