"""The runner's Databricks token provider over a stub gateway: it answers only for the host its
grant names (after normalization), never for another host or an unparsable one."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: stands in for a GatewayClient
from uuid import uuid4

import pytest

from shifde.adapters.native.contracts import CredentialGrant, JobLease, LeasedJob, Target, utcnow
from shifde.executor.credentials import CredentialBroker, CredentialUnavailable
from shifde.executor.runner import FakePlatform, LeaseState, Runner
from tests.executor.support import CREDENTIAL_TOKEN, credentials_file

HOST = "https://adb-1111111111111111.cloud.databricks.com"


class Stub:
    """A gateway that must never be called: the grant is held and still valid."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the gateway was called: {name}")


def test_the_databricks_provider_answers_for_the_grants_audience_only(tmp_path: Path) -> None:
    tenant, executor = uuid4(), uuid4()
    target = Target(tenant_id=tenant, id=uuid4(), host=HOST)
    job = LeasedJob(
        id=uuid4(),
        kind="apply",
        plan_id=uuid4(),
        deployment_id=uuid4(),
        target_id=target.id,
        deadline=None,
        attempts=1,
        max_attempts=1,
    )
    gateway: Any = Stub()
    runner = Runner(
        gateway,
        CredentialBroker.from_file(credentials_file(tmp_path, HOST, tenant=tenant)),
        executor,
        FakePlatform(environment="test"),
        tenant_id=tenant,
    )
    until = utcnow() + timedelta(seconds=45)
    lease = JobLease(job=job, lease_id=uuid4(), fence=1, lease_until=until, request_id=uuid4())
    state = LeaseState(lease=lease, lease_until=until)
    state.grant = CredentialGrant(
        job_id=job.id,
        executor_id=executor,
        target_id=target.id,
        credential_ref="databricks-dev",
        audience=HOST,
        expires_at=utcnow() + timedelta(seconds=30),
    )
    provider = runner._token_provider(state, target)
    for host in (
        "https://adb-2222222222222222.cloud.databricks.com",
        "http://adb-1111111111111111.cloud.databricks.com",
        "https://adb-1111111111111111.cloud.databricks.com:8443",
        "not a host",
        "",
    ):
        with pytest.raises(CredentialUnavailable):
            provider(host)
    assert provider(HOST) == CREDENTIAL_TOKEN
    assert provider("https://ADB-1111111111111111.Cloud.Databricks.com:443/") == CREDENTIAL_TOKEN
