"""Logical backup of the SHIFDE database with PostgreSQL's own tools (go-live design 6.1, SP4-4).

`create_backup` runs as the schema owner. It opens a REPEATABLE READ transaction, exports its
snapshot and, while that transaction stays open, runs `pg_dump -Fc --no-owner --snapshot=<id>`
and reads the per-table row counts, the stamped revisions and a catalog fingerprint in the same
snapshot. It writes `manifest.json` (sorted keys) beside the dump. `verify_backup` needs no
database: it checks the manifest, both digests, that the revisions are known to this code and
not newer than its heads, and that `pg_restore` exists.
`restore_backup` (SP4-5) verifies first, refuses a target that is not an empty database owned
by the schema owner on a cluster with both roles, runs `pg_restore --single-transaction`, and
`restore_checks` then compares the target with the manifest (seven checks, design 6.1).
The `catalog rules` check never reads the manifest: it applies the code's own copy of the schema
tests' isolation rules (`catalog_rules`) to the restored catalog -- the row-level security rules,
the app role's privileges, the SECURITY DEFINER rules and the full context-write scan -- so a
forged manifest cannot hide a catalog that breaks them. The schema tests' tenant key, admin-flag
and principal-table checks are not ported (see `catalog_rules`).
It also pins the context helper definitions (`current_tenant_id()` and the other two) to the
texts reviewed in `catalog_rules`.

The digests detect corruption and edits, not a deliberate forger: the manifest is unsigned, so
anyone who can edit it can recompute them. The KEK ring (`SHIFDE_SECRET_KEYS`, only its key ids
are recorded) and the API-key pepper are not in the backup; they stay in separate custody.
Passwords reach the tools only through `PGPASSWORD` in the child's environment, never on a
command line, and are scrubbed from any tool output in an error. The child gets only PATH (and
SYSTEMROOT, TEMP, TMP on Windows) and the PG* variables set here, never a `SHIFDE_*` variable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

# the PostgreSQL tools run with fixed argv lists and no shell (_run_tool)
import subprocess  # nosec B404
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # Any: the manifest is a JSON document

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, table, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.audit import service as audit
from shifde.cli import catalog_rules
from shifde.core.clock import utcnow
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, read_db_revisions
from shifde.core.dbrole import UnsafeDatabaseRole
from shifde.core.health import migration_state
from shifde.core.migrations import code_heads, migrations_dir
from shifde.secrets import service as secrets_service

MANIFEST = "manifest.json"
DUMP_FILE = "shifde.dump"
MANIFEST_FORMAT = 1
SCHEMA = "shifde"
BIN_DIR_VAR = "SHIFDE_PG_BIN_DIR"
# change with any sign-in or worker start: counted, but not compared on restore (design call 16)
VOLATILE_TABLES = ("login_lockouts", "oidc_auth_requests", "sessions", "workers")
REQUIRED_FIELDS = (
    "format",
    "app_version",
    "alembic_revisions",
    "pg_dump_version",
    "server_version",
    "created_at",
    "dump_file",
    "dump_sha256",
    "dump_size_bytes",
    "database",
    "secret_key_ids",
    "row_counts",
    "volatile_tables",
    "catalog_fingerprint",
    "manifest_sha256",
)
_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")
# URL query parameters handed to the tools as libpq variables; any other parameter is refused
_SSL_VARS = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
}
_RELKINDS = {"r": "table", "p": "table", "f": "table", "v": "view", "m": "view", "S": "sequence"}


class BackupError(Exception):
    """A backup step failed (the message is safe to print: no password, no key material)."""


@dataclass(frozen=True)
class BackupResult:
    directory: Path
    manifest: dict[str, Any]


def find_pg_tool(name: str) -> str:
    """Path of a PostgreSQL client binary: in `SHIFDE_PG_BIN_DIR` when set, otherwise on PATH."""
    directory = os.environ.get(BIN_DIR_VAR)
    if directory:
        candidate = Path(directory) / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    else:
        found = shutil.which(name)
        if found:
            return found
    raise BackupError(f"{name} not found: put it on PATH or set {BIN_DIR_VAR} to its directory")


def _parse_url(url: str, what: str = "the migration database URL") -> URL:
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise BackupError(f"{what} is not valid") from None
    if not parsed.database:
        raise BackupError(f"{what} must name a database")
    unsupported = sorted(
        name
        for name, value in parsed.query.items()
        if name not in _SSL_VARS or not isinstance(value, str)
    )
    if unsupported:
        raise BackupError(
            f"{what} has query parameters backup does not pass to the PostgreSQL tools: "
            f"{', '.join(unsupported)} (supported: {', '.join(sorted(_SSL_VARS))})"
        )
    return parsed


def _base_env() -> dict[str, str]:
    """The only inherited variables a tool gets: PATH, and on Windows what sockets and temp
    files need."""
    names = ("PATH", "SYSTEMROOT", "TEMP", "TMP") if os.name == "nt" else ("PATH",)
    return {name: os.environ[name] for name in names if name in os.environ}


def _pg_env(parsed: URL) -> dict[str, str]:
    env = _base_env()
    if parsed.password:
        env["PGPASSWORD"] = str(parsed.password)
    for name, var in _SSL_VARS.items():
        value = parsed.query.get(name)
        if isinstance(value, str):
            env[var] = value
    return env


def _conn_args(parsed: URL) -> list[str]:
    args: list[str] = []
    if parsed.host:
        args += ["-h", parsed.host]
    if parsed.port:
        args += ["-p", str(parsed.port)]
    if parsed.username:
        args += ["-U", parsed.username]
    return [*args, "-d", parsed.database or ""]


def _run_tool(argv: list[str], env: Mapping[str, str], password: str | None, what: str) -> str:
    """Run a PostgreSQL tool without a shell; its stdout, or a BackupError with the password
    scrubbed from the tail of its stderr."""
    try:
        # argv starts with a binary from find_pg_tool; no shell, the password only in env
        proc = subprocess.run(  # nosec B603
            argv, env=dict(env), capture_output=True, text=True, errors="replace", check=False
        )
    except OSError as exc:
        raise BackupError(f"{what}: cannot run the tool ({exc.__class__.__name__})") from None
    if proc.returncode != 0:
        message = " | ".join((proc.stderr or "").strip().splitlines()[-5:])
        if password:
            message = message.replace(password, "***")
        raise BackupError(f"{what} failed (exit {proc.returncode}): {message}")
    return proc.stdout


def _pg_dump_version(pg_dump: str) -> str:
    """`pg_dump --version` output, e.g. `pg_dump (PostgreSQL) 17.2`."""
    return _run_tool([pg_dump, "--version"], _base_env(), None, "pg_dump --version")


def _tool_version(output: str) -> tuple[int, str]:
    match = _VERSION.search(output)
    if match is None:
        raise BackupError("cannot read the pg_dump version")
    number = match.group(1) + (f".{match.group(2)}" if match.group(2) else "")
    return int(match.group(1)), number


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """sha256 over every manifest field except `manifest_sha256` itself (canonical JSON)."""
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _private(path: Path, mode: int) -> None:
    """Owner-only permissions on POSIX (a backup holds every client's data); on Windows the files
    inherit the parent directory's ACL (DECISIONS B-2791)."""
    if os.name == "posix":
        path.chmod(mode)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sorted(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))


