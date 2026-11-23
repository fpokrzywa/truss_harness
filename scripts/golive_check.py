"""Go-live check of sub-project 4 (design SP4 sections 1 and 10.9), local cluster and loopback.

    python scripts/golive_check.py --local
    python scripts/golive_check.py --final

Needs the local cluster running (`python scripts/dev_db.py start`), this checkout's
`.devdata/dev.env` (`python scripts/dev_db.py env`), the backend environment (`uv sync` in
`backend`), `pg_dump`/`pg_restore` on `PATH` or in `SHIFDE_PG_BIN_DIR`, and port 8792 free.

It creates two scratch databases `shifde_golive_<hex>` on the local cluster as the cluster superuser
(credentials from `SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`): a
source, migrated with `shifde-admin db upgrade`, and an empty restore target. It never uses the
checkout's own database. It generates one API-key pepper, secret key ring and metrics token per
run and gives the same values to every child process; each child's environment is this process's
environment without any `SHIFDE_*` key plus the script's own values (only `SHIFDE_PG_BIN_DIR` is
carried over). Env files go to an owner-only temporary directory. Every socket is on 127.0.0.1.

The checks, in order: `config preflight` passes on a production env file and refuses nine
variants of it, each differing in one setting; `shifde-api` in production behind an in-script
edge proxy sends the security headers, answers 404 on `/api/docs` and `/api/openapi.json`, and
audits the edge's peer address, never a client-supplied `X-Forwarded-For`; `procctl.py up`
restarts a hard-killed worker, `down` stops everything, and a child exiting 2 is not restarted;
`dev-seed` (keeping the passwords of `.devdata/dev-users.txt` when it exists), `backup create`
and `backup restore` into the target ending `RESTORE VERIFIED`; `shifde-api` on the restored
database on port 8792 passes `scripts/two_org_proof.py`; and the go-live register
(`docs/build/golive-register.md`) has no `open` row. `--local` lists the `pending-owner` rows;
`--final` also refuses them and needs a `Hosting target` row that is `closed` or `accepted`.

Prints one `ok   <step>` or `FAIL <step>` line per check and ends with
`GO-LIVE CHECK PASSED (local)` or `GO-LIVE CHECK PASSED (final)` (exit 0),
`GO-LIVE CHECK FAILED: <step>` (exit 1) or `GO-LIVE CHECK NOT RUN: <reason>` (exit 2). Child
processes, the edge proxy and both scratch databases are removed at the end, also on failure and
Ctrl+C. It never prints a password, token, key, database URL or response body. Standard library
only at the top level: when `shifde` is not importable it re-runs itself once in the backend
environment (`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WINDOWS = sys.platform == "win32"
VENV_BIN = BACKEND / ".venv" / ("Scripts" if WINDOWS else "bin")
DEV_USERS = ROOT / ".devdata" / "dev-users.txt"
RERUN_FLAG = "GOLIVE_CHECK_RERUN"
NO_MODE = "choose --local or --final"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)
CARRIED = ("SHIFDE_PG_BIN_DIR",)  # the only SHIFDE_* setting a child inherits
RESTORED_API_PORT = 8792
TWO_ORG_ORIGIN = "http://localhost:3710"
PUBLIC_BASE_URL = "https://seth.golive.invalid"
LOOPBACK_ORIGIN = "http://127.0.0.1:3710"  # accepted in production (loopback)
SPOOFED_IP = "203.0.113.7"
READY_SECONDS = 90.0
RESTART_SECONDS = 30.0
NOT_RESTARTED_SECONDS = 5.0
CHILD_TIMEOUT = 600
REGISTER_OK = frozenset({"closed", "accepted", "pending-owner"})
HOSTING_TARGET = "Hosting target"
PRODUCTION_REQUIRED = (
    "SHIFDE_ENV",
    "SHIFDE_DATABASE_URL",
    "SHIFDE_MIGRATION_DATABASE_URL",
    "SHIFDE_API_KEY_PEPPER",
    "SHIFDE_SECRET_KEYS",
    "SHIFDE_METRICS_TOKEN",
    "SHIFDE_PUBLIC_BASE_URL",
    "SHIFDE_ALLOWED_ORIGINS",
    "SHIFDE_TRUSTED_PROXIES",
)
EDGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}
# hop-by-hop headers (RFC 9110 7.6.1) the edge does not pass on; it sets its own length
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


class NotRun(Exception):
    """The check could not run (no cluster, no dev.env, no backend environment, a busy port)."""


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


# --- pure parts (tested without a cluster) ------------------------------------------------------


def child_env(parent: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    """`parent` without its `SHIFDE_*` keys (except `CARRIED`), overlaid with `values`."""
    env = {k: v for k, v in parent.items() if not k.upper().startswith("SHIFDE_") or k in CARRIED}
    env.update(values)
    return env


def good_env(app_url: str, owner_url: str, run_secrets: Mapping[str, str]) -> dict[str, str]:
    """The production env file preflight must pass: docs, local login and loopback trust off,
    the edge at 127.0.0.1 as the only trusted proxy, the default HSTS."""
    return {
        "SHIFDE_ENV": "production",
        "SHIFDE_DATABASE_URL": app_url,
        "SHIFDE_MIGRATION_DATABASE_URL": owner_url,
        **run_secrets,
        "SHIFDE_PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "SHIFDE_ALLOWED_ORIGINS": PUBLIC_BASE_URL,
        "SHIFDE_TRUSTED_PROXIES": "127.0.0.1/32",
        "SHIFDE_TRUST_LOOPBACK_PROXY": "false",
        "SHIFDE_API_DOCS_ENABLED": "false",
        "SHIFDE_AUTH_LOCAL_ENABLED": "false",
        "SHIFDE_LOG_FORMAT": "json",
    }


def service_env(values: Mapping[str, str]) -> dict[str, str]:
    """A copy of `values` without the owner URL, which is never given to a service."""
    return {k: v for k, v in values.items() if k != "SHIFDE_MIGRATION_DATABASE_URL"}


def changed(env: Mapping[str, str], changes: Mapping[str, str | None]) -> dict[str, str]:
    """`env` with each key of `changes` set to its value, or removed when the value is None."""
    out = dict(env)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def preflight_variants(
    owner_url: str, unreachable_url: str
) -> list[tuple[str, dict[str, str | None], str]]:
    """(name, changes to the good env, expected preflight line) per bad setting."""
    return [
        (
            "SHIFDE_TRUSTED_PROXIES empty",
            {"SHIFDE_TRUSTED_PROXIES": ""},
            "refused: trusted proxies",
        ),
        (
            "SHIFDE_HSTS_MAX_AGE_SECONDS=86400",
            {"SHIFDE_HSTS_MAX_AGE_SECONDS": "86400"},
            "refused: hsts",
        ),
        ("SHIFDE_API_DOCS_ENABLED=true", {"SHIFDE_API_DOCS_ENABLED": "true"}, "refused: api docs"),
        (
            "local login allowed in production",
            {"SHIFDE_AUTH_LOCAL_ENABLED": "true", "SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION": "true"},
            "refused: local login",
        ),
        (
            "SHIFDE_DATABASE_URL as the schema owner",
            {"SHIFDE_DATABASE_URL": owner_url},
            "refused: database role",
        ),
        (
            "SHIFDE_DATABASE_URL on a closed port",
            {"SHIFDE_DATABASE_URL": unreachable_url},
            "refused: database unreachable",
        ),
        (
            "SHIFDE_METRICS_TOKEN removed",
            {"SHIFDE_METRICS_TOKEN": None},
            "refused: configuration invalid",
        ),
        (
            "SHIFDE_LOG_FORMAT=console",
            {"SHIFDE_LOG_FORMAT": "console"},
            "refused: configuration invalid",
        ),
        (
            "SHIFDE_PUBLIC_BASE_URL removed",
            {"SHIFDE_PUBLIC_BASE_URL": None},
            "refused: configuration invalid",
        ),
    ]


def register_verdict(rows: Sequence[tuple[str, str]], mode: str) -> tuple[list[str], list[str]]:
    """(failures, pending ids) of the register rows `(id, state)` under `mode` (`local`/`final`)."""
    failures = [f"open: {rid}" for rid, state in rows if state not in REGISTER_OK]
    pending = [rid for rid, state in rows if state == "pending-owner"]
    if mode == "final":
        failures += [f"pending-owner: {rid}" for rid in pending]
        if not any(
            rid == HOSTING_TARGET and state in ("closed", "accepted") for rid, state in rows
        ):
            failures.append(f"no closed or accepted {HOSTING_TARGET} row")
    return failures, pending


def write_env_file(path: Path, values: Mapping[str, str]) -> Path:
    """`KEY=VALUE` lines, readable only by the owner."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(f"{key}={value}\n" for key, value in values.items())
    return path


