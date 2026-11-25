"""Scripted Snowflake demo of sub-project 6's "done when" against the in-process Snowflake fakes and
the fake model provider (design SP6 sections 1 and 10).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient, a worker on a thread with
`shifde.worker_main.job_handlers()`, the `shifde.executor` runner over the native Snowflake fake
(`FakeSnowflakeNativePlatform`), and every read-connection call of the API and the worker answered
by one `FakeSnowflakeAccount(sample_data=True, cortex={"mistral-large2": AGENT_SCRIPT})`. The read
connection, the delivery target and the native fake are the same account (one `<org>-<account>`
name). Nothing contacts a Snowflake account or opens a socket for it. Needs the local cluster
running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/snowflake_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
1. Organization B (project P, `main/sample`) gets an FDE and a second tenant administrator, a
   tenant budget, a Snowflake read connection to the sample-data account (tested and discovered)
   and two `fake` model providers: drafting (`DRAFT_SCRIPT`) and runtime (`AGENT_SCRIPT`, with the
   priced model `mistral-large2`). Organization A gets an administrator of its own.
2. The FDE requests a draft of a `[[appgen-sample-snowflake]]` brief over the `DEMO_DB` tables; the
   worker drafts it (`drafted`), with a settled budget hold and the draft call's usage row
   (`model_call`, B-3944).
3. The FDE accepts it: the prompt, the two tools, the agent, the 3-case dataset and the solution
   version with two screens exist; the solution is for `snowflake.native-app/v1`.
4. An ordinary SETH run of the published agent answers case 1 over the Snowflake fake, passing its
   assertions.
5. The FDE seals a release (`shifde.packaging/2`, one model binding `model = "mistral-large2"`); a
   plan of it is refused (409 `evaluation_required`); its evaluation runs to `passed`.
6. A target with `cortex_model: mistral-large2` accepts the plan and the runner seals it; rendering
   the release twice gives the same bytes and the plan's artifact digest. A plan of it on a second
   target, bound to another Cortex model, is refused (409 `cortex_model_mismatch`).
7. The requester's self-approval is refused (403); a second tenant administrator approves.
8. The runner applies the eight steps, observes and verifies the deployment (`app-deep-health`
   passed): `verified`. A second release (a changed screen title) deploys as patch 1.
9. The first plan's stored artifact is unpacked and its `ui/streamlit_app.py` run under Streamlit's
   `AppTest` on a fake Snowpark session of a sample-data account: both screens, every case's answer
   passing its assertions, the table screen's rows, one usage row per answer, and no file with the
   API or web origin, the account host, key material, `shx_`, an `sk-` key or a token.
10. Organization A's administrator gets 404 on B's app request, app preview, target, plan and
    deployment (B's own preview answers 200), and registering B's account as a target is refused
    (409 `platform_already_registered`).
The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `SNOWFLAKE DEMO PASSED` (exit 0) or
`SNOWFLAKE DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a token,
key, password, cookie, database URL or response body. Standard library only at the top level: when
`shifde` is not importable it re-runs itself once in the backend environment
(`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
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
    "only the Snowflake fakes and the fake model provider are supported (no Snowflake account is "
    "configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_SNOWFLAKE_DEMO_RERUN"
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
    """The Snowflake flow, one printed line per step; `StepFailed` names the first failing one."""
    import pytest
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from streamlit.logger import set_log_level

    from shifde import worker_main
    from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
    from shifde.adapters.native.base import create_archive, read_archive
    from shifde.adapters.native.snowflake.adapter import STEPS
    from shifde.app import create_app
    from shifde.appgen.render import render
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.delivery import plans as delivery_plans
    from shifde.evaluation.assertions import evaluate
    from shifde.executor.credentials import snowflake_account
    from shifde.runtime.worker import Worker
    from tests._support.appgen_fixtures import AGENT_SCRIPT, CASE_CALLS, SNOWFLAKE_SAMPLE_DRAFT
    from tests._support.p3util import fake_model_stack
    from tests._support.p5util import fast, start_run, wait_for
    from tests._support.p8util import rate_card, rate_version
    from tests._support.snowpark_session import TOKEN as SESSION_TOKEN, FakeSnowparkSession
    from tests.appgen.helpers import (
        PROJECT,
        SF_SAMPLE,
        SF_TABLES,
        SF_TOKEN,
        body,
        created,
        drafting_model,
        get,
        request_url,
        snowflake,
        wire_snowflake,
    )
    from tests.appgen.rendered import CORTEX_MODEL, SNOWFLAKE_KIND, write_files
    from tests.appgen.test_accept import EXPECTED, THRESHOLDS, _counts
    from tests.appgen.test_generated_snowflake_app import (
        TOOLS,
        App,
        _chat,
        _said,
        seth_answer,
        streamlit_apps,
    )
    from tests.conftest import ORIGIN
    from tests.connections.routes import as_user, system_role_user
    from tests.delivery.conftest import make_executor
    from tests.delivery.routes import (
        SNOWFLAKE_BINDINGS,
        Flow,
        deployments,
        make_due,
        plans,
        register,
        request_plan,
        snowflake_address,
        snowflake_body,
        targets,
    )
    from tests.delivery.snowflake import app_target
    from tests.evaluation.helpers import capture
    from tests.executor.support import (
        ask_plan,
        fake_platform,
        make_runner,
        snowflake_credentials_file,
        snowflake_fake,
        snowflake_key_pem,
    )
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
    cases = SNOWFLAKE_SAMPLE_DRAFT["evaluation_cases"]
    # one account: the read connection's, the delivery target's and the native fake's
    address = snowflake_address()
    account = snowflake_account(address)
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
            a_admin = system_role_user(owner_db, other.tenant, "tenant_administrator")
            return org, other, fde, approver, a_admin

        org, other, fde_email, approver, a_admin = step(
            "1. organization B (project P, an FDE, a second tenant administrator and a tenant "
            "budget) and organization A with its own administrator",
            people,
        )
        base = f"/api/v1/orgs/{org.slug}"
        a_base = f"/api/v1/orgs/{other.slug}"
        with contextlib.ExitStack() as stack:

            def start() -> tuple[Any, Any]:
                client = stack.enter_context(
                    TestClient(
                        create_app(settings),
                        base_url=API_ORIGIN,
                        headers={"Origin": ORIGIN},
                    )
                )
                wire_snowflake(client, account=account, cortex={CORTEX_MODEL: AGENT_SCRIPT})
                # the FDE's own session (a cookie jar of its own)
                fde = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
                return client, fde

            client, fde = step(
                "1. start the API in-process on the sample-data Snowflake account with Cortex "
                "model mistral-large2",
                start,
            )
            h = harness.login(fde, fde_email)

            warehouse = step(
                "1. a Snowflake read connection of B to the sample-data account, tested and "
                "discovered",
                lambda: snowflake(fde, h, org=base, account=account),
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
                # the release's model binding: model `mistral-large2` of the runtime provider,
                # priced by a rate card
                r = fde.post(
                    f"{base}/models",
                    headers=h,
                    json={
                        "provider_id": ids["provider"],
                        "model_key": CORTEX_MODEL,
                        "display_name": "Cortex model",
                        "capabilities": ["chat"],
                        "context_window": 8192,
                        "max_output_tokens": 1024,
                        "lifecycle": "active",
                        "rate_limits": {},
                    },
                )
                need(r.status_code == 201)
                card = rate_card(fde, h, model_key=CORTEX_MODEL, base=base)
                rate_version(
                    fde, h, card["id"], base=base, currency="USD", input_rate=1, output_rate=1
                )
                return ids

            runtime = step(
                "1. a runtime model on a fake provider scripted with AGENT_SCRIPT, and its "
                "priced model mistral-large2",
                runtime_stack,
            )

            def start_worker() -> None:
                app: Any = client.app
                worker = Worker(
                    fast(settings),
                    worker_id=f"snowflake-demo-{time.monotonic_ns()}",
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
                made = created(
                    fde,
                    h,
                    body(warehouse, draft_model, brief=f"Sales app {SF_SAMPLE}", tables=SF_TABLES),
                    org=base,
                )

                def drafted() -> dict[str, Any] | None:
                    out = get(fde, h, made["id"], org=base)
                    return out if out["status"] not in ("queued", "drafting") else None

                out: dict[str, Any] = wait_for(drafted, 60.0)
                need(out["status"] == "drafted")
                need([r["revision_no"] for r in out["revisions"]] == [1])
                need(out["revisions"][0]["draft"] == SNOWFLAKE_SAMPLE_DRAFT)
                return out

            request = step(
                "2. the FDE requests a draft of a [[appgen-sample-snowflake]] brief over the "
                "DEMO_DB tables and the worker drafts it (drafted, revision 1)",
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
                need(out["status"] == "accepted" and out["accepted_revision_no"] == 1)
                return out

            accepted = step("3. the FDE accepts revision 1", accept)
            progress = accepted["accept_progress"]

            def objects() -> dict[str, Any]:
                with owner_db.session() as o:
                    need(_counts(o, request["project_id"]) == EXPECTED)
                    spec: dict[str, Any] = o.execute(
                        text("SELECT spec FROM shifde.solution_versions WHERE id = :v"),
                        {"v": accepted["solution_version_id"]},
                    ).scalar_one()
                    n_cases = o.execute(
                        text(
                            "SELECT jsonb_array_length(cases) FROM shifde.evaluation_datasets "
                            "WHERE id = :d"
                        ),
                        {"d": progress["dataset_id"]},
                    ).scalar_one()
                    kind = o.execute(
                        text("SELECT target_kind FROM shifde.solutions WHERE id = :s"),
                        {"s": progress["solution_id"]},
                    ).scalar_one()
                    o.rollback()
                screens = spec["screens"]
                need(len(screens) == 2 and screens == SNOWFLAKE_SAMPLE_DRAFT["screens"])
                need(n_cases == 3 and kind == SNOWFLAKE_KIND)
                return spec

            version_spec = step(
                "3. the prompt, the 2 tools, the agent, the 3-case dataset and the solution "
                "version with 2 screens exist; the solution is for snowflake.native-app/v1",
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
                "4. an ordinary SETH run of the published agent answers case 1 over the Snowflake "
                "fake and passes its assertions",
                run_case_1,
            )

            versions_url = f"{base}{PROJECT}/solutions/{progress['solution_id']}/versions"

            def seal(version_id: str) -> dict[str, Any]:
                sealed = fde.post(
                    f"{versions_url}/{version_id}/releases",
                    headers=h,
                    json={
                        "environment_bindings": {
                            "model_bindings": {
                                runtime["model"]: {
                                    "provider_id": runtime["provider"],
                                    "model": CORTEX_MODEL,
                                }
                            },
                            "connection_bindings": {warehouse: warehouse},
                        }
                    },
                )
                need(sealed.status_code == 201)
                release: dict[str, Any] = sealed.json()
                manifest = release["manifest"]
                need(manifest["compiler_version"] == "shifde.packaging/2")
                need(manifest["target_kind"] == SNOWFLAKE_KIND)
                bound = list(manifest["bindings"]["model_bindings"].values())
                need([b["model"] for b in bound] == [CORTEX_MODEL])
                return release

            release = step(
                "5. the FDE seals a release under shifde.packaging/2 with the single model "
                "binding model = mistral-large2",
                lambda: seal(accepted["solution_version_id"]),
            )

            executor = step(
                "5. an executor of B with its credential",
                lambda: make_executor(client, org, fde_email),
            )
            flow = Flow(
                client, org.slug, "main", org.tenant, fde_email, approver, executor.token, owner_db
            )
            target = step(
                "5. a Snowflake target on the read connection's account with cortex_model "
                "mistral-large2",
                lambda: app_target(flow, address=address),
            )
            pem = snowflake_key_pem()

            def start_runner() -> tuple[Any, Any]:
                native = snowflake_fake(address, pem)
                need(native.account == account)
                native.data(address).cortex[CORTEX_MODEL] = AGENT_SCRIPT
                credentials = snowflake_credentials_file(workdir, address, pem, tenant=org.tenant)
                runner = make_runner(client, executor.token, credentials, fake_platform(), native)
                return native, runner

            native, runner = step(
                "5. start the executor's runner against the native Snowflake fake of the same "
                "account",
                start_runner,
            )

            def refused(target_id: str, code: str) -> None:
                hh = as_user(client, fde_email)
                answer = request_plan(client, hh, org.slug, target_id, release["id"], status=409)
                client.cookies.clear()
                need(answer["code"] == code)

            step(
                "5. a plan of the release is refused before any evaluation (409 "
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
                "5. an evaluation of the release runs to passed (in-process worker)",
                lambda: evaluation(release["id"]),
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
                need(made["status"] in ("requested", "planning", "queued", "sealing"))
                need(len(resolved) == 1)
                return made

            plan = step(
                "6. the target with cortex_model mistral-large2 accepts a plan of the evaluated "
                "release",
                accepted_plan,
            )

            def seal_plan(plan_id: str) -> dict[str, Any]:
                need(runner.run_once())
                sealed = flow.get_plan(plan_id)
                need(sealed["status"] == "sealed")
                return sealed

            sealed = step("6. the runner seals the plan", lambda: seal_plan(plan["id"]))

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

            def other_model() -> None:
                second = app_target(
                    flow,
                    address=address,
                    bindings={
                        **SNOWFLAKE_BINDINGS,
                        "package": "SHIFDE_OTHER_PKG",
                        "application": "SHIFDE_OTHER_APP",
                        "cortex_model": "llama3.1-70b",
                    },
                )
                refused(second["id"], "cortex_model_mismatch")

            step(
                "6. a plan of the same release on a second target of B bound to another Cortex "
                "model is refused (409 cortex_model_mismatch)",
                other_model,
            )

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

            def approve(value: dict[str, Any]) -> str:
                made = flow.approve(value)["deployment_id"]
                need(made is not None)
                return str(made)

            deployment_id = step(
                "7. a second tenant administrator approves", lambda: approve(sealed)
            )

            def run_to(deployment: str, status: str, due: bool) -> None:
                if due:
                    make_due(owner_db, deployment)
                need(runner.run_once())
                need(flow.get(deployment)["status"] == status)

            def apply_first() -> None:
                run_to(deployment_id, "observing", due=False)
                journal = flow.get(deployment_id)["journal"]
                states = {s["step_key"]: s["state"] for s in journal}
                need(len(STEPS) == 8 and states == dict.fromkeys(STEPS, "complete"))

            def verified(deployment: str) -> None:
                detail = flow.get(deployment)
                checks = [(c["name"], c["status"]) for c in detail["verification"]["checks"]]
                names = ("app-deep-health", "native-version", "runtime-health")
                need(sorted(checks) == [(name, "passed") for name in names])
                need(detail["status"] == "verified")

            step(
                "8. apply on the native fake completes all eight checkpoints of the journal",
                apply_first,
            )
            step(
                "8. observe on the native fake",
                lambda: run_to(deployment_id, "verifying", due=True),
            )
            step(
                "8. verify on the native fake", lambda: run_to(deployment_id, "verified", due=True)
            )
            step(
                "8. deployment verified: exactly native-version, runtime-health and "
                "app-deep-health, all passed",
                lambda: verified(deployment_id),
            )

            def staged(digest: str, path: str) -> bytes:
                stage = native.state(address).stages[SNOWFLAKE_BINDINGS["package"]]
                return bytes(stage["files"][f"{digest}/{path}"]["data"])

            step(
                "8. the staged files hold ui/deep_health.py under the plan's artifact digest",
                lambda: need(bool(staged(plan["artifact_digest"], "ui/deep_health.py"))),
            )
            first_writes = len(native.writes())

            def second_release() -> dict[str, Any]:
                screens = [dict(s) for s in version_spec["screens"]]
                screens[0]["title"] += " 2"
                made = fde.post(
                    versions_url,
                    headers=h,
                    json={
                        "entrypoint_kind": "agent",
                        "entrypoint_version_id": progress["agent_version_id"],
                        "data_reads": version_spec["data_reads"],
                        "evaluation": version_spec["evaluation"],
                        "screens": screens,
                    },
                )
                need(made.status_code == 201)
                again = seal(made.json()["id"])
                need(again["id"] != release["id"] and again["digest"] != release["digest"])
                return again

            second = step(
                "8. a new solution version with a changed screen title is sealed as a second "
                "release",
                second_release,
            )
            step(
                "8. the second release's evaluation runs to passed",
                lambda: evaluation(second["id"]),
            )

            def deploy_second() -> str:
                asked = ask_plan(flow, target["id"], release_id=second["id"])
                deployment = approve(seal_plan(asked["id"]))
                run_to(deployment, "observing", due=False)
                run_to(deployment, "verifying", due=True)
                run_to(deployment, "verified", due=True)
                verified(deployment)
                return str(asked["artifact_digest"])

            second_artifact = step(
                "8. the second release is planned, approved by the second administrator and "
                "deployed (verified)",
                deploy_second,
            )

            def patch_1() -> None:
                writes = "\n".join(native.writes()[first_writes:])
                need("ADD PATCH 1 FOR VERSION V1" in writes)
                need("UPGRADE USING VERSION V1 PATCH 1" in writes)
                need("CREATE APPLICATION" not in writes)
                app = native.state(address).applications[SNOWFLAKE_BINDINGS["application"]]
                need(app["patch"] == 1)
                agent = json.loads(staged(second_artifact, "ui/config/agent.json"))
                need(agent["release_digest"] == second["digest"])

            step(
                "8. the second release is patch 1 of the same package and application (no "
                "CREATE APPLICATION), its staged agent.json naming its release digest",
                patch_1,
            )

            def unpack() -> dict[str, bytes]:
                with owner_db.session() as o:
                    content = o.execute(
                        text("SELECT content FROM shifde.delivery_artifacts WHERE id = :a"),
                        {"a": plan["artifact_id"]},
                    ).scalar_one()
                    o.rollback()
                files = read_archive(bytes(content))
                need("ui/streamlit_app.py" in files and "setup.sql" in files)
                write_files(workdir / "artifact", files)
                return files

            files = step("9. unpack the first plan's stored artifact", unpack)

            def no_origin() -> None:
                # `sk-` as a key prefix: at the start of a token
                key = re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]")
                hidden = [
                    API_ORIGIN,
                    ORIGIN,
                    account,
                    ".snowflakecomputing.com",
                    "-----BEGIN",
                    "shx_",
                    executor.token,
                    SF_TOKEN,
                    SESSION_TOKEN,
                    *pem.splitlines()[1:-1],
                ]
                for content in files.values():
                    need(key.search(content) is None)
                    need(not any(value.encode() in content for value in hidden))

            step(
                "9. no file of the archive holds the API or web origin, the account host, key "
                "material, shx_, an sk- key or a token the demo used",
                no_origin,
            )

            session = FakeSnowparkSession(
                FakeSnowflakeAccount(
                    sample_data=True, cortex={CORTEX_MODEL: AGENT_SCRIPT}, pat=SESSION_TOKEN
                )
            )
            # Streamlit logs its own warnings and a script's exception text: neither is a step line
            set_log_level("CRITICAL")
            with pytest.MonkeyPatch.context() as mp, streamlit_apps(mp) as make_app:

                def generated_app() -> Any:
                    at = make_app(App(workdir / "artifact" / "ui"), session)
                    at.run()
                    need(not at.exception)
                    titles = [s["title"] for s in SNOWFLAKE_SAMPLE_DRAFT["screens"]]
                    need([t.label for t in at.tabs] == titles)
                    return at

                app = step(
                    "9. run the generated streamlit_app.py on a fake Snowpark session: both "
                    "screens are drawn",
                    generated_app,
                )

                def ask(index: int) -> None:
                    question = cases[index]["input"]["message"]
                    _chat(app, question)
                    said = _said(app)
                    need(not app.error and len(said) == 2 * (index + 1))
                    need(said[-2] == ("user", question) and said[-1][0] == "assistant")
                    outcome = evaluate(cases[index]["assertions"], said[-1][1])
                    need(all(a["passed"] for a in outcome))

                for index, case in enumerate(cases):
                    step(
                        f"9. the chat screen answers case {case['case_id']} and passes its "
                        "assertions",
                        lambda index=index: ask(index),
                    )

                def table_screen() -> None:
                    [(number, screen)] = [
                        (n, s)
                        for n, s in enumerate(SNOWFLAKE_SAMPLE_DRAFT["screens"])
                        if s["kind"] == "table"
                    ]
                    name, arguments, _, _ = CASE_CALLS[0]
                    need(screen["tool"] == name)
                    customer = app.number_input(key=f"input-{number}-customer_id")
                    customer.set_value(arguments["customer_id"])
                    since = app.date_input(key=f"input-{number}-since")
                    since.set_value(datetime.date.fromisoformat(arguments["since"]))
                    app.button[0].click().run()
                    need(not app.exception and not app.error)
                    [frame] = app.dataframe
                    expected = seth_answer(TOOLS[name], arguments)
                    need(expected["row_count"] >= 1)
                    need(list(frame.value.columns) == [c["name"] for c in expected["columns"]])
                    need(frame.value.values.tolist() == expected["rows"])

                step(
                    "9. the table screen returns the tool's rows for case 1's arguments",
                    table_screen,
                )

                def usage_rows() -> None:
                    need(len(session.usage) == len(cases))
                    for row in session.usage:
                        need(row["release_digest"] == release["digest"])
                        need(row["model"] == CORTEX_MODEL)

                step(
                    "9. the usage table has one row per answer, each with the first release's "
                    "digest and model mistral-large2",
                    usage_rows,
                )

            def paths(api: str, slug: str) -> list[str]:
                return [
                    request_url(request["id"], api),
                    f"{api}/releases/{release['id']}/app-preview",
                    f"{targets(slug)}/{target['id']}",
                    f"{plans(slug)}/{plan['id']}",
                    f"{deployments(slug)}/{deployment_id}",
                ]

            a = TestClient(client.app, base_url=API_ORIGIN, headers={"Origin": ORIGIN})
            ha = harness.login(a, a_admin)

            def isolation() -> None:
                for url in paths(base, org.slug):
                    need(fde.get(url, headers=h).status_code == 200)
                for url in [*paths(a_base, other.slug), *paths(base, org.slug)]:
                    need(a.get(url, headers=ha).status_code == 404)

            step(
                "10. organization A's administrator gets 404 on B's app request, app preview, "
                "target, plan and deployment under both slugs (200 for B)",
                isolation,
            )

            def claimed() -> None:
                answer = register(
                    a, ha, other.slug, snowflake_body(ws_slug(other), address), status=409
                )
                need(answer["code"] == "platform_already_registered")

            step(
                "10. A registering B's account as a target is refused (409 "
                "platform_already_registered)",
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
        print(f"SNOWFLAKE DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("SNOWFLAKE DEMO FAILED: unexpected error")
        return 1
    print("SNOWFLAKE DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against the Snowflake fakes and the fake model provider",
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"SNOWFLAKE DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"SNOWFLAKE DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
