"""`shifde-admin backup restore` (go-live design 6.1, section 11 calls 15-20, SP4-5).

Behaviour: two seeded organizations (people, runs, agent artifacts, secrets, audit events) are
backed up with `backup create` and restored into an empty scratch database; the seven checks print
`ok` and the command ends with `RESTORE VERIFIED`. Through the full app on the restored copy, as
the app role, A's administrator lists only A's secrets and B's secret is a 404 like an unknown id.
A tampered dump, a tampered manifest and a too-new revision are refused before the target is
touched; a relation in `public`, a target owned by another role, a target URL of another user and
a missing app role are each refused. A dropped policy fails the fingerprint check by name; a ring
lacking the secrets' key fails the secrets check; a weakened policy, unforced row-level security, a
wide grant or an extra SECURITY DEFINER function behind a forged manifest passes the fingerprint
but fails the catalog rules check by name; the password reaches `pg_restore` only as
`PGPASSWORD`.

The source is a clone of the run's migrated template, seeded once per module; each scratch target
is created from `template0` and dropped at teardown. Names carry the run's prefix, so a dying
worker's leftovers are dropped at session end.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from filelock import FileLock
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from shifde.cli import backup
from shifde.cli import main as cli_main
from shifde.core.crypto import KeyRing
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.fullapp import ORIGIN, World, fresh_world, full_client, login
from tests._support.runs import add_run
from tests.conftest import (
    DB_PREFIX,
    _create_database,
    _drop_database,
    _ensure_template,
    _env,
    _lock_path,
    _with_db,
)

TARGET_VAR = "SHIFDE_TEST_RESTORE_TARGET_URL"
A, B = "/api/v1/orgs/demo-a", "/api/v1/orgs/demo-b"
PLAIN = {"demo-a": "restore-plain-A-0123456789", "demo-b": "restore-plain-B-0123456789"}
RING = KeyRing.generate_entry("rk1")
APP_URL = _env("SHIFDE_TEST_DATABASE_URL")
OWNER_URL = _env("SHIFDE_TEST_MIGRATION_DATABASE_URL")
APP_ROLE = make_url(APP_URL).username or "shifde_app"
OWNER_ROLE = make_url(OWNER_URL).username or "shifde_owner"


def _settings_for(database: str, **update: Any) -> Settings:  # Any: Settings field values
    base = Settings(
        _env_file=None,
        env="test",
        database_url=_with_db(APP_URL, database),
        migration_database_url=_with_db(OWNER_URL, database),
        auth_local_enabled=True,
        login_rate_per_account_per_minute=0,
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="INFO",
        secret_keys=SecretStr(RING),
    )
    return base.model_copy(update=update) if update else base


@pytest.fixture(scope="module")
def admin(admin_url: str) -> Iterator[Engine]:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    yield engine
    engine.dispose()


def _scratch(run_id: str, tag: str) -> str:
    return f"{DB_PREFIX}{run_id}_{tag}{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def source(run_id: str, admin: Engine) -> Iterator[tuple[str, World, dict[str, str]]]:
    """(database, world, ids) of a seeded clone of the run's template; `ids` maps
    `secret:<slug>` and `run:<slug>` to that organization's secret and run."""
    name = _scratch(run_id, "rs")
    try:
        with FileLock(str(_lock_path(run_id))):
            template = _ensure_template(admin, run_id, OWNER_URL, APP_ROLE)
        with admin.connect() as c:
            _create_database(c, name, OWNER_ROLE, APP_ROLE, template)
        owner = Database(_with_db(OWNER_URL, name), pool_size=1, verify_role=False)
        try:
            runs: dict[str, str] = {}
            with owner.session() as o:
                world = fresh_world(o)
                for slug in ("demo-a", "demo-b"):
                    tenant = o.execute(
                        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": slug}
                    ).scalar_one()
                    # with a workspace, a project and a published agent artifact
                    runs[slug] = str(add_run(o, tenant))
                o.commit()
        finally:
            owner.dispose()
        ids: dict[str, str] = {}
        with full_client(_settings_for(name)) as client:
            for slug, email in (("demo-a", "admin@demo-a.local"), ("demo-b", "admin@demo-b.local")):
                headers = login(client, world, email)
                made = client.post(
                    f"/api/v1/orgs/{slug}/secrets",
                    json={"name": f"restore-{slug}", "value": PLAIN[slug]},
                    headers=headers,
                )
                assert made.status_code == 201, made.text
                ids[f"secret:{slug}"] = made.json()["id"]
                ids[f"run:{slug}"] = runs[slug]
                client.cookies.clear()
        yield name, world, ids
    finally:
        with admin.connect() as c:
            _drop_database(c, name)


