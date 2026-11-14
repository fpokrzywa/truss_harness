"""EgressGuard / SSRF suite (SECURITY_MODEL 6): classification, pinning, redirects, caps, TLS."""

from __future__ import annotations

import datetime
import ipaddress
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from shifde.adapters.testservers import RestTestServer
from shifde.core.egress import (
    LOOPBACK_NETWORKS,
    EgressGuard,
    EgressPolicy,
    EgressViolation,
    classify_ip,
    installation_layer,
    normalize_ip,
    parse_cidrs,
)
from shifde.core.ssrf import OutboundHttp

PUBLIC = "93.184.216.34"


def guard(*layers: EgressPolicy, resolver: Any = None, inner: Any = None) -> EgressGuard:
    return EgressGuard(
        layers or [EgressPolicy(allowed_schemes=frozenset({"http", "https"}))],
        resolver=resolver or (lambda host: [PUBLIC]),
        inner=inner,
    )


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", "private"),
        ("::1", "private"),
        ("10.1.2.3", "private"),
        ("172.16.5.5", "private"),
        ("192.168.1.1", "private"),
        ("100.64.0.1", "private"),  # CGNAT
        ("fd00::1", "private"),  # IPv6 ULA
        ("169.254.169.254", "hard"),  # cloud metadata
        ("169.254.1.1", "hard"),
        ("fe80::1", "hard"),
        ("fd00:ec2::254", "hard"),  # AWS IPv6 metadata
        ("168.63.129.16", "hard"),  # Azure wireserver (public-looking)
        ("224.0.0.1", "hard"),
        ("ff02::1", "hard"),
        ("0.0.0.0", "hard"),
        ("::", "hard"),
        ("64:ff9b::a00:1", "hard"),  # NAT64 would tunnel to an arbitrary IPv4
        ("::ffff:127.0.0.1", "private"),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", "private"),
        ("::ffff:169.254.169.254", "hard"),
        ("8.8.8.8", "public"),
        ("2606:4700:4700::1111", "public"),
    ],
)
def test_ip_classification(address: str, expected: str) -> None:
    assert classify_ip(normalize_ip(address)) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3:8080/x",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://172.20.0.1/",
        "http://192.168.0.10/",
        "http://100.64.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://168.63.129.16/",
    ],
)
def test_private_and_metadata_destinations_are_denied_by_default(url: str) -> None:
    with pytest.raises(EgressViolation) as exc:
        guard().check_url(url)
    assert exc.value.code == "address"
    assert "non-public" in str(exc.value)


def test_decimal_octal_and_hex_ip_spellings_do_not_bypass() -> None:
    """Hosts like 2130706433 / 0x7f000001 resolve through the resolver (never as literals)."""
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["127.0.0.1"]  # what getaddrinfo would answer for these spellings

    for host in ("2130706433", "0x7f000001", "0177.0.0.1"):
        with pytest.raises(EgressViolation):
            guard(resolver=resolver).check_url(f"http://{host}/")
    # numeric spellings go through the resolver; a dotted-quad one is parsed as a literal by the
    # URL layer and judged directly - either way the loopback target is refused
    assert seen[:2] == ["2130706433", "0x7f000001"]


def test_a_hostname_with_any_private_answer_is_denied() -> None:
    mixed = guard(resolver=lambda h: [PUBLIC, "10.0.0.7"])
    with pytest.raises(EgressViolation):
        mixed.check_url("https://mixed.test/")
    private_first = guard(resolver=lambda h: ["10.0.0.7", PUBLIC])
    with pytest.raises(EgressViolation):
        private_first.check_url("https://mixed.test/")


