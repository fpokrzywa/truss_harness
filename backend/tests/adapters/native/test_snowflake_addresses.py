"""Snowflake account address normalization and the platform dispatch (SP6 design 6.4)."""

from __future__ import annotations

import pytest

from shifde.adapters.dataplatforms.snowflake import host_of
from shifde.adapters.native.addresses import (
    AddressRefused,
    SnowflakeAddressRefused,
    address_key,
    normalize_address,
    normalize_databricks_address,
    normalize_snowflake_address,
    snowflake_claim_identity,
)

MESSAGE = "the address is not a Snowflake account URL (https://<account>.snowflakecomputing.com)"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://My_Acct.snowflakecomputing.com:443/", "https://my-acct.snowflakecomputing.com"),
        ("https://acct1.snowflakecomputing.com", "https://acct1.snowflakecomputing.com"),
        ("HTTPS://ACCT1.SNOWFLAKECOMPUTING.COM/", "https://acct1.snowflakecomputing.com"),
        (
            "https://xy12345.us-east-2.aws.snowflakecomputing.com",
            "https://xy12345.us-east-2.aws.snowflakecomputing.com",
        ),
        (
            "https://org-acct.privatelink.snowflakecomputing.com",
            "https://org-acct.privatelink.snowflakecomputing.com",
        ),
        (
            "https://xy12345.eu-west-1.privatelink.snowflakecomputing.com:443",
            "https://xy12345.eu-west-1.privatelink.snowflakecomputing.com",
        ),
    ],
)
def test_accepted_addresses_normalize(url: str, expected: str) -> None:
    assert normalize_snowflake_address(url) == expected


