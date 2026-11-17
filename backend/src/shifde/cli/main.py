"""`shifde-admin` command line (Typer). Runs with owner (migration) credentials.

Not ported (DECISIONS B-1101, B-2221): `profile`, `backup`, `demo`, `dev-sample-pipeline`,
`dev-sample-ontology`, `dev-rest-sink` (carry_forward, app part 1). `dev-sample-data` is ported
(app-2 B-2420)."""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from alembic import command
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.adapters.conformance import SuiteSkipped, run_suite
from shifde.adapters.evidence import record_run
from shifde.adapters.registry import AdapterRegistry
from shifde.audit import service as audit
from shifde.cli.sampledata import build_sample_database
from shifde.cli.seeding import STAFF_USERS, seed_dev_data
from shifde.cli.seeding import USERS as SEED_USERS
from shifde.core import configtools
from shifde.core.crypto import CryptoError
from shifde.core.db import Database
from shifde.core.devenv import append_dev_env, read_dev_env
from shifde.core.errors import AppError
from shifde.core.health import database_reachable, migration_state
from shifde.core.migrations import alembic_config, code_head
from shifde.core.settings import (
    DEV_USERS_FILE,
    Settings,
    format_validation_error,
    get_settings,
)
from shifde.identity import service as identity
from shifde.identity.passwords import PasswordService
from shifde.secrets import service as secrets_service
from shifde.tenancy import service as tenancy

