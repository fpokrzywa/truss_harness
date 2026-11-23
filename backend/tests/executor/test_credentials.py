"""The credential broker (design 5, settled call 14; B-2689, B-2737): a token only for a grant that
names this job, executor and target, has not expired, whose audience is the normalized host of the
file's entry, and whose target belongs to the entry's organization. Every refusal is the same
`CredentialUnavailable`, naming nothing. A file of more than one organization is refused
(issue #155). A Snowflake entry (SP6-3) gets a key pair under the same checks, for a Snowflake
grant only, its key file read on each use."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: grant field overrides
from uuid import UUID, uuid4

import pytest

from shifde.adapters.native.contracts import CredentialGrant, utcnow
from shifde.adapters.native.snowflake.clients import SnowflakeKeyPair
from shifde.executor import credentials
from shifde.executor.credentials import (
    CredentialBroker,
    CredentialEntry,
    CredentialFileRefused,
    CredentialUnavailable,
)
from tests.executor.support import (
    CREDENTIAL_TOKEN,
    DEPLOYER,
    SNOWFLAKE_ROLE,
    SNOWFLAKE_USER,
    credentials_file,
    snowflake_credentials_file,
    snowflake_key_pem,
)

HOST = "https://adb-1234567890.cloud.databricks.com"
JOB, EXECUTOR, TARGET, TENANT = uuid4(), uuid4(), uuid4(), uuid4()


def _grant(**overrides: Any) -> CredentialGrant:
    values: dict[str, Any] = {
        "job_id": JOB,
        "executor_id": EXECUTOR,
        "target_id": TARGET,
        "credential_ref": "databricks-dev",
        "audience": HOST,
        "expires_at": utcnow() + timedelta(seconds=30),
    }
    values.update(overrides)
    return CredentialGrant(**values)


def _broker(tmp_path: Path, host: str = HOST) -> CredentialBroker:
    return CredentialBroker.from_file(credentials_file(tmp_path, host, tenant=TENANT))


def _token(broker: CredentialBroker, grant: CredentialGrant, tenant: UUID = TENANT) -> str:
    return broker.token_for(
        grant, job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=tenant
    )


def _entry(broker: CredentialBroker) -> CredentialEntry:
    return broker.entry_for(
        _grant(), job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=TENANT
    )


def test_a_matching_grant_gets_the_token_and_the_service_principal(tmp_path: Path) -> None:
    broker = _broker(tmp_path, HOST.upper().replace("HTTPS", "https") + ":443/")
    assert _token(broker, _grant()) == CREDENTIAL_TOKEN
    entry = _entry(broker)
    assert entry.service_principal == DEPLOYER
    assert entry.tenant_id == TENANT


@pytest.mark.parametrize(
    "overrides",
    [
        {"job_id": uuid4()},
        {"executor_id": uuid4()},
        {"target_id": uuid4()},
        {"expires_at": utcnow() - timedelta(seconds=1)},
        {"credential_ref": "databricks-prod"},
        {"audience": "https://adb-999.cloud.databricks.com"},
    ],
    ids=["job", "executor", "target", "expired", "unknown-ref", "audience"],
)
def test_every_mismatch_is_refused_with_one_fixed_message(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    broker = _broker(tmp_path)
    with pytest.raises(CredentialUnavailable) as raised:
        _token(broker, _grant(**overrides))
    message = str(raised.value)
    assert message == "the deployment credential is unavailable for this job"
    for leaked in (CREDENTIAL_TOKEN, HOST, "databricks"):
        assert leaked not in message


def test_a_target_of_another_tenant_is_refused_with_the_fixed_message(tmp_path: Path) -> None:
    with pytest.raises(CredentialUnavailable) as raised:
        _token(_broker(tmp_path), _grant(), tenant=uuid4())
    assert str(raised.value) == "the deployment credential is unavailable for this job"


def test_a_host_outside_the_allow_list_is_refused(tmp_path: Path) -> None:
    broker = _broker(tmp_path, "https://example.com")
    with pytest.raises(CredentialUnavailable):
        _token(broker, _grant(audience="https://example.com"))


def test_the_token_is_never_in_a_repr(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    entry = _entry(broker)
    for text in (repr(broker), repr(entry), str(entry)):
        assert CREDENTIAL_TOKEN not in text


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"ref": {"tenant_id": str(TENANT), "host": HOST, "token": CREDENTIAL_TOKEN}}),
        json.dumps(
            {
                "ref": {
                    "tenant_id": str(TENANT),
                    "host": HOST,
                    "token": CREDENTIAL_TOKEN,
                    "service_principal": "a",
                    "x": 1,
                }
            }
        ),
        json.dumps({"ref": {"host": HOST, "token": CREDENTIAL_TOKEN, "service_principal": "a"}}),
        json.dumps(
            {
                "ref": {
                    "tenant_id": "not-a-uuid",
                    "host": HOST,
                    "token": CREDENTIAL_TOKEN,
                    "service_principal": "a",
                }
            }
        ),
    ],
    ids=[
        "not-json",
        "not-a-map",
        "no-principal",
        "extra-field",
        "no-tenant_id",
        "tenant_id-not-uuid",
    ],
)
def test_a_malformed_file_is_refused_without_echoing_it(tmp_path: Path, content: str) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(CredentialFileRefused) as raised:
        CredentialBroker.from_file(path)
    assert CREDENTIAL_TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None


def _two_entries(tmp_path: Path, second_tenant: UUID) -> Path:
    path = tmp_path / "credentials.json"
    entry = {"host": HOST, "token": CREDENTIAL_TOKEN, "service_principal": DEPLOYER}
    path.write_text(
        json.dumps(
            {
                "databricks-dev": {**entry, "tenant_id": str(TENANT)},
                "databricks-prod": {**entry, "tenant_id": str(second_tenant)},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def test_a_file_of_two_organizations_is_refused_without_echoing_it(tmp_path: Path) -> None:
    other = uuid4()
    with pytest.raises(CredentialFileRefused) as raised:
        CredentialBroker.from_file(_two_entries(tmp_path, other))
    message = str(raised.value)
    assert message == "the credentials file must belong to one organization"
    for leaked in (CREDENTIAL_TOKEN, str(TENANT), str(other), HOST):
        assert leaked not in message


def test_a_file_of_two_entries_of_one_organization_loads(tmp_path: Path) -> None:
    broker = CredentialBroker.from_file(_two_entries(tmp_path, TENANT))
    assert broker.tenant_id == TENANT
    assert _token(broker, _grant(credential_ref="databricks-prod")) == CREDENTIAL_TOKEN


def test_a_file_with_no_entries_has_no_organization(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)
    assert CredentialBroker.from_file(path).tenant_id is None


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialFileRefused):
        CredentialBroker.from_file(tmp_path / "absent.json")


def test_posix_refuses_a_file_open_to_group_or_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = credentials_file(tmp_path, HOST, tenant=TENANT)
    mode = 0o100644

    class Stat:
        st_mode = mode

    monkeypatch.setattr(credentials, "POSIX", True)
    monkeypatch.setattr(credentials.os, "stat", lambda _: Stat())
    with pytest.raises(CredentialFileRefused, match="group or others"):
        CredentialBroker.from_file(path)
    Stat.st_mode = 0o100600
    assert _token(CredentialBroker.from_file(path), _grant()) == CREDENTIAL_TOKEN


@pytest.mark.skipif(os.name != "posix", reason="file modes are checked on POSIX only")
def test_posix_refuses_a_real_file_open_to_group_or_others(tmp_path: Path) -> None:
    path = credentials_file(tmp_path, HOST, tenant=TENANT)
    for mode in (0o640, 0o604):
        os.chmod(path, mode)
        with pytest.raises(CredentialFileRefused, match="group or others"):
            CredentialBroker.from_file(path)
    os.chmod(path, 0o600)
    assert _token(CredentialBroker.from_file(path), _grant()) == CREDENTIAL_TOKEN


def test_windows_does_not_check_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "POSIX", False)
    assert _token(_broker(tmp_path), _grant()) == CREDENTIAL_TOKEN


# ------------------------------------------------------------------ Snowflake entries (SP6-3)
SF_HOST = "https://acct-1.snowflakecomputing.com"
SF_PEM = snowflake_key_pem()


def _sf_grant(**overrides: Any) -> CredentialGrant:
    return _grant(**{"credential_ref": "snowflake-dev", "audience": SF_HOST, **overrides})


def _sf_broker(tmp_path: Path, host: str = SF_HOST, tenant: UUID = TENANT) -> CredentialBroker:
    return CredentialBroker.from_file(
        snowflake_credentials_file(tmp_path, host, SF_PEM, tenant=tenant)
    )


def _key_pair(
    broker: CredentialBroker, grant: CredentialGrant, tenant: UUID = TENANT
) -> SnowflakeKeyPair:
    return broker.key_pair_for(
        grant, job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=tenant
    )


def test_a_snowflake_entry_loads_and_an_entry_without_platform_is_databricks(
    tmp_path: Path,
) -> None:
    broker = _sf_broker(tmp_path)
    assert broker.tenant_id == TENANT
    entry = broker.snowflake_entry_for(
        _sf_grant(), job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=TENANT
    )
    assert entry.platform == "snowflake" and entry.user == SNOWFLAKE_USER
    databricks = _entry(_broker(tmp_path))
    assert databricks.platform == "databricks"


def test_a_snowflake_grant_gets_a_key_pair(tmp_path: Path) -> None:
    broker = _sf_broker(tmp_path, "https://ACCT_1.privatelink.snowflakecomputing.com:443/")
    grant = _sf_grant(audience="https://acct-1.privatelink.snowflakecomputing.com")
    pair = _key_pair(broker, grant)
    assert pair.host == "https://acct-1.privatelink.snowflakecomputing.com"
    assert pair.account == "acct-1"
    assert pair.tenant_id == TENANT
    assert (pair.user, pair.role) == (SNOWFLAKE_USER, SNOWFLAKE_ROLE)
    assert pair.private_key_pem == SF_PEM


@pytest.mark.parametrize(
    "host",
    [
        "https://xy12345.us-east-2.aws.snowflakecomputing.com",
        "https://xy12345.snowflakecomputing.com",
        "https://xy12345.privatelink.snowflakecomputing.com",
        "https://acct-1.us-east-1.snowflakecomputing.com",
    ],
    ids=["locator-region", "locator", "locator-privatelink", "org-account-region"],
)
def test_a_snowflake_host_without_a_claim_identity_gets_no_key_pair(
    tmp_path: Path, host: str
) -> None:
    """The key pair's account is the claim identity's organization-account name (B-3942), so an
    account-locator host, which no target can claim, gets no key pair."""
    with pytest.raises(CredentialUnavailable) as raised:
        _key_pair(_sf_broker(tmp_path, host), _sf_grant(audience=host))
    _assert_fixed(raised.value, tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"job_id": uuid4()},
        {"executor_id": uuid4()},
        {"target_id": uuid4()},
        {"expires_at": utcnow() - timedelta(seconds=1)},
        {"credential_ref": "snowflake-prod"},
        {"audience": "https://acct-2.snowflakecomputing.com"},
    ],
    ids=["job", "executor", "target", "expired", "unknown-ref", "audience"],
)
def test_every_snowflake_mismatch_is_refused_with_one_fixed_message(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(CredentialUnavailable) as raised:
        _key_pair(_sf_broker(tmp_path), _sf_grant(**overrides))
    _assert_fixed(raised.value, tmp_path)


def _assert_fixed(error: BaseException, tmp_path: Path) -> None:
    message = str(error)
    assert message == "the deployment credential is unavailable for this job"
    assert error.__cause__ is None
    for leaked in (SF_PEM[40:80], str(tmp_path), SF_HOST, SNOWFLAKE_USER, "snowflake-dev"):
        assert leaked not in message


def test_a_snowflake_entry_of_another_tenant_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialUnavailable) as raised:
        _key_pair(_sf_broker(tmp_path, tenant=uuid4()), _sf_grant())
    _assert_fixed(raised.value, tmp_path)


def test_a_databricks_entry_for_a_snowflake_target_and_the_reverse_are_refused(
    tmp_path: Path,
) -> None:
    # a Databricks entry under the reference the Snowflake grant names
    databricks = CredentialBroker.from_file(
        credentials_file(tmp_path, SF_HOST, tenant=TENANT, ref="snowflake-dev")
    )
    with pytest.raises(CredentialUnavailable) as raised:
        _key_pair(databricks, _sf_grant())
    _assert_fixed(raised.value, tmp_path)
    # a Snowflake entry under the reference a Databricks grant names
    snowflake = CredentialBroker.from_file(
        snowflake_credentials_file(tmp_path, HOST, SF_PEM, tenant=TENANT, ref="databricks-dev")
    )
    with pytest.raises(CredentialUnavailable):
        _token(snowflake, _grant())
    with pytest.raises(CredentialUnavailable):
        _entry(snowflake)


def test_a_missing_or_open_key_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _sf_broker(tmp_path)
    key_file = tmp_path / "credentials.json.p8"
    key_file.unlink()
    with pytest.raises(CredentialUnavailable) as raised:
        _key_pair(broker, _sf_grant())
    _assert_fixed(raised.value, tmp_path)
    broker = _sf_broker(tmp_path)

    class Stat:
        st_mode = 0o100640

    monkeypatch.setattr(credentials, "POSIX", True)
    monkeypatch.setattr(credentials.os, "stat", lambda _: Stat())
    with pytest.raises(CredentialUnavailable):
        _key_pair(broker, _sf_grant())
    Stat.st_mode = 0o100600
    assert _key_pair(broker, _sf_grant()).private_key_pem == SF_PEM


def test_a_snowflake_entry_mixed_with_another_organization_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    snowflake = {
        "host": SF_HOST,
        "platform": "snowflake",
        "user": SNOWFLAKE_USER,
        "role": SNOWFLAKE_ROLE,
        "private_key_file": str(tmp_path / "key.p8"),
    }
    databricks = {"host": HOST, "token": CREDENTIAL_TOKEN, "service_principal": DEPLOYER}
    path.write_text(
        json.dumps(
            {
                "snowflake-dev": {**snowflake, "tenant_id": str(TENANT)},
                "databricks-dev": {**databricks, "tenant_id": str(uuid4())},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    with pytest.raises(CredentialFileRefused, match="one organization"):
        CredentialBroker.from_file(path)


@pytest.mark.parametrize(
    "entry",
    [
        {"platform": "snowflake", "user": "u", "role": "r"},
        {"platform": "snowflake", "user": "u", "role": "r", "private_key_file": "k", "token": "t"},
        {"platform": "snowflake", "token": CREDENTIAL_TOKEN, "service_principal": "a"},
        {"platform": "oracle", "token": CREDENTIAL_TOKEN, "service_principal": "a"},
        {"user": "u", "role": "r", "private_key_file": "k"},
    ],
    ids=["no-key-file", "extra-token", "databricks-fields", "other-platform", "no-platform"],
)
def test_a_malformed_snowflake_entry_is_refused(tmp_path: Path, entry: dict[str, str]) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"ref": {"tenant_id": str(TENANT), "host": SF_HOST, **entry}}), encoding="utf-8"
    )
    os.chmod(path, 0o600)
    with pytest.raises(CredentialFileRefused) as raised:
        CredentialBroker.from_file(path)
    assert CREDENTIAL_TOKEN not in str(raised.value) and raised.value.__cause__ is None


def test_the_key_and_its_path_are_never_in_a_repr(tmp_path: Path) -> None:
    broker = _sf_broker(tmp_path)
    entry = broker.snowflake_entry_for(
        _sf_grant(), job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=TENANT
    )
    pair = _key_pair(broker, _sf_grant())
    for text in (repr(broker), repr(entry), str(entry), repr(pair), str(pair)):
        assert SF_PEM[40:80] not in text and "BEGIN PRIVATE KEY" not in text
        assert str(tmp_path) not in text and "credentials.json.p8" not in text
