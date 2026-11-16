"""In-process sliding-window rate limiter (per key, e.g. client IP).

Single-process only (state is not shared between workers); it is a brake on credential guessing,
complementing the per-account lockout, not a distributed quota system."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from collections.abc import Callable

from shifde.core.errors import RateLimited


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        message: str = "too many login attempts; try again later",
    ) -> None:
        self._message = message
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record a hit; raise RateLimited if `key` exceeded the limit (<= 0 disables)."""
        if self._limit <= 0:
            return
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                raise RateLimited(self._message)
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory: drop idle keys
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self._window]:
                    del self._hits[k]

    def exceeded(self, key: str) -> bool:
        """True if `key` is at its limit in the current window; records nothing (a caller that
        counts only failures checks this first, so a success cannot slip past a full window)."""
        if self._limit <= 0:
            return False
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            live = sum(1 for t in hits if now - t < self._window) if hits else 0
            return live >= self._limit


def source_key(client_ip: str | None) -> str:
    """The rate-limit / lockout key of a client address: an IPv6 address is grouped by its /64
    (one subscriber usually holds a whole /64, so rotating within it must not reset a counter,
    identity review 2 item 3); IPv4 and unparseable values are used as they are."""
    if not client_ip:
        return ""
    addr = _parse_ip(client_ip)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr) if addr is not None else client_ip


def ip_limit_applies(client_ip: str | None, trusted_proxies: list[str]) -> bool:
    """Per-IP limiting only makes sense for a real remote address: not for loopback or a
    configured trusted proxy (all users would share that one key)."""
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return True  # unparseable peer (e.g. test transport): treat as a distinct remote
    if addr.is_loopback:
        return False
    return not any(addr in ipaddress.ip_network(p, strict=False) for p in trusted_proxies)


_LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def resolve_client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxies: list[str],
    *,
    trust_loopback: bool = True,
) -> str | None:
    """The real client address of a request (audit Item 3).

    `X-Forwarded-For` is believed only when the TCP peer is a trusted proxy: one of
    `trusted_proxies` or loopback (the frontend proxy runs on the same host, so with the default
    empty setting every peer is 127.0.0.1 and the real client was lost). The chain is read from the
    right, skipping trusted proxies; the first other valid address is the client, so entries a
    client prepends to the header cannot be used to pick its own address. Anything unparseable
    falls back to the peer. With `trust_loopback` false (a deployment whose proxy is not on the same
    host) loopback is trusted no more than any other address: only `trusted_proxies` count."""
    if not peer:
        return None
    peer_addr = _parse_ip(peer)
    if peer_addr is None or not forwarded_for:
        return peer
    networks = [ipaddress.ip_network(p, strict=False) for p in trusted_proxies]
    if trust_loopback:
        networks.extend(_LOOPBACK_NETWORKS)

    def trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(addr.version == n.version and addr in n for n in networks)

    if not trusted(peer_addr):
        return peer
    for hop in reversed(forwarded_for.split(",")):
        addr = _parse_ip(hop)
        if addr is None:
            return peer  # a malformed hop breaks the chain of custody: do not guess
        if not trusted(addr):
            return str(addr)
    return peer
