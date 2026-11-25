"""Scripted Docker demo of sub-project 8 part D against the in-memory Docker daemon, the in-process
Databricks workspace fakes and the fake model provider (design SP8 section 6).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient, a worker on a thread with
`shifde.worker_main.job_handlers()` and the `shifde.executor` runner over the Docker fake
(`FakeContainerRuntime`), which serves the staged rendered app on the sample-data workspace fake.
It runs no `docker` command, needs no Docker daemon and contacts no Databricks workspace. Needs the
local cluster running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/docker_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
1. Organization A (project P, `main/sample`) has an FDE, a second tenant administrator, a
   Databricks target (its claim on the workspace) and a Docker target of that workspace;
   organization B has a Databricks target of its own. The FDE drafts and accepts an app of a
   `[[appgen-sample]]` brief and seals a `docker.compose/v1` release of it
   (`shifde.packaging/2`); its plan is refused (409 `evaluation_required`) and its evaluation runs
   to `passed`.
2. A plan on a Docker target bound to another endpoint is refused (409
   `serving_endpoint_mismatch`), one on a Docker target of B's workspace too (409
   `workspace_not_claimed`, the answer an unclaimed workspace gets).
3. Rendering the release twice for the target gives the same bytes and the plan's artifact digest;
   the requester's self-approval is refused (403) and a second person approves; the runner applies,
   observes and verifies it: `verified`, with `app-deep-health` passed by the staged rendered code.
4. A second release replaces the container in the same compose project and is `verified`.
5. The first plan's stored artifact holds no API or web origin, `shx_`, `sk-` key, client secret
   or token.
6. Organization B's administrator gets 404 on A's Docker target, release and plan, and 409
   `platform_already_registered` when registering A's daemon address.
The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `DOCKER DEMO PASSED` (exit 0) or
`DOCKER DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a token,
key, password, cookie, database URL or response body. Standard library only at the top level: when
`shifde` is not importable it re-runs itself once in the backend environment
(`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FAKE_ONLY = (
    "only the in-memory Docker daemon, the Databricks fakes and the fake model provider are "
    "supported (no Docker daemon is configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_DOCKER_DEMO_RERUN"
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
    """The Docker flow, one printed line per step; `StepFailed` names the first failing one."""
    import pytest
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from shifde import worker_main
    from shifde.adapters.native.base import create_archive, read_archive
    from shifde.adapters.native.docker.runtime import DockerCliRuntime
    from shifde.adapters.native.docker.source import compose_project, image_tag
    from shifde.app import create_app
    from shifde.appgen.render import render
    from shifde.appgen.render_docker import TARGET_KIND, DockerTargetValues
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.delivery import plans as delivery_plans
    from shifde.executor.client import GatewayClient
    from shifde.executor.credentials import CredentialBroker
    from shifde.executor.runner import FakePlatform, Runner
    from shifde.runtime.worker import Worker
    from tests._support.appgen_fixtures import AGENT_SCRIPT, SAMPLE_DRAFT
    from tests._support.p3util import fake_model_stack
    from tests._support.p5util import fast, wait_for
    from tests._support.p8util import rate_card, rate_version
    from tests.adapters.dataplatforms.helpers import HOST, WAREHOUSE
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
    from tests.appgen.rendered import ENDPOINT
    from tests.appgen.test_accept import THRESHOLDS
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.docker import docker_target, workspace_target
    from tests.delivery.routes import (
        Flow,
        address,
        make_due,
        plans,
        register,
        request_plan,
        sp8_body,
        target_body,
        targets,
    )
    from tests.evaluation.helpers import capture
    from tests.executor.support import ask_plan, sp8_credentials_file
    from tests.identity import harness
    from tests.ledger.util import add_budget
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
        # every step signs a person in afresh: more than the default login limits
        login_rate_per_account_per_minute=240,
        login_rate_per_minute=240,
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    workspace_host = f"https://{HOST}"  # the read connection's workspace
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

        org, other, fde_email, approver, b_email = step(
            "1. organization A (project P, an FDE, a second tenant administrator and a tenant "
            "budget) and organization B with its own administrator",
            people,
        )
        base = f"/api/v1/orgs/{org.slug}"
        b_base = f"/api/v1/orgs/{other.slug}"
        with contextlib.ExitStack() as stack:
            patch = stack.enter_context(pytest.MonkeyPatch.context())
            patch.setenv("SHIFDE_ENV", "test")  # the Docker fake refuses anything else

            def no_command(*_args: object, **_options: object) -> bytes:
                raise RuntimeError("the demo runs no docker command")

            patch.setattr(DockerCliRuntime, "_run", no_command)

            def start() -> tuple[Any, Any]:
                client = stack.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url=API_ORIGIN,
                        headers={"Origin": ORIGIN},
                    )
                )
                wire(client, serving={ENDPOINT: AGENT_SCRIPT})
                # the FDE's own session (a cookie jar of its own)
                fde = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
                return client, fde

            client, fde = step(
                "1. start the API in-process on the sample-data workspace with serving endpoint "
                "agent-endpoint",
                start,
            )
            h = harness.login(fde, fde_email)
            b = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
            hb = harness.login(b, b_email)

            executor = step(
                "1. an executor of A with its credential",
                lambda: make_executor(client, org, fde_email),
            )
            flow = Flow(
                client, org.slug, "main", org.tenant, fde_email, approver, executor.token, owner_db
            )
            step(
                "1. a Databricks target of A: its claim on the workspace",
                lambda: workspace_target(flow, workspace_host),
            )
            target = step(
                "1. a Docker target of A on a daemon of its own, with that workspace and serving "
                "endpoint agent-endpoint",
                lambda: docker_target(flow, workspace_host=workspace_host, warehouse_id=WAREHOUSE),
            )
            b_workspace = step(
                "1. a Databricks target of B: its claim on another workspace",
                lambda: register(b, hb, other.slug, target_body(ws_slug(other), address())),
            )

            lake = step(
                "1. a Databricks read connection of A to the sample-data workspace, tested and "
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
                "priced model agent-endpoint",
                runtime_stack,
            )

            def start_worker() -> None:
                app: Any = client.app
                worker = Worker(
                    fast(settings),
                    worker_id=f"docker-demo-{time.monotonic_ns()}",
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

            def draft() -> dict[str, Any]:
                made = created(fde, h, body(lake, draft_model), org=base)

                def drafted() -> dict[str, Any] | None:
                    out = get(fde, h, made["id"], org=base)
                    return out if out["status"] not in ("queued", "drafting") else None

                out: dict[str, Any] = wait_for(drafted, 60.0)
                need(out["status"] == "drafted")
                need(out["revisions"][0]["draft"] == SAMPLE_DRAFT)
                return out

            request = step(
                "1. the FDE requests a draft of a [[appgen-sample]] brief and the worker drafts "
                "it (drafted, revision 1)",
                draft,
            )

            def accept() -> dict[str, Any]:
                r = fde.post(
                    request_url(request["id"], base, "/accept"),
                    headers=h,
                    json={
                        "revision_no": 1,
                        "solution_name": "Sales app",
                        "model_id": runtime["model"],
                        "thresholds": THRESHOLDS,
                    },
                )
                need(r.status_code == 200)
                out: dict[str, Any] = r.json()
                need(out["status"] == "accepted")
                return out

            accepted = step("1. the FDE accepts revision 1", accept)
            progress = accepted["accept_progress"]

            def docker_solution() -> tuple[str, dict[str, Any]]:
                """A `docker.compose/v1` solution of the accepted agent, and its version spec."""
                with owner_db.session() as o:
                    spec: dict[str, Any] = o.execute(
                        text("SELECT spec FROM shifde.solution_versions WHERE id = :v"),
                        {"v": accepted["solution_version_id"]},
                    ).scalar_one()
                    o.rollback()
                r = fde.post(
                    f"{base}{PROJECT}/solutions",
                    headers=h,
                    json={"name": "Sales app container", "target_kind": TARGET_KIND},
                )
                need(r.status_code == 201 and r.json()["target_kind"] == TARGET_KIND)
                return r.json()["id"], spec

            solution_id, spec = step(
                "1. a docker.compose/v1 solution of the accepted agent", docker_solution
            )

            def seal(title_suffix: str) -> dict[str, Any]:
                screens = [dict(s) for s in spec["screens"]]
                screens[0]["title"] += title_suffix
                url = f"{base}{PROJECT}/solutions/{solution_id}/versions"
                version = fde.post(
                    url,
                    headers=h,
                    json={
                        "entrypoint_kind": "agent",
                        "entrypoint_version_id": progress["agent_version_id"],
                        "data_reads": spec["data_reads"],
                        "evaluation": spec["evaluation"],
                        "screens": screens,
                    },
                )
                need(version.status_code == 201)
                sealed = fde.post(
                    f"{url}/{version.json()['id']}/releases",
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
                need(manifest["target_kind"] == TARGET_KIND)
                need(manifest["app"] == {"generator_version": "shifde.appgen.docker/1.0"})
                return release

            release = step(
                "1. the FDE seals a docker.compose/v1 release under shifde.packaging/2, "
                "generator shifde.appgen.docker/1.0",
                lambda: seal(""),
            )

            def refused(target_id: str, code: str) -> dict[str, Any]:
                hh = as_user(client, fde_email)
                answer: dict[str, Any] = request_plan(
                    client, hh, org.slug, target_id, release["id"], status=409
                )
                client.cookies.clear()
                need(answer["code"] == code)
                return {k: v for k, v in answer.items() if k not in ("request_id", "instance")}

            step(
                "1. a plan of the release is refused before any evaluation (409 "
                "evaluation_required)",
                lambda: refused(target["id"], "evaluation_required"),
            )

            def evaluation(release_id: str) -> None:
                requested = fde.post(
                    f"{base}/releases/{release_id}/evaluations", headers=h, json={}
                )
                need(requested.status_code == 201)
                url = f"{base}/evaluation-runs/{requested.json()['id']}"

                def scored() -> dict[str, Any] | None:
                    got = fde.get(url, headers=h)
                    need(got.status_code == 200)
                    out: dict[str, Any] = got.json()
                    return out if out["status"] in ("passed", "failed", "error") else None

                need(wait_for(scored, 180.0)["status"] == "passed")

            step(
                "1. an evaluation of the release runs to passed (in-process worker)",
                lambda: evaluation(release["id"]),
            )

            def other_endpoint() -> None:
                second = docker_target(
                    flow, workspace_host=workspace_host, serving_endpoint="other-endpoint"
                )
                refused(second["id"], "serving_endpoint_mismatch")

            step(
                "2. a plan on a Docker target bound to another endpoint is refused (409 "
                "serving_endpoint_mismatch)",
                other_endpoint,
            )

            def not_claimed() -> None:
                answers = [
                    refused(docker_target(flow, workspace_host=host)["id"], "workspace_not_claimed")
                    for host in (b_workspace["address"], address())
                ]
                need(answers[0] == answers[1])

            step(
                "2. a plan on a Docker target of organization B's workspace is refused (409 "
                "workspace_not_claimed), as one of an unclaimed workspace is",
                not_claimed,
            )

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
                need(made["status"] == "requested" and len(resolved) == 1)
                return made

            plan = step(
                "3. the Docker target accepts a plan of the evaluated release", accepted_plan
            )

            def render_twice() -> None:
                values = DockerTargetValues(
                    target_id=UUID(target["id"]),
                    port=target["bindings"]["port"],
                    warehouse_id=WAREHOUSE,
                    serving_endpoint=ENDPOINT,
                )
                once = render(release["manifest"], release["digest"], resolved[0], values)
                twice = render(release["manifest"], release["digest"], resolved[0], values)
                need(once == twice and "compose.yaml" in once)
                first = hashlib.sha256(create_archive(once)).hexdigest()
                second = hashlib.sha256(create_archive(twice)).hexdigest()
                need(first == second == plan["artifact_digest"])

            step(
                "3. rendering the release twice for the target gives identical bytes and the "
                "plan's artifact digest",
                render_twice,
            )

            platform = FakePlatform(environment="test")

            def start_runner() -> Runner:
                gateway = GatewayClient(API_ORIGIN, executor.token, http=client)
                me = gateway.whoami()
                creds = sp8_credentials_file(
                    workdir, "docker", target["address"], tenant=org.tenant
                )
                broker = CredentialBroker.from_file(creds)
                return Runner(gateway, broker, me.executor_id, platform, tenant_id=me.tenant_id)

            runner = step(
                "3. start the executor's runner against the in-memory Docker daemon", start_runner
            )

            def seal_plan(plan_id: str) -> dict[str, Any]:
                need(runner.run_once())
                sealed = flow.get_plan(plan_id)
                need(sealed["status"] == "sealed")
                return sealed

            sealed = step("3. the runner seals the plan", lambda: seal_plan(plan["id"]))

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
                "3. the requester's self-approval is refused (403 self_approval_forbidden)",
                self_approval,
            )

            def approve(value: dict[str, Any]) -> str:
                made = flow.approve(value)["deployment_id"]
                need(made is not None)
                return str(made)

            deployment_id = step(
                "3. a second tenant administrator approves", lambda: approve(sealed)
            )

            def run_to(deployment: str, status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment)
                need(runner.run_once())
                need(flow.get(deployment)["status"] == status)

            def verified(deployment: str) -> None:
                run_to(deployment, "verified", due=True)
                detail = flow.get(deployment)
                checks = [(c["name"], c["status"]) for c in detail["verification"]["checks"]]
                names = ("native-version", "runtime-health", "app-deep-health")
                need(checks == [(name, "passed") for name in names])

            project = compose_project(UUID(target["id"]))

            def apply(deployment: str, digest: str) -> None:
                run_to(deployment, "observing", due=False)
                journal = {s["step_key"]: s["state"] for s in flow.get(deployment)["journal"]}
                steps = ("image-build", "image-verify", "compose-up", "container-ready")
                need(journal == dict.fromkeys(steps, "complete"))
                fake = platform.docker
                need(fake is not None and list(fake.projects) == [project])
                need(fake.projects[project].image == image_tag(project, digest))

            step(
                "3. apply completes the four checkpoints of the journal on the in-memory daemon",
                lambda: apply(deployment_id, release["digest"]),
            )
            step("3. observe", lambda: run_to(deployment_id, "verifying", due=True))
            step(
                "3. deployment verified: native-version, runtime-health and app-deep-health passed",
                lambda: verified(deployment_id),
            )

            def staged_code() -> None:
                fake = platform.docker
                need(fake is not None)
                port = target["bindings"]["port"]
                status, answer = fake.http_get_local(project, port, "/health?deep=1")
                need(status == 200 and release["digest"].encode() in answer)
                need(b"table:main.default.orders" in answer)

            step(
                "3. the staged rendered app itself answers deep health with the release digest "
                "and each data-read table",
                staged_code,
            )

            second = step(
                "4. a second release (a changed screen title) is sealed", lambda: seal(" 2")
            )
            step(
                "4. the second release's evaluation runs to passed",
                lambda: evaluation(second["id"]),
            )

            def replace() -> None:
                need(second["digest"] != release["digest"])
                asked = ask_plan(flow, target["id"], release_id=second["id"])
                deployment = approve(seal_plan(asked["id"]))
                apply(deployment, second["digest"])
                run_to(deployment, "verifying", due=True)
                verified(deployment)

            step(
                "4. the second release replaced the container in the same compose project and "
                "is verified",
                replace,
            )

            def no_secret() -> None:
                with owner_db.session() as o:
                    content = o.execute(
                        text("SELECT content FROM shifde.delivery_artifacts WHERE id = :a"),
                        {"a": plan["artifact_id"]},
                    ).scalar_one()
                    o.rollback()
                files = read_archive(bytes(content))
                need({"Dockerfile", "compose.yaml", "app/app.py", "release.json"} <= set(files))
                # `sk-` as a key prefix: at the start of a token (the bundled React code holds
                # attribute names such as `mask-type`)
                key = re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]")
                hidden = [API_ORIGIN, ORIGIN, "shx_", executor.token, CONNECTION_TOKEN]
                for data in files.values():
                    need(key.search(data) is None and b"client_secret" not in data.lower())
                    need(not any(value.encode() in data for value in hidden))

            step(
                "5. no file of the stored artifact holds the API or web origin, shx_, an sk- "
                "key, a client secret or a token the demo used",
                no_secret,
            )

            def isolation() -> None:
                for api, slug in ((base, org.slug), (b_base, other.slug)):
                    for url in (
                        f"{targets(slug)}/{target['id']}",
                        f"{api}/releases/{release['id']}",
                        f"{plans(slug)}/{plan['id']}",
                    ):
                        need(b.get(url, headers=hb).status_code == 404)
                for url in (
                    f"{targets(org.slug)}/{target['id']}",
                    f"{base}/releases/{release['id']}",
                    f"{plans(org.slug)}/{plan['id']}",
                ):
                    need(fde.get(url, headers=h).status_code == 200)

            step(
                "6. organization B's administrator gets 404 on A's Docker target, release and "
                "plan under both slugs (200 for A)",
                isolation,
            )

            def claimed() -> None:
                bad = sp8_body("docker", ws_slug(other), target["address"])
                answer = register(b, hb, other.slug, bad, status=409)
                need(answer["code"] == "platform_already_registered")

            step(
                "6. B registering A's daemon address is refused (409 platform_already_registered)",
                claimed,
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
        print(f"DOCKER DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("DOCKER DEMO FAILED: unexpected error")
        return 1
    print("DOCKER DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against the Docker and Databricks fakes and the fake model provider",
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"DOCKER DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"DOCKER DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
