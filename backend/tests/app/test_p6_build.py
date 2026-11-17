"""P6 pipelines as versioned artifacts: node-type registry, validation (every error code),
lifecycle, permissions, tenant isolation, input hygiene, schedules."""

from __future__ import annotations

import concurrent.futures
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.build import pipeline_nodes as registry
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, ADMIN_B, VIEWER, A, B, connection
from tests._support.p4util import art, edit, publish
from tests._support.p5util import limited_user, user_session
from tests._support.p6util import World6, error_codes, report_of, validate
from tests.conftest import ORIGIN


@pytest.fixture
def w(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> World6:
    return World6(client, world, owner_session, rest_server, sample_db_url)


def spec_with(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = deepcopy(base)
    out.update(changes)
    return out


def node(base: dict[str, Any], nid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(n for n in base["nodes"] if n["id"] == nid)
    return found


# ================================================================== registry
def test_node_type_registry_lists_only_real_nodes(client: TestClient, w: World6) -> None:
    r = client.get("/api/v1/pipelines/node-types", headers=w.h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["type"] for i in items] == [
        "data_source",
        "api_request",
        "db_query",
        "ontology_query",
        "validate",
        "transform",
        "condition",
        "model",
        "prompt",
        "agent",
        "tool",
        "approval",
        "sink",
    ]
    by = {i["type"]: i for i in items}
    for item in items:
        assert item["category"] in ("source", "logic", "ai", "action", "control")
        assert item["maturity"] in ("validated", "experimental", "planned")
        assert isinstance(item["config_schema"], dict) and item["config_schema"]["type"] == "object"
        assert all({"name", "schema"} <= set(p) for p in item["inputs"] + item["outputs"])
    assert (
        by["sink"]["side_effect"] and by["api_request"]["side_effect"] and by["tool"]["side_effect"]
    )
    assert not by["transform"]["side_effect"]
    assert by["condition"]["category"] == "control" and by["agent"]["category"] == "ai"
    assert by["ontology_query"]["enabled"] and by["ontology_query"]["category"] == "source"
    # an enabled type must have an executor; nothing is listed enabled without one
    from shifde.runtime.pipeline import EXECUTOR_TYPES

    assert {i["type"] for i in items if i["enabled"]} <= EXECUTOR_TYPES
    assert all(i["enabled"] for i in items if i["type"] in EXECUTOR_TYPES)
    assert all(i["reason_disabled"] for i in items if not i["enabled"])
    assert client.get("/api/auth/session").status_code == 200
    anon = TestClient(client.app, base_url="https://testserver", headers={"Origin": ORIGIN})
    assert anon.get("/api/v1/pipelines/node-types").status_code == 401


def test_unimplemented_node_types_are_declared_planned_and_cannot_be_used(
    client: TestClient, w: World6, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = registry.NodeType(
        "ontology_query",
        "Ontology query",
        "source",
        {"type": "object", "properties": {}, "additionalProperties": False},
        (),
        (registry.Port("rows", registry.ROWS),),
        implemented=False,
        reason_disabled="arrives with the ontology module",
    )
    monkeypatch.setitem(registry._REGISTRY, "ontology_query", ghost)
    items = {
        i["type"]: i
        for i in client.get("/api/v1/pipelines/node-types", headers=w.h).json()["items"]
    }
    assert items["ontology_query"]["enabled"] is False
    assert items["ontology_query"]["maturity"] == "planned"
    assert items["ontology_query"]["reason_disabled"] == "arrives with the ontology module"
    spec = {
        "schema_version": "1",
        "nodes": [{"id": "q", "type": "ontology_query", "config": {}, "inputs": {}}],
        "edges": [],
    }
    report = report_of(w, spec)
    assert error_codes(report) == {"node_type_disabled"}
    assert report["errors"][0]["node_id"] == "q"


# ================================================================== validation
def test_valid_reference_pipeline_reports_topological_order(w: World6) -> None:
    created = art(w.client, w.h, "pipelines", "ok", w.reference_spec(), w.scope)
    report = validate(w.client, w.h, created["id"])
    assert report["valid"] is True and report["errors"] == []
    order = report["topo_order"]
    assert sorted(order) == sorted(n["id"] for n in w.reference_spec()["nodes"])
    for a, b in [
        ("src", "check"),
        ("check", "prep"),
        ("prep", "summ"),
        ("risk", "gate"),
        ("gate", "deliver"),
    ]:
        assert order.index(a) < order.index(b)
    # other artifact kinds keep their validate shape (no topo_order)
    prompt = art(w.client, w.h, "prompts", "pp", None, w.scope)
    r = w.client.post(f"{A}/prompts/{prompt['id']}/draft/validate", headers=w.h)
    assert "topo_order" not in r.json()


def test_every_contract_error_code_is_reported_with_its_node_or_edge(w: World6) -> None:
    base = w.reference_spec()

    def codes(spec: dict[str, Any]) -> dict[str, Any]:
        return report_of(w, spec, f"v{abs(hash(repr(spec))) % 10**9}")

    unknown = deepcopy(base)
    node(unknown, "check")["type"] = "frobnicate"
    r = codes(unknown)
    assert "unknown_node_type" in error_codes(r)
    assert next(e for e in r["errors"] if e["code"] == "unknown_node_type")["node_id"] == "check"

    dup = deepcopy(base)
    dup["nodes"].append(deepcopy(node(base, "check")))
    assert "duplicate_node_id" in error_codes(codes(dup))

    dangling = spec_with(base, edges=[*base["edges"], {"from": "src", "to": "nope"}])
    r = codes(dangling)
    err = next(e for e in r["errors"] if e["code"] == "dangling_edge")
    assert err["edge"] == {"from": "src", "to": "nope"}

    cyc = spec_with(base, edges=[*base["edges"], {"from": "store", "to": "src"}])
    assert "cycle_detected" in error_codes(codes(cyc))
    selfloop = spec_with(base, edges=[*base["edges"], {"from": "src", "to": "src"}])
    assert "cycle_detected" in error_codes(codes(selfloop))

    not_up = deepcopy(base)
    node(not_up, "check")["inputs"] = {"data": "store.result"}
    assert "input_not_upstream" in error_codes(codes(not_up))

    incompatible = deepcopy(base)
    node(incompatible, "prep")["inputs"] = {"data": "src.row_count"}  # integer -> list / object
    r = codes(incompatible)
    err = next(e for e in r["errors"] if e["code"] == "schema_incompatible")
    assert err["node_id"] == "prep" and "integer" in err["message"]
    param_mismatch = deepcopy(base)
    node(param_mismatch, "prep")["inputs"] = {"data": "$params.region"}  # string -> list / object
    assert "schema_incompatible" in error_codes(codes(param_mismatch))

    bad_expr = deepcopy(base)
    node(bad_expr, "risk")["config"]["expression"] = "len(inputs.rollup) > 1"
    assert "expression_invalid" in error_codes(codes(bad_expr))
    reads_nodes = deepcopy(base)
    node(reads_nodes, "risk")["config"]["expression"] = "nodes.rollup.output == 1"
    assert "expression_invalid" in error_codes(codes(reads_nodes))
    bad_op = deepcopy(base)
    node(bad_op, "prep")["config"]["operations"][0]["expression"] = "open(row)"
    assert "expression_invalid" in error_codes(codes(bad_op))
    jsonlogic = deepcopy(base)
    jsonlogic["edges"][6]["when"] = {"op": "gt", "left": "x", "right": 1}
    r = codes(jsonlogic)
    err = next(e for e in r["errors"] if e["code"] == "expression_invalid")
    assert err["edge"] == {"from": "risk", "to": "gate"}

    no_gate = deepcopy(base)
    no_gate["edges"] = [e for e in base["edges"] if e["to"] != "gate" and e["from"] != "gate"]
    no_gate["edges"].append({"from": "summ", "to": "deliver"})
    no_gate["nodes"] = [n for n in no_gate["nodes"] if n["id"] != "gate"]
    r = codes(no_gate)
    err = next(e for e in r["errors"] if e["code"] == "approval_required_before_side_effect")
    assert err["node_id"] == "deliver"

    version = deepcopy(base)
    version["schema_version"] = "2"
    assert "unsupported_schema_version" in error_codes(codes(version))
    missing = deepcopy(base)
    node(missing, "check")["inputs"] = {}
    assert "missing_input" in error_codes(codes(missing))
    bad_cfg = deepcopy(base)
    node(bad_cfg, "src")["config"]["max_rows"] = 0
    assert "invalid_config" in error_codes(codes(bad_cfg))
    writes = deepcopy(base)
    node(writes, "src")["config"]["sql"] = "DELETE FROM service_requests"
    assert "invalid_config" in error_codes(codes(writes))
    undeclared = deepcopy(base)
    node(undeclared, "src")["config"]["args"] = {}
    assert "invalid_config" in error_codes(codes(undeclared))
    bad_binding = deepcopy(base)
    node(bad_binding, "check")["inputs"] = {"data": "src"}
    assert "invalid_binding" in error_codes(codes(bad_binding))
    bad_when = deepcopy(base)
    bad_when["edges"][0]["when"] = "true"  # only condition nodes branch
    assert "invalid_edge_condition" in error_codes(codes(bad_when))


def test_references_must_be_published_visible_active_and_usable(w: World6) -> None:
    base = w.reference_spec()
    # a draft agent has no published version
    draft_agent = art(w.client, w.h, "agents", "draft-agent", _agent_spec(w), w.scope)
    spec = deepcopy(base)
    node(spec, "summ")["config"]["agent_version_id"] = f"latest:{draft_agent['id']}"
    assert "ref_not_published" in error_codes(report_of(w, spec, "r1"))
    # a draft VERSION id is no better than latest:
    draft_version = w.client.get(f"{A}/agents/{draft_agent['id']}/draft", headers=w.h).json()["id"]
    node(spec, "summ")["config"]["agent_version_id"] = draft_version
    assert "ref_not_published" in error_codes(report_of(w, spec, "r2"))
    # another tenant's ids never resolve (no existence oracle: same code as a missing id)
    hb = login(w.client, w.world.passwords, ADMIN_B)
    conn_b = connection(
        w.client,
        hb,
        {
            "name": "rest-b",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "config": {"base_url": w.rest.base_url, "health_path": "/health"},
        },
        B,
    )["id"]
    foreign = deepcopy(base)
    node(foreign, "src")["config"]["connection_id"] = conn_b
    node(foreign, "summ")["config"]["agent_version_id"] = "00000000-0000-7000-8000-00000000dead"
    w.h = login(w.client, w.world.passwords, ADMIN)
    r = report_of(w, foreign, "r3")
    assert error_codes(r) == {"ref_cross_tenant"}
    assert {e["node_id"] for e in r["errors"]} == {"src", "summ"}
    # a connection the author may read but not use
    limited_user(
        w.client,
        w.owner,
        w.h,
        w.world.passwords,
        "author@demo-a.local",
        [
            "workspace:read",
            "project:read",
            "pipeline:read",
            "pipeline:create",
            "pipeline:update",
            "connection:read",
            "agent:read",
            "agent:execute",
        ],
    )
    ah = user_session(w.client, w.world.passwords, "author@demo-a.local")
    mine = ah.client.post(
        f"{A}/pipelines",
        headers=ah,
        json={"key": "author-p", "name": "A", "scope": w.scope, "spec": base},
    )
    assert mine.status_code == 201, mine.text
    rep = ah.client.post(f"{A}/pipelines/{mine.json()['id']}/draft/validate", headers=ah).json()
    assert "permission_missing" in error_codes(rep)
    assert {e["node_id"] for e in rep["errors"] if e["code"] == "permission_missing"} >= {
        "src",
        "deliver",
    }
    # a disabled connection cannot be referenced
    w.h = login(w.client, w.world.passwords, ADMIN)
    w.client.post(
        f"{A}/connections/{w.pg}/status",
        json={
            "status": "disabled",
            "row_version": w.client.get(f"{A}/connections/{w.pg}", headers=w.h).json()[
                "row_version"
            ],
        },
        headers=w.h,
    )
    assert "connection_inactive" in error_codes(report_of(w, base, "r4"))


def _agent_spec(w: World6) -> dict[str, Any]:
    from tests._support.p4util import agent_spec

    spec = agent_spec(f"latest:{w.stack.prompt['id']}", w.stack.model)
    return spec


def test_publish_is_blocked_by_validation_errors_and_stores_pins_refs_and_report(w: World6) -> None:
    bad = spec_with(w.reference_spec(), edges=[])
    created = art(w.client, w.h, "pipelines", "bad", bad, w.scope)
    r = w.client.post(
        f"{A}/pipelines/{created['id']}/publish", headers=w.h, json={"change_note": "x"}
    )
    assert r.status_code == 422
    assert any(e["code"] == "input_not_upstream" and e["node_id"] for e in r.json()["errors"])
    pipe = w.pipeline("good", w.reference_spec())
    version = pipe["version"]
    assert version["state"] == "published" and version["spec_hash"]
    pinned = {(p["role"], p["kind"]) for p in version["pins"]}
    assert pinned == {("agent", "agents")}
    refs = {(r["node_id"], r["ref_type"]) for r in version["refs"]}
    assert refs == {
        ("src", "connection"),
        ("deliver", "connection"),
        ("summ", "agent_version"),
    }
    assert version["validation_report"]["valid"] is True
    assert sorted(version["validation_report"]["topo_order"]) == sorted(
        n["id"] for n in w.reference_spec()["nodes"]
    )
    stored = version["spec"]
    assert (
        node(stored, "summ")["config"]["agent_version_id"] == w.stack.agent_v["id"]
    )  # latest: pinned
    rows = w.owner.execute(
        text("SELECT ref_type, count(*) FROM shifde.pipeline_version_refs GROUP BY ref_type")
    ).all()
    assert dict(rows) == {"connection": 2, "agent_version": 1}
    got = w.client.get(f"{A}/pipelines/{pipe['id']}/versions/{version['id']}", headers=w.h).json()
    assert got["refs"] and got["validation_report"]


def test_pipeline_artifact_lifecycle_draft_diff_rollback_and_states(w: World6) -> None:
    spec = w.reference_spec()
    pipe = w.pipeline("life", spec)
    listed = w.client.get(f"{A}/pipelines", headers=w.h).json()["items"]
    assert [p["key"] for p in listed] == ["life"]
    assert (
        w.client.patch(f"{A}/pipelines/{pipe['id']}", json={"name": "Renamed"}, headers=w.h).json()[
            "name"
        ]
        == "Renamed"
    )
    # a draft needs the right row_version
    changed = deepcopy(spec)
    node(changed, "rollup")["config"]["operations"][0]["fn"] = "min"
    saved = edit(w.client, w.h, "pipelines", pipe["id"], changed, row_version=0)
    assert saved["state"] == "draft" and saved["row_version"] >= 1
    stale = w.client.put(
        f"{A}/pipelines/{pipe['id']}/draft", json={"spec": changed, "row_version": 0}, headers=w.h
    )
    assert stale.status_code == 409
    again = edit(w.client, w.h, "pipelines", pipe["id"], changed, saved["row_version"])
    assert again["row_version"] == saved["row_version"] + 1
    v2 = publish(w.client, w.h, "pipelines", pipe["id"], "min instead of max")
    assert v2["version_no"] == 2
    diff = w.client.get(
        f"{A}/pipelines/{pipe['id']}/diff",
        params={"from": pipe["version"]["id"], "to": v2["id"]},
        headers=w.h,
    ).json()
    assert diff["changes"]
    rolled = w.client.post(
        f"{A}/pipelines/{pipe['id']}/rollback",
        json={"version_id": pipe["version"]["id"]},
        headers=w.h,
    )
    assert rolled.status_code == 200 and rolled.json()["state"] == "draft"
    deprecated = w.client.post(
        f"{A}/pipelines/{pipe['id']}/versions/{v2['id']}/state",
        json={"state": "deprecated"},
        headers=w.h,
    )
    assert deprecated.status_code == 200 and deprecated.json()["state"] == "deprecated"
    # deprecated versions still run; a retired one cannot
    ok = w.run(pipe["id"], version=v2["id"])
    assert ok["pipeline_version_id"] == v2["id"]
    w.client.post(
        f"{A}/pipelines/{pipe['id']}/versions/{v2['id']}/state",
        json={"state": "retired"},
        headers=w.h,
    )
    w.run(pipe["id"], version=v2["id"], status=422)


def test_concurrent_publish_and_draft_saves_are_serialized_by_row_version(
    w: World6, owner_session: Session
) -> None:
    pipe = w.pipeline("race", w.reference_spec(), publish_it=False)
    sessions = [user_session(w.client, w.world.passwords, ADMIN) for _ in range(2)]
    # two simultaneous publishes of one draft: exactly one wins
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda s: s.client.post(
                    f"{A}/pipelines/{pipe['id']}/publish", headers=s, json={"change_note": "x"}
                ),
                sessions,
            )
        )
    assert sorted(r.status_code for r in results)[0] == 200
    assert [r.status_code for r in results].count(200) == 1
    assert all(r.status_code in (200, 404, 409) for r in results)
    published = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.artifact_versions "
            "WHERE state = 'published' AND kind = 'pipelines'"
        )
    ).scalar_one()
    assert published == 1
    # two draft saves from the same row_version: one wins, the other gets 409
    fresh = edit(w.client, w.h, "pipelines", pipe["id"], w.reference_spec(), row_version=0)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda s: s.client.put(
                    f"{A}/pipelines/{pipe['id']}/draft",
                    headers=s,
                    json={"spec": w.reference_spec(), "row_version": fresh["row_version"]},
                ),
                sessions,
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    # a save racing a publish never loses the published content
    versions = w.client.get(f"{A}/pipelines/{pipe['id']}/versions", headers=w.h).json()
    assert [v["state"] for v in versions].count("published") == 1


