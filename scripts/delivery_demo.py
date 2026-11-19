"""Scripted delivery demo against the fake Databricks platform (design SP2 section 7 item 10,
settled call 19).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient and the `shifde.executor`
runner over it with the fake platform. Nothing contacts a Databricks endpoint. Needs the local
cluster running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/delivery_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
In it a fresh organization's requester registers a target, creates an executor and issues its
credential, and asks for a plan; the runner seals it; the requester cannot approve it and a second
tenant administrator does; the runner then applies, observes and verifies the deployment. The
test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `DELIVERY DEMO PASSED` (exit 0) or
`DELIVERY DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a token,
password or database URL. Standard library only at the top level: when `shifde` is not importable
it re-runs itself once in the backend environment (`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FAKE_ONLY = (
    "only the fake platform is supported (no Databricks workspace is configured): "
    "run with --fake"
)
RERUN_FLAG = "SHIFDE_DELIVERY_DEMO_RERUN"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)


class DemoError(Exception):
    """The demo could not run (no cluster, no dev.env, no backend environment)."""


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def _in_backend_env() -> bool:
    try:
        import shifde  # noqa: F401
    except ImportError:
        return False
    return True


def rerun_in_backend() -> int:
    """Run this script once more under `uv run` in the backend environment; its exit code."""
    if os.environ.get(RERUN_FLAG):
        raise DemoError("the backend environment has no shifde package: run uv sync in backend")
    uv = shutil.which("uv")
    if not uv:
        raise DemoError("uv is not installed: the demo needs the backend environment")
    env = {**os.environ, "UV_LINK_MODE": "copy", RERUN_FLAG: "1"}
    command = [uv, "run", "--frozen", "--project", str(BACKEND)]
    command += ["python", str(Path(__file__).resolve()), "--fake"]
    # fixed arguments: uv, this script and --fake
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode  # noqa: S603


@contextlib.contextmanager
def scratch_database() -> Iterator[tuple[str, str]]:
    """(app_url, owner_url) of a migrated `shifde_demo_<hex>` database, dropped afterwards."""
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool

    from shifde.core.migrations import alembic_config
    from tests.conftest import _create_database, _drop_database, _env_or_none, _with_db

    urls = {key: _env_or_none(key) for key in URL_KEYS}
    if not all(urls.values()):
        raise DemoError(
            ".devdata/dev.env is missing or incomplete: run python scripts/dev_db.py env"
        )
    admin_url, app_url, owner_url = (str(urls[key]) for key in URL_KEYS)
    app_role = make_url(app_url).username or "shifde_app"
    owner_role = make_url(owner_url).username or "shifde_owner"
    name = f"shifde_demo_{secrets.token_hex(6)}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        try:
            with admin.connect() as c:
                _create_database(c, name, owner_role, app_role, "template0")
        except SQLAlchemyError:
            raise DemoError(
                "the local cluster is not reachable: run python scripts/dev_db.py start"
            ) from None
        try:
            try:
                command.upgrade(alembic_config(_with_db(owner_url, name)), "heads")
            except Exception:
                raise DemoError("the scratch database could not be migrated") from None
            print(f"ok   scratch database {name} created and migrated")
            yield _with_db(app_url, name), _with_db(owner_url, name)
        finally:
            with contextlib.suppress(Exception), admin.connect() as c:
                _drop_database(c, name)
                print(f"ok   scratch database {name} dropped")
    finally:
        admin.dispose()


def drive(app_url: str, owner_url: str, workdir: Path) -> None:
    """The delivery flow, one printed line per step; `StepFailed` names the first failing one."""
    from fastapi.testclient import TestClient

    from shifde.app import create_app
    from shifde.core.db import Database
    from shifde.core.settings import Settings
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.routes import Flow, address, make_due, plans, register, target_body
    from tests.executor.support import (
        ask_plan,
        credentials_file,
        deploy_actions,
        fake_platform,
        make_runner,
    )
    from tests.tenancy.conftest import _client
    from tests.tenancy.routes import ws_slug

    def step(name: str, call: Callable[[], Any]) -> Any:
        try:
            result = call()
        except Exception:
            # the exception text may carry a response body: name the step only
            raise StepFailed(name) from None
        print(f"ok   {name}")
        return result

    def check(name: str, ok: bool) -> None:
        if not ok:
            raise StepFailed(name)

    settings = Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="WARNING",
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    try:
        with owner_db.session() as o:
            org = _client(o, "demo")
        requester = system_role_user(owner_db, org.tenant, "tenant_administrator")
        approver = system_role_user(owner_db, org.tenant, "tenant_administrator")
        print("ok   a fresh organization with a requester and a second tenant administrator")
        fake = fake_platform()
        with TestClient(
            create_app(settings), base_url="https://testserver", headers={"Origin": ORIGIN}
        ) as client:

            def register_target() -> Any:
                h = as_user(client, requester)
                made = register(client, h, org.slug, target_body(ws_slug(org), address()))
                client.cookies.clear()
                return made

            target = step("register a target", register_target)
            executor = step(
                "create the executor and issue its credential",
                lambda: make_executor(client, org, requester),
            )
            flow = Flow(
                client, org.slug, ws_slug(org), org.tenant, requester, approver, executor.token
            )
            runner = make_runner(
                client, executor.token, credentials_file(workdir, target["address"]), fake
            )

            plan = step("request a plan", lambda: ask_plan(flow, target["id"]))

            def seal() -> Any:
                assert runner.run_once()
                sealed = flow.get_plan(plan["id"])
                assert sealed["status"] == "sealed"
                return sealed

            sealed = step("the runner seals the plan", seal)

            def self_approval() -> None:
                h = as_user(client, requester)
                r = client.post(
                    f"{plans(flow.slug)}/{plan['id']}/approve",
                    json={"plan_digest": sealed["digest"]},
                    headers=h,
                )
                client.cookies.clear()
                assert r.status_code == 403 and r.json()["code"] == "self_approval_forbidden"

            step("the requester's self-approval is refused (403 self_approval_forbidden)", self_approval)
            approved = step("a second tenant administrator approves", lambda: flow.approve(sealed))
            deployment_id = approved["deployment_id"]
            check("a second tenant administrator approves", deployment_id is not None)

            def run_to(status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment_id)
                assert runner.run_once()
                assert flow.get(deployment_id)["status"] == status

            step("apply", lambda: run_to("observing", due=False))
            step("observe", lambda: run_to("verifying", due=True))
            step("verify", lambda: run_to("verified", due=True))

            body = flow.get(deployment_id)
            checks = {c["name"]: c["status"] for c in body["verification"]["checks"]}
            check(
                "both verify checks passed",
                checks == {"native-runtime": "passed", "runtime-warehouse": "passed"},
            )
            print("ok   deployment verified; native-runtime and runtime-warehouse passed")
            journal = body["journal"]
            check(
                "every receipt carries platform: fake",
                bool(journal)
                and all(s["receipt"]["platform"] == "fake" for s in journal)
                and body["receipt"]["operation_handles"]["platform"] == "fake"
                and (deploy_actions(fake, "deploy"), deploy_actions(fake, "run")) == (1, 1),
            )
            print(f"ok   {len(journal)} journal receipts and the deployment receipt are platform: fake")
    finally:
        owner_db.dispose()


def run_demo() -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    with scratch_database() as (app_url, owner_url), tempfile.TemporaryDirectory() as workdir:
        try:
            drive(app_url, owner_url, Path(workdir))
        except StepFailed as failed:
            print(f"FAIL {failed.step}")
            print(f"DELIVERY DEMO FAILED: {failed.step}")
            return 1
    print("DELIVERY DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="run against the fake platform")
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"DELIVERY DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"DELIVERY DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
