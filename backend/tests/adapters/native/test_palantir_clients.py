"""The Foundry REST client against a recording mock transport: egress, paths, token and limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any  # Any: httpx.Client keyword arguments
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

import shifde.adapters.native.palantir.clients as clients
from shifde.adapters.native.contracts import FoundryMarketplaceSpec, Target, utcnow
from shifde.adapters.native.palantir.clients import FoundryCredentials, PalantirClient
from shifde.adapters.native.palantir.source import bindings
from tests.adapters.native.origins import NON_PUBLIC_ORIGINS

HOST = "https://enrollment.example.test"
ORIGIN = "https://native-app.example.test"
SECRET = "fixture-not-a-secret"
CREDENTIALS = FoundryCredentials("fixture-client", SECRET)
ONTOLOGY = f"ri.ontology.main.ontology.{uuid4()}"
INSTALLATION = f"ri.marketplace.main.block-set-installation.{uuid4()}"


def _target(allow_preview: str = "true") -> Target:
    return Target(
        tenant_id=uuid4(),
        host=HOST,
        bindings={
            "marketplace_rid": f"ri.marketplace.main.local.{uuid4()}",
            "product_id": str(uuid4()),
            "version_id": str(uuid4()),
            "namespace_rid": f"ri.compass.main.namespace.{uuid4()}",
            "parent_folder_rid": f"ri.compass.main.folder.{uuid4()}",
            "ontology_rid": ONTOLOGY,
            "object_type": "Example",
            "frontend_origin": ORIGIN,
            "frontend_asset_sha256": "a" * 64,
            "allow_preview_apis": allow_preview,
        },
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _token(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/multipass/api/oauth2/token":
        return httpx.Response(200, json={"access_token": "fixture-token"})
    return None


def _recording(
    api: Handler, sent: list[httpx.Request], token: Handler | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        minted = (token or _token)(request)
        if minted is not None and request.url.path == "/multipass/api/oauth2/token":
            return minted
        return api(request)

    return httpx.MockTransport(handler)


def _client(
    api: Handler,
    sent: list[httpx.Request],
    *,
    target: Target | None = None,
    token: Handler | None = None,
    hosts: list[str] | None = None,
    authorize: Callable[[], FoundryCredentials] | None = None,
) -> PalantirClient:
    def provider(host: str) -> FoundryCredentials:
        if hosts is not None:
            hosts.append(host)
        return CREDENTIALS

    return PalantirClient(
        target or _target(),
        credential_provider=provider,
        authorize=authorize or (lambda: CREDENTIALS),
        transport=_recording(api, sent, token),
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.mark.parametrize("path", ["/v3/x", "/api/x?y=1", "/api/x#y", "/api/../x"])
def test_an_invalid_path_is_refused_before_any_request(path: str) -> None:
    sent: list[httpx.Request] = []
    with pytest.raises(ValueError, match="invalid Foundry API path"):
        _client(_ok, sent).request("GET", path, preview=False)
    assert sent == []


def test_authenticated_requests_go_to_the_target_host_with_the_bearer() -> None:
    sent: list[httpx.Request] = []
    _client(_ok, sent).request("GET", "/api/v2/x", preview=False, params={"pageSize": "1"})
    request = sent[-1]
    assert str(request.url) == f"{HOST}/api/v2/x?pageSize=1"
    assert request.headers["authorization"] == "Bearer fixture-token"


def test_every_http_client_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    real = httpx.Client

    def spy(**kwargs: Any) -> httpx.Client:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(clients.httpx, "Client", spy)
    sent: list[httpx.Request] = []
    _client(_ok, sent).request("GET", "/api/v2/x", preview=False)
    assert len(seen) == 2  # the token and the request
    for kwargs in seen:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == httpx.Timeout(30, connect=5)


def test_a_redirect_is_not_followed() -> None:
    sent: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.example/api/x"})

    with pytest.raises(RuntimeError, match="Foundry request failed"):
        _client(redirect, sent).request("GET", "/api/v2/x", preview=False)
    assert [request.url.host for request in sent] == ["enrollment.example.test"] * 2


def test_a_response_over_two_mib_is_refused() -> None:
    sent: list[httpx.Request] = []

    def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))

    with pytest.raises(ValueError, match="budget"):
        _client(large, sent).request("GET", "/api/v2/x", preview=False)


def test_an_error_status_never_echoes_the_body() -> None:
    sent: list[httpx.Request] = []

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="native-detail-xyz")

    with pytest.raises(RuntimeError) as error:
        _client(failing, sent).request("GET", "/api/v2/x", preview=False)
    assert "native-detail-xyz" not in str(error.value)


@pytest.mark.parametrize("body", [b"[1, 2]", b"not json", b'"text"'])
def test_a_response_that_is_not_a_json_object_is_refused(body: bytes) -> None:
    sent: list[httpx.Request] = []
    with pytest.raises(ValueError, match="invalid Foundry response"):
        _client(lambda request: httpx.Response(200, content=body), sent).request(
            "GET", "/api/v2/x", preview=False
        )


def test_a_passed_deadline_refuses_before_any_request() -> None:
    sent: list[httpx.Request] = []
    client = _client(_ok, sent)
    client.deadline = utcnow() - timedelta(seconds=1)
    with pytest.raises(TimeoutError):
        client.request("GET", "/api/v2/x", preview=False)
    assert sent == []


# --------------------------------------------------------------------------- token
def test_the_token_is_minted_once_per_client_with_a_form_post() -> None:
    sent: list[httpx.Request] = []
    hosts: list[str] = []
    client = _client(_ok, sent, hosts=hosts)
    client.request("GET", "/api/v2/x", preview=False)
    client.request("GET", "/api/v2/y", preview=False)
    tokens = [request for request in sent if request.url.path == "/multipass/api/oauth2/token"]
    assert len(tokens) == 1
    (token,) = tokens
    assert token.method == "POST"
    assert str(token.url) == f"{HOST}/multipass/api/oauth2/token"
    assert token.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in token.headers
    form = {key: values[0] for key, values in parse_qs(token.content.decode()).items()}
    assert form == {
        "grant_type": "client_credentials",
        "client_id": "fixture-client",
        "client_secret": SECRET,
    }
    assert hosts == [HOST]
    second = _client(_ok, sent)
    second.request("GET", "/api/v2/x", preview=False)
    assert len([r for r in sent if r.url.path == "/multipass/api/oauth2/token"]) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text=SECRET),
        httpx.Response(200, json={"token_type": "bearer"}),
        httpx.Response(200, json={"access_token": ""}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_a_failing_token_endpoint_is_a_credential_error(response: httpx.Response) -> None:
    sent: list[httpx.Request] = []
    with pytest.raises(PermissionError, match="Foundry native credential unavailable") as error:
        _client(_ok, sent, token=lambda request: response).request(
            "GET", "/api/v2/x", preview=False
        )
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert [request.url.path for request in sent] == ["/multipass/api/oauth2/token"]


@pytest.mark.parametrize(
    "answer",
    [
        ({"content-encoding": "gzip"}, b"not gzip " + SECRET.encode()),
        *(
            ({}, json.dumps({"access_token": token}, ensure_ascii=False).encode())
            for token in (
                "fixture-töken",
                "fixture-token\n",
                "fixture\ntoken",
                "fixture-token ",
                "\tfixture-token",
                "fixture-token\x7f",
            )
        ),
    ],
    ids=["encoding", "non-ascii", "final-newline", "newline", "final-space", "tab", "del"],
)
def test_a_token_answer_that_cannot_be_decoded_or_sent_as_a_header_is_a_refusal(
    answer: tuple[dict[str, str], bytes],
) -> None:
    headers, content = answer
    sent: list[httpx.Request] = []

    def token(request: httpx.Request) -> httpx.Response:
        # `stream`: decoded when the client reads it, as a real answer is.
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(content))

    client = _client(_ok, sent, token=token)
    with pytest.raises(PermissionError) as error:
        client.request("GET", "/api/v2/x", preview=False)
    assert str(error.value) == "Foundry native credential unavailable"
    assert error.value.__cause__ is None and client.refused
    assert [request.url.path for request in sent] == ["/multipass/api/oauth2/token"]


def test_the_secret_and_token_stay_out_of_reprs() -> None:
    assert SECRET not in repr(CREDENTIALS)
    assert "fixture-client" in repr(CREDENTIALS)
    sent: list[httpx.Request] = []
    client = _client(_ok, sent)
    client.request("GET", "/api/v2/x", preview=False)
    assert SECRET not in repr(client) and "fixture-token" not in repr(client)


# --------------------------------------------------------------------------- preview
def test_marketplace_calls_send_preview_true() -> None:
    sent: list[httpx.Request] = []

    def installation(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rid": INSTALLATION})

    _client(installation, sent).get_installation(INSTALLATION)
    request = sent[-1]
    assert request.url.path == f"/api/v3/platform/marketplaceInstallations/{INSTALLATION}"
    assert request.url.params["preview"] == "true"


@pytest.mark.parametrize("allow", ["false", "", "True"])
def test_marketplace_calls_need_the_preview_opt_in_before_the_token(allow: str) -> None:
    sent: list[httpx.Request] = []
    client = _client(_ok, sent, target=_target(allow))
    for call in (
        lambda: client.create_installation({}),
        lambda: client.get_installation(INSTALLATION),
        lambda: client.submit_job({}),
        lambda: client.get_job(f"ri.marketplace.main.installation-job.{uuid4()}"),
    ):
        with pytest.raises(PermissionError, match="preview"):
            call()
    assert sent == []


def test_marketplace_reads_validate_the_rid_before_any_request() -> None:
    sent: list[httpx.Request] = []
    client = _client(_ok, sent)
    with pytest.raises(ValueError):
        client.get_installation("ri.marketplace.main.block-set-installation.x/../y")
    with pytest.raises(ValueError):
        client.get_job(INSTALLATION)
    assert sent == []


# --------------------------------------------------------------------------- ontology reads
def _ontology(object_type: str = "Example") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "preview" not in request.url.params
        path = request.url.path
        if path == f"/api/v2/ontologies/{ONTOLOGY}":
            return httpx.Response(200, json={"rid": ONTOLOGY})
        if path == f"/api/v2/ontologies/{ONTOLOGY}/objectTypes/Example":
            return httpx.Response(200, json={"apiName": object_type})
        if path == f"/api/v2/ontologies/{ONTOLOGY}/objects/Example":
            assert request.url.params["pageSize"] == "1"
            return httpx.Response(200, json={"data": [{"id": "1"}], "totalCount": "1"})
        return httpx.Response(404)

    return handler


def test_ontology_reads_are_read_only_gets_without_preview() -> None:
    sent: list[httpx.Request] = []
    target = _target("false")
    client = _client(_ontology(), sent, target=target)
    assert len(client.schema(bindings(target))) == 64
    assert client.read_one_object(bindings(target)) is True
    assert all(request.method == "GET" for request in sent[1:])


def test_a_changed_object_type_is_refused() -> None:
    sent: list[httpx.Request] = []
    target = _target()
    with pytest.raises(ValueError, match="object type"):
        _client(_ontology("Other"), sent, target=target).schema(bindings(target))


def test_an_object_read_without_data_and_total_count_is_refused() -> None:
    sent: list[httpx.Request] = []
    target = _target()
    client = _client(lambda r: httpx.Response(200, json={"data": []}), sent, target=target)
    with pytest.raises(ValueError, match="object read"):
        client.read_one_object(bindings(target))


# --------------------------------------------------------------------------- frontend asset
def test_the_asset_is_fetched_from_the_frontend_origin_without_authorization() -> None:
    sent: list[httpx.Request] = []
    target = _target()

    def asset(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"published")

    client = _client(asset, sent, target=target)
    spec = FoundryMarketplaceSpec(frontend_asset_path="assets/a b.txt")
    digest = client.frontend_asset_sha256(bindings(target), spec)
    assert len(digest) == 64
    (request,) = sent
    assert str(request.url) == f"{ORIGIN}/assets/a%20b.txt"
    assert "authorization" not in request.headers


def test_the_token_request_carries_no_json_body() -> None:
    sent: list[httpx.Request] = []
    _client(_ok, sent).request("POST", "/api/v3/x", {"a": 1}, preview=False)
    token, api = sent
    assert token.content and not token.content.startswith(b"{")
    assert json.loads(api.content) == {"a": 1}


@pytest.mark.parametrize("origin", NON_PUBLIC_ORIGINS)
def test_the_asset_of_a_non_public_frontend_origin_is_never_fetched(origin: str) -> None:
    sent: list[httpx.Request] = []
    target = _target()
    broken = replace(bindings(target), frontend_origin=origin)
    spec = FoundryMarketplaceSpec(frontend_asset_path="index.html")
    with pytest.raises(ValueError, match="^must be a public HTTPS origin$"):
        _client(_ok, sent, target=target).frontend_asset_sha256(broken, spec)
    assert sent == []


# --------------------------------------------------------------------------- authorize
class Refused(Exception):
    pass


def _calls(client: PalantirClient) -> list[Callable[[], object]]:
    b = bindings(client.target)
    spec = FoundryMarketplaceSpec(frontend_asset_path="index.html")
    return [
        lambda: client.request("GET", "/api/v2/x", preview=False),
        lambda: client.request("POST", "/api/v3/x", {}, preview=True),
        lambda: client.get_installation(INSTALLATION),
        lambda: client.schema(b),
        lambda: client.read_one_object(b),
        lambda: client.frontend_asset_sha256(b, spec),
    ]


@pytest.mark.parametrize("call", range(6))
@pytest.mark.parametrize("cached", [False, True], ids=["no-token", "cached-token"])
def test_a_refused_client_sends_no_request_not_even_with_a_cached_token(
    call: int, cached: bool
) -> None:
    sent: list[httpx.Request] = []
    allowed = [True]

    def authorize() -> FoundryCredentials:
        if not allowed[0]:
            raise Refused
        return CREDENTIALS

    client = _client(_ok, sent, authorize=authorize)
    if cached:
        client.request("GET", "/api/v2/x", preview=False)
    assert not client.refused
    before, allowed[0] = len(sent), False
    with pytest.raises(Refused):
        _calls(client)[call]()
    assert len(sent) == before and client.refused


def test_a_check_that_fails_while_the_token_is_minted_sends_no_request_with_it() -> None:
    sent: list[httpx.Request] = []

    def authorize() -> FoundryCredentials:
        if sent:
            raise Refused
        return CREDENTIALS

    client = _client(_ok, sent, authorize=authorize)
    with pytest.raises(Refused):
        client.request("GET", "/api/v2/x", preview=False)
    assert [request.url.path for request in sent] == ["/multipass/api/oauth2/token"]


@pytest.mark.parametrize("other", [False, True], ids=["refused", "other-credentials"])
def test_a_token_whose_check_failed_in_mid_mint_is_not_used_by_a_later_request(
    other: bool,
) -> None:
    sent: list[httpx.Request] = []
    checks = 0

    def authorize() -> FoundryCredentials:
        nonlocal checks
        checks += 1
        if checks != 2:  # the second is the one after the first mint
            return CREDENTIALS
        if other:
            return replace(CREDENTIALS, client_secret="rotated-fixture")
        raise Refused

    client = _client(_ok, sent, authorize=authorize)
    with pytest.raises(PermissionError if other else Refused):
        client.request("GET", "/api/v2/x", preview=False)
    client.request("GET", "/api/v2/y", preview=False)
    token = "/multipass/api/oauth2/token"
    assert [request.url.path for request in sent] == [token, token, "/api/v2/y"]


def test_a_token_of_other_credentials_is_not_used() -> None:
    sent: list[httpx.Request] = []
    now = [CREDENTIALS]
    client = _client(_ok, sent, authorize=lambda: now[0])
    client.request("GET", "/api/v2/x", preview=False)
    now[0] = replace(CREDENTIALS, client_secret="rotated-fixture")
    with pytest.raises(PermissionError, match="credential unavailable"):  # the provider's are older
        client.request("GET", "/api/v2/y", preview=False)
    assert [r.url.path for r in sent if r.url.path.startswith("/api")] == ["/api/v2/x"]
