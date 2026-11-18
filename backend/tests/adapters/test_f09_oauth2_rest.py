"""F-09: the `rest.http` adapter's OAuth2 client-credentials auth, against a local fake token
endpoint + API (no real platform)."""

from __future__ import annotations

import base64
import itertools
import logging
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit
from uuid import UUID, uuid4

import pytest

from shifde.adapters import oauth2
from shifde.adapters.base import (
    AdapterFailure,
    ConnectionConfig,
    Credential,
    ErrorClass,
    ExecContext,
    ProbeOutcome,
)
from shifde.adapters.conformance import loopback_guard
from shifde.adapters.rest_http import RestHttpAdapter
from shifde.adapters.testservers import _LocalHttpServer, _send_json

SECRET = "client-secret-f09-value"
_serial = itertools.count(1)


class OAuth2FakeServer(_LocalHttpServer):
    """`POST /token` (client credentials, HTTP Basic) plus a bearer-protected API on `/api/*`,
    `/health` and `/openapi.json`. Every fetch issues a new, distinct token."""

    def __init__(self, clients: dict[str, str]) -> None:
        super().__init__()
        self.clients = clients  # client_id -> secret
        self.expires_in: int | None = 3600
        self.token_status: int | None = None  # force an error status at /token
        self.token_body: bytes | None = None  # force a raw body at /token
        self.valid: set[str] = set()
        self.issued: list[str] = []
        self.token_forms: list[dict[str, str]] = []
        self.api_tokens: list[str] = []  # bearer presented on each API request

    def revoke(self, token: str) -> None:
        self.valid.discard(token)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return

            def _token(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                outer.token_forms.append(form)
                if outer.token_status is not None:
                    _send_json(self, outer.token_status, {"error": "forced"})
                    return
                if outer.token_body is not None:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(outer.token_body)))
                    self.end_headers()
                    self.wfile.write(outer.token_body)
                    return
                auth = self.headers.get("Authorization", "")
                try:
                    cid, sec = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":")
                except ValueError:
                    _send_json(self, 401, {"error": "invalid_client"})
                    return
                cid, sec = unquote_plus(cid), unquote_plus(sec)
                if (
                    not auth.startswith("Basic ")
                    or outer.clients.get(cid) != sec
                    or form.get("grant_type") != "client_credentials"
                ):
                    _send_json(self, 401, {"error": "invalid_client"})
                    return
                token = f"at-{cid}-{next(_serial)}-{uuid4().hex}"
                outer.issued.append(token)
                outer.valid.add(token)
                doc: dict[str, Any] = {"access_token": token, "token_type": "Bearer"}
                if outer.expires_in is not None:
                    doc["expires_in"] = outer.expires_in
                _send_json(self, 200, doc)

            def _api(self) -> None:
                got = self.headers.get("Authorization", "").removeprefix("Bearer ")
                outer.api_tokens.append(got)
                if got not in outer.valid:
                    _send_json(self, 401, {"error": "unauthorized"})
                elif urlsplit(self.path).path.endswith("/openapi.json"):
                    _send_json(self, 200, {"paths": {"/items": {"get": {"summary": "list"}}}})
                else:
                    _send_json(self, 200, {"ok": True})

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                if urlsplit(self.path).path == "/token":
                    self._token()
                else:
                    self._api()

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self._api()

        return Handler


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    oauth2.TOKEN_CACHE.clear()
    yield
    oauth2.TOKEN_CACHE.clear()


@pytest.fixture
def fake() -> Iterator[OAuth2FakeServer]:
    with OAuth2FakeServer({"c1": SECRET}) as server:
        yield server


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(oauth2, "clock", lambda: now[0])
    return now


def ctx(tenant: UUID | None = None, guard: Any = None) -> ExecContext:
    return ExecContext(
        tenant_id=tenant or uuid4(), principal_id=None, egress=guard or loopback_guard()
    )


def conn(
    fake: OAuth2FakeServer,
    *,
    conn_id: UUID | None = None,
    secret: str | None = SECRET,
    **overrides: Any,
) -> ConnectionConfig:
    config: dict[str, Any] = {
        "base_url": f"{fake.base_url}/api",
        "health_path": "/health",
        "oauth2_url": f"{fake.base_url}/token",
        "client_id": "c1",
        "scope": "read write",
        **overrides,
    }
    return ConnectionConfig(
        id=conn_id or uuid4(),
        name="f09",
        adapter_key="rest.http",
        config={k: v for k, v in config.items() if v is not None},
        auth_method="oauth2_client_credentials",
        credential=Credential(secret) if secret else None,
    )