REFUSED = {
    "http": "http://acct1.snowflakecomputing.com",
    "ftp": "ftp://acct1.snowflakecomputing.com",
    "no scheme": "acct1.snowflakecomputing.com",
    "other port": "https://acct1.snowflakecomputing.com:8443",
    "empty port": "https://acct1.snowflakecomputing.com:",
    "user info": "https://user:pw@acct1.snowflakecomputing.com",
    "path": "https://acct1.snowflakecomputing.com/console",
    "query": "https://acct1.snowflakecomputing.com/?a=1",
    "empty query": "https://acct1.snowflakecomputing.com?",
    "fragment": "https://acct1.snowflakecomputing.com#x",
    "empty fragment": "https://acct1.snowflakecomputing.com#",
    "space": "https://acct1.snowflakecomputing.com /",
    "tab": "https://acct1\t.snowflakecomputing.com",
    "newline": "https://acct1.snowflakecomputing.com\n",
    "control": "https://acct1.snowflake\x00computing.com",
    "non-ascii": "https://accté.snowflakecomputing.com",
    "ipv4": "https://10.0.0.1",
    "ipv6": "https://[::1]",
    "empty label": "https://acct1..snowflakecomputing.com",
    "bad label": "https://acct!.snowflakecomputing.com",
    "leading dash": "https://-acct.snowflakecomputing.com",
    "four account labels": "https://a.b.c.d.snowflakecomputing.com",
    "suffix only": "https://snowflakecomputing.com",
    "china": "https://acct1.snowflakecomputing.cn",
    "other suffix": "https://acct1.snowflakecomputing.com.evil.com",
    "lookalike": "https://acct1.snowflakecomputing.co",
    "databricks": "https://adb-1.azuredatabricks.net",
    "evil": "https://evil.example.com",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_each_refusal_class_has_one_fixed_message(name: str) -> None:
    url = REFUSED[name]
    with pytest.raises(SnowflakeAddressRefused) as refused:
        normalize_snowflake_address(url)
    assert str(refused.value) == MESSAGE
    assert url.strip() not in str(refused.value)
    assert isinstance(refused.value, AddressRefused) and isinstance(refused.value, ValueError)


@pytest.mark.parametrize(
    "account", ["acct1", "Acct_1", "xy12345.us-east-2.aws", "ORG-ACCT", "a" * 255, "a_b.eu-1"]
)
def test_host_of_is_a_fixed_point(account: str) -> None:
    assert normalize_snowflake_address(host_of(account)) == host_of(account)


def test_the_dispatch_by_platform() -> None:
    url = "https://ADB-1.azuredatabricks.net:443/"
    assert normalize_address("databricks", url) == normalize_databricks_address(url)
    assert normalize_address("databricks", "https://w.example.org", ("example.org",)) == (
        "https://w.example.org"
    )
    with pytest.raises(AddressRefused):
        normalize_address("databricks", "https://acct1.snowflakecomputing.com")
    assert normalize_address("snowflake", "https://Acct1.snowflakecomputing.com") == (
        "https://acct1.snowflakecomputing.com"
    )
    with pytest.raises(SnowflakeAddressRefused):
        normalize_address("snowflake", "https://adb-1.azuredatabricks.net")


@pytest.mark.parametrize("platform", ["", "Snowflake", "bigquery", "rest"])
def test_an_unknown_platform_is_refused(platform: str) -> None:
    with pytest.raises(ValueError, match="unknown platform"):
        normalize_address(platform, "https://acct1.snowflakecomputing.com")


def test_the_databricks_message_is_unchanged() -> None:
    with pytest.raises(AddressRefused) as refused:
        normalize_databricks_address("http://x.azuredatabricks.net")
    assert str(refused.value) == (
        "the address is not an allowed Databricks workspace URL (https://<host>)"
    )


# ------------------------------------------------------------------ claim identity
ORG_ACCOUNT = "https://myorg-acct1.snowflakecomputing.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://myorg-acct1.snowflakecomputing.com",
        "https://myorg-acct1.privatelink.snowflakecomputing.com",
        "https://MYORG-ACCT1.SNOWFLAKECOMPUTING.COM",
        "https://myorg_acct1.snowflakecomputing.com",
        "https://myorg-acct1.snowflakecomputing.com:443",
        "https://myorg-acct1.snowflakecomputing.com/",
        "https://MyOrg_Acct1.snowflakecomputing.com:443/",
    ],
)
def test_every_org_account_form_gives_one_claim_identity(url: str) -> None:
    assert snowflake_claim_identity(url) == ORG_ACCOUNT
    assert address_key(snowflake_claim_identity(url)) == address_key(ORG_ACCOUNT)


def test_two_organizations_get_one_claim_key_for_one_account() -> None:
    org_a = address_key(snowflake_claim_identity("https://myorg-acct1.snowflakecomputing.com"))
    org_b = address_key(
        snowflake_claim_identity("https://MYORG_ACCT1.privatelink.snowflakecomputing.com:443/")
    )
    assert org_a == org_b  # the unique claim on the key refuses organization B


@pytest.mark.parametrize(
    "account",
    ["xy12345", "xy12345.us-east-2.aws", "xy12345.us-east-2.privatelink", "myorg-acct1.us-east-1"],
)
def test_locator_forms_are_refused_for_claims(account: str) -> None:
    url = f"https://{account}.snowflakecomputing.com"
    normalize_snowflake_address(url)  # still a valid egress host
    with pytest.raises(SnowflakeAddressRefused) as refused:
        snowflake_claim_identity(url)
    assert str(refused.value) == MESSAGE


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_claim_identity_refuses_what_the_normalizer_refuses(name: str) -> None:
    with pytest.raises(SnowflakeAddressRefused) as refused:
        snowflake_claim_identity(REFUSED[name])
    assert str(refused.value) == MESSAGE


def test_the_egress_host_keeps_privatelink() -> None:
    url = "https://myorg-acct1.privatelink.snowflakecomputing.com"
    assert normalize_snowflake_address(url) == url
    assert snowflake_claim_identity(url) == ORG_ACCOUNT
