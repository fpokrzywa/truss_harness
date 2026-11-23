"""Scripted connections demo of the connection management space's "done when" against the
in-process Databricks and Snowflake fakes (design 2026-11-22 section 1, section 12 item 8).

Runs in-process: the API (`shifde.app.create_app`) in a TestClient with test settings and
`dev_fake_data_platforms` on, so `databricks` and `snowflake` connections reach only
`shifde.adapters.dataplatforms.fakes.dev_transport()`; nothing contacts a Databricks workspace or a
Snowflake account. Needs the local cluster running (`python scripts/dev_db.py start`) and this
checkout's `.devdata/dev.env` (`python scripts/dev_db.py env`).

    python scripts/connections_demo.py --fake

The demo creates a scratch database `shifde_demo_<hex>` on the local cluster (credentials from
`SHIFDE_TEST_*_DATABASE_URL`, environment first, then `.devdata/dev.env`), migrates it as the
schema owner and drops it at the end, also on failure; it never uses the checkout's own database.
In it, client A has a workspace with projects P1 and P2 and client B its own workspace; A and B
each have a tenant administrator, and A a member whose only grant is a custom role at P1. A's
administrator creates a Databricks connection in P1 (bearer) and a Snowflake connection in P2
(key pair, a PEM generated at run time), each with an inline credential, then tests, discovers,
edits and rotates each one (same secret, version 2, the test passes again). The P1-only member
reads P1's connection and gets 404 on P2's; binding P1's secret to P2's connection is 422
`secret_scope_mismatch`; the same update as `shifde_app` is refused by the database (23514); B's
administrator gets 404 on both of A's connections. A client-wide model provider and a delivery
target in P1's workspace are set up, and P1's `include_shared` list, the model-providers list and
the delivery-targets list show them. Last, both connections are archived and their owned secrets
are disabled. The test helpers under `backend/tests/` drive each step.

Prints one line per step and ends with `CONNECTIONS DEMO PASSED` (exit 0) or
`CONNECTIONS DEMO FAILED: <step>` (exit 1). Exit 2: the demo could not run. It never prints a
token, PEM, password, cookie, database URL, secret value or response body. Standard library only
at the top level: when `shifde` is not importable it re-runs itself once in the backend
environment (`uv run --frozen --project backend`).
"""

from __future__ import annotations