def _table_names(session: Session) -> list[str]:
    rows = session.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')"
        ),
        {"schema": SCHEMA},
    ).scalars()
    return sorted(str(r) for r in rows)


def row_counts(session: Session) -> dict[str, int]:
    """`count(*)` of every ordinary or partitioned table in schema `shifde`."""
    return {
        name: int(
            session.execute(
                select(func.count()).select_from(table(name, schema=SCHEMA))
            ).scalar_one()
        )
        for name in _table_names(session)
    }


def catalog_fingerprint(session: Session, app_role: str) -> dict[str, Any]:
    """Row-level security flags, policies, the grants to `app_role` and the SECURITY DEFINER
    functions of schema `shifde`: sorted, with no oid or other per-database value. Expressions
    and signatures are rendered under `search_path = pg_catalog`, so every name is qualified."""
    session.execute(text("SET LOCAL search_path = pg_catalog"))
    params = {"schema": SCHEMA, "role": app_role}
    tables = {
        str(r.relname): {
            "relrowsecurity": bool(r.relrowsecurity),
            "relforcerowsecurity": bool(r.relforcerowsecurity),
        }
        for r in session.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')"
            ),
            params,
        )
    }
    policies = _sorted(
        {
            "table": str(r.tablename),
            "name": str(r.policyname),
            "command": str(r.cmd),
            "permissive": str(r.permissive),
            "roles": sorted(str(role) for role in r.roles),
            "using": r.qual,
            "with_check": r.with_check,
        }
        for r in session.execute(
            text(
                "SELECT tablename, policyname, cmd, permissive, roles::text[] AS roles, qual, "
                "with_check FROM pg_policies WHERE schemaname = :schema"
            ),
            params,
        )
    )
    grant_rows = session.execute(
        text(
            "SELECT 'schema' AS kind, n.nspname::text AS object, a.privilege_type, a.is_grantable "
            "FROM pg_namespace n, aclexplode(n.nspacl) a "
            "WHERE n.nspname = :schema AND a.grantee = (SELECT oid FROM pg_roles "
            "WHERE rolname = :role) "
            "UNION ALL "
            "SELECT c.relkind::text, c.relname::text, a.privilege_type, a.is_grantable "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
            "WHERE n.nspname = :schema AND a.grantee = (SELECT oid FROM pg_roles "
            "WHERE rolname = :role) "
            "UNION ALL "
            "SELECT 'column', c.relname::text || '.' || t.attname::text, a.privilege_type, "
            "a.is_grantable FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(t.attacl) a "
            "WHERE n.nspname = :schema AND t.attnum > 0 AND NOT t.attisdropped "
            "AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :role) "
            "UNION ALL "
            "SELECT 'function', p.oid::regprocedure::text, a.privilege_type, a.is_grantable "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a "
            "WHERE n.nspname = :schema AND a.grantee = (SELECT oid FROM pg_roles "
            "WHERE rolname = :role)"
        ),
        params,
    )
    grants = _sorted(
        {
            "kind": _RELKINDS.get(str(r.kind), str(r.kind)),
            "object": str(r.object),
            "privilege": str(r.privilege_type),
            "grantable": bool(r.is_grantable),
        }
        for r in grant_rows
    )
    definers = _sorted(
        {"signature": str(r.signature), "search_path": _search_path(r.proconfig)}
        for r in session.execute(
            text(
                "SELECT p.oid::regprocedure::text AS signature, p.proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema AND p.prosecdef"
            ),
            params,
        )
    )
    return {
        "tables": tables,
        "policies": policies,
        "app_role": app_role,
        "app_role_grants": grants,
        "security_definer_functions": definers,
    }


