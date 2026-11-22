"""`shifde-admin backup create` / `backup verify` (go-live design 6.1, SP4-4, B-2791).

Behaviour: a create/verify round trip over two seeded organizations (row counts equal the owner's
own `count(*)`, the volatile tables are listed and counted, the catalog fingerprint holds RLS
flags, policies and grants to the app role, `pg_restore --list` reads the dump); a tampered dump,
a tampered manifest and a too-new revision are each refused by verify; no argv element holds the
owner password, which reaches `pg_dump` only as `PGPASSWORD`; the manifest holds key ids and no
key material; a non-empty output directory and an old `pg_dump` are refused; on POSIX the files
are owner-only.

Data is seeded and counted as the schema owner; the command connects as the owner too.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from shifde.cli import backup
from shifde.cli import main as cli_main
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, read_db_revisions
from shifde.core.settings import Settings
from tests._support.runs import add_run
from tests._support.tenants import add_tenant


@pytest.fixture
def cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    return CliRunner()


def _create(runner: CliRunner, out: Path) -> Any:  # Any: click Result
    return runner.invoke(cli_main.app, ["backup", "create", "--out", str(out)])


def _verify(runner: CliRunner, directory: Path) -> Any:  # Any: click Result
    return runner.invoke(cli_main.app, ["backup", "verify", "--from", str(directory)])


def _seed_two_clients(owner_db: Database) -> None:
    with owner_db.session() as o:
        for _ in range(2):
            add_run(o, add_tenant(o))
        o.commit()


def _manifest(directory: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / backup.MANIFEST).read_text(encoding="utf-8"))
    return loaded


def _write_manifest(directory: Path, manifest: Mapping[str, Any]) -> None:
    (directory / backup.MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


@pytest.fixture
def made(cli: CliRunner, owner_db: Database, tmp_path: Path) -> Path:
    _seed_two_clients(owner_db)
    out = tmp_path / "backup"
    result = _create(cli, out)
    assert result.exit_code == 0, result.output
    return out


def test_round_trip_counts_fingerprint_and_verify(
    cli: CliRunner, made: Path, owner_db: Database, settings: Settings
) -> None:
    raw = (made / backup.MANIFEST).read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert list(manifest) == sorted(manifest)
    assert set(backup.REQUIRED_FIELDS) <= set(manifest)
    assert manifest["format"] == 1
    assert manifest["manifest_sha256"] == backup.manifest_digest(manifest)
    with owner_db.session() as o:
        tenants = o.execute(text("SELECT count(*) FROM shifde.tenants")).scalar_one()
        runs = o.execute(text("SELECT count(*) FROM shifde.runs")).scalar_one()
        revisions = sorted(read_db_revisions(o))
    assert tenants >= 2 and runs >= 2
    assert manifest["row_counts"]["tenants"] == tenants
    assert manifest["row_counts"]["runs"] == runs
    assert manifest["volatile_tables"] == [
        "login_lockouts",
        "oidc_auth_requests",
        "sessions",
        "workers",
    ]
    assert all(name in manifest["row_counts"] for name in manifest["volatile_tables"])
    fingerprint = manifest["catalog_fingerprint"]
    assert fingerprint["tables"]["tenants"] == {
        "relrowsecurity": True,
        "relforcerowsecurity": True,
    }
    assert fingerprint["policies"]
    app_role = make_url(settings.database_url).username or "shifde_app"
    assert fingerprint["app_role"] == app_role
    assert fingerprint["app_role_grants"]
    assert manifest["alembic_revisions"] == revisions
    assert manifest["database"] == make_url(settings.effective_migration_url).database
    listed = subprocess.run(  # nosec B603
        [backup.find_pg_tool("pg_restore"), "--list", str(made / backup.DUMP_FILE)],
        capture_output=True,
        check=False,
    )
    assert listed.returncode == 0
    result = _verify(cli, made)
    assert result.exit_code == 0, result.output
    assert "backup verified" in result.output
    assert all(revision in result.output for revision in revisions)


def test_two_creates_of_an_unchanged_database_give_equal_fingerprints(
    cli: CliRunner, made: Path, tmp_path: Path
) -> None:
    again = tmp_path / "again"
    assert _create(cli, again).exit_code == 0
    first, second = _manifest(made), _manifest(again)
    assert first["catalog_fingerprint"] == second["catalog_fingerprint"]


def test_a_tampered_dump_is_refused(cli: CliRunner, made: Path) -> None:
    dump = made / backup.DUMP_FILE
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    dump.write_bytes(bytes(data))
    result = _verify(cli, made)
    assert result.exit_code == 1
    assert "dump digest" in result.output


def test_a_tampered_manifest_is_refused(cli: CliRunner, made: Path) -> None:
    manifest = _manifest(made)
    manifest["row_counts"]["tenants"] += 1
    _write_manifest(made, manifest)
    result = _verify(cli, made)
    assert result.exit_code == 1
    assert "own digest" in result.output


def test_a_revision_unknown_to_the_code_is_refused(cli: CliRunner, made: Path) -> None:
    manifest = _manifest(made)
    manifest["alembic_revisions"] = ["zz_future_9999"]
    manifest["manifest_sha256"] = backup.manifest_digest(manifest)
    _write_manifest(made, manifest)
    result = _verify(cli, made)
    assert result.exit_code == 1
    assert "revision zz_future_9999" in result.output


def test_the_password_reaches_pg_dump_only_through_pgpassword(
    cli: CliRunner, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = make_url(settings.effective_migration_url)
    password = owner.password
    assert password
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = backup._run_tool

    def recording(argv: list[str], env: Mapping[str, str], secret: str | None, what: str) -> str:
        calls.append((list(argv), dict(env)))
        return real(argv, env, secret, what)

    monkeypatch.setattr(backup, "_run_tool", recording)
    result = _create(cli, tmp_path / "out")
    assert result.exit_code == 0, result.output
    dumps = [env for argv, env in calls if "-Fc" in argv]
    assert len(dumps) == 1
    assert dumps[0]["PGPASSWORD"] == password
    full_url = owner.render_as_string(hide_password=False)
    for argv, _ in calls:
        assert all(str(password) not in arg and full_url not in arg for arg in argv)
    assert str(password) not in result.output


def test_the_tools_get_no_shifde_variable(
    cli: CliRunner, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHIFDE_BACKUP_CANARY", "leak")
    envs: list[dict[str, str]] = []
    real = backup._run_tool

    def recording(argv: list[str], env: Mapping[str, str], secret: str | None, what: str) -> str:
        envs.append(dict(env))
        return real(argv, env, secret, what)

    monkeypatch.setattr(backup, "_run_tool", recording)
    result = _create(cli, tmp_path / "out")
    assert result.exit_code == 0, result.output
    assert len(envs) == 2  # pg_dump --version, pg_dump -Fc
    allowed = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "PGPASSWORD", "PGSSLMODE"}
    for env in envs:
        assert not [name for name in env if name.upper().startswith("SHIFDE_")]
        assert set(env) <= allowed


def test_a_pg_dump_failure_shows_the_password_as_stars(
    cli: CliRunner, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password = str(make_url(settings.effective_migration_url).password)
    real = subprocess.run

    def failing(argv: list[str], **kwargs: Any) -> Any:  # Any: subprocess.run's signature
        if "-Fc" not in argv:
            return real(argv, **kwargs)
        Path(argv[argv.index("-f") + 1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(
            argv, 1, "", f'FATAL: password "{password}" rejected for user\n'
        )

    monkeypatch.setattr(backup.subprocess, "run", failing)
    out = tmp_path / "out"
    result = _create(cli, out)
    assert result.exit_code == 1
    assert 'password "***" rejected' in result.output
    assert password not in result.output
    assert not (out / backup.DUMP_FILE).exists()
    assert not (out / backup.MANIFEST).exists()


def test_an_interrupted_pg_dump_leaves_no_partial_dump(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = backup._run_tool

    def interrupted(argv: list[str], env: Mapping[str, str], secret: str | None, what: str) -> str:
        if "-Fc" not in argv:
            return real(argv, env, secret, what)
        Path(argv[argv.index("-f") + 1]).write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(backup, "_run_tool", interrupted)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        backup.create_backup(settings.effective_migration_url, "shifde_app", settings.keyring, out)
    assert not (out / backup.DUMP_FILE).exists()


def test_the_ssl_parameters_of_the_url_become_pg_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIFDE_BACKUP_CANARY", "leak")
    parsed = backup._parse_url(
        "postgresql+psycopg://owner:pw@db.example:5432/shifde?sslmode=verify-full"
        "&sslrootcert=/ca.pem&sslcert=/c.pem&sslkey=/c.key&sslcrl=/crl.pem"
    )
    env = backup._pg_env(parsed)
    assert {k: v for k, v in env.items() if k.startswith("PG")} == {
        "PGPASSWORD": "pw",
        "PGSSLMODE": "verify-full",
        "PGSSLROOTCERT": "/ca.pem",
        "PGSSLCERT": "/c.pem",
        "PGSSLKEY": "/c.key",
        "PGSSLCRL": "/crl.pem",
    }
    assert "SHIFDE_BACKUP_CANARY" not in env
    assert backup._conn_args(parsed) == [
        "-h",
        "db.example",
        "-p",
        "5432",
        "-U",
        "owner",
        "-d",
        "shifde",
    ]


def test_a_url_query_parameter_backup_does_not_pass_on_is_refused() -> None:
    with pytest.raises(backup.BackupError, match="connect_timeout"):
        backup._parse_url(
            "postgresql+psycopg://owner:pw@db/shifde?sslmode=require&connect_timeout=5"
        )


def test_the_manifest_holds_key_ids_and_no_key_material(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, second = KeyRing.generate_entry("kold"), KeyRing.generate_entry("knew")
    ring = f"{second},{first}"
    keyed = settings.model_copy(update={"secret_keys": SecretStr(ring)})
    monkeypatch.setattr(cli_main, "_settings", lambda: keyed)
    out = tmp_path / "out"
    result = _create(CliRunner(), out)
    assert result.exit_code == 0, result.output
    raw = (out / backup.MANIFEST).read_text(encoding="utf-8")
    assert json.loads(raw)["secret_key_ids"] == ["knew", "kold"]
    for secret in (ring, first.split(":", 1)[1], second.split(":", 1)[1]):
        assert secret not in raw
        assert secret not in result.output


def test_a_non_empty_output_directory_is_refused(cli: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    result = _create(cli, out)
    assert result.exit_code == 1
    assert "error:" in result.output
    assert [p.name for p in out.iterdir()] == ["keep.txt"]
    assert (out / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_an_older_pg_dump_than_the_server_is_refused(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backup, "_pg_dump_version", lambda _: "pg_dump (PostgreSQL) 9.6")
    out = tmp_path / "out"
    result = _create(cli, out)
    assert result.exit_code == 1
    assert "older than the server" in result.output
    assert not (out / backup.MANIFEST).exists()


@pytest.mark.skipif(os.name != "posix", reason="owner-only modes are POSIX; Windows inherits ACLs")
def test_the_backup_is_owner_only(made: Path) -> None:
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    for name in (backup.DUMP_FILE, backup.MANIFEST):
        assert stat.S_IMODE((made / name).stat().st_mode) == 0o600