def test_scheme_port_host_allowlists_and_credentials_in_urls() -> None:
    g = guard(
        EgressPolicy(
            allowed_schemes=frozenset({"https"}),
            allowed_ports=frozenset({443, 8443}),
            allowed_hosts=("api.example.com", "*.corp.example"),
        )
    )
    assert g.check_url("https://api.example.com/x").port == 443
    assert g.check_url("https://svc.corp.example:8443/").ip == PUBLIC
    for url, code in (
        ("http://api.example.com/", "scheme"),
        ("https://api.example.com:9000/", "port"),
        ("https://evil.example/", "host"),
        ("https://corp.example/", "host"),  # *.suffix does not match the bare domain
        ("ftp://api.example.com/", "scheme"),
        ("https://user:pw@api.example.com/", "scheme"),
    ):
        with pytest.raises(EgressViolation) as exc:
            g.check_url(url)
        assert exc.value.code == code, url


def test_unresolvable_host_is_a_dns_violation() -> None:
    def broken(host: str) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(EgressViolation) as exc:
        guard(resolver=broken).check_url("https://nowhere.test/")
    assert exc.value.code == "dns"
    with pytest.raises(EgressViolation):
        guard(resolver=lambda h: []).check_url("https://nowhere.test/")


# ------------------------------------------------------------------ layered policy
def test_effective_policy_is_the_intersection_of_all_set_layers() -> None:
    install = installation_layer(None, env="test")  # dev/test profile: loopback allowed
    assert guard(install).check_url("http://127.0.0.1:9/").ip == "127.0.0.1"
    # a connection policy without the CIDR (and without allow_private) narrows it back to deny
    narrow = EgressPolicy(allowed_schemes=frozenset({"http"}))
    with pytest.raises(EgressViolation):
        guard(install, narrow).check_url("http://127.0.0.1:9/")
    # ... and an explicit CIDR in every layer permits exactly that range
    exact = EgressPolicy(
        allowed_schemes=frozenset({"http"}), allowed_cidrs=parse_cidrs(["127.0.0.0/8"])
    )
    assert guard(install, exact).check_url("http://127.0.0.1:9/").ip == "127.0.0.1"
    # a tenant layer can never widen what the installation layer denies
    prod = installation_layer(None, env="production")
    wide = EgressPolicy(allow_private=True, allowed_cidrs=parse_cidrs(["0.0.0.0/0"]))
    with pytest.raises(EgressViolation):
        guard(prod, wide).check_url("https://127.0.0.1/")


def test_production_installation_default_denies_loopback_and_plain_http() -> None:
    prod = installation_layer(None, env="production")
    g = guard(prod)
    for url in ("https://127.0.0.1/", "http://example.com/", "https://example.com:8443/"):
        with pytest.raises(EgressViolation):
            g.check_url(url)
    assert g.check_url("https://example.com/").ip == PUBLIC
    assert not any(net.overlaps(ipaddress.ip_network("127.0.0.0/8")) for net in prod.allowed_cidrs)


def test_development_default_allows_loopback_only() -> None:
    for env in ("development", "test"):
        layer = installation_layer(None, env=env)
        assert set(layer.allowed_cidrs) == set(LOOPBACK_NETWORKS)
        g = guard(layer)
        assert g.check_url("http://127.0.0.1:1234/").ip == "127.0.0.1"
        for url in ("http://10.0.0.1/", "http://169.254.169.254/", "http://[fd00::1]/"):
            with pytest.raises(EgressViolation):
                g.check_url(url)


def test_most_restrictive_limits_win() -> None:
    g = guard(
        EgressPolicy(max_response_bytes=1000, timeout_seconds=5.0, allow_redirects=True),
        EgressPolicy(max_response_bytes=500, timeout_seconds=9.0, allow_redirects=False),
    )
    assert (g.max_response_bytes, g.timeout_seconds, g.allow_redirects) == (500, 5.0, False)


# ------------------------------------------------------------------ DNS pinning / rebinding
class Recorder(httpx.BaseTransport):
    def __init__(self, responder: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder or (lambda request: httpx.Response(200, content=b"ok"))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)  # type: ignore[no-any-return]


