"""The opt-in live check of the `databricks` read connection (design 2026-11-22 section 7.5, CM-5,
issue #163; runbook `docs/runbooks/databricks-live-validation.md`, "Read connection").

`test_databricks_read_connection_against_a_development_workspace` drives the real
`DatabricksAdapter` through test, discover and `SELECT 1` against the owner's development
workspace, behind the strict production egress layer. It runs only when
`<checkout root>/.devdata/databricks.env` exists, holds `DATABRICKS_READ_TOKEN` and `-m live`
selects it; else it is skipped, so it never runs in CI, `factory/checks.sh` or a default pytest
run. It reads only and writes nothing. The deploy token `DATABRICKS_TOKEN` of the same file is
never read.

The env file is read here, never from the process environment. No value of it appears in a
message, skip reason, exception or `repr`: every refusal names only the key or rule. The parsing
tests at the end need no network and no database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from pydantic import SecretStr

from shifde.adapters.base import AdapterRules, ConnectionConfig, Credential, ExecContext
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.connections.schemas import SqlInvokeIn
from shifde.core.egress import EgressGuard, installation_layer
from tests.adapters.native.test_databricks_live import live_selected

LIVE_ENV = Path(__file__).resolve().parents[4] / ".devdata" / "databricks.env"
NO_WORKSPACE = "live validation waiting on owner input: no Databricks workspace"
OPT_IN = "live validation is opt-in: run with -m live"
KEYS = ("DATABRICKS_HOST", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_READ_TOKEN")


class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


class LiveConfigMissing(LiveConfigRefused):
    """A required key is missing or empty: the live test skips instead of failing."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    host: str  # normalized
    warehouse_id: str
    token: SecretStr  # the read token, never the deploy token

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"