# ================================================================== permissions / tenancy / hygiene
def test_viewer_reads_but_cannot_author_or_execute(
    client: TestClient, w: World6, world: World
) -> None:
    pipe = w.pipeline("vis", w.reference_spec())
    hv = login(client, world.passwords, VIEWER)
    assert client.get(f"{A}/pipelines", headers=hv).status_code == 200
    assert client.get(f"{A}/pipelines/{pipe['id']}", headers=hv).status_code == 200
    body = {"key": "v", "name": "V", "scope": w.scope, "spec": w.reference_spec()}
    assert client.post(f"{A}/pipelines", headers=hv, json=body).status_code == 403
    put = client.put(
        f"{A}/pipelines/{pipe['id']}/draft", headers=hv, json={"spec": {}, "row_version": 0}
    )
    assert put.status_code == 403
    assert (
        client.post(f"{A}/pipelines/{pipe['id']}/publish", headers=hv, json={}).status_code == 403
    )
    run = {
        "kind": "pipeline",
        "pipeline_id": pipe["id"],
        "workspace": "main",
        "project": "sample",
        "params": {"region": "north", "threshold": 1},
    }
    assert client.post(f"{A}/runs", headers=hv, json=run).status_code == 403
    assert (
        client.post(f"{A}/runs", headers=hv, json={**run, "version_id": "draft"}).status_code == 403
    )
    assert client.get(f"{A}/pipelines/{pipe['id']}/runs", headers=hv).status_code == 200


