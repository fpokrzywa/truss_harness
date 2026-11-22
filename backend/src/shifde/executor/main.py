"""`shifde-executor`: run the organization's delivery jobs until interrupted (design 2 and 5;
B-2693, B-2694).

Configured from the environment only (never `shifde.core.settings`, which reaches the database):
`SHIFDE_EXECUTOR_API_URL`, `SHIFDE_EXECUTOR_TOKEN`, `SHIFDE_EXECUTOR_CREDENTIALS_FILE` and
`SHIFDE_ENV` (`development`, `test` or, when unset, `production`). Outside development and test
the API URL must be https and `--fake-platform` is refused. No message ever echoes the token.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker, CredentialFileRefused
from shifde.executor.runner import FakePlatform, Platform, RealPlatform, Runner

API_URL = "SHIFDE_EXECUTOR_API_URL"
TOKEN = "SHIFDE_EXECUTOR_TOKEN"
CREDENTIALS_FILE = "SHIFDE_EXECUTOR_CREDENTIALS_FILE"
REQUIRED = (API_URL, TOKEN, CREDENTIALS_FILE)
DEV_ENVS = ("development", "test")

EPILOG = f"""environment:
  {API_URL}           the SETH API origin (https outside development and test)
  {TOKEN}             the executor credential (shx_...) issued on the Executors screen
  {CREDENTIALS_FILE}  JSON: credential_ref -> {{tenant_id, host, token, service_principal}},
                                    one organization per file
  SHIFDE_ENV                        development | test | production (default production)
"""


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str
    broker: CredentialBroker
    platform: Platform

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
        help="run against the in-memory fake Databricks platform (development and test only)",
    )
    return parser


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
    try:
        broker = CredentialBroker.from_file(Path(environ[CREDENTIALS_FILE]))
    except CredentialFileRefused as exc:
        parser.error(str(exc))
    platform: Platform = FakePlatform() if args.fake_platform else RealPlatform()
    return Config(api_url=api_url, token=environ[TOKEN], broker=broker, platform=platform)


def run(argv: Sequence[str] | None = None) -> None:
    config = configure(argv, os.environ)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with GatewayClient(config.api_url, config.token) as client:
        me = client.whoami()
        logging.getLogger(__name__).info(
            "executor %s running against the %s platform", me.executor_id, config.platform.kind
        )
        runner = Runner(client, config.broker, me.executor_id, config.platform)
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            runner.stop()


if __name__ == "__main__":  # pragma: no cover
    run()