class LoopbackServer(http.server.ThreadingHTTPServer):
    """A threading HTTP server whose port no other socket can bind while it is open. The base
    sets SO_REUSEADDR, which on Windows lets a second socket bind the same port and take its
    connections; SO_EXCLUSIVEADDRUSE (Windows only) refuses even such a socket."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class EdgeProxy:
    """A loopback reverse proxy in a thread: every request goes to `upstream_port` with
    `X-Forwarded-For` replaced by the connecting peer's address (the supported edge, design 5)."""

    def __init__(self, upstream_port: int) -> None:
        upstream = upstream_port

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in HOP_BY_HOP and k.lower() != "x-forwarded-for"
                }
                headers["X-Forwarded-For"] = self.client_address[0]
                conn = http.client.HTTPConnection("127.0.0.1", upstream, timeout=30)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    response = conn.getresponse()
                    data = response.read()
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        # the handler sends its own Server and Date
                        if key.lower() not in HOP_BY_HOP | {"server", "date"}:
                            self.send_header(key, value)
                except (OSError, http.client.HTTPException):
                    data = b""
                    self.send_response(502)
                finally:
                    conn.close()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
                pass  # no request line on the terminal

        self.server = LoopbackServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> EdgeProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(10)


# --- processes and sockets -----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_get(port: int, path: str, headers: Mapping[str, str] | None = None) -> tuple[int, Any]:
    """(status, headers) of a GET on 127.0.0.1; status 0 when nothing answered."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=dict(headers or {}))
        response = conn.getresponse()
        response.read()
        return response.status, response.headers
    except (OSError, http.client.HTTPException):
        return 0, None
    finally:
        conn.close()


def console_script(name: str) -> str:
    path = VENV_BIN / (f"{name}.exe" if WINDOWS else name)
    if not path.is_file():
        raise NotRun(f"{name} is not installed: run uv sync in backend")
    return str(path)


def run_child(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    """(exit code, combined output) of a child run to completion; the output is never printed."""
    # fixed argv: this checkout's console scripts and scripts
    done = subprocess.run(  # noqa: S603
        argv,
        cwd=ROOT,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def spawn(argv: list[str], env: Mapping[str, str], log: Path) -> subprocess.Popen[bytes]:
    """A long-running child in its own process group, its output appended to `log`."""
    kwargs: dict[str, Any] = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log.open("ab") as fh:
        # fixed argv: this checkout's console scripts and scripts
        return subprocess.Popen(  # noqa: S603
            argv,
            cwd=ROOT,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            **kwargs,
        )


def stop(proc: subprocess.Popen[bytes], grace: float = 20.0) -> None:
    """Stop a child gracefully (Windows `CTRL_BREAK_EVENT`, elsewhere SIGTERM), then hard-kill."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        if WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def hard_kill(pid: int) -> None:
    """SIGKILL where it exists; on Windows `os.kill` with SIGTERM is TerminateProcess."""
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def wait_until(check: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return check()


def wait_ready(proc: subprocess.Popen[bytes], port: int) -> bool:
    """`/api/ready` answers 200 before the child exits or the timeout passes."""
    return wait_until(
        lambda: proc.poll() is not None or http_get(port, "/api/ready")[0] == 200, READY_SECONDS
    ) and (proc.poll() is None)


# --- the run -------------------------------------------------------------------------------------


def _in_backend_env() -> bool:
    """`shifde` imports from this checkout's backend (not another checkout's active venv)."""
    try:
        import shifde
    except ImportError:
        return False
    return Path(shifde.__file__).resolve().is_relative_to(BACKEND)


def rerun_in_backend(mode: str) -> int:
    """Run this script once more under `uv run` in the backend environment; its exit code."""
    if os.environ.get(RERUN_FLAG):
        raise NotRun("the backend environment has no shifde package: run uv sync in backend")
    uv = shutil.which("uv")
    if not uv:
        raise NotRun("uv is not installed: the check needs the backend environment")
    env = {**os.environ, "UV_LINK_MODE": "copy", RERUN_FLAG: "1"}
    command = [uv, "run", "--frozen", "--project", str(BACKEND)]
    command += ["python", str(Path(__file__).resolve()), f"--{mode}"]
    # fixed arguments: uv, this script and its mode flag
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode  # noqa: S603


def _pg_dump_present() -> bool:
    directory = os.environ.get("SHIFDE_PG_BIN_DIR")
    if directory:
        return (Path(directory) / ("pg_dump.exe" if WINDOWS else "pg_dump")).is_file()
    return shutil.which("pg_dump") is not None


def step(name: str, call: Callable[[], Any]) -> Any:
    try:
        result = call()
    except (StepFailed, NotRun):
        raise
    except Exception:
        # the exception text may carry a URL or a response body: name the step only
        print(f"FAIL {name}", flush=True)
        raise StepFailed(name) from None
    print(f"ok   {name}", flush=True)
    return result


def need(ok: bool) -> None:
    """An explicit check inside a step (kept under `python -O`, unlike `assert`)."""
    if not ok:
        raise ValueError("check failed")


class Check:
    def __init__(self, mode: str, stack: contextlib.ExitStack, workdir: Path) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import NullPool
        from tests.conftest import _env_or_none

        from shifde.core.crypto import KeyRing

        self.mode = mode
        self.stack = stack
        self.workdir = workdir
        urls = {key: _env_or_none(key) for key in URL_KEYS}
        if not all(urls.values()):
            raise NotRun(
                ".devdata/dev.env is missing or incomplete: run python scripts/dev_db.py env"
            )
        self.admin_url, self.app_url, self.owner_url = (str(urls[key]) for key in URL_KEYS)
        self.app_role = make_url(self.app_url).username or "shifde_app"
        self.owner_role = make_url(self.owner_url).username or "shifde_owner"
        if not _pg_dump_present():
            raise NotRun("pg_dump not found: put it on PATH or set SHIFDE_PG_BIN_DIR")
        if port_in_use(RESTORED_API_PORT):
            raise NotRun(f"port {RESTORED_API_PORT} is in use")
        # a stopped cluster is refused within the timeout (Windows retries a refused connect)
        self.admin = create_engine(
            self.admin_url,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            connect_args={"connect_timeout": 10},
        )
        stack.callback(self.admin.dispose)
        self.secrets = {
            "SHIFDE_API_KEY_PEPPER": secrets.token_urlsafe(48),
            "SHIFDE_SECRET_KEYS": KeyRing.generate_entry(),
            "SHIFDE_METRICS_TOKEN": secrets.token_urlsafe(32),
        }
        self.admin_cli = console_script("shifde-admin")
        self.api_cli = console_script("shifde-api")
        console_script("shifde-worker")
        self.drop_failed = False

    # databases --------------------------------------------------------------------------------
    def scratch(self, what: str) -> tuple[str, str, str]:
        """(name, app URL, owner URL) of a new empty `shifde_golive_<hex>`, dropped at the end."""
        from sqlalchemy.exc import SQLAlchemyError
        from tests.conftest import _create_database, _with_db

        name = f"shifde_golive_{secrets.token_hex(6)}"
        try:
            with self.admin.connect() as c:
                _create_database(c, name, self.owner_role, self.app_role, "template0")
        except SQLAlchemyError:
            if what == "source":
                raise NotRun(
                    "the local cluster is not reachable: run python scripts/dev_db.py start"
                ) from None
            print(f"FAIL {what} database created", flush=True)
            raise StepFailed(f"{what} database created") from None
        self.stack.callback(self.drop, name)
        return name, _with_db(self.app_url, name), _with_db(self.owner_url, name)

    def drop(self, name: str) -> None:
        from tests.conftest import _drop_database

        try:
            with self.admin.connect() as c:
                _drop_database(c, name)
        except Exception:
            self.drop_failed = True
            print(f"FAIL scratch database {name} dropped", flush=True)
        else:
            print(f"ok   scratch database {name} dropped", flush=True)

    # helpers ------------------------------------------------------------------------------------
    def env(self, values: Mapping[str, str]) -> dict[str, str]:
        return child_env(os.environ, values)

    def admin_cmd(self, values: Mapping[str, str], *args: str) -> tuple[int, str]:
        return run_child([self.admin_cli, *args], self.env(values))

    def start_api(self, values: Mapping[str, str], port: int, log: str) -> subprocess.Popen[bytes]:
        values = {**values, "SHIFDE_API_HOST": "127.0.0.1", "SHIFDE_API_PORT": str(port)}
        proc = spawn([self.api_cli], self.env(service_env(values)), self.workdir / log)
        self.stack.callback(stop, proc)
        return proc

    # steps ----------------------------------------------------------------------------------------
    def run(self) -> None:
        from sqlalchemy.engine import make_url

        source, app_url, owner_url = self.scratch("source")
        good = good_env(app_url, owner_url, self.secrets)
        code, _ = self.admin_cmd(good, "db", "upgrade")
        step(f"scratch database {source} created and migrated", lambda: need(code == 0))
        closed = make_url(app_url).set(port=free_port()).render_as_string(hide_password=False)
        self.preflight(good, owner_url, closed)
        self.edge(good, owner_url)
        self.procctl(good)
        backup_dir = self.backup(app_url, owner_url, good)
        self.restore(good, backup_dir)
        self.two_orgs()
        self.register()

    def preflight(self, good: dict[str, str], owner_url: str, closed: str) -> None:
        def check(values: Mapping[str, str], label: str) -> tuple[int, str]:
            path = write_env_file(self.workdir / f"preflight-{label}.env", values)
            return self.admin_cmd({}, "config", "preflight", "--env-file", str(path))

        def passes() -> None:
            code, out = check(good, "good")
            need(code == 0 and not re.search(r"^refused:", out, re.M))

        step("preflight passes on the production env file", passes)
        for index, (name, changes, expected) in enumerate(preflight_variants(owner_url, closed)):

            def refuses(
                changes: Mapping[str, str | None] = changes,
                expected: str = expected,
                index: int = index,
            ) -> None:
                code, out = check(changed(good, changes), f"variant-{index}")
                need(code != 0 and any(line.startswith(expected) for line in out.splitlines()))

            step(f"preflight refuses {name} ({expected})", refuses)

    def edge(self, good: dict[str, str], owner_url: str) -> None:
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool

        port = free_port()
        values = {
            **good,
            "SHIFDE_AUTH_LOCAL_ENABLED": "true",
            "SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION": "true",
            "SHIFDE_TRUSTED_PROXIES": "127.0.0.1/32",
            "SHIFDE_TRUST_LOOPBACK_PROXY": "false",
            "SHIFDE_ALLOWED_ORIGINS": f"{PUBLIC_BASE_URL},{LOOPBACK_ORIGIN}",
        }
        api = self.start_api(values, port, "api-edge.log")
        step("api in production is ready", lambda: need(wait_ready(api, port)))
        with EdgeProxy(port) as proxy:

            def headers() -> None:
                status, got = http_get(proxy.port, "/api/health")
                need(status == 200 and got is not None)
                for name, value in EDGE_HEADERS.items():
                    need(got.get(name) == value)
                need("default-src 'none'" in (got.get("Content-Security-Policy") or ""))
                need("max-age=" in (got.get("Strict-Transport-Security") or ""))

            step("edge: /api/health 200 with the security headers and HSTS", headers)
            step("edge: /api/docs 404", lambda: need(http_get(proxy.port, "/api/docs")[0] == 404))
            step(
                "edge: /api/openapi.json 404",
                lambda: need(http_get(proxy.port, "/api/openapi.json")[0] == 404),
            )

            def forwarded() -> None:
                email = f"nobody-{secrets.token_hex(4)}@golive.invalid"
                body = json.dumps({"email": email, "password": secrets.token_urlsafe(12)})
                conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=30)
                try:
                    conn.request(
                        "POST",
                        "/api/auth/local/login",
                        body=body.encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Origin": LOOPBACK_ORIGIN,
                            "X-Forwarded-For": SPOOFED_IP,
                        },
                    )
                    response = conn.getresponse()
                    response.read()
                    need(response.status == 401)
                finally:
                    conn.close()
                owner = create_engine(owner_url, poolclass=NullPool)
                try:
                    with owner.connect() as c:
                        failed = c.execute(
                            text(
                                "SELECT count(*) FROM shifde.audit_events "
                                "WHERE action = 'identity.login.failed' AND actor_ip = '127.0.0.1'"
                            )
                        ).scalar_one()
                        spoofed = c.execute(
                            text(
                                "SELECT (SELECT count(*) FROM shifde.audit_events "
                                "WHERE actor_ip = :ip) + "
                                "(SELECT count(*) FROM shifde.sessions WHERE ip = :ip)"
                            ),
                            {"ip": SPOOFED_IP},
                        ).scalar_one()
                finally:
                    owner.dispose()
                need(failed >= 1 and spoofed == 0)

            step(
                "edge: a spoofed X-Forwarded-For is ignored and the peer address 127.0.0.1 audited",
                forwarded,
            )
        stop(api)

    def procctl(self, good: dict[str, str]) -> None:
        script = str(ROOT / "scripts" / "procctl.py")
        api_port, worker_port = free_port(), free_port()
        ports = {
            "SHIFDE_API_HOST": "127.0.0.1",
            "SHIFDE_API_PORT": str(api_port),
            "SHIFDE_WORKER_HEALTH_PORT": str(worker_port),
        }
        state = self.workdir / "procctl"
        env = self.env({})

        def ctl(*args: str) -> tuple[int, str]:
            return run_child([sys.executable, script, *args, "--state-dir", str(state)], env)

        def pids() -> dict[str, str]:
            _, out = ctl("status")
            return dict(re.findall(r"^(\w+) pid=(\S+)", out, re.M))

        path = write_env_file(self.workdir / "procctl.env", service_env({**good, **ports}))
        log = self.workdir / "procctl-up.log"
        up = spawn(
            [sys.executable, script, "up", "--env-file", str(path), "--state-dir", str(state)],
            env,
            log,
        )
        self.stack.callback(stop, up, 60.0)
        self.stack.callback(ctl, "down")
        step(
            "procctl up: api and worker ready",
            lambda: need(
                wait_until(lambda: up.poll() is not None or ctl("status")[0] == 0, 300)
                and up.poll() is None
            ),
        )

        def restarted() -> None:
            old = pids().get("worker", "-")
            need(old.isdigit())
            hard_kill(int(old))

            def back() -> bool:
                new = pids().get("worker", "-")
                return new.isdigit() and new != old and http_get(worker_port, "/readyz")[0] == 200

            need(wait_until(back, RESTART_SECONDS))

        step(
            "procctl: a hard-killed worker is restarted (new pid, /readyz 200 within 30 s)",
            restarted,
        )

        def down() -> None:
            code, _ = ctl("down")
            need(code == 0)
            up.wait(60)
            need(not port_in_use(api_port) and not port_in_use(worker_port))

        step("procctl down exits 0 and neither port accepts connections", down)

        refused_path = write_env_file(
            self.workdir / "procctl-refused.env",
            {**changed(good, {"SHIFDE_METRICS_TOKEN": None}), **ports},
        )
        refused_log = self.workdir / "procctl-refused.log"
        refused_state = self.workdir / "procctl-refused"
        refused = spawn(
            [
                sys.executable,
                script,
                "up",
                "--env-file",
                str(refused_path),
                "--state-dir",
                str(refused_state),
            ],
            env,
            refused_log,
        )
        self.stack.callback(stop, refused, 60.0)

        def not_restarted() -> None:
            def output() -> str:
                return refused_log.read_text(encoding="utf-8", errors="replace")

            reported = re.compile(
                r"^procctl: (\w+) exited 2 \(configuration refused\); not restarted", re.M
            )
            need(wait_until(lambda: bool(reported.search(output())), READY_SECONDS))
            time.sleep(NOT_RESTARTED_SECONDS)
            match = reported.search(output())
            need(match is not None)
            name = match.group(1) if match else ""
            need(len(re.findall(rf"^procctl: {name} started", output(), re.M)) == 1)
            need(refused.wait(60) == 2)
            code, _ = run_child(
                [sys.executable, script, "down", "--state-dir", str(refused_state)], env
            )
            need(code == 0)

        step("procctl: a child exiting 2 (configuration refused) is not restarted", not_restarted)

    def backup(self, app_url: str, owner_url: str, good: dict[str, str]) -> Path:
        seed = {
            "SHIFDE_ENV": "development",
            "SHIFDE_DATABASE_URL": app_url,
            "SHIFDE_MIGRATION_DATABASE_URL": owner_url,
            "SHIFDE_AUTH_LOCAL_ENABLED": "true",
            **self.secrets,
        }
        args = ["dev-seed", "--reuse-passwords"] if DEV_USERS.is_file() else ["dev-seed"]
        step("dev-seed on the source database", lambda: need(self.admin_cmd(seed, *args)[0] == 0))
        out = self.workdir / "backup"
        step(
            "backup create",
            lambda: need(self.admin_cmd(good, "backup", "create", "--out", str(out))[0] == 0),
        )
        return out

    def restore(self, good: dict[str, str], backup_dir: Path) -> None:
        name, self.target_app, self.target_owner = self.scratch("restore target")
        print(f"ok   restore target database {name} created", flush=True)

        def restored() -> None:
            code, out = self.admin_cmd(
                {**good, "GOLIVE_RESTORE_TARGET_URL": self.target_owner},
                "backup",
                "restore",
                "--from",
                str(backup_dir),
                "--target-url-env",
                "GOLIVE_RESTORE_TARGET_URL",
            )
            need(code == 0 and "RESTORE VERIFIED" in out.splitlines())

        step("backup restore: RESTORE VERIFIED", restored)

    def two_orgs(self) -> None:
        if port_in_use(RESTORED_API_PORT):
            raise NotRun(f"port {RESTORED_API_PORT} is in use")
        values = {
            "SHIFDE_ENV": "development",
            "SHIFDE_DATABASE_URL": self.target_app,
            "SHIFDE_AUTH_LOCAL_ENABLED": "true",
            **self.secrets,
        }
        api = self.start_api(values, RESTORED_API_PORT, "api-restored.log")
        step(
            f"api on the restored database is ready on {RESTORED_API_PORT}",
            lambda: need(wait_ready(api, RESTORED_API_PORT)),
        )

        def proof() -> None:
            code, out = run_child(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "two_org_proof.py"),
                    "--base-url",
                    f"http://127.0.0.1:{RESTORED_API_PORT}",
                    "--origin",
                    TWO_ORG_ORIGIN,
                    "--users",
                    str(DEV_USERS),
                ],
                self.env({}),
            )
            need(code == 0 and "TWO-ORG PROOF PASSED" in out)

        step("two-org proof on the restored database: TWO-ORG PROOF PASSED", proof)
        stop(api)

    def register(self) -> None:
        from shifde.core.preflight import DEFAULT_REGISTER, read_register

        try:
            rows = [(r.id, r.state) for r in read_register(DEFAULT_REGISTER)]
        except Exception:
            print("FAIL register: unreadable or invalid", flush=True)
            raise StepFailed("register") from None
        failures, pending = register_verdict(rows, self.mode)
        for rid in pending:
            print(f"pending: {rid}", flush=True)
        for failure in failures:
            print(f"FAIL register: {failure}", flush=True)
        if failures:
            raise StepFailed("register")
        print("ok   register", flush=True)