def test_connection_is_pinned_to_the_checked_address_when_dns_changes_its_answer() -> None:
    answers = [[PUBLIC], ["127.0.0.1"], ["169.254.169.254"]]
    calls: list[str] = []

    def rebinding(host: str) -> list[str]:
        calls.append(host)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    inner = Recorder()
    g = guard(resolver=rebinding, inner=inner)
    with g.client() as client:
        assert client.get("http://rebind.test:8080/path").status_code == 200
    assert calls == ["rebind.test"]  # resolved exactly once for the request
    [seen] = inner.requests
    assert seen.url.host == PUBLIC and seen.url.port == 8080  # the CHECKED address is the target
    assert seen.headers["host"] == "rebind.test:8080"  # Host header keeps the name
    # the next request re-resolves, sees the poisoned answer, and is refused
    with g.client() as client, pytest.raises(EgressViolation):
        client.get("http://rebind.test/")
    assert len(inner.requests) == 1


def test_https_keeps_the_server_name_for_sni_and_certificate_checks() -> None:
    inner = Recorder()
    with guard(inner=inner).client() as client:
        client.get("https://api.example.com/v1")
    [seen] = inner.requests
    assert seen.url.host == PUBLIC
    assert seen.extensions["sni_hostname"] == "api.example.com"
    assert seen.headers["host"] == "api.example.com"


def test_outbound_http_for_idp_traffic_is_pinned_too() -> None:
    """The P2 IdP client now runs on the EgressGuard (closes the DNS-rebinding known limit)."""
    calls: list[str] = []

    def rebinding(host: str) -> list[str]:
        calls.append(host)
        return [PUBLIC] if len(calls) == 1 else ["10.0.0.9"]

    inner = Recorder(lambda r: httpx.Response(200, json={"issuer": "https://idp.test"}))
    http = OutboundHttp(allow_dev_loopback=False, resolver=rebinding, transport=inner)
    assert http.get_json("https://idp.test/.well-known/openid-configuration")["issuer"]
    assert calls == ["idp.test"] and inner.requests[0].url.host == PUBLIC
    with pytest.raises(EgressViolation):
        http.get_json("https://idp.test/.well-known/openid-configuration")  # poisoned answer
    with pytest.raises(EgressViolation):
        OutboundHttp(allow_dev_loopback=False, resolver=lambda h: ["10.0.0.9"]).check(
            "https://internal.test/x"
        )


def test_postgres_connections_use_hostaddr_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    from shifde.adapters import postgres
    from shifde.adapters.conformance import connection, exec_ctx

    captured: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_connect(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise Stop

    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)
    g = EgressGuard(
        [EgressPolicy(allowed_schemes=frozenset({"postgresql"}))], resolver=lambda h: [PUBLIC]
    )
    conn = connection(
        "postgres",
        {"host": "db.example.com", "database": "d", "user": "u"},
        auth_method="basic",
        secret="pw-123456",
    )
    with pytest.raises(Stop):
        postgres._connect(exec_ctx(g), conn, write=False)
    assert captured["host"] == "db.example.com" and captured["hostaddr"] == PUBLIC
    private = EgressGuard(
        [EgressPolicy(allowed_schemes=frozenset({"postgresql"}))], resolver=lambda h: ["10.1.1.1"]
    )
    with pytest.raises(EgressViolation):
        postgres._connect(exec_ctx(private), conn, write=False)


# ------------------------------------------------------------------ redirects
def test_redirects_are_not_followed_by_default() -> None:
    inner = Recorder(
        lambda r: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
    )
    with guard(inner=inner).client() as client:
        response = client.get("http://api.example.com/redirect")
    assert response.status_code == 302 and len(inner.requests) == 1


def test_each_redirect_hop_is_revalidated_when_redirects_are_enabled() -> None:
    hops: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        hops.append(request.headers["host"])
        if request.headers["host"] == "start.test":
            return httpx.Response(302, headers={"Location": "http://second.test/"})
        if request.headers["host"] == "second.test":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        return httpx.Response(200)

    resolutions: list[str] = []

    def resolver(host: str) -> list[str]:
        resolutions.append(host)
        return [PUBLIC]

    g = guard(
        EgressPolicy(allowed_schemes=frozenset({"http"}), allow_redirects=True),
        resolver=resolver,
        inner=Recorder(responder),
    )
    with g.client() as client, pytest.raises(EgressViolation) as exc:
        client.get("http://start.test/")
    assert exc.value.code == "address"
    assert hops == ["start.test", "second.test"]  # the metadata hop was never connected
    assert resolutions == ["start.test", "second.test"]  # re-resolved and re-checked per hop


