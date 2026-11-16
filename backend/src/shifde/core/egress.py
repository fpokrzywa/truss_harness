"""EgressGuard: the only way adapters (and the IdP client) talk to the network (SECURITY_MODEL 6).

* DNS is resolved ONCE per connection attempt; every resolved address is checked against policy and
  the connection is made to the pinned address, with the original host kept for the HTTP ``Host``
  header and TLS SNI/certificate verification (httpx ``sni_hostname``; libpq ``hostaddr`` for
  PostgreSQL). A resolver that changes its answer between the check and the connect (DNS
  rebinding) therefore cannot redirect the connection.
* Denied by default: loopback, RFC 1918, CGNAT, link-local (cloud metadata), ULA, multicast,
  unspecified/reserved ranges, IPv4-mapped IPv6 forms of those, and well-known metadata addresses.
* Scheme / port / host allow lists, no redirects by default (each hop is re-validated when enabled),
  response size cap and timeouts.
* Effective policy = installation default AND tenant AND connection: every *set* layer must permit a
  request (most restrictive wins). Layers are `EgressPolicy` values; empty lists mean "this layer
  does not restrict that dimension"; private ranges need ``allow_private`` or a matching CIDR in
  every layer, hard-denied ranges (link-local, metadata, multicast, unspecified) a matching CIDR.

Nothing here knows about tenants, databases or the web framework (core depends on nothing).
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from dataclasses import dataclass, replace
from typing import Any, cast  # Any: opaque httpx extension dicts / client keyword arguments

import httpx

Resolver = Callable[[str], list[str]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

SUPPORTED_SCHEMES = frozenset({"http", "https", "postgresql"})
DEFAULT_PORTS = {"http": 80, "https": 443, "postgresql": 5432}
LOOPBACK_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
_HARD_DENIED: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. 169.254.169.254 metadata
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64: would tunnel to arbitrary IPv4
    ipaddress.ip_network("2002::/16"),  # 6to4: embeds an arbitrary IPv4
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
)
_METADATA_HOSTS: frozenset[IPAddress] = frozenset(
    ipaddress.ip_address(a)
    for a in ("168.63.129.16", "100.100.100.200", "169.254.170.2", "fd00:ec2::254")
)


class EgressViolation(Exception):
    """A destination or response is not allowed by the egress policy. Messages never contain
    resolved addresses or credentials. ``code`` is a stable machine-readable class."""

    def __init__(self, message: str, code: str = "egress_denied") -> None:
        super().__init__(message)
        self.code = code


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen: dict[str, None] = {}
    for info in infos:
        seen[str(info[4][0]).split("%", 1)[0]] = None  # drop IPv6 zone ids
    return list(seen)


def normalize_ip(raw: str) -> IPAddress:
    """Parse an address; IPv4-mapped IPv6 (::ffff:a.b.c.d) is unwrapped so it is judged as IPv4."""
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def classify_ip(ip: IPAddress) -> str:
    """``public`` | ``private`` (RFC1918/ULA/loopback/CGNAT/...) | ``hard`` (link-local, metadata,
    multicast, unspecified, reserved)."""
    if ip in _METADATA_HOSTS or any(ip in net for net in _HARD_DENIED if net.version == ip.version):
        return "hard"
    if ip.is_loopback:  # (::1 also falls in the reserved ::/8)
        return "private"
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "hard"
    return "public" if ip.is_global else "private"


def parse_cidrs(values: Sequence[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(v, strict=False) for v in values)


def _host_matches(host: str, patterns: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    for raw in patterns:
        pat = raw.lower().rstrip(".")
        if pat.startswith("*."):
            if host.endswith(pat[1:]) and host != pat[2:]:
                return True
        elif host == pat:
            return True
    return False


@dataclass(frozen=True)
class EgressPolicy:
    """One policy layer. See the module docstring for the empty-list convention."""

    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_ports: frozenset[int] = frozenset()
    allowed_hosts: tuple[str, ...] = ()
    allowed_cidrs: tuple[IPNetwork, ...] = ()
    allow_private: bool = False
    allow_redirects: bool = False
    max_response_bytes: int = 5_000_000
    timeout_seconds: float = 10.0

    def permits_scheme(self, scheme: str) -> bool:
        return not self.allowed_schemes or scheme in self.allowed_schemes

    def permits_port(self, port: int) -> bool:
        return not self.allowed_ports or port in self.allowed_ports

    def permits_host(self, host: str) -> bool:
        return not self.allowed_hosts or _host_matches(host, self.allowed_hosts)

    def permits_ip(self, ip: IPAddress) -> bool:
        category = classify_ip(ip)
        if category == "public":
            return True
        if any(ip in net for net in self.allowed_cidrs if net.version == ip.version):
            return True
        return category == "private" and self.allow_private


def installation_layer(stored: EgressPolicy | None, *, env: str) -> EgressPolicy:
    """The installation default layer. Development/test additionally allow loopback (127.0.0.0/8
    and ::1), plain http / postgresql and any port, so local sample targets work; production uses
    exactly the stored (or built-in strict) policy."""
    base = stored or EgressPolicy(allowed_ports=frozenset({443}))
    if env not in ("development", "test"):
        return base
    schemes = base.allowed_schemes | {"http", "postgresql"} if base.allowed_schemes else frozenset()
    return replace(
        base,
        allowed_schemes=schemes,
        allowed_ports=frozenset(),
        allowed_cidrs=base.allowed_cidrs + LOOPBACK_NETWORKS,
    )


@dataclass(frozen=True)
class PinnedTarget:
    host: str  # name to present for Host / SNI / certificate verification
    ip: str  # address to connect to
    port: int
    scheme: str


class EgressGuard:
    """Validates destinations against the effective policy layers and pins connections."""

    def __init__(
        self,
        layers: Sequence[EgressPolicy],
        *,
        resolver: Resolver = default_resolver,
        inner: httpx.BaseTransport | None = None,
        inner_async: httpx.AsyncBaseTransport | None = None,
        inner_factory: Callable[[], httpx.BaseTransport] | None = None,
        inner_async_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        max_redirects: int = 5,
    ) -> None:
        if not layers:
            raise ValueError("an egress guard needs at least one policy layer")
        self._layers = tuple(layers)
        self._resolver = resolver
        self._inner = inner
        self._inner_async = inner_async
        # One real transport (= one connection pool) per (name, address, port): httpcore pools by
        # address and ignores SNI, so hosts sharing an IP must never share a TLS connection.
        self._inner_factory = inner_factory or _default_transport
        self._inner_async_factory = inner_async_factory or _default_async_transport
        self.max_redirects = max_redirects
        self._deadline: float | None = None

    # ------------------------------------------------------------------ call deadline
    def set_deadline(self, seconds: float) -> None:
        """Hard overall budget for everything done through this guard from now on: the
        transports refuse new requests and stop reading bodies once it has passed."""
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float | None:
        return None if self._deadline is None else self._deadline - time.monotonic()

    def check_deadline(self) -> None:
        left = self.remaining()
        if left is not None and left <= 0:
            raise EgressViolation("the call exceeded its deadline", "deadline")

    def sleep(self, seconds: float) -> None:
        """Sleep for at most the remaining budget; raises when the budget runs out."""
        left = self.remaining()
        if left is not None and seconds >= left:
            time.sleep(max(left, 0))
            # The budget is spent either way: never fall through to a second full sleep, even if
            # the timer woke slightly early and the deadline has not strictly passed yet.
            raise EgressViolation("the call exceeded its deadline", "deadline")
        time.sleep(seconds)

    def _resolve_bounded(self, host: str) -> list[str]:
        """DNS resolution that cannot outlive the call deadline: the (blocking) resolver runs in a
        daemon thread and is abandoned when the remaining budget is spent."""
        left = self.remaining()
        if left is None:
            return self._resolver(host)
        if left <= 0:
            raise EgressViolation("the call exceeded its deadline", "deadline")
        box: dict[str, Any] = {}  # Any: resolver result / exception

        def work() -> None:
            try:
                box["ok"] = self._resolver(host)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
                box["err"] = exc

        thread = threading.Thread(target=work, daemon=True, name="egress-dns")
        thread.start()
        thread.join(left)
        if thread.is_alive():
            raise EgressViolation("DNS resolution exceeded the call deadline", "deadline")
        if "err" in box:
            raise box["err"]
        return list(box["ok"])

    def bound_extensions(self, extensions: dict[str, Any]) -> dict[str, Any]:  # Any: httpx ext
        """Copy of request extensions whose connect / read / write / pool timeouts are each
        capped by what is left of the call deadline (DNS, connect, TLS and reads are separate
        steps; none may outlive the deadline)."""
        left = self.remaining()
        if left is None:
            return extensions
        cap = max(left, 0.001)
        current = extensions.get("timeout") or {}
        bounded = {
            key: (cap if current.get(key) is None else min(float(current[key]), cap))
            for key in ("connect", "read", "write", "pool")
        }
        return {**extensions, "timeout": bounded}

    # ------------------------------------------------------------------ effective policy
    @property
    def layers(self) -> tuple[EgressPolicy, ...]:
        return self._layers

    @property
    def allow_redirects(self) -> bool:
        return all(layer.allow_redirects for layer in self._layers)

    @property
    def max_response_bytes(self) -> int:
        return min(layer.max_response_bytes for layer in self._layers)

    @property
    def timeout_seconds(self) -> float:
        return min(layer.timeout_seconds for layer in self._layers)

    # ------------------------------------------------------------------ validation / pinning
    def resolve_pinned(self, host: str, port: int | None, scheme: str) -> PinnedTarget:
        """Check scheme/port/host, resolve DNS once, check EVERY address, return the pinned one."""
        scheme = scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise EgressViolation("scheme is not supported", "scheme")
        if not host:
            raise EgressViolation("a host is required", "host")
        effective_port = port or DEFAULT_PORTS[scheme]
        for layer in self._layers:
            if not layer.permits_scheme(scheme):
                raise EgressViolation(
                    f"scheme {scheme!r} is not allowed by egress policy", "scheme"
                )
            if not layer.permits_port(effective_port):
                raise EgressViolation(f"port {effective_port} is not allowed", "port")
            if not layer.permits_host(host):
                raise EgressViolation("host is not allowed by egress policy", "host")
        literal = host.strip("[]")
        try:
            addresses = [str(ipaddress.ip_address(literal.split("%", 1)[0]))]
        except ValueError:
            try:
                addresses = self._resolve_bounded(host)
            except OSError as exc:
                raise EgressViolation("host did not resolve", "dns") from exc
        if not addresses:
            raise EgressViolation("host did not resolve", "dns")
        parsed: list[IPAddress] = []
        for raw in addresses:
            try:
                parsed.append(normalize_ip(raw))
            except ValueError as exc:
                raise EgressViolation("host resolved to an invalid address", "address") from exc
        for ip in parsed:
            for layer in self._layers:
                if not layer.permits_ip(ip):
                    category = classify_ip(ip)
                    raise EgressViolation(
                        f"host resolves to a non-public ({category}) address denied by policy",
                        "address",
                    )
        return PinnedTarget(host=host, ip=str(parsed[0]), port=effective_port, scheme=scheme)

    def check_url(self, url: str) -> PinnedTarget:
        """Validate a URL (DNS included) without connecting: config-time / dry-run checks."""
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise EgressViolation("invalid URL", "scheme") from exc
        if parsed.userinfo:
            raise EgressViolation("credentials in URLs are not allowed", "scheme")
        return self.resolve_pinned(parsed.host, parsed.port, parsed.scheme)

    # ------------------------------------------------------------------ httpx integration
    def _timeout(self) -> httpx.Timeout:
        """Per-step httpx timeout: the policy timeout, never more than what is left of the call
        deadline when one is set (so a tool's `timeout_s` also bounds a slow upstream)."""
        seconds = self.timeout_seconds
        left = self.remaining()
        if left is not None:
            seconds = max(min(seconds, left), 0.001)
        return httpx.Timeout(seconds)

    def client(self, **kwargs: Any) -> httpx.Client:  # Any: forwarded httpx.Client options
        return httpx.Client(
            transport=EgressTransport(self, self._inner),
            timeout=kwargs.pop("timeout", self._timeout()),
            follow_redirects=self.allow_redirects,
            max_redirects=self.max_redirects,
            trust_env=False,  # a proxy from the environment would bypass pinning
            **kwargs,
        )

    def async_client(self, **kwargs: Any) -> httpx.AsyncClient:  # Any: httpx.AsyncClient options
        return httpx.AsyncClient(
            transport=AsyncEgressTransport(self, self._inner_async),
            timeout=kwargs.pop("timeout", self._timeout()),
            follow_redirects=self.allow_redirects,
            max_redirects=self.max_redirects,
            trust_env=False,
            **kwargs,
        )

    def mcp_client_factory(
        self,
    ) -> Callable[..., httpx.AsyncClient]:
        """`httpx_client_factory` for the MCP SDK's streamable-HTTP client."""

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return self.async_client(headers=headers, auth=auth, timeout=timeout or self._timeout())

        return factory


def _pinned_copy(
    request: httpx.Request, target: PinnedTarget, guard: EgressGuard | None = None
) -> httpx.Request:
    """A COPY of the request aimed at the pinned IP (Host header and TLS server name keep the
    original name). The caller's request keeps its original URL, so httpx resolves relative
    redirects against the real host, not the IP."""
    extensions = dict(request.extensions)
    if guard is not None:
        extensions = guard.bound_extensions(extensions)
    if request.url.scheme == "https":
        extensions["sni_hostname"] = target.host
    headers = httpx.Headers(request.headers)
    headers["Accept-Encoding"] = "identity"  # caps count the bytes we hand to the caller
    return httpx.Request(
        request.method,
        request.url.copy_with(host=target.ip),
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def _check_response(response: httpx.Response, limit: int) -> None:
    """Refuse encoded bodies (a small gzip/deflate body could expand past the size cap after
    decoding) and bodies that declare themselves too large."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise EgressViolation("encoded (compressed) responses are refused", "encoding")
    declared = _declared_length(response)
    if declared is not None and declared > limit:
        raise EgressViolation("response exceeds the size limit", "size")


class _CappedStream(httpx.SyncByteStream):
    def __init__(self, inner: Iterable[bytes], limit: int, guard: EgressGuard) -> None:
        self._inner = inner
        self._limit = limit
        self._guard = guard

    def __iter__(self) -> Iterator[bytes]:
        seen = 0
        for chunk in self._inner:
            self._guard.check_deadline()
            seen += len(chunk)
            if seen > self._limit:
                raise EgressViolation("response exceeds the size limit", "size")
            yield chunk

    def close(self) -> None:
        if hasattr(self._inner, "close"):
            self._inner.close()


class _AsyncCappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: AsyncIterable[bytes], limit: int, guard: EgressGuard) -> None:
        self._inner = inner
        self._limit = limit
        self._guard = guard

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen = 0
        async for chunk in self._inner:
            self._guard.check_deadline()
            seen += len(chunk)
            if seen > self._limit:
                raise EgressViolation("response exceeds the size limit", "size")
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self._inner, "aclose"):
            await self._inner.aclose()


def _declared_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    return int(raw) if raw and raw.isdigit() else None


@functools.cache
def _verifying_context() -> ssl.SSLContext:
    """httpx's default verifying context (certifi CAs, CERT_REQUIRED, hostname check), built once.

    Loading the CA bundle costs ~0.3 s, and every pinned transport used to load its own inside the
    call's deadline (B-2330). A context carries no connections and no client credentials, so
    sharing one changes nothing about which certificates are accepted or which TLS connection a
    host gets (each still has its own transport, see `EgressGuard.__init__`)."""
    return httpx.create_ssl_context()


def _default_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(verify=_verifying_context())


def _default_async_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(verify=_verifying_context())


class EgressTransport(httpx.BaseTransport):
    def __init__(self, guard: EgressGuard, inner: httpx.BaseTransport | None = None) -> None:
        self.guard = guard
        self._shared = inner
        self._pools: dict[tuple[str, str, int, str], httpx.BaseTransport] = {}

    def _transport_for(self, target: PinnedTarget) -> httpx.BaseTransport:
        if self._shared is not None:
            return self._shared
        key = (target.host, target.ip, target.port, target.scheme)
        if key not in self._pools:
            self._pools[key] = self.guard._inner_factory()
        return self._pools[key]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.guard.check_deadline()
        target = self.guard.resolve_pinned(url.host, url.port, url.scheme)
        response = self._transport_for(target).handle_request(
            _pinned_copy(request, target, self.guard)
        )
        limit = self.guard.max_response_bytes
        try:
            _check_response(response, limit)
        except EgressViolation:
            response.close()
            raise
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CappedStream(cast(Iterable[bytes], response.stream), limit, self.guard),
            extensions=response.extensions,
        )

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
        for pool in self._pools.values():
            pool.close()


class AsyncEgressTransport(httpx.AsyncBaseTransport):
    def __init__(self, guard: EgressGuard, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.guard = guard
        self._shared = inner
        self._pools: dict[tuple[str, str, int, str], httpx.AsyncBaseTransport] = {}

    def _transport_for(self, target: PinnedTarget) -> httpx.AsyncBaseTransport:
        if self._shared is not None:
            return self._shared
        key = (target.host, target.ip, target.port, target.scheme)
        if key not in self._pools:
            self._pools[key] = self.guard._inner_async_factory()
        return self._pools[key]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        target = await asyncio.to_thread(self.guard.resolve_pinned, url.host, url.port, url.scheme)
        self.guard.check_deadline()
        transport = self._transport_for(target)
        response = await transport.handle_async_request(_pinned_copy(request, target, self.guard))
        limit = self.guard.max_response_bytes
        try:
            _check_response(response, limit)
        except EgressViolation:
            await response.aclose()
            raise
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_AsyncCappedStream(
                cast(AsyncIterable[bytes], response.stream), limit, self.guard
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        if self._shared is not None:
            await self._shared.aclose()
        for pool in self._pools.values():
            await pool.aclose()
