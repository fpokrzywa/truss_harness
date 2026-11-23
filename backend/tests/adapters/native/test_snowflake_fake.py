"""`FakeSnowflakeNativePlatform`: the environment refusal, every fault switch through the adapter,
and the SQL API answers (unknown and multi-statement bodies, the JWT check)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shifde.adapters.native.base import NeedsAttention
from shifde.adapters.native.contracts import (
    ApplyRequest,
    ReconcileRequest,
    VerifyRequest,
)
from shifde.adapters.native.snowflake.adapter import STEPS
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from tests.adapters.native.conftest import (
    SF_KEY,
    SfSetup,
    make_sf_setup,
    sf_credential,
)

VERSION_OR_APP = (
    "ALTER APPLICATION PACKAGE",
    'CREATE APPLICATION "',
    'ALTER APPLICATION "',
)


def _platform(*faults: str, hang_timeout: float = 0.05) -> FakeSnowflakeNativePlatform:
    return FakeSnowflakeNativePlatform(
        environment="test",
        public_key=SF_KEY.public_key(),
        user="deployer",
        faults=set(faults),
        hang_timeout=hang_timeout,
    )


@pytest.mark.parametrize("environment", ["production", "staging", "", "Development", "TEST"])
def test_the_fake_refuses_outside_development_and_test(environment: str) -> None:
    with pytest.raises(RuntimeError, match="development or test"):
        FakeSnowflakeNativePlatform(environment=environment, public_key=SF_KEY.public_key())


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_fake_runs_in_development_and_test(environment: str) -> None:
    FakeSnowflakeNativePlatform(environment=environment, public_key=SF_KEY.public_key())


def test_an_unknown_fault_switch_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown fault"):
        _platform("fail_after_accept:nothing")


@pytest.mark.parametrize("step", STEPS)
def test_fail_after_accept_is_never_resent(step: str) -> None:
    sf = make_sf_setup(_platform(f"fail_after_accept:{step}"))
    artifact, plan, deploy = sf.prepare()
    with pytest.raises(TimeoutError):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert deploy.journal.has_intent(step) and deploy.journal.completed(step) is None
    sent = list(sf.platform.log)
    with pytest.raises(NeedsAttention):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert sf.platform.log == sent  # nothing re-sent, that step's statement included
    assert sf.adapter.reconcile(ReconcileRequest(plan=plan), sf.read).state == "ambiguous"


def test_fail_after_accept_lets_the_write_take_effect() -> None:
    sf = make_sf_setup(_platform("fail_after_accept:package-create"))
    artifact, plan, deploy = sf.prepare()
    with pytest.raises(TimeoutError):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert "SHIFDE_PKG" in sf.account.packages


def test_setup_mismatch_stops_before_any_version_or_application() -> None:
    sf = make_sf_setup(_platform("setup_mismatch"))
    artifact, plan, deploy = sf.prepare()
    with pytest.raises(ValueError, match="setup files differ"):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert deploy.journal.completed("stage-upload") is not None
    assert deploy.journal.completed("setup-validate") is None
    assert not any(w.startswith(VERSION_OR_APP) for w in sf.platform.writes())
    assert not sf.account.versions and not sf.account.applications


@pytest.mark.parametrize("fault", ["foreign_package", "foreign_application"])
def test_a_foreign_object_of_the_bound_name_refuses_the_plan(fault: str) -> None:
    sf = make_sf_setup(_platform(fault))
    with pytest.raises(PermissionError):
        sf.prepare()
    assert sf.platform.writes() == []


def test_hang_raises_timeout_after_release() -> None:
    sf = make_sf_setup(_platform("hang", hang_timeout=30))
    artifact, plan, deploy = sf.prepare()
    timer = threading.Timer(0.2, sf.platform.release)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(TimeoutError, match="never confirmed"):
            sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 20  # released, not timed out
    assert "SHIFDE_PKG" in sf.account.packages  # the write took effect
    with pytest.raises(NeedsAttention):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def test_hang_times_out_without_release() -> None:
    sf = make_sf_setup(_platform("hang"))
    artifact, plan, deploy = sf.prepare()
    with pytest.raises(TimeoutError):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def test_deep_health_bad_fails_verify_with_the_check_and_reason() -> None:
    sf = make_sf_setup(_platform("deep_health_bad"))
    _, _, receipt = sf.deploy()
    report = sf.adapter.verify(VerifyRequest(receipt=receipt), sf.read)
    assert not report.passed
    checks = {c.name: c for c in report.checks}
    assert checks["native-version"].status == "passed"
    assert checks["runtime-health"].status == "passed"
    deep = checks["app-deep-health"]
    assert deep.status == "failed"
    assert deep.evidence.startswith("app-deep-health: ")
    assert "serving_endpoint: the app cannot use Cortex model mistral-large2" in deep.evidence


def test_the_stubbed_deep_health_has_the_sp5_shape(sf: SfSetup) -> None:
    artifact, _, _ = sf.deploy()
    gateway = sf.platform.gateway(sf.read.target, lambda: sf.credential)
    rows = gateway.execute(
        'CALL "SHIFDE_APP"."APP_PUBLIC"."DEEP_HEALTH"()', warehouse="APP_WH"
    ).rows
    report = json.loads(rows[0]["deep_health"])
    assert report == {
        "status": "ok",
        "generator_version": "shifde.appgen.snowflake/1.0",
        "release_digest": artifact.archive.digest,
        "checks": [
            {"name": "warehouse", "status": "passed", "reason": None},
            {"name": "serving_endpoint", "status": "passed", "reason": None},
        ],
    }


@pytest.mark.parametrize(
    "statement", ["DROP DATABASE SHIFDE_PKG", "SELECT 1; SELECT 2", "SELECT 1"]
)
def test_unknown_and_multi_statement_bodies_are_sql_api_errors(sf: SfSetup, statement: str) -> None:
    gateway = sf.platform.gateway(sf.read.target, lambda: sf.credential)
    with pytest.raises(RuntimeError, match="HTTP 422"):
        gateway.execute(statement)
    assert sf.account.packages == {}


def test_a_jwt_of_another_key_is_401(sf: SfSetup) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    gateway = sf.platform.gateway(sf.read.target, lambda: sf_credential(sf.read.tenant_id, pem=pem))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        gateway.execute("SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE")
    assert sf.platform.log == []


def test_the_copier_refuses_an_overwrite(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    copier = sf.platform.copier(sf.read.target)
    assert copier.platform == "fake" and copier.check_version() == "3.33.0"
    with tempfile.TemporaryDirectory() as temporary:
        Path(temporary, "setup.sql").write_bytes(b"other")
        with pytest.raises(RuntimeError, match="exit code 1"):
            copier.copy(Path(temporary), "SHIFDE_PKG.SHIFDE.SOURCE", artifact.archive.digest)
    staged = sf.account.stages["SHIFDE_PKG"]["files"][f"{artifact.archive.digest}/setup.sql"]
    assert staged["data"] != b"other"
