"""Sealed releases (issue #79; sub-project 3 design section 5, settled calls 11 to 15): sealing,
idempotent re-sealing, the `databricks.app/v1` compatibility check and each refusal reason, the
manifest digest, withdrawing, the `releases` guard and the retire guard on pinned build versions,
the redaction of pins a reader cannot see, the scope rule at version create, and who may seal and
withdraw."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.models_admin.repository import SHI_PROVIDER_ID
from shifde.packaging.canonical import digest
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, system_role
from tests._support.p3util import (
    ADMIN,
    PLATFORM,
    REST_TOKEN,
    VIEWER,
    A,
    activate,
    connection,
    pg_body,
    rest_body,
    secret,
)
from tests._support.p4util import (
    PASSWORD,
    agent_spec,
    art,
    builtin_tool_spec,
    edit,
    make_user,
    prompt_spec,
    publish,
)
from tests._support.p5util import Hdr, user_session
from tests._support.p6util import mini, sink_artifact
from tests.packaging.helpers import (
    Stack,
    bindings,
    databricks_connection,
    dataset,
    new_version,
    releases_url,
    seal,
    solution,
    solutions,
    stack,
    version_body,
)
from tests.tenancy.conftest import app_session

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DEVELOPER = "dev@demo-a.local"
CLIENT_MODELS = "a deployed application uses models served in the client's workspace"
NOT_DATABRICKS = (
    "the bound connection is not a Databricks connection of this organization in the solution's "
    "scope"
)


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _count(owner: Session, sql: str = "SELECT count(*) FROM shifde.releases", **p: Any) -> int:
    n = int(owner.execute(text(sql), p).scalar_one())
    owner.rollback()
    return n


def _audits(owner: Session, action: str) -> list[Any]:  # Any: audit rows
    rows = owner.execute(
        text(
            "SELECT outcome, target_type, details FROM shifde.audit_events WHERE action = :a "
            "ORDER BY occurred_at"
        ),
        {"a": action},
    ).all()
    owner.rollback()
    return list(rows)


def _diagnostics(r: httpx.Response) -> dict[str, dict[str, Any]]:
    """The refusal's report by path (asserting the `release_incompatible` shape)."""
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "release_incompatible", body
    assert body["detail"] == "The release is not compatible with its target."
    out = {}
    for e in body["errors"]:
        assert e["mandatory"] is True and e["type"] == e["status"]
        assert e["loc"] == ["compatibility", *e["path"].split(".")]
        assert e["msg"] == (e["reason"] or "supported")
        out[e["path"]] = e
    return out


def _unsupported(r: httpx.Response) -> dict[str, str]:
    return {p: d["reason"] for p, d in _diagnostics(r).items() if d["status"] == "unsupported"}


def _setup(
    client: TestClient, world: World, name: str = "Sales assistant"
) -> tuple[Hdr, Stack, str]:
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    return h, s, solution(h.client, h, name)["id"]


def _own_bindings(s: Stack, **connections: str) -> dict[str, Any]:
    return bindings({s.model: s.provider}, dict(connections))


# ------------------------------------------------------------------ seal, re-seal, list, get
def test_seal_reseal_and_list(client: TestClient, world: World, owner_session: Session) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]

    first = seal(h.client, h, sid, vid, _own_bindings(s))
    assert first.status_code == 201, first.text
    rel = first.json()
    assert HEX64.match(rel["digest"]) and rel["digest"] == digest(rel["manifest"])
    assert rel["status"] == "sealed" and rel["workspace"] == "main" and rel["project"] == "sample"
    assert rel["solution_id"] == sid and rel["solution_version_id"] == vid
    assert rel["version_no"] == 1
    m = rel["manifest"]
    assert m["schema_version"] == 1 and m["compiler_version"] == "shifde.packaging/1"
    assert m["target_kind"] == "databricks.app/v1" and m["solution_version_id"] == vid
    assert m["entrypoint"] == {"kind": "agent", "version_id": s.agent_v1, "spec_hash": s.agent_hash}
    assert m["pins"] == [{"role": "prompt", "version_id": s.prompt_v1, "spec_hash": s.prompt_hash}]
    assert m["bindings"]["model_bindings"] == {
        s.model: {"provider_id": s.provider, "model": "served-chat"}
    }
    assert m["evaluation"]["dataset_id"] == s.dataset
    assert m["evaluation"]["dataset_digest"] == s.dataset_digest
    assert m["evaluation"]["thresholds"] == {
        "min_accuracy": "0.9",
        "max_p95_latency_ms": 2000,
        "max_total_cost_usd": "1.5",
    }
    assert "digest" not in m and "sealed_at" not in m and "sealed_by" not in m
    assert [(d["path"], d["status"]) for d in rel["compatibility"]] == [
        ("entrypoint", "supported"),
        (f"models.{s.model}", "supported"),
        ("evaluation", "supported"),
    ]

    again = seal(h.client, h, sid, vid, _own_bindings(s))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == rel["id"] and again.json()["digest"] == rel["digest"]
    assert _count(owner_session) == 1

    other = seal(h.client, h, sid, vid, bindings({s.model: s.provider}, served="other-chat"))
    assert other.status_code == 201, other.text
    assert other.json()["id"] != rel["id"] and other.json()["digest"] != rel["digest"]
    assert _count(owner_session) == 2

    listed = h.client.get(releases_url(sid, vid), headers=h)
    assert listed.status_code == 200, listed.text
    assert [x["id"] for x in listed.json()["items"]] == [other.json()["id"], rel["id"]]
    paged = h.client.get(releases_url(sid, vid), headers=h, params={"limit": 1}).json()
    assert [x["id"] for x in paged["items"]] == [other.json()["id"]] and paged["next_cursor"]
    got = h.client.get(f"{A}/releases/{rel['id']}", headers=h)
    assert got.status_code == 200 and got.json() == rel

    # re-sealing a withdrawn release finds it, unchanged
    withdrawn = h.client.post(f"{A}/releases/{rel['id']}/withdraw", headers=h, json={"note": "x"})
    assert withdrawn.status_code == 200, withdrawn.text
    resealed = seal(h.client, h, sid, vid, _own_bindings(s))
    assert resealed.status_code == 200 and resealed.json() == withdrawn.json()
    assert _count(owner_session) == 2

    sealed = _audits(owner_session, "packaging.release.sealed")
    assert len(sealed) == 2  # the re-seals wrote none
    outcome, target_type, details = sealed[0]
    assert outcome == "success" and target_type == "packaging_release"
    assert details["digest"] == rel["digest"] and details["solution_version_id"] == vid
    assert details["version_no"] == 1


