"""build part 4b: the per-kind panels of `build/kind_ops.py` through the build routes.

F4 (build isolation review 2): the tool test panel loads the version through the tenant-filtered
service, a write class needs `tool:test_side_effects` and the acknowledgement (nothing is sent for
a refused write), and it runs through the caller's own `ConnectionService`. The prompt and agent
panels call models through the client's own `ModelService` and the SHI call-time gate (plan Review
Focus 5): a client whose SHI switch is off cannot reach the shared provider through either panel.

Moved from SETH `test_p4_tools.py::test_rest_tool_runs_through_the_connection_and_guards_side_
effects` (the connection set-up goes through the connections routes of this harness; the
assertions are SETH's, unchanged).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import OpenAIFakeServer, RestTestServer
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests.build.routes import (
    agent_spec,
    art,
    build_client,
    edit,
    prompt_spec,
    publish,
    rest_spec,
    scope_of,
)
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    org,
    rest_body,
    secret,
    system_role_user,
)
from tests.models_admin.routes import PLATFORM, fake_model_stack, shared_model, switch
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}
SHI_KEY = "sk-shi-installation-key-0002"


def _same_404(r: httpx.Response, ref: httpx.Response) -> None:
    assert r.status_code == ref.status_code == 404, (r.status_code, r.text, ref.text)
    assert {k: v for k, v in r.json().items() if k not in _VARYING} == {
        k: v for k, v in ref.json().items() if k not in _VARYING
    }


def _rest_conn(
    client: TestClient,
    h: dict[str, str],
    server: RestTestServer,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    base: str,
    name: str = "rest-api",
    access: str = "read",
    activate_it: bool = True,
) -> str:
    sid = secret(app_db, settings, tenant, REST_TOKEN)
    conn = connection(client, h, {**rest_body(server, sid, name), "access_mode": access}, base)
    if activate_it:
        activate(client, h, conn["id"], base)
    return str(conn["id"])


def _validate(client: TestClient, h: dict[str, str], base: str, tid: str) -> dict[str, Any]:
    r = client.post(f"{base}/tools/{tid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def _run_tool(
    client: TestClient,
    h: dict[str, str],
    base: str,
    tid: str,
    args: dict[str, Any],
    version: str = "draft",
    ack: bool = False,
    status: int = 200,
) -> Any:  # Any: a JSON body
    body: dict[str, Any] = {"version_id": version, "args": args}
    if ack:
        body["acknowledge_side_effects"] = True
    r = client.post(f"{base}/tools/{tid}/test", json=body, headers=h)
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


def test_rest_tool_runs_through_the_connection_and_guards_side_effects(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    owner_session: Session,
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = scope_of(world.a.tenant)

    def conn(name: str, access: str = "read", *, active: bool = True) -> str:
        return _rest_conn(
            client, h, rest_server, app_db, settings, world.a.tenant, A, name, access, active
        )

    ro = conn("ro-api", "read")
    rw = conn("rw-api", "read_write")

    t = art(client, h, A, "tools", "lookup", rest_spec(ro), scope)
    assert _validate(client, h, A, t["id"])["valid"] is True
    before = len(rest_server.requests)
    out = _run_tool(client, h, A, t["id"], {"name": "a b&c"})
    assert out["valid_output"] is True and out["output"]["status"] == 200
    assert out["output"]["body"]["query"] == {"name": "a b&c"}  # a value, not a query fragment
    assert len(rest_server.requests) == before + 1 and rest_server.requests[-1]["authenticated"]

    # path placeholders are percent-encoded values
    spec = rest_spec(ro)
    spec["binding"]["path_template"] = "/{segment}"
    spec["input_schema"]["properties"]["segment"] = {"type": "string"}
    spec["input_schema"]["required"] = ["name", "segment"]
    edit(client, h, A, "tools", t["id"], spec, 1)
    assert _validate(client, h, A, t["id"])["valid"] is True
    res = _run_tool(client, h, A, t["id"], {"name": "x", "segment": "a b/c?d"})
    assert res["output"]["status"] == 404
    assert rest_server.requests[-1]["path"] == "/a%20b%2Fc%3Fd"  # exactly one encoded segment

    dots = client.post(
        f"{A}/tools/{t['id']}/test",
        json={"version_id": "draft", "args": {"name": "x", "segment": ".."}},
        headers=h,
    )
    assert dots.status_code == 422 and "relative path segment" in dots.json()["detail"]

    # ---- validation against the binding
    def codes_of(spec: dict[str, Any], rv: int) -> set[str]:
        edit(client, h, A, "tools", t["id"], spec, rv)
        return {e["code"] for e in _validate(client, h, A, t["id"])["errors"]}

    assert codes_of(rest_spec(ro, "POST", "read"), 2) == {"write_method_requires_write_class"}
    assert codes_of(rest_spec(ro, "POST", "write_non_idempotent"), 3) == {"connection_read_only"}
    unknown_arg = rest_spec(ro)
    unknown_arg["binding"]["query_template"] = {"x": "{ghost}"}
    assert codes_of(unknown_arg, 4) == {"unknown_argument"}
    bad_path = rest_spec(ro)
    bad_path["binding"]["path_template"] = "http://evil.example/x"
    assert codes_of(bad_path, 5) == {"invalid_path"}
    no_conn = rest_spec(ro)
    no_conn["binding"]["connection_id"] = "00000000-0000-0000-0000-000000000000"
    assert codes_of(no_conn, 6) == {"connection_not_found"}
    assert codes_of(rest_spec(conn("idle-api", active=False)), 7) == {"connection_not_active"}
    wrong_type = rest_spec(ro)
    wrong_type["executor"] = "mcp.tool"
    wrong_type["binding"] = {"connection_id": ro, "tool_name": "x"}
    assert codes_of(wrong_type, 8) == {"wrong_connection_type"}

    # cross-tenant: another organization's connection does not exist for us
    hb = as_user(client, people.owner_b)
    other = _rest_conn(
        client, hb, rest_server, app_db, settings, world.b.tenant, org(world.b.slug), "b-api"
    )
    h = as_user(client, people.owner_a)
    assert codes_of(rest_spec(other), 9) == {"connection_not_found"}
    edit(client, h, A, "tools", t["id"], rest_spec(ro), 10)
    relative = rest_spec(ro)
    relative["binding"]["path_template"] = "echo"
    assert codes_of(relative, 11) == {"invalid_path"}  # must start with '/'
    edit(client, h, A, "tools", t["id"], rest_spec(ro), 12)

    # ---- the side-effect guard (C1): a write needs the acknowledgement AND tool:test_side_effects
    w = art(
        client,
        h,
        A,
        "tools",
        "create-note",
        rest_spec(
            rw,
            "POST",
            "write_non_idempotent",
            binding={
                "connection_id": rw,
                "method": "POST",
                "path_template": "/echo",
                "body_template": {"note": "{name}", "n": 1},
            },
        ),
        scope,
    )
    assert _validate(client, h, A, w["id"])["valid"] is True

    def posts() -> list[Any]:  # Any: recorded requests
        return [r for r in rest_server.requests if r["method"] == "POST"]

    n0 = len(posts())
    refused = _run_tool(client, h, A, w["id"], {"name": "hello"}, status=422)
    assert refused["code"] == "side_effects_not_acknowledged" and len(posts()) == n0
    done = _run_tool(client, h, A, w["id"], {"name": "hello"}, ack=True)
    assert done["output"]["body"]["body"] == {"note": "hello", "n": 1} and len(posts()) == n0 + 1

    dev = as_user(client, system_role_user(owner_db, world.a.tenant, "developer"))
    forbidden = _run_tool(client, dev, A, w["id"], {"name": "x"}, ack=True, status=403)
    assert "tool:test_side_effects" in forbidden["detail"] and len(posts()) == n0 + 1
    # the developer may test read tools, and a write tool is still refused without the permission
    assert _run_tool(client, dev, A, t["id"], {"name": "x"}, status=200)
    viewer = as_user(client, people.viewer_a)
    assert _run_tool(client, viewer, A, t["id"], {"name": "x"}, status=403)

    # publish, then test the published version; audit_level=full records arguments
    h = as_user(client, people.owner_a)
    full_tool = art(client, h, A, "tools", "audited-full", rest_spec(ro, audit_level="full"), scope)
    v1 = publish(client, h, A, "tools", full_tool["id"])
    _run_tool(client, h, A, full_tool["id"], {"name": "visible-arg"}, version=v1["id"])
    _run_tool(client, h, A, t["id"], {"name": "hidden-arg"})
    rows = (
        owner_session.execute(
            text("SELECT details::text FROM shifde.audit_events WHERE action = 'tool.tested'")
        )
        .scalars()
        .all()
    )
    blob = "\n".join(rows)
    assert "visible-arg" in blob and "hidden-arg" not in blob  # metadata level: keys only
    assert '"arg_keys"' in blob and REST_TOKEN not in blob
    used = owner_session.execute(
        text("SELECT count(*) FROM shifde.audit_events WHERE action = 'connection.used'")
    ).scalar_one()
    assert used >= 4  # every tool call went through ConnectionService.open_for_use


def test_the_tool_panel_never_runs_bs_connection_or_version(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """F4: the panel reads the version and the connection as the caller's client only. A tool of
    A that names B's connection is invalid for A (draft refused, nothing sent); B's tool version
    id in A's panel is the unknown-version answer; B's connection is never used."""
    A, B = org(world.a.slug), org(world.b.slug)
    with RestTestServer(token=REST_TOKEN) as b_server:
        hb = as_user(client, people.owner_b)
        b_conn = _rest_conn(client, hb, b_server, app_db, settings, world.b.tenant, B)
        b_tool = art(client, hb, B, "tools", "b-tool", rest_spec(b_conn), scope_of(world.b.tenant))
        b_v1 = publish(client, hb, B, "tools", b_tool["id"])
        ha = as_user(client, people.owner_a)
        a_conn = _rest_conn(client, ha, rest_server, app_db, settings, world.a.tenant, A)
        a_tool = art(client, ha, A, "tools", "a-tool", rest_spec(a_conn), scope_of(world.a.tenant))
        seen = len(b_server.requests)  # B's own activation call is already counted
        # A's tool bound to B's connection id is rejected on save-for-test: nothing is sent
        stolen = art(client, ha, A, "tools", "stolen", rest_spec(b_conn), scope_of(world.a.tenant))
        refused = client.post(
            f"{A}/tools/{stolen['id']}/test",
            json={"version_id": "draft", "args": {"name": "x"}},
            headers=ha,
        )
        assert refused.status_code == 422, refused.text
        assert {e["code"] for e in refused.json()["errors"]} == {"connection_not_found"}
        # B's version id in A's panel (A's artifact or B's artifact id): the unknown answer
        missing = client.post(
            f"{A}/tools/{a_tool['id']}/test",
            json={"version_id": str(uuid4()), "args": {"name": "x"}},
            headers=ha,
        )
        crossed = client.post(
            f"{A}/tools/{a_tool['id']}/test",
            json={"version_id": b_v1["id"], "args": {"name": "x"}},
            headers=ha,
        )
        _same_404(crossed, missing)
        theirs = client.post(
            f"{A}/tools/{b_tool['id']}/test",
            json={"version_id": b_v1["id"], "args": {"name": "x"}},
            headers=ha,
        )
        unknown_tool = client.post(
            f"{A}/tools/{uuid4()}/test",
            json={"version_id": str(uuid4()), "args": {"name": "x"}},
            headers=ha,
        )
        _same_404(theirs, unknown_tool)
        assert len(b_server.requests) == seen  # B's listener saw nothing from A's panel
        # the control: B's own panel reaches its own connection
        hb = as_user(client, people.owner_b)
        mine = client.post(
            f"{B}/tools/{b_tool['id']}/test",
            json={"version_id": b_v1["id"], "args": {"name": "x"}},
            headers=hb,
        )
        assert mine.status_code == 200 and len(b_server.requests) == seen + 1


# ---------------------------------------------------------------- models: the SHI call-time gate
@dataclass(frozen=True)
class Shi:
    client: TestClient
    server: OpenAIFakeServer
    model: str


@pytest.fixture
def shi(settings: Settings, world: World, people: People, app_db: Database) -> Iterator[Shi]:
    """SHI's provider is an OpenAI-compatible local endpoint that records every chat call; A's
    switch is on, B's off (as `tests/models_admin/test_shi_gate.py`)."""
    with OpenAIFakeServer(api_key=SHI_KEY) as server:
        configured = settings.model_copy(
            update={
                "shi_provider_config": {"base_url": f"{server.base_url}/v1"},
                "shi_provider_api_key": SecretStr(SHI_KEY),
            }
        )
        with build_client(configured) as c:
            h = as_user(c, people.platform)
            r = c.put(
                f"{PLATFORM}/shi-provider",
                headers=h,
                json={"adapter_key": "openai_compatible", "status": "active"},
            )
            assert r.status_code == 200, r.text
            model = shared_model(app_db, model_key="fake-gpt")
            switch(c, people.platform, world.a.slug, True)
            yield Shi(c, server, model)


