"""`shifde-admin config preflight` and the go-live register (SP4-1, design
`2026-11-21-subproject-4-go-live-design.md` 4.7, 4.8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core import preflight
from shifde.core.crypto import KeyRing
from shifde.core.health import MigrationState

PEPPER = "preflight-pepper-" + "q" * 40
METRICS = "preflight-metrics-token-0123456789"
KEYS = KeyRing.generate_entry("k1")
UNREACHABLE = "postgresql+psycopg://shifde_app:unreachable-password@127.0.0.1:1/shifde"

HEADER = "| id | state | evidence |\n|---|---|---|\n"
NO_OPEN = HEADER + (
    "| Item 21 | closed | closed by #47 |\n"
    "| REQ F-08 | pending-owner | the owner must accept B-2655's residual risk |\n"
)


def _secrets(app_url: str) -> list[str]:
    password = make_url(app_url).password
    assert password
    return [PEPPER, METRICS, KEYS.split(":", 1)[1], password, "unreachable-password"]


def _env(app_url: str, **overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        "SHIFDE_ENV": "production",
        "SHIFDE_DATABASE_URL": app_url,
        "SHIFDE_API_KEY_PEPPER": PEPPER,
        "SHIFDE_SECRET_KEYS": KEYS,
        "SHIFDE_METRICS_TOKEN": METRICS,
        "SHIFDE_PUBLIC_BASE_URL": "https://seth.example.com",
        "SHIFDE_ALLOWED_ORIGINS": "https://seth.example.com",
        "SHIFDE_TRUSTED_PROXIES": "10.0.0.0/24",
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _preflight(
    tmp_path: Path, environ: dict[str, str], register: str = NO_OPEN
) -> tuple[int, list[str]]:
    env_file = tmp_path / "prod.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in environ.items()), encoding="utf-8")
    reg = tmp_path / "register.md"
    reg.write_text(register, encoding="utf-8")
    result = CliRunner().invoke(
        cli_main.app,
        ["config", "preflight", "--env-file", str(env_file), "--register", str(reg)],
    )
    return result.exit_code, result.output.splitlines()


def test_a_passing_production_env_prints_only_ok_and_pending_lines_and_exits_0(
    tmp_path: Path, app_url: str
) -> None:
    code, lines = _preflight(tmp_path, _env(app_url))
    assert lines == [
        "ok: trusted proxies",
        "ok: hsts",
        "ok: api docs",
        "ok: local login",
        "ok: metrics token",
        "ok: mcp access",
        "ok: database role",
        "ok: migrations",
        "ok: register",
        "pending: REQ F-08",
    ]
    assert code == 0
    for secret in _secrets(app_url):
        assert secret not in "\n".join(lines)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"SHIFDE_TRUSTED_PROXIES": None}, "refused: trusted proxies:"),
        ({"SHIFDE_HSTS_MAX_AGE_SECONDS": "15551999"}, "refused: hsts:"),
        ({"SHIFDE_API_DOCS_ENABLED": "true"}, "refused: api docs:"),
        (
            {"SHIFDE_AUTH_LOCAL_ENABLED": "true", "SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION": "true"},
            "refused: local login:",
        ),
    ],
)
def test_each_check_refuses_and_exits_1(
    tmp_path: Path, app_url: str, overrides: dict[str, Any], expected: str
) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, **overrides))
    assert code == 1
    assert [line for line in lines if line.startswith("refused:")] == [
        next(line for line in lines if line.startswith(expected))
    ]
    for secret in _secrets(app_url):
        assert secret not in "\n".join(lines)


def test_trusting_the_loopback_proxy_satisfies_the_proxy_check(
    tmp_path: Path, app_url: str
) -> None:
    environ = _env(app_url, SHIFDE_TRUSTED_PROXIES=None, SHIFDE_TRUST_LOOPBACK_PROXY="true")
    code, lines = _preflight(tmp_path, environ)
    assert code == 0 and "ok: trusted proxies" in lines


def test_the_metrics_token_line_outside_production(tmp_path: Path, app_url: str) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, SHIFDE_ENV="test", SHIFDE_METRICS_TOKEN=None))
    assert "refused: metrics token: SHIFDE_METRICS_TOKEN is not set" in lines and code == 1
    _, lines = _preflight(tmp_path, _env(app_url, SHIFDE_ENV="test", SHIFDE_API_DOCS_ENABLED="0"))
    assert "ok: metrics token" in lines


def test_mcp_access_on_without_allowed_hosts_is_refused(tmp_path: Path, app_url: str) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, SHIFDE_MCP_ENABLED="true"))
    assert code == 1
    assert (
        "refused: mcp access: SHIFDE_MCP_ENABLED is true and SHIFDE_MCP_ALLOWED_HOSTS is empty"
        in lines
    )
    code, lines = _preflight(
        tmp_path,
        _env(app_url, SHIFDE_MCP_ENABLED="true", SHIFDE_MCP_ALLOWED_HOSTS="seth.example.com"),
    )
    assert "ok: mcp access" in lines and code == 0


def test_an_accepted_req_f22_row_permits_local_login(tmp_path: Path, app_url: str) -> None:
    register = (
        NO_OPEN + '| REQ F-22 | accepted | "go live on local login", owner, 2026-11-22, #123 |\n'
    )
    environ = _env(
        app_url, SHIFDE_AUTH_LOCAL_ENABLED="true", SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION="true"
    )
    code, lines = _preflight(tmp_path, environ, register)
    assert "ok: local login (REQ F-22 accepted)" in lines and code == 0
    pending = NO_OPEN + "| REQ F-22 | pending-owner | the owner must accept local login |\n"
    code, lines = _preflight(tmp_path, environ, pending)
    assert code == 1 and any(line.startswith("refused: local login:") for line in lines)
    assert "pending: REQ F-22" in lines


def test_an_unreachable_database_is_refused_and_skips_the_migration_check(
    tmp_path: Path, app_url: str
) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, SHIFDE_DATABASE_URL=UNREACHABLE))
    assert code == 1
    assert "refused: database unreachable" in lines
    assert not any("migrations" in line for line in lines)
    assert "unreachable-password" not in "\n".join(lines)


def test_a_privileged_role_is_refused(tmp_path: Path, app_url: str, owner_url: str) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, SHIFDE_DATABASE_URL=owner_url))
    assert code == 1
    assert any(line.startswith("refused: database role:") for line in lines)
    password = make_url(owner_url).password
    assert password and password not in "\n".join(lines)


def test_pending_migrations_are_refused(
    tmp_path: Path, app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        preflight, "migration_state", lambda session: MigrationState("pending", "a", "b")
    )
    code, lines = _preflight(tmp_path, _env(app_url))
    assert code == 1 and "refused: migrations: status pending" in lines


def test_an_invalid_configuration_stops_before_the_checks(tmp_path: Path, app_url: str) -> None:
    code, lines = _preflight(tmp_path, _env(app_url, SHIFDE_METRICS_TOKEN=None))
    assert code == 1
    assert lines[0] == "config INVALID" and lines[-1] == "refused: configuration invalid"
    assert any("SHIFDE_METRICS_TOKEN" in line for line in lines)
    assert not any(line.startswith("ok:") for line in lines)
    for secret in _secrets(app_url):
        assert secret not in "\n".join(lines)


def test_an_open_row_is_refused(tmp_path: Path, app_url: str) -> None:
    register = NO_OPEN + "| Item 5a | open | closes with #124 and #125 |\n"
    code, lines = _preflight(tmp_path, _env(app_url), register)
    assert code == 1
    assert "ok: register" in lines and "refused: open: Item 5a" in lines


def test_a_missing_or_invalid_register_is_refused(tmp_path: Path, app_url: str) -> None:
    code, lines = _preflight(tmp_path, _env(app_url), "# no table here\n")
    assert code == 1 and any(line.startswith("refused: register invalid:") for line in lines)
    env_file = tmp_path / "prod.env"
    result = CliRunner().invoke(
        cli_main.app,
        ["config", "preflight", "--env-file", str(env_file), "--register", str(tmp_path / "no")],
    )
    assert result.exit_code == 1
    assert "refused: register unreadable (FileNotFoundError)" in result.output


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ("| a | closed |\n", "line 3: expected 3 cells, found 2"),
        ("|  | closed | x |\n", "line 3: empty id"),
        ("| a | done | x |\n", "line 3: state must be one of"),
        ("| a | closed |  |\n", "line 3: empty evidence"),
        ("| a | closed | x |\n| a | open | y |\n", "line 4: duplicate id 'a'"),
    ],
)
def test_register_rows_are_validated(row: str, reason: str) -> None:
    with pytest.raises(preflight.RegisterError, match=reason):
        preflight.parse_register(HEADER + row)


def test_the_register_parser_reads_the_first_matching_table() -> None:
    text = (
        "intro\n\n| other | table |\n|---|---|\n| x | y |\n\n"
        + HEADER
        + '| Item 21 | closed | #47 |\n| ARCH M11 | accepted | "ok" owner 2026-11-22 issue |\n'
        + "\nafter\n\n"
        + HEADER
        + "| ignored | bogus | x |\n"
    )
    rows = preflight.parse_register(text)
    assert [(r.id, r.state, r.line) for r in rows] == [
        ("Item 21", "closed", 9),
        ("ARCH M11", "accepted", 10),
    ]
    with pytest.raises(preflight.RegisterError, match="separator"):
        preflight.parse_register("| id | state | evidence |\n| a | closed | x |\n")


def test_the_committed_register_parses() -> None:
    rows = preflight.read_register(preflight.DEFAULT_REGISTER)
    assert {r.id: r.state for r in rows} == {
        "Item 5a": "closed",
        "Item 20 delete": "closed",
        "Item 20 audit-retention": "accepted",
        "Item 21": "closed",
        "Item 23": "accepted",
        "ARCH M11": "closed",
        "ARCH M5": "closed",
        "REQ F-08": "accepted",
        "REQ F-22": "pending-owner",
        "Hosting target": "accepted",
    }
