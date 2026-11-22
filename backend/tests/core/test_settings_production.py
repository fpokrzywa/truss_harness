"""Production startup refusals of SP4-1 (design `2026-11-21-subproject-4-go-live-design.md` 4):
metrics token, console logs, public base URL; the docs and HSTS settings; exit code 2."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shifde.core import settings as settings_module
from shifde.core.crypto import KeyRing
from shifde.core.settings import Settings, format_validation_error, settings_or_exit

PEPPER = "pepper-value-" + "p" * 40
METRICS = "metrics-token-value-0123456789"
KEYS = KeyRing.generate_entry("k1")
DB_URL = "postgresql+psycopg://shifde_app:db-password-value@127.0.0.1:1/shifde"
SECRETS = (PEPPER, METRICS, KEYS.split(":", 1)[1], "db-password-value")


def _production(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "_env_file": None,
        "env": "production",
        "database_url": DB_URL,
        "api_key_pepper": PEPPER,
        "secret_keys": KEYS,
        "metrics_token": METRICS,
        "public_base_url": "https://seth.example.com",
        "allowed_origins": "https://seth.example.com",
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _refusal(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as raised:
        Settings(**_production(**overrides))
    message = format_validation_error(raised.value)
    for secret in SECRETS:
        assert secret not in message
    return message


def test_a_complete_production_configuration_is_accepted() -> None:
    s = Settings(**_production())
    assert s.env == "production" and s.oidc_base_url == "https://seth.example.com"


@pytest.mark.parametrize("token", [None, ""])
def test_production_requires_the_metrics_token(token: str | None) -> None:
    values = _production()
    values.pop("metrics_token")
    if token is not None:
        values["metrics_token"] = token
    with pytest.raises(ValidationError) as raised:
        Settings(**values)
    message = format_validation_error(raised.value)
    assert "SHIFDE_METRICS_TOKEN" in message and "\n" not in message
    assert PEPPER not in message and "db-password-value" not in message


def test_production_requires_the_metrics_token_from_its_file_form_too(tmp_path: Path) -> None:
    empty = tmp_path / "metrics"
    empty.write_text("\n", encoding="utf-8")
    environ = {"SHIFDE_METRICS_TOKEN_FILE": str(empty)}
    with pytest.raises(ValueError, match="SHIFDE_METRICS_TOKEN_FILE: the secret file is empty"):
        settings_module.resolve_secret_files(environ)
    full = tmp_path / "metrics-full"
    full.write_text(METRICS + "\n", encoding="utf-8")
    files = settings_module.resolve_secret_files({"SHIFDE_METRICS_TOKEN_FILE": str(full)})
    values = _production()
    values.pop("metrics_token")
    s = Settings(**values, **files)
    assert s.metrics_token is not None and s.metrics_token.get_secret_value() == METRICS


def test_production_refuses_console_logs() -> None:
    message = _refusal(log_format="console")
    assert "SHIFDE_LOG_FORMAT" in message and "\n" not in message
    assert Settings(**_production(log_format="json")).log_format == "json"


@pytest.mark.parametrize("base", [None, ""])
def test_production_requires_the_public_base_url(base: str | None) -> None:
    values = _production()
    values.pop("public_base_url")
    if base is not None:
        values["public_base_url"] = base
    with pytest.raises(ValidationError) as raised:
        Settings(**values)
    message = format_validation_error(raised.value)
    assert "SHIFDE_PUBLIC_BASE_URL is required" in message and "\n" not in message
    for secret in SECRETS:
        assert secret not in message


def test_the_public_base_url_https_check_stays() -> None:
    assert "https://" in _refusal(public_base_url="http://seth.example.com")


def test_development_and_test_need_none_of_the_new_values() -> None:
    for env in ("development", "test"):
        s = Settings(_env_file=None, env=env, database_url=DB_URL, log_format="console")
        assert s.metrics_token is None and s.public_base_url is None


def test_docs_default_per_environment_and_an_explicit_value_wins() -> None:
    assert Settings(**_production()).api_docs_enabled is False
    assert Settings(**_production(api_docs_enabled=True)).api_docs_enabled is True
    for env in ("development", "test"):
        s = Settings(_env_file=None, env=env, database_url=DB_URL)
        assert s.api_docs_enabled is True
        off = Settings(_env_file=None, env=env, database_url=DB_URL, api_docs_enabled=False)
        assert off.api_docs_enabled is False


def test_hsts_default_and_negative_value_refused() -> None:
    assert Settings(**_production()).hsts_max_age_seconds == 31_536_000
    assert Settings(**_production(hsts_max_age_seconds=0)).hsts_max_age_seconds == 0
    message = _refusal(hsts_max_age_seconds=-1)
    assert "SHIFDE_HSTS_MAX_AGE_SECONDS" in message


def test_settings_or_exit_exits_2_with_one_redacted_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in [k for k in os.environ if k.startswith("SHIFDE_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SHIFDE_ENV", "production")
    monkeypatch.setenv("SHIFDE_DATABASE_URL", DB_URL)
    monkeypatch.setenv("SHIFDE_API_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("SHIFDE_SECRET_KEYS", KEYS)
    monkeypatch.setenv("SHIFDE_PUBLIC_BASE_URL", "https://seth.example.com")
    monkeypatch.setenv("SHIFDE_ALLOWED_ORIGINS", "https://seth.example.com")
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as raised:
            settings_or_exit()
    finally:
        settings_module.get_settings.cache_clear()
    assert raised.value.code == 2
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("shifde: invalid configuration: ")
    assert "SHIFDE_METRICS_TOKEN" in lines[0]
    for secret in SECRETS:
        assert secret not in err
