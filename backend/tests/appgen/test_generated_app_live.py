"""The opt-in live check of the generated app (SP5 design section 10, section 11 item 9, issue #262;
runbook `docs/runbooks/databricks-live-validation.md`, "Generated app").

`test_the_generated_app_against_a_development_workspace` renders the `SAMPLE_DRAFT` app with the
env file's serving endpoint, imports its `app.py` from a temporary directory and drives it with
Flask's test client, its `databricks.sdk.WorkspaceClient` pointed at the owner's development
workspace with the read token for the test's duration only: deep health passes every check,
`orders_by_customer` returns the sample orders 1 and 7 of customer 1, and `/api/ask` answers case
1. It needs the sample tables and the serving endpoint the runbook creates. It runs only when
`<checkout root>/.devdata/databricks.env` exists, holds every key of `KEYS` and `-m live` selects
it; else it is skipped, so it never runs in CI, `factory/checks.sh` or a default pytest run. It
deploys nothing and writes nothing to the workspace. The deploy token `DATABRICKS_TOKEN` of the
same file is never read.

The env file is read here, never from the process environment. No value of it appears in a
message, skip reason, exception or `repr`: every refusal names only the key or rule. The parsing
tests at the end need no network and no database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # Any: JSON documents, a Flask test client

import pytest
from dotenv import dotenv_values
from pydantic import SecretStr

from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.appgen.render import render
from tests._support.appgen_fixtures import CASE_CALLS, SAMPLE_DRAFT
from tests.adapters.native.test_databricks_live import live_selected
from tests.appgen.rendered import (
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    load_module,
    resolved_of,
    write_files,
)

LIVE_ENV = Path(__file__).resolve().parents[3] / ".devdata" / "databricks.env"
NO_WORKSPACE = "live validation waiting on owner input: no Databricks workspace"
OPT_IN = "live validation is opt-in: run with -m live"
KEYS = (
    "DATABRICKS_HOST",
    "DATABRICKS_WAREHOUSE_ID",
    "DATABRICKS_READ_TOKEN",
    "DATABRICKS_SERVING_ENDPOINT",
    "DATABRICKS_WORKSPACE_PURPOSE",
)


class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


class LiveConfigMissing(LiveConfigRefused):
    """A required key is missing or empty: the live test skips instead of failing."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    host: str  # normalized
    warehouse_id: str
    token: SecretStr  # the read token, never the deploy token
    serving_endpoint: str

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"


def read_live_config(path: Path) -> LiveConfig:
    """The development workspace of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    for key in KEYS:
        if not values.get(key):
            raise LiveConfigMissing(f"{key} is missing or empty in the live env file")
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
        warehouse_id=str(values["DATABRICKS_WAREHOUSE_ID"]),
        token=SecretStr(str(values["DATABRICKS_READ_TOKEN"])),
        serving_endpoint=str(values["DATABRICKS_SERVING_ENDPOINT"]),
    )


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


def _step[T](name: str, call: Callable[[], T]) -> T:
    """Run one call; an exception fails the test by its type only (its text may carry the
    workspace URL)."""
    try:
        return call()
    except Exception as exc:
        pytest.fail(f"{name} raised {type(exc).__name__}", pytrace=False)


def _app(config: LiveConfig, directory: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The rendered `SAMPLE_DRAFT` app on the workspace (a Flask test client)."""
    from databricks.sdk import WorkspaceClient

    resolved = resolved_of(SAMPLE_DRAFT, endpoint=config.serving_endpoint)
    write_files(directory, render(SAMPLE_MANIFEST, SAMPLE_DIGEST, resolved))
    module = load_module(directory / "app" / "app.py", "generated_app_live")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", config.warehouse_id)
    monkeypatch.setenv("SERVING_ENDPOINT", config.serving_endpoint)
    # no profile of `~/.databrickscfg`: the host and the read token below only
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(directory / "no-such.cfg"))
    workspace = WorkspaceClient(
        host=config.host,
        token=config.token.get_secret_value(),
        auth_type="pat",
    )
    return module.create_app(workspace_client=workspace).test_client()