def read_live_config(path: Path) -> LiveConfig:
    """The development workspace of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    for key in KEYS:
        if not values.get(key):
            raise LiveConfigMissing(f"{key} is missing or empty in the live env file")
    try:
        host = normalize_databricks_address(str(values["DATABRICKS_HOST"]))
    except AddressRefused:
        raise LiveConfigRefused(
            "DATABRICKS_HOST is not an allowed Databricks workspace URL"
        ) from None
    return LiveConfig(
        host=host,
        warehouse_id=str(values["DATABRICKS_WAREHOUSE_ID"]),
        token=SecretStr(str(values["DATABRICKS_READ_TOKEN"])),
    )


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


def _step[T](name: str, call: Callable[[], T]) -> T:
    """Run one adapter call; an exception fails the test by its type only (its text may carry
    the workspace URL)."""
    try:
        return call()
    except Exception as exc:
        pytest.fail(f"{name} raised {type(exc).__name__}", pytrace=False)


@pytest.mark.live
def test_databricks_read_connection_against_a_development_workspace(
    request: pytest.FixtureRequest,
) -> None:
    if not LIVE_ENV.is_file():
        pytest.skip(NO_WORKSPACE)
    if not live_selected(str(request.config.getoption("markexpr") or "")):
        pytest.skip(OPT_IN)
    try:
        config = read_live_config(LIVE_ENV)
    except LiveConfigMissing as exc:
        pytest.skip(str(exc))
    except LiveConfigRefused as exc:
        pytest.fail(str(exc), pytrace=False)

    adapter = DatabricksAdapter()
    ctx = ExecContext(
        tenant_id=uuid4(),
        principal_id=None,
        egress=EgressGuard([installation_layer(None, env="production")]),
    )
    settings = _step(
        "normalize_config",
        lambda: adapter.normalize_config(
            {"host": config.host, "warehouse_id": config.warehouse_id},
            AdapterRules(databricks_host_suffixes=()),
        ),
    )
    errors = adapter.validate_config(settings)
    if errors:
        pytest.fail(f"validate_config refused {sorted(e.path for e in errors)}", pytrace=False)
    conn = ConnectionConfig(
        id=uuid4(),
        name="databricks-live",
        adapter_key="databricks",
        config=settings,
        auth_method="bearer",
        access_mode="read",
        credential=Credential(config.token.get_secret_value()),
    )

    # 1. test: config, egress, dns, tls, auth and capability checks
    outcome = _step("test", lambda: adapter.test(ctx, conn))
    if not outcome.passed:
        checks = [(c.name, c.status) for c in outcome.checks]
        pytest.fail(f"test failed: {checks}, error class {outcome.error_class}", pytrace=False)

    # 2. discover: the Unity Catalog tables the read principal can see
    discovered = _step("discover", lambda: adapter.discover(ctx, conn))
    if discovered.kind != "schema":
        pytest.fail("discover did not return a schema", pytrace=False)
    if not isinstance(discovered.payload.get("tables"), list):
        pytest.fail("discover: tables is not a list", pytrace=False)
    if not isinstance(discovered.payload.get("truncated"), bool):
        pytest.fail("discover: truncated is not a bool", pytrace=False)

    # 3. SELECT 1 on the warehouse (JSON_ARRAY may answer the value as text)
    session = _step("open", lambda: adapter.open(ctx, conn))
    try:
        result = _step(
            "invoke", lambda: session.invoke(SqlInvokeIn(sql="SELECT 1").model_dump())
        ).result
    finally:
        session.close()
    rows = result.get("rows") if isinstance(result, dict) else None
    if not rows or not rows[0] or rows[0][0] not in (1, "1"):
        pytest.fail("SELECT 1 did not return 1", pytrace=False)


# ------------------------------------------------------------------ the env file parser
READ_TOKEN = "dapi-live-read-token-never-shown"  # noqa: S105 - a test value
DEPLOY_TOKEN = "dapi-live-deploy-token-never-used"  # noqa: S105 - a test value
HOST = "https://adb-1234567890123456.7.azuredatabricks.net"
WAREHOUSE = "warehouse-live-test"


def _env_file(directory: Path, **overrides: str | None) -> Path:
    values: dict[str, str | None] = {
        "DATABRICKS_HOST": HOST,
        "DATABRICKS_WAREHOUSE_ID": WAREHOUSE,
        "DATABRICKS_READ_TOKEN": READ_TOKEN,
    }
    values.update(overrides)
    path = directory / "databricks.env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return path


def test_a_complete_file_parses(tmp_path: Path) -> None:
    config = read_live_config(
        _env_file(tmp_path, DATABRICKS_HOST=HOST.replace("https://", "HTTPS://"))
    )
    assert config.host == HOST
    assert config.warehouse_id == WAREHOUSE
    assert config.token.get_secret_value() == READ_TOKEN
    assert READ_TOKEN not in repr(config) and HOST not in repr(config)


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_missing_by_name(tmp_path: Path, key: str, value: str | None) -> None:
    with pytest.raises(LiveConfigMissing) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == f"{key} is missing or empty in the live env file"


def test_a_file_of_only_the_deploy_keys_misses_the_read_token(tmp_path: Path) -> None:
    path = _env_file(
        tmp_path,
        DATABRICKS_READ_TOKEN=None,
        DATABRICKS_TOKEN=DEPLOY_TOKEN,
        DATABRICKS_SERVICE_PRINCIPAL="4a3c1f7e-0b5d-4c8e-9f21-6d7a8b9c0e1f",
        DATABRICKS_WORKSPACE_PURPOSE="development",
    )
    with pytest.raises(LiveConfigMissing) as refused:
        read_live_config(path)
    assert str(refused.value) == "DATABRICKS_READ_TOKEN is missing or empty in the live env file"


def test_the_deploy_token_is_never_used(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path, DATABRICKS_TOKEN=DEPLOY_TOKEN))
    assert config.token.get_secret_value() == READ_TOKEN
    assert config.token.get_secret_value() != DEPLOY_TOKEN


@pytest.mark.parametrize(
    "host",
    ["https://evil.example.com", "http://adb-1.azuredatabricks.net", "https://10.0.0.1"],
)
def test_a_host_outside_the_allow_list_is_refused(tmp_path: Path, host: str) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, DATABRICKS_HOST=host))
    assert not isinstance(refused.value, LiveConfigMissing)
    assert str(refused.value) == "DATABRICKS_HOST is not an allowed Databricks workspace URL"
    assert host not in str(refused.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DATABRICKS_WAREHOUSE_ID": None},
        {"DATABRICKS_READ_TOKEN": None, "DATABRICKS_TOKEN": DEPLOY_TOKEN},
        {"DATABRICKS_HOST": "https://evil.example.com", "DATABRICKS_TOKEN": DEPLOY_TOKEN},
        {"DATABRICKS_HOST": READ_TOKEN},
        {"DATABRICKS_HOST": DEPLOY_TOKEN, "DATABRICKS_TOKEN": DEPLOY_TOKEN},
    ],
)
def test_no_refusal_shows_a_token(tmp_path: Path, overrides: dict[str, str | None]) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, **overrides))
    shown = [str(refused.value), repr(refused.value), *map(str, refused.value.args)]
    assert not any(token in text for text in shown for token in (READ_TOKEN, DEPLOY_TOKEN))
    error = refused.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_values_are_not_read_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_LIVE_TEST_HOST", HOST)
    path = _env_file(tmp_path, DATABRICKS_HOST="${SHIFDE_LIVE_TEST_HOST}")
    with pytest.raises(LiveConfigRefused, match="DATABRICKS_HOST is not an allowed"):
        read_live_config(path)