def get(session: Any, path: str = "/items") -> dict[str, Any]:
    result: dict[str, Any] = session.invoke({"method": "GET", "path": path}).result
    return result


def assert_no_leak(text: str, fake: OAuth2FakeServer) -> None:
    assert SECRET not in text
    for token in fake.issued:
        assert token not in text


def probe_text(outcome: ProbeOutcome) -> str:
    return " ".join([outcome.message or ""] + [c.detail for c in outcome.checks])


# ----------------------------------------------------------------------------- descriptor/config
def test_descriptor_offers_oauth2_and_guards_the_token_url() -> None:
    d = RestHttpAdapter.descriptor
    assert "oauth2_client_credentials" in d.auth_methods
    assert {"none", "api_key", "bearer", "basic"} <= set(d.auth_methods)
    assert "oauth2_url" in d.destination_fields and "base_url" in d.destination_fields


def test_the_token_url_is_a_destination_only_under_oauth2() -> None:
    d = RestHttpAdapter.descriptor
    cfg = {"base_url": "https://api.example", "oauth2_url": "https://idp.example/token"}
    assert d.destination("bearer", cfg) == {"base_url": "https://api.example"}
    assert d.destination("oauth2_client_credentials", cfg) == cfg
    assert set(d.auth_only_fields) == {"oauth2_url", "client_id", "scope"}


def test_validate_config_checks_the_token_url_like_the_base_url() -> None:
    a = RestHttpAdapter()
    base = {"base_url": "https://api.example"}
    ok = {**base, "oauth2_url": "https://idp.example/token", "client_id": "c1", "scope": "read"}
    assert a.validate_config(ok) == []
    for bad in (
        "https://u:p@idp.example/token",
        "ftp://idp.example/token",
        "/token",
        "https://idp.example/token?x=1",
        "https://idp.example/token#f",
    ):
        errors = a.validate_config({**base, "oauth2_url": bad})
        assert [e.path for e in errors] == ["oauth2_url"], bad


@pytest.mark.parametrize(
    "missing", [{"oauth2_url": None}, {"client_id": None}, {"secret": None}], ids=str
)
def test_missing_oauth2_settings_fail_as_config_without_a_network_call(
    fake: OAuth2FakeServer, missing: dict[str, Any]
) -> None:
    c = conn(fake, **missing)
    a = RestHttpAdapter()
    for call in (a.open, a.discover):
        with pytest.raises(AdapterFailure) as exc:
            call(ctx(), c)
        assert exc.value.error_class is ErrorClass.CONFIG
    outcome = a.test(ctx(), c)
    assert outcome.error_class is ErrorClass.CONFIG
    assert outcome.checks[0].name == "config" and outcome.checks[0].status == "fail"
    assert fake.token_forms == [] and fake.api_tokens == []


# ----------------------------------------------------------------------------- token use
def test_token_is_fetched_once_and_sent_as_bearer(fake: OAuth2FakeServer) -> None:
    c, x = conn(fake), ctx()
    a = RestHttpAdapter()
    session = a.open(x, c)
    try:
        assert get(session)["status"] == 200
        assert get(session)["status"] == 200
    finally:
        session.close()
    session = a.open(x, c)  # a new session for the same client and connection
    try:
        assert get(session)["status"] == 200
    finally:
        session.close()
    assert len(fake.issued) == 1
    assert fake.api_tokens == [fake.issued[0]] * 3
    assert fake.token_forms == [{"grant_type": "client_credentials", "scope": "read write"}]


def test_probe_and_discovery_use_the_bearer_token(fake: OAuth2FakeServer) -> None:
    c, x = conn(fake), ctx()
    a = RestHttpAdapter()
    outcome = a.test(x, c)
    assert outcome.passed, outcome
    discovered = a.discover(x, c)
    assert discovered.payload["paths"][0]["path"] == "/items"
    assert len(fake.issued) == 1 and fake.api_tokens == [fake.issued[0]] * 2


def test_secrets_with_reserved_characters_are_form_encoded(fake: OAuth2FakeServer) -> None:
    odd = "s3cr:et+with spaces&more"
    fake.clients["id:odd"] = odd
    session = RestHttpAdapter().open(ctx(), conn(fake, client_id="id:odd", secret=odd))
    try:
        assert get(session)["status"] == 200
    finally:
        session.close()


