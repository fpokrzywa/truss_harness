"""SP5-3 (design 6.2): solution versions carry `screens`, and a version with screens seals under
`shifde.packaging/2`. Its manifest adds `screens` and `app.generator_version`, and four mandatory
checks refuse what a generated app cannot run: an entrypoint that is not an agent, a table screen
of an unpinned tool or of an input that is not the tool's parameter, a model count other than
one, and a tool that is not `connection.sql` over a Databricks connection. Each refused version
without screens seals under `/1` exactly as before. A table screen of a pinned tool the sealer
cannot see (private, or in a project the sealer cannot read) gets the answer of an unpinned key,
and another client learns nothing of the tools (issue #316)."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.appgen import render
from shifde.packaging import service as packaging
from shifde.packaging.canonical import digest
from tests._support.fullapp import World
from tests._support.p3util import ADMIN_B, B, fake_model_stack
from tests._support.p4util import agent_spec, art, builtin_tool_spec, make_user, publish
from tests._support.p5util import Hdr, user_session
from tests.appgen.helpers import add_project, databricks, wire
from tests.packaging.helpers import (
    Stack,
    bindings,
    new_version,
    seal,
    solutions,
    version_body,
)
from tests.packaging.test_releases import (
    DEVELOPER,
    _audits,
    _model_node,
    _pipeline,
    _project_developer,
    _setup,
    _sql_tool,
    _unsupported,
)

CHAT = {"kind": "chat", "title": "Ask", "placeholder": ""}
APP_TOOLS = "a generated app's tools are SQL tools over a Databricks connection"


def _table(tool: str, *inputs: str, title: str = "Orders") -> dict[str, Any]:
    return {"kind": "table", "title": title, "tool": tool, "inputs": list(inputs)}


def _with(body: dict[str, Any], screens: list[dict[str, Any]]) -> dict[str, Any]:
    return {**body, "screens": screens}


def _dbx_tool(
    h: Hdr,
    s: Stack,
    connection: str,
    key: str = "orders",
    *,
    scope: dict[str, str] | None = None,
    visibility: str = "scope",
) -> str:
    """A published `connection.sql` tool keyed `key` (in the stack's project unless `scope`)
    over the Databricks connection."""
    spec = {
        "executor": "connection.sql",
        "binding": {
            "connection_id": connection,
            "sql_template": "select o.id from main.default.orders o "
            "where o.customer_id = %(customer_id)s",
            "max_rows": 10,
        },
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}},
            "required": ["customer_id"],
        },
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "timeout_s": 10,
        "audit_level": "metadata",
    }
    place = scope or {"type": "project", "id": s.project_id}
    tool = art(h.client, h, "tools", key, spec, place, visibility=visibility)
    return str(publish(h.client, h, "tools", tool["id"])["id"])


def _agent(h: Hdr, s: Stack, key: str, tools: list[str], **model: Any) -> str:
    spec = agent_spec(f"latest:{s.prompt}", s.model, tool_version_ids=tools)
    spec["model"].update(model)
    made = art(h.client, h, "agents", key, spec, {"type": "project", "id": s.project_id})
    return str(publish(h.client, h, "agents", made["id"])["id"])


def test_the_generator_version_is_appgens() -> None:
    assert packaging.APP_GENERATOR_VERSION == render.GENERATOR_VERSION


def test_a_version_with_screens_seals_under_compiler_2(client: TestClient, world: World) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    agent = _agent(h, s, "app", [_dbx_tool(h, s, lake)])
    screens = [CHAT, _table("orders", "customer_id")]
    body = version_body(agent, dataset=s.dataset)
    version = new_version(h.client, h, sid, _with(body, screens))
    assert version["spec"]["screens"] == screens
    plain = new_version(h.client, h, sid, body)
    # a version without screens: the spec (and `spec_hash`) it had before screens existed
    assert plain["spec"]["screens"] == []
    assert plain["spec_hash"] == digest(
        {
            "data_reads": [],
            "screens": [],
            "evaluation": {**body["evaluation"], "max_total_cost_usd": "1.5"},
        }
    )
    assert version["spec_hash"] != plain["spec_hash"]

    r = seal(h.client, h, sid, version["id"], bindings({s.model: s.provider}, {lake: lake}))
    assert r.status_code == 201, r.text
    release = r.json()
    manifest = release["manifest"]
    assert manifest["compiler_version"] == "shifde.packaging/2"
    assert manifest["schema_version"] == 1
    assert manifest["screens"] == screens
    assert manifest["app"] == {"generator_version": "shifde.appgen/1.0"}
    assert release["digest"] == digest(manifest)
    paths = [d["path"] for d in release["compatibility"]]
    assert {"models", "screens.0", "screens.1"} <= set(paths)
    assert all(d["status"] == "supported" for d in release["compatibility"])

    old = seal(h.client, h, sid, plain["id"], bindings({s.model: s.provider}, {lake: lake}))
    assert old.status_code == 201, old.text
    assert old.json()["manifest"]["compiler_version"] == "shifde.packaging/1"
    assert "screens" not in old.json()["manifest"] and "app" not in old.json()["manifest"]
    assert "models" not in [d["path"] for d in old.json()["compatibility"]]


def _both(
    h: Hdr, sid: str, body: dict[str, Any], screens: list[dict[str, Any]], seal_body: Any
) -> tuple[dict[str, str], Any]:  # Any: the `/1` response
    """(the `/2` refusal's unsupported paths and reasons, the `/1` seal of the same version
    without screens)."""
    with_screens = new_version(h.client, h, sid, _with(body, screens))["id"]
    refused = _unsupported(seal(h.client, h, sid, with_screens, seal_body))
    without = new_version(h.client, h, sid, body)["id"]
    return refused, seal(h.client, h, sid, without, seal_body)


def test_a_pipeline_entrypoint_runs_no_generated_app(client: TestClient, world: World) -> None:
    h, s, sid = _setup(client, world)
    flow = _pipeline(h, s, "flow", [_model_node(s.model)])
    body = version_body(flow, kind="pipeline", dataset=s.dataset)
    refused, plain = _both(h, sid, body, [CHAT], bindings({s.model: s.provider}))
    assert refused == {"entrypoint": "a generated app runs one agent"}
    assert plain.status_code == 201, plain.text
    assert plain.json()["manifest"]["compiler_version"] == "shifde.packaging/1"


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (_table("nope"), "the version pins no tool 'nope'"),
        (_table("orders", "region"), "'region' is not a parameter of tool 'orders'"),
    ],
)
def test_a_table_screen_names_a_pinned_tool_and_its_parameters(
    client: TestClient, world: World, screen: dict[str, Any], reason: str
) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    agent = _agent(h, s, "app", [_dbx_tool(h, s, lake)])
    body = version_body(agent, dataset=s.dataset)
    seal_body = bindings({s.model: s.provider}, {lake: lake})
    refused, plain = _both(h, sid, body, [CHAT, screen], seal_body)
    assert refused == {"screens.1": reason}
    assert plain.status_code == 201, plain.text


def test_a_generated_app_uses_exactly_one_model(client: TestClient, world: World) -> None:
    h, s, sid = _setup(client, world)
    second = fake_model_stack(h.client, h, model_key="fake-two", conn_name="fake-two")
    agent = _agent(h, s, "two-models", [], fallback_model_ids=[second["model"]])
    body = version_body(agent, dataset=s.dataset)
    seal_body = bindings({s.model: s.provider, second["model"]: second["provider"]})
    refused, plain = _both(h, sid, body, [CHAT], seal_body)
    assert refused == {"models": "a generated app uses exactly one model"}
    assert plain.status_code == 201, plain.text


def test_a_generated_apps_tools_are_sql_over_databricks(
    client: TestClient, world: World, sample_db_url: str
) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    scope = {"type": "project", "id": s.project_id}
    pg_tool, pg_conn = _sql_tool(h, sample_db_url, scope)
    agent = _agent(h, s, "on-postgres", [pg_tool])
    seal_body = bindings({s.model: s.provider}, {pg_conn: lake})
    refused, plain = _both(h, sid, version_body(agent, dataset=s.dataset), [CHAT], seal_body)
    assert refused == {f"tools.{pg_tool}": APP_TOOLS}
    assert plain.status_code == 201, plain.text  # read-only SQL: `/1` releases it

    builtin = art(h.client, h, "tools", "echo", builtin_tool_spec(), scope)
    builtin_v1 = str(publish(h.client, h, "tools", builtin["id"])["id"])
    agent = _agent(h, s, "on-builtin", [builtin_v1])
    body = version_body(agent, dataset=s.dataset)
    refused, plain = _both(h, sid, body, [CHAT], bindings({s.model: s.provider}))
    assert refused == {f"tools.{builtin_v1}": "not on the read-only list"}
    assert _unsupported(plain) == refused  # refused by `/1` as before


# ------------------------------------------------------------------ hidden tools (issue #316)
SECRET_TOOL = "secret_orders"
UNPINNED = "the version pins no tool {!r}"
PROBES = ("customer_id", "not a parameter")


def _screen_versions(h: Hdr, s: Stack, sid: str, tool: str) -> dict[str, str]:
    """`/2` versions of one agent pinning `tool` (keyed `SECRET_TOOL`), by their second screen:
    `real` (the tool's key and parameter), `wrong` (an input the tool lacks) and `unpinned` (a key
    nothing pins)."""
    agent = _agent(h, s, "app", [tool])
    body = version_body(agent, dataset=s.dataset)
    screens = {
        "real": _table(SECRET_TOOL, "customer_id"),
        "wrong": _table(SECRET_TOOL, "region"),
        "unpinned": _table("nope", "customer_id"),
    }
    versions = {
        name: new_version(h.client, h, sid, _with(body, [CHAT, screen]))["id"]
        for name, screen in screens.items()
    }
    return versions


def _hidden_answers(sealer: Hdr, s: Stack, sid: str, versions: dict[str, str]) -> list[str]:
    """The `screens.1` reasons a sealer who cannot see the tool gets for `real` and `wrong`,
    which must read like the `unpinned` one and carry nothing of the tool's parameters (bound
    without the tool's connection: only the hidden tool names it)."""
    body = bindings({s.model: s.provider})
    unpinned = _unsupported(seal(sealer.client, sealer, sid, versions["unpinned"], body))
    assert unpinned["screens.1"] == UNPINNED.format("nope")
    reasons = []
    for name in ("real", "wrong"):
        r = seal(sealer.client, sealer, sid, versions[name], body)
        refused = _unsupported(r)
        assert "screens.1" in refused, r.text
        assert all(probe not in r.text for probe in PROBES), r.text
        reasons.append(refused["screens.1"])
    return reasons


def test_a_screen_of_a_tool_the_sealer_cannot_see_reads_like_an_unpinned_key(
    client: TestClient, world: World, owner_session: Session
) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    tool = _dbx_tool(h, s, lake, SECRET_TOOL, visibility="private")
    versions = _screen_versions(h, s, sid, tool)
    make_user(client, owner_session, h, DEVELOPER, "developer", world.passwords)
    dev = user_session(client, world.passwords, DEVELOPER)
    refused_before = len(_audits(owner_session, "packaging.release.refused"))

    assert _hidden_answers(dev, s, sid, versions) == [UNPINNED.format(SECRET_TOOL)] * 2
    refusals = _audits(owner_session, "packaging.release.refused")[refused_before:]
    assert len(refusals) == 3
    assert all(probe not in json.dumps(details) for _, _, details in refusals for probe in PROBES)

    # the administrator sees the tool: the normal answers
    admin = user_session(client, world.passwords, "admin@demo-a.local")
    body = bindings({s.model: s.provider}, {lake: lake})
    sealed = seal(admin.client, admin, sid, versions["real"], body)
    assert sealed.status_code == 201, sealed.text
    wrong = _unsupported(seal(admin.client, admin, sid, versions["wrong"], body))
    assert wrong == {"screens.1": f"'region' is not a parameter of tool {SECRET_TOOL!r}"}


def test_a_screen_of_another_projects_tool_reads_like_an_unpinned_key(
    client: TestClient, world: World, owner_session: Session
) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    other = add_project(h.client, h, "elsewhere")
    tool = _dbx_tool(h, s, lake, SECRET_TOOL, scope={"type": "project", "id": other})
    versions = _screen_versions(h, s, sid, tool)
    member = _project_developer(client, world, owner_session, s.project_id)
    assert _hidden_answers(member, s, sid, versions) == [UNPINNED.format(SECRET_TOOL)] * 2


def test_another_client_learns_nothing_of_a_generated_apps_tools(
    client: TestClient, world: World
) -> None:
    wire(client)
    h, s, sid = _setup(client, world)
    lake = databricks(h.client, h)
    tool = _dbx_tool(h, s, lake, SECRET_TOOL)
    versions = _screen_versions(h, s, sid, tool)
    body = bindings({s.model: s.provider}, {lake: lake})
    sealed = seal(h.client, h, sid, versions["real"], body)
    assert sealed.status_code == 201, sealed.text
    other = user_session(client, world.passwords, ADMIN_B)
    answers = [
        seal(other.client, other, sid, versions["real"], body, org=B),
        seal(other.client, other, sid, versions["unpinned"], body, org=B),
        other.client.get(f"{B}/releases/{sealed.json()['id']}", headers=other),
        other.client.get(f"{solutions(B)}/{sid}/versions/{versions['real']}", headers=other),
    ]
    for r in answers:
        assert r.status_code == 404, r.text
        assert SECRET_TOOL not in r.text and "customer_id" not in r.text