def _search_path(proconfig: list[str] | None) -> str | None:
    for entry in proconfig or ():
        name, _, value = entry.partition("=")
        if name == "search_path":
            return value
    return None


def create_backup(owner_url: str, app_role: str, keyring: KeyRing, out_dir: Path) -> BackupResult:
    """Dump the database at `owner_url` (the schema owner) into `out_dir`, which must not exist
    or must be an empty directory."""
    parsed = _parse_url(owner_url)
    pg_dump = find_pg_tool("pg_dump")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise BackupError("the output directory must not exist or must be empty")
    dump_major, dump_version = _tool_version(_pg_dump_version(pg_dump))
    password = str(parsed.password) if parsed.password else None
    database = Database(owner_url, pool_size=1, max_overflow=0, verify_role=False)
    try:
        with database.session() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            snapshot = str(session.execute(text("SELECT pg_export_snapshot()")).scalar_one())
            server = session.execute(
                text(
                    "SELECT current_setting('server_version_num')::int AS num, "
                    "current_setting('server_version') AS version"
                )
            ).one()
            if dump_major < int(server.num) // 10000:
                raise BackupError(
                    f"pg_dump {dump_version} is older than the server ({server.version}); "
                    f"use pg_dump {int(server.num) // 10000} or newer ({BIN_DIR_VAR})"
                )
            revisions = sorted(read_db_revisions(session))
            if not revisions:
                raise BackupError("the database has no migration revision; nothing to back up")
            counts = row_counts(session)
            fingerprint = catalog_fingerprint(session, app_role)
            out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _private(out_dir, 0o700)
            dump = out_dir / DUMP_FILE
            try:
                _run_tool(
                    [
                        pg_dump,
                        "-Fc",
                        "--no-owner",
                        f"--snapshot={snapshot}",
                        "-f",
                        str(dump),
                        *_conn_args(parsed),
                    ],
                    _pg_env(parsed),
                    password,
                    "pg_dump",
                )
            except BaseException:
                dump.unlink(missing_ok=True)  # never leave a partial dump behind
                raise
            session.rollback()  # read only; ends the snapshot's transaction
    except SQLAlchemyError as exc:
        raise BackupError(f"database error: {exc.__class__.__name__}") from None
    finally:
        database.dispose()
    _private(dump, 0o600)
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "app_version": __version__,
        "alembic_revisions": revisions,
        "pg_dump_version": dump_version,
        "server_version": str(server.version),
        "created_at": utcnow().isoformat(timespec="seconds"),
        "dump_file": DUMP_FILE,
        "dump_sha256": _sha256(dump),
        "dump_size_bytes": dump.stat().st_size,
        "database": parsed.database,
        "secret_key_ids": list(keyring.key_ids),
        "row_counts": counts,
        "volatile_tables": sorted(VOLATILE_TABLES),
        "catalog_fingerprint": fingerprint,
    }
    manifest["manifest_sha256"] = manifest_digest(manifest)
    path = out_dir / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _private(path, 0o600)
    return BackupResult(out_dir, manifest)


