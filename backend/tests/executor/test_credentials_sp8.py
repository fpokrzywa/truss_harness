"""The credential broker's Fabric, Palantir and Docker entries (SP8-1): each parses, and is handed
out only under the checks every entry gets (job, executor, target, expiry, reference, audience,
organization and platform), with the same fixed `CredentialUnavailable`. A Fabric or Palantir
secret file is read on each use, whitespace stripped, and refused when missing, unreadable, empty
or, on POSIX, open to group or others. No secret or path reaches a `repr` or a message."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: grant field overrides
from uuid import UUID, uuid4

import pytest

from shifde.adapters.native.contracts import CredentialGrant, utcnow
from shifde.adapters.native.palantir.clients import FoundryCredentials
from shifde.executor import credentials
from shifde.executor.credentials import (
    CredentialBroker,
    CredentialFileRefused,
    CredentialUnavailable,
    DockerEntry,
    FabricCredentials,
    FabricEntry,
    PalantirEntry,
)
from tests.delivery.routes import SP8_KINDS
from tests.executor.support import (
    CREDENTIAL_TOKEN,
    DEPLOYER,
    SP8_CLIENT_ID,
    SP8_ENTRA_TENANT,
    SP8_SECRET,
    sp8_credentials_file,
    sp8_entry,
)

JOB, EXECUTOR, TARGET, TENANT = uuid4(), uuid4(), uuid4(), uuid4()
GUID = "0a1b2c3d-0000-4000-8000-00000000abcd"
HOST = {
    "fabric": f"https://api.fabric.microsoft.com/v1/workspaces/{GUID}",
    "palantir": "https://stack-1.palantirfoundry.com",
    "docker": f"docker://{GUID}",
}
OTHER_HOST = {
    "fabric": f"https://api.fabric.microsoft.com/v1/workspaces/{UUID(int=7)}",
    "palantir": "https://stack-2.palantirfoundry.com",
    "docker": f"docker://{UUID(int=7)}",
}
ENTRIES = {"fabric": FabricEntry, "palantir": PalantirEntry, "docker": DockerEntry}
SECRET_KINDS = ("fabric", "palantir")
FIXED = "the deployment credential is unavailable for this job"


def _grant(kind: str, **overrides: Any) -> CredentialGrant:
    values: dict[str, Any] = {
        "job_id": JOB,
        "executor_id": EXECUTOR,
        "target_id": TARGET,
        "credential_ref": f"{kind}-dev",
        "audience": HOST[kind],
        "expires_at": utcnow() + timedelta(seconds=30),
    }
    values.update(overrides)
    return CredentialGrant(**values)


def _broker(
    tmp_path: Path, kind: str, host: str | None = None, tenant: UUID = TENANT
) -> CredentialBroker:
    return CredentialBroker.from_file(
        sp8_credentials_file(tmp_path, kind, host or HOST[kind], tenant=tenant)
    )


def _use(
    broker: CredentialBroker, kind: str, grant: CredentialGrant, tenant: UUID = TENANT
) -> Any:  # Any: what the kind's job would use (its credentials, or the checked Docker entry)
    calls: dict[str, Callable[..., Any]] = {
        "fabric": broker.fabric_credentials_for,
        "palantir": broker.foundry_credentials_for,
        "docker": broker.docker_entry_for,
    }
    return calls[kind](grant, job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=tenant)


def _entry(
    broker: CredentialBroker, kind: str, grant: CredentialGrant
) -> Any:  # Any: the kind's entry
    calls: dict[str, Callable[..., Any]] = {
        "fabric": broker.fabric_entry_for,
        "palantir": broker.palantir_entry_for,
        "docker": broker.docker_entry_for,
    }
    return calls[kind](grant, job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=TENANT)


def _assert_fixed(error: BaseException, tmp_path: Path) -> None:
    assert str(error) == FIXED and error.__cause__ is None
    for leaked in (SP8_SECRET, str(tmp_path), SP8_CLIENT_ID, *HOST.values()):
        assert leaked not in str(error)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_each_entry_parses(tmp_path: Path, kind: str) -> None:
    broker = _broker(tmp_path, kind)
    assert broker.tenant_id == TENANT
    entry = _entry(broker, kind, _grant(kind))
    assert isinstance(entry, ENTRIES[kind])
    assert entry.platform == kind and entry.tenant_id == TENANT


def test_a_matching_grant_gets_the_platforms_credentials(tmp_path: Path) -> None:
    app_url = f"https://app.fabric.microsoft.com/groups/{GUID.upper()}/list"
    fabric = _use(_broker(tmp_path, "fabric", app_url), "fabric", _grant("fabric"))
    assert fabric == FabricCredentials(SP8_ENTRA_TENANT, SP8_CLIENT_ID, SP8_SECRET)
    stack = "https://STACK-1.palantirfoundry.com:443/"
    palantir = _use(_broker(tmp_path, "palantir", stack), "palantir", _grant("palantir"))
    assert palantir == FoundryCredentials(SP8_CLIENT_ID, SP8_SECRET)
    docker = _use(_broker(tmp_path, "docker"), "docker", _grant("docker"))
    assert isinstance(docker, DockerEntry) and docker.host == HOST["docker"]


@pytest.mark.parametrize("kind", SP8_KINDS)
@pytest.mark.parametrize(
    "field", ["job_id", "executor_id", "target_id", "expires_at", "credential_ref", "audience"]
)
def test_every_mismatch_is_refused_with_one_fixed_message(
    tmp_path: Path, kind: str, field: str
) -> None:
    value = {
        "expires_at": utcnow() - timedelta(seconds=1),
        "credential_ref": "another",
        "audience": OTHER_HOST[kind],
    }.get(field, uuid4())
    broker = _broker(tmp_path, kind)
    for call in (_use, _entry):
        with pytest.raises(CredentialUnavailable) as raised:
            call(broker, kind, _grant(kind, **{field: value}))
        _assert_fixed(raised.value, tmp_path)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_target_of_another_tenant_is_refused(tmp_path: Path, kind: str) -> None:
    with pytest.raises(CredentialUnavailable) as raised:
        _use(_broker(tmp_path, kind), kind, _grant(kind), tenant=uuid4())
    _assert_fixed(raised.value, tmp_path)


def _file(tmp_path: Path, entries: dict[str, Any]) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_an_entry_of_another_platform_is_refused(tmp_path: Path, kind: str) -> None:
    """Every other platform's entry under the reference and at the host the grant names."""
    databricks = {
        "tenant_id": str(TENANT),
        "host": HOST[kind],
        "token": CREDENTIAL_TOKEN,
        "service_principal": DEPLOYER,
    }
    others = [
        sp8_entry(tmp_path, other, HOST[kind], tenant=TENANT)
        for other in SP8_KINDS
        if other != kind
    ]
    for other in (databricks, *others):
        broker = CredentialBroker.from_file(_file(tmp_path, {f"{kind}-dev": other}))
        with pytest.raises(CredentialUnavailable) as raised:
            _use(broker, kind, _grant(kind))
        _assert_fixed(raised.value, tmp_path)
    # and this kind's entry for a Databricks job
    with pytest.raises(CredentialUnavailable):
        _broker(tmp_path, kind).token_for(
            _grant(kind), job_id=JOB, executor_id=EXECUTOR, target_id=TARGET, tenant_id=TENANT
        )


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_host_the_platforms_normalizer_refuses_is_refused(tmp_path: Path, kind: str) -> None:
    host = "https://example.com"
    with pytest.raises(CredentialUnavailable) as raised:
        _use(_broker(tmp_path, kind, host), kind, _grant(kind, audience=host))
    _assert_fixed(raised.value, tmp_path)


