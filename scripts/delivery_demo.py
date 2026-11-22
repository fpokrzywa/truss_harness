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
credential; sets up a fake model stack (content capture `full`) and seals a release of a solution
version with an evaluation dataset; a plan of that release is refused (409
`evaluation_required`); the requester runs a real evaluation of the release to `passed` with an
in-process worker, then asks for a plan of it; the runner seals it; the requester cannot approve
it and a second tenant administrator does; the runner then applies, observes and verifies the
deployment. The test helpers under `backend/tests/` drive each step.

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
    "only the fake platform is supported (no Databricks workspace is configured): run with --fake"
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
            try:
                with admin.connect() as c:
                    _drop_database(c, name)
            except Exception:
                dropped = False
                print(f"FAIL scratch database {name} not dropped")
            else:
                dropped = True
                print(f"ok   scratch database {name} dropped")
        # reached only when the demo itself ended without an exception
        if not dropped:
            raise StepFailed("drop the scratch database")
    finally:
        admin.dispose()


def drive(app_url: str, owner_url: str, workdir: Path) -> None:
    """The delivery flow, one printed line per step; `StepFailed` names the first failing one."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from shifde.app import create_app
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from tests._support.p5util import Hdr, thread_worker
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.routes import (
        Flow,
        address,
        make_due,
        plans,
        register,
        request_plan,
        target_body,
    )
    from tests.evaluation.helpers import case, release, request, wait_scored
    from tests.executor.support import (
        ask_plan,
        credentials_file,
        deploy_actions,
        fake_platform,
        make_runner,
    )
    from tests.identity import harness
    from tests.tenancy.conftest import _client

    def step(name: str, call: Callable[[], Any]) -> Any:
        try:
            result = call()
        except Exception:
            # the exception text may carry a response body: name the step only
            print(f"FAIL {name}")
            raise StepFailed(name) from None
        print(f"ok   {name}")
        return result

    def need(ok: bool) -> None:
        """An explicit check inside a step (kept under `python -O`, unlike `assert`)."""
        if not ok:
            raise ValueError("check failed")

    def check(name: str, ok: bool) -> None:
        if not ok:
            print(f"FAIL {name}")
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
        # every step signs the requester in afresh: more than the default 10 a minute
        login_rate_per_account_per_minute=60,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    main_ws = uuid7()
    try:

        def people() -> tuple[Any, str, str]:
            with owner_db.session() as o:
                org = _client(o, "demo")
                # the packaging helpers' project `main/sample`
                o.execute(
                    text(
                        "INSERT INTO shifde.workspaces (id, tenant_id, slug, name) "
                        "VALUES (:w, :t, 'main', 'Main')"
                    ),
                    {"w": main_ws, "t": org.tenant},
                )
                o.execute(
                    text(
                        "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                        "VALUES (:p, :t, :w, 'sample', 'Sample')"
                    ),
                    {"p": uuid7(), "t": org.tenant, "w": main_ws},
                )
                o.commit()
            requester = system_role_user(owner_db, org.tenant, "tenant_administrator")
            approver = system_role_user(owner_db, org.tenant, "tenant_administrator")
            return org, requester, approver

        org, requester, approver = step(
            "a fresh organization with a requester and a second tenant administrator", people
        )
        with contextlib.ExitStack() as stack:

            def start() -> tuple[Any, Any]:
                fake = fake_platform()
                client = stack.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url="https://testserver",
                        headers={"Origin": ORIGIN},
                    )
                )
                return fake, client

            fake, client = step("start the API and the fake platform in-process", start)

            def register_target() -> Any:
                h = as_user(client, requester)
                made = register(client, h, org.slug, target_body("main", address()))
                client.cookies.clear()
                return made

            target = step("register a target", register_target)
            executor = step(
                "create the executor and issue its credential",
                lambda: make_executor(client, org, requester),
            )
            flow = Flow(
                client, org.slug, "main", org.tenant, requester, approver, executor.token, owner_db
            )
            runner = step(
                "start the executor's runner against the fake platform",
                lambda: make_runner(
                    client,
                    executor.token,
                    credentials_file(workdir, target["address"], tenant=org.tenant),
                    fake,
                ),
            )

            def seal_release() -> Any:
                own = TestClient(
                    client.app, base_url="https://testserver", headers={"Origin": ORIGIN}
                )
                hdr = Hdr(harness.login(own, requester))
                hdr.client = own
                cases = [
                    case("hello", "hello there", ("contains", "hello there")),
                    case("bye", "bye now", ("contains", "bye now")),
                ]
                with owner_db.session() as o:
                    return release(hdr, o, cases, org=f"/api/v1/orgs/{org.slug}", org_slug=org.slug)

            rel = step(
                "a fake model stack (capture full) and a sealed release of a solution version",
                seal_release,
            )

            def refused() -> None:
                h = as_user(client, requester)
                body = request_plan(client, h, org.slug, target["id"], rel.id, status=409)
                client.cookies.clear()
                need(body["code"] == "evaluation_required")

            step("a plan of the release is refused (409 evaluation_required)", refused)

            def evaluate() -> None:
                requested = request(rel)
                need(requested.status_code == 201)
                with thread_worker(settings, concurrency=3):
                    scored = wait_scored(rel, requested.json()["id"])
                need(scored["status"] == "passed")

            step("an evaluation of the release runs to passed (in-process worker)", evaluate)

            plan = step(
                "request a plan of the release",
                lambda: ask_plan(flow, target["id"], release_id=rel.id),
            )

            def seal() -> Any:
                need(runner.run_once())
                sealed = flow.get_plan(plan["id"])
                need(sealed["status"] == "sealed")
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
                need(r.status_code == 403 and r.json()["code"] == "self_approval_forbidden")

            step(
                "the requester's self-approval is refused (403 self_approval_forbidden)",
                self_approval,
            )
            approved = step("a second tenant administrator approves", lambda: flow.approve(sealed))
            deployment_id = approved["deployment_id"]
            check("a second tenant administrator approves", deployment_id is not None)

            def run_to(status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment_id)
                need(runner.run_once())
                need(flow.get(deployment_id)["status"] == status)

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
            print(
                f"ok   {len(journal)} journal receipts and the deployment receipt are "
                "platform: fake"
            )
    finally:
        owner_db.dispose()


def run_demo() -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    try:
        with (
            scratch_database() as (app_url, owner_url),
            tempfile.TemporaryDirectory() as workdir,
        ):
            drive(app_url, owner_url, Path(workdir))
    except StepFailed as failed:
        print(f"DELIVERY DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("DELIVERY DEMO FAILED: unexpected error")
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