def test_operator_may_execute_published_but_not_test_drafts(
    client: TestClient, w: World6, world: World, owner_session: Session
) -> None:
    from tests._support.p4util import make_user

    pipe = w.pipeline("op", w.reference_spec())
    make_user(client, owner_session, w.h, "ops@demo-a.local", "operator", world.passwords)
    ho = login(client, world.passwords, "ops@demo-a.local")
    run = {
        "kind": "pipeline",
        "pipeline_id": pipe["id"],
        "workspace": "main",
        "project": "sample",
        "params": {"region": "north", "threshold": 1},
    }
    drafted = client.post(f"{A}/runs", headers=ho, json={**run, "version_id": "draft"})
    assert drafted.status_code == 403
    allowed = client.post(f"{A}/runs", headers=ho, json=run)
    assert allowed.status_code == 202, allowed.text


def test_other_tenants_get_404_and_never_learn_anything(
    client: TestClient, w: World6, world: World
) -> None:
    pipe = w.pipeline("secret-pipe", w.reference_spec())
    hb = login(client, world.passwords, ADMIN_B)
    assert client.get(f"{B}/pipelines/{pipe['id']}", headers=hb).status_code == 404
    assert client.get(f"{A}/pipelines/{pipe['id']}", headers=hb).status_code == 404
    run_body = {
        "kind": "pipeline",
        "pipeline_id": pipe["id"],
        "workspace": "main",
        "project": "sample",
        "params": {"region": "north", "threshold": 1},
    }
    assert client.post(f"{B}/runs", headers=hb, json=run_body).status_code == 404
    assert client.get(f"{B}/pipelines/{pipe['id']}/runs", headers=hb).status_code == 404
    assert client.get(f"{B}/pipelines/{pipe['id']}/draft", headers=hb).status_code == 404
    w.h = login(client, world.passwords, ADMIN)  # the session cookie jar is shared
    run = w.run(pipe["id"])
    hb = login(client, world.passwords, ADMIN_B)
    assert client.get(f"{B}/runs/{run['id']}/nodes", headers=hb).status_code == 404
    assert client.get(f"{B}/runs/{run['id']}/artifacts/{run['id']}", headers=hb).status_code == 404