@pytest.mark.live
def test_the_generated_app_against_a_development_workspace(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    app = _step("render and import the app", lambda: _app(config, tmp_path, monkeypatch))

    # 1. deep health: the warehouse, the serving endpoint and the three sample tables
    health = _step("GET /health?deep=1", lambda: app.get("/health?deep=1"))
    doc = health.get_json(silent=True) or {}
    failed = [c.get("name") for c in doc.get("checks") or [] if c.get("status") != "passed"]
    if health.status_code != 200 or doc.get("status") != "ok" or failed:
        pytest.fail(
            f"deep health: status {health.status_code}, failed checks {failed}", pytrace=False
        )

    # 2. the table screen's tool: customer 1's orders since 2026-01-01 are orders 1 and 7
    name, arguments, _, _ = CASE_CALLS[0]
    rows = _step(
        f"POST /api/tools/{name}",
        lambda: app.post(f"/api/tools/{name}", json={"params": arguments}),
    )
    out = rows.get_json(silent=True) or {}
    ids = [str(row[0]) for row in out.get("rows") or [] if row]
    if rows.status_code != 200 or ids != ["1", "7"]:
        pytest.fail(
            f"{name}: status {rows.status_code}, {len(ids)} rows, not orders 1 and 7", pytrace=False
        )

    # 3. the agent answers case 1 (a real model: its shape, not the scripted assertions)
    message = SAMPLE_DRAFT["evaluation_cases"][0]["input"]["message"]
    answer = _step("POST /api/ask", lambda: app.post("/api/ask", json={"message": message}))
    text = (answer.get_json(silent=True) or {}).get("answer")
    if answer.status_code != 200 or not isinstance(text, str) or not text.strip():
        pytest.fail(f"/api/ask: status {answer.status_code}, no answer", pytrace=False)


# ------------------------------------------------------------------ the env file parser
READ_TOKEN = "dapi-live-read-token-never-shown"  # noqa: S105 - a test value
DEPLOY_TOKEN = "dapi-live-deploy-token-never-used"  # noqa: S105 - a test value
HOST = "https://adb-1234567890123456.7.azuredatabricks.net"
WAREHOUSE = "warehouse-live-test"
ENDPOINT = "endpoint-live-test"


def _env_file(directory: Path, **overrides: str | None) -> Path:
    values: dict[str, str | None] = {
        "DATABRICKS_HOST": HOST,
        "DATABRICKS_WAREHOUSE_ID": WAREHOUSE,
        "DATABRICKS_READ_TOKEN": READ_TOKEN,
        "DATABRICKS_SERVING_ENDPOINT": ENDPOINT,
        "DATABRICKS_WORKSPACE_PURPOSE": "development",
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
    assert config.serving_endpoint == ENDPOINT


def test_the_repr_hides_every_value(tmp_path: Path) -> None:
    shown = repr(read_live_config(_env_file(tmp_path)))
    assert shown == "LiveConfig(<values hidden>)"
    assert not any(value in shown for value in (READ_TOKEN, HOST, WAREHOUSE, ENDPOINT))


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_missing_by_name(tmp_path: Path, key: str, value: str | None) -> None:
    with pytest.raises(LiveConfigMissing) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == f"{key} is missing or empty in the live env file"


def test_the_deploy_token_is_never_used(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path, DATABRICKS_TOKEN=DEPLOY_TOKEN))
    assert config.token.get_secret_value() == READ_TOKEN
    with pytest.raises(LiveConfigMissing, match="DATABRICKS_READ_TOKEN is missing"):
        read_live_config(
            _env_file(tmp_path, DATABRICKS_READ_TOKEN=None, DATABRICKS_TOKEN=DEPLOY_TOKEN)
        )


@pytest.mark.parametrize("purpose", ["production", "Development", "staging"])
def test_a_purpose_other_than_development_is_refused(tmp_path: Path, purpose: str) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, DATABRICKS_WORKSPACE_PURPOSE=purpose))
    assert not isinstance(refused.value, LiveConfigMissing)
    assert str(refused.value) == "DATABRICKS_WORKSPACE_PURPOSE must be development"


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
        {"DATABRICKS_SERVING_ENDPOINT": None},
        {"DATABRICKS_READ_TOKEN": None, "DATABRICKS_TOKEN": DEPLOY_TOKEN},
        {"DATABRICKS_HOST": READ_TOKEN},
        {"DATABRICKS_WORKSPACE_PURPOSE": READ_TOKEN},
    ],
)
def test_no_refusal_shows_a_token(tmp_path: Path, overrides: dict[str, str | None]) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, **overrides))
    shown = [str(refused.value), repr(refused.value), *map(str, refused.value.args)]
    assert not any(token in text for text in shown for token in (READ_TOKEN, DEPLOY_TOKEN))
    error = refused.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
