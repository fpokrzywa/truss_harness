"""Scripted Fabric delivery demo of sub-project 8 (design SP8 section 4) against the in-memory
Fabric fake.

Runs in-process: the API (`shifde.app.create_app`) in a TestClient and the `shifde.executor` runner
over it, its platform chosen as the executor chooses it (`--fake-platform`, `SHIFDE_ENV=test`,
`SHIFDE_DEV_FAKE_DATA_PLATFORMS=true`). Nothing contacts Fabric or Entra. Needs the local cluster
running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/fabric_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
In it client A registers a Fabric target and seals a `/1` `fabric.solution/v1` release whose plan is
refused (409 `evaluation_required`); an evaluation runs to `passed` with an in-process worker, the
plan is sealed, the requester's self-approval is refused (403 `self_approval_forbidden`), a second
tenant administrator approves, and the runner applies, observes and verifies the deployment. A
second release updates the notebook definition of the same items and is verified too. On a second
target in another workspace an item of a bound name refuses the plan (`foreign_item`). Client B
registering A's workspace gets `platform_already_registered` and 404 on A's target, plan and
deployment, and B's executor leases nothing while A's job is due. The test helpers under
`backend/tests/` drive each step.

Prints one line per step and ends with `FABRIC DEMO PASSED` (exit 0) or
`FABRIC DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run (`FABRIC DEMO NOT RUN:
<reason>`). It never prints a token, secret, password, cookie, database URL or response body.
Standard library only at the top level: when `shifde` is not importable it re-runs itself once in
the backend environment (`uv run --frozen --project backend`).
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
    "only the fake platform is supported (no Fabric workspace or Entra tenant is configured): "
    "run with --fake"
)
RERUN_FLAG = "SHIFDE_FABRIC_DEMO_RERUN"
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
    """The Fabric flow, one printed line per step; `StepFailed` names the first failing one."""
    import json
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
    from shifde.app import create_app
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.executor import main as executor_main
    from shifde.executor.client import GatewayClient
    from shifde.executor.runner import FakePlatform, Runner
    from tests._support.p5util import Hdr, thread_worker
    from tests._support.p8util import rate_card, rate_version
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.routes import (
        Flow,
        deployments,
        make_due,
        plan_row,
        plans,
        register,
        request_plan,
        sp8_address,
        sp8_body,
        targets,
    )
    from tests.evaluation.helpers import (
        SERVED,
        Release,
        capture,
        case,
        request,
        served_model,
        wait_scored,
    )
    from tests.executor.support import ask_plan, sp8_credentials_file, sp8_entry
    from tests.identity import harness
    from tests.ledger.util import add_budget
    from tests.packaging.helpers import (
        bindings,
        dataset,
        new_version,
        seal,
        solution,
        stack,
        version_body,
    )
    from tests.tenancy.conftest import _client
    from tests.tenancy.routes import ws_slug

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
        # every step signs a person in afresh: more than the default rates a minute
        login_rate_per_account_per_minute=240,
        login_rate_per_minute=240,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    try:

        def people() -> tuple[Any, Any, str, str, str]:
            with owner_db.session() as o:
                a, b = _client(o, "demo-a"), _client(o, "demo-b")
                # the packaging helpers' project `main/sample`
                main_ws = uuid7()
                o.execute(
                    text(
                        "INSERT INTO shifde.workspaces (id, tenant_id, slug, name) "
                        "VALUES (:w, :t, 'main', 'Main')"
                    ),
                    {"w": main_ws, "t": a.tenant},
                )
                o.execute(
                    text(
                        "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                        "VALUES (:p, :t, :w, 'sample', 'Sample')"
                    ),
                    {"p": uuid7(), "t": a.tenant, "w": main_ws},
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

        a, b, requester, approver, b_admin = step(
            "client A with a requester and a second tenant administrator, and client B", people
        )
        base = f"/api/v1/orgs/{a.slug}"
        with contextlib.ExitStack() as exits:
            client = step(
                "start the API in-process",
                lambda: exits.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url="https://testserver",
                        headers={"Origin": ORIGIN},
                    )
                ),
            )

            def register_targets() -> tuple[Any, Any]:
                h = as_user(client, requester)
                first = register(
                    client, h, a.slug, sp8_body("fabric", "main", sp8_address("fabric"))
                )
                second = register(
                    client,
                    h,
                    a.slug,
                    sp8_body(
                        "fabric", "main", sp8_address("fabric"), credential_ref="fabric-second"
                    ),
                )
                client.cookies.clear()
                return first, second

            target, second_target = step(
                "client A registers a Fabric target (and a second one in another workspace)",
                register_targets,
            )
            workspace = target["address"].rsplit("/", 1)[1]
            second_workspace = second_target["address"].rsplit("/", 1)[1]
            fake = FakeFabricWorkspace()

            def start_runner(org: Any, token: str, creds: Path) -> Any:
                config = executor_main.configure(
                    ["--fake-platform"],
                    {
                        executor_main.API_URL: "https://testserver",
                        executor_main.TOKEN: token,
                        executor_main.CREDENTIALS_FILE: str(creds),
                        executor_main.FAKE_DATA_PLATFORMS: "true",
                        "SHIFDE_ENV": "test",
                    },
                )
                need(isinstance(config.platform, FakePlatform))
                config.platform.fabric = fake  # one fake service, as one Fabric serves both
                gateway = GatewayClient("https://testserver", token, http=client)
                me = gateway.whoami()
                need(me.tenant_id == org.tenant)
                return Runner(
                    gateway, config.broker, me.executor_id, config.platform, tenant_id=me.tenant_id
                )

            def a_executor() -> Any:
                executor = make_executor(client, a, requester)
                directory = workdir / "a"
                directory.mkdir()
                entries = {
                    made["credential_ref"]: sp8_entry(
                        directory, "fabric", made["address"], tenant=a.tenant
                    )
                    for made in (target, second_target)
                }
                creds = directory / "credentials.json"
                creds.write_text(json.dumps(entries), encoding="utf-8")
                creds.chmod(0o600)
                return executor.token, start_runner(a, executor.token, creds)

            token, runner = step(
                "client A's executor and its runner against the switched-on Fabric fake", a_executor
            )
            flow = Flow(client, a.slug, "main", a.tenant, requester, approver, token, owner_db)

            def b_executor() -> Any:
                executor = make_executor(client, b, b_admin)
                directory = workdir / "b"
                directory.mkdir()
                creds = sp8_credentials_file(
                    directory, "fabric", sp8_address("fabric"), tenant=b.tenant
                )
                return start_runner(b, executor.token, creds)

            b_runner = step("client B's executor and its runner", b_executor)

            def seal_release() -> Any:
                with owner_db.session() as o:
                    add_budget(o, a.tenant, amount="1000000")
                    o.commit()
                own = TestClient(
                    client.app, base_url="https://testserver", headers={"Origin": ORIGIN}
                )
                hdr = Hdr(harness.login(own, requester))
                hdr.client = own
                s = stack(own, hdr, base)
                model = served_model(own, hdr, s, base)
                card = rate_card(own, hdr, model_key=SERVED, base=base)
                rate_version(
                    own, hdr, card["id"], base=base, currency="USD", input_rate=1, output_rate=1
                )
                cases = [
                    case("hello", "hello there", ("contains", "hello there")),
                    case("bye", "bye now", ("contains", "bye now")),
                ]
                golden = dataset(own, hdr, "Fabric-cases", org=base, cases=cases)
                sid = solution(own, hdr, "Fabric", base, "fabric.solution/v1")["id"]
                with owner_db.session() as o:
                    capture(o, a.slug, "full")
                return version(hdr, s, model, golden, sid, "0.9")

            def version(
                hdr: Any, s: Any, model: str, golden: Any, sid: str, min_accuracy: str
            ) -> Any:
                body = version_body(s.agent_v1, dataset=golden["id"], min_accuracy=min_accuracy)
                body["evaluation"].update(max_p95_latency_ms=600_000, max_total_cost_usd="1")
                vid = new_version(hdr.client, hdr, sid, body, base)["id"]
                sealed = seal(hdr.client, hdr, sid, vid, bindings({s.model: s.provider}), base)
                need(sealed.status_code == 201)
                made = sealed.json()
                need(made["manifest"]["compiler_version"] == "shifde.packaging/1")
                need(made["manifest"]["target_kind"] == "fabric.solution/v1")
                return Release(hdr, s, model, golden, sid, vid, made["id"], made["digest"], base)

            rel = step("a sealed /1 fabric.solution/v1 release", seal_release)

            def refused(release_id: Any) -> None:
                h = as_user(client, requester)
                body = request_plan(client, h, a.slug, target["id"], release_id, status=409)
                client.cookies.clear()
                need(body["code"] == "evaluation_required")

            step(
                "its plan is refused before any evaluation (409 evaluation_required)",
                lambda: refused(rel.id),
            )

            def evaluate(which: Any) -> None:
                requested = request(which)
                need(requested.status_code == 201)
                with thread_worker(settings, concurrency=3):
                    scored = wait_scored(which, requested.json()["id"])
                need(scored["status"] == "passed")

            step("its evaluation runs to passed (in-process worker)", lambda: evaluate(rel))

            def seal_plan(which: Any) -> Any:
                plan = ask_plan(flow, target["id"], release_id=which.id)
                before = len(fake.writes)
                need(runner.run_once())
                sealed = flow.get_plan(plan["id"])
                need(sealed["status"] == "sealed" and len(fake.writes) == before)
                return sealed

            sealed = step("the plan is accepted and the runner seals it", lambda: seal_plan(rel))
            first_steps = [s["key"] for s in sealed["steps"]]
            step(
                "the plan has the create steps, the verification job and the terminal step",
                lambda: need(
                    first_steps
                    == [
                        "lakehouse-metadata-submit",
                        "lakehouse-metadata-complete",
                        "notebook-metadata-submit",
                        "notebook-metadata-complete",
                        "fabric-verification-submit",
                        "fabric-finalize",
                    ]
                ),
            )

            def self_approval() -> None:
                h = as_user(client, requester)
                r = client.post(
                    f"{plans(a.slug)}/{sealed['id']}/approve",
                    json={"plan_digest": sealed["digest"]},
                    headers=h,
                )
                client.cookies.clear()
                need(r.status_code == 403 and r.json()["code"] == "self_approval_forbidden")

            step(
                "the requester's self-approval is refused (403 self_approval_forbidden)",
                self_approval,
            )

            def approve(plan: Any) -> str:
                deployment = flow.approve(plan)["deployment_id"]
                need(deployment is not None)
                return str(deployment)

            deployment = step("a second tenant administrator approves", lambda: approve(sealed))

            def run_to(which: str, status: str) -> None:
                make_due(owner_db, which)
                need(runner.run_once())
                need(flow.get(which)["status"] == status)

            step("apply", lambda: run_to(deployment, "observing"))
            step("observe", lambda: run_to(deployment, "verifying"))
            step("verify: the deployment is verified", lambda: run_to(deployment, "verified"))

            def proof() -> dict[str, str]:
                body = flow.get(deployment)
                checks = {c["name"]: c["status"] for c in body["verification"]["checks"]}
                need(checks == {"native-items": "passed", "runtime-health": "passed"})
                need([s["step_key"] for s in body["journal"]] == first_steps)
                need(all(s["receipt"]["platform"] == "fake" for s in body["journal"]))
                need(body["receipt"]["operation_handles"]["platform"] == "fake")
                return {r["key"]: r["native_id"] for r in body["receipt"]["resources"]}

            items = step(
                "native-items and runtime-health passed; every receipt is platform: fake", proof
            )

            def second_release() -> None:
                again = version(rel.h, rel.stack, rel.served, rel.dataset, rel.solution, "0.8")
                need(again.digest != rel.digest)
                evaluate(again)
                plan = seal_plan(again)
                keys = [s["key"] for s in plan["steps"]]
                need(
                    "notebook-definition-submit" in keys and "notebook-definition-complete" in keys
                )
                which = approve(plan)
                for status in ("observing", "verifying", "verified"):
                    run_to(which, status)
                receipt = flow.get(which)["receipt"]
                need({r["key"]: r["native_id"] for r in receipt["resources"]} == items)
                need(receipt["operation_handles"]["release_digest"] == again.digest)
                need(len(fake.items[workspace]) == 2)

            step(
                "a second release updates the notebook definition of the same items and is "
                "verified",
                second_release,
            )

            def foreign() -> None:
                fake.add_workspace(second_workspace)
                fake.seed_foreign_item(
                    second_workspace, "Notebook", second_target["bindings"]["notebook_name"]
                )
                writes = len(fake.writes)
                plan = ask_plan(flow, second_target["id"], release_id=rel.id)
                need(runner.run_once())
                row = plan_row(owner_db, plan["id"])
                need(row["status"] == "plan_failed")
                need(row["status_reason"] == "plan job failed: foreign_item")
                need(len(fake.writes) == writes)

            step(
                "a foreign item of a bound name refuses a plan on the second target "
                "(foreign_item, no write)",
                foreign,
            )

            def b_registers() -> None:
                for form in (workspace, workspace.upper(), target["address"]):
                    h = as_user(client, b_admin)
                    body = sp8_body("fabric", ws_slug(b), form)
                    refusal = register(client, h, b.slug, body, status=409)
                    client.cookies.clear()
                    need(refusal["code"] == "platform_already_registered")
                    need(workspace not in json.dumps(refusal))

            step(
                "client B registering A's workspace is refused (409 platform_already_registered)",
                b_registers,
            )

            def b_reads() -> None:
                h = as_user(client, b_admin)
                for url, found in (
                    (targets(b.slug), target["id"]),
                    (plans(b.slug), sealed["id"]),
                    (deployments(b.slug), deployment),
                ):
                    named = client.get(f"{url}/{found}", headers=h)
                    missing = client.get(f"{url}/{uuid4()}", headers=h)
                    need(named.status_code == missing.status_code == 404)
                    need(named.json()["detail"] == missing.json()["detail"])
                client.cookies.clear()

            step("client B gets 404 on A's target, plan and deployment", b_reads)

            def b_leases() -> None:
                plan = ask_plan(flow, target["id"], release_id=rel.id)  # A's plan job is due
                need(not b_runner.run_once())
                need(plan_row(owner_db, plan["id"])["status"] == "requested")
                need(runner.run_once())
                need(flow.get_plan(plan["id"])["status"] == "sealed")

            step("client B's executor leases nothing of A's while A's job is due", b_leases)
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
        print(f"FABRIC DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("FABRIC DEMO FAILED: unexpected error")
        return 1
    print("FABRIC DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="run against the in-memory Fabric fake")
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"FABRIC DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"FABRIC DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
