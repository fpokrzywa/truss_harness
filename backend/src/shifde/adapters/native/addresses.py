"""Databricks workspace address normalization (design sections 3 and 9 item 14, DECISIONS B-2501).

One normalizer for target registration and, later, the executor's credential audience check: an
address is reduced to `https://<host>` (lowercase host, `:443` dropped, any other port refused, so
the claim key is host-only, B-2524) and accepted only on an allow-listed Databricks host suffix.
`address_key` is the sha256 of that string; the one-platform-per-organization claim stores only
the key. Pure string handling: no DNS, no network,
no settings (the installation's extra suffixes are passed in by the caller).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

DATABRICKS_HOST_SUFFIXES = ("cloud.databricks.com", "azuredatabricks.net", "gcp.databricks.com")

_LABEL = re.compile(r"[a-z0-9-]{1,63}")


class AddressRefused(ValueError):
    """The address is not an allowed Databricks workspace URL (one fixed reason for every
    refusal, so the message never echoes the input)."""

    def __init__(self) -> None:
        super().__init__("the address is not an allowed Databricks workspace URL (https://<host>)")


def _host_labels(host: str) -> list[str]:
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise AddressRefused
    return labels


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalize_databricks_address(url: str, extra_suffixes: Iterable[str] = ()) -> str:
    """`https://<host>` for an allow-listed Databricks workspace URL, else `AddressRefused`.
    Refused: any scheme but https, any explicit port but 443 (another port would give the same host
    a second claim key, B-2524), user info, a path other than `/`, a query or fragment (even
    empty), whitespace or control characters, non-ASCII, IP literals, malformed labels, and a host
    that is not `<label>.<suffix>` for a listed suffix."""
    if not url.isascii() or any(ch.isspace() or not ch.isprintable() for ch in url):
        raise AddressRefused
    if "?" in url or "#" in url:
        raise AddressRefused
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or parts.path not in ("", "/"):
        raise AddressRefused
    netloc = parts.netloc
    if "@" in netloc or netloc.startswith("["):
        raise AddressRefused
    host, colon, port = netloc.partition(":")
    if colon and port != "443":
        raise AddressRefused
    host = host.lower()
    if not host or _is_ip(host):
        raise AddressRefused
    _host_labels(host)
    suffixes = (*DATABRICKS_HOST_SUFFIXES, *(s.lower() for s in extra_suffixes))
    if not any(host.endswith("." + suffix) for suffix in suffixes):
        raise AddressRefused
    return f"https://{host}"


def address_key(normalized: str) -> str:
    """The claim key: sha256 hex of the normalized address (UTF-8)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