def _create_cli(runner: CliRunner, out: Path) -> Any:  # Any: click Result
    return runner.invoke(cli_main.app, ["backup", "create", "--out", str(out)])


def _restore_cli(runner: CliRunner, directory: Path) -> Any:  # Any: click Result
    return runner.invoke(
        cli_main.app,
        ["backup", "restore", "--from", str(directory), "--target-url-env", TARGET_VAR],
    )


@pytest.fixture(scope="module")
def made(
    source: tuple[str, World, dict[str, str]], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    out = tmp_path_factory.mktemp("restore") / "backup"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli_main, "_settings", lambda: _settings_for(source[0]))
        result = _create_cli(CliRunner(), out)
    assert result.exit_code == 0, result.output
    return out


@pytest.fixture
def copy(made: Path, tmp_path: Path) -> Path:
    """This test's own copy of the backup (refusal tests edit it)."""
    return Path(shutil.copytree(made, tmp_path / "backup"))


def _make_target(admin: Engine, run_id: str, owner: str = OWNER_ROLE) -> str:
    name = _scratch(run_id, "rt")
    with admin.connect() as c:
        _create_database(c, name, owner, APP_ROLE, "template0")
        if owner != OWNER_ROLE:
            c.execute(text(f"GRANT CONNECT ON DATABASE {name} TO {OWNER_ROLE}"))
    return name


@pytest.fixture
def new_target(admin: Engine, run_id: str) -> Iterator[Callable[..., str]]:
    """Make empty scratch targets (dropped at teardown)."""
    names: list[str] = []

    def make(owner: str = OWNER_ROLE) -> str:
        names.append(_make_target(admin, run_id, owner))
        return names[-1]

    yield make
    with admin.connect() as c:
        for name in names:
            _drop_database(c, name)


@pytest.fixture
def target(new_target: Callable[..., str], monkeypatch: pytest.MonkeyPatch) -> str:
    """An empty target owned by the schema owner, named by `TARGET_VAR`."""
    name = new_target()
    monkeypatch.setenv(TARGET_VAR, _with_db(OWNER_URL, name))
    return name


@pytest.fixture
def cli(source: tuple[str, World, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli_main, "_settings", lambda: _settings_for(source[0]))
    return CliRunner()


def _is_empty(admin_url: str, name: str) -> bool:
    engine = create_engine(_with_db(admin_url, name), poolclass=NullPool)
    try:
        with engine.connect() as c:
            found = c.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname NOT IN "
                    "('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'"
                )
            ).scalar_one()
            schema = c.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname = 'shifde'")
            ).scalar_one()
    finally:
        engine.dispose()
    return found == 0 and schema == 0