def read_manifest(directory: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError(
            f"{MANIFEST} is missing or not valid JSON in the backup directory"
        ) from None
    if not isinstance(loaded, dict) or any(k not in loaded for k in REQUIRED_FIELDS):
        raise BackupError(f"{MANIFEST} is incomplete")
    if loaded["format"] != MANIFEST_FORMAT:
        raise BackupError(f"unsupported backup format {loaded['format']!r}")
    if manifest_digest(loaded) != loaded["manifest_sha256"]:
        raise BackupError(f"{MANIFEST} does not match its own digest (edited or corrupt manifest)")
    dump_name = loaded["dump_file"]
    if (
        not isinstance(dump_name, str)
        or not dump_name
        or dump_name in (".", "..")
        or "/" in dump_name
        or "\\" in dump_name
        or dump_name != Path(dump_name).name
    ):
        raise BackupError(f"{MANIFEST} names a dump file outside the backup directory")
    revisions = loaded["alembic_revisions"]
    if (
        not isinstance(revisions, list)
        or not revisions
        or not all(isinstance(r, str) for r in revisions)
        or not isinstance(loaded["dump_sha256"], str)
    ):
        raise BackupError(f"{MANIFEST} is incomplete")
    manifest: dict[str, Any] = loaded
    return manifest


def known_revisions() -> frozenset[str]:
    """Every revision that is one of `code_heads()` or an ancestor of one (down revisions and
    dependencies): a revision outside this set is unknown to, or newer than, this code."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    script = ScriptDirectory.from_config(cfg)
    known: set[str] = set()
    pending = list(code_heads())
    while pending:
        revision = script.get_revision(pending.pop())
        if revision is None or revision.revision in known:
            continue
        known.add(revision.revision)
        for name in (*_names(revision.down_revision), *_names(revision.dependencies)):
            pending.append(name)
    return frozenset(known)


def _names(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def verify_backup(from_dir: Path) -> tuple[dict[str, Any], Path, str]:
    """Read-only check that `from_dir` can be restored by this code; no database connection.

    Raises BackupError (one-line reason) unless the manifest parses and its digest matches, the
    dump is a plain file inside `from_dir` whose sha256 matches, every stamped revision is known
    to this code and not newer than its heads, and `pg_restore` is available. Returns the
    manifest, the dump path and the `pg_restore` path."""
    manifest = read_manifest(from_dir)
    dump = from_dir / str(manifest["dump_file"])
    if not dump.is_file():
        raise BackupError("the dump file named by the manifest is missing")
    if _sha256(dump) != manifest["dump_sha256"]:
        raise BackupError("dump digest mismatch: the dump does not match its manifest")
    unknown = sorted(set(manifest["alembic_revisions"]) - known_revisions())
    if unknown:
        raise BackupError(
            f"the backup's revision {', '.join(unknown)} is unknown to or newer than this code; "
            "install the matching or a newer release first"
        )
    return manifest, dump, find_pg_tool("pg_restore")


# --------------------------------------------------------------------------- restore (SP4-5)
RESTORE_CHECKS = (
    "fingerprint",
    "catalog rules",
    "row counts",
    "migration state",
    "app role",
    "audit chains",
    "secrets",
)
_TARGET = "the target database URL"
_CLIENTS = text("SELECT id, slug FROM shifde.tenants ORDER BY id")
_NOT_EMPTY = text(
    "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' ORDER BY 1, 2 LIMIT 5"
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def target_url_from_env(name: str) -> str:
    """The URL in environment variable `name`, or in the file named by `name_FILE` when `name`
    is unset. Errors never carry the URL or the file's contents."""
    value = os.environ.get(name)
    if value is None:
        path = os.environ.get(f"{name}_FILE")
        if path is None:
            raise BackupError(f"neither {name} nor {name}_FILE is set")
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise BackupError(f"cannot read the file named by {name}_FILE") from None
    if not value:
        raise BackupError(f"{name} is empty")
    return value


def _check_target(session: Session, owner_role: str, app_role: str) -> None:
    """Refuse a target not owned by `owner_role`, not empty, or on a cluster lacking a role."""
    datdba = session.execute(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
    ).scalar_one()
    if datdba != owner_role:
        raise BackupError(f"the target database is owned by {datdba}, not by {owner_role}")
    if session.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :s)"), {"s": SCHEMA}
    ).scalar_one():
        raise BackupError(f"the target database is not empty: schema {SCHEMA} exists")
    found = [f"{r[0]}.{r[1]}" for r in session.execute(_NOT_EMPTY)]
    if found:
        raise BackupError(f"the target database is not empty: {', '.join(found)}")
    present = set(
        session.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
            {"names": [owner_role, app_role]},
        ).scalars()
    )
    missing = [role for role in (owner_role, app_role) if role not in present]
    if missing:
        raise BackupError(f"role(s) missing on the target cluster: {', '.join(missing)}")