def test_a_client_with_the_shi_switch_off_cannot_reach_shi_through_the_panels(
    shi: Shi, world: World, people: People, owner_db: Database
) -> None:
    """Plan Review Focus 5: B (switch off) naming the shared SHI model in the prompt or agent test
    panel gets the unknown-model answer, nothing reaches SHI's endpoint, no run row or usage row
    is written; A (switch on) reaches it through the same panel."""
    c, server = shi.client, shi.server
    A, B = org(world.a.slug), org(world.b.slug)
    sides: dict[str, tuple[str, str, str, str]] = {}
    for label, base, email, tenant in (
        ("a", A, people.owner_a, world.a.tenant),
        ("b", B, people.owner_b, world.b.tenant),
    ):
        h = as_user(c, email)
        stack = fake_model_stack(c, h, base, conn_name=f"fake-{label}")
        prompt = art(c, h, base, "prompts", "p", prompt_spec(), scope_of(tenant))
        pv = publish(c, h, base, "prompts", prompt["id"])
        agent = art(
            c, h, base, "agents", "ag", agent_spec(pv["id"], stack["model"]), scope_of(tenant)
        )
        sides[label] = (prompt["id"], agent["id"], stack["model"], pv["id"])
    # B's own fake model works through the prompt panel (control), and writes a run row
    hb = as_user(c, people.owner_b)
    body = {"version_id": sides["b"][3], "variables": {"message": "hi"}}
    ok = c.post(
        f"{B}/prompts/{sides['b'][0]}/test", json={**body, "model_id": sides["b"][2]}, headers=hb
    )
    assert ok.status_code == 200, ok.text
    # B names the shared SHI model: the unknown-model answer, nothing sent
    missing = c.post(
        f"{B}/prompts/{sides['b'][0]}/test", json={**body, "model_id": str(uuid4())}, headers=hb
    )
    refused = c.post(
        f"{B}/prompts/{sides['b'][0]}/test", json={**body, "model_id": shi.model}, headers=hb
    )
    _same_404(refused, missing)
    assert server.bodies == []
    runs = c.get(f"{B}/prompts/{sides['b'][0]}/test-runs", headers=hb).json()["items"]
    assert len(runs) == 1  # only the fake-model run
    # the agent panel: B's agent naming the shared model cannot even be assembled for the test
    draft = c.get(f"{B}/agents/{sides['b'][1]}/draft", headers=hb).json()
    edit(
        c,
        hb,
        B,
        "agents",
        sides["b"][1],
        agent_spec(sides["b"][3], shi.model),
        draft["row_version"],
    )
    agent_test = c.post(
        f"{B}/agents/{sides['b'][1]}/test",
        json={"version_id": "draft", "input": "hi"},
        headers=hb,
    )
    assert agent_test.status_code in (404, 422), agent_test.text
    assert server.bodies == []
    with owner_db.session() as o:
        denied = o.execute(
            text(
                "SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t "
                "AND model_id = CAST(:m AS uuid)"
            ),
            {"t": world.b.tenant, "m": shi.model},
        ).scalar_one()
    assert denied == 0
    # A (switch on) reaches SHI's endpoint through the agent panel (the prompt panel cannot
    # store a run of a shared model yet: carry_forward, build 4b)
    ha = as_user(c, people.owner_a)
    draft = c.get(f"{A}/agents/{sides['a'][1]}/draft", headers=ha).json()
    edit(
        c,
        ha,
        A,
        "agents",
        sides["a"][1],
        agent_spec(sides["a"][3], shi.model),
        draft["row_version"],
    )
    reached = c.post(
        f"{A}/agents/{sides['a'][1]}/test",
        json={"version_id": "draft", "input": "hi shi"},
        headers=ha,
    )
    assert reached.status_code == 200, reached.text
    assert len(server.bodies) == 1 and "hi shi" in str(server.bodies[0])
