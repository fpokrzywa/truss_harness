"""P6 execution (continued): data hygiene, redaction, publish approval, dev sample spec."""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.cli.sample_pipeline import reference_pipeline_spec
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import thread_worker
from tests._support.p6util import World6, mini, report_of


def test_db_query_arguments_are_bound_never_interpolated_and_datasets_are_identifiers(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    nodes = [
        {
            "id": "q",
            "type": "db_query",
            "config": {
                "connection_id": w.pg,
                "sql": "SELECT id FROM service_requests WHERE region = %(region)s",
                "args": {"region": "$params.region"},
            },
            "inputs": {},
        }
    ]
    params = {"type": "object", "properties": {"region": {"type": "string"}}}
    pipe = w.pipeline("bound", mini(nodes, [], params=params))
    hostile = "north'; DROP TABLE service_requests; --"
    run = w.run(pipe["id"], {"region": hostile})
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    assert w.nodes(run["id"])["q"]["output_preview"] == {"rows": [], "row_count": 0}
    with psycopg.connect(sample_db_url) as conn:
        assert conn.execute("SELECT count(*) FROM service_requests").fetchone() == (200,)
    # a dataset that is not a plain (schema.)table identifier never validates
    bad = mini(
        [
            {
                "id": "d",
                "type": "data_source",
                "config": {"connection_id": w.pg, "dataset": 'customers"; DROP TABLE x; --'},
                "inputs": {},
            }
        ],
        [],
    )
    assert any(e["code"] == "invalid_config" for e in report_of(w, bad, "ds-bad")["errors"])
    # two statements / writes are refused at validation
    for sql in ("SELECT 1; DELETE FROM customers", "UPDATE customers SET name = 'x'"):
        spec = mini([{**nodes[0], "config": {**nodes[0]["config"], "sql": sql, "args": {}}}], [])
        assert any(
            e["code"] == "invalid_config"
            for e in report_of(w, spec, f"sql-{abs(hash(sql))}")["errors"]
        )


def test_node_outputs_are_redacted_and_follow_the_capture_mode(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    nodes = [
        {
            "id": "t",
            "type": "transform",
            "config": {"operations": [{"op": "select", "fields": ["a", "password", "blob"]}]},
            "inputs": {"data": "$params.doc"},
        },
        {
            "id": "store",
            "type": "sink",
            "config": {"target": "run_artifact"},
            "inputs": {"document": "t.data"},
        },
    ]
    params = {"type": "object", "properties": {"doc": {"type": "object"}}}
    pipe = w.pipeline("redact", mini(nodes, [{"from": "t", "to": "store"}], params=params))
    big = "x" * 9000
    run = w.run(pipe["id"], {"doc": {"a": 1, "password": "hunter2-secret", "blob": big}})
    with thread_worker(settings):
        assert w.wait(run["id"])["status"] == "completed"
    n = w.nodes(run["id"])
    preview = n["t"]["output_preview"]
    assert "hunter2-secret" not in str(preview)
    assert preview.get("truncated") is True and len(str(preview)) <= 4300  # at most ~4 KiB
    artifact_id = n["store"]["output_preview"]["result"]["artifact_id"]
    got = client.get(f"{A}/runs/{run['id']}/artifacts/{artifact_id}", headers=w.h).json()
    assert "hunter2-secret" not in str(got) and got["content"][0]["a"] == 1
    # with the tenant's capture mode below `full`, payloads are never served
    set_tenant_setting(owner_session, "demo-a", "capture_content", "metadata")
    n2 = w.nodes(run["id"])
    assert n2["t"]["output_preview"] == {"redacted": True}
    got2 = client.get(f"{A}/runs/{run['id']}/artifacts/{artifact_id}", headers=w.h).json()
    assert got2["redacted"] is True and got2["content"] is None
    assert client.get(f"{A}/runs/{run['id']}/result", headers=w.h).json()["redacted"] is True


def test_publish_approval_applies_to_pipelines_and_stores_refs_on_approval(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    pipe = w.pipeline("needs-approval", w.reference_spec(), publish_it=False)
    r = client.post(f"{A}/pipelines/{pipe['id']}/publish", headers=w.h, json={"change_note": "x"})
    assert r.status_code == 202, r.text
    request = r.json()["publish_request"]
    own = client.post(
        f"{A}/publish-requests/{request['id']}/decide", headers=w.h, json={"decision": "approve"}
    )
    assert own.status_code == 403  # separation of duties
    ok = w.approver.client.post(
        f"{A}/publish-requests/{request['id']}/decide",
        headers=w.approver,
        json={"decision": "approve"},
    )
    assert ok.status_code == 200, ok.text
    art = client.get(f"{A}/pipelines/{pipe['id']}", headers=w.h).json()
    version = client.get(
        f"{A}/pipelines/{pipe['id']}/versions/{art['latest_published_version']['id']}", headers=w.h
    ).json()
    assert version["state"] == "published" and len(version["refs"]) == 3
    assert version["validation_report"]["valid"] is True


def test_the_dev_sample_pipeline_spec_validates_and_publishes(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    spec = reference_pipeline_spec(w.pg, w.rest_id, f"latest:{w.stack.agent['id']}")
    pipe = w.pipeline("customer-risk-review", spec)
    assert pipe["version"]["state"] == "published"
    assert pipe["version"]["validation_report"]["topo_order"][0] == "src"
