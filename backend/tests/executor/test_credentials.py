"""The credential broker (design 5, settled call 14; B-2689, B-2737): a token only for a grant that
names this job, executor and target, has not expired, whose audience is the normalized host of the
file's entry, and whose target belongs to the entry's organization. Every refusal is the same
`CredentialUnavailable`, naming nothing. A file of more than one organization is refused
(issue #155)."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: grant field overrides
from uuid import UUID, uuid4

import pytest

from shifde.adapters.native.contracts import CredentialGrant, utcnow
from shifde.executor import credentials
from shifde.executor.credentials import (
    CredentialBroker,
    CredentialEntry,
    CredentialFileRefused,
    CredentialUnavailable,
)
from tests.executor.support import CREDENTIAL_TOKEN, DEPLOYER, credentials_file

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