def _manifest(directory: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / backup.MANIFEST).read_text(encoding="utf-8"))
    return loaded


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    (directory / backup.MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _assert_refused(result: Any, reason: str) -> None:  # Any: click Result
    assert result.exit_code == 1, result.output
    assert reason in result.output
    assert "RESTORE VERIFIED" not in result.output


# ------------------------------------------------------------------------------ happy path
def test_create_then_restore_passes_all_checks(cli: CliRunner, target: str, tmp_path: Path) -> None:
    out = tmp_path / "fresh"
    created = _create_cli(cli, out)
    assert created.exit_code == 0, created.output
    counts = _manifest(out)["row_counts"]
    for table in ("tenants", "runs", "secrets", "audit_events", "workspaces"):
        assert counts[table] >= 2, table
    result = _restore_cli(cli, out)
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert [line for line in lines if line.startswith(("ok ", "FAILED"))] == [
        f"ok {name}" for name in backup.RESTORE_CHECKS
    ]
    assert lines[-1] == "RESTORE VERIFIED"
    password = str(make_url(OWNER_URL).password)
    assert password not in result.output


@pytest.fixture(scope="module")
def restored(
    made: Path, source: tuple[str, World, dict[str, str]], admin: Engine, run_id: str
) -> Iterator[str]:
    name = _make_target(admin, run_id)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cli_main, "_settings", lambda: _settings_for(source[0]))
            mp.setenv(TARGET_VAR, _with_db(OWNER_URL, name))
            result = _restore_cli(CliRunner(), made)
        assert result.exit_code == 0, result.output
        yield name
    finally:
        with admin.connect() as c:
            _drop_database(c, name)


def test_the_restored_copy_keeps_the_organizations_apart(
    restored: str, source: tuple[str, World, dict[str, str]]
) -> None:
    _, world, ids = source
    with full_client(_settings_for(restored)) as client:
        headers = login(client, world, "admin@demo-a.local")
        listed = client.get(f"{A}/secrets", headers=headers)
        assert listed.status_code == 200, listed.text
        names = [s["name"] for s in listed.json()["items"]]
        assert "restore-demo-a" in names
        assert "restore-demo-b" not in names
        runs = client.get(f"{A}/runs", headers=headers)
        assert runs.status_code == 200, runs.text
        run_ids = [r["id"] for r in runs.json()["items"]]
        assert ids["run:demo-a"] in run_ids
        assert ids["run:demo-b"] not in run_ids
        other = client.get(f"{A}/runs/{ids['run:demo-b']}", headers=headers)
        unknown = client.get(f"{A}/runs/{uuid.uuid4()}", headers=headers)
        assert other.status_code == unknown.status_code == 404
        assert other.json()["detail"] == unknown.json()["detail"]
        assert client.get(f"{B}/secrets", headers=headers).status_code == 404


# ------------------------------------------------------------------------------ refusals
def test_a_tampered_dump_is_refused_before_the_target(
    cli: CliRunner, copy: Path, target: str, admin_url: str
) -> None:
    dump = copy / backup.DUMP_FILE
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    dump.write_bytes(bytes(data))
    _assert_refused(_restore_cli(cli, copy), "dump digest")
    assert _is_empty(admin_url, target)


def test_a_tampered_manifest_is_refused_before_the_target(
    cli: CliRunner, copy: Path, target: str, admin_url: str
) -> None:
    manifest = _manifest(copy)
    manifest["row_counts"]["tenants"] += 1
    _write_manifest(copy, manifest)
    _assert_refused(_restore_cli(cli, copy), "own digest")
    assert _is_empty(admin_url, target)


def test_a_revision_newer_than_the_code_is_refused_before_the_target(
    cli: CliRunner, copy: Path, target: str, admin_url: str
) -> None:
    manifest = _manifest(copy)
    manifest["alembic_revisions"] = ["zz_future_9999"]
    manifest["manifest_sha256"] = backup.manifest_digest(manifest)
    _write_manifest(copy, manifest)
    _assert_refused(_restore_cli(cli, copy), "revision zz_future_9999")
    assert _is_empty(admin_url, target)


def test_a_relation_in_public_is_refused(
    cli: CliRunner, made: Path, target: str, admin_url: str
) -> None:
    engine = create_engine(_with_db(admin_url, target), poolclass=NullPool)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE TABLE public.leftover (id int)"))
    finally:
        engine.dispose()
    _assert_refused(_restore_cli(cli, made), "not empty: public.leftover")