def test_token_is_refetched_after_expiry(fake: OAuth2FakeServer, clock: list[float]) -> None:
    fake.expires_in = 100
    c, x = conn(fake), ctx()
    session = RestHttpAdapter().open(x, c)
    try:
        get(session)
        clock[0] += 69  # still inside expires_in - 30
        get(session)
        assert len(fake.issued) == 1
        clock[0] += 2  # past expires_in - 30
        get(session)
    finally:
        session.close()
    assert len(fake.issued) == 2
    assert fake.api_tokens == [fake.issued[0], fake.issued[0], fake.issued[1]]


def test_default_lifetime_without_expires_in(fake: OAuth2FakeServer, clock: list[float]) -> None:
    fake.expires_in = None
    c, x = conn(fake), ctx()
    session = RestHttpAdapter().open(x, c)
    try:
        get(session)
        clock[0] += oauth2.DEFAULT_LIFETIME_S - oauth2.EXPIRY_MARGIN_S - 1
        get(session)
        assert len(fake.issued) == 1
        clock[0] += 2
        get(session)
    finally:
        session.close()
    assert len(fake.issued) == 2


def test_a_revoked_token_is_refreshed_and_the_call_retried_once(fake: OAuth2FakeServer) -> None:
    c, x = conn(fake), ctx()
    session = RestHttpAdapter().open(x, c)
    try:
        get(session)
        fake.revoke(fake.issued[0])
        assert get(session)["status"] == 200
        assert len(fake.issued) == 2
        assert fake.api_tokens == [fake.issued[0], fake.issued[0], fake.issued[1]]
        # the refreshed token is the cached one now
        assert get(session)["status"] == 200
        assert len(fake.issued) == 2
        # a write is retried the same way
        fake.revoke(fake.issued[1])
        result = session.invoke({"method": "POST", "path": "/items", "body": {}}, write=True)
        assert result.result["status"] == 200 and len(fake.issued) == 3
    finally:
        session.close()


def test_a_second_401_is_not_retried_again(fake: OAuth2FakeServer) -> None:
    c, x = conn(fake), ctx()
    a = RestHttpAdapter()
    fake.valid = _NeverValid()
    session = a.open(x, c)
    try:
        assert get(session)["status"] == 401
    finally:
        session.close()
    assert len(fake.issued) == 2 and len(fake.api_tokens) == 2
    outcome = a.test(x, c)
    assert outcome.error_class is ErrorClass.AUTH
    assert {ch.name: ch.status for ch in outcome.checks}["auth"] == "fail"
    assert len(fake.issued) == 3 and len(fake.api_tokens) == 4  # cached token, then one refresh


class _NeverValid(set[str]):
    def __contains__(self, _item: object) -> bool:
        return False


def test_uncacheable_contexts_cache_nothing(fake: OAuth2FakeServer) -> None:
    x = ExecContext(tenant_id=None, principal_id=None, egress=loopback_guard())
    session = RestHttpAdapter().open(x, conn(fake))
    try:
        get(session)
        get(session)
    finally:
        session.close()
    assert len(fake.issued) == 2 and len(oauth2.TOKEN_CACHE) == 0


def test_the_cache_is_bounded() -> None:
    cache = oauth2.TokenCache(max_entries=3)
    keys = [(uuid4(), uuid4(), str(i)) for i in range(5)]
    for k in keys:
        cache.put(k, "t" * 10, oauth2.clock() + 100)
    assert len(cache) == 3
    assert cache.get(keys[0]) is None and cache.get(keys[4]) is not None
    assert oauth2.MAX_ENTRIES == 1024


# ----------------------------------------------------------------------------- isolation
def test_two_clients_never_share_a_token(fake: OAuth2FakeServer) -> None:
    conn_id = uuid4()
    a = RestHttpAdapter()
    ctx_a, ctx_b = ctx(uuid4()), ctx(uuid4())
    for x in (ctx_a, ctx_b):
        session = a.open(x, conn(fake, conn_id=conn_id))
        try:
            assert get(session)["status"] == 200
        finally:
            session.close()
    assert len(fake.issued) == 2
    token_a, token_b = fake.issued
    assert token_a != token_b
    assert fake.api_tokens == [token_a, token_b]  # B's request carried B's token, not A's


def test_a_rotated_secret_does_not_reuse_the_old_token(fake: OAuth2FakeServer) -> None:
    conn_id, x = uuid4(), ctx()
    a = RestHttpAdapter()
    session = a.open(x, conn(fake, conn_id=conn_id))
    try:
        get(session)
    finally:
        session.close()
    rotated = "rotated-client-secret-f09"
    fake.clients["c1"] = rotated
    session = a.open(x, conn(fake, conn_id=conn_id, secret=rotated))
    try:
        assert get(session)["status"] == 200
    finally:
        session.close()
    assert len(fake.issued) == 2 and fake.api_tokens == fake.issued


