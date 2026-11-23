"""Databricks workspace and Snowflake account address normalization (design sections 3 and 9
item 14, DECISIONS B-2501; SP6 design 6.4).

One normalizer for target registration and, later, the executor's credential audience check: an
address is reduced to `https://<host>` (lowercase host, `:443` dropped, any other port refused, so
the claim key is host-only, B-2524) and accepted only on an allow-listed Databricks host suffix.
`address_key` is the sha256 of that string; the one-platform-per-organization claim stores only
the key. Pure string handling: no DNS, no network,
no settings (the installation's extra suffixes are passed in by the caller). `normalize_address`
dispatches on the platform kind; the Snowflake entry takes no extra suffixes.

For Snowflake the egress host and the claim identity are different strings. The egress host
(`normalize_snowflake_address`) keeps `privatelink` and the region, because requests go there. The
claim identity (`snowflake_claim_identity`) is the organization-account name only, so every URL
form of one account gives one claim key: a Snowflake claim must use `address_key` of the identity,
never of the egress host.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

DATABRICKS_HOST_SUFFIXES = ("cloud.databricks.com", "azuredatabricks.net", "gcp.databricks.com")

_LABEL = re.compile(r"[a-z0-9-]{1,63}")

SNOWFLAKE_HOST_SUFFIX = "snowflakecomputing.com"
# The labels `dataplatforms.snowflake.ACCOUNT` allows, after lowercasing and "_" -> "-".
_SNOWFLAKE_LOCATOR = re.compile(r"[a-z0-9][a-z0-9-]{0,254}")
_SNOWFLAKE_LABEL = re.compile(r"[a-z0-9-]{1,64}")


class AddressRefused(ValueError):
    """The address is not an allowed Databricks workspace URL (one fixed reason for every
    refusal, so the message never echoes the input)."""

    def __init__(self) -> None:
        super().__init__("the address is not an allowed Databricks workspace URL (https://<host>)")


class SnowflakeAddressRefused(AddressRefused):
    """The address is not a Snowflake account URL (one fixed reason, never the input)."""

    def __init__(self) -> None:
        ValueError.__init__(
            self,
            "the address is not a Snowflake account URL (https://<account>.snowflakecomputing.com)",
        )


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


def _https_host(url: str, refused: type[AddressRefused]) -> str:
    """The lowercased host of an `https://<host>[:443][/]` URL, else `refused`."""
    if not url.isascii() or any(ch.isspace() or not ch.isprintable() for ch in url):
        raise refused
    if "?" in url or "#" in url:
        raise refused
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or parts.path not in ("", "/"):
        raise refused
    netloc = parts.netloc
    if "@" in netloc or netloc.startswith("["):
        raise refused
    host, colon, port = netloc.partition(":")
    if colon and port != "443":
        raise refused
    host = host.lower()
    if not host or _is_ip(host):
        raise refused
    return host


def normalize_databricks_address(url: str, extra_suffixes: Iterable[str] = ()) -> str:
    """`https://<host>` for an allow-listed Databricks workspace URL, else `AddressRefused`.
    Refused: any scheme but https, any explicit port but 443 (another port would give the same host
    a second claim key, B-2524), user info, a path other than `/`, a query or fragment (even
    empty), whitespace or control characters, non-ASCII, IP literals, malformed labels, and a host
    that is not `<label>.<suffix>` for a listed suffix."""
    host = _https_host(url, AddressRefused)
    _host_labels(host)
    suffixes = (*DATABRICKS_HOST_SUFFIXES, *(s.lower() for s in extra_suffixes))
    if not any(host.endswith("." + suffix) for suffix in suffixes):
        raise AddressRefused
    return f"https://{host}"


def normalize_snowflake_address(url: str) -> str:
    """`https://<host>` for `https://<account>.snowflakecomputing.com` or
    `https://<account>.privatelink.snowflakecomputing.com` (`<account>` one to three labels, the
    host lowercased, `_` -> `-`, `:443` dropped), else `SnowflakeAddressRefused`. Refused as the
    Databricks normalizer refuses, and any other suffix (`.snowflakecomputing.cn` included)."""
    host = _https_host(url, SnowflakeAddressRefused).replace("_", "-")
    labels = host.split(".")
    if labels[-2:] != SNOWFLAKE_HOST_SUFFIX.split("."):
        raise SnowflakeAddressRefused
    account = labels[:-2]
    if len(account) > 1 and account[-1] == "privatelink":
        account = account[:-1]
    if not 1 <= len(account) <= 3 or not _SNOWFLAKE_LOCATOR.fullmatch(account[0]):
        raise SnowflakeAddressRefused
    if not all(_SNOWFLAKE_LABEL.fullmatch(label) for label in account[1:]):
        raise SnowflakeAddressRefused
    return f"https://{host}"


def snowflake_claim_identity(url: str) -> str:
    """`https://<org>-<account>.snowflakecomputing.com` for an organization-account URL (plain or
    `privatelink`, any case, `_` or `-`, `:443` or a trailing `/`), else `SnowflakeAddressRefused`.
    Accepts only what `normalize_snowflake_address` accepts. Account-locator forms (a first label
    without `-`, with or without region and `privatelink`) and an org-account name followed by
    region labels are refused: from the string alone a locator cannot be shown to be the same
    account as an org-account name, and a locator is unique only within a region, so claims key
    only on the org-account name."""
    account = normalize_snowflake_address(url).removeprefix("https://").split(".")[:-2]
    if len(account) > 1 and account[-1] == "privatelink":
        account = account[:-1]
    if len(account) != 1 or "-" not in account[0]:
        raise SnowflakeAddressRefused
    return f"https://{account[0]}.{SNOWFLAKE_HOST_SUFFIX}"


def normalize_address(platform: str, url: str, extra_suffixes: Iterable[str] = ()) -> str:
    """The platform's normalizer: `databricks` (with the installation's extra suffixes) or
    `snowflake` (no extra suffixes); `ValueError` for any other platform."""
    if platform == "databricks":
        return normalize_databricks_address(url, extra_suffixes)
    if platform == "snowflake":
        return normalize_snowflake_address(url)
    raise ValueError("unknown platform kind")


def address_key(normalized: str) -> str:
    """The claim key: sha256 hex of the normalized address (UTF-8)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
