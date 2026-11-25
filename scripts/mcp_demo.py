"""Scripted MCP access demo of SP8-2 and SP8-3 (design 2026-11-23 sections 3.1 to 3.7): SETH as an
MCP server at `/mcp`, called with the MCP SDK client.

Runs in-process: the API (`shifde.app.create_app`) with test settings and `SHIFDE_MCP_ENABLED` on,
the fake model provider for the agent, and the MCP SDK client (`mcp.client.streamable_http`) over
an `httpx.AsyncClient` with `httpx.ASGITransport`, so nothing goes over a network. Needs the local
cluster running (`python scripts/dev_db.py start`) and this checkout's `.devdata/dev.env`
(`python scripts/dev_db.py env`).

    python scripts/mcp_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner, seeds the demo organizations `demo-a` and `demo-b` and drops the database at the
end, also on failure; it never uses the checkout's own database. A's administrator publishes an
agent over a fake model, switches assist on and installs the `cost` specialist over another, mints
two API keys with `mcp:access` and switches MCP on for A (every v1 tool but `get_deployment`, 40
calls a minute); B's administrator mints a key and leaves MCP off. Then, over MCP: A's key
initializes, calls `whoami`, lists the twelve tools, starts a run and reads it back, asks the
project's specialists (`assist_ask`: routed, with a run), reads the same request and run
(`assist_get`) and its project's board (`board_get`); B's run, release, deployment, project, agent
version and assist request are `not_found` to A, like an unknown id; B's key gets `mcp_disabled`;
`get_deployment` is `tool_not_allowed`; A's second key is rate limited on its 41st call; a session
cookie without a key gets 401 and any `Origin` 403.

Prints one line per step and ends with `MCP DEMO PASSED` (exit 0) or `MCP DEMO FAILED: <step>`
(exit 1). Exit 2: the demo could not run (`MCP DEMO NOT RUN: <reason>`). It never prints a key,
cookie, password, database URL or response body. Standard library only at the top level: when
`shifde` is not importable it re-runs itself once in the backend environment
(`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FAKE_ONLY = (
    "only the in-process app with the fake model provider is supported (no MCP endpoint of a "
    "running installation is configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_MCP_DEMO_RERUN"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)
RATE = 40
HOST = "testserver"


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


def drive(app_url: str, owner_url: str) -> None:
    """The MCP flow, one printed line per step; `StepFailed` names the first failing one."""
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from sqlalchemy import text

    from shifde.app import create_app
    from shifde.cli.seeding import seed_dev_data
    from shifde.core.db import Database
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from shifde.identity.passwords import PasswordService
    from tests._support.assist_fixtures import assist_model
    from tests._support.fullapp import World
    from tests._support.p5util import user_session
    from tests._support.runs import add_agent_version, add_run
    from tests.assist.helpers import enable, installed, seed_proposal
    from tests.delivery.releases import make_release
    from tests.delivery.routes import make_deployment
    from tests.mcp_access.helpers import (
        A,
        ADMIN_A,
        ADMIN_B,
        B,
        ORG_A,
        ORG_B,
        V1_TOOLS,
        agent,
        make_key,
        mcp_client,
        project,
        set_mcp,
        sign_in,
        tenant,
    )

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
        allowed_origins=["http://127.0.0.1:3710"],
        log_format="json",
        log_level="WARNING",
        mcp_enabled=True,
        mcp_allowed_hosts=[HOST],
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    try:

        def seed() -> World:
            with owner_db.session() as o:
                creds = seed_dev_data(o, PasswordService(fast=True))
                o.commit()
            return World(passwords=creds)

        world = step("seed organizations demo-a and demo-b", seed)

        def setup() -> dict[str, Any]:
            with owner_db.session() as o:
                a, b = tenant(o, ORG_A), tenant(o, ORG_B)
                p_a, p_b = project(o, ORG_A), project(o, ORG_B)
                ws_b = o.execute(
                    text("SELECT workspace_id FROM shifde.projects WHERE id = :p"), {"p": p_b}
                ).scalar_one()
                _, version_b = add_agent_version(o, b)
                run_b = add_run(o, b, workspace_id=ws_b, project_id=p_b)
                o.commit()
                request_b, _ = seed_proposal(o, ORG_B, "evaluation_run", {}, email=ADMIN_B)
            theirs = {
                "run_id": str(run_b),
                "release_id": str(make_release(owner_db, b, ws_b, p_b).id),
                "deployment_id": str(make_deployment(owner_db, b)),
                "project_id": str(p_b),
                "agent_version_id": str(version_b),
                "request_id": request_b,
            }
            with mcp_client(settings) as c:
                h = sign_in(c, world, ADMIN_A)
                published = agent(c, h, A, p_a)
                own = user_session(c, world.passwords, ADMIN_A)
                enable(own)
                installed(own, assist_model(own.client, own), keys=["cost"])
                _, key_a = make_key(c, world, ADMIN_A, A, name="demo key")
                _, key_rate = make_key(c, world, ADMIN_A, A, name="demo rate key")
                _, key_b = make_key(c, world, ADMIN_B, B, name="demo key")
                allowed = sorted(V1_TOOLS - {"get_deployment"})
                set_mcp(c, world, ADMIN_A, A, allowed_tools=allowed, rate_per_minute=RATE)
                set_mcp(c, world, ADMIN_B, B, enabled=False)
                sign_in(c, world, ADMIN_A)
                cookie = dict(c.cookies)
            return {
                "a": a,
                "project_a": str(p_a),
                "version_a": published["version"],
                "key_a": key_a,
                "key_rate": key_rate,
                "key_b": key_b,
                "theirs": theirs,
                "cookie": cookie,
            }

        ctx = step(
            "A: agent published, assist on, keys minted, MCP on; B: key minted, MCP off", setup
        )

        async def over_mcp() -> None:
            app = create_app(settings)
            transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 4000))
            base = f"http://{HOST}"

            def http(key: str | None = None, **kw: Any) -> httpx.AsyncClient:
                auth = {"Authorization": f"Bearer {key}"} if key else {}
                return httpx.AsyncClient(transport=transport, base_url=base, headers=auth, **kw)

            async def tool(mcp: ClientSession, name: str, args: dict[str, Any]) -> Any:
                result = await mcp.call_tool(name, args)
                body = json.loads(result.content[0].text)  # type: ignore[union-attr]
                return bool(result.isError), body

            async def raw(client: httpx.AsyncClient, headers: dict[str, str] | None = None) -> Any:
                return await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream", **(headers or {})},
                )

            async def astep(name: str, call: Callable[[], Any]) -> Any:
                try:
                    result = await call()
                except Exception:
                    print(f"FAIL {name}")
                    raise StepFailed(name) from None
                print(f"ok   {name}")
                return result

            try:
                async with app.router.lifespan_context(app):
                    async with (
                        http(ctx["key_a"]) as client,
                        streamable_http_client(f"{base}/mcp", http_client=client) as (r, w, _),
                        ClientSession(r, w) as mcp,
                    ):
                        await astep("A's key: MCP initialize", mcp.initialize)

                        async def whoami() -> None:
                            is_error, me = await tool(mcp, "whoami", {})
                            need(not is_error and me["client_slug"] == ORG_A)
                            need("get_deployment" not in me["tools"])

                        await astep("A's key: whoami names A and its allowed tools", whoami)

                        async def listed() -> None:
                            tools = (await mcp.list_tools()).tools
                            need({t.name for t in tools} == V1_TOOLS and len(tools) == 12)
                            need(all(t.inputSchema["additionalProperties"] is False for t in tools))

                        await astep("A's key: tools/list has exactly the twelve v1 tools", listed)

                        async def run() -> None:
                            is_error, started = await tool(
                                mcp,
                                "start_run",
                                {
                                    "project_id": ctx["project_a"],
                                    "agent_version_id": ctx["version_a"],
                                    "input": "hello from MCP",
                                },
                            )
                            need(not is_error and started["status"] == "pending")
                            is_error, got = await tool(
                                mcp, "get_run", {"run_id": started["run_id"]}
                            )
                            need(not is_error and got["run_id"] == started["run_id"])

                        await astep("A's key: start_run, then get_run reads the run", run)

                        async def asked() -> None:
                            ask = {
                                "project_id": ctx["project_a"],
                                "text": "how much did we spend last week",
                            }
                            is_error, made = await tool(mcp, "assist_ask", ask)
                            need(not is_error and made["status"] == "routed")
                            need(made["run_id"] is not None)
                            is_error, got = await tool(
                                mcp, "assist_get", {"request_id": made["id"]}
                            )
                            need(not is_error and got["id"] == made["id"])
                            need(got["run_id"] == got["run"]["id"] == made["run_id"])

                        await astep(
                            "A's key: assist_ask is routed, then assist_get reads the request "
                            "and its run",
                            asked,
                        )

                        async def board() -> None:
                            is_error, got = await tool(
                                mcp, "board_get", {"project_id": ctx["project_a"]}
                            )
                            need(not is_error)
                            need(
                                [p["project"]["id"] for p in got["projects"]] == [ctx["project_a"]]
                            )

                        await astep("A's key: board_get reads A's project", board)

                        async def theirs() -> None:
                            t = ctx["theirs"]
                            unknown = str(uuid7())
                            run_args = {"input": "x"}
                            cases = [
                                ("get_run", {"run_id": t["run_id"]}, {"run_id": unknown}),
                                (
                                    "get_release",
                                    {"release_id": t["release_id"]},
                                    {"release_id": unknown},
                                ),
                                (
                                    "list_agents",
                                    {"project_id": t["project_id"]},
                                    {"project_id": unknown},
                                ),
                                (
                                    "start_run",
                                    {
                                        "project_id": t["project_id"],
                                        "agent_version_id": t["agent_version_id"],
                                        **run_args,
                                    },
                                    {
                                        "project_id": unknown,
                                        "agent_version_id": unknown,
                                        **run_args,
                                    },
                                ),
                                (
                                    "board_get",
                                    {"project_id": t["project_id"]},
                                    {"project_id": unknown},
                                ),
                                (
                                    "assist_ask",
                                    {"project_id": t["project_id"], "text": "what did we spend"},
                                    {"project_id": unknown, "text": "what did we spend"},
                                ),
                                (
                                    "assist_get",
                                    {"request_id": t["request_id"]},
                                    {"request_id": unknown},
                                ),
                            ]
                            for name, mine, missing in cases:
                                answer = await tool(mcp, name, mine)
                                need(answer[0] and answer[1]["error"] == "not_found")
                                need(answer == await tool(mcp, name, missing))
                            is_error, deployments = await tool(mcp, "list_deployments", {})
                            need(not is_error and t["deployment_id"] not in json.dumps(deployments))

                        await astep(
                            "A's key: B's ids, its project and assist request too, are not_found "
                            "like unknown ids",
                            theirs,
                        )

                        async def not_allowed() -> None:
                            is_error, body = await tool(
                                mcp, "get_deployment", {"deployment_id": str(uuid7())}
                            )
                            need(is_error and body["error"] == "tool_not_allowed")

                        await astep("A's key: get_deployment is tool_not_allowed", not_allowed)

                    async def disabled() -> None:
                        async with http(ctx["key_b"]) as client:
                            r = await raw(client)
                        need(r.status_code == 403 and r.json() == {"error": "mcp_disabled"})

                    await astep("B's key: 403 mcp_disabled", disabled)

                    async def rate() -> None:
                        async with (
                            http(ctx["key_rate"]) as client,
                            streamable_http_client(f"{base}/mcp", http_client=client) as (r, w, _),
                            ClientSession(r, w) as mcp,
                        ):
                            await mcp.initialize()
                            for _ in range(RATE):
                                need(not (await tool(mcp, "whoami", {}))[0])
                            is_error, body = await tool(mcp, "whoami", {})
                            need(is_error and body["error"] == "rate_limited")
                            need(isinstance(body["retry_after"], int))

                    await astep(f"A's second key: call {RATE + 1} is rate_limited", rate)

                    async def cookie() -> None:
                        async with http(cookies=ctx["cookie"]) as client:
                            r = await raw(client)
                        need(r.status_code == 401 and r.json() == {"error": "invalid_credentials"})

                    await astep("a session cookie without a key: 401", cookie)

                    async def origin() -> None:
                        async with http(ctx["key_a"]) as client:
                            r = await raw(client, {"Origin": "https://evil.example"})
                        need(r.status_code == 403 and r.json() == {"error": "origin_not_allowed"})

                    await astep("a request with an Origin header: 403", origin)
            finally:
                app.state.db.dispose()

        anyio.run(over_mcp)

        def audited() -> None:
            with owner_db.session() as o:
                rows = o.execute(
                    text(
                        "SELECT tenant_id, action FROM shifde.audit_events "
                        "WHERE action LIKE 'mcp.%'"
                    )
                ).all()
            in_a = [r.action for r in rows if r.tenant_id == ctx["a"]]
            need("mcp.tool_called" in in_a and "mcp.access_refused" in in_a)
            need(any(r.action == "mcp.access_refused" and r.tenant_id != ctx["a"] for r in rows))

        step("audit: tool calls and refusals filed in each key's own client", audited)
    finally:
        owner_db.dispose()


def run_demo() -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    try:
        with scratch_database() as (app_url, owner_url):
            drive(app_url, owner_url)
    except StepFailed as failed:
        print(f"MCP DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("MCP DEMO FAILED: unexpected error")
        return 1
    print("MCP DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake", action="store_true", help="run in-process with the fake model provider"
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"MCP DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"MCP DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