@pytest.mark.parametrize(
    "raw",
    [
        '{"key":"nul","name":"n","scope":{"type":"tenant","id":"%s"},"spec":{"schema_version":"1","x":"a\\u0000b"}}',
        '{"key":"sur","name":"n","scope":{"type":"tenant","id":"%s"},"spec":{"schema_version":"1","x":"\\ud800"}}',
        '{"key":"nan","name":"n","scope":{"type":"tenant","id":"%s"},"spec":{"schema_version":"1","x":NaN}}',
        '{"key":"inf","name":"n","scope":{"type":"tenant","id":"%s"},"spec":{"schema_version":"1","x":Infinity}}',
    ],
)
def test_unstorable_json_is_a_422_never_a_500(w: World6, raw: str) -> None:
    body = raw % w.scope["id"]
    headers = {**w.h, "Content-Type": "application/json"}
    r = w.client.post(f"{A}/pipelines", content=body, headers=headers)
    assert r.status_code == 422, r.text
    pipe = w.pipeline("hyg", w.reference_spec(), publish_it=False)
    r = w.client.put(
        f"{A}/pipelines/{pipe['id']}/draft",
        content=body.replace('"key":', '"row_version":0,"k":'),
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ['"a\\u0000b"', '"\\ud800"', "NaN", "-Infinity"])
def test_run_params_with_unstorable_json_are_422(w: World6, bad: str) -> None:
    pipe = w.pipeline("hyg-run", w.reference_spec())
    body = (
        f'{{"kind":"pipeline","pipeline_id":"{pipe["id"]}","workspace":"main","project":"sample",'
        f'"params":{{"region":{bad},"threshold":1}}}}'
    )
    r = w.client.post(
        f"{A}/runs", content=body, headers={**w.h, "Content-Type": "application/json"}
    )
    assert r.status_code == 422, r.text