import argparse
import contextlib
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
    "only the in-process Databricks and Snowflake fakes are supported (no Databricks workspace or "
    "Snowflake account is configured): run with --fake"
)
RERUN_FLAG = "SHIFDE_CONNECTIONS_DEMO_RERUN"
URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)
DATABRICKS_HOST = "adb-1234567890123456.7.azuredatabricks.net"
MEMBER_PERMS = ["workspace:read", "project:read", "connection:read", "connection:test"]


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
    """The connections flow, one printed line per step; `StepFailed` names the first failing one."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from shifde.app import create_app
    from shifde.core.db import Database, set_tenant
    from shifde.core.ids import uuid7
    from shifde.core.settings import Settings
    from tests._support.conntests import run_test
    from tests._support.tenants import add_project, add_tenant, add_workspace
    from tests.conftest import ORIGIN
    from tests.connections.routes import grant, system_role_user
    from tests.delivery.routes import address, register, target_body, targets
    from tests.identity import harness
    from tests.models_admin.routes import fake_model_stack
    from tests.tenancy.conftest import app_session, owner_member

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

    def pem() -> str:
        """An unencrypted PKCS#8 RSA 2048-bit private key, made for this run only."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def token() -> str:
        """A bearer token made for this run only (the dev fake accepts any non-empty one)."""
        return f"dapi-demo-{secrets.token_hex(16)}"

    settings = Settings(
        _env_file=None,
        env="test",
        dev_fake_data_platforms=True,
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="WARNING",
    )
    owner_db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, for setup
    app_db = Database(app_url, pool_size=2)  # `shifde_app`, for the database refusal
    try:

        def world() -> dict[str, Any]:
            with owner_db.session() as o:
                a, b = uuid7(), uuid7()
                add_tenant(o, a, slug=f"demo-a-{a.hex[-12:]}")
                add_tenant(o, b, slug=f"demo-b-{b.hex[-12:]}")
                ws = add_workspace(o, a)
                p1, p2 = add_project(o, a, ws), add_project(o, a, ws)
                add_workspace(o, b)
                member_email = f"p1-member-{uuid7().hex[-12:]}@demo.test"
                member = harness.person(o, member_email)
                owner_member(o, a, member)
                o.commit()
            grant(owner_db, a, member, MEMBER_PERMS, scope_type="project", scope_id=p1)
            return {
                "a": a,
                "b": b,
                "a_slug": f"demo-a-{a.hex[-12:]}",
                "b_slug": f"demo-b-{b.hex[-12:]}",
                "ws": f"w-{ws.hex}",
                "p1": f"p-{p1.hex}",
                "p2": f"p-{p2.hex}",
                "admin_a": system_role_user(owner_db, a, "tenant_administrator"),
                "admin_b": system_role_user(owner_db, b, "tenant_administrator"),
                "member": member_email,
            }

        w = step(
            "two clients A and B; A's workspace with projects P1 and P2; B's workspace; "
            "A's and B's administrators and a P1-only member of A",
            world,
        )
        A, B = f"/api/v1/orgs/{w['a_slug']}", f"/api/v1/orgs/{w['b_slug']}"
        with contextlib.ExitStack() as stack:
            app = create_app(settings)

            def session_for(email: str) -> tuple[Any, dict[str, str]]:
                c = TestClient(app, base_url="https://testserver", headers={"Origin": ORIGIN})
                return c, harness.login(c, email)

            def start() -> tuple[Any, dict[str, str]]:
                c = stack.enter_context(
                    TestClient(app, base_url="https://testserver", headers={"Origin": ORIGIN})
                )
                return c, harness.login(c, w["admin_a"])

            ca, ha = step("start the API in-process with the dev data-platform fakes", start)

            def get(cid: str) -> dict[str, Any]:
                r = ca.get(f"{A}/connections/{cid}", headers=ha)
                need(r.status_code == 200)
                out: dict[str, Any] = r.json()
                return out

            def secret_row(sid: str) -> tuple[str, str, int]:
                with owner_db.session() as o:
                    set_tenant(o, w["a"])
                    row = o.execute(
                        text(
                            "SELECT name, status, secret_version FROM shifde.secrets "
                            "WHERE id = :i"
                        ),
                        {"i": sid},
                    ).one()
                    o.rollback()
                return str(row[0]), str(row[1]), int(row[2])

            def create(body: dict[str, Any], scope: dict[str, str]) -> dict[str, Any]:
                body = {**body, "scope": scope, "access_mode": "read", "read_only_confirmed": True}
                r = ca.post(f"{A}/connections", json=body, headers=ha)
                need(r.status_code == 201)
                made: dict[str, Any] = r.json()
                need(bool(made["secret_id"]) and made["secret_set"] is True)
                name, status, version = secret_row(made["secret_id"])
                need((name, status, version) == (f"connection-{made['id']}", "active", 1))
                return made

            dbx = step(
                "a Databricks connection (bearer, inline credential) created in P1",
                lambda: create(
                    {
                        "name": "lakehouse",
                        "connection_type": "data_platform",
                        "adapter_key": "databricks",
                        "config": {"host": DATABRICKS_HOST},
                        "auth_method": "bearer",
                        "credential": {"value": token()},
                    },
                    {"workspace": w["ws"], "project": w["p1"]},
                ),
            )
            snow = step(
                "a Snowflake connection (key pair, inline credential) created in P2",
                lambda: create(
                    {
                        "name": "warehouse",
                        "connection_type": "data_platform",
                        "adapter_key": "snowflake",
                        "config": {
                            "account": "demoacct",
                            "user": "reader",
                            "warehouse": "COMPUTE_WH",
                        },
                        "auth_method": "key_pair",
                        "credential": {"value": pem()},
                    },
                    {"workspace": w["ws"], "project": w["p2"]},
                ),
            )

            def tested(cid: str) -> None:
                out = run_test(ca, ha, A, cid, tenant_id=w["a"])
                need(out["status"] == "done" and out["result"]["outcome"] == "pass")

            def discovered(cid: str) -> None:
                r = ca.post(f"{A}/connections/{cid}/discover", headers=ha)
                need(r.status_code == 200)
                need(bool(r.json()["payload"]["tables"]))

            def edited(cid: str) -> None:
                name = f"renamed-{uuid7().hex[-8:]}"
                r = ca.patch(
                    f"{A}/connections/{cid}",
                    json={"name": name, "row_version": get(cid)["row_version"]},
                    headers=ha,
                )
                need(r.status_code == 200)
                need(get(cid)["name"] == name)

            def rotated(cid: str, sid: str, value: str) -> None:
                r = ca.put(
                    f"{A}/connections/{cid}/credential",
                    json={
                        "value": value,
                        "row_version": get(cid)["row_version"],
                        "read_only_confirmed": True,
                    },
                    headers=ha,
                )
                need(r.status_code == 200)
                need(get(cid)["secret_id"] == sid)
                need(secret_row(sid) == (f"connection-{cid}", "active", 2))
                tested(cid)

            for label, made, new_value in (
                ("Databricks", dbx, token),
                ("Snowflake", snow, pem),
            ):
                cid, sid = made["id"], made["secret_id"]
                step(f"{label}: test passes", lambda cid=cid: tested(cid))
                step(f"{label}: discover lists tables", lambda cid=cid: discovered(cid))
                step(f"{label}: edit (rename) reads back", lambda cid=cid: edited(cid))
                step(
                    f"{label}: rotate keeps the secret id, version 2, the test passes again",
                    lambda cid=cid, sid=sid, new_value=new_value: rotated(cid, sid, new_value()),
                )

            # ---------------------------------------------------------- isolation
            def member_refused() -> None:
                c, h = session_for(w["member"])
                r = c.get(f"{A}/connections/{dbx['id']}", headers=h)
                need(r.status_code == 200)  # positive control: P1's connection
                need(c.get(f"{A}/connections/{snow['id']}", headers=h).status_code == 404)
                need(c.post(f"{A}/connections/{snow['id']}/test", headers=h).status_code == 404)
                put = c.put(
                    f"{A}/connections/{snow['id']}/credential",
                    json={"value": token(), "row_version": 1, "read_only_confirmed": True},
                    headers=h,
                )
                need(put.status_code == 404)
                q = f"{A}/connections?workspace={w['ws']}&project={w['p1']}&limit=200"
                listed = c.get(q, headers=h)
                need(listed.status_code == 200)
                ids = {x["id"] for x in listed.json()["items"]}
                need(dbx["id"] in ids and snow["id"] not in ids)

            step(
                "P1-only member: reads P1's connection (200); P2's connection is 404 on get, "
                "test and credential, and not in P1's list",
                member_refused,
            )

            def service_refused() -> None:
                before = get(snow["id"])
                need(before["secret_id"] == snow["secret_id"])  # positive control: its own secret
                r = ca.patch(
                    f"{A}/connections/{snow['id']}",
                    json={
                        "secret_id": dbx["secret_id"],
                        "row_version": before["row_version"],
                        "read_only_confirmed": True,
                    },
                    headers=ha,
                )
                need(r.status_code == 422 and r.json()["code"] == "secret_scope_mismatch")
                after = get(snow["id"])
                need(
                    (after["secret_id"], after["row_version"])
                    == (before["secret_id"], before["row_version"])
                )

            step(
                "service: binding P1's secret to P2's connection is 422 secret_scope_mismatch; "
                "P2's connection is unchanged",
                service_refused,
            )

            def database_refused() -> None:
                with app_session(app_db, w["a"]) as s:
                    # positive control: shifde_app sees the row in A's context
                    bound = s.execute(
                        text("SELECT secret_ref_id FROM shifde.connections WHERE id = :i"),
                        {"i": snow["id"]},
                    ).scalar_one()
                    need(str(bound) == snow["secret_id"])
                    try:
                        s.execute(
                            text("UPDATE shifde.connections SET secret_ref_id = :x WHERE id = :i"),
                            {"x": dbx["secret_id"], "i": snow["id"]},
                        )
                    except DBAPIError as exc:
                        sqlstate = getattr(exc.orig, "sqlstate", None)
                    else:
                        sqlstate = None
                    s.rollback()
                need(sqlstate == "23514")
                need(get(snow["id"])["secret_id"] == snow["secret_id"])

            step(
                "database: as shifde_app in A's context, binding P1's secret to P2's connection "
                "is refused (SQLSTATE 23514) and rolled back",
                database_refused,
            )

            def client_b_refused() -> None:
                c, h = session_for(w["admin_b"])
                need(c.get(B, headers=h).status_code == 200)  # positive control: its own org
                for base in (B, A):
                    for made in (dbx, snow):
                        one = f"{base}/connections/{made['id']}"
                        need(c.get(one, headers=h).status_code == 404)
                        need(c.post(f"{one}/test", headers=h).status_code == 404)
                        put = c.put(
                            f"{one}/credential",
                            json={"value": token(), "row_version": 1, "read_only_confirmed": True},
                            headers=h,
                        )
                        need(put.status_code == 404)

            step(
                "client B: B's administrator reads B (200) and gets 404 on get, test and "
                "credential of both of A's connections, under B and under A",
                client_b_refused,
            )

            # ---------------------------------------------------------- the three lists
            stack_ids = step(
                "a client-wide fake model provider and its connection",
                lambda: fake_model_stack(ca, ha, A),
            )

            def add_target() -> Any:
                return register(ca, ha, w["a_slug"], target_body(w["ws"], address()))

            target = step("a delivery target in P1's workspace", add_target)

            def shared_list() -> None:
                q = f"{A}/connections?workspace={w['ws']}&project={w['p1']}&include_shared=true"
                r = ca.get(f"{q}&limit=200", headers=ha)
                need(r.status_code == 200)
                ids = {x["id"] for x in r.json()["items"]}
                need(dbx["id"] in ids and stack_ids["connection"] in ids)
                need(snow["id"] not in ids)

            step(
                "include_shared list of P1: P1's Databricks connection and the client-wide "
                "model connection, not P2's Snowflake connection",
                shared_list,
            )

            def providers_list() -> None:
                r = ca.get(f"{A}/model-providers?limit=200", headers=ha)
                need(r.status_code == 200)
                found = [p for p in r.json()["items"] if p["id"] == stack_ids["provider"]]
                need(len(found) == 1 and found[0]["connection_id"] == stack_ids["connection"])
                scope = get(stack_ids["connection"])["scope"]
                need(scope["workspace"] is None and scope["project"] is None)

            step(
                "model-providers list: the provider, on a client-wide connection", providers_list
            )

            def targets_list() -> None:
                r = ca.get(f"{targets(w['a_slug'])}?limit=200", headers=ha)
                need(r.status_code == 200)
                found = [t for t in r.json()["items"] if t["id"] == target["id"]]
                need(len(found) == 1 and found[0]["workspace"] == w["ws"])

            step("delivery-targets list: the target, in P1's workspace", targets_list)

            # ---------------------------------------------------------- archive
            def archived(cid: str, sid: str) -> None:
                r = ca.post(
                    f"{A}/connections/{cid}/status",
                    json={"status": "retired", "row_version": get(cid)["row_version"]},
                    headers=ha,
                )
                need(r.status_code == 200)
                need(get(cid)["status"] == "retired")
                need(secret_row(sid)[:2] == (f"connection-{cid}", "disabled"))

            for label, made in (("Databricks", dbx), ("Snowflake", snow)):
                step(
                    f"{label}: archive retires the connection and disables its secret",
                    lambda made=made: archived(made["id"], made["secret_id"]),
                )
            check(
                "both connections archived",
                get(dbx["id"])["status"] == get(snow["id"])["status"] == "retired",
            )
    finally:
        app_db.dispose()
        owner_db.dispose()


def run_demo() -> int:
    sys.path.insert(0, str(BACKEND))  # the `tests` helpers
    try:
        with scratch_database() as (app_url, owner_url):
            drive(app_url, owner_url)
    except StepFailed as failed:
        print(f"CONNECTIONS DEMO FAILED: {failed.step}")
        return 1
    except DemoError:
        raise
    except Exception:
        # outside any named step; its text may carry a URL or a response body
        print("FAIL an unexpected error outside the named steps (details withheld)")
        print("CONNECTIONS DEMO FAILED: unexpected error")
        return 1
    print("CONNECTIONS DEMO PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fake", action="store_true", help="run against the in-process data-platform fakes"
    )
    args = parser.parse_args(argv)
    if not args.fake:
        print(f"CONNECTIONS DEMO NOT RUN: {FAKE_ONLY}")
        return 2
    try:
        if not _in_backend_env():
            return rerun_in_backend()
        return run_demo()
    except DemoError as exc:
        print(f"CONNECTIONS DEMO NOT RUN: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
