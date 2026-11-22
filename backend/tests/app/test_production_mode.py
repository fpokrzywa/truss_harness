"""Production mode of the app (SP4-1, design `2026-11-21-subproject-4-go-live-design.md` 4.1,
4.2): docs and openapi off, no `fake` adapter, a stored `fake` provider cannot be invoked."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.settings import Settings
from tests._support.fullapp import ORIGIN, World, full_client
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A, fake_model_stack, invoke_body


def _production(settings: Settings) -> Settings:
    """The test settings' database, keys and pepper, validated as production settings."""
    assert settings.api_key_pepper is not None and settings.secret_keys is not None
    return Settings(
        _env_file=None,
        env="production",
        database_url=settings.database_url,
        migration_database_url=settings.migration_database_url,
        auth_local_enabled=True,
        auth_local_allow_in_production=True,
        login_rate_per_account_per_minute=0,
        allowed_origins=[ORIGIN],
        public_base_url="https://seth.example.com",
        api_key_pepper=settings.api_key_pepper.get_secret_value(),
        secret_keys=settings.secret_keys.get_secret_value(),
        metrics_token="metrics-token-0123456789abcdef",
    )


@pytest.fixture
def prod_client(settings: Settings, world: World) -> Iterator[TestClient]:
    with full_client(_production(settings)) as c:
        yield c


def _usage_events(owner_session: Session) -> int:
    owner_session.rollback()  # a fresh snapshot
    return int(owner_session.execute(text("SELECT count(*) FROM shifde.usage_events")).scalar_one())


def test_docs_and_openapi_are_off_in_production_and_on_in_test(
    client: TestClient, prod_client: TestClient
) -> None:
    for path in ("/api/docs", "/api/openapi.json"):
        assert client.get(path).status_code == 200
        assert prod_client.get(path).status_code == 404


def test_the_production_registry_has_no_fake_adapter(
    client: TestClient, prod_client: TestClient
) -> None:
    test_keys = client.app.state.integration.registry.keys()  # type: ignore[attr-defined]
    assert "fake" in test_keys
    keys = prod_client.app.state.integration.registry.keys()  # type: ignore[attr-defined]
    assert "fake" not in keys and "openai_compatible" in keys


def test_creating_a_fake_provider_in_production_is_refused(
    client: TestClient, prod_client: TestClient, world: World
) -> None:
    stack = fake_model_stack(client, login(client, world.passwords, ADMIN))  # a fake connection
    h = login(prod_client, world.passwords, ADMIN)
    body = {"name": "prov-fake-2", "adapter_key": "fake", "connection_id": stack["connection"]}
    r = prod_client.post(f"{A}/model-providers", headers=h, json=body)
    assert r.status_code == 422, r.text
    assert "not a model provider adapter" in r.text


def test_a_stored_fake_provider_cannot_be_invoked_in_production(
    client: TestClient, prod_client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = fake_model_stack(client, h)
    ok = client.post(f"{A}/models/{stack['model']}/invoke", headers=h, json=invoke_body())
    assert ok.status_code == 200, ok.text
    before = _usage_events(owner_session)
    assert before >= 1

    ph = login(prod_client, world.passwords, ADMIN)
    r = prod_client.post(f"{A}/models/{stack['model']}/invoke", headers=ph, json=invoke_body())
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "model_unavailable"
    assert "not available in this environment" in r.json()["detail"]
    assert _usage_events(owner_session) == before


def test_adapters_verify_under_production_refuses_fake_as_unknown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = _production(settings)
    monkeypatch.setattr(cli_main, "_settings", lambda: production)
    result = CliRunner().invoke(cli_main.app, ["adapters", "verify", "--adapter", "fake"])
    assert result.exit_code == 1
    assert "unknown adapter(s): fake" in result.output