def run_check(mode: str) -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    outcome: str | None = None
    check: Check | None = None
    try:
        with contextlib.ExitStack() as stack:
            workdir = Path(tempfile.mkdtemp(prefix="golive_"))  # owner-only (mode 0o700)
            stack.callback(shutil.rmtree, workdir, True)
            check = Check(mode, stack, workdir)
            try:
                check.run()
            except StepFailed as failed:
                outcome = failed.step
            except NotRun:
                raise
            except KeyboardInterrupt:
                outcome = "interrupted"
            except Exception:
                # outside any named step; its text may carry a URL or a response body
                print("FAIL an unexpected error outside the named steps (details withheld)")
                outcome = "unexpected error"
    except KeyboardInterrupt:
        outcome = outcome or "interrupted"
    if outcome is None and check is not None and check.drop_failed:
        outcome = "drop the scratch databases"
    if outcome is not None:
        print(f"GO-LIVE CHECK FAILED: {outcome}")
        return 1
    print(f"GO-LIVE CHECK PASSED ({mode})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--local", action="store_true", help="prove sub-project 4 met locally")
    group.add_argument(
        "--final", action="store_true", help="--local plus the go-live register rules"
    )
    args = parser.parse_args(argv)
    if not (args.local or args.final):
        print(f"GO-LIVE CHECK NOT RUN: {NO_MODE}")
        return 2
    mode = "final" if args.final else "local"
    try:
        if not _in_backend_env():
            return rerun_in_backend(mode)
        return run_check(mode)
    except NotRun as exc:
        print(f"GO-LIVE CHECK NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
