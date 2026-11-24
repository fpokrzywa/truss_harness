"""The Databricks address normalizer (design 3, 9 item 14; B-2501): pure string handling. SP8-1:
the Fabric, Palantir and Docker normalizers, each idempotent, each refusal one fixed text that
never echoes the input, and `normalize_address` dispatching on all five kinds."""

from __future__ import annotations

import hashlib

import pytest

from shifde.adapters.native.addresses import (
    DATABRICKS_HOST_SUFFIXES,
    AddressRefused,
    DockerAddressRefused,
    FabricAddressRefused,
    PalantirAddressRefused,
    address_key,
    normalize_address,
    normalize_databricks_address,
    normalize_docker_address,
    normalize_fabric_address,
    normalize_palantir_address,
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


# ------------------------------------------------------------------ Fabric, Palantir, Docker (SP8-1)
GUID = "0A1B2C3D-0000-4000-8000-00000000ABCD"
WORKSPACE = "https://api.fabric.microsoft.com/v1/workspaces/0a1b2c3d-0000-4000-8000-00000000abcd"
DAEMON_UUID = "0a1b2c3d-0000-4000-8000-00000000abcd"
DAEMON_BASE32 = ":".join(["ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX", "YZ23", "4567"] * 2)[:59]


@pytest.mark.parametrize(
    "value",
    [
        GUID,
        GUID.lower(),
        f"https://app.fabric.microsoft.com/groups/{GUID}",
        f"https://app.fabric.microsoft.com/groups/{GUID}/",
        f"https://app.fabric.microsoft.com/groups/{GUID}/lakehouses/x",
        f"HTTPS://APP.Fabric.Microsoft.com:443/groups/{GUID.lower()}/list",
        WORKSPACE,
        WORKSPACE.replace("0a1b2c3d", "0A1B2C3D"),
    ],
)
def test_fabric_addresses_normalize_to_the_workspace_collection(value: str) -> None:
    assert normalize_fabric_address(value) == WORKSPACE
    assert normalize_address("fabric", value) == WORKSPACE
    assert normalize_fabric_address(WORKSPACE) == WORKSPACE  # idempotent


@pytest.mark.parametrize(
    "value",
    [
        "{" + GUID + "}",
        f"urn:uuid:{GUID}",
        GUID.replace("-", ""),
        GUID[:-1],
        f"https://app.fabric.microsoft.com/groups/{GUID}/lakehouses?x",
        f"https://app.fabric.microsoft.com/groups/{GUID}?",
        f"https://app.fabric.microsoft.com/groups/{GUID}#f",
        f"https://app.fabric.microsoft.com:8443/groups/{GUID}",
        f"https://app.fabric.microsoft.com:/groups/{GUID}",
        f"http://app.fabric.microsoft.com/groups/{GUID}",
        f"https://user@app.fabric.microsoft.com/groups/{GUID}",
        f"https://evil.example/groups/{GUID}",
        f"https://app.fabric.microsoft.com.evil.example/groups/{GUID}",
        f"https://app.fabric.microsoft.com/workspaces/{GUID}",
        f"https://[app.fabric.microsoft.com/groups/{GUID}",
        f"https://[zz]/groups/{GUID}",
        f"https://app.fabric.microsoft.com/groups/{{{GUID}}}",
        f"https://api.fabric.microsoft.com/v1/workspaces/{GUID}/items",
        f"https://api.fabric.microsoft.com/v1/workspaces/{GUID}/",
        f" {GUID}",
        f"{GUID}\n",
        f"https://app.fabric.microsoft.com/groups/{GUID}/é",
        "",
    ],
)
def test_fabric_refusals_never_echo_the_input(value: str) -> None:
    with pytest.raises(AddressRefused) as refused:
        normalize_fabric_address(value)
    assert isinstance(refused.value, FabricAddressRefused)
    assert str(refused.value) == str(FabricAddressRefused())
    if value:
        assert value not in str(refused.value)


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://acme.palantirfoundry.com", "https://acme.palantirfoundry.com"),
        ("HTTPS://ACME.PalantirFoundry.COM", "https://acme.palantirfoundry.com"),
        ("https://acme.palantirfoundry.com:443/", "https://acme.palantirfoundry.com"),
        ("https://eu.acme.palantirfoundry.com/", "https://eu.acme.palantirfoundry.com"),
    ],
)
def test_palantir_normalization(url: str, normalized: str) -> None:
    assert normalize_palantir_address(url) == normalized
    assert normalize_address("palantir", url) == normalized
    assert normalize_palantir_address(normalized) == normalized  # idempotent


