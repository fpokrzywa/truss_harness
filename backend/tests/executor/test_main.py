"""`shifde-executor` (B-2693, B-2694): `--help`, the environment it needs, https outside
development and test, and `--fake-platform` refused outside them, before any gateway call; the fake
platform's Snowflake fake built only with a development or test `SHIFDE_ENV` (SP6-3)."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 - runs the installed console script
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from shifde.adapters.native.snowflake.clients import SnowflakeKeyPair
from shifde.core.httplogs import HTTP_CLIENT_LOGGERS
from shifde.executor import main
from shifde.executor.runner import FakePlatform, RealPlatform
from tests.executor.support import credentials_file, snowflake_key_pem

TOKEN = "shx_prefix_executor-token-never-logged"  # noqa: S105 - a test value
HOST = "https://adb-1234567890.cloud.databricks.com"


class GatewayCalled(Exception):
    pass


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every gateway client `main` would create (none is: a stub raises instead)."""
    made: list[str] = []

    def client(api_url: str, token: str) -> None:
        made.append(api_url)
        raise GatewayCalled

    monkeypatch.setattr(main, "GatewayClient", client)
    return made


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(main.API_URL, "https://seth.example.com")
    monkeypatch.setenv(main.TOKEN, TOKEN)
    monkeypatch.setenv(main.CREDENTIALS_FILE, str(credentials_file(tmp_path, HOST, tenant=uuid4())))
    monkeypatch.delenv("SHIFDE_ENV", raising=False)


def test_help_names_the_option_and_the_environment() -> None:
    script = Path(sys.executable).with_name("shifde-executor")
    done = subprocess.run(  # nosec B603 - fixed arguments
        [str(script), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    for name in ("--fake-platform", main.API_URL, main.TOKEN, main.CREDENTIALS_FILE):
        assert name in done.stdout


@pytest.mark.parametrize("shifde_env", [None, "production"])
def test_fake_platform_is_refused_outside_development_and_test(
    env: None,
    calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shifde_env: str | None,
) -> None:
    if shifde_env is not None:
        monkeypatch.setenv("SHIFDE_ENV", shifde_env)
    with pytest.raises(SystemExit) as raised:
        main.run(["--fake-platform"])
    assert raised.value.code != 0
    assert calls == []
    err = capsys.readouterr().err
    assert "--fake-platform is refused" in err and TOKEN not in err


@pytest.mark.parametrize("shifde_env", ["development", "test"])
def test_fake_platform_is_accepted_in_development_and_test(
    env: None, monkeypatch: pytest.MonkeyPatch, shifde_env: str
) -> None:
    monkeypatch.setenv("SHIFDE_ENV", shifde_env)
    config = main.configure(["--fake-platform"], main.os.environ)
    assert isinstance(config.platform, FakePlatform)
    assert TOKEN not in repr(config)


def test_http_is_refused_under_production_and_accepted_under_test(
    env: None,
    calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(main.API_URL, "http://127.0.0.1:8000")
    monkeypatch.setenv("SHIFDE_ENV", "production")
    with pytest.raises(SystemExit) as raised:
        main.run([])
    assert raised.value.code != 0 and calls == []
    assert "must be an https URL" in capsys.readouterr().err
    monkeypatch.setenv("SHIFDE_ENV", "test")
    with pytest.raises(GatewayCalled):
        main.run([])
    assert calls == ["http://127.0.0.1:8000"]


def test_a_missing_variable_is_named_without_the_token(
    env: None,
    calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(main.CREDENTIALS_FILE)
    with pytest.raises(SystemExit) as raised:
        main.run([])
    assert raised.value.code != 0 and calls == []
    err = capsys.readouterr().err
    assert main.CREDENTIALS_FILE in err and TOKEN not in err


def test_the_real_platform_is_the_default(env: None) -> None:
    assert isinstance(main.configure([], main.os.environ).platform, RealPlatform)


def test_the_logging_setup_raises_the_http_client_loggers_to_warning(
    env: None, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-3901: the executor's logging setup leaves no gateway request URL at INFO."""
    levels = {name: logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS}
    monkeypatch.setenv("SHIFDE_ENV", "test")
    try:
        for name in HTTP_CLIENT_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        with pytest.raises(GatewayCalled):
            main.run([])
        assert {n: logging.getLogger(n).level for n in HTTP_CLIENT_LOGGERS} == dict.fromkeys(
            HTTP_CLIENT_LOGGERS, logging.WARNING
        )
    finally:
        for name, value in levels.items():
            logging.getLogger(name).setLevel(value)


def test_help_shows_both_credentials_entry_shapes() -> None:
    script = Path(sys.executable).with_name("shifde-executor")
    done = subprocess.run(  # nosec B603 - fixed arguments
        [str(script), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    text = " ".join(done.stdout.split())
    assert "Databricks {tenant_id, host, token, service_principal}" in text
    assert (
        'Snowflake {tenant_id, host, platform: "snowflake", user, role, private_key_file}' in text
    )
    assert "fake Databricks and Snowflake platforms" in text


@pytest.mark.parametrize("shifde_env", ["development", "test"])
def test_the_fake_platform_carries_the_executors_environment(
    env: None, monkeypatch: pytest.MonkeyPatch, shifde_env: str
) -> None:
    monkeypatch.setenv("SHIFDE_ENV", shifde_env)
    platform = main.configure(["--fake-platform"], main.os.environ).platform
    assert isinstance(platform, FakePlatform) and platform.environment == shifde_env


@pytest.mark.parametrize("environment", ["", "production", "staging"])
def test_the_fake_platform_refuses_to_build_the_snowflake_fake_outside_development_and_test(
    environment: str,
) -> None:
    pair = SnowflakeKeyPair(
        tenant_id=uuid4(),
        host="https://acct-1.snowflakecomputing.com",
        account="acct-1",
        user="deployer",
        role="DEPLOY_ROLE",
        private_key_pem=snowflake_key_pem(),
    )
    platform = FakePlatform(environment=environment)
    with pytest.raises(RuntimeError, match="only in development or test"):
        platform.snowflake_fake(pair)
    assert platform.snowflake is None
    assert FakePlatform(environment="test").snowflake_fake(pair).account == "acct-1"
