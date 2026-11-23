"""The FDE SDK Snowflake adapter tests (1fd902b `tests/adapters/test_snowflake.py`), synchronous,
over `FakeSnowflakeNativePlatform`, adapted to SP6 design 6.2: stage `SOURCE`, a fixed `V1`, the
patch from inspect, no scaffold (the source is always rendered)."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from shifde.adapters.native.base import NeedsAttention, create_archive
from shifde.adapters.native.contracts import (
    ApplyRequest,
    DatabricksApplicationSpec,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ScaffoldRequest,
    SnowflakeApplicationSpec,
    ValidateRequest,
    VerifyRequest,
)
from shifde.adapters.native.snowflake import LIVE_VALIDATED, MATURITY
from shifde.adapters.native.snowflake.adapter import STEPS, SnowflakePatchLimit
from shifde.adapters.native.snowflake.source import identifier, validate_source
from tests.adapters.native.conftest import SF_FILES, Setup, SfSetup

PKG, APP = '"SHIFDE_PKG"', '"SHIFDE_APP"'


def _kind(entry: str) -> str:
    """A label for one logged statement or copy of the deployment sequence."""
    for prefix, label in (
        ("CREATE APPLICATION PACKAGE", "package"),
        ("CREATE SCHEMA", "schema"),
        ("CREATE STAGE", "stage"),
        ("COPY ", "upload"),
        ("ALTER APPLICATION PACKAGE", "version"),
        ("CREATE APPLICATION ", "application"),
        ("ALTER APPLICATION ", "application"),
        ("GRANT APPLICATION ROLE", "grant"),
    ):
        if entry.startswith(prefix):
            return label
    if entry.startswith("LIST "):
        return "setup-check" if " PATTERN = " in entry else "list"
    return "read"


def _sequence(log: list[str]) -> list[str]:
    return [kind for kind in map(_kind, log) if kind != "read"]


def _second_release() -> dict[str, bytes]:
    return {**SF_FILES, "ui/streamlit_app.py": SF_FILES["ui/streamlit_app.py"] + b"# release 2\n"}


def test_the_adapter_is_experimental() -> None:
    assert MATURITY == "experimental" and LIVE_VALIDATED is False


def test_build_packages_offline_and_rejects_local_hooks(sf: SfSetup) -> None:
    artifact = sf.artifact()
    assert artifact.archive.digest == artifact.source_digest
    assert artifact.spec == SnowflakeApplicationSpec()
    for name in ("snowflake.yml", "snowflake.local.yml", "ui/config.toml", ".env", "ui/.hidden"):
        injected = sf.build.artifacts.put(
            sf.build.tenant_id,
            create_archive({**SF_FILES, name: b"native_app:\n  post_deploy: [rm -rf /]\n"}),
        )
        report = sf.adapter.validate(
            ValidateRequest(spec=SnowflakeApplicationSpec(), source=injected), sf.build
        )
        assert not report.passed, name
        with pytest.raises(ValueError, match="validation failed"):
            sf.artifact(files={**SF_FILES, name: b"x"})
    assert sf.platform.log == []  # building never reaches the account


@pytest.mark.parametrize("missing", ["manifest.yml", "setup.sql", "ui/environment.yml"])
def test_the_managed_files_are_required(missing: str) -> None:
    with pytest.raises(ValueError, match="missing"):
        validate_source({k: v for k, v in SF_FILES.items() if k != missing})


@pytest.mark.parametrize(
    "name", ["APP; DROP DATABASE X", 'APP"X', "DB.SCHEMA", "name with space", "../APP", "1APP", ""]
)
def test_native_identifiers_reject_injection_and_ambiguous_names(name: str) -> None:
    with pytest.raises(ValueError):
        identifier(name)


def test_identifiers_are_uppercased() -> None:
    assert identifier("shifde_app$1") == "SHIFDE_APP$1"


def test_there_is_no_scaffold(sf: SfSetup) -> None:
    with pytest.raises(ValueError, match="rendered"):
        sf.adapter.scaffold(ScaffoldRequest(spec=SnowflakeApplicationSpec()), sf.build)


def test_a_databricks_spec_is_refused_before_any_platform_call(sf: SfSetup, setup: Setup) -> None:
    source = sf.build.artifacts.put(sf.build.tenant_id, create_archive(SF_FILES))
    with pytest.raises(ValueError, match="snowflake.native-app/v1"):
        sf.adapter.validate(
            ValidateRequest(spec=DatabricksApplicationSpec(), source=source), sf.build
        )
    databricks = setup.artifact()
    with pytest.raises(ValueError, match="snowflake.native-app/v1"):
        sf.adapter.plan(PlanRequest(artifact=databricks), setup.read)
    assert sf.platform.log == []


def test_the_databricks_adapter_refuses_a_snowflake_spec(setup: Setup, sf: SfSetup) -> None:
    source = setup.build.artifacts.put(setup.build.tenant_id, create_archive(SF_FILES))
    artifact = sf.artifact().model_copy(
        update={"tenant_id": setup.read.tenant_id, "archive": source}
    )
    with pytest.raises(ValueError, match="databricks.app/v1"):
        setup.adapter.plan(PlanRequest(artifact=artifact), setup.read)
    with pytest.raises(ValueError, match="databricks.app/v1"):
        setup.adapter.scaffold(ScaffoldRequest(spec=SnowflakeApplicationSpec()), setup.build)
    assert setup.platform.actions == []


def test_deploy_follows_the_eight_steps_then_observes_and_verifies(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    assert [step.key for step in plan.steps] == list(STEPS)
    assert plan.details["patch"] == 0 and plan.details["version"] == "V1"
    receipt = sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert _sequence(sf.platform.log) == [
        "package",
        "schema",
        "stage",
        "list",
        "upload",
        "list",
        "setup-check",
        "version",
        "application",
        "grant",
    ]
    digest = artifact.archive.digest
    location = f'@{PKG}."SHIFDE"."SOURCE"/{digest}/'
    assert sf.platform.writes() == [
        f"CREATE APPLICATION PACKAGE {PKG} ENABLE_RELEASE_CHANNELS = FALSE",
        f'CREATE SCHEMA IF NOT EXISTS {PKG}."SHIFDE"',
        f'CREATE STAGE IF NOT EXISTS {PKG}."SHIFDE"."SOURCE" ENCRYPTION = (TYPE = \'SNOWFLAKE_SSE\')',
        next(e for e in sf.platform.log if e.startswith("COPY ")),
        f"ALTER APPLICATION PACKAGE {PKG} ADD VERSION V1 USING '{location}' "
        f"LABEL = 'shifde {digest[:12]}'",
        f"CREATE APPLICATION {APP} FROM APPLICATION PACKAGE {PKG} USING VERSION V1 PATCH 0",
        f'GRANT APPLICATION ROLE {APP}."APP_USER" TO ROLE "DEPLOY_ROLE"',
    ]
    assert next(e for e in sf.platform.log if e.startswith("COPY ")).endswith(
        f"@SHIFDE_PKG.SHIFDE.SOURCE/{digest}/"
    )
    assert {r.key for r in receipt.resources} == {"package", "stage", "version", "application"}
    assert receipt.operation_handles["patch"] == "0"
    assert receipt.operation_handles["digest"] == digest
    assert receipt.operation_handles["platform"] == "fake"
    staged = sf.account.stages["SHIFDE_PKG"]["files"]
    assert {path.removeprefix(f"{digest}/") for path in staged} == set(SF_FILES)
    assert sf.adapter.observe(ObserveRequest(receipt=receipt), sf.read).state == "succeeded"
    report = sf.adapter.verify(VerifyRequest(receipt=receipt), sf.read)
    assert report.passed
    assert [(c.name, c.status) for c in report.checks] == [
        ("native-version", "passed"),
        ("runtime-health", "passed"),
        ("app-deep-health", "passed"),
    ]


def test_a_second_release_is_patch_1_and_an_upgrade(sf: SfSetup) -> None:
    _, _, first = sf.deploy()
    original = {r.key: r for r in first.resources}
    nxt = sf.with_owned(first)
    artifact, plan, deploy = nxt.prepare(first.application_id, _second_release())
    assert plan.details["patch"] == 1
    assert [s.action for s in plan.steps][-3:-1] == ["update", "update"]
    before = len(sf.platform.writes())
    receipt = nxt.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    digest = artifact.archive.digest
    location = f'@{PKG}."SHIFDE"."SOURCE"/{digest}/'
    writes = sf.platform.writes()[before:]
    assert (
        f"ALTER APPLICATION PACKAGE {PKG} ADD PATCH 1 FOR VERSION V1 USING '{location}' "
        + (f"LABEL = 'shifde {digest[:12]}'")
        in writes
    )
    assert f"ALTER APPLICATION {APP} UPGRADE USING VERSION V1 PATCH 1" in writes
    assert not any(
        w.startswith(("CREATE APPLICATION ", "CREATE APPLICATION PACKAGE")) for w in writes
    )
    assert {r.key: r for r in receipt.resources} == original  # identities preserved
    assert receipt.operation_handles["patch"] == "1"
    assert sf.account.applications["SHIFDE_APP"]["patch"] == 1
    assert nxt.adapter.verify(VerifyRequest(receipt=receipt), nxt.read).passed
    assert not any("DROP " in entry for entry in sf.platform.log)


def test_patch_130_is_refused_at_plan_time(sf: SfSetup) -> None:
    _, _, first = sf.deploy()
    versions = sf.account.versions["SHIFDE_PKG"]
    for patch in range(1, 130):
        versions.append({**versions[0], "patch": patch, "created_on": f"seeded {patch}"})
    writes = len(sf.platform.writes())
    nxt = sf.with_owned(first)
    with pytest.raises(SnowflakePatchLimit) as refused:
        nxt.prepare(first.application_id, _second_release())
    assert refused.value.code == "snowflake_patch_limit"
    assert str(refused.value) == "this package has no free patch; a new package is needed"
    assert isinstance(refused.value, ValueError)
    assert len(sf.platform.writes()) == writes  # no write


def test_patch_129_is_still_planned(sf: SfSetup) -> None:
    _, _, first = sf.deploy()
    versions = sf.account.versions["SHIFDE_PKG"]
    for patch in range(1, 129):
        versions.append({**versions[0], "patch": patch, "created_on": f"seeded {patch}"})
    _, plan, _ = sf.with_owned(first).prepare(first.application_id, _second_release())
    assert plan.details["patch"] == 129


def test_unknown_install_acceptance_does_not_retry_native_install(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    sf.platform.faults.add("fail_after_accept:application-apply")
    with pytest.raises(TimeoutError):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert sf.account.applications
    with pytest.raises(NeedsAttention):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    installs = [w for w in sf.platform.writes() if w.startswith("CREATE APPLICATION ")]
    assert len([w for w in installs if "FROM APPLICATION PACKAGE" in w]) == 1
    assert sf.adapter.reconcile(ReconcileRequest(plan=plan), sf.read).state == "ambiguous"


def test_a_completed_apply_returns_its_saved_receipt(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    receipt = sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    log = len(sf.platform.log)
    assert sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy) == receipt
    assert len(sf.platform.log) == log


def test_existing_unowned_package_is_not_adopted(sf: SfSetup) -> None:
    sf.deploy()
    with pytest.raises(PermissionError, match="unowned"):
        sf.prepare()


def test_a_missing_owned_application_is_drift(sf: SfSetup) -> None:
    _, _, first = sf.deploy()
    del sf.account.applications["SHIFDE_APP"]
    with pytest.raises(ValueError, match="drift"):
        sf.with_owned(first).prepare(first.application_id, _second_release())


def test_a_foreign_object_appearing_mid_apply_is_not_recorded_as_owned(
    sf: SfSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, plan, deploy = sf.prepare()
    original = sf.platform._create_schema

    def create_schema(account, role, warehouse, package):  # type: ignore[no-untyped-def]
        account.applications["SHIFDE_APP"] = {
            "name": "SHIFDE_APP",
            "created_on": "foreign-created",
            "owner": "FOREIGN_ROLE",
            "source": "OTHER_PACKAGE",
            "source_type": "APPLICATION PACKAGE",
            "version": "V1",
            "patch": 0,
            "label": "foreign",
            "upgrade_state": "COMPLETE",
        }
        return original(account, role, warehouse, package)

    monkeypatch.setattr(sf.platform, "_create_schema", create_schema)
    with pytest.raises(NeedsAttention, match="object"):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert deploy.journal.completed("package-create") is not None
    assert deploy.journal.completed("schema-create") is None


def test_a_changed_account_since_planning_is_refused_before_writes(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    sf.account.packages["SHIFDE_PKG"] = {"name": "SHIFDE_PKG", "created_on": "x", "owner": "R"}
    with pytest.raises(PermissionError):
        sf.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert sf.platform.writes() == []


def test_stale_and_altered_artifacts_are_rejected_before_mutation(sf: SfSetup) -> None:
    artifact, plan, deploy = sf.prepare()
    with pytest.raises(ValueError):
        sf.adapter.apply(
            ApplyRequest(artifact=artifact.model_copy(update={"spec_digest": "c" * 64}), plan=plan),
            deploy,
        )
    other = sf.artifact(artifact.application_id, _second_release())
    with pytest.raises(ValueError):
        sf.adapter.apply(ApplyRequest(artifact=other, plan=plan), deploy)
    with pytest.raises(ValueError, match="toolchain"):
        sf.adapter.apply(
            ApplyRequest(artifact=artifact, plan=plan), replace(deploy, toolchain_digest="d" * 64)
        )
    assert sf.platform.writes() == []


def test_inspect_reports_owned_objects_by_bound_name(sf: SfSetup) -> None:
    app_id = uuid4()
    empty = sf.adapter.inspect(
        InspectRequest(application_id=app_id, app_name="shifde_app"), sf.read
    )
    assert empty.resources == []
    with pytest.raises(PermissionError):
        sf.adapter.inspect(InspectRequest(application_id=app_id, app_name="OTHER_APP"), sf.read)
    _, _, receipt = sf.deploy(app_id)
    found = sf.adapter.inspect(
        InspectRequest(application_id=app_id, app_name="SHIFDE_APP"), sf.read
    )
    assert set(found.resources) == set(receipt.resources)


def test_reconcile_is_always_ambiguous(sf: SfSetup) -> None:
    _, plan, _ = sf.prepare()
    outcome = sf.adapter.reconcile(ReconcileRequest(plan=plan), sf.read)
    assert outcome.state == "ambiguous"
    assert outcome.evidence == "inspect query history; no automatic replay"


def test_capabilities_report_the_pinned_cli_and_metadata(sf: SfSetup) -> None:
    report = sf.adapter.capabilities(sf.read)
    assert report.supports("cli") and report.supports("metadata")
    assert report.application_kinds == ["snowflake.native-app/v1"]
