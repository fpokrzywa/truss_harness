"""`shifde-executor` (B-2693, B-2694): `--help`, the environment it needs, https outside
development and test, and `--fake-platform` refused outside them, before any gateway call."""

from __future__ import annotations

import subprocess  # nosec B404 - runs the installed console script
import sys
from pathlib import Path

import pytest

from shifde.executor import main
from shifde.executor.runner import FakePlatform, RealPlatform
from tests.executor.support import credentials_file

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
    monkeypatch.setenv(main.CREDENTIALS_FILE, str(credentials_file(tmp_path, HOST)))
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
