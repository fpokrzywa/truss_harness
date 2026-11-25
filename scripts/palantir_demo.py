"""Scripted Palantir delivery demo against the fake Foundry stack (SP8 design section 5, SP8-7).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient and the `shifde.executor`
runner over it with the fake platform. Nothing contacts a Foundry stack. Needs the local cluster
running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/palantir_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
In it organization A registers a Palantir target and seals a `palantir.marketplace/v1` release of
a fake model stack; a plan is refused (409 `evaluation_required`) until a real evaluation passes;
a target of the same stack with preview off refuses it (409 `palantir_preview_not_allowed`). The
plan seals, the requester's self-approval is refused, a second administrator approves, and the
runner applies, observes and verifies it; the verification holds `content-check: not_run`, because
the product content is not derived from the release. A second release lands on the same
installation, a mismatching frontend asset fails a third deployment, and organization B reaches
nothing of A's. The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `PALANTIR DEMO PASSED` (exit 0) or
`PALANTIR DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a token,
secret, password or database URL. Standard library only at the top level: when `shifde` is not
importable it re-runs itself once in the backend environment
(`uv run --frozen --project backend`).
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
FAKE_ONLY = "only the fake platform is supported (no Foundry stack is configured): run with --fake"
RERUN_FLAG = "SHIFDE_PALANTIR_DEMO_RERUN"
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
    """The Palantir delivery flow, one printed line per step; `StepFailed` names the first failing
    one."""
    import dataclasses

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from shifde.adapters.native.palantir.fake import FakeFoundryStack
    from shifde.app import create_app
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.executor.client import GatewayClient
    from shifde.executor.credentials import CredentialBroker
    from shifde.executor.runner import FakePlatform, Runner
    from shifde.packaging.schemas import PALANTIR_TARGET_KIND
    from tests._support.p5util import Hdr, thread_worker
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.palantir import CHECKS, DEPLOYABLE, palantir_target
    from tests.delivery.routes import (
        Flow,
        deployments,
        lease,
        make_due,
        plans,
        register,
        request_plan,
        sp8_address,
        sp8_body,
        targets,
    )
    from tests.evaluation.helpers import case, release, request, wait_scored
    from tests.executor.support import ask_plan, sp8_credentials_file
    from tests.identity import harness
    from tests.packaging.helpers import bindings, new_version, seal, version_body
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

    settings = Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="WARNING",
        # every step signs a person in afresh: more than the default login limits
        login_rate_per_account_per_minute=240,
        login_rate_per_minute=240,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    address = sp8_address("palantir")
    try:

        def people() -> tuple[Any, Any, str, str, str]:
            with owner_db.session() as o:
                a, b = _client(o, "demo"), _client(o, "other")
                for org in (a, b):
                    # the packaging helpers' project `main/sample`
                    main_ws = uuid7()
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
            requester = system_role_user(owner_db, a.tenant, "tenant_administrator")
            approver = system_role_user(owner_db, a.tenant, "tenant_administrator")
            return (
                a,
                b,
                requester,
                approver,
                system_role_user(owner_db, b.tenant, "tenant_administrator"),
            )

        org, other, requester, approver, b_admin = step(
            "1. organization A (a requester and a second tenant administrator) and organization B",
            people,
        )
        with contextlib.ExitStack() as stack:
            client = step(
                "1. start the API in-process",
                lambda: stack.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url="https://testserver",
                        headers={"Origin": ORIGIN},
                    )
                ),
            )
            executor = step(
                "1. create A's executor and issue its credential",
                lambda: make_executor(client, org, requester),
            )
            flow = Flow(
                client, org.slug, "main", org.tenant, requester, approver, executor.token, owner_db
            )
            target = step(
                "1. A registers a Palantir target with preview allowed",
                lambda: palantir_target(flow, address),
            )

            def start_runner() -> tuple[Any, Any]:
                foundry = FakeFoundryStack(
                    environment="test",
                    host=target["address"],
                    frontend_origin=target["bindings"]["frontend_origin"],
                    ontology_rid=target["bindings"]["ontology_rid"],
                    namespace_rid=target["bindings"]["namespace_rid"],
                )
                credentials = sp8_credentials_file(
                    workdir, "palantir", target["address"], tenant=org.tenant
                )
                gateway = GatewayClient("https://testserver", executor.token, http=client)
                me = gateway.whoami()
                return foundry, Runner(
                    gateway,
                    CredentialBroker.from_file(credentials),
                    me.executor_id,
                    FakePlatform(palantir=foundry, environment="test"),
                    tenant_id=me.tenant_id,
                )

            foundry, runner = step(
                "1. start the executor's runner against the fake Foundry stack", start_runner
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
                    return release(
                        hdr,
                        o,
                        cases,
                        org=f"/api/v1/orgs/{org.slug}",
                        org_slug=org.slug,
                        target_kind=PALANTIR_TARGET_KIND,
                    )

            rel = step(
                "1. a fake model stack and a sealed palantir.marketplace/v1 release of it",
                seal_release,
            )

            def refused(target_id: str, code: str) -> None:
                h = as_user(client, requester)
                body = request_plan(client, h, org.slug, target_id, rel.id, status=409)
                client.cookies.clear()
                need(body["code"] == code)

            step(
                "1. a plan of the release is refused (409 evaluation_required)",
                lambda: refused(target["id"], "evaluation_required"),
            )

            def evaluate(rel: Any) -> None:
                requested = request(rel)
                need(requested.status_code == 201)
                with thread_worker(settings, concurrency=3):
                    scored = wait_scored(rel, requested.json()["id"])
                need(scored["status"] == "passed")

            step(
                "1. an evaluation of the release runs to passed (in-process worker)",
                lambda: evaluate(rel),
            )

            def preview_off() -> None:
                second = palantir_target(
                    flow, address, bindings={**DEPLOYABLE, "allow_preview_apis": False}
                )
                refused(second["id"], "palantir_preview_not_allowed")

            step(
                "2. a plan on a second target of the stack with preview off is refused "
                "(409 palantir_preview_not_allowed)",
                preview_off,
            )

            def seal_plan(release_id: object) -> Any:
                plan = ask_plan(flow, target["id"], release_id=release_id)
                need(runner.run_once())
                sealed = flow.get_plan(plan["id"])
                need(sealed["status"] == "sealed")
                return sealed

            sealed = step("3. the runner seals a plan of the release", lambda: seal_plan(rel.id))

            def self_approval() -> None:
                h = as_user(client, requester)
                r = client.post(
                    f"{plans(flow.slug)}/{sealed['id']}/approve",
                    json={"plan_digest": sealed["digest"]},
                    headers=h,
                )
                client.cookies.clear()
                need(r.status_code == 403 and r.json()["code"] == "self_approval_forbidden")

            step(
                "3. the requester's self-approval is refused (403 self_approval_forbidden)",
                self_approval,
            )

            def approve(value: Any) -> str:
                made = flow.approve(value)["deployment_id"]
                need(made is not None)
                return str(made)

            first = step("3. a second tenant administrator approves", lambda: approve(sealed))

            def run_to(deployment: str, status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment)
                need(runner.run_once())
                need(flow.get(deployment)["status"] == status)

            step("3. apply", lambda: run_to(first, "observing", due=False))
            step("3. observe", lambda: run_to(first, "verifying", due=True))
            step("3. verify: verified", lambda: run_to(first, "verified", due=True))

            def checks(deployment: str) -> dict[str, str]:
                stored = flow.get(deployment)["verification"]["checks"]
                return {c["name"]: c["status"] for c in stored}

            passed = {**dict.fromkeys(CHECKS, "passed"), "content-check": "not_run"}

            def content_check() -> str:
                stored = checks(first)
                need(stored == passed)
                return stored["content-check"]

            status = step(
                "4. native-version, ontology-read and frontend-asset passed in the stored "
                "verification",
                content_check,
            )
            print(f"ok   content-check: {status}")

            def receipts() -> None:
                body = flow.get(first)
                need(len(body["journal"]) == 3)
                need(all(s["receipt"]["platform"] == "fake" for s in body["journal"]))
                need(body["receipt"]["operation_handles"]["platform"] == "fake")

            step("4. every journal receipt and the deployment receipt are platform: fake", receipts)

            def second_release() -> Any:
                h = rel.h
                body = version_body(rel.stack.agent_v1, dataset=rel.dataset["id"])
                body["evaluation"].update(max_p95_latency_ms=600_000, max_total_cost_usd="1")
                version = new_version(h.client, h, rel.solution, body, rel.org)["id"]
                bound = bindings({rel.stack.model: rel.stack.provider})
                made = seal(h.client, h, rel.solution, version, bound, rel.org)
                need(made.status_code == 201)
                return dataclasses.replace(
                    rel, version=version, id=made.json()["id"], digest=made.json()["digest"]
                )

            later = step("5. A seals a second release", second_release)
            step("5. its evaluation runs to passed", lambda: evaluate(later))

            def deploy(release_id: object) -> str:
                deployment = approve(seal_plan(release_id))
                run_to(deployment, "observing", due=False)
                run_to(deployment, "verifying", due=True)
                return deployment

            def same_installation() -> None:
                deployment = deploy(later.id)
                run_to(deployment, "verified", due=True)
                need(checks(deployment) == passed)
                need(len(foundry.installations) == 1 and len(foundry.jobs) == 2)

            step(
                "5. the second release is planned, approved, applied on the same installation and "
                "verified",
                same_installation,
            )

            def mismatch() -> None:
                deployment = deploy(later.id)
                foundry.asset_mismatch = True
                run_to(deployment, "failed", due=True)
                foundry.asset_mismatch = False
                need(flow.get(deployment)["status_reason"] == "verification failed")
                need(checks(deployment) == {**passed, "frontend-asset": "failed"})

            step(
                "6. with another frontend asset a deployment is failed (verification failed), "
                "frontend-asset: failed",
                mismatch,
            )

            def b_claim() -> None:
                h = as_user(client, b_admin)
                body = register(
                    client, h, other.slug, sp8_body("palantir", "main", address), status=409
                )
                client.cookies.clear()
                need(body["code"] == "platform_already_registered")

            step("7. B registering A's stack is refused (409 platform_already_registered)", b_claim)

            def b_404s() -> None:
                h = as_user(client, b_admin)
                for base, item in (
                    (targets, target["id"]),
                    (plans, sealed["id"]),
                    (deployments, first),
                ):
                    for slug in (other.slug, org.slug):
                        need(client.get(f"{base(slug)}/{item}", headers=h).status_code == 404)
                client.cookies.clear()

            step("7. B gets 404 on A's target, plan and deployment", b_404s)

            def b_leases_nothing() -> None:
                ask_plan(flow, target["id"], release_id=later.id)
                b_executor = make_executor(client, other, b_admin)
                need(lease(client, b_executor.token).status_code == 204)
                need(runner.run_once())  # A's own executor leases the job

            step("7. B's executor leases none of A's jobs", b_leases_nothing)
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
        print(f"PALANTIR DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("PALANTIR DEMO FAILED: unexpected error")
        return 1
    print("PALANTIR DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="run against the fake platform")
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"PALANTIR DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"PALANTIR DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
