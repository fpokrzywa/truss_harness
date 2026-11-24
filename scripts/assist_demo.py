"""Scripted assist demo of sub-project 7's "done when" against the fake model provider and the
native fake platform (design SP7 section 12, steps 1 to 7; step 8, the board, is SP7-8).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient, a worker on a thread with
`shifde.worker_main.job_handlers()` and `shifde.worker_main.internal_tools()`, and the
`shifde.executor` runner over the native fake platform. Every model is the `fake` provider
scripted with `tests/_support/assist_fixtures.py` (`ROUTER_SCRIPT` and scripted specialist tool
calls); nothing opens a socket. Needs the local cluster running (`python scripts/dev_db.py
start`) and this checkout's `.devdata/dev.env` (`python scripts/dev_db.py env`).

    python scripts/assist_demo.py --fake [--through N]

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
1. Organizations A (project P1, `main/sample`, an FDE and a second tenant administrator) and B
   (project Q1, `main/sample`): a sealed, evaluated release in each; A's deployed and `verified`
   on the native fake; B's with an `approved` open plan and one earlier `failed` deployment
   (B's rows written as the schema owner, as the board tests do).
2. Each organization's administrator enables assist; the specialists are installed in P1, Q1 and
   A's P2 (`main/other`) with a scripted `fake` model.
3. In A, six requests (five rule families and one with a release as its context) are each routed
   by rule to the expected specialist; each run completes with an answer; the deployment run's
   `seth.board.read` result names A's release.
4. In A, an ambiguous request with model routing off is refused `route_ambiguous`; with routing on
   (`ROUTER_SCRIPT`) it is routed by `model` to `evaluation`, with one `model_call` usage row.
5. The deployment specialist proposes a plan; the FDE accepts it (a `requested` plan); the FDE's
   approval is refused (403); a second administrator approves; the deployment is `verified`.
6. A viewer's deployment request is refused `specialist_not_permitted`.
7. A's specialist run scripted to read B's release id gets `not_found`.
`--through N` (1..7) stops after step N. The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `ASSIST DEMO PASSED` (exit 0) or `ASSIST DEMO FAILED:
<step>` (exit 1). Exit 2: the demo could not run. It never prints a token, password, cookie,
database URL or response body. Standard library only at the top level: when `shifde` is not
importable it re-runs itself once in the backend environment (`uv run --frozen --project
backend`).
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
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FAKE_ONLY = (
    "only the fake model provider and the native fake platform are supported (no model "
    "provider or platform is configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_ASSIST_DEMO_RERUN"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)
API_ORIGIN = "https://testserver"
STEPS = 7  # design section 12 steps 1-7; step 8 is SP7-8


class DemoError(Exception):
    """The demo could not run (no cluster, no dev.env, no backend environment)."""


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


class Done(Exception):
    """`--through N` reached: the remaining steps are not run."""


def _in_backend_env() -> bool:
    try:
        import shifde  # noqa: F401
    except ImportError:
        return False
    return True


def rerun_in_backend(through: int) -> int:
    """Run this script once more under `uv run` in the backend environment; its exit code."""
    if os.environ.get(RERUN_FLAG):
        raise DemoError("the backend environment has no shifde package: run uv sync in backend")
    uv = shutil.which("uv")
    if not uv:
        raise DemoError("uv is not installed: the demo needs the backend environment")
    env = {**os.environ, "UV_LINK_MODE": "copy", RERUN_FLAG: "1"}
    command = [uv, "run", "--frozen", "--project", str(BACKEND)]
    command += ["python", str(Path(__file__).resolve()), "--fake", "--through", str(through)]
    # fixed arguments: uv, this script, --fake and a step number checked by argparse
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode  # noqa: S603


@contextlib.contextmanager
def scratch_database() -> Iterator[tuple[str, str]]:
    """(app_url, owner_url) of a migrated `shifde_demo_<hex>` database, dropped afterwards."""
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool
    from tests.conftest import _create_database, _drop_database, _env_or_none, _with_db

    from shifde.core.migrations import alembic_config

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


def drive(app_url: str, owner_url: str, workdir: Path, through: int) -> None:
    """The assist flow, one printed line per step; `StepFailed` names the first failing one and
    `Done` ends it after step `through`."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from tests._support.assist_fixtures import PICKS_EVALUATION, specialist_model, tool_results
    from tests._support.p3util import fake_model_stack
    from tests._support.p5util import Hdr, fast, wait_for
    from tests.board.conftest import deployment as board_deployment
    from tests.board.conftest import plan as board_plan
    from tests.board.conftest import target as board_target
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, grant, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.releases import make_release
    from tests.delivery.routes import Flow, address, make_due, plans, register, target_body
    from tests.evaluation.helpers import case, release, request, wait_scored
    from tests.executor.support import ask_plan, credentials_file, fake_platform, make_runner
    from tests.identity import harness
    from tests.tenancy.conftest import _client

    from shifde import worker_main
    from shifde.app import create_app
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.runtime.worker import Worker

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

    def done_with(number: int) -> None:
        if number >= through:
            raise Done

    settings = Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="WARNING",
        # every step signs a person in afresh: more than the default 10 a minute
        login_rate_per_account_per_minute=120,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    try:

        def project(o: Any, tenant: Any, ws: Any, slug: str) -> Any:
            pid = uuid7()
            o.execute(
                text(
                    "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                    "VALUES (:p, :t, :w, :s, :s)"
                ),
                {"p": pid, "t": tenant, "w": ws, "s": slug},
            )
            return pid

        def people() -> dict[str, Any]:
            out: dict[str, Any] = {}
            with owner_db.session() as o:
                for side in ("a", "b"):
                    org = _client(o, f"assist-{side}")
                    ws = uuid7()
                    o.execute(
                        text(
                            "INSERT INTO shifde.workspaces (id, tenant_id, slug, name) "
                            "VALUES (:w, :t, 'main', 'Main')"
                        ),
                        {"w": ws, "t": org.tenant},
                    )
                    out[side] = {
                        "org": org,
                        "ws": ws,
                        "sample": project(o, org.tenant, ws, "sample"),
                    }
                out["a"]["other"] = project(o, out["a"]["org"].tenant, out["a"]["ws"], "other")
                o.commit()
            for side in ("a", "b"):
                out[side]["admin"] = system_role_user(
                    owner_db, out[side]["org"].tenant, "tenant_administrator"
                )
            out["a"]["approver"] = system_role_user(
                owner_db, out["a"]["org"].tenant, "tenant_administrator"
            )
            return out

        world = step(
            "1. organizations A (P1 main/sample, P2 main/other, an FDE and a second tenant "
            "administrator) and B (Q1 main/sample)",
            people,
        )
        a, b = world["a"], world["b"]
        base_a, base_b = f"/api/v1/orgs/{a['org'].slug}", f"/api/v1/orgs/{b['org'].slug}"
        fde_email = a["admin"]
        with contextlib.ExitStack() as stack:

            def start() -> tuple[Any, Any]:
                native = fake_platform()
                client = stack.enter_context(
                    TestClient(
                        create_app(settings), base_url=API_ORIGIN, headers={"Origin": ORIGIN}
                    )
                )
                app: Any = client.app
                worker = Worker(
                    fast(settings),
                    worker_id=f"assist-demo-{time.monotonic_ns()}",
                    concurrency=3,
                    lease_seconds=5,
                    poll_ms=100,
                    reap_seconds=0.3,
                    runtime=app.state.integration,
                    job_handlers=worker_main.job_handlers(),
                    internal_tools=worker_main.internal_tools(),
                )
                thread = threading.Thread(target=worker.run, daemon=True)
                thread.start()

                def stop() -> None:
                    worker.request_stop()
                    thread.join(timeout=30)

                stack.callback(stop)
                return native, client

            native, client = step(
                "1. start the API, the worker (job_handlers and internal_tools of "
                "shifde.worker_main) and the native fake platform in-process",
                start,
            )

            def session(email: str) -> Hdr:
                own = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
                hdr = Hdr(harness.login(own, email))
                hdr.client = own
                return hdr

            fde, b_admin = session(fde_email), session(b["admin"])

            def target_a() -> Any:
                h = as_user(client, fde_email)
                body = target_body("main", address(), project="sample")
                made = register(client, h, a["org"].slug, body)
                client.cookies.clear()
                return made

            target = step("1. A registers a target in P1", target_a)
            executor = step(
                "1. A's executor with its credential",
                lambda: make_executor(client, a["org"], fde_email),
            )
            flow = Flow(client, a["org"].slug, "main", a["org"].tenant, fde_email, a["approver"],
                        executor.token, owner_db)  # fmt: skip
            runner = step(
                "1. start the executor's runner against the native fake platform",
                lambda: make_runner(
                    client,
                    executor.token,
                    credentials_file(workdir, target["address"], tenant=a["org"].tenant),
                    native,
                ),
            )

            def seal_release() -> Any:
                cases = [case("hello", "hello there", ("contains", "hello there"))]
                with owner_db.session() as o:
                    return release(fde, o, cases, org=base_a, org_slug=a["org"].slug)

            rel = step("1. A seals a release of a solution version in P1", seal_release)

            def evaluate() -> None:
                requested = request(rel)
                need(requested.status_code == 201)
                need(wait_scored(rel, requested.json()["id"], 120.0)["status"] == "passed")

            step("1. an evaluation of A's release runs to passed", evaluate)

            def deploy(application_key: str, plan_id: Any = None) -> Any:
                made = plan_id or ask_plan(flow, target["id"], application_key, rel.id)["id"]
                need(runner.run_once())
                sealed = flow.get_plan(made)
                need(sealed["status"] == "sealed")
                deployment_id = flow.approve(sealed)["deployment_id"]
                need(runner.run_once())
                need(flow.get(deployment_id)["status"] == "observing")
                for status in ("verifying", "verified"):
                    make_due(owner_db, deployment_id)
                    need(runner.run_once())
                    need(flow.get(deployment_id)["status"] == status)
                return deployment_id

            step("1. A's release is planned, approved, applied and verified on the native fake",
                 lambda: deploy("sales-app"))  # fmt: skip

            def b_rows() -> dict[str, Any]:
                tenant = b["org"].tenant
                sealed = make_release(owner_db, tenant, b["ws"], b["sample"])
                tid = board_target(owner_db, tenant, b["ws"], b["sample"])
                earlier = board_plan(owner_db, tenant, tid, sealed.id, "approved")
                board_deployment(owner_db, tenant, earlier, tid, "failed", "apply_failed")
                board_plan(owner_db, tenant, tid, sealed.id, "approved")
                return {"release": sealed.id, "target": tid}

            b_side = step(
                "1. B's sealed, evaluated release in Q1 with an approved open plan and an earlier "
                "failed deployment",
                b_rows,
            )
            done_with(1)

            # ------------------------------------------------------------------ step 2
            board_marker, plan_marker, foreign_marker = "[[demo-k1]]", "[[demo-k2]]", "[[demo-k3]]"
            plan_payload = {
                "target_id": target["id"],
                "release_id": rel.id,
                "application_key": "sales-assist",
                "display_name": "Sales assist",
            }
            script = {
                board_marker: [("seth.board.read", {})],
                plan_marker: [("seth.propose.delivery_plan", plan_payload)],
                foreign_marker: [("seth.releases.read", {"release_id": str(b_side["release"])})],
            }

            def ok(r: Any, status: int = 200) -> Any:
                need(r.status_code == status)
                return r.json()

            def enable_and_install() -> dict[str, str]:
                models = {"a": specialist_model(fde.client, fde, script, org=base_a)}
                b_stack = fake_model_stack(
                    b_admin.client, b_admin, org=base_b, config={"script": []}, conn_name="assist-b"
                )
                models["b"] = b_stack["model"]
                for h, base in ((fde, base_a), (b_admin, base_b)):
                    ok(h.client.post(f"{base}/assist/enable", headers=h))
                projects = [(fde, base_a, "sample", "a"), (fde, base_a, "other", "a"),
                            (b_admin, base_b, "sample", "b")]  # fmt: skip
                for h, base, proj, side in projects:
                    url = f"{base}/workspaces/main/projects/{proj}/assist/install"
                    out = ok(h.client.post(url, headers=h, json={"model_id": models[side]}))
                    need(len(out) == 6 and all(s["available"] for s in out))
                return models

            models = step(
                "2. each administrator enables assist; the six specialists are installed in P1, "
                "P2 and Q1 with a scripted fake model",
                enable_and_install,
            )
            done_with(2)

            # ------------------------------------------------------------------ step 3
            requests_url = f"{base_a}/workspaces/main/projects/sample/assist/requests"

            def ask(h: Hdr, body: dict[str, Any], status: int = 201) -> dict[str, Any]:
                out: dict[str, Any] = ok(h.client.post(requests_url, headers=h, json=body), status)
                return out

            def run_of(out: dict[str, Any]) -> dict[str, Any]:
                def ended() -> dict[str, Any] | None:
                    got = ok(fde.client.get(f"{base_a}/runs/{out['run_id']}", headers=fde))
                    return got if got["status"] in ("completed", "failed", "cancelled") else None

                run: dict[str, Any] = wait_for(ended, 60.0)
                need(run["status"] == "completed")
                answer = ok(fde.client.get(f"{base_a}/runs/{out['run_id']}/result", headers=fde))
                need(bool(answer["output"]))
                return run

            six = [
                ("research which tables hold customer data", None, "research", "research"),
                ("design the approach for a churn agent", None, "solution_design", "design"),
                ("build an app for the sales team", None, "build", "build"),
                ("evaluate the accuracy of the agent", None, "evaluation", "eval"),
                ("how much did we spend last week", None, "cost", "cost"),
                (f"tell me about this one {board_marker}", {"kind": "release", "id": rel.id},
                 "deployment", "context:release"),
            ]  # fmt: skip
            routed: dict[str, dict[str, Any]] = {}
            for words, context, key, rule in six:

                def routed_by_rule(words: str = words, context: Any = context, key: str = key,
                                   rule: str = rule) -> None:  # fmt: skip
                    body = {"text": words, **({"context": context} if context else {})}
                    out = ask(fde, body)
                    need((out["status"], out["route_method"]) == ("routed", "rule"))
                    need((out["specialist_key"], out["rule_id"]) == (key, rule))
                    run_of(out)
                    routed[key] = out

                step(
                    f"3. a request of rule {rule} is routed by rule to {key} and its run answers",
                    routed_by_rule,
                )

            def board_names_the_release() -> None:
                with owner_db.session() as o:
                    [(name, status, content)] = tool_results(o, routed["deployment"]["run_id"])
                need((name, status) == ("seth-board-read", "succeeded"))
                [p1] = content["board"]["projects"]
                need(p1["project"]["slug"] == "sample")
                releases = [r["id"] for s in p1["solutions"] for r in s["releases"]]
                need(rel.id in releases)

            step("3. the deployment run's seth.board.read result names A's release",
                 board_names_the_release)  # fmt: skip
            done_with(3)

            # ------------------------------------------------------------------ step 4
            ambiguous = f"design an evaluation for the churn agent {PICKS_EVALUATION}"

            def refused_ambiguous() -> None:
                out = ask(fde, {"text": ambiguous})
                need((out["status"], out["status_reason"]) == ("refused", "route_ambiguous"))
                need(out["candidates"] == ["evaluation", "solution_design"]
                     or set(out["candidates"]) == {"evaluation", "solution_design"})  # fmt: skip

            step("4. an ambiguous request with model routing off is refused route_ambiguous",
                 refused_ambiguous)  # fmt: skip

            def routed_by_model() -> None:
                ok(fde.client.put(f"{base_a}/assist/settings", headers=fde, json={
                    "model_routing_enabled": True, "router_model_id": models["a"],
                    "specialist_max_cost": "1"}))  # fmt: skip
                out = ask(fde, {"text": ambiguous}, 202)
                need(out["status"] == "queued")

                def settled() -> dict[str, Any] | None:
                    got = ok(fde.client.get(f"{base_a}/assist/requests/{out['id']}", headers=fde))
                    return got if got["status"] in ("routed", "refused", "failed") else None

                final: dict[str, Any] = wait_for(settled, 60.0)
                need((final["status"], final["route_method"]) == ("routed", "model"))
                need(final["specialist_key"] == "evaluation")
                run_of(final)
                with owner_db.session() as o:
                    calls = o.execute(
                        text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t "
                             "AND event_type = 'model_call' AND run_id IS NULL"),
                        {"t": a["org"].tenant},
                    ).scalar_one()  # fmt: skip
                need(calls == 1)

            step("4. with model routing on it is routed by model to evaluation, with one "
                 "model_call usage row", routed_by_model)  # fmt: skip
            done_with(4)

            # ------------------------------------------------------------------ step 5
            def proposed() -> dict[str, Any]:
                out = ask(fde, {"text": f"deploy the sales app {plan_marker}"})
                need(out["specialist_key"] == "deployment")
                run_of(out)
                detail = ok(fde.client.get(f"{base_a}/assist/requests/{out['id']}", headers=fde))
                [proposal] = detail["proposals"]
                need((proposal["kind"], proposal["status"]) == ("delivery_plan", "open"))
                need(proposal["payload"] == plan_payload)
                return dict(proposal)

            proposal = step("5. the deployment specialist proposes a delivery plan", proposed)

            def accepted() -> str:
                url = f"{base_a}/assist/proposals/{proposal['id']}/accept"
                out = ok(fde.client.post(url, headers=fde, json={}))
                need(out["status"] == "accepted" and out["result_ref"]["kind"] == "delivery_plan")
                made = flow.get_plan(out["result_ref"]["id"])
                need(made["status"] in ("requested", "planning"))
                return str(made["id"])

            plan_id = step("5. the FDE accepts it: a requested plan", accepted)

            def self_approval() -> Any:
                need(runner.run_once())
                sealed = flow.get_plan(plan_id)
                need(sealed["status"] == "sealed")
                r = fde.client.post(f"{plans(a['org'].slug)}/{plan_id}/approve", headers=fde,
                                    json={"plan_digest": sealed["digest"]})  # fmt: skip
                need(r.status_code == 403 and r.json()["code"] == "self_approval_forbidden")
                return sealed

            sealed = step("5. the runner seals it; the FDE's approval is refused (403 "
                          "self_approval_forbidden)", self_approval)  # fmt: skip

            def verified() -> None:
                deployment_id = flow.approve(sealed)["deployment_id"]
                need(runner.run_once())
                for status in ("verifying", "verified"):
                    make_due(owner_db, deployment_id)
                    need(runner.run_once())
                    need(flow.get(deployment_id)["status"] == status)

            step("5. a second administrator approves; the deployment reaches verified", verified)
            done_with(5)

            # ------------------------------------------------------------------ step 6
            def viewer_refused() -> None:
                email = system_role_user(owner_db, a["org"].tenant, "viewer")
                with owner_db.session() as o:
                    uid = o.execute(text("SELECT id FROM shifde.users WHERE primary_email = :e"),
                                    {"e": email}).scalar_one()  # fmt: skip
                grant(owner_db, a["org"].tenant, uid, ["assist:request:write"])
                out = ask(session(email), {"text": "deploy the sales app"})
                need(
                    (out["status"], out["status_reason"]) == ("refused", "specialist_not_permitted")
                )
                need(out["specialist_key"] == "deployment" and out["run"] is None)

            step("6. a viewer's deployment request is refused specialist_not_permitted",
                 viewer_refused)  # fmt: skip
            done_with(6)

            # ------------------------------------------------------------------ step 7
            def foreign_not_found() -> None:
                out = ask(fde, {"text": f"evaluate the accuracy {foreign_marker}"})
                need(out["specialist_key"] == "evaluation")
                run_of(out)
                with owner_db.session() as o:
                    [(name, status, content)] = tool_results(o, out["run_id"])
                need((name, status) == ("seth-releases-read", "succeeded"))
                need(content == {"error": "not_found", "truncated": False})

            step("7. A's specialist run scripted to read B's release id gets not_found",
                 foreign_not_found)  # fmt: skip
            done_with(7)
    finally:
        owner_db.dispose()


def run_demo(through: int) -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    try:
        with (
            scratch_database() as (app_url, owner_url),
            tempfile.TemporaryDirectory() as workdir,
            contextlib.suppress(Done),
        ):
            drive(app_url, owner_url, Path(workdir), through)
    except StepFailed as failed:
        print(f"ASSIST DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("ASSIST DEMO FAILED: unexpected error")
        return 1
    print("ASSIST DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against the fake model provider and the native fake platform",
    )
    parser.add_argument(
        "--through",
        type=int,
        choices=range(1, STEPS + 1),
        default=STEPS,
        metavar=f"1..{STEPS}",
        help=f"stop after this step (default {STEPS})",
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"ASSIST DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend(args.through)
        return run_demo(args.through)
    except DemoError as exc:
        print(f"ASSIST DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