def test_missing_evaluation_is_refused(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, evaluation=False))["id"]
    r = seal(h.client, h, sid, vid, _own_bindings(s))
    report = _diagnostics(r)
    assert report["evaluation"]["status"] == "unsupported"
    assert report["evaluation"]["reason"] == "a release needs an evaluation dataset"
    assert report["entrypoint"]["status"] == "supported"
    assert _count(owner_session) == 0
    [(outcome, target_type, details)] = _audits(owner_session, "packaging.release.refused")
    assert outcome == "failure" and target_type == "packaging_solution_version"
    assert details["unsupported"] == ["evaluation"] and details["solution_version_id"] == vid


def test_unknown_binding_keys_are_a_validation_error(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    nobody = str(uuid7())
    for body, loc in (
        (
            bindings({s.model: s.provider, nobody: s.provider}),
            ["body", "environment_bindings", "model_bindings", nobody],
        ),
        (
            bindings({s.model: s.provider}, {nobody: s.connection}),
            ["body", "environment_bindings", "connection_bindings", nobody],
        ),
    ):
        r = seal(h.client, h, sid, vid, body)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", r.text
        assert [e["loc"] for e in r.json()["errors"]] == [loc]
    assert _count(owner_session) == 0


def test_deprecated_entrypoint_is_unsupported(client: TestClient, world: World) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    post(
        h.client,
        f"{A}/agents/{s.agent}/versions/{s.agent_v1}/state",
        h,
        {"state": "deprecated"},
        200,
    )
    assert _unsupported(seal(h.client, h, sid, vid, _own_bindings(s))) == {
        "entrypoint": "the entrypoint is no longer a published agent or pipeline version"
    }


# ------------------------------------------------------------------ tools
def _sql_tool(h: Hdr, sample_db_url: str, scope: dict[str, str]) -> tuple[str, str]:
    """A published read-only SQL tool over an active PostgreSQL connection: (version, conn)."""
    pg_password = sample_db_url.split(":")[2].split("@")[0]
    sid = secret(h.client, h, "pg-pass", pg_password)
    conn = connection(h.client, h, pg_body(sample_db_url, sid))
    activate(h.client, h, conn["id"])
    spec = {
        "executor": "connection.sql",
        "binding": {"connection_id": conn["id"], "sql_template": "SELECT name FROM employees"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "required": ["columns", "rows"]},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 10,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = art(h.client, h, "tools", "by-sql", spec, scope)
    return str(publish(h.client, h, "tools", tool["id"])["id"]), str(conn["id"])


def _rest_tool(h: Hdr, server: RestTestServer, scope: dict[str, str]) -> tuple[str, str]:
    """A published read REST tool over an active REST connection: (version, connection)."""
    sid = secret(h.client, h, "rest-token", REST_TOKEN)
    conn = connection(h.client, h, rest_body(server, sid))
    activate(h.client, h, conn["id"])
    spec = {
        "executor": "rest.http",
        "binding": {"connection_id": conn["id"], "method": "GET", "path_template": "/echo"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "required_permissions": ["connection:use"],
        "invocation_policy": {},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = art(h.client, h, "tools", "by-rest", spec, scope)
    return str(publish(h.client, h, "tools", tool["id"])["id"]), str(conn["id"])


def test_each_tool_refusal_reason(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    h, s, sid = _setup(client, world)
    scope = {"type": "project", "id": s.project_id}
    tools: dict[str, str] = {}
    for key, spec in (
        ("writer", builtin_tool_spec(side_effect_class="write_non_idempotent")),
        ("approver", builtin_tool_spec(invocation_policy={"require_approval": True})),
        ("builtin", builtin_tool_spec()),
    ):
        made = art(h.client, h, "tools", key, spec, scope)
        tools[key] = publish(h.client, h, "tools", made["id"])["id"]
    tools["rest"], rest_conn = _rest_tool(h, rest_server, scope)
    tools["sql"], pg_conn = _sql_tool(h, sample_db_url, scope)
    spec = agent_spec(f"latest:{s.prompt}", s.model, tool_version_ids=sorted(tools.values()))
    agent = art(h.client, h, "agents", "tooled", spec, scope)
    agent_v1 = publish(h.client, h, "agents", agent["id"])["id"]
    vid = new_version(h.client, h, sid, version_body(agent_v1, dataset=s.dataset))["id"]
    lake = databricks_connection(h.client, h)

    r = seal(h.client, h, sid, vid, _own_bindings(s, **{rest_conn: lake, pg_conn: lake}))
    report = _diagnostics(r)
    assert {p: d["reason"] for p, d in report.items() if p.startswith("tools.")} == {
        f"tools.{tools['writer']}": "writes",
        f"tools.{tools['approver']}": "needs a human approval step",
        f"tools.{tools['builtin']}": "not on the read-only list",
        f"tools.{tools['rest']}": "calls an outside service",
        f"tools.{tools['sql']}": None,  # connection.sql, read: on the read-only list
    }
    assert report[f"connections.{pg_conn}"]["status"] == "supported"
    assert report[f"connections.{rest_conn}"]["status"] == "supported"
    paths = list(report)
    tool_paths = [p for p in paths if p.startswith("tools.")]
    assert paths[0] == "entrypoint" and paths[-1] == "evaluation"
    assert tool_paths == sorted(tool_paths) and paths[1 : 1 + len(tool_paths)] == tool_paths
    assert _count(owner_session) == 0


# ------------------------------------------------------------------ models
def _shared_model(client: TestClient, world: World, owner: Session) -> str:
    """The shared SHI provider (active) and one shared model, with demo-a's switch on."""
    model = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, settings, "
            "status) VALUES (:p, NULL, 'SHI', 'fake', '{}'::jsonb, 'active') "
            "ON CONFLICT (id) DO UPDATE SET status = 'active'"
        ),
        {"p": SHI_PROVIDER_ID},
    )
    owner.execute(
        text(
            "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name, "
            "capabilities, lifecycle) VALUES (:m, NULL, :p, 'shi-chat', 'SHI chat', "
            "ARRAY['chat'], 'active')"
        ),
        {"m": model, "p": SHI_PROVIDER_ID},
    )
    owner.commit()
    platform = user_session(client, world.passwords, PLATFORM)
    r = platform.client.put(
        "/api/v1/platform/orgs/demo-a/shi-provider-access", headers=platform, json={"enabled": True}
    )
    assert r.status_code == 200, r.text
    return str(model)


def test_each_model_refusal_reason(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    pin = f"models.{s.model}"
    shared = _shared_model(client, world, owner_session)
    for body, reason in (
        (bindings(), "no model binding"),
        (bindings({s.model: str(SHI_PROVIDER_ID)}), CLIENT_MODELS),
        (
            bindings({s.model: str(uuid7())}),
            "the provider is not a model provider of this organization",
        ),
    ):
        assert _unsupported(seal(h.client, h, sid, vid, body)) == {pin: reason}

    # an agent on a shared SHI model: refused even when bound to the client's own provider
    scope = {"type": "project", "id": s.project_id}
    spec = agent_spec(f"latest:{s.prompt}", shared)
    agent = art(h.client, h, "agents", "on-shi", spec, scope)
    shi_v1 = publish(h.client, h, "agents", agent["id"])["id"]
    shi_vid = new_version(h.client, h, sid, version_body(shi_v1, dataset=s.dataset))["id"]
    r = seal(h.client, h, sid, shi_vid, bindings({shared: s.provider}))
    assert _unsupported(r) == {f"models.{shared}": CLIENT_MODELS}
    assert _count(owner_session) == 0


# ------------------------------------------------------------------ connections
def test_each_connection_refusal_reason(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(
        h.client, h, sid, version_body(s.agent_v1, connection=s.connection, dataset=s.dataset)
    )["id"]
    pin = f"connections.{s.connection}"
    assert _unsupported(seal(h.client, h, sid, vid, _own_bindings(s))) == {
        pin: "no connection binding"
    }
    # the fake model connection is readable and in scope, but not a Databricks connection
    not_lake = _own_bindings(s, **{s.connection: s.connection})
    assert _unsupported(seal(h.client, h, sid, vid, not_lake)) == {pin: NOT_DATABRICKS}
    # a Databricks connection of another workspace is out of the solution's scope
    post(h.client, f"{A}/workspaces", h, {"slug": "other", "name": "Other"})
    away = databricks_connection(h.client, h, "away", scope={"workspace": "other"})
    assert _unsupported(seal(h.client, h, sid, vid, _own_bindings(s, **{s.connection: away}))) == {
        pin: NOT_DATABRICKS
    }
    lake = databricks_connection(h.client, h)
    ok = seal(h.client, h, sid, vid, _own_bindings(s, **{s.connection: lake}))
    assert ok.status_code == 201, ok.text
    assert ok.json()["manifest"]["data_reads"] == [
        {"connection_id": s.connection, "tables": ["main.sales.orders"]}
    ]
    assert _count(owner_session) == 1


# ------------------------------------------------------------------ pipelines
def _pipeline(h: Hdr, s: Stack, key: str, nodes: list[dict[str, Any]]) -> str:
    params = {"type": "object", "properties": {"term": {"type": "string"}}}
    spec = mini(nodes, [], params=params)
    made = art(h.client, h, "pipelines", key, spec, {"type": "project", "id": s.project_id})
    return str(publish(h.client, h, "pipelines", made["id"])["id"])


def _model_node(model: str) -> dict[str, Any]:
    return {
        "id": "mdl",
        "type": "model",
        "config": {"model_id": model, "system": "Be brief."},
        "inputs": {"prompt": "$params.term"},
    }


def test_pipeline_model_and_connection_refs(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    h, s, sid = _setup(client, world)
    secret_id = secret(h.client, h, "rest-token", REST_TOKEN)
    rest = connection(h.client, h, rest_body(rest_server, secret_id))["id"]
    activate(h.client, h, rest)
    api = {
        "id": "api",
        "type": "api_request",
        "config": {"connection_id": rest, "method": "GET", "path_template": "/echo"},
        "inputs": {"term": "$params.term"},
    }
    own = _pipeline(h, s, "own-pipe", [_model_node(s.model), api])
    vid = new_version(h.client, h, sid, version_body(own, kind="pipeline", dataset=s.dataset))["id"]
    assert _unsupported(seal(h.client, h, sid, vid, bindings())) == {
        f"models.{s.model}": "no model binding",
        f"connections.{rest}": "no connection binding",
    }
    lake = databricks_connection(h.client, h)
    ok = seal(h.client, h, sid, vid, _own_bindings(s, **{rest: lake}))
    assert ok.status_code == 201, ok.text
    assert ok.json()["manifest"]["entrypoint"]["kind"] == "pipeline"

    shared = _shared_model(client, world, owner_session)
    on_shi = _pipeline(h, s, "shi-pipe", [_model_node(shared)])
    shi_vid = new_version(
        h.client, h, sid, version_body(on_shi, kind="pipeline", dataset=s.dataset)
    )["id"]
    r = seal(h.client, h, sid, shi_vid, bindings({shared: s.provider}))
    assert _unsupported(r) == {f"models.{shared}": CLIENT_MODELS}
    assert _count(owner_session) == 1


# ------------------------------------------------------------------ withdraw, retire guard
def test_withdraw_then_conflict_and_retire_guard(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    rel = seal(h.client, h, sid, vid, _own_bindings(s)).json()

    def state(kind: str, artifact: str, version: str, to: str) -> httpx.Response:
        return h.client.post(
            f"{A}/{kind}/{artifact}/versions/{version}/state", headers=h, json={"state": to}
        )

    for kind, artifact, version in (
        ("agents", s.agent, s.agent_v1),  # the entrypoint
        ("prompts", s.prompt, s.prompt_v1),  # a transitive pin
    ):
        assert state(kind, artifact, version, "deprecated").status_code == 200
        refused = state(kind, artifact, version, "retired")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == "a sealed release pins this version"

    url = f"{A}/releases/{rel['id']}/withdraw"
    done = h.client.post(url, headers=h, json={"note": "superseded"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "withdrawn" and done.json()["digest"] == rel["digest"]
    again = h.client.post(url, headers=h, json={"note": "again"})
    assert again.status_code == 409, again.text
    for bad in ({}, {"note": ""}, {"note": "x", "extra": 1}):
        assert h.client.post(url, headers=h, json=bad).status_code == 422
    [(outcome, target_type, details)] = _audits(owner_session, "packaging.release.withdrawn")
    assert outcome == "success" and target_type == "packaging_release"
    assert details == {**details, "digest": rel["digest"], "note": "superseded"}

    for kind, artifact, version in (
        ("agents", s.agent, s.agent_v1),
        ("prompts", s.prompt, s.prompt_v1),
    ):
        retired = state(kind, artifact, version, "retired")
        assert retired.status_code == 200, retired.text


# ------------------------------------------------------------------ the database
def _release_row(client: TestClient, world: World) -> tuple[str, str]:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    r = seal(h.client, h, sid, vid, _own_bindings(s))
    assert r.status_code == 201, r.text
    return vid, r.json()["id"]


def _tenant(owner: Session) -> UUID:
    tenant: UUID = owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = 'demo-a'")
    ).scalar_one()
    return tenant


def test_releases_guard_for_the_owner_and_the_app(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    vid, rid = _release_row(client, world)
    tenant = _tenant(owner_session)
    insert = (
        "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, manifest, "
        "compatibility, status, sealed_by) VALUES (:t, :i, :v, repeat('b', 64), '{}', '[]', "
        "'withdrawn', :t)"
    )
    changes = {
        "digest": "UPDATE shifde.releases SET digest = repeat('0', 64) WHERE id = :r",
        "manifest": "UPDATE shifde.releases SET manifest = '{}' WHERE id = :r",
        "delete": "DELETE FROM shifde.releases WHERE id = :r",
    }

    def owner_refused(sql: str, **params: Any) -> None:
        set_tenant(owner_session, tenant)
        with pytest.raises(DBAPIError) as err:  # the guard, for the schema owner too
            owner_session.execute(text(sql), params)
        owner_session.rollback()
        assert _sqlstate(err.value) == "23514", (sql, err.value)

    def app_refused(sql: str, want: str, **params: Any) -> None:
        with app_session(app_db, tenant) as app, pytest.raises(DBAPIError) as err:
            app.execute(text(sql), params)
        assert _sqlstate(err.value) == want, (sql, err.value)

    for sql in changes.values():
        owner_refused(sql, r=rid)
        # the app role holds no UPDATE (digest, manifest) or DELETE grant: refused before the
        # guard runs
        app_refused(sql, "42501", r=rid)
    owner_refused(insert, i=uuid7(), v=vid, t=tenant)
    app_refused(insert, "23514", i=uuid7(), v=vid, t=tenant)

    # withdrawn -> sealed is refused; sealed -> withdrawn (status only) is the one update
    with app_session(app_db, tenant) as app:
        app.execute(
            text("UPDATE shifde.releases SET status = 'withdrawn' WHERE id = :r"), {"r": rid}
        )
        app.commit()
    back = "UPDATE shifde.releases SET status = 'sealed' WHERE id = :r"
    owner_refused(back, r=rid)
    app_refused(back, "23514", r=rid)
    row = owner_session.execute(
        text("SELECT status, updated_at IS NOT NULL FROM shifde.releases WHERE id = :r"),
        {"r": rid},
    ).one()
    assert tuple(row) == ("withdrawn", True)
    assert _count(owner_session) == 1


def test_release_key_restricts_deletes_and_table_is_isolated(owner_session: Session) -> None:
    deltype = owner_session.execute(
        text(
            "SELECT confdeltype FROM pg_catalog.pg_constraint "
            "WHERE conname = 'fk_releases__solution_version'"
        )
    ).scalar_one()
    assert deltype == "r"
    row = owner_session.execute(
        text(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid "
            "AND p.polname = 'tenant_isolation') FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'shifde' AND c.relname = 'releases'"
        )
    ).one()
    assert tuple(row) == (True, True, True)


# ------------------------------------------------------------------ readers and roles
def test_hidden_pins_are_redacted_for_a_reader(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """A developer sees the agent but not the administrator's private prompt it pins."""
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    scope = {"type": "project", "id": s.project_id}
    secret_prompt = art(
        h.client, h, "prompts", "private-greet", prompt_spec(), scope, visibility="private"
    )
    prompt_v1 = publish(h.client, h, "prompts", secret_prompt["id"])
    spec = agent_spec(f"latest:{secret_prompt['id']}", s.model)
    agent = art(h.client, h, "agents", "discreet", spec, scope)
    agent_v1 = publish(h.client, h, "agents", agent["id"])["id"]
    sid = solution(h.client, h, "Discreet")["id"]
    vid = new_version(h.client, h, sid, version_body(agent_v1, dataset=s.dataset))["id"]
    rid = seal(h.client, h, sid, vid, _own_bindings(s)).json()["id"]

    make_user(client, owner_session, h, DEVELOPER, "developer", world.passwords)
    dev = user_session(client, world.passwords, DEVELOPER)
    hidden = {"role": "prompt", "version_id": None, "spec_hash": None}
    version = dev.client.get(f"{solutions()}/{sid}/versions/{vid}", headers=dev)
    assert version.status_code == 200, version.text
    assert version.json()["pins"] == [hidden]
    assert version.json()["entrypoint"]["version_id"] == agent_v1
    release = dev.client.get(f"{A}/releases/{rid}", headers=dev)
    assert release.status_code == 200, release.text
    assert release.json()["manifest"]["pins"] == [hidden]
    assert release.json()["manifest"]["entrypoint"]["version_id"] == agent_v1

    shown = {"role": "prompt", "version_id": prompt_v1["id"], "spec_hash": prompt_v1["spec_hash"]}
    admin_version = h.client.get(f"{solutions()}/{sid}/versions/{vid}", headers=h).json()
    assert admin_version["pins"] == [shown]
    assert h.client.get(f"{A}/releases/{rid}", headers=h).json()["manifest"]["pins"] == [shown]
    stored = owner_session.execute(
        text("SELECT manifest FROM shifde.releases WHERE id = :r"), {"r": rid}
    ).scalar_one()
    assert stored["pins"] == [shown]  # the stored row is not redacted


def test_who_may_seal_and_withdraw(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    rid = seal(h.client, h, sid, vid, _own_bindings(s)).json()["id"]
    viewer = user_session(client, world.passwords, VIEWER)
    for method, url, body in (
        ("POST", releases_url(sid, vid), _own_bindings(s)),
        ("POST", f"{A}/releases/{rid}/withdraw", {"note": "no"}),
    ):
        r = viewer.client.request(method, url, headers=viewer, json=body)
        assert r.status_code in (403, 404), (url, r.status_code, r.text)

    make_user(client, owner_session, h, DEVELOPER, "developer", world.passwords)
    dev = user_session(client, world.passwords, DEVELOPER)
    sealed = seal(dev.client, dev, sid, vid, bindings({s.model: s.provider}, served="dev-chat"))
    assert sealed.status_code == 201, sealed.text
    refused = dev.client.post(f"{A}/releases/{rid}/withdraw", headers=dev, json={"note": "no"})
    assert refused.status_code == 403, refused.text  # packaging:release:withdraw is not granted
    assert h.client.get(f"{A}/releases/{rid}", headers=h).json()["status"] == "sealed"


def test_seal_refuses_a_version_of_another_solution_and_an_archived_solution(
    client: TestClient, world: World
) -> None:
    h, s, sid = _setup(client, world)
    other = solution(h.client, h, "Other")["id"]
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    wrong = seal(h.client, h, other, vid, _own_bindings(s))
    assert wrong.status_code == 404 and wrong.json()["detail"] == "solution version not found"
    listed = h.client.get(releases_url(other, vid), headers=h)
    assert listed.status_code == 404, listed.text
    assert h.client.post(f"{solutions()}/{sid}/archive", headers=h).status_code == 200
    assert seal(h.client, h, sid, vid, _own_bindings(s)).status_code == 409


# ------------------------------------------------------------------ the scope rule
def test_version_entrypoint_and_connections_must_be_in_the_solutions_scope(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, s, sid = _setup(client, world)
    url = f"{solutions()}/{sid}/versions"
    main = h.client.get(f"{A}/workspaces/main", headers=h).json()["id"]
    post(h.client, f"{A}/workspaces", h, {"slug": "other", "name": "Other"})
    other_ws = h.client.get(f"{A}/workspaces/other", headers=h).json()["id"]
    side = post(h.client, f"{A}/workspaces/main/projects", h, {"slug": "side", "name": "Side"})
    tenant = str(_tenant(owner_session))

    def agent_at(key: str, scope: dict[str, str]) -> str:
        made = art(h.client, h, "agents", key, agent_spec(f"latest:{s.prompt}", s.model), scope)
        return str(publish(h.client, h, "agents", made["id"])["id"])

    # the prompt is project-scoped: an agent elsewhere pins it only if it can see it; the
    # administrator can, so the agent publishes and only the solution's scope decides
    org_wide = agent_at("org-wide", {"type": "tenant", "id": tenant})
    same_ws = agent_at("same-ws", {"type": "workspace", "id": main})
    away_ws = agent_at("away-ws", {"type": "workspace", "id": other_ws})
    away_proj = agent_at("away-proj", {"type": "project", "id": side["id"]})
    for accepted in (org_wide, same_ws):
        assert (
            h.client.post(
                url, headers=h, json=version_body(accepted, dataset=s.dataset)
            ).status_code
            == 201
        )

    def answer(r: httpx.Response) -> tuple[int, Any]:  # Any: the problem body
        return r.status_code, {
            k: v for k, v in r.json().items() if k not in ("request_id", "instance")
        }

    unknown = answer(
        h.client.post(url, headers=h, json=version_body(str(uuid7()), dataset=s.dataset))
    )
    assert unknown[0] == 422
    for refused in (away_ws, away_proj):
        assert (
            answer(h.client.post(url, headers=h, json=version_body(refused, dataset=s.dataset)))
            == unknown
        )

    away_conn = databricks_connection(h.client, h, "away", scope={"workspace": "other"})
    unknown_conn = answer(
        h.client.post(
            url,
            headers=h,
            json=version_body(s.agent_v1, connection=str(uuid7()), dataset=s.dataset),
        )
    )
    assert unknown_conn[0] == 422
    assert unknown_conn[1]["errors"][0]["loc"] == ["body", "data_reads", 0, "connection_id"]
    got = h.client.post(
        url, headers=h, json=version_body(s.agent_v1, connection=away_conn, dataset=s.dataset)
    )
    assert answer(got) == unknown_conn


def test_a_dataset_of_another_project_is_refused_as_an_unknown_id(
    client: TestClient, world: World
) -> None:
    """B-2703 within one client: a version's evaluation dataset must be one of the solution's
    project; another project's dataset gets the 422 an unknown id gets."""
    h, s, sid = _setup(client, world)
    url = f"{solutions()}/{sid}/versions"
    post(h.client, f"{A}/workspaces/main/projects", h, {"slug": "side", "name": "Side"})
    side = dataset(h.client, h, "side-golden", project="/workspaces/main/projects/side")

    def answer(dataset_id: str) -> tuple[int, Any]:  # Any: the problem body
        r = h.client.post(url, headers=h, json=version_body(s.agent_v1, dataset=dataset_id))
        return r.status_code, {
            k: v for k, v in r.json().items() if k not in ("request_id", "instance")
        }

    unknown = answer(str(uuid7()))
    assert unknown[0] == 422
    assert unknown[1]["errors"][0]["loc"] == ["body", "evaluation", "dataset_id"]
    assert answer(side["id"]) == unknown
    assert answer(s.dataset)[0] == 201


# ------------------------------------------------------------------ hidden models, connections
@dataclass(frozen=True)
class _Hidden:
    """A version whose visible pipeline entrypoint pins the administrator's private agent; only
    that agent names `model`, and only its private SQL tool (`tool`) names `pg`. The data reads
    are the organization-wide fake connection and the project's `inner` connection; `lake` is
    an organization-wide Databricks connection."""

    h: Hdr
    s: Stack
    sid: str
    vid: str
    agent: str
    tool: str
    model: str
    pg: str
    inner: str
    lake: str


def _hidden(client: TestClient, world: World, sample_db_url: str) -> _Hidden:
    h, s, sid = _setup(client, world, "Discreet")
    scope = {"type": "project", "id": s.project_id}
    model = post(
        h.client,
        f"{A}/models",
        h,
        {
            "provider_id": s.provider,
            "model_key": "hidden-chat",
            "display_name": "Hidden chat",
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": "active",
            "rate_limits": {},
        },
    )["id"]
    pg_password = sample_db_url.split(":")[2].split("@")[0]
    pg_secret = secret(h.client, h, "pg-pass", pg_password)
    pg = connection(h.client, h, pg_body(sample_db_url, pg_secret))["id"]
    activate(h.client, h, pg)
    tool_spec = {
        "executor": "connection.sql",
        "binding": {"connection_id": pg, "sql_template": "SELECT name FROM employees"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "required": ["columns", "rows"]},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 10,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = art(h.client, h, "tools", "private-sql", tool_spec, scope, visibility="private")
    tool_v1 = publish(h.client, h, "tools", tool["id"])["id"]
    spec = agent_spec(f"latest:{s.prompt}", model, tool_version_ids=[tool_v1])
    agent = art(h.client, h, "agents", "private-agent", spec, scope, visibility="private")
    agent_v1 = publish(h.client, h, "agents", agent["id"])["id"]
    node = {
        "id": "agt",
        "type": "agent",
        "config": {"agent_version_id": agent_v1},
        "inputs": {"input": "$params.term"},
    }
    pipe = _pipeline(h, s, "open-pipe", [node])
    inner = databricks_connection(
        h.client, h, "inner", scope={"workspace": "main", "project": "sample"}
    )
    body = version_body(pipe, kind="pipeline", dataset=s.dataset)
    body["data_reads"] = [
        {"connection_id": s.connection, "tables": ["main.sales.orders"]},
        {"connection_id": inner, "tables": ["main.sales.customers"]},
    ]
    vid = new_version(h.client, h, sid, body)["id"]
    lake = databricks_connection(h.client, h)
    return _Hidden(h, s, sid, vid, agent_v1, tool_v1, model, pg, inner, lake)


def _project_developer(client: TestClient, world: World, owner: Session, project: str) -> Hdr:
    """A developer bound only at the project: it cannot read organization-wide connections."""
    email = "proj-dev@demo-a.local"
    login(client, world.passwords, ADMIN)
    user = new_user(client, {}, "demo-a", email, PASSWORD)
    world.passwords[email] = PASSWORD
    admin = user_session(client, world.passwords, ADMIN)
    binding = {
        "subject_type": "user",
        "subject_id": user,
        "role_id": str(system_role(owner, "developer")),
        "scope": {"type": "project", "id": project},
    }
    post(admin.client, f"{A}/role-bindings", admin, binding)
    owner.rollback()
    return user_session(client, world.passwords, email)


def test_hidden_models_and_connections_are_redacted_for_a_reader(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    x = _hidden(client, world, sample_db_url)
    h, s = x.h, x.s
    full = bindings({x.model: s.provider}, {x.pg: x.lake, s.connection: x.lake, x.inner: x.lake})
    sealed = seal(h.client, h, x.sid, x.vid, full)
    assert sealed.status_code == 201, sealed.text
    rid = sealed.json()["id"]

    # the administrator sees every id and path, as stored
    admin = h.client.get(f"{A}/releases/{rid}", headers=h)
    assert admin.status_code == 200, admin.text
    paths = {d["path"] for d in admin.json()["compatibility"]}
    assert {f"models.{x.model}", f"connections.{x.pg}", f"tools.{x.tool}"} <= paths
    assert {f"connections.{s.connection}", f"connections.{x.inner}"} <= paths
    stored_manifest, stored_compatibility = owner_session.execute(
        text("SELECT manifest, compatibility FROM shifde.releases WHERE id = :r"), {"r": rid}
    ).one()
    owner_session.rollback()
    assert admin.json()["manifest"] == stored_manifest == sealed.json()["manifest"]
    assert admin.json()["compatibility"] == stored_compatibility
    assert stored_manifest["bindings"]["model_bindings"] == {
        x.model: {"provider_id": s.provider, "model": "served-chat"}
    }
    assert stored_manifest["bindings"]["connection_bindings"] == {
        x.pg: x.lake,
        s.connection: x.lake,
        x.inner: x.lake,
    }
    for value in (x.model, x.pg, x.tool, x.agent, s.connection, x.lake):
        assert value in admin.text

    # an organization-wide developer cannot see the private agent and tool
    make_user(client, owner_session, h, DEVELOPER, "developer", world.passwords)
    dev = user_session(client, world.passwords, DEVELOPER)
    got = dev.client.get(f"{A}/releases/{rid}", headers=dev)
    assert got.status_code == 200, got.text
    for value in (x.model, x.pg, x.tool, x.agent):
        assert value not in got.text
    rel = got.json()
    paths = {d["path"] for d in rel["compatibility"]}
    assert {"models.hidden", "connections.hidden", "tools.hidden"} <= paths
    assert {f"connections.{s.connection}", f"connections.{x.inner}"} <= paths
    assert rel["manifest"]["bindings"] == {
        "model_bindings": {},
        "connection_bindings": {s.connection: x.lake, x.inner: x.lake},
    }
    assert rel["manifest"]["data_reads"] == stored_manifest["data_reads"]
    assert rel["digest"] == admin.json()["digest"]

    # a project developer cannot read the organization-wide connections either
    proj = _project_developer(client, world, owner_session, s.project_id)
    got = proj.client.get(f"{A}/releases/{rid}", headers=proj)
    assert got.status_code == 200, got.text
    for value in (x.model, x.pg, x.tool, x.agent, s.connection, x.lake):
        assert value not in got.text
    rel = got.json()
    assert [r["connection_id"] for r in rel["manifest"]["data_reads"]] == [None, x.inner]
    assert rel["manifest"]["bindings"] == {
        "model_bindings": {},
        "connection_bindings": {x.inner: None},
    }
    connection_paths = [d["path"] for d in rel["compatibility"] if d["path"].startswith("conn")]
    assert sorted(connection_paths) == sorted(
        ["connections.hidden", "connections.hidden", f"connections.{x.inner}"]
    )

    # sealing the same bindings again returns the release; the stored row is unchanged
    again = seal(h.client, h, x.sid, x.vid, full)
    assert again.status_code == 200 and again.json() == admin.json()
    unchanged = owner_session.execute(
        text("SELECT manifest FROM shifde.releases WHERE id = :r"), {"r": rid}
    ).scalar_one()
    owner_session.rollback()
    assert unchanged == stored_manifest


def test_unreadable_connection_ids_are_null_on_version_routes(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    """Isolation review 065: a solution version's data reads and a visible tool's bound
    connection hide the ids of connections the reader cannot read (findings 1 and 2)."""
    x = _hidden(client, world, sample_db_url)
    h, s = x.h, x.s
    scope = {"type": "project", "id": s.project_id}
    tool_spec = {
        "executor": "connection.sql",
        "binding": {"connection_id": x.pg, "sql_template": "SELECT name FROM employees"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "required": ["columns", "rows"]},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 10,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = art(h.client, h, "tools", "shared-sql", tool_spec, scope)
    tool_v1 = publish(h.client, h, "tools", tool["id"])["id"]
    version_url = f"{solutions()}/{x.sid}/versions/{x.vid}"
    tool_url = f"{A}/tools/{tool['id']}/versions/{tool_v1}"

    # the administrator reads every connection
    admin = h.client.get(version_url, headers=h)
    assert admin.status_code == 200, admin.text
    reads = [r["connection_id"] for r in admin.json()["spec"]["data_reads"]]
    assert reads == [s.connection, x.inner]
    admin_tool = h.client.get(tool_url, headers=h)
    assert admin_tool.status_code == 200, admin_tool.text
    assert admin_tool.json()["spec"]["binding"]["connection_id"] == x.pg

    # a project developer reads the solution and the tool, not the organization-wide connections
    proj = _project_developer(client, world, owner_session, s.project_id)
    got = proj.client.get(version_url, headers=proj)
    assert got.status_code == 200, got.text
    assert s.connection not in got.text
    assert [r["connection_id"] for r in got.json()["spec"]["data_reads"]] == [None, x.inner]
    listed = proj.client.get(f"{solutions()}/{x.sid}/versions", headers=proj)
    assert listed.status_code == 200, listed.text
    assert s.connection not in listed.text
    [item] = [v for v in listed.json()["items"] if v["id"] == x.vid]
    assert [r["connection_id"] for r in item["spec"]["data_reads"]] == [None, x.inner]
    got_tool = proj.client.get(tool_url, headers=proj)
    assert got_tool.status_code == 200, got_tool.text
    assert x.pg not in got_tool.text
    assert got_tool.json()["spec"]["binding"] == {
        **tool_spec["binding"],
        "connection_id": None,
    }
    assert got_tool.json()["spec_hash"] == admin_tool.json()["spec_hash"]

    # the tool's diff hides the unreadable connection as well
    edit(
        h.client,
        h,
        "tools",
        tool["id"],
        {**tool_spec, "binding": {**tool_spec["binding"], "connection_id": x.inner}},
    )
    diff_url = f"{A}/tools/{tool['id']}/diff?from={tool_v1}&to=draft"
    admin_diff = h.client.get(diff_url, headers=h)
    assert admin_diff.status_code == 200, admin_diff.text
    path = "binding.connection_id"
    assert [c for c in admin_diff.json()["changes"] if c["path"] == path] == [
        {"path": path, "op": "replace", "from": x.pg, "to": x.inner}
    ]
    got_diff = proj.client.get(diff_url, headers=proj)
    assert got_diff.status_code == 200, got_diff.text
    assert x.pg not in got_diff.text
    assert [c for c in got_diff.json()["changes"] if c["path"] == path] == [
        {"path": path, "op": "replace", "from": None, "to": x.inner}
    ]

    # a pipeline node's connection id, its connection ref and its diff hide it too (issue #167)
    pipe_spec = _db_pipeline(x.pg)
    pipe = art(h.client, h, "pipelines", "shared-query", pipe_spec, scope)
    pipe_v1 = publish(h.client, h, "pipelines", pipe["id"])["id"]
    pipe_url = f"{A}/pipelines/{pipe['id']}/versions/{pipe_v1}"
    admin_pipe = h.client.get(pipe_url, headers=h)
    assert admin_pipe.status_code == 200, admin_pipe.text
    assert admin_pipe.json()["spec"]["nodes"][0]["config"]["connection_id"] == x.pg
    assert {"node_id": "src", "ref_type": "connection", "ref_id": x.pg} in admin_pipe.json()["refs"]
    got_pipe = proj.client.get(pipe_url, headers=proj)
    assert got_pipe.status_code == 200, got_pipe.text
    assert x.pg not in got_pipe.text
    assert got_pipe.json()["spec"]["nodes"][0]["config"] == {
        **pipe_spec["nodes"][0]["config"],
        "connection_id": None,
    }
    assert got_pipe.json()["spec"]["nodes"][1:] == pipe_spec["nodes"][1:]
    assert [r for r in got_pipe.json()["refs"] if r["ref_type"] == "connection"] == [
        {"node_id": "src", "ref_type": "connection", "ref_id": None}
    ]
    assert len(got_pipe.json()["refs"]) == len(admin_pipe.json()["refs"])
    assert got_pipe.json()["spec_hash"] == admin_pipe.json()["spec_hash"]

    edit(h.client, h, "pipelines", pipe["id"], _db_pipeline(x.inner))
    pipe_diff_url = f"{A}/pipelines/{pipe['id']}/diff?from={pipe_v1}&to=draft"
    admin_pipe_diff = h.client.get(pipe_diff_url, headers=h)
    assert admin_pipe_diff.status_code == 200, admin_pipe_diff.text
    [admin_change] = [
        c for c in admin_pipe_diff.json()["changes"] if c["path"].endswith("connection_id")
    ]
    assert (admin_change["from"], admin_change["to"]) == (x.pg, x.inner)
    got_pipe_diff = proj.client.get(pipe_diff_url, headers=proj)
    assert got_pipe_diff.status_code == 200, got_pipe_diff.text
    assert x.pg not in got_pipe_diff.text
    [change] = [c for c in got_pipe_diff.json()["changes"] if c["path"].endswith("connection_id")]
    assert change == {**admin_change, "from": None}


def _db_pipeline(connection_id: str) -> dict[str, Any]:
    nodes = [
        {
            "id": "src",
            "type": "db_query",
            "config": {"connection_id": connection_id, "sql": "SELECT name FROM employees"},
            "inputs": {},
        },
        sink_artifact("store", "src.rows"),
    ]
    return mini(nodes, [{"from": "src", "to": "store"}])


def _sql_tool_spec(connection_id: str | None) -> dict[str, Any]:
    return {
        "executor": "connection.sql",
        "binding": {"connection_id": connection_id, "sql_template": "SELECT name FROM employees"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "required": ["columns", "rows"]},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 10,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }


def _stored_draft(owner: Session, artifact: str) -> dict[str, Any]:
    spec = owner.execute(
        text(
            "SELECT spec FROM shifde.artifact_versions WHERE artifact_id = :a AND state = 'draft'"
        ),
        {"a": artifact},
    ).scalar_one()
    owner.rollback()
    return dict(spec)


def _stored_connection(kind: str, spec: dict[str, Any]) -> Any:  # Any: JSON value
    return spec["binding"] if kind == "tools" else spec["nodes"][0]["config"]


def test_a_draft_saved_unchanged_keeps_an_unreadable_connection(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    """Issue #167: a reader shown `null` for a connection it cannot read saves the draft back
    unchanged; the stored connection stays. A reader who can read it clears it with `null`."""
    x = _hidden(client, world, sample_db_url)
    h, s = x.h, x.s
    scope = {"type": "project", "id": s.project_id}
    proj = _project_developer(client, world, owner_session, s.project_id)
    for kind, spec_of in (("tools", _sql_tool_spec), ("pipelines", _db_pipeline)):
        made = art(h.client, h, kind, f"kept-{kind}", spec_of(x.pg), scope)
        draft_url = f"{A}/{kind}/{made['id']}/draft"

        # the developer saves the spec it was shown: the stored id is kept, and stays hidden
        shown = proj.client.get(draft_url, headers=proj)
        assert shown.status_code == 200, shown.text
        assert x.pg not in shown.text
        assert _stored_connection(kind, shown.json()["spec"])["connection_id"] is None
        saved = proj.client.put(
            draft_url,
            headers=proj,
            json={"spec": shown.json()["spec"], "row_version": shown.json()["row_version"]},
        )
        assert saved.status_code == 200, saved.text
        assert x.pg not in saved.text
        assert _stored_connection(kind, saved.json()["spec"])["connection_id"] is None
        assert _stored_draft(owner_session, made["id"]) == spec_of(x.pg)
        admin = h.client.get(draft_url, headers=h)
        assert _stored_connection(kind, admin.json()["spec"])["connection_id"] == x.pg

        # the administrator reads the connection: its `null` clears it
        cleared = h.client.put(
            draft_url,
            headers=h,
            json={"spec": spec_of(None), "row_version": admin.json()["row_version"]},
        )
        assert cleared.status_code == 200, cleared.text
        assert _stored_connection(kind, _stored_draft(owner_session, made["id"])) == {
            **_stored_connection(kind, spec_of(x.pg)),
            "connection_id": None,
        }

        # so does the developer's `null` for the project connection it reads
        inner = edit(h.client, h, kind, made["id"], spec_of(x.inner), cleared.json()["row_version"])
        again = proj.client.put(
            draft_url,
            headers=proj,
            json={"spec": spec_of(None), "row_version": inner["row_version"]},
        )
        assert again.status_code == 200, again.text
        assert _stored_draft(owner_session, made["id"]) == spec_of(None)


def test_a_hidden_binding_key_is_an_unknown_key_and_reports_read_hidden(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    x = _hidden(client, world, sample_db_url)
    s = x.s
    make_user(client, owner_session, x.h, DEVELOPER, "developer", world.passwords)
    dev = user_session(client, world.passwords, DEVELOPER)
    nobody = str(uuid7())

    def answer(body: dict[str, Any], key: str) -> dict[str, Any]:  # Any: the problem body
        r = seal(dev.client, dev, x.sid, x.vid, body)
        assert r.status_code == 422, r.text
        out = {k: r.json()[k] for k in ("code", "detail", "errors")}
        for e in out["errors"]:
            e["loc"] = ["<key>" if part == key else part for part in e["loc"]]
        return out

    lakes = {s.connection: x.lake, x.inner: x.lake}
    for hidden_body, unknown_body, key in (
        (bindings({x.model: s.provider}, lakes), bindings({nobody: s.provider}, lakes), x.model),
        (bindings({}, {**lakes, x.pg: x.lake}), bindings({}, {**lakes, nobody: x.lake}), x.pg),
    ):
        hidden, unknown = answer(hidden_body, key), answer(unknown_body, nobody)
        assert hidden == unknown and hidden["code"] == "validation_error"
        assert [e["loc"][-1] for e in hidden["errors"]] == ["<key>"]
    assert _count(owner_session) == 0

    # without the hidden bindings the report names the hidden pins as hidden
    r = seal(dev.client, dev, x.sid, x.vid, bindings({}, lakes))
    for value in (x.model, x.pg, x.tool, x.agent):
        assert value not in r.text
    assert _unsupported(r) == {
        "models.hidden": "no model binding",
        "connections.hidden": "no connection binding",
    }
    shown = set(_diagnostics(r))
    assert {"tools.hidden", f"connections.{s.connection}", f"connections.{x.inner}"} <= shown
    [(outcome, _, details)] = _audits(owner_session, "packaging.release.refused")
    assert outcome == "failure"
    assert details["unsupported"] == ["models.hidden", "connections.hidden"]

    # the administrator sees the pins and seals with the bindings the developer could not use
    full = bindings({x.model: s.provider}, {**lakes, x.pg: x.lake})
    assert seal(x.h.client, x.h, x.sid, x.vid, full).status_code == 201


# ------------------------------------------------------------------ the retire guard, no client
def test_the_retire_guard_for_the_owner_without_a_client_context(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Issue #93: the schema owner, with no `app.tenant_id` set, retires a pinned version. The
    owner is BYPASSRLS on the dev and test clusters, so the guard sees the release and refuses."""
    h, s, sid = _setup(client, world)
    vid = new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    assert seal(h.client, h, sid, vid, _own_bindings(s)).status_code == 201
    post(
        h.client,
        f"{A}/agents/{s.agent}/versions/{s.agent_v1}/state",
        h,
        {"state": "deprecated"},
        200,
    )
    setting = owner_session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    assert setting.scalar_one() in (None, "")
    with pytest.raises(DBAPIError) as err:
        owner_session.execute(
            text("UPDATE shifde.artifact_versions SET state = 'retired' WHERE id = :v"),
            {"v": s.agent_v1},
        )
    owner_session.rollback()
    assert _sqlstate(err.value) == "23514", err.value
    assert "a sealed release pins this version" in str(err.value)
    state = owner_session.execute(
        text("SELECT state FROM shifde.artifact_versions WHERE id = :v"), {"v": s.agent_v1}
    ).scalar_one()
    owner_session.rollback()
    assert state == "deprecated"