# ----------------------------------------------------------------------------- failures
@pytest.mark.parametrize(
    ("status", "body", "error_class"),
    [
        (400, None, ErrorClass.AUTH),
        (401, None, ErrorClass.AUTH),
        (403, None, ErrorClass.AUTH),
        (429, None, ErrorClass.RATE_LIMIT),
        (500, None, ErrorClass.UPSTREAM),
        (None, b"not json", ErrorClass.UPSTREAM),
        (None, b'{"token_type": "bearer"}', ErrorClass.UPSTREAM),
        (None, b'{"access_token": "", "token_type": "bearer"}', ErrorClass.UPSTREAM),
        (None, b'{"access_token": "mac-token-f09-xyz", "token_type": "mac"}', ErrorClass.UPSTREAM),
    ],
)
def test_token_endpoint_failures_are_classified(
    fake: OAuth2FakeServer, status: int | None, body: bytes | None, error_class: ErrorClass
) -> None:
    fake.token_status, fake.token_body = status, body
    session = RestHttpAdapter().open(ctx(), conn(fake))
    try:
        with pytest.raises(AdapterFailure) as exc:
            get(session)
    finally:
        session.close()
    assert exc.value.error_class is error_class
    assert "mac-token-f09-xyz" not in exc.value.message and "not json" not in exc.value.message
    assert fake.api_tokens == []


def test_wrong_secret_fails_the_probe_as_auth(fake: OAuth2FakeServer) -> None:
    outcome = RestHttpAdapter().test(ctx(), conn(fake, secret="wrong-secret-f09-value"))
    assert outcome.error_class is ErrorClass.AUTH
    assert {ch.name: ch.status for ch in outcome.checks}["auth"] == "fail"
    assert fake.api_tokens == []


def test_a_denied_token_url_fails_before_any_token_request(fake: OAuth2FakeServer) -> None:
    c = conn(fake, oauth2_url="http://169.254.169.254/token")  # metadata address: denied
    a = RestHttpAdapter()
    outcome = a.test(ctx(), c)
    assert outcome.error_class is ErrorClass.EGRESS
    assert {ch.name: ch.status for ch in outcome.checks}["egress"] == "fail"
    session = a.open(ctx(), c)
    try:
        with pytest.raises(AdapterFailure) as exc:
            get(session)
    finally:
        session.close()
    assert exc.value.error_class in (ErrorClass.EGRESS, ErrorClass.DNS)
    # a guard without the loopback layer refuses the local token endpoint too
    denied = ExecContext(tenant_id=uuid4(), principal_id=None, egress=_no_loopback())
    session = a.open(denied, conn(fake))
    try:
        with pytest.raises(AdapterFailure):
            get(session)
    finally:
        session.close()
    assert fake.token_forms == [] and fake.api_tokens == []


def _no_loopback() -> Any:
    from shifde.core.egress import EgressGuard, EgressPolicy

    return EgressGuard([EgressPolicy(allowed_schemes=frozenset({"http"}))])


# ----------------------------------------------------------------------------- no leaks
def test_secret_and_tokens_never_leak(
    fake: OAuth2FakeServer, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.expires_in = 60
    c, x = conn(fake), ctx()
    a = RestHttpAdapter()
    texts: list[str] = []
    session = a.open(x, c)
    try:
        texts.append(repr(session.invoke({"method": "GET", "path": "/items"}).result))
        clock[0] += 31  # expiry refresh
        texts.append(repr(session.invoke({"method": "GET", "path": "/items"}).result))
        fake.revoke(fake.issued[-1])  # 401 refresh
        texts.append(repr(session.invoke({"method": "GET", "path": "/items"}).result))
        texts.append(repr(session) + repr(session._oauth2) + repr(oauth2.TOKEN_CACHE))
    finally:
        session.close()
    texts.append(probe_text(a.test(x, c)))
    fake.valid = _NeverValid()
    oauth2.TOKEN_CACHE.clear()
    texts.append(probe_text(a.test(x, c)))
    for status in (401, 500):
        fake.token_status = status
        oauth2.TOKEN_CACHE.clear()
        texts.append(probe_text(a.test(x, c)))
        session = a.open(x, c)
        try:
            with pytest.raises(AdapterFailure) as exc:
                get(session)
            texts.append(exc.value.message + str(exc.value))
        finally:
            session.close()
    assert len(fake.issued) >= 4
    for record in caplog.records:
        texts.append(record.getMessage())
    texts.append(caplog.text)
    assert caplog.records  # httpx did log at DEBUG/INFO
    for text in texts:
        assert_no_leak(text, fake)
