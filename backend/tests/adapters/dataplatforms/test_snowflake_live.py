"""The opt-in live check of the `snowflake` read connection (design 2026-11-22 section 7.5, CM-5,
issue #163; runbook `docs/runbooks/snowflake-live-validation.md`).

`test_snowflake_read_connection_against_a_development_account` drives the real
`SnowflakeAdapter` with key-pair auth through test, discover and `SELECT 1` against the owner's
development account, behind the strict production egress layer. It runs only when
`<checkout root>/.devdata/snowflake.env` exists, holds every key and `-m live` selects it; else
it is skipped, so it never runs in CI, `factory/checks.sh` or a default pytest run. It reads only
and writes nothing.

The env file is read here, never from the process environment. No value of it (account, user,
key or key path) appears in a message, skip reason, exception or `repr`: every refusal names only
the key or rule. The parsing tests at the end need no network and no database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from dotenv import dotenv_values
from pydantic import SecretStr

from shifde.adapters.base import ConnectionConfig, Credential, ExecContext
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.connections.schemas import SqlInvokeIn
from shifde.core.egress import EgressGuard, installation_layer
from tests.adapters.dataplatforms.helpers import pkcs8, rsa_key
from tests.adapters.native.test_databricks_live import live_selected

LIVE_ENV = Path(__file__).resolve().parents[4] / ".devdata" / "snowflake.env"
NO_ACCOUNT = "live validation waiting on owner input: no Snowflake account"
OPT_IN = "live validation is opt-in: run with -m live"
KEYS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY_FILE",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ACCOUNT_PURPOSE",
)
# the env key of each connection config field
CONFIG_KEYS = {
    "account": "SNOWFLAKE_ACCOUNT",
    "user": "SNOWFLAKE_USER",
    "role": "SNOWFLAKE_ROLE",
    "warehouse": "SNOWFLAKE_WAREHOUSE",
}
UNREADABLE_KEY_FILE = "SNOWFLAKE_PRIVATE_KEY_FILE does not name a readable file"
BAD_KEY = "SNOWFLAKE_PRIVATE_KEY_FILE is not an unencrypted PKCS#8 RSA key of at least 2048 bits"


class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


class LiveConfigMissing(LiveConfigRefused):
    """A required key is missing or empty: the live test skips instead of failing."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    account: str
    user: str
    role: str
    warehouse: str
    private_key: SecretStr  # the unencrypted PKCS#8 PEM

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"

    @property
    def config(self) -> dict[str, str]:
        return {
            "account": self.account,
            "user": self.user,
            "role": self.role,
            "warehouse": self.warehouse,
        }


