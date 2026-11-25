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

Fabric, Palantir and Docker (SP8 design section 2): a Fabric workspace normalizes to its REST
collection URL `https://api.fabric.microsoft.com/v1/workspaces/<lowercase guid>`; a Palantir
Foundry stack to `https://<label>.palantirfoundry.com` or `https://<label>.<installation suffix>`
(one label, so a sub-host of a stack is no second claim key; a suffix below another listed one or
below `palantirfoundry.com` is refused where the list is read, `core.hostsuffixes`); a Docker
daemon to `docker://<daemon id>` (`contracts.DOCKER_HOST`), unchanged. Each claim keys on
`address_key` of the normalized address.

`public_https_origin` is the rule of a Palantir target's `frontend_origin` (SP8-7): a public HTTPS
host by name, with no DNS lookup.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from shifde.adapters.native.contracts import DOCKER_HOST
from shifde.core.hostsuffixes import PALANTIR_HOST_SUFFIX

DATABRICKS_HOST_SUFFIXES = ("cloud.databricks.com", "azuredatabricks.net", "gcp.databricks.com")

_LABEL = re.compile(r"[a-z0-9-]{1,63}")

SNOWFLAKE_HOST_SUFFIX = "snowflakecomputing.com"
# The labels `dataplatforms.snowflake.ACCOUNT` allows, after lowercasing and "_" -> "-".
_SNOWFLAKE_LOCATOR = re.compile(r"[a-z0-9][a-z0-9-]{0,254}")
_SNOWFLAKE_LABEL = re.compile(r"[a-z0-9-]{1,64}")

_FABRIC_API_ORIGIN = "https://api.fabric.microsoft.com"
_FABRIC_APP_ORIGIN = "https://app.fabric.microsoft.com"
FABRIC_API_WORKSPACES = f"{_FABRIC_API_ORIGIN}/v1/workspaces/"
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# the names `public_https_origin` refuses, each itself and every name below it
_PRIVATE_SUFFIXES = ("localhost", "localdomain", "local", "internal", "home.arpa")


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


class FabricAddressRefused(AddressRefused):
    """The address is not a Fabric workspace (one fixed reason, never the input)."""

    def __init__(self) -> None:
        ValueError.__init__(
            self,
            "the address is not a Fabric workspace id or "
            "https://app.fabric.microsoft.com/groups/<workspace id> URL",
        )


class PalantirAddressRefused(AddressRefused):
    """The address is not a Palantir Foundry stack URL (one fixed reason, never the input)."""

    def __init__(self) -> None:
        ValueError.__init__(
            self,
            "the address is not a Palantir Foundry stack URL (https://<stack>.palantirfoundry.com)",
        )


class PublicOriginRefused(AddressRefused):
    """The value is not a public HTTPS origin (one fixed reason, never the input)."""

    def __init__(self) -> None:
        ValueError.__init__(self, "must be a public HTTPS origin")


class DockerAddressRefused(AddressRefused):
    """The address is not a Docker daemon address (one fixed reason, never the input)."""

    def __init__(self) -> None:
        ValueError.__init__(self, "the address is not a Docker daemon address (docker://<id>)")


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
    if any(ch in url for ch in "?#[]"):  # `urlsplit` raises on some bracketed hosts
        raise refused
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or parts.path not in ("", "/"):
        raise refused
    netloc = parts.netloc
    if "@" in netloc:
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


