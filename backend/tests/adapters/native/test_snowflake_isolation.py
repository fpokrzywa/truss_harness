"""Snowflake native adapter isolation (SP6 design section 8): another tenant's artifact, receipt or
plan, another target's receipt or plan, and a credential of another account host are refused with
`PermissionError` before any statement reaches the account; the SQL API client talks only to
`*.snowflakecomputing.com`; objects of the bound names are never adopted."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from shifde.adapters.native.base import ReadContext
from shifde.adapters.native.contracts import (
    ApplyRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    Target,
    VerifyRequest,
)
from shifde.adapters.native.snowflake.clients import SnowflakeSqlApi
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from tests.adapters.native.conftest import (
    SF_BINDINGS,
    SF_HOST,
    SfSetup,
    make_sf_setup,
    sf_credential,
)


def _same_tenant_other_target(sf: SfSetup) -> ReadContext:
    target = Target(tenant_id=sf.read.tenant_id, host=SF_HOST, bindings=dict(SF_BINDINGS))
    return ReadContext(tenant_id=sf.read.tenant_id, target=target)


def test_an_artifact_of_another_tenant_is_refused(sf: SfSetup, sf_other: SfSetup) -> None:
    theirs = sf_other.artifact()
    with pytest.raises(PermissionError, match="another tenant"):
        sf.adapter.plan(PlanRequest(artifact=theirs), sf.read)
    _, plan, deploy = sf.prepare()
    sent = list(sf.platform.log)
    with pytest.raises(PermissionError):
        sf.adapter.apply(ApplyRequest(artifact=theirs, plan=plan), deploy)
    assert sf.platform.log == sent


def test_a_receipt_of_another_tenant_or_target_is_refused(sf: SfSetup, sf_other: SfSetup) -> None:
    _, _, theirs = sf_other.deploy()
    _, _, mine = sf.deploy()
    sent = list(sf.platform.log)
    other_target = _same_tenant_other_target(sf)
    for receipt, ctx in ((theirs, sf.read), (mine, other_target)):
        with pytest.raises(PermissionError, match="another tenant or target"):
            sf.adapter.observe(ObserveRequest(receipt=receipt), ctx)
        with pytest.raises(PermissionError, match="another tenant or target"):
            sf.adapter.verify(VerifyRequest(receipt=receipt), ctx)
    assert sf.platform.log == sent


def test_a_plan_of_another_tenant_or_target_is_refused(sf: SfSetup, sf_other: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    _, their_plan, _ = sf_other.prepare()
    other = _same_tenant_other_target(sf)
    _, other_plan, _ = replace(sf, read=other).prepare(artifact.application_id)
    sent = list(sf.platform.log)
    for foreign in (their_plan, other_plan):
        with pytest.raises(PermissionError, match="another tenant or target"):
            sf.adapter.apply(ApplyRequest(artifact=artifact, plan=foreign), deploy)
        with pytest.raises(PermissionError):
            sf.adapter.reconcile(ReconcileRequest(plan=foreign), sf.read)
    assert sf.platform.log == sent


def test_a_credential_of_another_host_is_refused_before_sending(
    sf_platform: FakeSnowflakeNativePlatform,
) -> None:
    sf = make_sf_setup(sf_platform, credential_host="https://acct2.snowflakecomputing.com")
    with pytest.raises(PermissionError, match="another account host"):
        sf.prepare()
    for host in ("https://evil.example.com", "not a url"):
        stray = make_sf_setup(sf_platform, credential_host=host)
        with pytest.raises(PermissionError):
            stray.prepare()
    assert sf_platform.log == []


def test_a_credential_host_is_compared_normalized(sf_platform: FakeSnowflakeNativePlatform) -> None:
    sf = make_sf_setup(sf_platform, credential_host="https://ACCT1.snowflakecomputing.com:443/")
    sf.prepare()
    assert sf_platform.log


@pytest.mark.parametrize(
    "host",
    [
        "https://evil.example.com",
        "https://acct.snowflakecomputing.com.evil.com",
        "http://acct.snowflakecomputing.com",
        "https://acct.snowflakecomputing.cn",
        "https://10.0.0.1",
    ],
)
def test_the_sql_api_talks_only_to_snowflakecomputing_com(host: str) -> None:
    target = Target(tenant_id=uuid4(), host=SF_HOST).model_copy(update={"host": host})
    credential = sf_credential(host)
    with pytest.raises(ValueError):
        SnowflakeSqlApi(target, credential_provider=lambda: credential)


def test_an_unowned_package_or_application_is_never_adopted(
    sf: SfSetup, sf_platform: FakeSnowflakeNativePlatform
) -> None:
    _, _, receipt = sf.deploy()
    with pytest.raises(PermissionError, match="unowned"):
        sf.prepare()  # owns nothing: refuses the package and application of the bound names
    owned_by_other_app = tuple(r.model_copy(update={"native_id": "x"}) for r in receipt.resources)
    stranger = replace(sf, read=replace(sf.read, owned_resources=owned_by_other_app))
    with pytest.raises(PermissionError, match="unowned"):
        stranger.prepare(receipt.application_id)


def test_names_come_only_from_the_target_bindings(sf: SfSetup) -> None:
    artifact, plan, _ = sf.prepare()
    assert {k: plan.details[k] for k in SF_BINDINGS} == SF_BINDINGS
    renamed = replace(
        sf,
        read=replace(
            sf.read,
            target=sf.read.target.model_copy(
                update={"bindings": {**SF_BINDINGS, "application": "x; DROP"}}
            ),
        ),
    )
    with pytest.raises(ValueError, match="identifier"):
        renamed.adapter.plan(PlanRequest(artifact=artifact), renamed.read)
    missing = replace(
        sf,
        read=replace(
            sf.read,
            target=sf.read.target.model_copy(update={"bindings": {"package": "P"}}),
        ),
    )
    with pytest.raises(ValueError, match="binding is missing"):
        missing.adapter.plan(PlanRequest(artifact=artifact), missing.read)
