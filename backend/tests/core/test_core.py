from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from shifde.core.config_resolution import resolve_config, resolve_with_sources
from shifde.core.crypto import KeyRing
from shifde.core.errors import ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import decode_cursor, encode_cursor
from shifde.core.settings import Settings


def test_uuid7_is_versioned_and_time_ordered() -> None:
    ids = [uuid7() for _ in range(2000)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_cursor_roundtrip_and_invalid() -> None:
    i = uuid7()
    assert decode_cursor(encode_cursor(i)) == i
    assert decode_cursor(None) is None
    with pytest.raises(ValidationFailed):
        decode_cursor("not-a-cursor!!")


def test_config_resolution_precedence_and_deep_merge() -> None:
    installation = {"limits": {"runs": 10, "tokens": 100}, "flags": {"a": True}, "tags": ["x"]}
    tenant = {"limits": {"runs": 20}, "tags": ["y", "z"]}
    workspace = {"limits": {"tokens": 5}, "flags": None}
    project = {"limits": {"runs": 1}}
    merged = resolve_config(installation, tenant, workspace, project)
    assert merged == {
        "limits": {"runs": 1, "tokens": 5},
        "flags": None,  # explicit None overrides
        "tags": ["y", "z"],  # lists replace, never concatenate
    }


def test_config_resolution_is_pure_and_deterministic() -> None:
    inst = {"a": {"b": 1}}
    tenant = {"a": {"c": 2}}
    first = resolve_config(inst, tenant)
    first["a"]["b"] = 99
    assert inst == {"a": {"b": 1}} and tenant == {"a": {"c": 2}}
    assert resolve_config(inst, tenant) == {"a": {"b": 1, "c": 2}}
    assert resolve_config() == {}
    assert resolve_config(None, {}, None, {"k": 1}) == {"k": 1}


def test_config_resolution_reports_winning_layer() -> None:
    out = resolve_with_sources({"a": {"b": 1, "c": 1}}, {"a": {"b": 2}}, None, {"a": {"d": 3}})
    assert (out["a.b"].value, out["a.b"].source) == (2, "tenant")
    assert (out["a.c"].value, out["a.c"].source) == (1, "installation")
    assert (out["a.d"].value, out["a.d"].source) == (3, "project")


def test_scalar_replacing_mapping_drops_stale_sources() -> None:
    out = resolve_with_sources({"a": {"b": 1}}, {"a": 5})
    assert set(out) == {"a"} and out["a"].source == "tenant"


def test_production_refuses_local_auth_unless_explicitly_allowed() -> None:
    base = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x",
        "env": "production",
        "api_key_pepper": "p" * 40,
        "secret_keys": KeyRing.generate_entry("k1"),
    }
    with pytest.raises(ValidationError, match="refused in production"):
        Settings(**base, auth_local_enabled=True)
    allowed = Settings(**base, auth_local_enabled=True, auth_local_allow_in_production=True)
    assert allowed.auth_local_enabled
    assert Settings(**base).auth_local_enabled is False  # secure default
    assert Settings(**base).session_cookie_name == "__Host-shifde_session"


def test_settings_parse_csv_origins_and_dev_cookie(tmp_path: Path) -> None:
    s = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x",
        env="development",
        allowed_origins="http://a:1, http://b:2/",
    )
    assert s.allowed_origins == ["http://a:1", "http://b:2"]
    assert s.session_cookie_name == "shifde_session" and not s.cookie_secure


def test_settings_read_bom_prefixed_env_file(tmp_path: Path) -> None:
    f = tmp_path / "dev.env"
    f.write_bytes(
        b"\xef\xbb\xbfSHIFDE_DATABASE_URL=postgresql+psycopg://bom\nSHIFDE_OTHER=1\n"
        b"SHIFDE_API_KEY_PEPPER="
        + b"x" * 40
        + b"\nSHIFDE_SECRET_KEYS="
        + KeyRing.generate_entry("k1").encode()
        + b"\n"
    )
    s = Settings(_env_file=f)
    assert s.database_url == "postgresql+psycopg://bom"


def test_production_requires_pepper_and_secret_keys_but_dev_and_test_generate_them() -> None:
    base = {"_env_file": None, "database_url": "postgresql+psycopg://x", "env": "production"}
    with pytest.raises(ValidationError, match="SHIFDE_API_KEY_PEPPER is required"):
        Settings(**base)
    with pytest.raises(ValidationError, match="SHIFDE_SECRET_KEYS is required"):
        Settings(**base, api_key_pepper="p" * 40)
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(**base, api_key_pepper="short", secret_keys=KeyRing.generate_entry())
    with pytest.raises(ValidationError, match="32 bytes"):
        Settings(**base, api_key_pepper="p" * 40, secret_keys="k:YWJj")
    for env in ("development", "test"):
        s = Settings(_env_file=None, database_url="postgresql+psycopg://x", env=env)
        assert len(s.pepper) >= 32 and s.keyring.active_id


def test_dev_env_generation_appends_missing_secrets_once_and_never_overrides_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shifde.core.settings import _ensure_dev_env_values

    f = tmp_path / "dev.env"
    f.write_bytes(b"\xef\xbb\xbfSHIFDE_DATABASE_URL=postgresql+psycopg://x\n")
    monkeypatch.delenv("SHIFDE_API_KEY_PEPPER", raising=False)
    monkeypatch.delenv("SHIFDE_SECRET_KEYS", raising=False)
    _ensure_dev_env_values(f)
    first = f.read_text(encoding="utf-8-sig")
    assert "SHIFDE_API_KEY_PEPPER=" in first and "SHIFDE_SECRET_KEYS=" in first
    _ensure_dev_env_values(f)  # idempotent: values are generated once and then stable
    assert f.read_text(encoding="utf-8-sig") == first
    s = Settings(_env_file=f, env="development")
    assert len(s.pepper) >= 32 and s.keyring.active_id == "dev1"
    # an explicit environment variable wins and nothing is appended for it
    g = tmp_path / "other.env"
    g.write_text("SHIFDE_DATABASE_URL=postgresql+psycopg://x\n")
    monkeypatch.setenv("SHIFDE_API_KEY_PEPPER", "e" * 40)
    _ensure_dev_env_values(g)
    assert "SHIFDE_API_KEY_PEPPER" not in g.read_text() and "SHIFDE_SECRET_KEYS=" in g.read_text()


def test_problem_details_scrub_registered_secrets() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shifde.core.errors import Conflict
    from shifde.core.problem import install_error_handlers
    from shifde.core.redaction import register_secret

    register_secret("problem-detail-secret-1")
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise Conflict("failed using problem-detail-secret-1 and Bearer abcdefghijklmnop")

    r = TestClient(app).get("/boom")
    assert r.status_code == 409
    assert "problem-detail-secret-1" not in r.text and "abcdefghijklmnop" not in r.text