def restore_backup(
    from_dir: Path, target_url: str, owner_role: str, app_role: str
) -> dict[str, Any]:
    """Verify `from_dir`, check the target's preconditions as its URL's user, then restore the
    dump with `pg_restore --single-transaction --exit-on-error --no-owner`; returns the manifest.
    Creates no role and no extension, and never cleans a target."""
    manifest, dump, pg_restore = verify_backup(from_dir)
    parsed = _parse_url(target_url, _TARGET)
    if parsed.username != owner_role:
        raise BackupError(f"the target URL's user is not the schema owner role {owner_role}")
    database = Database(target_url, pool_size=1, max_overflow=0, verify_role=False)
    try:
        with database.session() as session:
            _check_target(session, owner_role, app_role)
    except SQLAlchemyError as exc:
        raise BackupError(f"target database error: {exc.__class__.__name__}") from None
    finally:
        database.dispose()
    _run_tool(
        [
            pg_restore,
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            *_conn_args(parsed),
            str(dump),
        ],
        _pg_env(parsed),
        str(parsed.password) if parsed.password else None,
        "pg_restore",
    )
    return manifest


def _differences(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """What differs between two catalog fingerprints, by table, policy, grant and function."""
    e_tables, a_tables = expected.get("tables", {}), actual.get("tables", {})
    found = [
        f"table {name}"
        for name in sorted(set(e_tables) | set(a_tables))
        if e_tables.get(name) != a_tables.get(name)
    ]
    labels: tuple[tuple[str, str, Callable[[Mapping[str, Any]], str]], ...] = (
        ("policies", "policy", lambda p: f"{p['table']}.{p['name']}"),
        ("app_role_grants", "grant", lambda g: f"{g['privilege']} on {g['kind']} {g['object']}"),
        ("security_definer_functions", "function", lambda f: str(f["signature"])),
    )
    for key, label, name_of in labels:
        e_items = {json.dumps(i, sort_keys=True): i for i in expected.get(key, [])}
        a_items = {json.dumps(i, sort_keys=True): i for i in actual.get(key, [])}
        names = {name_of(i) for k, i in e_items.items() if k not in a_items}
        names |= {name_of(i) for k, i in a_items.items() if k not in e_items}
        found += [f"{label} {name}" for name in sorted(names)]
    if expected.get("app_role") != actual.get("app_role"):
        found.append("app role")
    return found


def _check_fingerprint(session: Session, manifest: Mapping[str, Any], app_role: str) -> str:
    # through JSON, as the manifest went: tuples become lists
    actual = json.loads(json.dumps(catalog_fingerprint(session, app_role)))
    expected = manifest["catalog_fingerprint"]
    if actual == expected:
        return ""
    return "differs: " + (", ".join(_differences(expected, actual)) or "other fields")


def _check_catalog_rules(session: Session, owner_role: str, app_role: str) -> str:
    conn = session.connection()
    problems = [
        *catalog_rules.rls_violations(conn),
        *catalog_rules.privilege_problems(conn, app_role),
        *catalog_rules.definer_violations(conn, owner_role, app_role),
        *catalog_rules.helper_violations(conn),
        # policies are scanned by rls_violations
        *catalog_rules.context_write_violations(conn, kinds=catalog_rules.ALL_KINDS - {"policy"}),
    ]
    return "violates: " + "; ".join(problems) if problems else ""


def _check_row_counts(session: Session, manifest: Mapping[str, Any]) -> str:
    volatile = set(manifest["volatile_tables"])
    expected = {k: v for k, v in manifest["row_counts"].items() if k not in volatile}
    actual = {k: v for k, v in row_counts(session).items() if k not in volatile}
    differ = sorted(n for n in set(expected) | set(actual) if expected.get(n) != actual.get(n))
    return f"differ: {', '.join(differ)}" if differ else ""


def _check_migrations(session: Session) -> str:
    state = migration_state(session).status
    return "" if state == "ok" else f"migration state is {state}"


def _check_app_role(app_url: str) -> str:
    try:
        database = Database(app_url, pool_size=1, max_overflow=0)  # runs check_app_role
    except UnsafeDatabaseRole as exc:
        return str(exc)
    database.dispose()
    return ""


def _check_audit(session: Session) -> str:
    broken = []
    for tenant_id, slug in session.execute(_CLIENTS).all():
        result = audit.verify_chain(session, tenant_id)
        if not result.ok:
            broken.append(f"{slug} at event {result.first_broken_id}")
    return f"broken: {', '.join(broken)}" if broken else ""


def _check_secrets(session: Session, keyring: KeyRing) -> str:
    try:
        ok, failed = secrets_service.check_all(session, keyring)
    except secrets_service.RowSecurityHidesSecrets:
        return secrets_service.ROW_SECURITY_HIDES_SECRETS
    if not failed:
        return ""
    return f"ok {ok}, failed {len(failed)}: {', '.join(str(i) for i in failed)}"


def restore_checks(
    manifest: Mapping[str, Any], target_url: str, app_url: str, app_role: str, keyring: KeyRing
) -> list[CheckResult]:
    """The seven post-restore checks of design 6.1, in `RESTORE_CHECKS` order; each runs even when
    an earlier one failed. `app_url` gives the app role's credentials; its host, port and
    database become the target's. A detail never carries connection details or key material."""
    target = make_url(target_url)
    app_target = (
        make_url(app_url)
        .set(host=target.host, port=target.port, database=target.database)
        .render_as_string(hide_password=False)
    )
    steps: dict[str, Callable[[Session], str]] = {
        "fingerprint": lambda s: _check_fingerprint(s, manifest, app_role),
        "catalog rules": lambda s: _check_catalog_rules(s, target.username or "", app_role),
        "row counts": lambda s: _check_row_counts(s, manifest),
        "migration state": _check_migrations,
        "audit chains": _check_audit,
        "secrets": lambda s: _check_secrets(s, keyring),
    }
    database = Database(target_url, pool_size=1, max_overflow=0, verify_role=False)
    results: list[CheckResult] = []
    try:
        for name in RESTORE_CHECKS:
            try:
                if name == "app role":
                    detail = _check_app_role(app_target)
                else:
                    with database.session() as session:
                        detail = steps[name](session)
                        session.rollback()  # read only
            except SQLAlchemyError as exc:
                detail = f"database error: {exc.__class__.__name__}"
            results.append(CheckResult(name, not detail, detail))
    finally:
        database.dispose()
    return results