def normalize_fabric_address(value: str) -> str:
    """`https://api.fabric.microsoft.com/v1/workspaces/<lowercase guid>` for a workspace GUID
    (hyphenated `8-4-4-4-12`, any case), `https://app.fabric.microsoft.com/groups/<guid>` with an
    optional `/...` path, or the normalized form; else `FabricAddressRefused`. Refused: any other
    GUID form (braces, `urn:uuid:`, no hyphens), any other scheme or host, a port other than 443,
    user info, a query or fragment (even empty), whitespace or control characters and non-ASCII."""
    if not value.isascii() or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise FabricAddressRefused
    if _GUID.fullmatch(value):
        return FABRIC_API_WORKSPACES + value.lower()
    if any(ch in value for ch in "?#[]"):  # `urlsplit` raises on some bracketed hosts
        raise FabricAddressRefused
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or "@" in parts.netloc:
        raise FabricAddressRefused
    host, colon, port = parts.netloc.partition(":")
    if colon and port != "443":
        raise FabricAddressRefused
    origin, path = f"https://{host.lower()}", parts.path
    if origin == _FABRIC_APP_ORIGIN and path.startswith("/groups/"):
        guid = path.removeprefix("/groups/").split("/", 1)[0]
    elif origin == _FABRIC_API_ORIGIN and path.startswith("/v1/workspaces/"):
        guid = path.removeprefix("/v1/workspaces/")
    else:
        raise FabricAddressRefused
    if not _GUID.fullmatch(guid):
        raise FabricAddressRefused
    return FABRIC_API_WORKSPACES + guid.lower()


def normalize_palantir_address(url: str, extra_suffixes: Iterable[str] = ()) -> str:
    """`https://<host>` for `https://<label>.palantirfoundry.com` or `<label>.<extra suffix>`
    (the host lowercased, `:443` dropped), else `PalantirAddressRefused`. Refused as the Databricks
    normalizer refuses: any other port, a path other than `/`, a query or fragment, IP literals,
    malformed labels and any other suffix (a bare suffix included). Also refused: two or
    more labels before the suffix, so a sub-host of a stack never gets a claim key of its own. The
    extra suffixes hold no entry below another one or below `palantirfoundry.com`: such a list is
    refused when read (`core.hostsuffixes`), as one stack would get two claim keys."""
    host = _https_host(url, PalantirAddressRefused)
    try:
        _host_labels(host)
    except AddressRefused:
        raise PalantirAddressRefused from None
    suffixes = (PALANTIR_HOST_SUFFIX, *(s.lower() for s in extra_suffixes))
    if host.partition(".")[2] not in suffixes:  # `_host_labels` refused an empty first label
        raise PalantirAddressRefused
    return f"https://{host}"


def public_https_origin(value: str) -> str:
    """`https://<host>` (lowercase host, `:443` and a trailing `/` dropped) for a public HTTPS
    origin, else `PublicOriginRefused`. Refused as `_https_host` refuses (any other scheme, port,
    user info, path, query, fragment, whitespace, control or non-ASCII character), and: IP literals
    in any form a resolver reads (the last label must start with a letter, so `127.1`,
    `2130706433` and `0x7f.0.0.1` too), single-label hosts, an empty label or a trailing dot, and
    `localhost`, `localdomain`, `local`, `internal` and `home.arpa` with every name below them. By
    name only: no DNS."""
    host = _https_host(value, PublicOriginRefused)
    try:
        labels = _host_labels(host)
    except AddressRefused:
        raise PublicOriginRefused from None
    if len(labels) < 2 or not labels[-1][0].isalpha():
        raise PublicOriginRefused
    if any(("." + host).endswith("." + suffix) for suffix in _PRIVATE_SUFFIXES):
        raise PublicOriginRefused
    return f"https://{host}"


def normalize_docker_address(value: str) -> str:
    """The value, unchanged, when it fully matches `contracts.DOCKER_HOST` (`docker://` and a
    lowercase UUID or uppercase base32 daemon id; never case-folded), else
    `DockerAddressRefused`."""
    if not DOCKER_HOST.fullmatch(value):
        raise DockerAddressRefused
    return value


def normalize_address(platform: str, url: str, extra_suffixes: Iterable[str] = ()) -> str:
    """The platform's normalizer: `databricks` or `palantir` (each with the caller's extra
    suffixes), `snowflake`, `fabric` or `docker` (no extra suffixes); `ValueError` for any other
    platform."""
    if platform == "databricks":
        return normalize_databricks_address(url, extra_suffixes)
    if platform == "snowflake":
        return normalize_snowflake_address(url)
    if platform == "fabric":
        return normalize_fabric_address(url)
    if platform == "palantir":
        return normalize_palantir_address(url, extra_suffixes)
    if platform == "docker":
        return normalize_docker_address(url)
    raise ValueError("unknown platform kind")


def address_key(normalized: str) -> str:
    """The claim key: sha256 hex of the normalized address (UTF-8)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