app = typer.Typer(help="SHIFDE administration CLI.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")
secrets_app = typer.Typer(help="Secret store maintenance.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
audit_app = typer.Typer(help="Audit log maintenance.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
adapters_app = typer.Typer(help="Integration adapter registry and evidence.", no_args_is_help=True)
app.add_typer(adapters_app, name="adapters")
usage_app = typer.Typer(help="Usage ledger maintenance.", no_args_is_help=True)
app.add_typer(usage_app, name="usage")
config_app = typer.Typer(help="Configuration schema and validation.", no_args_is_help=True)
app.add_typer(config_app, name="config")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _settings() -> Settings:
    try:
        return get_settings()
    except ValueError as exc:  # pydantic ValidationError / SecretFileError subclass ValueError
        # redacted: pydantic's own rendering echoes input values, secrets included
        raise _fail(f"invalid configuration: {format_validation_error(exc)}") from exc


@contextmanager
def _owner_session(settings: Settings) -> Iterator[Session]:
    # the schema owner's own tooling (`Database` docstring): the owner role is privileged by
    # design, so the startup role check that guards the app's URL does not apply to this one
    database = Database(
        settings.effective_migration_url, pool_size=1, max_overflow=0, verify_role=False
    )
    try:
        with database.session() as session:
            yield session
    finally:
        database.dispose()


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Upgrade the database to REVISION (default: head)."""
    settings = _settings()
    try:
        command.upgrade(alembic_config(settings.effective_migration_url), revision)
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}: {exc}") from exc
    typer.echo(f"database upgraded to {revision}")


@db_app.command("downgrade")
def db_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision, e.g. base")],
) -> None:
    """Downgrade the database to REVISION (data-destructive; back up first)."""
    settings = _settings()
    try:
        command.downgrade(alembic_config(settings.effective_migration_url), revision)
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}: {exc}") from exc
    typer.echo(f"database downgraded to {revision}")


@db_app.command("current")
def db_current() -> None:
    """Show the database's current revision."""
    settings = _settings()
    try:
        command.current(alembic_config(settings.effective_migration_url), verbose=False)
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}: {exc}") from exc


@db_app.command("check")
def db_check() -> None:
    """Exit 0 iff the database revision equals the code's expected head (prints both)."""
    settings = _settings()
    try:
        with _owner_session(settings) as session:
            if not database_reachable(session):
                raise _fail("database is not reachable")
            state = migration_state(session)
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    typer.echo(f"current:       {state.revision or '(none)'}")
    typer.echo(f"expected head: {state.head}")
    if state.status != "ok":
        typer.echo(f"status: {state.status} (run `shifde-admin db upgrade`)", err=True)
        raise typer.Exit(code=1)
    typer.echo("status: ok")


@app.command("version")
def version(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Print the application version and the expected migration head."""
    if as_json:
        typer.echo(json.dumps({"version": __version__, "migration_head": code_head()}))
    else:
        typer.echo(f"shifde {__version__} (migration head {code_head()})")


@config_app.command("schema")
def config_schema(
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of stdout")] = None,
) -> None:
    """Emit the JSON Schema of the SHIFDE_* environment (generated from the settings)."""
    document = json.dumps(configtools.env_schema(), indent=2) + "\n"
    if out is None:
        typer.echo(document, nl=False)
        return
    try:
        out.write_text(document, encoding="utf-8")
    except OSError as exc:
        raise _fail(f"cannot write {out}: {exc.__class__.__name__}") from exc
    typer.echo(f"schema written to {out}")


@config_app.command("validate")
def config_validate(
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file", help="Validate this env file (default: the process environment)"
        ),
    ] = None,
) -> None:
    """Validate the configuration as the services would load it; exit 0/1, redacted report."""
    try:
        environ = (
            configtools.read_env_file(env_file)
            if env_file is not None
            else {k: v for k, v in os.environ.items() if k.startswith(configtools.PREFIX)}
        )
    except OSError as exc:
        raise _fail(f"cannot read env file: {exc.__class__.__name__}") from exc
    report = configtools.validate_environment(environ)
    typer.echo(report.render(), err=not report.ok)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("bootstrap")
def bootstrap(
    email: Annotated[str, typer.Option(help="Platform admin email")],
    display_name: Annotated[str | None, typer.Option()] = None,
    password_stdin: Annotated[
        bool, typer.Option(help="Read the password from stdin (needs local auth enabled)")
    ] = False,
    force_additional: Annotated[
        bool, typer.Option(help="Allow creating another platform admin")
    ] = False,
) -> None:
    """Create the first platform admin (no default credentials exist anywhere)."""
    settings = _settings()
    password: str | None = None
    if password_stdin:
        if not settings.auth_local_enabled:
            raise _fail("--password-stdin requires SHIFDE_AUTH_LOCAL_ENABLED=true")
        # Bytes, not text: a BOM (piped from PowerShell) must not silently join the password.
        try:
            password = sys.stdin.buffer.readline().decode("utf-8-sig").rstrip("\r\n")
        except UnicodeDecodeError:
            raise _fail("--password-stdin expects UTF-8") from None
    try:
        with _owner_session(settings) as session:
            if identity.platform_admin_exists(session) and not force_additional:
                raise _fail("a platform admin already exists (use --force-additional)")
            if identity.find_user_id_by_email(session, email) is not None:
                raise _fail("a user with that email already exists")
            user_id = identity.create_user(
                session,
                email=email,
                display_name=display_name or email,
                is_platform_admin=True,
                password=password,
                passwords=PasswordService() if password is not None else None,
            )
            token_line = ""
            if password is None:
                token, expires = identity.create_setup_token(session, user_id)
                token_line = (
                    f"one-time setup token (valid until {expires.isoformat()}): {token}\n"
                    "store it now; it is not shown again"
                )
            session.commit()
    except AppError as exc:
        raise _fail(exc.detail) from exc
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    typer.echo(f"platform admin created: {identity.normalize_email(email)}")
    if token_line:
        typer.echo(token_line)


@secrets_app.command("rewrap")
def secrets_rewrap() -> None:
    """Re-wrap every secret DEK under the active KEK (KEK rotation; plaintext is never exposed)."""
    settings = _settings()
    try:
        with _owner_session(settings) as session:
            changed, total = secrets_service.rewrap_all(session, settings.keyring)
            session.commit()
    except CryptoError as exc:
        raise _fail(
            f"a secret could not be re-wrapped ({exc}); is the old KEK still in the ring?"
        ) from exc
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    typer.echo(f"re-wrapped {changed} of {total} secret(s) under key {settings.keyring.active_id}")


@audit_app.command("verify")
def audit_verify(org: Annotated[str, typer.Option(help="Organization slug")]) -> None:
    """Verify the tenant audit hash chain; exits non-zero if it is broken."""
    settings = _settings()
    try:
        with _owner_session(settings) as session:
            tenant_id = tenancy.tenant_id_by_slug(session, org)
            if tenant_id is None:
                raise _fail(f"organization not found: {org}")
            result = audit.verify_chain(session, tenant_id)
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    if result.ok:
        typer.echo(f"audit chain OK: {result.checked} event(s) verified for {org}")
        return
    typer.echo(
        f"audit chain BROKEN at event {result.first_broken_id} "
        f"after {result.checked} verified event(s)",
        err=True,
    )
    raise typer.Exit(code=2)


def _read_dev_users() -> dict[str, str]:
    """`email<TAB>password` lines of .devdata/dev-users.txt ({} when it does not exist)."""
    if not DEV_USERS_FILE.exists():
        return {}
    users: dict[str, str] = {}
    for line in DEV_USERS_FILE.read_text(encoding="utf-8-sig").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and not line.lstrip().startswith("#"):
            users[parts[0].strip().lower()] = parts[1].strip()
    return users


@app.command("dev-seed")
def dev_seed(
    reuse_passwords: Annotated[
        bool,
        typer.Option(
            help="Give the users the passwords already in .devdata/dev-users.txt and leave that "
            "file unchanged (for a second database, such as the smoke stack's)"
        ),
    ] = False,
) -> None:
    """Seed demo orgs/users (SHIFDE_ENV=development only); writes .devdata/dev-users.txt."""
    settings = _settings()
    if not settings.is_development:
        raise _fail("dev-seed refuses to run unless SHIFDE_ENV=development")
    known: dict[str, str] | None = None
    if reuse_passwords:
        known = _read_dev_users()
        missing = [u.email for u in (*SEED_USERS, *STAFF_USERS) if u.email not in known]
        if missing:
            raise _fail(
                f"{DEV_USERS_FILE} lacks {', '.join(missing)}: run dev-seed without it once"
            )
    try:
        with _owner_session(settings) as session:
            credentials = seed_dev_data(session, PasswordService(), known, with_groups=True)
            session.commit()
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}: {exc}") from exc
    if reuse_passwords:
        typer.echo(f"seeded {len(credentials)} users with the passwords in {DEV_USERS_FILE}")
        return
    lines = ["# SHIFDE dev users (generated by `shifde-admin dev-seed`; local only, gitignored)"]
    lines += [f"{email}\t{password}" for email, password in credentials.items()]
    DEV_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEV_USERS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.echo(f"seeded {len(credentials)} users; credentials written to {DEV_USERS_FILE}")


@adapters_app.command("verify")
def adapters_verify(
    adapter: Annotated[
        str | None, typer.Option("--adapter", help="Verify one adapter key (default: all)")
    ] = None,
) -> None:
    """Run adapter conformance suites and record the evidence that derives adapter maturity.

    Suites whose target is unavailable (e.g. no sample database, no ANTHROPIC_API_KEY) are
    skipped and record nothing; planned adapters have no suite. Exit code 1 if a suite failed."""
    settings = _settings()
    registry = AdapterRegistry.load()
    keys = [adapter] if adapter else registry.keys()
    unknown = [k for k in keys if registry.descriptor(k) is None]
    if unknown:
        raise _fail(f"unknown adapter(s): {', '.join(unknown)}")
    failed = False
    try:
        with _owner_session(settings) as session:
            for key in keys:
                descriptor = registry.descriptor(key)
                assert descriptor is not None
                suite = registry.suite_for(key)
                if suite is None:
                    typer.echo(f"{key:20} no conformance suite ({descriptor.declared_maturity})")
                    continue
                try:
                    run = run_suite(suite, descriptor)
                except SuiteSkipped as exc:
                    typer.echo(f"{key:20} SKIPPED: {exc}")
                    continue
                record_run(session, run)
                session.commit()
                passed = sum(1 for c in run.checks if c["status"] == "pass")
                typer.echo(
                    f"{key:20} {run.outcome.upper():5} {passed}/{len(run.checks)} checks; "
                    f"validated: {', '.join(run.validated_capabilities) or '-'}"
                )
                for c in run.checks:
                    if c["status"] != "pass":
                        typer.echo(f"    FAIL {c['name']}: {c['detail']}", err=True)
                failed = failed or run.outcome != "pass"
    except SQLAlchemyError as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    if failed:
        raise typer.Exit(code=1)


SAMPLE_DB = "shifde_sample"
SAMPLE_ROLE = "shifde_sample_reader"


@app.command("dev-sample-data")
def dev_sample_data(
    recreate: Annotated[bool, typer.Option(help="Drop and recreate the sample database")] = False,
) -> None:
    """Create the `shifde_sample` database + read-only role (SHIFDE_ENV=development only).

    Uses superuser access (SHIFDE_SAMPLE_ADMIN_DATABASE_URL or SHIFDE_TEST_ADMIN_DATABASE_URL, from
    the environment or .devdata/dev.env). A random reader password is appended to .devdata/dev.env
    together with SHIFDE_SAMPLE_DATABASE_URL (never printed)."""
    settings = _settings()
    if not settings.is_development:
        raise _fail("dev-sample-data refuses to run unless SHIFDE_ENV=development")
    dev = read_dev_env()
    admin_url = (
        os.environ.get("SHIFDE_SAMPLE_ADMIN_DATABASE_URL")
        or os.environ.get("SHIFDE_TEST_ADMIN_DATABASE_URL")
        or dev.get("SHIFDE_SAMPLE_ADMIN_DATABASE_URL")
        or dev.get("SHIFDE_TEST_ADMIN_DATABASE_URL")
    )
    if not admin_url:
        raise _fail("no superuser URL: set SHIFDE_SAMPLE_ADMIN_DATABASE_URL (or start the dev DB)")
    password = dev.get("SHIFDE_SAMPLE_READER_PASSWORD") or secrets.token_urlsafe(24)
    try:
        build_sample_database(admin_url, SAMPLE_DB, SAMPLE_ROLE, password, recreate=recreate)
    except psycopg.Error as exc:
        raise _fail(f"database error: {exc.__class__.__name__}") from exc
    admin = make_url(admin_url)
    reader_url = make_url("postgresql://").set(
        username=SAMPLE_ROLE,
        password=password,
        host=admin.host,
        port=admin.port,
        database=SAMPLE_DB,
    )
    updates = {}
    if "SHIFDE_SAMPLE_READER_PASSWORD" not in dev:
        updates["SHIFDE_SAMPLE_READER_PASSWORD"] = password
    wanted_url = reader_url.render_as_string(hide_password=False)
    if dev.get("SHIFDE_SAMPLE_DATABASE_URL") != wanted_url:
        updates["SHIFDE_SAMPLE_DATABASE_URL"] = wanted_url
    if updates:
        append_dev_env(updates)
    typer.echo(
        f"sample database '{SAMPLE_DB}' ready on {admin.host}:{admin.port} "
        f"(read-only role '{SAMPLE_ROLE}'; connection URL in .devdata/dev.env)"
    )


@app.command("dev-sample-rates")
def dev_sample_rates(
    currency: Annotated[str, typer.Option(help="Currency of the sample rates")] = "USD",
    input_rate: Annotated[float, typer.Option(help="SAMPLE input price per 1M tokens")] = 1.0,
    output_rate: Annotated[float, typer.Option(help="SAMPLE output price per 1M tokens")] = 2.0,
    model: Annotated[
        list[str] | None,
        typer.Option(help="extra provider_key:model_key to seed (fake / openai_compatible)"),
    ] = None,
) -> None:
    """Seed GLOBAL sample rate cards for the fake / openai_compatible models (development only).

    The numbers are placeholders, labelled `SAMPLE - not vendor pricing` in the version source;
    this repository contains no vendor prices. Costs calculated from them are labelled as
    calculated from a rate card (never an invoice)."""
    from decimal import Decimal

    from shifde.cli.governance_seed import seed_sample_rates

    settings = _settings()
    if not settings.is_development:
        raise _fail("dev-sample-rates refuses to run unless SHIFDE_ENV=development")
    extra: list[tuple[str, str]] = []
    for item in model or []:
        provider_key, _, model_key = item.partition(":")
        if not provider_key or not model_key:
            raise _fail("--model must look like provider_key:model_key")
        extra.append((provider_key, model_key))
    try:
        with _owner_session(settings) as session:
            result = seed_sample_rates(
                session,
                currency=currency.upper(),
                input_rate=Decimal(str(input_rate)),
                output_rate=Decimal(str(output_rate)),
                extra_models=extra,
            )
    except (SQLAlchemyError, AppError) as exc:
        raise _fail(f"{exc.__class__.__name__}: {exc}") from exc
    typer.echo(
        f"sample rate cards: {result.cards_created} created, {result.versions_created} versions "
        f"created for {len(result.models)} model(s): SAMPLE values, not vendor pricing"
    )


@usage_app.command("record-build")
def usage_record_build(
    org: Annotated[str, typer.Option(help="Organization slug")],
    build_ref: Annotated[str, typer.Option(help="Build / CI reference (e.g. a pipeline run id)")],
    duration_ms: Annotated[int, typer.Option(help="Measured duration in milliseconds")] = 0,
    workspace: Annotated[str | None, typer.Option(help="Workspace slug")] = None,
    project: Annotated[str | None, typer.Option(help="Project slug (needs --workspace)")] = None,
    cost: Annotated[float | None, typer.Option(help="ESTIMATED infrastructure cost")] = None,
    currency: Annotated[str | None, typer.Option(help="Currency of --cost")] = None,
    key: Annotated[
        str | None, typer.Option(help="Idempotency key (default: the build ref)")
    ] = None,
) -> None:
    """Record one `build_op` usage event for a build / test operation (CI hook).

    Idempotent per key. With --cost the event is an `estimated_infra` amount (never presented as
    an invoice); without it the cost is `unmeasured`."""
    from decimal import Decimal

    from shifde.cli.governance_seed import record_build_op

    settings = _settings()
    try:
        with _owner_session(settings) as session:
            created = record_build_op(
                session,
                org=org,
                workspace=workspace,
                project=project,
                build_ref=build_ref,
                duration_ms=duration_ms,
                key=key,
                cost=None if cost is None else Decimal(str(cost)),
                currency=currency.upper() if currency else None,
            )
    except (SQLAlchemyError, AppError) as exc:
        raise _fail(f"{exc.__class__.__name__}: {exc}") from exc
    typer.echo("recorded" if created else "already recorded (idempotent)")
