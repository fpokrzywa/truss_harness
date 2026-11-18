"""The Databricks address normalizer (design 3, 9 item 14; B-2501): pure string handling."""

from __future__ import annotations

import hashlib

import pytest

from shifde.adapters.native.addresses import (
    DATABRICKS_HOST_SUFFIXES,
    AddressRefused,
    address_key,
    normalize_databricks_address,
)


@pytest.mark.parametrize("suffix", DATABRICKS_HOST_SUFFIXES)
def test_each_builtin_suffix_is_accepted(suffix: str) -> None:
    assert normalize_databricks_address(f"https://adb-1.{suffix}") == f"https://adb-1.{suffix}"


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("HTTPS://ADB-1.Cloud.Databricks.COM", "https://adb-1.cloud.databricks.com"),
        ("https://adb-1.cloud.databricks.com:443", "https://adb-1.cloud.databricks.com"),
        ("https://adb-1.cloud.databricks.com/", "https://adb-1.cloud.databricks.com"),
        ("https://ADB-1.cloud.databricks.com:443/", "https://adb-1.cloud.databricks.com"),
        ("https://a.b.azuredatabricks.net", "https://a.b.azuredatabricks.net"),
    ],
)
def test_normalization(url: str, normalized: str) -> None:
    assert normalize_databricks_address(url) == normalized


def test_an_extra_suffix_is_accepted_only_when_passed() -> None:
    url = "https://ws-1.databricks.corp.example"
    with pytest.raises(AddressRefused):
        normalize_databricks_address(url)
    assert normalize_databricks_address(url, ["databricks.corp.example"]) == url
    assert normalize_databricks_address(url.upper(), ["Databricks.Corp.Example"]) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://adb-1.cloud.databricks.com",
        "ftp://adb-1.cloud.databricks.com",
        "adb-1.cloud.databricks.com",
        "https://adb-1.cloud.databricks.com/api",
        "https://adb-1.cloud.databricks.com//",
        "https://adb-1.cloud.databricks.com?x=1",
        "https://adb-1.cloud.databricks.com?",
        "https://adb-1.cloud.databricks.com/?",
        "https://adb-1.cloud.databricks.com#frag",
        "https://adb-1.cloud.databricks.com#",
        "https://user@adb-1.cloud.databricks.com",
        "https://user:pw@adb-1.cloud.databricks.com",
        "https://10.0.0.1",
        "https://[::1]",
        "https://[::1]:443",
        "https://169.254.169.254",
        "https://cloud.databricks.com",
        "https://.cloud.databricks.com",
        "https://evil-cloud.databricks.com.attacker.net",
        "https://xcloud.databricks.com",
        "https://adb-1.cloud.databricks.com.",
        "https://adb..cloud.databricks.com",
        "https://adb_1.cloud.databricks.com",
        "https://adb-1.cloud.databricks.com:8443",
        "https://adb-1.cloud.databricks.com:80",
        "https://adb-1.cloud.databricks.com:0443",
        "https://adb-1.cloud.databricks.com:0",
        "https://adb-1.cloud.databricks.com:99999",
        "https://adb-1.cloud.databricks.com:",
        "https://adb-1.cloud.databricks.com:44a",
        "https://adb-1.cloud.databricks.com:1:2",
        "https://adbé.cloud.databricks.com",
        "https://xn--adb-1.cloud.databricks.com。",
        " https://adb-1.cloud.databricks.com",
        "https://adb-1.cloud.databricks.com ",
        "https://adb-1 .cloud.databricks.com",
        "https://adb-1.cloud.databricks.com\t",
        "https://adb-1.cloud.databricks.com\n",
        "https://adb-1.cloud\x00.databricks.com",
        "",
    ],
)
def test_refused(url: str) -> None:
    with pytest.raises(AddressRefused) as refused:
        normalize_databricks_address(url)
    assert str(refused.value) == str(AddressRefused())  # one fixed reason, no echo of the input


def test_the_bare_extra_suffix_is_refused() -> None:
    with pytest.raises(AddressRefused):
        normalize_databricks_address("https://corp.example", ["corp.example"])


def test_address_key_is_the_sha256_of_the_normalized_string() -> None:
    normalized = normalize_databricks_address("https://ADB-7.cloud.databricks.com:443/")
    expected = hashlib.sha256(b"https://adb-7.cloud.databricks.com").hexdigest()
    assert address_key(normalized) == expected
    assert len(expected) == 64