# ------------------------------------------------------------------ the secret file
@pytest.mark.parametrize("kind", SECRET_KINDS)
def test_the_secret_file_is_read_on_each_use_and_stripped(tmp_path: Path, kind: str) -> None:
    broker = _broker(tmp_path, kind)
    secret_file = tmp_path / f"{kind}.json.secret"
    for value in (SP8_SECRET, "rotated-secret"):
        secret_file.write_text(f" \t{value}\r\n", encoding="utf-8")
        assert _use(broker, kind, _grant(kind)).client_secret == value


@pytest.mark.parametrize("kind", SECRET_KINDS)
def test_a_missing_empty_or_unreadable_secret_file_is_refused(tmp_path: Path, kind: str) -> None:
    broker = _broker(tmp_path, kind)
    secret_file = tmp_path / f"{kind}.json.secret"
    for content in (b"", b" \n\t", b"\xff\xfe\xff"):
        secret_file.write_bytes(content)
        with pytest.raises(CredentialUnavailable) as raised:
            _use(broker, kind, _grant(kind))
        _assert_fixed(raised.value, tmp_path)
    secret_file.unlink()
    with pytest.raises(CredentialUnavailable) as raised:
        _use(broker, kind, _grant(kind))
    _assert_fixed(raised.value, tmp_path)


