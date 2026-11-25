"""`FakeSnowflakeNativePlatform`: the environment refusal, every fault switch through the adapter,
the staged deep health, and the SQL API answers (unknown and multi-statement bodies, the JWT
check)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any  # Any: the deep-health document

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
    SF_FILES,
    SF_HOST,
    SF_KEY,
    SF_RELEASE,
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
    report = sf.adapter.verify(VerifyRequest(receipt=receipt, release_digest=SF_RELEASE), sf.read)
    assert not report.passed
    checks = {c.name: c for c in report.checks}
    assert checks["native-version"].status == "passed"
    assert checks["runtime-health"].status == "passed"
    deep = checks["app-deep-health"]
    assert deep.status == "failed"
    assert deep.evidence.startswith("app-deep-health: ")
    assert "serving_endpoint: the app cannot use Cortex model mistral-large2" in deep.evidence


def _call(sf: SfSetup) -> Any:  # Any: the deep-health document
    gateway = sf.platform.gateway(sf.read.target, lambda: sf.credential)
    rows = gateway.execute(
        'CALL "SHIFDE_APP"."APP_PUBLIC"."DEEP_HEALTH"()', warehouse="APP_WH"
    ).rows
    return json.loads(rows[0]["deep_health"])


def test_the_staged_deep_health_answers_with_the_sp5_shape(sf: SfSetup) -> None:
    artifact, _, _ = sf.deploy()
    modules, options = set(sys.modules), dict(sys._xoptions)
    tables = ("DEMO_DB.PUBLIC.CUSTOMERS", "DEMO_DB.PUBLIC.ORDERS", "DEMO_DB.SALES.INVOICES")
    names = ["warehouse", "serving_endpoint", *(f"table:{t}" for t in tables)]
    report = _call(sf)
    assert report == {
        "status": "ok",
        "generator_version": "shifde.appgen.snowflake/1.0",
        "release_digest": SF_RELEASE,
        "checks": [{"name": n, "status": "passed", "reason": None} for n in names],
    }
    assert artifact.archive.digest != SF_RELEASE
    added = set(sys.modules) - modules
    assert not any("deep_health" in name for name in added) and sys._xoptions == options


def _handler(body: str) -> dict[str, bytes]:
    """The sample package with another handler source."""
    return {**SF_FILES, "ui/deep_health.py": body.encode()}


def _deep_check(sf: SfSetup, files: dict[str, bytes] | None = None) -> Any:  # Any: a Check
    _, _, receipt = sf.deploy(files=files)
    report = sf.adapter.verify(VerifyRequest(receipt=receipt, release_digest=SF_RELEASE), sf.read)
    return {c.name: c for c in report.checks}["app-deep-health"]


def test_deep_health_is_the_staged_handlers_answer(sf: SfSetup) -> None:
    answer = {
        "status": "ok",
        "release_digest": SF_RELEASE,
        "checks": [{"name": "mine", "status": "failed", "reason": "staged answer"}],
    }
    check = _deep_check(sf, _handler(f"def run(session):\n    return {answer!r}\n"))
    assert check.status == "failed"
    assert check.evidence == "app-deep-health: mine: staged answer"
    assert _call(sf) == answer


@pytest.mark.parametrize(
    "files",
    [
        {k: v for k, v in SF_FILES.items() if k != "ui/deep_health.py"},
        {k: v for k, v in SF_FILES.items() if k != "ui/config/agent.json"},
        {**SF_FILES, "setup.sql": SF_FILES["setup.sql"].replace(b"IMPORTS =", b"IMPORTED =")},
        {
            **SF_FILES,
            "setup.sql": SF_FILES["setup.sql"].replace(b"'/ui/deep_health.py', ", b""),
        },
        _handler("raise RuntimeError('secret-text')\n"),
        _handler("def run(session):\n    raise ValueError('secret-text')\n"),
        _handler("def run(session):\n    raise SystemExit('secret-text')\n"),
        _handler("def run(session):\n    return 'secret-text'\n"),
        _handler("def run(session):\n    return {'secret-text': object()}\n"),
        _handler("x = 1\n"),
    ],
    ids=[
        "no-handler-file",
        "unstaged-import",
        "no-imports",
        "handler-not-imported",
        "module-raises",
        "run-raises",
        "run-exits",
        "not-an-object",
        "not-json",
        "no-run",
    ],
)
def test_a_handler_that_cannot_answer_fails_the_check_without_its_text(
    sf: SfSetup, files: dict[str, bytes]
) -> None:
    check = _deep_check(sf, files)
    assert (check.status, check.evidence) == ("failed", "app-deep-health: call failed")
    with pytest.raises(RuntimeError, match="HTTP 422") as refused:
        _call(sf)
    assert "secret-text" not in str(refused.value)
    assert "snowflake_import_directory" not in sys._xoptions


def test_the_call_error_carries_no_exception_text(sf: SfSetup) -> None:
    sf.deploy(files=_handler("def run(session):\n    raise ValueError('secret-text')\n"))
    answers: list[str] = []
    submit = sf.platform._submit

    def record(host: str, body: Any) -> Any:  # Any: SQL API JSON, an httpx.Response
        response = submit(host, body)
        answers.append(response.text)
        return response

    sf.platform._submit = record  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        _call(sf)
    assert len(answers) == 1 and "secret-text" not in answers[0]


@pytest.mark.parametrize(
    ("fault", "evidence"),
    [
        (
            "table_denied:DEMO_DB.PUBLIC.ORDERS",
            "table:DEMO_DB.PUBLIC.ORDERS: the app cannot read DEMO_DB.PUBLIC.ORDERS",
        ),
        ("cortex_denied", "serving_endpoint: the app cannot use Cortex model mistral-large2"),
    ],
)
def test_a_data_fault_fails_only_its_own_hosts_deep_health(
    sf: SfSetup, sf_other: SfSetup, fault: str, evidence: str
) -> None:
    sf.platform.data(SF_HOST).faults.add(fault)
    check = _deep_check(sf)
    assert check.status == "failed" and evidence in check.evidence
    assert _deep_check(sf_other).status == "passed"


def test_a_model_the_account_does_not_have_fails_the_cortex_check(sf: SfSetup) -> None:
    sf.platform.data(SF_HOST).cortex.clear()
    check = _deep_check(sf)
    assert check.status == "failed" and "serving_endpoint: " in check.evidence


def test_deep_health_runs_the_installed_patchs_own_files(sf: SfSetup) -> None:
    _, _, first = sf.deploy()
    nxt = sf.with_owned(first)
    answer = {"status": "ok", "release_digest": "2" * 64, "checks": [{"name": "second"}]}
    files = _handler(f"def run(session):\n    return {answer!r}\n")
    artifact, plan, deploy = nxt.prepare(first.application_id, files)
    assert _call(sf)["release_digest"] == SF_RELEASE  # patch 0 until the upgrade
    nxt.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert _call(sf) == answer


# ------------------------------------------------------------------ the release digest
def test_deep_health_must_answer_the_requests_release_digest(sf: SfSetup) -> None:
    artifact, _, receipt = sf.deploy()
    verdicts = {}
    for digest in (SF_RELEASE, artifact.archive.digest, "3" * 64):
        report = sf.adapter.verify(VerifyRequest(receipt=receipt, release_digest=digest), sf.read)
        verdicts[digest] = report.passed
    assert verdicts == {SF_RELEASE: True, artifact.archive.digest: False, "3" * 64: False}


def test_a_handler_answering_the_artifact_digest_fails(sf: SfSetup) -> None:
    """The stage directory is the artifact digest; deep health must never be compared with it."""
    files = _handler(
        "def run(session):\n"
        "    import pathlib, sys\n"
        "    base = pathlib.Path(sys._xoptions['snowflake_import_directory'])\n"
        "    return {'status': 'ok', 'release_digest': DIGEST, 'checks': [\n"
        "        {'name': str((base / 'agent.json').is_file()), 'status': 'passed'}]}\n"
        "DIGEST = ''\n"
    )
    artifact, _, receipt = sf.deploy(files=files)
    staged = sf.account.stages["SHIFDE_PKG"]["files"]
    handler = staged[f"{artifact.archive.digest}/ui/deep_health.py"]
    handler["data"] = handler["data"].replace(
        b"DIGEST = ''", f"DIGEST = '{artifact.archive.digest}'".encode()
    )
    assert _call(sf)["checks"][0]["name"] == "True"
    report = sf.adapter.verify(VerifyRequest(receipt=receipt, release_digest=SF_RELEASE), sf.read)
    deep = {c.name: c for c in report.checks}["app-deep-health"]
    assert deep.status == "failed" and "release digest differs" in deep.evidence


def test_verify_without_a_release_digest_fails_without_a_call(sf: SfSetup) -> None:
    _, _, receipt = sf.deploy()
    sent = len(sf.platform.log)
    report = sf.adapter.verify(VerifyRequest(receipt=receipt), sf.read)
    checks = {c.name: c for c in report.checks}
    assert checks["native-version"].status == checks["runtime-health"].status == "passed"
    assert checks["app-deep-health"].status == "failed"
    assert not any(entry.startswith("CALL ") for entry in sf.platform.log[sent:])


@pytest.mark.parametrize("checks", [[], None, "passed", {"name": "a"}, ["passed"]])
def test_a_report_without_checks_fails(sf: SfSetup, checks: Any) -> None:
    answer = {"status": "ok", "release_digest": SF_RELEASE, "checks": checks}
    check = _deep_check(sf, _handler(f"def run(session):\n    return {answer!r}\n"))
    assert check.status == "failed"


def test_a_report_with_no_checks_key_fails(sf: SfSetup) -> None:
    answer = {"status": "ok", "release_digest": SF_RELEASE}
    check = _deep_check(sf, _handler(f"def run(session):\n    return {answer!r}\n"))
    assert check.status == "failed" and "checks: failed" in check.evidence


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
