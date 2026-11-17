"""P6: malformed or hostile specs and run bodies are answered with 4xx documents, never a 500."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p6util import World6, doc_transform, mini

NODE = {
    "id": "n",
    "type": "transform",
    "config": {"operations": [{"op": "select", "fields": ["a"]}]},
}

HOSTILE_SPECS: list[Any] = [
    {},
    {"schema_version": "1"},
    {"schema_version": "1", "nodes": "nope", "edges": 3},
    {"schema_version": "1", "nodes": [1, None, "x", [], {"id": 5}], "edges": [None, 1, []]},
    {"schema_version": "1", "nodes": [{"id": "a", "type": ["x"]}], "edges": []},
    {"schema_version": "1", "nodes": [{"id": "a", "type": {"x": 1}, "config": 5}], "edges": []},
    mini([NODE, {**NODE, "id": "m"}], [{"from": {"x": 1}, "to": ["n"], "when": {"op": "gt"}}]),
    mini([NODE], [{"from": "n", "to": "n", "when": 5}]),
    mini([{**NODE, "inputs": {"data": 5}}], []),
    mini([{**NODE, "inputs": [1, 2]}], []),
    mini([{**NODE, "inputs": {"data": "n.data"}}], []),
    mini([{**NODE, "retry": {"max_attempts": "many"}}], []),
    mini([{**NODE, "retry": [1]}], []),
    mini([{**NODE, "config": {"operations": [{"op": ["select"]}]}}], []),
    mini([{**NODE, "config": {"operations": [5, None, "x"]}}], []),
    mini([{**NODE, "config": {"operations": [{"op": "aggregate", "group_by": 5, "fn": 1}]}}], []),
    mini([NODE], [], params=5),
    mini([NODE], [], params={"type": ["x", {}]}),
    mini([NODE], [], params={"type": "object", "properties": {"x": 7}}),
    mini([NODE], [], policies=7),
    mini([NODE], [], policies={"max_parallel": "x", "deadline_seconds": [], "on_failure": {}}),
    mini([NODE], [], schemas=[1]),
    mini([NODE], [], schemas={"A": {"$ref": "https://example.test/x"}}),
    mini([NODE], [], schemas={"A": {"$ref": "#/schemas/A"}}),
    mini([NODE], [], ui=[1]),
    mini([NODE], [], ui={"positions": {"n": {"x": "a"}}}),
    mini(
        [
            {
                "id": "d",
                "type": "db_query",
                "config": {
                    "connection_id": "not-a-uuid",
                    "sql": 5,
                    "args": {"a": [1], "b": {"x": 1}},
                },
                "inputs": {},
            }
        ],
        [],
    ),
    mini(
        [
            {
                "id": "c",
                "type": "condition",
                "config": {"expression": "(" * 500 + "1" + ")" * 500},
                "inputs": {},
            }
        ],
        [],
    ),
    mini(
        [{"id": "c", "type": "condition", "config": {"expression": "a" * 5000}, "inputs": {}}], []
    ),
    mini([{"id": f"n{i}", "type": "transform", "config": NODE["config"]} for i in range(150)], []),
    {"schema_version": "1", "nodes": [NODE], "edges": [], "x" * 50: {"y": [1] * 1000}},
]


def test_hostile_specs_validate_to_an_error_report_not_a_500(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    for index, spec in enumerate(HOSTILE_SPECS):
        r = client.post(
            f"{A}/pipelines",
            headers=w.h,
            json={"key": f"h{index}", "name": "H", "scope": w.scope, "spec": spec},
        )
        assert r.status_code in (201, 422), (index, r.text)
        if r.status_code == 201:
            v = client.post(f"{A}/pipelines/{r.json()['id']}/draft/validate", headers=w.h)
            assert v.status_code == 200, (index, v.text)
            publish = client.post(
                f"{A}/pipelines/{r.json()['id']}/publish", headers=w.h, json={"change_note": "x"}
            )
            assert publish.status_code in (200, 422), (index, publish.text)
            if not v.json()["valid"]:
                assert publish.status_code == 422, index


def test_oversized_params_and_bad_run_bodies_are_4xx(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    params = {"type": "object", "properties": {"doc": {"type": "object"}}}
    pipe = w.pipeline("p", mini([doc_transform("n")], [], params=params))
    w.run(pipe["id"], {"doc": {"a": "y" * 250_000}}, status=422)
    base = {"kind": "pipeline", "pipeline_id": pipe["id"], "workspace": "main", "project": "sample"}
    for body in (
        {**base, "params": []},
        {**base, "params": {"a": 1}, "extra": 1},
        {**base, "version_id": "latest"},
        {**base, "version_id": 5},
        {**base, "is_test": "maybe"},
        {**base, "pipeline_id": "nope"},
        {**base, "workspace": ""},
        {"kind": "pipeline"},
    ):
        r = client.post(f"{A}/runs", headers=w.h, json=body)
        assert r.status_code in (404, 422), (body, r.text)
    nope = client.post(
        f"{A}/runs",
        headers=w.h,
        json={**base, "pipeline_id": "00000000-0000-7000-8000-000000000001", "params": {}},
    )
    assert nope.status_code == 404