def test_redirect_to_private_from_a_real_server() -> None:
    with RestTestServer() as server:
        loop = EgressPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_cidrs=LOOPBACK_NETWORKS,
            allow_redirects=True,
        )
        with EgressGuard([loop]).client() as client, pytest.raises(EgressViolation):
            client.get(f"{server.base_url}/redirect")
        assert [r["path"] for r in server.requests] == ["/redirect"]


# ------------------------------------------------------------------ size caps / timeouts
def test_declared_and_streamed_oversize_responses_are_rejected() -> None:
    cap = EgressPolicy(allowed_schemes=frozenset({"http"}), max_response_bytes=100)
    declared = Recorder(lambda r: httpx.Response(200, content=b"x" * 500))
    with guard(cap, inner=declared).client() as client, pytest.raises(EgressViolation) as exc:
        client.get("http://big.test/")
    assert exc.value.code == "size"

    def chunked(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            for _ in range(10):
                yield b"y" * 30

        return httpx.Response(200, content=body())  # no Content-Length

    with guard(cap, inner=Recorder(chunked)).client() as client, pytest.raises(EgressViolation):
        client.get("http://big.test/").read()


def test_oversized_response_from_a_real_server() -> None:
    with RestTestServer(big_bytes=2_000_000) as server:
        loop = EgressPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_cidrs=LOOPBACK_NETWORKS,
            max_response_bytes=100_000,
        )
        with EgressGuard([loop]).client() as client, pytest.raises(EgressViolation) as exc:
            client.get(f"{server.base_url}/big").read()
        assert exc.value.code == "size"


def test_timeouts_are_enforced() -> None:
    with RestTestServer() as server:
        loop = EgressPolicy(
            allowed_schemes=frozenset({"http"}),
            allowed_cidrs=LOOPBACK_NETWORKS,
            timeout_seconds=0.4,
        )
        with EgressGuard([loop]).client() as client, pytest.raises(httpx.TimeoutException):
            client.get(f"{server.base_url}/slow")


def test_sleep_never_falls_through_to_a_second_full_sleep_after_an_early_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (P3 review): a budget-limited sleep that wakes slightly early must raise, not
    continue into a second full sleep."""
    import shifde.core.egress as egress_mod

    slept: list[float] = []
    # The fake sleep returns immediately without advancing the clock = an early wake-up.
    monkeypatch.setattr(egress_mod.time, "sleep", lambda s: slept.append(s))
    g = guard()
    g.set_deadline(5.0)
    with pytest.raises(EgressViolation) as exc:
        g.sleep(60.0)
    assert exc.value.code == "deadline"
    assert len(slept) == 1 and slept[0] <= 5.0

    slept.clear()
    g.sleep(0.01)  # well within the budget: one plain sleep
    assert slept == [0.01]


# ------------------------------------------------------------------ TLS against the pinned address
def _cert(tmp: Path, name: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"hello": "tls"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: object) -> None:
        return


def test_tls_certificate_is_verified_against_the_name_not_the_pinned_ip(tmp_path: Path) -> None:
    cert, key = _cert(tmp_path, "pinned.test")
    server = HTTPServer(("127.0.0.1", 0), _Hello)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        loop = EgressPolicy(allowed_cidrs=LOOPBACK_NETWORKS)
        trust = ssl.create_default_context(cafile=str(cert))
        trusting = httpx.HTTPTransport(verify=trust)
        ok = EgressGuard([loop], resolver=lambda h: ["127.0.0.1"], inner=trusting)
        with ok.client() as client:
            assert client.get(f"https://pinned.test:{port}/").json() == {"hello": "tls"}
        # same server, but the caller asks for a name the certificate does not cover
        with ok.client() as client, pytest.raises(httpx.ConnectError):
            client.get(f"https://other.test:{port}/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