def test_a_palantir_extra_suffix_is_accepted_only_when_passed() -> None:
    url = "https://stack-1.foundry.corp.example"
    with pytest.raises(PalantirAddressRefused):
        normalize_palantir_address(url)
    assert normalize_palantir_address(url, ["foundry.corp.example"]) == url
    assert normalize_address("palantir", url, ["Foundry.Corp.Example"]) == url
    with pytest.raises(PalantirAddressRefused):  # a bare suffix names no stack
        normalize_palantir_address("https://foundry.corp.example", ["foundry.corp.example"])
    with pytest.raises(PalantirAddressRefused):
        normalize_palantir_address("https://palantirfoundry.com")


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.palantirfoundry.com",
        "https://acme.palantirfoundry.com:8443",
        "https://acme.palantirfoundry.com/workspace",
        "https://acme.palantirfoundry.com?x=1",
        "https://acme.palantirfoundry.com#f",
        "https://user@acme.palantirfoundry.com",
        "https://10.0.0.1",
        "https://[::1]",
        "https://[::1",
        "https://[zz]",
        "https://acme_1.palantirfoundry.com",
        "https://acme..palantirfoundry.com",
        "https://acme.palantirfoundry.com.evil.example",
        "https://acme.cloud.databricks.com",
        "https://acmé.palantirfoundry.com",
        " https://acme.palantirfoundry.com",
        "",
    ],
)
def test_palantir_refusals_never_echo_the_input(url: str) -> None:
    with pytest.raises(AddressRefused) as refused:
        normalize_palantir_address(url)
    assert isinstance(refused.value, PalantirAddressRefused)
    assert str(refused.value) == str(PalantirAddressRefused())
    if url:
        assert url not in str(refused.value)


@pytest.mark.parametrize("daemon", [DAEMON_UUID, DAEMON_BASE32])
def test_docker_addresses_are_kept_unchanged(daemon: str) -> None:
    value = f"docker://{daemon}"
    assert normalize_docker_address(value) == value
    assert normalize_address("docker", value) == value
    assert normalize_docker_address(normalize_docker_address(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        f"docker://{DAEMON_UUID.upper()}",
        f"docker://{DAEMON_BASE32.lower()}",
        f"DOCKER://{DAEMON_UUID}",
        f"docker://{DAEMON_UUID}/",
        f"docker:///{DAEMON_UUID}",
        f"tcp://{DAEMON_UUID}",
        f" docker://{DAEMON_UUID}",
        f"docker://{DAEMON_UUID}\n",
        "docker://local",
        "unix:///var/run/docker.sock",
        "",
    ],
)
def test_docker_refusals_never_echo_the_input(value: str) -> None:
    with pytest.raises(AddressRefused) as refused:
        normalize_docker_address(value)
    assert isinstance(refused.value, DockerAddressRefused)
    assert str(refused.value) == str(DockerAddressRefused())
    if value:
        assert value not in str(refused.value)


def test_normalize_address_dispatches_on_all_five_kinds() -> None:
    assert normalize_address("databricks", "https://adb-1.cloud.databricks.com/")
    assert normalize_address("snowflake", "https://org-acct.snowflakecomputing.com")
    with pytest.raises(FabricAddressRefused):
        normalize_address("fabric", "https://adb-1.cloud.databricks.com")
    with pytest.raises(PalantirAddressRefused):
        normalize_address("palantir", "https://adb-1.cloud.databricks.com")
    with pytest.raises(DockerAddressRefused):
        normalize_address("docker", "https://adb-1.cloud.databricks.com")
    with pytest.raises(ValueError, match="unknown platform kind"):
        normalize_address("oracle", "https://x.example")
