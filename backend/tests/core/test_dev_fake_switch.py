"""`SHIFDE_DEV_FAKE_DATA_PLATFORMS` is refused outside development and test (CM-3, issue #161;
design 2026-11-22 section 7.5, B-2755)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from shifde.core.crypto import KeyRing
from shifde.core.settings import Settings

DB_URL = "postgresql+psycopg://shifde_app:db-password-value@127.0.0.1:1/shifde"
MESSAGE = "SHIFDE_DEV_FAKE_DATA_PLATFORMS is allowed only when SHIFDE_ENV is development or test"


def _settings(env: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "env": env, "database_url": DB_URL}
    if env == "production":  # an otherwise valid production configuration
        values |= {
            "api_key_pepper": "pepper-value-" + "p" * 40,
            "secret_keys": KeyRing.generate_entry("k1"),
            "metrics_token": "metrics-token-value-0123456789",
            "public_base_url": "https://seth.example.com",
            "allowed_origins": "https://seth.example.com",
        }
    return Settings(**(values | overrides))


def test_the_switch_is_refused_in_production() -> None:
    with pytest.raises(ValidationError) as raised:
        _settings("production", dev_fake_data_platforms=True)
    assert MESSAGE in str(raised.value)


def test_the_switch_is_refused_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "production")
    monkeypatch.setenv("SHIFDE_DEV_FAKE_DATA_PLATFORMS", "true")
    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None, database_url=DB_URL)  # type: ignore[call-arg]
    assert MESSAGE in str(raised.value)


@pytest.mark.parametrize("env", ["development", "test"])
def test_the_switch_is_accepted_in_development_and_test(env: str) -> None:
    assert _settings(env, dev_fake_data_platforms=True).dev_fake_data_platforms is True


def test_the_switch_is_off_by_default() -> None:
    assert _settings("production").dev_fake_data_platforms is False
    assert _settings("test").dev_fake_data_platforms is False
