"""HTTP helpers shared by the REST, MCP and model-provider adapters."""

from __future__ import annotations

import base64
import re
import ssl
from typing import Any  # Any: decoded JSON bodies
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

from shifde.adapters.base import (
    AdapterFailure,
    CheckRecorder,
    ConnectionConfig,
    ErrorClass,
    ExecContext,
)
from shifde.core.egress import EgressViolation

_EGRESS_CLASS = {
    "dns": ErrorClass.DNS,
    "size": ErrorClass.UPSTREAM,
    "encoding": ErrorClass.UPSTREAM,
    "deadline": ErrorClass.TIMEOUT,
}


# header names a connection must never set (credentials are added by the platform, the rest
# are transport-level); shared by the REST adapter's request headers and `api_key_header`
FORBIDDEN_HEADERS = frozenset({"host", "authorization", "cookie", "proxy-authorization"})


def _decode_until_stable(value: str) -> str:
    for _ in range(16):
        decoded = unquote(value)
        if decoded == value:
            return decoded
        value = decoded
    return value


_OVERLONG = re.compile(r"%c[01]", re.IGNORECASE)


def has_overlong_utf8(path: str) -> bool:
    """True when the path part carries a percent-encoded 0xC0 / 0xC1 byte in any layer of
    encoding: those never occur in valid UTF-8, and `%c0%ae` is the classic overlong `.` that a
    lenient server decodes to a dot segment. Other non-UTF-8 bytes (a Latin-1 `%E9`) are fine."""
    value = path.split("?", 1)[0].split("#", 1)[0]
    for _ in range(16):
        if _OVERLONG.search(value):
            return True
        decoded = unquote(value)
        if decoded == value:
            return False
        value = decoded
    return False


def check_base_url(base: str) -> SplitResult:
    """Parse a connection's base URL or fail as a CONFIG error (never as a crash, never as a
    call that may have been delivered): malformed hosts / ports, and any query string or
    fragment -- even an empty one -- are refused."""
    try:
        parts = urlsplit(base)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as exc:
        raise AdapterFailure(ErrorClass.CONFIG, "the base URL is not a valid URL") from exc
    if "?" in base or "#" in base:
        raise AdapterFailure(
            ErrorClass.CONFIG, "the base URL must not contain a query string or a fragment"
        )
    return parts


def has_dot_segment(path: str) -> bool:
    """True when the path part of `path` has a `.` or `..` segment in any encoding (`%2e`, a
    repeatedly encoded `%252e`, an encoded slash, a `;parameter` suffix): such a path can climb
    out of the base URL's path."""
    decoded = _decode_until_stable(path.split("?", 1)[0].split("#", 1)[0])
    for part in decoded.replace("\\", "/").split("/"):
        if part.split(";", 1)[0].strip() in (".", ".."):
            return True
    return False


def classify_exception(exc: BaseException) -> AdapterFailure:
    """Map transport exceptions to a stable, credential-free failure class."""
    if isinstance(exc, AdapterFailure):
        return exc
    if isinstance(exc, EgressViolation):
        return AdapterFailure(_EGRESS_CLASS.get(exc.code, ErrorClass.EGRESS), str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return AdapterFailure(ErrorClass.TIMEOUT, "the request timed out")
    if isinstance(exc, httpx.ConnectError):
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, ssl.SSLError) or "certificate" in str(exc).lower():
            return AdapterFailure(
                ErrorClass.TLS, "TLS handshake or certificate verification failed"
            )
        return AdapterFailure(ErrorClass.NETWORK, "could not connect to the endpoint")
    if isinstance(exc, httpx.HTTPError):
        return AdapterFailure(ErrorClass.NETWORK, f"HTTP transport error: {type(exc).__name__}")
    return AdapterFailure(ErrorClass.UPSTREAM, f"unexpected error: {type(exc).__name__}")


def auth_headers(conn: ConnectionConfig, *, header_name: str = "X-API-Key") -> dict[str, str]:
    """Credential headers for the connection's auth method (secret revealed only here)."""
    cred = conn.credential
    method = conn.auth_method
    if method == "none" or cred is None:
        return {}
    value = cred.reveal()
    if method == "bearer":
        return {"Authorization": f"Bearer {value}"}
    if method == "api_key":
        name = str(conn.config.get("api_key_header") or header_name)
        if name.lower() in FORBIDDEN_HEADERS:
            raise AdapterFailure(ErrorClass.CONFIG, f"header {name!r} cannot carry the api key")
        return {name: value}
    if method == "basic":
        user = str(conn.config.get("username", ""))
        token = base64.b64encode(f"{user}:{value}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    raise AdapterFailure(ErrorClass.CONFIG, f"auth method {method!r} is not supported here")


def join_url(base: str, path: str) -> str:
    """Join a request path onto a base URL, refusing anything that could leave the base host
    (absolute URLs, scheme-relative `//host`, backslashes, CR/LF)."""
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or "\r" in path
        or "\n" in path
    ):
        raise AdapterFailure(ErrorClass.CONFIG, "path must be a relative path starting with '/'")
    if "://" in path.split("?", 1)[0]:
        raise AdapterFailure(ErrorClass.CONFIG, "path must not contain a URL")
    if has_dot_segment(path):
        raise AdapterFailure(ErrorClass.CONFIG, "path must not contain '.' or '..' segments")
    path_part = path.split("?", 1)[0]
    decoded_part = _decode_until_stable(path_part)
    # raw spaces and control characters are refused; an ENCODED space inside a segment
    # (`annual%20report.pdf`, which the tool runtime produces) is ordinary, an encoded control
    # character is not
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in path_part) or any(
        ord(c) < 0x20 or ord(c) == 0x7F for c in path + decoded_part
    ):
        raise AdapterFailure(
            ErrorClass.CONFIG, "path must not contain raw spaces or control characters"
        )
    if has_overlong_utf8(path):
        raise AdapterFailure(ErrorClass.CONFIG, "path must not contain overlong UTF-8 escapes")
    b = check_base_url(base)
    # plain concatenation (dot segments are refused above): urljoin would read `documents:x` as a
    # scheme, and the result is still checked below
    joined = base.rstrip("/") + path
    try:
        j = urlsplit(joined)
    except ValueError as exc:
        raise AdapterFailure(ErrorClass.CONFIG, "the joined URL is not valid") from exc
    if (j.scheme, j.netloc) != (b.scheme, b.netloc) or not j.path.startswith(
        b.path.rstrip("/") + "/"
    ):
        raise AdapterFailure(ErrorClass.CONFIG, "path escapes the configured base URL")
    return joined


def standard_network_checks(
    ctx: ExecContext, url: str, rec: CheckRecorder, *, scheme_label: str = "egress"
) -> bool:
    """Run the `egress` + `dns` checks for `url`; True if the destination is permitted."""
    try:
        ctx.egress.check_url(url)
    except EgressViolation as exc:
        if exc.code == "dns":
            rec.ok("egress", "scheme, port and host are permitted")
            rec.fail("dns", str(exc))
        else:
            rec.fail("egress", str(exc))
        return False
    rec.ok("egress", "scheme, port and host are permitted")
    rec.ok("dns", "resolved once; every address is permitted; connection is pinned")
    return True


def body_preview(response: httpx.Response, limit: int = 65_536) -> tuple[Any, bool]:
    """(parsed JSON or text, truncated) for a response body."""
    raw = response.content
    truncated = len(raw) > limit
    raw = raw[:limit]
    ctype = response.headers.get("content-type", "")
    if "json" in ctype and not truncated:
        try:
            return response.json(), False
        except ValueError:
            pass
    return raw.decode("utf-8", "replace"), truncated