def test_run_params_are_validated_against_the_spec_schema(w: World6) -> None:
    pipe = w.pipeline("params", w.reference_spec())
    r = w.run(pipe["id"], {"region": 5, "threshold": "x", "extra": 1}, status=422)
    locs = {tuple(e["loc"]) for e in r["errors"]}
    assert ("body", "params", "region") in locs and ("body", "params", "threshold") in locs
    assert all(e["type"] == "invalid_params" for e in r["errors"])
    w.run(pipe["id"], {}, status=422)
    # a pipeline run takes params, not an agent-style input
    w.run(pipe["id"], status=422, input="hello")
    # draft without a draft
    w.run(pipe["id"], version="draft", status=422)
    # an id of another kind of run target is refused
    r2 = w.client.post(
        f"{A}/runs",
        headers=w.h,
        json={
            "kind": "pipeline",
            "agent_id": w.stack.agent["id"],
            "workspace": "main",
            "project": "sample",
        },
    )
    assert r2.status_code == 422


# ================================================================== schedules (extension point)
def test_schedules_are_not_enabled_in_the_mvp_and_work_when_the_flag_is_on(
    client: TestClient, w: World6, settings: Settings, world: World
) -> None:
    pipe = w.pipeline("sched", w.reference_spec())
    r = client.get(f"{A}/pipelines/{pipe['id']}/schedules", headers=w.h)
    assert r.status_code == 501 and r.json()["code"] == "scheduling_not_enabled"
    post = client.post(f"{A}/pipelines/{pipe['id']}/schedules", headers=w.h, json={})
    assert post.status_code in (422, 501)
    from shifde.app import create_app

    on = settings.model_copy(update={"feature_pipeline_schedules": True})
    with TestClient(
        create_app(on), base_url="https://testserver", headers={"Origin": ORIGIN}
    ) as c2:
        h2 = login(c2, world.passwords, ADMIN)
        assert c2.get(f"{A}/pipelines/{pipe['id']}/schedules", headers=h2).json() == []
        sa = c2.post(f"{A}/service-accounts", headers=h2, json={"name": "sched-sa"})
        assert sa.status_code == 201, sa.text
        good = c2.post(
            f"{A}/pipelines/{pipe['id']}/schedules",
            headers=h2,
            json={
                "cron": "*/15 * * * *",
                "timezone": "Europe/Berlin",
                "run_as_service_account_id": sa.json()["id"],
                "status": "active",
                "params": {"region": "north", "threshold": 1},
            },
        )
        assert good.status_code == 201, good.text
        assert good.json()["next_run_at"] and good.json()["status"] == "active"
        bad = c2.post(
            f"{A}/pipelines/{pipe['id']}/schedules",
            headers=h2,
            json={"cron": "61 * * * *", "run_as_service_account_id": sa.json()["id"]},
        )
        assert bad.status_code == 422
        assert len(c2.get(f"{A}/pipelines/{pipe['id']}/schedules", headers=h2).json()) == 1
