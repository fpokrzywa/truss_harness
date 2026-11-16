"""P4 skills and agents (contract sections 4-5, verification items 5-8)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login, post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, VIEWER, A, B, fake_model_stack
from tests._support.p4util import (
    agent_spec,
    art,
    as_user,
    builtin_tool_spec,
    codes,
    edit,
    make_user,
    prompt_spec,
    publish,
    skill_spec,
    tenant_scope,
)


def issues(client: TestClient, h: dict[str, str], kind: str, aid: str) -> dict[str, Any]:
    r = client.post(f"{A}/{kind}/{aid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def error_codes(client: TestClient, h: dict[str, str], kind: str, aid: str) -> list[str]:
    return sorted(e["code"] for e in issues(client, h, kind, aid)["errors"])


class Stack:
    """A published prompt, tool and skill plus two fake models."""

    def __init__(self, client: TestClient, h: dict[str, str], owner: Session) -> None:
        self.client, self.h, self.owner = client, h, owner
        self.scope = tenant_scope(owner)
        self.m1 = fake_model_stack(client, h, model_key="chat-a", conn_name="fa")["model"]
        self.m2 = fake_model_stack(client, h, model_key="chat-b", conn_name="fb")["model"]
        self.prompt = art(client, h, "prompts", "agent-prompt", prompt_spec(), self.scope)
        self.prompt_v1 = publish(client, h, "prompts", self.prompt["id"])
        self.tool = art(client, h, "tools", "echo", builtin_tool_spec(), self.scope)
        self.tool_v1 = publish(client, h, "tools", self.tool["id"])
        self.skill = art(client, h, "skills", "greeting", skill_spec(), self.scope)
        self.skill_v1 = publish(client, h, "skills", self.skill["id"])

    def spec(self, **over: Any) -> dict[str, Any]:
        base = {
            "tool_version_ids": [f"latest:{self.tool['id']}"],
            "skill_version_ids": [f"latest:{self.skill['id']}"],
        }
        return agent_spec(f"latest:{self.prompt['id']}", self.m1, **{**base, **over})

    def agent(self, key: str = "support-agent", **over: Any) -> Any:
        return art(self.client, self.h, "agents", key, self.spec(**over), self.scope)

    def new_version(self, kind: str, artifact: Any, spec: dict[str, Any]) -> Any:
        edit(self.client, self.h, kind, artifact["id"], spec, 0)
        return publish(self.client, self.h, kind, artifact["id"], "next")


# ================================================================== 5. skills
def test_skill_lifecycle_dependencies_and_cycles(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    a = art(client, h, "skills", "skill-a", skill_spec("A instructions"), scope)
    a1 = publish(client, h, "skills", a["id"])
    assert a1["pins"] == []

    b = art(
        client,
        h,
        "skills",
        "skill-b",
        skill_spec("B instructions", dependencies=[{"skill_version_id": a1["id"]}]),
        scope,
    )
    b1 = publish(client, h, "skills", b["id"])
    assert [(p["role"], p["version_id"], p["key"]) for p in b1["pins"]] == [
        ("skill_dependency", a1["id"], "skill-a")
    ]

    # A v2 depending on B v1 closes the loop A -> B -> A: rejected (also with a self-dependency)
    edit(
        client,
        h,
        "skills",
        a["id"],
        skill_spec("A v2", dependencies=[{"skill_version_id": b1["id"]}]),
        0,
    )
    v = issues(client, h, "skills", a["id"])
    assert v["valid"] is False and [e["code"] for e in v["errors"]] == ["dependency_cycle"]
    assert client.post(f"{A}/skills/{a['id']}/publish", json={}, headers=h).status_code == 422
    edit(
        client,
        h,
        "skills",
        a["id"],
        skill_spec("A v2", dependencies=[{"skill_version_id": a1["id"]}]),
        1,
    )
    assert error_codes(client, h, "skills", a["id"]) == ["dependency_cycle"]  # itself
    edit(client, h, "skills", a["id"], skill_spec("A v2"), 2)
    assert issues(client, h, "skills", a["id"])["valid"] is True

    # instructional only: executable fields are refused; applicable_tools is advisory
    bad = skill_spec("do it", command="rm -rf /", tool_binding={"connection_id": "x"})
    edit(client, h, "skills", a["id"], bad, 3)
    assert error_codes(client, h, "skills", a["id"]) == [
        "executable_content_forbidden",
        "executable_content_forbidden",
    ]
    edit(
        client,
        h,
        "skills",
        a["id"],
        skill_spec(
            "x",
            required_inputs=[{"name": "q", "description": "d", "type": "string", "script": "1"}],
        ),
        4,
    )
    assert error_codes(client, h, "skills", a["id"]) == ["executable_content_forbidden"]
    advisory = skill_spec("use the tool", applicable_tools=["echo"])
    edit(client, h, "skills", a["id"], advisory, 5)
    out = issues(client, h, "skills", a["id"])
    assert out["valid"] is True and [w["code"] for w in out["warnings"]] == ["advisory_only"]
    # limits and shapes
    edit(client, h, "skills", a["id"], skill_spec("x" * 2001), 6)
    assert error_codes(client, h, "skills", a["id"]) == ["too_long"]
    edit(
        client,
        h,
        "skills",
        a["id"],
        skill_spec(
            "",
            required_inputs=[{"name": "1x"}, {"name": "a"}, {"name": "a"}],
            dependencies=[{"skill_version_id": "nope"}],
        ),
        7,
    )
    assert error_codes(client, h, "skills", a["id"]) == [
        "duplicate",
        "invalid_value",
        "invalid_value",
        "required",
    ]


# ================================================================== 6 + 7 + 8. agents
def test_agent_pins_components_and_is_reproducible(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    agent = st.agent()
    assert agent["has_draft"] and agent["status"] == "active"
    v = issues(client, h, "agents", agent["id"])
    assert v["valid"] is True, v

    # the draft keeps `latest:` references until it is published
    draft = client.get(f"{A}/agents/{agent['id']}/draft", headers=h).json()
    assert draft["spec"]["prompt_version_id"] == f"latest:{st.prompt['id']}"

    a1 = publish(client, h, "agents", agent["id"], "first agent")
    spec = a1["spec"]
    assert spec["prompt_version_id"] == st.prompt_v1["id"]
    assert spec["tool_version_ids"] == [st.tool_v1["id"]]
    assert spec["skill_version_ids"] == [st.skill_v1["id"]]
    assert {(p["role"], p["version_id"]) for p in a1["pins"]} == {
        ("prompt", st.prompt_v1["id"]),
        ("tool", st.tool_v1["id"]),
        ("skill", st.skill_v1["id"]),
    }
    assert spec["approval_rules"]["require_approval_for"] == ["write_non_idempotent", "destructive"]

    resolved1 = client.get(f"{A}/agents/{agent['id']}/versions/{a1['id']}/resolved", headers=h)
    assert resolved1.status_code == 200
    r1 = resolved1.json()
    assert (
        r1["spec_hash"] == a1["spec_hash"]
        and r1["prompt_version"]["version_id"] == st.prompt_v1["id"]
    )
    assert [t["artifact"]["key"] for t in r1["tools"]] == ["echo"]
    assert [(m["id"], m["role"]) for m in r1["models"]] == [(st.m1, "primary")]
    assert r1["models"][0]["model_key"] == "chat-a"
    assert [s["direct"] for s in r1["skills"]] == [True]

    # publishing NEW prompt / tool / skill versions never changes the published agent version
    st.new_version(
        "prompts", st.prompt, prompt_spec(sections={"system": "CHANGED", "context": "{{message}}"})
    )
    st.new_version("tools", st.tool, builtin_tool_spec(timeout_s=9))
    st.new_version("skills", st.skill, skill_spec("CHANGED instructions"))
    again = client.get(f"{A}/agents/{agent['id']}/versions/{a1['id']}/resolved", headers=h).json()
    assert again == r1
    same = client.get(f"{A}/agents/{agent['id']}/versions/{a1['id']}", headers=h).json()
    assert same["spec_hash"] == a1["spec_hash"] and same["spec"] == a1["spec"]

    # a new agent version re-resolves `latest:` to the new component versions
    edit(client, h, "agents", agent["id"], st.spec(), 0)
    a2 = publish(client, h, "agents", agent["id"], "pick up the new components")
    assert (
        a2["spec"]["prompt_version_id"] != st.prompt_v1["id"] and a2["spec_hash"] != a1["spec_hash"]
    )
    r2 = client.get(f"{A}/agents/{agent['id']}/versions/{a2['id']}/resolved", headers=h).json()
    assert r2["prompt_version"]["version_no"] == 2 and r2["skills"][0]["version_no"] == 2
    diff = client.get(
        f"{A}/agents/{agent['id']}/diff", params={"from": a1["id"], "to": a2["id"]}, headers=h
    ).json()["changes"]
    assert {c["path"] for c in diff} == {
        "prompt_version_id",
        "tool_version_ids[0]",
        "skill_version_ids[0]",
    }
    assert (
        client.get(
            f"{A}/agents/{agent['id']}/versions/{st.prompt_v1['id']}/resolved", headers=h
        ).status_code
        == 404
    )

    # pins forbid deleting a referenced version at the database level
    with_pin = owner_session.execute(
        text("SELECT count(*) FROM shifde.artifact_version_pins WHERE pinned_version_id = :v"),
        {"v": st.prompt_v1["id"]},
    ).scalar_one()
    assert with_pin == 1


def test_agent_assembly_test_renders_the_full_context(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    agent = st.agent()
    out = post(
        client,
        f"{A}/agents/{agent['id']}/test",
        h,
        {"version_id": "draft", "variables": {}, "input": "where </untrusted> is my order?"},
        200,
    )
    msgs = out["assembled"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "user"]
    assert msgs[0]["content"].startswith("You are a support assistant.")
    assert "# Skills" in msgs[0]["content"] and "Always greet the customer." in msgs[0]["content"]
    # the run input reaches the prompt variable (untrusted, escaped) and the last message
    assert (
        '<untrusted source="variable:message">where &lt;/untrusted&gt; is my order?</untrusted>'
        in msgs[1]["content"]
    )
    assert (
        msgs[2]["content"]
        == '<untrusted source="input">where &lt;/untrusted&gt; is my order?</untrusted>'
    )
    assert [t["name"] for t in out["assembled"]["tools"]] == ["echo"]
    assert out["assembled"]["tools"][0]["requires_approval"] is False
    assert out["output"].startswith("fake: ") and out["usage"]["input_tokens"] > 0
    assert out["model_id"] == st.m1
    published = publish(client, h, "agents", agent["id"])
    again = post(
        client,
        f"{A}/agents/{agent['id']}/test",
        h,
        {"version_id": published["id"], "variables": {"message": "explicit"}, "input": ""},
        200,
    )
    assert (
        '"explicit"' not in again["output"]
        and "explicit" in again["assembled"]["messages"][1]["content"]
    )
    assert len(again["assembled"]["messages"]) == 2  # no input message when the input is empty
    # a bad variable is a 422 with a path
    bad = client.post(
        f"{A}/agents/{agent['id']}/test",
        json={"version_id": published["id"], "variables": {"nope": 1}, "input": "x"},
        headers=h,
    )
    assert bad.status_code == 422 and codes(bad.json()) == {"unknown_variable"}
    # the test needs a model it may invoke
    st2 = client.get(f"{A}/agents/{agent['id']}/versions/{published['id']}/resolved", headers=h)
    assert st2.json()["models"][0]["id"] == st.m1


def test_publish_rejects_models_outside_the_allowlist_and_unknown_models(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    tid = st.scope["id"]
    put = client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "tenant", "scope_id": tid},
        json={"enabled": True, "model_ids": [st.m1]},
        headers=h,
    )
    assert put.status_code == 200

    ok = st.agent("ok-agent")
    assert issues(client, h, "agents", ok["id"])["valid"] is True
    disallowed = art(
        client,
        h,
        "agents",
        "bad-model",
        st.spec(model={"model_id": st.m2, "fallback_model_ids": []}),
        st.scope,
    )
    v = issues(client, h, "agents", disallowed["id"])
    assert [(e["path"], e["code"]) for e in v["errors"]] == [
        ("model.model_id", "model_not_allowed")
    ]
    pub = client.post(
        f"{A}/agents/{disallowed['id']}/publish", json={"change_note": "x"}, headers=h
    )
    assert pub.status_code == 422 and codes(pub.json()) == {"model_not_allowed"}
    assert pub.json()["errors"][0]["loc"] == ["spec", "model", "model_id"]
    fallback = art(
        client,
        h,
        "agents",
        "bad-fallback",
        st.spec(model={"model_id": st.m1, "fallback_model_ids": [st.m2]}),
        st.scope,
    )
    assert [
        (e["path"], e["code"]) for e in issues(client, h, "agents", fallback["id"])["errors"]
    ] == [("model.fallback_model_ids[0]", "model_not_allowed")]
    unknown = art(
        client,
        h,
        "agents",
        "ghost-model",
        st.spec(
            model={"model_id": "00000000-0000-0000-0000-000000000000", "fallback_model_ids": []}
        ),
        st.scope,
    )
    assert error_codes(client, h, "agents", unknown["id"]) == ["model_not_found"]

    # an allowlist at the agent's own (narrower) scope applies too: workspace-owned agent
    ws = client.get(f"{A}/workspaces/main", headers=h).json()
    client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "tenant", "scope_id": tid},
        json={"enabled": False, "model_ids": []},
        headers=h,
    )
    client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "workspace", "scope_id": ws["id"]},
        json={"enabled": True, "model_ids": [st.m1]},
        headers=h,
    )
    in_ws = art(
        client,
        h,
        "agents",
        "ws-agent",
        st.spec(model={"model_id": st.m2, "fallback_model_ids": []}),
        {"type": "workspace", "id": ws["id"]},
    )
    # prompt/tool/skill are tenant-visible scope artifacts: visible to the tenant admin anyway
    assert "model_not_allowed" in error_codes(client, h, "agents", in_ws["id"])
    assert (
        issues(client, h, "agents", disallowed["id"])["valid"] is True
    )  # tenant scope is open now

    # a deprecated model is unusable; a route is checked through its models
    client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "workspace", "scope_id": ws["id"]},
        json={"enabled": False, "model_ids": []},
        headers=h,
    )
    route = post(
        client,
        f"{A}/model-routes",
        h,
        {
            "name": "r1",
            "scope": {"type": "tenant", "id": tid},
            "primary_model_id": st.m1,
            "fallback_model_ids": [st.m2],
        },
        201,
    )
    routed = art(client, h, "agents", "routed", st.spec(model={"route_id": route["id"]}), st.scope)
    assert issues(client, h, "agents", routed["id"])["valid"] is True
    edit(
        client,
        h,
        "agents",
        routed["id"],
        st.spec(model={"route_id": "00000000-0000-0000-0000-000000000000"}),
        1,
    )
    assert error_codes(client, h, "agents", routed["id"]) == ["unresolved_reference"]
    edit(client, h, "agents", routed["id"], st.spec(model={"route_id": route["id"]}), 2)
    models = client.get(f"{A}/models", headers=h).json()["items"]
    m2 = next(m for m in models if m["id"] == st.m2)
    patched = client.patch(
        f"{A}/models/{st.m2}",
        json={"lifecycle": "deprecated", "row_version": m2["row_version"]},
        headers=h,
    )
    assert patched.status_code == 200
    assert error_codes(client, h, "agents", routed["id"]) == ["model_unavailable"]
    a_routed = client.get(f"{A}/agents/{routed['id']}/draft", headers=h).json()
    assert a_routed["spec"]["model"] == {"route_id": route["id"]}


def test_agent_validation_rules(client: TestClient, world: World, owner_session: Session) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    agent = st.agent()
    aid = agent["id"]

    def check(spec: dict[str, Any], rv: int) -> list[str]:
        edit(client, h, "agents", aid, spec, rv)
        return error_codes(client, h, "agents", aid)

    base = st.spec()
    assert check(base, 1) == []
    assert check({**base, "limits": {**base["limits"], "max_iterations": 10_000}}, 2) == [
        "exceeds_maximum"
    ]
    bad_limits = {**base, "limits": {"max_iterations": 0, "max_tool_calls": 1, "max_tokens": 1}}
    assert check(bad_limits, 3) == ["invalid_value", "required"]
    assert check({**base, "variables_map": {}}, 4) == ["unsatisfied_variable"]
    assert check({**base, "variables_map": {"message": "{{input}}", "ghost": "x"}}, 5) == [
        "unknown_variable"
    ]
    assert check({**base, "variables_map": {"message": "{{other}}"}}, 6) == ["invalid_template"]
    assert check({**base, "variables_map": {"message": "{{input.field}} and {{input}}"}}, 7) == []
    assert check({**base, "tool_version_ids": base["tool_version_ids"] * 2}, 8) == ["duplicate"]
    assert check({**base, "model": {"model_id": st.m1, "route_id": "x"}}, 9) == ["invalid_value"]
    assert check({**base, "memory": {"mode": "forever", "max_turns": 1}}, 10) == ["invalid_value"]
    assert check({**base, "execution_permissions": {"allowed_principal_types": []}}, 11) == [
        "invalid_value"
    ]
    assert check(
        {
            **base,
            "execution_permissions": {
                "allowed_principal_types": ["user"],
                "required_permission": "agent:read",
            },
        },
        12,
    ) == ["invalid_value"]
    assert check({**base, "prompt_version_id": "nope"}, 13) == ["invalid_value"]
    assert check({k: v for k, v in base.items() if k != "prompt_version_id"}, 14) == ["required"]
    # destructive always requires approval: forced into the pinned spec, with a warning
    weak = {**base, "approval_rules": {"require_approval_for": ["read"]}}
    edit(client, h, "agents", aid, weak, 15)
    out = issues(client, h, "agents", aid)
    assert out["valid"] is True and [w["code"] for w in out["warnings"]] == ["destructive_forced"]
    published = publish(client, h, "agents", aid)
    assert published["spec"]["approval_rules"]["require_approval_for"] == ["read", "destructive"]
    # a pinned version id works as well as `latest:`, and a draft id cannot be referenced
    edit(client, h, "agents", aid, {**base, "prompt_version_id": st.prompt_v1["id"]}, 0)
    assert error_codes(client, h, "agents", aid) == []
    own_draft = client.get(f"{A}/agents/{aid}/draft", headers=h).json()["id"]
    edit(client, h, "agents", aid, {**base, "prompt_version_id": own_draft}, 1)
    assert error_codes(client, h, "agents", aid) == ["wrong_kind"]


def test_retired_and_restricted_components_are_not_referenced(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    agent = st.agent()
    aid = agent["id"]
    pinned = st.spec(tool_version_ids=[st.tool_v1["id"]])

    def state(kind: str, art_id: str, vid: str, to: str) -> None:
        r = client.post(f"{A}/{kind}/{art_id}/versions/{vid}/state", json={"state": to}, headers=h)
        assert r.status_code == 200, r.text

    state("tools", st.tool["id"], st.tool_v1["id"], "deprecated")
    edit(client, h, "agents", aid, pinned, 1)
    out = issues(client, h, "agents", aid)
    assert out["valid"] is True and [w["code"] for w in out["warnings"]] == ["reference_deprecated"]
    state("tools", st.tool["id"], st.tool_v1["id"], "retired")
    assert error_codes(client, h, "agents", aid) == ["reference_retired"]
    edit(client, h, "agents", aid, st.spec(), 2)  # latest: has no published version left
    assert error_codes(client, h, "agents", aid) == ["unresolved_reference"]

    # a tool can restrict which agents may use it
    restricted = art(
        client,
        h,
        "tools",
        "restricted",
        builtin_tool_spec(
            invocation_policy={"allowed_agent_ids": ["00000000-0000-0000-0000-0000000000aa"]}
        ),
        st.scope,
    )
    rv = publish(client, h, "tools", restricted["id"])
    edit(client, h, "agents", aid, st.spec(tool_version_ids=[rv["id"]]), 3)
    assert error_codes(client, h, "agents", aid) == ["tool_not_allowed_for_agent"]
    allowed = builtin_tool_spec(invocation_policy={"allowed_agent_ids": [aid]})
    edit(client, h, "tools", restricted["id"], allowed, 0)
    rv2 = publish(client, h, "tools", restricted["id"])
    edit(client, h, "agents", aid, st.spec(tool_version_ids=[rv2["id"]]), 4)
    assert error_codes(client, h, "agents", aid) == []

    # a disabled artifact cannot be referenced either
    skill = client.get(f"{A}/skills/{st.skill['id']}", headers=h).json()
    client.patch(
        f"{A}/skills/{st.skill['id']}",
        json={"status": "disabled", "row_version": skill["row_version"]},
        headers=h,
    )
    assert "reference_disabled" in error_codes(client, h, "agents", aid)


def test_agent_activation_and_permissions(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    agent = st.agent()
    url = f"{A}/agents/{agent['id']}/status"
    off = post(client, url, h, {"status": "disabled"}, 200)
    assert off["status"] == "disabled"
    assert [
        a["key"] for a in client.get(f"{A}/agents?status=disabled", headers=h).json()["items"]
    ] == ["support-agent"]
    assert post(client, url, h, {"status": "active"}, 200)["status"] == "active"
    assert client.post(url, json={"status": "bogus"}, headers=h).status_code == 422

    make_user(client, owner_session, h, "dev2@demo-a.local", "developer", world.passwords)
    dev = as_user(client, world.passwords, "dev2@demo-a.local")
    assert client.post(url, json={"status": "disabled"}, headers=dev).status_code == 403
    # the developer can author and publish agents but not activate them (agent:activate)
    patched = client.patch(
        f"{A}/agents/{agent['id']}", json={"status": "disabled", "row_version": 1}, headers=dev
    )
    assert patched.status_code == 403
    assert client.post(f"{A}/agents/{agent['id']}/publish", json={}, headers=dev).status_code == 200
    viewer = login(client, world.passwords, VIEWER)
    assert client.post(url, json={"status": "disabled"}, headers=viewer).status_code == 403
    assert (
        client.post(
            f"{A}/agents/{agent['id']}/test",
            json={"version_id": "draft", "variables": {}, "input": "x"},
            headers=viewer,
        ).status_code
        == 403
    )
    h = login(client, world.passwords, ADMIN)
    retired = post(client, url, h, {"status": "disabled"}, 200)
    assert retired["status"] == "disabled"
    r = client.patch(
        f"{A}/agents/{agent['id']}",
        json={"status": "retired", "row_version": retired["row_version"]},
        headers=h,
    )
    assert r.status_code == 200
    assert client.post(url, json={"status": "active"}, headers=h).status_code == 409  # terminal


def test_cross_tenant_references_and_ids_are_not_visible(
    client: TestClient, world: World, owner_session: Session
) -> None:
    hb = login(client, world.passwords, ADMIN_B)
    scope_b = tenant_scope(owner_session, "demo-b")
    pb = art(client, hb, "prompts", "b-prompt", prompt_spec(), scope_b, org=B)
    pb_v = publish(client, hb, "prompts", pb["id"], org=B)
    model_b = fake_model_stack(client, hb, org=B)["model"]

    h = login(client, world.passwords, ADMIN)
    st = Stack(client, h, owner_session)
    # another tenant's artifacts and versions do not exist for us
    for path in (
        f"prompts/{pb['id']}",
        f"prompts/{pb['id']}/versions",
        f"prompts/{pb['id']}/draft",
    ):
        assert client.get(f"{A}/{path}", headers=h).status_code == 404
    assert (
        client.get(f"{A}/prompts/{st.prompt['id']}/versions/{pb_v['id']}", headers=h).status_code
        == 404
    )
    assert client.get(f"{A}/prompts/{pb['id']}", headers=h).status_code == 404
    assert client.get(f"{A}/prompts", headers=h).json()["items"][0]["key"] == "agent-prompt"
    assert (
        client.patch(
            f"{A}/prompts/{pb['id']}", json={"name": "x", "row_version": 1}, headers=h
        ).status_code
        == 404
    )
    assert client.post(f"{A}/prompts/{pb['id']}/publish", json={}, headers=h).status_code == 404
    assert client.get(f"{B}/prompts", headers=h).status_code == 404  # not a member of demo-b

    # references to another tenant's versions / models are rejected at publish
    agent = art(client, h, "agents", "leaky", agent_spec(pb_v["id"], model_b), st.scope)
    assert error_codes(client, h, "agents", agent["id"]) == [
        "model_not_found",
        "unresolved_reference",
    ]
    pub = client.post(f"{A}/agents/{agent['id']}/publish", json={}, headers=h)
    assert pub.status_code == 422 and codes(pub.json()) == {
        "model_not_found",
        "unresolved_reference",
    }
    edit(client, h, "agents", agent["id"], agent_spec(f"latest:{pb['id']}", st.m1), 1)
    assert error_codes(client, h, "agents", agent["id"]) == ["unresolved_reference"]
    # a prompt composing another tenant's prompt version is rejected as well
    comp = art(
        client,
        h,
        "prompts",
        "composer",
        prompt_spec(composed_of=[{"prompt_version_id": pb_v["id"], "placement": "system_prefix"}]),
        st.scope,
    )
    assert error_codes(client, h, "prompts", comp["id"]) == ["unresolved_reference"]
    # creating at another tenant's scope id is a 404
    r = client.post(
        f"{A}/prompts",
        json={"key": "x1", "name": "x", "scope": scope_b, "visibility": "scope", "spec": {}},
        headers=h,
    )
    assert r.status_code == 404
    assert tenant_id(owner_session, "demo-a") != tenant_id(owner_session, "demo-b")
