"""Logical backup of the SHIFDE database with PostgreSQL's own tools (go-live design 6.1, SP4-4).

`create_backup` runs as the schema owner. It opens a REPEATABLE READ transaction, exports its
snapshot and, while that transaction stays open, runs `pg_dump -Fc --no-owner --snapshot=<id>`
and reads the per-table row counts, the stamped revisions and a catalog fingerprint in the same
snapshot. It writes `manifest.json` (sorted keys) beside the dump. `verify_backup` needs no
database: it checks the manifest, both digests, that the revisions are known to this code and
not newer than its heads, and that `pg_restore` exists.

The digests detect corruption and edits, not a deliberate forger: the manifest is unsigned, so
anyone who can edit it can recompute them. The KEK ring (`SHIFDE_SECRET_KEYS`, only its key ids
are recorded) and the API-key pepper are not in the backup; they stay in separate custody.
Passwords reach the tools only through `PGPASSWORD` in the child's environment, never on a
command line, and are scrubbed from any tool output in an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

# the PostgreSQL tools run with fixed argv lists and no shell (_run_tool)
import subprocess  # nosec B404
from collections.abc import Iterable, Mapping
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
from shifde.core.clock import utcnow
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, read_db_revisions
from shifde.core.migrations import code_heads, migrations_dir

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


def _parse_url(url: str) -> URL:
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise BackupError("the migration database URL is not valid") from None
    if not parsed.database:
        raise BackupError("the migration database URL must name a database")
    return parsed


def _pg_env(parsed: URL) -> dict[str, str]:
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = str(parsed.password)
    sslmode = parsed.query.get("sslmode")
    if isinstance(sslmode, str):
        env["PGSSLMODE"] = sslmode
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
            argv, env=dict(env), capture_output=True, text=True, check=False
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
    return _run_tool([pg_dump, "--version"], os.environ, None, "pg_dump --version")


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
            except BackupError:
                dump.unlink(missing_ok=True)
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