@pytest.mark.parametrize("kind", SECRET_KINDS)
def test_posix_refuses_a_secret_file_open_to_group_or_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    broker = _broker(tmp_path, kind)

    class Stat:
        st_mode = 0o100640

    monkeypatch.setattr(credentials, "POSIX", True)
    monkeypatch.setattr(credentials.os, "stat", lambda _: Stat())
    for mode in (0o100640, 0o100604):
        Stat.st_mode = mode
        with pytest.raises(CredentialUnavailable) as raised:
            _use(broker, kind, _grant(kind))
        _assert_fixed(raised.value, tmp_path)
    Stat.st_mode = 0o100600
    assert _use(broker, kind, _grant(kind)).client_secret == SP8_SECRET


@pytest.mark.skipif(os.name != "posix", reason="file modes are checked on POSIX only")
@pytest.mark.parametrize("kind", SECRET_KINDS)
def test_posix_refuses_a_real_secret_file_open_to_group_or_others(
    tmp_path: Path, kind: str
) -> None:
    broker = _broker(tmp_path, kind)
    secret_file = tmp_path / f"{kind}.json.secret"
    for mode in (0o640, 0o604):
        os.chmod(secret_file, mode)
        with pytest.raises(CredentialUnavailable):
            _use(broker, kind, _grant(kind))
    os.chmod(secret_file, 0o600)
    assert _use(broker, kind, _grant(kind)).client_secret == SP8_SECRET


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_the_secret_and_its_path_are_never_in_a_repr(tmp_path: Path, kind: str) -> None:
    broker = _broker(tmp_path, kind)
    entry, used = _entry(broker, kind, _grant(kind)), _use(broker, kind, _grant(kind))
    for shown in (repr(broker), repr(entry), str(entry), repr(used), str(used)):
        assert SP8_SECRET not in shown
        assert str(tmp_path) not in shown and "json.secret" not in shown


# ------------------------------------------------------------------ malformed entries
@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_malformed_entry_is_refused_without_echoing_it(tmp_path: Path, kind: str) -> None:
    good = sp8_entry(tmp_path, kind, HOST[kind], tenant=TENANT)
    bad = [{**good, "token": CREDENTIAL_TOKEN}, {**good, "platform": "oracle"}]
    bad += [{k: v for k, v in good.items() if k != key} for key in good]
    if kind == "fabric":
        bad.append({**good, "entra_tenant_id": "contoso.onmicrosoft.com"})
        bad.append({**good, "client_id": "{" + SP8_CLIENT_ID + "}"})
    for entry in bad:
        with pytest.raises(CredentialFileRefused) as raised:
            CredentialBroker.from_file(_file(tmp_path, {"ref": entry}))
        assert str(raised.value) == "a credentials file entry is invalid"
        assert raised.value.__cause__ is None


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_an_entry_mixed_with_another_organization_is_refused(tmp_path: Path, kind: str) -> None:
    entries = {
        f"{kind}-dev": sp8_entry(tmp_path, kind, HOST[kind], tenant=TENANT),
        "docker-other": sp8_entry(tmp_path, "docker", HOST["docker"], tenant=uuid4()),
    }
    with pytest.raises(CredentialFileRefused, match="one organization"):
        CredentialBroker.from_file(_file(tmp_path, entries))