def test_a_target_owned_by_another_role_is_refused(
    cli: CliRunner,
    made: Path,
    new_target: Callable[..., str],
    admin_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = make_url(admin_url).username or "postgres"
    name = new_target(owner=other)
    monkeypatch.setenv(TARGET_VAR, _with_db(OWNER_URL, name))
    _assert_refused(_restore_cli(cli, made), f"owned by {other}, not by {OWNER_ROLE}")
    assert _is_empty(admin_url, name)


def test_a_target_url_of_another_user_is_refused(
    cli: CliRunner, made: Path, target: str, admin_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TARGET_VAR, _with_db(APP_URL, target))
    result = _restore_cli(cli, made)
    _assert_refused(result, "not the schema owner role")
    assert str(make_url(APP_URL).password) not in result.output
    assert _is_empty(admin_url, target)


def test_a_missing_app_role_is_refused(
    made: Path,
    target: str,
    admin_url: str,
    source: tuple[str, World, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renamed = make_url(_with_db(APP_URL, source[0])).set(username="shifde_app_missing")
    settings = _settings_for(source[0], database_url=renamed.render_as_string(hide_password=False))
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    _assert_refused(_restore_cli(CliRunner(), made), "missing on the target cluster")
    assert _is_empty(admin_url, target)


def test_an_unset_target_variable_is_refused(
    cli: CliRunner, made: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TARGET_VAR, raising=False)
    monkeypatch.delenv(f"{TARGET_VAR}_FILE", raising=False)
    _assert_refused(_restore_cli(cli, made), f"neither {TARGET_VAR} nor {TARGET_VAR}_FILE")


def test_the_target_url_may_come_from_a_file(
    cli: CliRunner,
    made: Path,
    new_target: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    name = new_target()
    url_file = tmp_path / "target.url"
    url_file.write_text(_with_db(OWNER_URL, name) + "\n", encoding="utf-8")
    monkeypatch.delenv(TARGET_VAR, raising=False)
    monkeypatch.setenv(f"{TARGET_VAR}_FILE", str(url_file))
    result = _restore_cli(cli, made)
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == "RESTORE VERIFIED"


# ------------------------------------------------------------------------------ failed checks
def test_a_dropped_policy_fails_the_fingerprint_check_by_name(
    cli: CliRunner,
    made: Path,
    target: str,
    source: tuple[str, World, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _restore_cli(cli, made).exit_code == 0
    target_url = _with_db(OWNER_URL, target)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with engine.begin() as c:
            table, policy = c.execute(
                text(
                    "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'shifde' "
                    "ORDER BY 1, 2 LIMIT 1"
                )
            ).one()
            c.execute(text(f'DROP POLICY "{policy}" ON shifde."{table}"'))
    finally:
        engine.dispose()
    settings = _settings_for(source[0])
    results = backup.restore_checks(
        _manifest(made), target_url, settings.database_url, APP_ROLE, settings.keyring
    )
    by_name = {r.name: r for r in results}
    assert not by_name["fingerprint"].ok
    assert f"policy {table}.{policy}" in by_name["fingerprint"].detail
    assert all(r.ok for r in results if r.name != "fingerprint")
    with pytest.raises(typer.Exit) as exited:
        cli_main._report_restore_checks(results)
    assert exited.value.exit_code == 1
    captured = capsys.readouterr()
    assert "FAILED fingerprint" in captured.out
    assert "RESTORE VERIFIED" not in captured.out + captured.err


FORGERIES = {
    "weak policy": (
        ["CREATE POLICY zz_forged_read ON shifde.runs FOR SELECT USING (true)"],
        "shifde.runs: policy zz_forged_read (SELECT) USING constant true",
    ),
    "no forced rls": (
        ["ALTER TABLE shifde.runs NO FORCE ROW LEVEL SECURITY"],
        "shifde.runs: row level security not forced",
    ),
    "wide grant": (
        [f"GRANT TRUNCATE ON shifde.runs TO {APP_ROLE}"],
        f"shifde.runs: {APP_ROLE} holds TRUNCATE",
    ),
    "extra definer": (
        [
            "CREATE FUNCTION shifde.zz_forged() RETURNS int LANGUAGE sql SECURITY DEFINER"
            " SET search_path = pg_catalog, pg_temp AS $$ SELECT 1 $$",
            "REVOKE EXECUTE ON FUNCTION shifde.zz_forged() FROM PUBLIC",
        ],
        "shifde.zz_forged: SECURITY DEFINER but not on the allowlist",
    ),
}


@pytest.mark.parametrize(("statements", "named"), list(FORGERIES.values()), ids=list(FORGERIES))
def test_a_forged_manifest_passes_the_fingerprint_but_not_the_catalog_rules(
    cli: CliRunner,
    made: Path,
    target: str,
    source: tuple[str, World, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    statements: list[str],
    named: str,
) -> None:
    """The manifest is unsigned: a forger who weakens the restored catalog can recompute its
    fingerprint and digest. The catalog rules check reads only the catalog, so it still fails."""
    assert _restore_cli(cli, made).exit_code == 0
    target_url = _with_db(OWNER_URL, target)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with engine.begin() as c:
            for statement in statements:
                c.execute(text(statement))
    finally:
        engine.dispose()
    forged = _manifest(made)
    owner = Database(target_url, pool_size=1, max_overflow=0, verify_role=False)
    try:
        with owner.session() as session:
            fingerprint = backup.catalog_fingerprint(session, APP_ROLE)
            session.rollback()
    finally:
        owner.dispose()
    forged["catalog_fingerprint"] = json.loads(json.dumps(fingerprint))
    forged["manifest_sha256"] = backup.manifest_digest(forged)
    settings = _settings_for(source[0])
    results = backup.restore_checks(
        forged, target_url, settings.database_url, APP_ROLE, settings.keyring
    )
    by_name = {r.name: r for r in results}
    assert by_name["fingerprint"].ok
    assert not by_name["catalog rules"].ok
    assert by_name["catalog rules"].detail.startswith("violates: ")
    assert named in by_name["catalog rules"].detail
    assert all(r.ok for r in results if r.name != "catalog rules")
    with pytest.raises(typer.Exit) as exited:
        cli_main._report_restore_checks(results)
    assert exited.value.exit_code == 1
    captured = capsys.readouterr()
    assert "ok fingerprint" in captured.out
    assert "FAILED catalog rules: violates: " in captured.out
    assert "RESTORE VERIFIED" not in captured.out + captured.err


def test_a_ring_lacking_the_key_fails_the_secrets_check(
    made: Path,
    target: str,
    source: tuple[str, World, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = KeyRing.generate_entry("rk9")
    settings = _settings_for(source[0], secret_keys=SecretStr(other))
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    result = _restore_cli(CliRunner(), made)
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    failed = [line for line in lines if line.startswith("FAILED")]
    assert len(failed) == 1 and failed[0].startswith("FAILED secrets: ok 0, failed 2")
    assert all(i in failed[0] for k, i in source[2].items() if k.startswith("secret:"))
    assert "RESTORE VERIFIED" not in result.output
    for secret in (*PLAIN.values(), other.split(":", 1)[1], RING.split(":", 1)[1]):
        assert secret not in result.output


def test_the_password_reaches_pg_restore_only_through_pgpassword(
    cli: CliRunner, made: Path, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = str(make_url(OWNER_URL).password)
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def recording(argv: list[str], **kwargs: Any) -> Any:  # Any: subprocess.run's signature
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, **kwargs)

    monkeypatch.setattr(backup.subprocess, "run", recording)
    result = _restore_cli(cli, made)
    assert result.exit_code == 0, result.output
    restores = [(argv, env) for argv, env in calls if "--single-transaction" in argv]
    assert len(restores) == 1
    argv, env = restores[0]
    assert {"--exit-on-error", "--no-owner"} <= set(argv)
    assert all(password not in arg for arg in argv)
    assert env["PGPASSWORD"] == password
    assert not [name for name in env if name.upper().startswith("SHIFDE_")]
