"""`shifde-executor`: run the organization's delivery jobs until interrupted (design 2 and 5;
B-2693, B-2694).

Configured from the environment only (never `shifde.core.settings`, which reaches the database):
`SHIFDE_EXECUTOR_API_URL`, `SHIFDE_EXECUTOR_TOKEN`, `SHIFDE_EXECUTOR_CREDENTIALS_FILE` and
`SHIFDE_ENV` (`development`, `test` or, when unset, `production`). Outside development and test
the API URL must be https and `--fake-platform` is refused. With `--fake-platform`, Fabric jobs run
on the in-memory fake only when `SHIFDE_DEV_FAKE_DATA_PLATFORMS` is true (SP8-5). No message ever
echoes the token.
`SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES` (optional) is the installation's list of extra Palantir
host suffixes, read with the API's rules (`core.hostsuffixes`): an invalid list is exit 2, naming
the variable only.

Process control (SP4 design 6.2): SIGTERM, SIGINT and CTRL_BREAK stop the runner (no new lease;
the job in flight runs to its normal end) and the process waits up to
`SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS` for it: exit 0 when drained, 1 when a job was still in
flight (nothing more is posted for it; the server's unknown-outcome path decides it) or on a
crash, 2 configuration refused (also when the credentials file's organization is not the one
`whoami` names), 5 when `whoami` answers 401 or 403. Any other `whoami` failure is
retried every `POLL_SECONDS`. With `SHIFDE_EXECUTOR_HEALTH_PORT` set, `GET /healthz` and
`GET /readyz` answer from the runner's ticks (`Runner.live`, `Runner.ready`).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from shifde.core.healthserver import HealthServer
from shifde.core.hostsuffixes import checked_palantir_suffixes, split_suffixes
from shifde.core.httplogs import quiet_http_client_logs
from shifde.executor.client import GatewayClient, GatewayError, Whoami
from shifde.executor.credentials import CredentialBroker, CredentialFileRefused
from shifde.executor.runner import POLL_SECONDS, FakePlatform, Platform, RealPlatform, Runner

API_URL = "SHIFDE_EXECUTOR_API_URL"
TOKEN = "SHIFDE_EXECUTOR_TOKEN"
CREDENTIALS_FILE = "SHIFDE_EXECUTOR_CREDENTIALS_FILE"
HEALTH_HOST = "SHIFDE_EXECUTOR_HEALTH_HOST"
HEALTH_PORT = "SHIFDE_EXECUTOR_HEALTH_PORT"
SHUTDOWN_GRACE = "SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS"
FAKE_DATA_PLATFORMS = "SHIFDE_DEV_FAKE_DATA_PLATFORMS"
PALANTIR_SUFFIXES = "SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES"
REQUIRED = (API_URL, TOKEN, CREDENTIALS_FILE)
DEV_ENVS = ("development", "test")
EXIT_STOPPED = 0  # stopped on request and drained
EXIT_CRASHED = 1  # crashed, or the shutdown grace passed with a job in flight: restart it
EXIT_CONFIG = 2  # as `parser.error`
EXIT_AUTH = 5  # `whoami` answered 401 / 403: a restart cannot fix the credential
RUNNER_THREAD = "shifde-executor-runner"

EPILOG = f"""environment:
  {API_URL}           the SETH API origin (https outside development and test)
  {TOKEN}             the executor credential (shx_...) issued on the Executors screen
  {CREDENTIALS_FILE}  JSON: credential_ref -> an entry of the target's platform, one
                                    organization per file:
                                    Databricks {{tenant_id, host, token, service_principal}}
                                    ("platform": "databricks" optional)
                                    Snowflake {{tenant_id, host, platform: "snowflake", user,
                                    role, private_key_file}} (unencrypted PKCS#8 PEM; on POSIX
                                    the file and the key file mode 0600)
                                    Fabric {{tenant_id, host, platform: "fabric",
                                    entra_tenant_id, client_id, client_secret_file}}
                                    Palantir {{tenant_id, host, platform: "palantir",
                                    client_id, client_secret_file}}
                                    Docker {{tenant_id, host, platform: "docker"}}
                                    (a secret file holds one value; on POSIX mode 0600;
                                    Fabric and Palantir jobs run (experimental, not
                                    live validated; on the fakes with --fake-platform))
  SHIFDE_ENV                        development | test | production (default production)
  {FAKE_DATA_PLATFORMS}     true: with --fake-platform, Fabric jobs run on the
                                    in-memory fake (they fail closed otherwise)
  {PALANTIR_SUFFIXES}
                                    extra Palantir stack host suffixes, comma-separated (the
                                    API's value; default none)
  {HEALTH_HOST}       the /healthz and /readyz host (default 127.0.0.1)
  {HEALTH_PORT}       their port, 0..65535 (default 0: off)
  {SHUTDOWN_GRACE}
                                    seconds a stop waits for the job in flight (default 60)
"""


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str
    broker: CredentialBroker
    platform: Platform
    health_host: str = "127.0.0.1"
    health_port: int = 0
    shutdown_grace_seconds: int = 60

    def __repr__(self) -> str:
        return f"Config(api_url={self.api_url!r}, platform={self.platform.kind!r})"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shifde-executor",
        description="Lease and run this organization's SETH delivery jobs.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fake-platform",
        action="store_true",
        help=(
            "run against the in-memory fake Databricks, Snowflake, Fabric, Palantir and Docker "
            "platforms (development and test only; Fabric jobs run only with "
            f"{FAKE_DATA_PLATFORMS}=true)"
        ),
    )
    return parser


def _integer(
    parser: argparse.ArgumentParser,
    environ: Mapping[str, str],
    name: str,
    default: int,
    low: int,
    high: int | None = None,
) -> int:
    """`environ[name]` as an integer in `low..high`, `default` when unset; exit 2 naming the
    variable (never its value) otherwise."""
    raw = environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        parser.error(f"{name} must be an integer")
    if value < low or (high is not None and value > high):
        bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
        parser.error(f"{name} must be {bounds}")
    return value


def configure(argv: Sequence[str] | None, environ: Mapping[str, str]) -> Config:
    """The validated configuration, or exit 2 with a message (never the token)."""
    parser = _parser()
    args = parser.parse_args(argv)
    missing = [name for name in REQUIRED if not environ.get(name)]
    if missing:
        parser.error(f"missing environment variable(s): {', '.join(missing)}")
    dev = (environ.get("SHIFDE_ENV") or "production") in DEV_ENVS
    api_url = environ[API_URL]
    scheme = urlsplit(api_url).scheme
    if scheme not in ("https", "http") or (scheme != "https" and not dev):
        parser.error(f"{API_URL} must be an https URL outside development and test")
    if args.fake_platform and not dev:
        parser.error("--fake-platform is refused unless SHIFDE_ENV is development or test")
    health_port = _integer(parser, environ, HEALTH_PORT, 0, 0, 65535)
    grace = _integer(parser, environ, SHUTDOWN_GRACE, 60, 1)
    try:
        suffixes = checked_palantir_suffixes(
            split_suffixes(environ.get(PALANTIR_SUFFIXES) or ""), PALANTIR_SUFFIXES
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        broker = CredentialBroker.from_file(
            Path(environ[CREDENTIALS_FILE]), palantir_host_suffixes=suffixes
        )
    except CredentialFileRefused as exc:
        parser.error(str(exc))
    platform: Platform = RealPlatform()
    if args.fake_platform:
        fake = FakePlatform()
        # the Snowflake, Fabric, Palantir and Docker fakes refuse unless this is development or test
        fake.environment = environ.get("SHIFDE_ENV") or "production"
        # and the Fabric fake also refuses without this switch
        fake.fabric_enabled = (environ.get(FAKE_DATA_PLATFORMS) or "").strip().lower() == "true"
        platform = fake
    return Config(
        api_url=api_url,
        token=environ[TOKEN],
        broker=broker,
        platform=platform,
        health_host=environ.get(HEALTH_HOST) or "127.0.0.1",
        health_port=health_port,
        shutdown_grace_seconds=grace,
    )


class Process:
    """One executor process: the health port, the start (`whoami`, retried) and the runner on a
    thread of its own, so the main thread can enforce the shutdown grace."""

    def __init__(self, config: Config, client: GatewayClient) -> None:
        self.config, self.client = config, client
        self.stopping = threading.Event()
        self.runner: Runner | None = None
        self.start_tick = time.monotonic()  # the start loop's last pass, until the runner runs

    def request_stop(self) -> None:
        """The signal handler: no new lease; the job in flight runs to its normal end."""
        self.stopping.set()
        runner = self.runner
        if runner is not None:
            runner.stop()

    def live(self) -> bool:
        runner = self.runner
        if runner is None:
            return time.monotonic() - self.start_tick <= 3 * POLL_SECONDS
        return runner.live()

    def ready(self) -> bool:
        runner = self.runner
        return runner is not None and not self.stopping.is_set() and runner.ready()

    def serve(self) -> int:
        """Run until stopped; the process exit code."""
        server: HealthServer | None = None
        if self.config.health_port:
            try:
                server = HealthServer(
                    self.config.health_host,
                    self.config.health_port,
                    live=self.live,
                    ready=self.ready,
                ).start()
            except OSError as exc:
                print(
                    f"shifde-executor: refusing to start: cannot bind {HEALTH_PORT} "
                    f"({exc.__class__.__name__})",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_CONFIG
        try:
            return self._serve()
        finally:
            if server is not None:
                server.stop()

    def _start(self) -> Whoami | int:
        """`whoami`, retried every `POLL_SECONDS` until it answers or a stop is requested (an exit
        code then); 401 / 403 exit `EXIT_AUTH`."""
        log = logging.getLogger(__name__)
        while not self.stopping.is_set():
            self.start_tick = time.monotonic()
            failure: Exception
            try:
                return self.client.whoami()
            except GatewayError as exc:
                if exc.status in (401, 403):
                    print(
                        f"shifde-executor: refusing to start: the gateway answered {exc.status} "
                        "to whoami (check the executor credential)",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_AUTH
                failure = exc
            except httpx.HTTPError as exc:
                failure = exc
            log.warning("whoami failed (%s); retrying", type(failure).__name__)
            self.stopping.wait(POLL_SECONDS)
        return EXIT_STOPPED

    def _serve(self) -> int:
        log = logging.getLogger(__name__)
        me = self._start()
        if isinstance(me, int):
            return me
        log.info(
            "executor %s running against the %s platform", me.executor_id, self.config.platform.kind
        )
        if self.config.broker.tenant_id not in (None, me.tenant_id):
            print(
                "shifde-executor: refusing to start: the credentials file belongs to another "
                "organization than this executor",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_CONFIG
        runner = Runner(
            self.client,
            self.config.broker,
            me.executor_id,
            self.config.platform,
            tenant_id=me.tenant_id,
        )
        self.runner = runner
        if self.stopping.is_set():
            runner.stop()
        thread = threading.Thread(target=runner.run_forever, daemon=True, name=RUNNER_THREAD)
        thread.start()
        while not self.stopping.wait(0.2):
            if not thread.is_alive():
                log.error("the runner stopped unexpectedly")
                return EXIT_CRASHED
        grace = self.config.shutdown_grace_seconds
        thread.join(grace)
        if not thread.is_alive():
            log.info("executor stopped")
            return EXIT_STOPPED
        write = runner.write_in_flight()
        runner.abandon()
        log.warning(
            "shutdown grace of %d s passed with a job in flight (%s); no result is posted",
            grace,
            "a platform write was in flight" if write else "no platform write was in flight",
        )
        return EXIT_CRASHED


def _install_signals(process: Process) -> None:
    def handler(_signum: int, _frame: object) -> None:
        process.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread / unsupported here
                logging.getLogger(__name__).warning("signal %s unavailable", name)


def run(argv: Sequence[str] | None = None) -> int:
    """The process exit code (`EXIT_*`); an unhandled exception is a crash (exit 1)."""
    config = configure(argv, os.environ)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_http_client_logs()  # no gateway request URL at INFO (B-3901)
    with GatewayClient(config.api_url, config.token) as client:
        process = Process(config, client)
        _install_signals(process)
        return process.serve()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
