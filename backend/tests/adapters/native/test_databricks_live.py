"""The opt-in live validation of the Databricks adapter (design SP2 section 7 item 10, settled
call 19; runbook `docs/runbooks/databricks-live-validation.md`).

`test_real_adapter_against_a_development_workspace` drives the real adapter (pinned CLI and
workspace REST reads, as `RealPlatform.adapter` builds it) through capabilities, scaffold,
validate, build, plan, apply, observe and verify against the owner's development workspace. It
runs only when `<checkout root>/.devdata/databricks.env` exists and `-m live` selects it; else it
is skipped, so it never runs in CI, `factory/checks.sh` or a default pytest run. `apply` is called
once and never retried. The integration stays `experimental`: a passing run does not change that.

The env file is read here, never from the process environment. No value of it appears in a
message, skip reason, exception, log line or `repr`: every refusal names only the key or rule.
The parsing tests at the end need no network and no database.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from _pytest.mark.expression import Expression
from dotenv import dotenv_values
from pydantic import SecretStr

from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
)
from shifde.adapters.native.contracts import (
    ApplyRequest,
    BuildRequest,
    DatabricksApplicationSpec,
    ObserveRequest,
    PlanRequest,
    ScaffoldRequest,
    Target,
    ValidateRequest,
    VerifyRequest,
)
from shifde.adapters.native.databricks.adapter import PRINCIPAL_BINDING, WAREHOUSE_BINDING
from shifde.executor.runner import RealPlatform, toolchain_digest

LIVE_ENV = Path(__file__).resolve().parents[4] / ".devdata" / "databricks.env"
NO_WORKSPACE = "live validation waiting on owner input: no Databricks workspace"
OPT_IN = "live validation is opt-in: run with -m live"
RUNBOOK = "docs/runbooks/databricks-live-validation.md"
KEYS = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_WAREHOUSE_ID",
    "DATABRICKS_SERVICE_PRINCIPAL",
    "DATABRICKS_WORKSPACE_PURPOSE",
)
POLL_SECONDS = 15
OBSERVE_DEADLINE_SECONDS = 20 * 60


class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    host: str  # normalized
    token: SecretStr
    warehouse_id: str
    service_principal: str

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"


def read_live_config(path: Path) -> LiveConfig:
    """The development workspace of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    for key in KEYS:
        if not values.get(key):
            raise LiveConfigRefused(f"{key} is missing or empty in the live env file")
    if values["DATABRICKS_WORKSPACE_PURPOSE"] != "development":
        raise LiveConfigRefused("DATABRICKS_WORKSPACE_PURPOSE must be development")
    try:
        host = normalize_databricks_address(str(values["DATABRICKS_HOST"]))
    except AddressRefused:
        raise LiveConfigRefused(
            "DATABRICKS_HOST is not an allowed Databricks workspace URL"
        ) from None
    return LiveConfig(
        host=host,
        token=SecretStr(str(values["DATABRICKS_TOKEN"])),
        warehouse_id=str(values["DATABRICKS_WAREHOUSE_ID"]),
        service_principal=str(values["DATABRICKS_SERVICE_PRINCIPAL"]),
    )


def live_selected(markexpr: str) -> bool:
    """Whether the `-m` expression selects `live`: true for an item marked `live` only and false
    for an unmarked one (no `-m` selects nothing)."""
    if not markexpr.strip():
        return False
    expression = Expression.compile(markexpr)
    return expression.evaluate(lambda name, **_: name == "live") and not expression.evaluate(
        lambda name, **_: False
    )


def _step[T](name: str, call: Callable[[], T]) -> T:
    """Run one adapter call; an exception fails the test by its type only (its text may carry
    the workspace URL)."""
    try:
        return call()
    except Exception as exc:
        pytest.fail(f"{name} raised {type(exc).__name__}", pytrace=False)


