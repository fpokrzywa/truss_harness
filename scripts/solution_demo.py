"""Scripted solution demo of sub-project 5's "done when" against the fake Databricks workspace, the
fake model provider and the native fake platform (design SP5 section 10, section 11 item 9).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient, a worker on a thread with
`shifde.worker_main.job_handlers()`, the `shifde.executor` runner over the native fake platform,
and every data-platform call of the API, the worker and the generated app answered by one
`FakeDatabricksWorkspace(sample_data=True, serving={"agent-endpoint": AGENT_SCRIPT})`. Nothing
contacts a Databricks workspace or opens a socket for it. Needs the local cluster running
(`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/solution_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
In it, organization A (project P, `main/sample`) gets an FDE and a second tenant administrator, a
tenant budget, a Databricks read connection to the sample-data workspace (tested and discovered)
and two `fake` model providers: drafting (`DRAFT_SCRIPT`) and runtime (`AGENT_SCRIPT`).
1. The FDE requests a draft of a `[[appgen-sample]]` brief; the worker drafts it (`drafted`), with
   a settled budget hold and the draft call's usage row (`model_call`, B-3944).
2. The FDE posts revision 2 (one tool's `max_rows` changed) and accepts it: the prompt, the two
   tools, the agent, the 3-case dataset and the solution version with two screens exist.
3. An ordinary SETH run of the published agent answers case 1, passing its assertions.
4. The FDE seals a release (`shifde.packaging/2`, one model binding `model = "agent-endpoint"`); a
   plan of it on a target bound to serving endpoint `agent-endpoint` is refused (409
   `evaluation_required`); its evaluation runs to `passed`.
5. The plan is accepted, and rendering the release twice gives the same bytes and digest.
6. The requester's self-approval is refused (403); a second tenant administrator approves.
7. The runner applies, observes and verifies the deployment (`app-deep-health` passed):
   `verified`.
8. The plan's stored artifact is unpacked and its `app/app.py` imported and driven with Flask's
   test client on a real `databricks.sdk.WorkspaceClient` routed to the same fake workspace:
   every case's `/api/ask` answer passes its assertions, the table screen's tool returns sample
   rows, `/health?deep=1` is ok, `/` serves `index.html`, and no file holds the API origin,
   `shx_` or `sk-`.
9. A tenant administrator of organization B gets 404 on A's request, a draft of it, A's release
   and A's plan. (The app preview route of #340 is not on main: the release read route stands in.)
The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `SOLUTION DEMO PASSED` (exit 0) or
`SOLUTION DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a token,
password, cookie, database URL or response body. Standard library only at the top level: when
`shifde` is not importable it re-runs itself once in the backend environment
(`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import logging
import os
import re
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
    "only the fake Databricks workspace, fake model provider and native fake platform are "
    "supported (no Databricks workspace is configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_SOLUTION_DEMO_RERUN"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)
API_ORIGIN = "https://testserver"


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
    """The solution flow, one printed line per step; `StepFailed` names the first failing one."""
    import pytest
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from shifde import worker_main
    from shifde.adapters.native.base import create_archive, read_archive
    from shifde.app import create_app
    from shifde.appgen.render import render
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.delivery import plans as delivery_plans
    from shifde.evaluation.assertions import evaluate
    from shifde.runtime.worker import Worker
    from tests._support.appgen_fixtures import AGENT_SCRIPT, CASE_CALLS, SAMPLE_DRAFT
    from tests._support.p3util import fake_model_stack
    from tests._support.p5util import fast, start_run, wait_for
    from tests._support.p8util import rate_card, rate_version
    from tests.adapters.dataplatforms.helpers import WAREHOUSE
    from tests.appgen.helpers import (
        PROJECT,
        TOKEN as CONNECTION_TOKEN,
        body,
        created,
        databricks,
        drafting_model,
        get,
        request_url,
        wire,
    )
    from tests.appgen.rendered import (
        ENDPOINT,
        load_module,
        workspace_client,
        write_files,
    )
    from tests.appgen.test_accept import EXPECTED, THRESHOLDS, _counts
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.routes import Flow, make_due, plans, request_plan
    from tests.delivery.test_generated_app import app_target
    from tests.evaluation.helpers import capture
    from tests.executor.support import ask_plan, credentials_file, fake_platform, make_runner
    from tests.identity import harness
    from tests.ledger.util import add_budget
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
        # every step signs a person in afresh: more than the default 10 a minute
        login_rate_per_account_per_minute=60,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    cases = SAMPLE_DRAFT["evaluation_cases"]
    try:

        def people() -> tuple[Any, Any, str, str, str]:
            with owner_db.session() as o:
                org, other = _client(o, "demo"), _client(o, "other")
                # project P: the appgen and run helpers' `main/sample`
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
                # an applicable budget, so every model call places a hold
                add_budget(o, org.tenant, amount="1000000")
                o.commit()
                # case outputs are kept, so the evaluation can score them
                capture(o, org.slug, "full")
            fde = system_role_user(owner_db, org.tenant, "tenant_administrator")
            approver = system_role_user(owner_db, org.tenant, "tenant_administrator")
            b_admin = system_role_user(owner_db, other.tenant, "tenant_administrator")
            return org, other, fde, approver, b_admin

        org, other, fde_email, approver, b_admin = step(
            "1. organization A (project P, an FDE, a second tenant administrator and a tenant "
            "budget) and organization B",
            people,
        )
        base = f"/api/v1/orgs/{org.slug}"
        b_base = f"/api/v1/orgs/{other.slug}"
        with contextlib.ExitStack() as stack:

            def start() -> tuple[Any, Any, Any]:
                client = stack.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url=API_ORIGIN,
                        headers={"Origin": ORIGIN},
                    )
                )
                workspace = wire(client, serving={ENDPOINT: AGENT_SCRIPT})
                # the FDE's own session (a cookie jar of its own)
                fde = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
                return client, workspace, fde

            client, workspace, fde = step(
                "1. start the API in-process on the sample-data workspace with serving endpoint "
                "agent-endpoint",
                start,
            )
            h = harness.login(fde, fde_email)

            lake = step(
                "1. a Databricks read connection to the sample-data workspace, tested and "
                "discovered",
                lambda: databricks(fde, h, org=base),
            )
            draft_model = step(
                "1. a drafting model on a fake provider scripted with DRAFT_SCRIPT",
                lambda: drafting_model(fde, h, org=base),
            )

            def runtime_stack() -> dict[str, str]:
                ids = fake_model_stack(
                    fde,
                    h,
                    org=base,
                    config={"script": AGENT_SCRIPT},
                    conn_name="agent-model",
                )
                # the release's model binding: model `agent-endpoint` of the runtime provider,
                # priced by a rate card
                r = fde.post(
                    f"{base}/models",
                    headers=h,
                    json={
                        "provider_id": ids["provider"],
                        "model_key": ENDPOINT,
                        "display_name": "Agent endpoint",
                        "capabilities": ["chat"],
                        "context_window": 8192,
                        "max_output_tokens": 1024,
                        "lifecycle": "active",
                        "rate_limits": {},
                    },
                )
                need(r.status_code == 201)
                card = rate_card(fde, h, model_key=ENDPOINT, base=base)
                rate_version(
                    fde, h, card["id"], base=base, currency="USD", input_rate=1, output_rate=1
                )
                return ids

            runtime = step(
                "1. a runtime model on a fake provider scripted with AGENT_SCRIPT, and its "
                "model agent-endpoint",
                runtime_stack,
            )

            def start_worker() -> None:
                app: Any = client.app
                worker = Worker(
                    fast(settings),
                    worker_id=f"solution-demo-{time.monotonic_ns()}",
                    # one case run at a time: concurrent case runs of one connection can
                    # deadlock on its secret's `last_used_at`
                    concurrency=1,
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

            step(
                "1. start the worker in-process (job_handlers of shifde.worker_main)", start_worker
            )

            def request_draft() -> dict[str, Any]:
                made = created(fde, h, body(lake, draft_model), org=base)

                def drafted() -> dict[str, Any] | None:
                    out = get(fde, h, made["id"], org=base)
                    return out if out["status"] not in ("queued", "drafting") else None

                out: dict[str, Any] = wait_for(drafted, 60.0)
                need(out["status"] == "drafted")
                need([r["revision_no"] for r in out["revisions"]] == [1])
                need(out["revisions"][0]["draft"] == SAMPLE_DRAFT)
                return out

            request = step(
                "2. the FDE requests a draft of a [[appgen-sample]] brief and the worker drafts "
                "it (drafted, revision 1)",
                request_draft,
            )

            def draft_ledger() -> None:
                with owner_db.session() as o:
                    holds = o.execute(
                        text(
                            "SELECT status FROM shifde.budget_reservations "
                            "WHERE tenant_id = :t AND invocation_id IS NOT NULL"
                        ),
                        {"t": org.tenant},
                    ).all()
                    usage = o.execute(
                        text(
                            "SELECT event_type FROM shifde.usage_events "
                            "WHERE tenant_id = :t AND model_id = :m"
                        ),
                        {"t": org.tenant, "m": draft_model},
                    ).all()
                    o.rollback()
                need([r[0] for r in holds] == ["settled"])
                need([r[0] for r in usage] == ["model_call"])

            step(
                "2. the draft call has a settled budget hold and a usage row (model_call, B-3944)",
                draft_ledger,
            )

            def revise_and_accept() -> dict[str, Any]:
                edit = copy.deepcopy(SAMPLE_DRAFT)
                edit["tools"][1]["max_rows"] = 50
                revised = fde.post(
                    request_url(request["id"], base, "/drafts"),
                    headers=h,
                    json={"base_revision_no": 1, "draft": edit},
                )
                need(revised.status_code == 201)
                accepted = fde.post(
                    request_url(request["id"], base, "/accept"),
                    headers=h,
                    json={
                        "revision_no": 2,
                        "solution_name": "Sales app",
                        "model_id": runtime["model"],
                        "thresholds": THRESHOLDS,
                    },
                )
                need(accepted.status_code == 200)
                out: dict[str, Any] = accepted.json()
                need(out["status"] == "accepted" and out["accepted_revision_no"] == 2)
                return out

            accepted = step(
                "3. the FDE posts revision 2 (one tool's max_rows changed) and accepts it "
                "(accepted revision 2)",
                revise_and_accept,
            )
            progress = accepted["accept_progress"]

            def objects() -> None:
                with owner_db.session() as o:
                    need(_counts(o, request["project_id"]) == EXPECTED)
                    screens = o.execute(
                        text(
                            "SELECT spec -> 'screens' FROM shifde.solution_versions WHERE id = :v"
                        ),
                        {"v": accepted["solution_version_id"]},
                    ).scalar_one()
                    max_rows = o.execute(
                        text(
                            "SELECT spec -> 'binding' ->> 'max_rows' FROM shifde.artifact_versions "
                            "WHERE id = :v"
                        ),
                        {"v": progress["tool_version_ids"]["invoiced_by_region"]},
                    ).scalar_one()
                    o.rollback()
                need(len(screens) == 2 and screens == SAMPLE_DRAFT["screens"])
                need(max_rows == "50")

            step(
                "3. the prompt, the 2 tools, the agent, the 3-case dataset and the solution "
                "version with 2 screens exist",
                objects,
            )

            def run_case_1() -> None:
                started = start_run(
                    fde, h, progress["agent_artifact_id"], cases[0]["input"]["message"], org=base
                )

                def ended() -> dict[str, Any] | None:
                    r = fde.get(f"{base}/runs/{started['id']}", headers=h)
                    need(r.status_code == 200)
                    run: dict[str, Any] = r.json()
                    return run if run["status"] in ("completed", "failed", "cancelled") else None

                need(wait_for(ended, 60.0)["status"] == "completed")
                result = fde.get(f"{base}/runs/{started['id']}/result", headers=h)
                need(result.status_code == 200)
                outcome = evaluate(cases[0]["assertions"], result.json()["output"])
                need(all(a["passed"] for a in outcome))

            step(
                "4. an ordinary SETH run of the published agent answers case 1 and passes its "
                "assertions",
                run_case_1,
            )

            def seal() -> dict[str, Any]:
                version = accepted["solution_version_id"]
                sealed = fde.post(
                    f"{base}{PROJECT}/solutions/{progress['solution_id']}/versions/{version}"
                    "/releases",
                    headers=h,
                    json={
                        "environment_bindings": {
                            "model_bindings": {
                                runtime["model"]: {
                                    "provider_id": runtime["provider"],
                                    "model": ENDPOINT,
                                }
                            },
                            "connection_bindings": {lake: lake},
                        }
                    },
                )
                need(sealed.status_code == 201)
                release: dict[str, Any] = sealed.json()
                manifest = release["manifest"]
                need(manifest["compiler_version"] == "shifde.packaging/2")
                bound = list(manifest["bindings"]["model_bindings"].values())
                need([b["model"] for b in bound] == [ENDPOINT])
                return release

            release = step(
                "5. the FDE seals a release under shifde.packaging/2 with the single model "
                "binding model = agent-endpoint",
                seal,
            )

            executor = step(
                "5. an executor of A with its credential",
                lambda: make_executor(client, org, fde_email),
            )
            flow = Flow(
                client, org.slug, "main", org.tenant, fde_email, approver, executor.token, owner_db
            )
            target = step(
                "5. a target whose bindings include serving_endpoint agent-endpoint",
                lambda: app_target(flow),
            )
            native = fake_platform()
            runner = step(
                "5. start the executor's runner against the native fake platform",
                lambda: make_runner(
                    client,
                    executor.token,
                    credentials_file(workdir, target["address"], tenant=org.tenant),
                    native,
                ),
            )

            def refused() -> None:
                hh = as_user(client, fde_email)
                answer = request_plan(client, hh, org.slug, target["id"], release["id"], status=409)
                client.cookies.clear()
                need(answer["code"] == "evaluation_required")

            step(
                "5. a plan of the release is refused before any evaluation (409 "
                "evaluation_required)",
                refused,
            )

            def evaluation() -> None:
                requested = fde.post(
                    f"{base}/releases/{release['id']}/evaluations", headers=h, json={}
                )
                need(requested.status_code == 201)
                url = f"{base}/evaluation-runs/{requested.json()['id']}"

                def scored() -> dict[str, Any] | None:
                    got = fde.get(url, headers=h)
                    need(got.status_code == 200)
                    out: dict[str, Any] = got.json()
                    return out if out["status"] in ("passed", "failed", "error") else None

                need(wait_for(scored, 180.0)["status"] == "passed")

            step("5. an evaluation of the release runs to passed (in-process worker)", evaluation)

            resolved: list[Any] = []
            gathered = delivery_plans.resolved_for_release

            def spy(*args: Any, **kwargs: Any) -> Any:
                out = gathered(*args, **kwargs)
                resolved.append(out)
                return out

            def accepted_plan() -> dict[str, Any]:
                with pytest.MonkeyPatch.context() as mp:
                    # observe the `Resolved` the plan request gathers (not changed)
                    mp.setattr(delivery_plans, "resolved_for_release", spy)
                    made: dict[str, Any] = ask_plan(flow, target["id"], release_id=release["id"])
                need(made["status"] in ("requested", "planning", "queued", "sealing"))
                need(len(resolved) == 1)
                return made

            plan = step(
                "6. the target with serving_endpoint agent-endpoint accepts a plan of the "
                "evaluated release",
                accepted_plan,
            )

            def render_twice() -> None:
                once = render(release["manifest"], release["digest"], resolved[0])
                twice = render(release["manifest"], release["digest"], resolved[0])
                need(once == twice)
                first = hashlib.sha256(create_archive(once)).hexdigest()
                second = hashlib.sha256(create_archive(twice)).hexdigest()
                need(first == second == plan["artifact_digest"])

            step(
                "6. rendering the release twice gives identical bytes and the plan's artifact "
                "digest",
                render_twice,
            )

            def seal_plan() -> dict[str, Any]:
                need(runner.run_once())
                sealed = flow.get_plan(plan["id"])
                need(sealed["status"] == "sealed")
                return sealed

            sealed = step("6. the runner seals the plan", seal_plan)

            def self_approval() -> None:
                hh = as_user(client, fde_email)
                r = client.post(
                    f"{plans(flow.slug)}/{plan['id']}/approve",
                    json={"plan_digest": sealed["digest"]},
                    headers=hh,
                )
                client.cookies.clear()
                need(r.status_code == 403 and r.json()["code"] == "self_approval_forbidden")

            step(
                "7. the requester's self-approval is refused (403 self_approval_forbidden)",
                self_approval,
            )
            approved = step(
                "7. a second tenant administrator approves", lambda: flow.approve(sealed)
            )
            deployment_id = approved["deployment_id"]
            check("7. a second tenant administrator approves", deployment_id is not None)

            def run_to(status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment_id)
                need(runner.run_once())
                need(flow.get(deployment_id)["status"] == status)

            step("8. apply on the native fake", lambda: run_to("observing", due=False))
            step("8. observe on the native fake", lambda: run_to("verifying", due=True))
            step("8. verify on the native fake", lambda: run_to("verified", due=True))
            checks = {
                c["name"]: c["status"] for c in flow.get(deployment_id)["verification"]["checks"]
            }
            check(
                "8. native-runtime, runtime-warehouse and app-deep-health passed",
                checks
                == {
                    "native-runtime": "passed",
                    "runtime-warehouse": "passed",
                    "app-deep-health": "passed",
                },
            )
            print(
                "ok   8. deployment verified; native-runtime, runtime-warehouse and "
                "app-deep-health passed"
            )
            check("8. the native fake deployed the app", bool(native.actions))

            def unpack() -> dict[str, bytes]:
                with owner_db.session() as o:
                    content = o.execute(
                        text("SELECT content FROM shifde.delivery_artifacts WHERE id = :a"),
                        {"a": plan["artifact_id"]},
                    ).scalar_one()
                    o.rollback()
                files = read_archive(bytes(content))
                need("app/app.py" in files and "app/static/index.html" in files)
                write_files(workdir / "artifact", files)
                return files

            files = step("9. unpack the plan's stored artifact", unpack)

            def no_origin() -> None:
                # `sk-` as a key prefix: at the start of a token (the bundled React code holds
                # attribute names such as `mask-type`)
                key = re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]")
                secrets_used = (executor.token.encode(), CONNECTION_TOKEN.encode())
                for content in files.values():
                    need(API_ORIGIN.encode() not in content and ORIGIN.encode() not in content)
                    need(b"shx_" not in content and key.search(content) is None)
                    need(not any(value in content for value in secrets_used))

            step(
                "9. no file of the archive holds the API origin, shx_, an sk- key or a token "
                "the demo used",
                no_origin,
            )

            with pytest.MonkeyPatch.context() as mp:

                def generated_app() -> Any:
                    mp.setenv("DATABRICKS_WAREHOUSE_ID", WAREHOUSE)
                    mp.setenv("SERVING_ENDPOINT", ENDPOINT)
                    module = load_module(
                        workdir / "artifact" / "app" / "app.py", "solution_demo_generated_app"
                    )
                    # a real WorkspaceClient whose requests reach the same fake (no socket); its
                    # host-metadata probe's warning is not a step line
                    mp.setattr(logging.getLogger("databricks.sdk"), "disabled", True)
                    sdk = workspace_client(workspace, mp)
                    return module.create_app(workspace_client=sdk).test_client()

                app = step(
                    "9. import the generated app.py with a WorkspaceClient routed to the fake "
                    "workspace",
                    generated_app,
                )

                def ask(index: int) -> None:
                    # the app's one usage line per answer goes to its log, not the demo's output
                    with contextlib.redirect_stdout(io.StringIO()):
                        r = app.post("/api/ask", json={"message": cases[index]["input"]["message"]})
                    need(r.status_code == 200)
                    outcome = evaluate(cases[index]["assertions"], r.get_json()["answer"])
                    need(all(a["passed"] for a in outcome))

                for index, case in enumerate(cases):
                    step(
                        f"9. POST /api/ask answers case {case['case_id']} and passes its "
                        "assertions",
                        lambda index=index: ask(index),
                    )

                def table_tool() -> None:
                    [screen] = [s for s in SAMPLE_DRAFT["screens"] if s["kind"] == "table"]
                    name, arguments, _, _ = CASE_CALLS[0]
                    need(screen["tool"] == name)
                    r = app.post(f"/api/tools/{name}", json={"params": arguments})
                    need(r.status_code == 200)
                    out = r.get_json()
                    need(out["row_count"] >= 1 and len(out["rows"]) == out["row_count"])

                step("9. POST /api/tools/<the table screen's tool> returns sample rows", table_tool)

                def deep_health() -> None:
                    r = app.get("/health?deep=1")
                    need(r.status_code == 200 and r.get_json()["status"] == "ok")

                step("9. GET /health?deep=1 is 200 with status ok", deep_health)

                def index_html() -> None:
                    r = app.get("/")
                    need(r.status_code == 200 and r.data == files["app/static/index.html"])

                step("9. GET / serves index.html", index_html)

            def isolation() -> None:
                b = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
                hb = harness.login(b, b_admin)
                urls = [
                    ("GET", request_url(request["id"], b_base), None),
                    ("GET", request_url(request["id"], base), None),
                    (
                        "POST",
                        request_url(request["id"], b_base, "/drafts"),
                        {"base_revision_no": 2, "draft": SAMPLE_DRAFT},
                    ),
                    (
                        "POST",
                        request_url(request["id"], base, "/drafts"),
                        {"base_revision_no": 2, "draft": SAMPLE_DRAFT},
                    ),
                    ("GET", f"{b_base}/releases/{release['id']}", None),
                    ("GET", f"{base}/releases/{release['id']}", None),
                    ("GET", f"{plans(other.slug)}/{plan['id']}", None),
                    ("GET", f"{plans(org.slug)}/{plan['id']}", None),
                ]
                for method, url, payload in urls:
                    r = b.request(method, url, headers=hb, json=payload)
                    need(r.status_code == 404)

            step(
                "10. organization B's administrator gets 404 on A's request, a draft of it, A's "
                "release and A's plan",
                isolation,
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
        print(f"SOLUTION DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("SOLUTION DEMO FAILED: unexpected error")
        return 1
    print("SOLUTION DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against the fake workspace, fake model provider and native fake platform",
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"SOLUTION DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"SOLUTION DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
