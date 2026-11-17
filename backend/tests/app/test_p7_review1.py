"""P7 review round 1: scope of action runs, relationship-heavy types, tool visibility, decimal
keys, per-property row visibility, lineage counts and idempotent replay."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p4util import art, builtin_tool_spec, publish
from tests._support.p5util import Stack, steps, thread_worker, tool_call_reply
from tests._support.p7util import (
    ACCOUNT,
    CUSTOMER,
    R_ACCOUNTS,
    R_REQUESTS,
    REQUEST,
    World7,
    mapping_body,
    q,
    reference_spec,
    rows_of,
    sample_admin_exec,
)
from tests.app.test_p7_exec import invoke, wait


@pytest.fixture
def w(client: TestClient, world: World, owner_session: Session, sample_db_url: str) -> World7:
    return World7(client, world, owner_session, sample_db_url)


def echo_tool(w: World7, key: str = "echo-summary") -> str:
    spec = builtin_tool_spec("util.echo", side_effect_class="none", required_permissions=[])
    tool = art(w.client, w.h, "tools", key, spec, w.scope)
    return str(publish(w.client, w.h, "tools", tool["id"])["id"])


def action_spec(tool_version: str) -> dict[str, Any]:
    spec = reference_spec(tool_version)
    spec["actions"][0]["param_mapping"] = {"text": "$object.summary"}
    return spec


# ================================================================== 1. action run scope
def test_action_run_must_stay_inside_the_ontologys_workspace(w: World7) -> None:
    ws = w.client.get("/api/v1/orgs/demo-a/workspaces/main", headers=w.h).json()
    assert w.client.post(
        "/api/v1/orgs/demo-a/workspaces", json={"slug": "other", "name": "Other"}, headers=w.h
    ).status_code in (200, 201)
    assert w.client.post(
        "/api/v1/orgs/demo-a/workspaces/other/projects",
        json={"slug": "x", "name": "X"},
        headers=w.h,
    ).status_code in (200, 201)
    spec = action_spec(echo_tool(w))
    onto = w.ready("ws-scoped", spec, scope={"type": "workspace", "id": ws["id"]})
    w.allow_all(onto["id"], ("role", w.admin_role))
    url = f"{A}/ontologies/{onto['id']}/actions/cs.escalate_request/invoke"
    foreign = w.client.post(
        url,
        json={"object_pk": "3", "workspace": "other", "project": "x"},
        headers=w.h,
    )
    assert foreign.status_code == 422 and "workspace" in foreign.text
    assert w.client.get(f"{A}/runs", headers=w.h).json()["items"] == []  # nothing was created
    own = w.client.post(
        url, json={"object_pk": "3", "workspace": "main", "project": "sample"}, headers=w.h
    )
    assert own.status_code == 202 and own.json()["workspace"]["slug"] == "main"


# ================================================================== 2. busy object types
def busy_spec(tool_version: str, extra: int = 10) -> dict[str, Any]:
    spec = action_spec(tool_version)
    spec["actions"][0]["object_type"] = CUSTOMER
    spec["actions"][0]["param_mapping"] = {"text": "$object.name"}
    base = next(r for r in spec["relationships"] if r["key"] == R_ACCOUNTS)
    for i in range(extra):
        spec["relationships"].append({**base, "key": f"cs.more_accounts_{i}", "label": f"more {i}"})
    return spec


def test_object_view_and_actions_work_for_types_with_many_relationships(
    w: World7, settings: Settings
) -> None:
    spec = busy_spec(echo_tool(w))
    onto = w.create("busy", spec)
    for ot in spec["object_types"]:
        w.put_mapping(onto["id"], mapping_body(w.pg, "object_type", ot["key"], spec))
    for rel in spec["relationships"]:
        base = rel["key"] if not rel["key"].startswith("cs.more_accounts_") else R_ACCOUNTS
        body = mapping_body(w.pg, "relationship", base, spec)
        w.put_mapping(onto["id"], {**body, "target_key": rel["key"]})
    onto["version"] = publish(w.client, w.h, "ontologies", onto["id"])
    w.allow_all(onto["id"], ("role", w.admin_role))
    got = w.client.get(f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/1", headers=w.h)
    assert got.status_code == 200, got.text
    assert len(got.json()["relationships"]) >= 11
    # an action over that type does not resolve relationship summaries at all
    r = invoke(w, onto, {"object_pk": "1"})
    assert r.status_code == 202, r.text
    with thread_worker(settings):
        done = wait(w.client, w.h, r.json()["id"])
    assert done["status"] == "completed", done
    # and a query may still use at most 8 relationships
    hops = [{"relationship": f"cs.more_accounts_{i}", "select": ["id"]} for i in range(9)]
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], traverse=hops),
            headers=w.h,
        ).status_code
        == 422
    )


# ================================================================== 3. tool visibility
def test_agent_tool_cannot_read_an_ontology_the_principal_cannot_see(
    w: World7, settings: Settings
) -> None:
    onto = w.ready("secret-onto", visibility="private")  # owned by the administrator
    call = tool_call_reply(
        "ontology-query", {"ontology_id": onto["id"], "query": q(CUSTOMER, ["id"])}
    )
    stack = Stack(
        w.client,
        w.h,
        w.owner,
        script=[
            {"match": "Tool results", "reply": "done"},
            {"match": "list customers", "reply": call},
        ],
        tool=builtin_tool_spec("ontology.query", side_effect_class="read"),
        tool_key="ontology-query",
    )
    perms = [
        "workspace:read", "project:read", "ontology:read", "ontology:query", "run:read",
        "agent:read", "agent:execute", "tool:read", "tool:invoke", "model:read", "model:invoke",
    ]  # fmt: skip
    perms.append("prompt:read")  # setup (B-2432): run start reads the pinned prompt (B-2270)
    dev, _, role = w.member("dev@demo-a.local", perms)
    w.policy(onto["id"], CUSTOMER, ("role", role))
    # through the API the private ontology does not exist for the developer ...
    assert (
        dev.client.post(
            f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=dev
        ).status_code
        == 404
    )
    # ... and the model cannot reach it through the tool either
    run = dev.client.post(
        f"{A}/runs",
        headers=dev,
        json={
            "kind": "agent",
            "agent_id": stack.agent["id"],
            "workspace": "main",
            "project": "sample",
            "input": "list customers",
        },
    ).json()
    with thread_worker(settings):
        done = wait(dev.client, dev, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "authz_denied", done
    assert w.lineage(onto["id"]) == []
    assert [s["status"] for s in steps(w.client, w.h, run["id"]) if s["kind"] == "tool_call"] == []


# ================================================================== 4. decimal keys
def test_traversal_and_counts_work_for_numeric_decimal_keys(w: World7, sample_db_url: str) -> None:
    reader = sample_db_url.split("//")[1].split(":")[0]
    sample_admin_exec(
        sample_db_url,
        "CREATE TABLE nk_parent (id numeric(10,0) PRIMARY KEY, label text NOT NULL)",
        "CREATE TABLE nk_child (id numeric(10,0) PRIMARY KEY, parent_id numeric(10,0) NOT NULL, "
        "note text NOT NULL)",
        "INSERT INTO nk_parent VALUES (12, 'p12'), (30, 'p30')",
        "INSERT INTO nk_child VALUES (100, 12, 'a'), (101, 12, 'b'), (102, 30, 'c')",
        f'GRANT SELECT ON nk_parent, nk_child TO "{reader}"',
    )
    try:
        w.discover()

        def prop(key: str, ptype: str) -> dict[str, Any]:
            return {"key": key, "type": ptype, "nullable": False, "classification": "internal"}

        spec = {
            "namespaces": [{"key": "nk", "label": "NK"}],
            "object_types": [
                {"key": "nk.parent", "label": "P", "namespace": "nk", "primary_key": ["id"],
                 "properties": [prop("id", "number"), prop("label", "string")]},
                {"key": "nk.child", "label": "C", "namespace": "nk", "primary_key": ["id"],
                 "properties": [prop("id", "number"), prop("parent_id", "number"), prop("note", "string")]},
            ],
            "relationships": [
                {"key": "nk.children", "label": "children", "from": "nk.parent", "to": "nk.child",
                 "cardinality": "one_to_many", "constraints": {"required": False}}
            ],
            "actions": [],
            "ui": {},
        }  # fmt: skip
        onto = w.create("nk", spec)

        def put(
            kind: str, key: str, relation: str, columns: dict[str, str] | None, key_map: Any = None
        ) -> None:
            body: dict[str, Any] = {
                "target_kind": kind, "target_key": key, "connection_id": w.pg,
                "relation": {"schema": "public", "name": relation}, "filters": [],
            }  # fmt: skip
            if columns is not None:
                body["column_map"] = columns
            if key_map is not None:
                body["key_map"] = key_map
            w.put_mapping(onto["id"], body)

        put("object_type", "nk.parent", "nk_parent", {"id": "id", "label": "label"})
        put(
            "object_type",
            "nk.child",
            "nk_child",
            {"id": "id", "parent_id": "parent_id", "note": "note"},
        )
        put(
            "relationship",
            "nk.children",
            "nk_child",
            None,
            {"from_column": "parent_id", "to_column": "id"},
        )
        onto["version"] = publish(w.client, w.h, "ontologies", onto["id"])
        for ot in ("nk.parent", "nk.child"):
            w.policy(onto["id"], ot, ("role", w.admin_role))
        out = w.query(
            onto["id"],
            q(
                "nk.parent",
                ["id", "label"],
                traverse=[{"relationship": "nk.children", "select": ["id", "note"]}],
            ),
        )
        links = {
            r["label"]: sorted(c["note"] for c in r["_links"]["nk.children"]) for r in out["rows"]
        }
        assert links == {"p12": ["a", "b"], "p30": ["c"]}
        obj = w.client.get(f"{A}/ontologies/{onto['id']}/objects/nk.parent/12", headers=w.h).json()
        assert {x["key"]: x["count"] for x in obj["relationships"]} == {"nk.children": 2}
    finally:
        sample_admin_exec(
            sample_db_url, "DROP TABLE IF EXISTS nk_child", "DROP TABLE IF EXISTS nk_parent"
        )


# ================================================================== 5. object view row visibility
def test_object_view_returns_the_properties_each_grant_admits_for_that_row(
    w: World7, sample_db_url: str
) -> None:
    onto = w.ready()
    dev, _, role = w.member("dev@demo-a.local")
    w.policy(onto["id"], CUSTOMER, ("role", role), ["id", "name"])
    w.policy(onto["id"], CUSTOMER, ("role", role), ["email"], 'row.region == "north"')
    south = rows_of(sample_db_url, "SELECT id FROM customers WHERE region = 'south' LIMIT 1")[0][0]
    north = rows_of(sample_db_url, "SELECT id FROM customers WHERE region = 'north' LIMIT 1")[0][0]
    url = f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}"
    # the row is visible through /query for its id and name, so the object view shows it too
    listed = dev.client.post(
        f"{A}/ontologies/{onto['id']}/query",
        json=q(CUSTOMER, ["id", "name"], limit=100),
        headers=dev,
    ).json()["rows"]
    assert south in [r["id"] for r in listed]
    got = dev.client.get(f"{url}/{south}", headers=dev)
    assert got.status_code == 200, got.text
    assert set(got.json()["object"]) == {"id", "name"}  # email only on north rows
    assert set(dev.client.get(f"{url}/{north}", headers=dev).json()["object"]) == {
        "id",
        "name",
        "email",
    }
    # a row that no grant admits through its identifier does not exist
    w.policy(onto["id"], REQUEST, ("role", role), ["id", "summary"], 'row.region == "north"')
    gone = rows_of(
        sample_db_url, "SELECT id FROM service_requests WHERE region <> 'north' LIMIT 1"
    )[0][0]
    assert (
        dev.client.get(
            f"{A}/ontologies/{onto['id']}/objects/{REQUEST}/{gone}", headers=dev
        ).status_code
        == 404
    )


# ================================================================== 7. lineage counts
def test_lineage_counts_only_the_links_the_principal_can_see(w: World7, sample_db_url: str) -> None:
    onto = w.ready()
    w.policy(onto["id"], CUSTOMER, ("role", w.admin_role))
    w.policy(onto["id"], REQUEST, ("role", w.admin_role), None, 'row.status == "open"')
    out = w.query(
        onto["id"],
        q(CUSTOMER, ["id"], limit=100, traverse=[{"relationship": R_REQUESTS, "select": ["id"]}]),
    )
    visible = rows_of(sample_db_url, "SELECT count(*) FROM service_requests WHERE status = 'open'")[
        0
    ][0]
    everything = rows_of(sample_db_url, "SELECT count(*) FROM service_requests")[0][0]
    assert sum(len(r["_links"][R_REQUESTS]) for r in out["rows"]) == visible < everything
    rec = w.lineage(onto["id"])[0]
    assert rec["row_counts"][R_REQUESTS] == visible
    assert rec["row_counts"][REQUEST] == visible  # nor does the target count reveal hidden rows


# ================================================================== 8. idempotent replay
def test_idempotent_action_replay_answers_before_the_object_is_resolved_again(
    w: World7,
) -> None:
    onto = w.ready("replay", action_spec(echo_tool(w)))
    policy = w.policy(onto["id"], REQUEST, ("role", w.admin_role))
    for ot in (CUSTOMER, ACCOUNT):
        w.policy(onto["id"], ot, ("role", w.admin_role))
    first = invoke(w, onto, {"object_pk": "2"}, key="replay-1")
    assert first.status_code == 202
    before = len(w.lineage(onto["id"]))
    assert (
        w.client.delete(
            f"{A}/ontologies/{onto['id']}/policies/{policy['id']}", headers=w.h
        ).status_code
        == 204
    )
    again = invoke(w, onto, {"object_pk": "2"}, key="replay-1")
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert len(w.lineage(onto["id"])) == before  # the source was not queried again