@pytest.mark.live
def test_real_adapter_against_a_development_workspace(request: pytest.FixtureRequest) -> None:
    if not LIVE_ENV.is_file():
        pytest.skip(NO_WORKSPACE)
    if not live_selected(str(request.config.getoption("markexpr") or "")):
        pytest.skip(OPT_IN)
    try:
        config = read_live_config(LIVE_ENV)
    except LiveConfigRefused as exc:
        pytest.fail(str(exc), pytrace=False)

    def token_for(host: str) -> str:
        if host != config.host:
            raise PermissionError("the live token is only for the configured workspace")
        return config.token.get_secret_value()

    tenant = uuid4()
    target = Target(
        tenant_id=tenant,
        host=config.host,
        bindings={
            WAREHOUSE_BINDING: config.warehouse_id,
            PRINCIPAL_BINDING: config.service_principal,
        },
    )
    adapter = RealPlatform().adapter(target, token_for)
    read = ReadContext(tenant_id=tenant, target=target)
    build = BuildContext(
        tenant_id=tenant,
        artifacts=InMemoryArtifactStore(),
        toolchain_digest=toolchain_digest(RealPlatform.kind),
    )

    # 1. capabilities: the pinned CLI and the workspace probe
    report = _step("capabilities", lambda: adapter.capabilities(read))
    statuses = {c.operation: c.status for c in report.capabilities}
    assert statuses == {"cli": "supported", "workspace": "supported"}, statuses

    # 2. scaffold, validate and build of the template app
    spec = DatabricksApplicationSpec()
    source = _step("scaffold", lambda: adapter.scaffold(ScaffoldRequest(spec=spec), build))
    validation = _step(
        "validate", lambda: adapter.validate(ValidateRequest(spec=spec, source=source), build)
    )
    assert validation.passed, "the template app failed validation"
    artifact = _step(
        "build",
        lambda: adapter.build(
            BuildRequest(spec=spec, source=source, application_id=uuid4()), build
        ),
    )

    # 3. plan
    plan = _step("plan", lambda: adapter.plan(PlanRequest(artifact=artifact), read))

    # 4. apply: once, never retried
    operation = uuid4()
    deploy = DeployContext(
        tenant_id=tenant,
        target=target,
        operation_id=operation,
        artifacts=build.artifacts,
        journal=InMemoryJournal(tenant, operation),
        toolchain_digest=build.toolchain_digest,
    )
    try:
        receipt = adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    except Exception as exc:
        pytest.fail(
            f"apply raised {type(exc).__name__}: its outcome is unknown. Do not run apply again; "
            f'follow "Writes, retries and unknown outcomes" in {RUNBOOK}',
            pytrace=False,
        )
    handles = receipt.operation_handles
    # a fresh read context: its one-hour deadline starts after apply, not before the build
    read = ReadContext(tenant_id=tenant, target=target)
    print(f"deployed app {handles['app_name']} (deployment {handles.get('deployment_id')})")

    # 5. observe every 15 s until succeeded, failing on failed or after 20 minutes
    deadline = time.monotonic() + OBSERVE_DEADLINE_SECONDS
    while True:
        observed = _step("observe", lambda: adapter.observe(ObserveRequest(receipt=receipt), read))
        if observed.state == "succeeded":
            break
        assert observed.state != "failed", f"observe: failed {observed.diagnostics}"
        assert time.monotonic() < deadline, "observe: not succeeded within 20 minutes"
        time.sleep(POLL_SECONDS)

    # 6. verify: the native runtime and the app's warehouse check
    verified = _step("verify", lambda: adapter.verify(VerifyRequest(receipt=receipt), read))
    checks = {c.name: c.status for c in verified.checks}
    assert checks == {"native-runtime": "passed", "runtime-warehouse": "passed"}, checks


# ------------------------------------------------------------------ the env file parser
TOKEN = "dapi-live-test-token-never-shown"  # noqa: S105 - a test value
HOST = "https://adb-1234567890123456.7.azuredatabricks.net"
WAREHOUSE = "warehouse-live-test"
PRINCIPAL = "4a3c1f7e-0b5d-4c8e-9f21-6d7a8b9c0e1f"


def _env_file(directory: Path, **overrides: str | None) -> Path:
    values: dict[str, str | None] = {
        "DATABRICKS_HOST": HOST,
        "DATABRICKS_TOKEN": TOKEN,
        "DATABRICKS_WAREHOUSE_ID": WAREHOUSE,
        "DATABRICKS_SERVICE_PRINCIPAL": PRINCIPAL,
        "DATABRICKS_WORKSPACE_PURPOSE": "development",
    }
    values.update(overrides)
    path = directory / "databricks.env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return path


def test_a_complete_development_file_parses(tmp_path: Path) -> None:
    config = read_live_config(
        _env_file(tmp_path, DATABRICKS_HOST=HOST.replace("https://", "HTTPS://"))
    )
    assert config.host == HOST
    assert config.token.get_secret_value() == TOKEN
    assert (config.warehouse_id, config.service_principal) == (WAREHOUSE, PRINCIPAL)
    assert TOKEN not in repr(config) and HOST not in repr(config)


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_refused_by_name(tmp_path: Path, key: str, value: str | None) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == f"{key} is missing or empty in the live env file"


def test_a_production_workspace_is_refused(tmp_path: Path) -> None:
    path = _env_file(tmp_path, DATABRICKS_WORKSPACE_PURPOSE="production")
    with pytest.raises(LiveConfigRefused, match="DATABRICKS_WORKSPACE_PURPOSE must be development"):
        read_live_config(path)


@pytest.mark.parametrize(
    "host",
    ["https://evil.example.com", "http://adb-1.azuredatabricks.net", "https://10.0.0.1"],
)
def test_a_host_outside_the_allow_list_is_refused(tmp_path: Path, host: str) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, DATABRICKS_HOST=host))
    assert str(refused.value) == "DATABRICKS_HOST is not an allowed Databricks workspace URL"
    assert host not in str(refused.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DATABRICKS_WAREHOUSE_ID": None},
        {"DATABRICKS_WORKSPACE_PURPOSE": "production"},
        {"DATABRICKS_HOST": "https://evil.example.com"},
        {"DATABRICKS_HOST": TOKEN},
        {"DATABRICKS_WORKSPACE_PURPOSE": TOKEN},
    ],
)
def test_no_refusal_shows_the_token(tmp_path: Path, overrides: dict[str, str | None]) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, **overrides))
    shown = [str(refused.value), repr(refused.value), *map(str, refused.value.args)]
    assert not any(TOKEN in text for text in shown), shown
    error = refused.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


@pytest.mark.parametrize(
    ("markexpr", "selected"),
    [("", False), ("live", True), ("live or slow", True), ("not slow", False), ("slow", False)],
)
def test_only_an_expression_that_names_live_selects_it(markexpr: str, selected: bool) -> None:
    assert live_selected(markexpr) is selected


def test_values_are_not_read_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_LIVE_TEST_HOST", HOST)
    path = _env_file(tmp_path, DATABRICKS_HOST="${SHIFDE_LIVE_TEST_HOST}")
    with pytest.raises(LiveConfigRefused, match="DATABRICKS_HOST is not an allowed"):
        read_live_config(path)