def read_live_config(path: Path) -> LiveConfig:
    """The development account of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    for key in KEYS:
        if not values.get(key):
            raise LiveConfigMissing(f"{key} is missing or empty in the live env file")
    if values["SNOWFLAKE_ACCOUNT_PURPOSE"] != "development":
        raise LiveConfigRefused("SNOWFLAKE_ACCOUNT_PURPOSE must be development")
    key_file = Path(str(values["SNOWFLAKE_PRIVATE_KEY_FILE"]))
    if not key_file.is_absolute():
        key_file = path.parent / key_file
    try:
        data = key_file.read_bytes()
    except OSError:
        raise LiveConfigRefused(UNREADABLE_KEY_FILE) from None
    try:
        pem = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LiveConfigRefused(BAD_KEY) from None
    adapter = SnowflakeAdapter()
    if adapter.validate_credential("key_pair", pem):
        raise LiveConfigRefused(BAD_KEY)
    config = LiveConfig(
        account=str(values["SNOWFLAKE_ACCOUNT"]),
        user=str(values["SNOWFLAKE_USER"]),
        role=str(values["SNOWFLAKE_ROLE"]),
        warehouse=str(values["SNOWFLAKE_WAREHOUSE"]),
        private_key=SecretStr(pem),
    )
    errors = adapter.validate_config(config.config)
    if errors:
        # only the four fields are set, so every error path starts with one of them
        field = errors[0].path.split("/", 1)[0]
        raise LiveConfigRefused(f"{CONFIG_KEYS[field]} is not valid")
    return config


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


def _step[T](name: str, call: Callable[[], T]) -> T:
    """Run one adapter call; an exception fails the test by its type only (its text may carry
    the account host)."""
    try:
        return call()
    except Exception as exc:
        pytest.fail(f"{name} raised {type(exc).__name__}", pytrace=False)


@pytest.mark.live
def test_snowflake_read_connection_against_a_development_account(
    request: pytest.FixtureRequest,
) -> None:
    if not LIVE_ENV.is_file():
        pytest.skip(NO_ACCOUNT)
    if not live_selected(str(request.config.getoption("markexpr") or "")):
        pytest.skip(OPT_IN)
    try:
        config = read_live_config(LIVE_ENV)
    except LiveConfigMissing as exc:
        pytest.skip(str(exc))
    except LiveConfigRefused as exc:
        pytest.fail(str(exc), pytrace=False)

    adapter = SnowflakeAdapter()
    ctx = ExecContext(
        tenant_id=uuid4(),
        principal_id=None,
        egress=EgressGuard([installation_layer(None, env="production")]),
    )
    conn = ConnectionConfig(
        id=uuid4(),
        name="snowflake-live",
        adapter_key="snowflake",
        config=config.config,
        auth_method="key_pair",
        access_mode="read",
        credential=Credential(config.private_key.get_secret_value()),
    )

    # 1. test: config, egress, dns, tls, auth and capability checks
    outcome = _step("test", lambda: adapter.test(ctx, conn))
    if not outcome.passed:
        checks = [(c.name, c.status) for c in outcome.checks]
        pytest.fail(f"test failed: {checks}, error class {outcome.error_class}", pytrace=False)

    # 2. discover: the tables the role can see
    discovered = _step("discover", lambda: adapter.discover(ctx, conn))
    if discovered.kind != "schema":
        pytest.fail("discover did not return a schema", pytrace=False)
    if not isinstance(discovered.payload.get("tables"), list):
        pytest.fail("discover: tables is not a list", pytrace=False)
    if not isinstance(discovered.payload.get("truncated"), bool):
        pytest.fail("discover: truncated is not a bool", pytrace=False)

    # 3. SELECT 1 on the warehouse (the SQL API answers values as text)
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
ACCOUNT = "myorg-liveaccount"
USER = "live_reader_never_shown"
KEY_FILE = "live_key_never_shown.p8"
PEM = pkcs8(rsa_key())


def _env_file(directory: Path, pem: str = PEM, **overrides: str | None) -> Path:
    (directory / KEY_FILE).write_bytes(pem.encode())
    values: dict[str, str | None] = {
        "SNOWFLAKE_ACCOUNT": ACCOUNT,
        "SNOWFLAKE_USER": USER,
        "SNOWFLAKE_PRIVATE_KEY_FILE": KEY_FILE,
        "SNOWFLAKE_ROLE": "LIVE_READER",
        "SNOWFLAKE_WAREHOUSE": "LIVE_WH",
        "SNOWFLAKE_ACCOUNT_PURPOSE": "development",
    }
    values.update(overrides)
    path = directory / "snowflake.env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return path


def _pem_body(pem: str) -> str:
    """The base64 lines of a PEM, without its header and footer."""
    return "".join(line for line in pem.splitlines() if not line.startswith("-----"))


def test_a_complete_file_parses(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path))
    assert config.config == {
        "account": ACCOUNT,
        "user": USER,
        "role": "LIVE_READER",
        "warehouse": "LIVE_WH",
    }
    assert config.private_key.get_secret_value() == PEM
    shown = repr(config)
    assert ACCOUNT not in shown and USER not in shown and _pem_body(PEM)[:40] not in shown


def test_an_absolute_key_path_parses(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    absolute = key_dir / "absolute.p8"
    absolute.write_bytes(PEM.encode())
    config = read_live_config(_env_file(tmp_path, SNOWFLAKE_PRIVATE_KEY_FILE=str(absolute)))
    assert config.private_key.get_secret_value() == PEM


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_missing_by_name(tmp_path: Path, key: str, value: str | None) -> None:
    with pytest.raises(LiveConfigMissing) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == f"{key} is missing or empty in the live env file"


def test_a_production_account_is_refused(tmp_path: Path) -> None:
    path = _env_file(tmp_path, SNOWFLAKE_ACCOUNT_PURPOSE="production")
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(path)
    assert not isinstance(refused.value, LiveConfigMissing)
    assert str(refused.value) == "SNOWFLAKE_ACCOUNT_PURPOSE must be development"


def test_a_nonexistent_key_file_is_refused(tmp_path: Path) -> None:
    path = _env_file(tmp_path, SNOWFLAKE_PRIVATE_KEY_FILE="no_such_key.p8")
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(path)
    assert str(refused.value) == UNREADABLE_KEY_FILE


def _encrypted() -> str:
    return (
        rsa_key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"live-passphrase"),
        )
        .decode()
    )


@pytest.mark.parametrize(
    "pem",
    [
        pytest.param(_encrypted(), id="encrypted"),
        pytest.param(pkcs8(rsa_key("short", 1024)), id="1024-bit"),
    ],
)
def test_an_unusable_key_is_refused(tmp_path: Path, pem: str) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, pem=pem))
    assert str(refused.value) == BAD_KEY


def test_an_invalid_account_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, SNOWFLAKE_ACCOUNT="not an account!"))
    assert str(refused.value) == "SNOWFLAKE_ACCOUNT is not valid"


@pytest.mark.parametrize(
    ("pem", "overrides"),
    [
        (PEM, {"SNOWFLAKE_ROLE": None}),
        (PEM, {"SNOWFLAKE_ACCOUNT_PURPOSE": "production"}),
        (PEM, {"SNOWFLAKE_PRIVATE_KEY_FILE": f"missing-{KEY_FILE}"}),
        (pkcs8(rsa_key("short", 1024)), {}),
        (_encrypted(), {}),
        (PEM, {"SNOWFLAKE_ACCOUNT": f"{ACCOUNT}!"}),
    ],
)
def test_no_refusal_shows_a_value(
    tmp_path: Path, pem: str, overrides: dict[str, str | None]
) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, pem=pem, **overrides))
    shown = [str(refused.value), repr(refused.value), *map(str, refused.value.args)]
    hidden = (_pem_body(pem)[:40], _pem_body(PEM)[:40], ACCOUNT, KEY_FILE)
    assert not any(value in text for text in shown for value in hidden)
    error = refused.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_values_are_not_read_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_LIVE_TEST_PURPOSE", "development")
    path = _env_file(tmp_path, SNOWFLAKE_ACCOUNT_PURPOSE="${SHIFDE_LIVE_TEST_PURPOSE}")
    with pytest.raises(LiveConfigRefused, match="SNOWFLAKE_ACCOUNT_PURPOSE must be development"):
        read_live_config(path)
