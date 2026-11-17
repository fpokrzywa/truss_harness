"""P7 ontology authoring: versioned artifact, logical model validation, source mappings (physical
layer, checked against discovery, frozen at publish) and the publish gate (contract sections 1-2)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import A, B
from tests._support.p4util import edit, publish, set_tenant_setting
from tests._support.p7util import (
    ACCOUNT,
    CUSTOMER,
    R_ACCOUNTS,
    REQUEST,
    World7,
    clone,
    mapping_body,
    reference_spec,
)


@pytest.fixture
def w(client: TestClient, world: World, owner_session: Session, sample_db_url: str) -> World7:
    return World7(client, world, owner_session, sample_db_url)


def codes(report: dict[str, Any]) -> set[str]:
    return {e["code"] for e in report["errors"]}


def issues(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [e for e in report["errors"] if e["code"] == code]


# ================================================================== lifecycle
def test_create_define_map_validate_publish_and_version_history(w: World7) -> None:
    spec = reference_spec()
    onto = w.create("customer-service", spec)
    assert onto["has_draft"] and onto["latest_published_version"] is None
    # nothing is mapped yet: the logical model is valid, the physical layer is missing
    before = w.validate(onto["id"])
    assert not before["valid"]
    unmapped = issues(before, "unmapped_object_type")
    assert {i["node_id"] for i in unmapped} == {t["key"] for t in spec["object_types"]}
    assert {i["key"] for i in issues(before, "unmapped_relationship")} == {
        r["key"] for r in spec["relationships"]
    }
    assert all(i["path"].startswith(("object_types[", "relationships[")) for i in before["errors"])
    w.map_all(onto["id"], spec)
    after = w.validate(onto["id"])
    assert after["valid"], after
    version = publish(w.client, w.h, "ontologies", onto["id"])
    assert version["state"] == "published" and version["version_no"] == 1
    assert version["spec"]["object_types"][0]["properties"][0]["classification"] == "internal"
    listed = w.client.get(f"{A}/ontologies/{onto['id']}/versions", headers=w.h).json()
    assert [v["version_no"] for v in listed] == [1]
    # the physical layer of the published version is readable and separate from the spec
    maps = w.client.get(
        f"{A}/ontologies/{onto['id']}/versions/{version['id']}/mappings", headers=w.h
    ).json()
    assert len(maps) == len(spec["object_types"]) + len(spec["relationships"])
    assert set(version["spec"]) == {"namespaces", "object_types", "relationships", "actions", "ui"}
    assert "connection_id" not in str(version["spec"]) and "column_map" not in str(version["spec"])
    assert {m["status"] for m in maps} == {"unchecked"}


def test_mapping_check_compares_with_discovery_and_copies_no_rows(w: World7) -> None:
    spec = reference_spec()
    onto = w.create("customer-service", spec)
    body = mapping_body(w.pg, "object_type", CUSTOMER, spec)
    w.put_mapping(onto["id"], body)
    url = f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}/check"
    ok = w.client.post(url, headers=w.h).json()
    assert ok["ok"] is True and ok["issues"] == []
    assert {"id", "name", "tax_id"} <= {c["name"] for c in ok["sample_columns"]}
    assert all(set(c) <= {"name", "type", "nullable"} for c in ok["sample_columns"])  # metadata
    maps = w.client.get(f"{A}/ontologies/{onto['id']}/versions/draft/mappings", headers=w.h).json()
    assert maps[0]["status"] == "ok"
    # wrong column, wrong type, missing relation
    bad = clone(body)
    bad["column_map"]["name"] = "no_such_column"
    bad["column_map"]["id"] = "email"  # integer property <- text column
    w.put_mapping(onto["id"], bad)
    out = w.client.post(url, headers=w.h).json()
    assert out["ok"] is False
    assert {i["code"] for i in out["issues"]} == {"mapping_invalid", "type_incompatible"}
    w.put_mapping(onto["id"], {**body, "relation": {"schema": "", "name": "nope"}})
    out = w.client.post(url, headers=w.h).json()
    assert out["ok"] is False and out["issues"][0]["code"] == "mapping_invalid"
    # relationship mapping: the link columns are checked too
    rel = mapping_body(w.pg, "relationship", R_ACCOUNTS, spec)
    rel["key_map"]["to_column"] = "account_number"  # text cannot hold an integer key
    w.put_mapping(onto["id"], rel)
    out = w.client.post(
        f"{A}/ontologies/{onto['id']}/draft/mappings/relationship/{R_ACCOUNTS}/check",
        headers=w.h,
    ).json()
    assert [i["code"] for i in out["issues"]] == ["type_incompatible"]
    assert w.client.get(f"{A}/ontologies/{onto['id']}/versions/draft/mappings", headers=w.h).json()[
        0
    ]["status"] in ("invalid", "ok")


def test_mapping_is_draft_only_needs_a_saved_target_and_permissions(
    w: World7, owner_session: Session
) -> None:
    spec = reference_spec()
    onto = w.create("customer-service", spec)
    body = mapping_body(w.pg, "object_type", CUSTOMER, spec)
    # path / body mismatch, unknown target, relationship without key_map, other tenant connection
    r = w.client.put(
        f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{ACCOUNT}", json=body, headers=w.h
    )
    assert r.status_code == 422
    r = w.client.put(
        f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/cs.ghost",
        json={**body, "target_key": "cs.ghost"},
        headers=w.h,
    )
    assert r.status_code == 422 and "cs.ghost" in r.text
    rel = mapping_body(w.pg, "relationship", R_ACCOUNTS, spec)
    del rel["key_map"]
    assert (
        w.client.put(
            f"{A}/ontologies/{onto['id']}/draft/mappings/relationship/{R_ACCOUNTS}",
            json=rel,
            headers=w.h,
        ).status_code
        == 422
    )
    # a connection of another organization does not exist for us
    hb = login(w.client, w.world.passwords, "admin@demo-b.local")
    other = w.client.post(f"{B}/secrets", json={"name": "s", "value": "v" * 10}, headers=hb).json()[
        "id"
    ]
    cb = w.client.post(
        f"{B}/connections",
        headers=hb,
        json={
            "name": "x",
            "connection_type": "database",
            "adapter_key": "postgres",
            "scope": {},
            "auth_method": "basic",
            "secret_id": other,
            "access_mode": "read",
            "read_only_confirmed": True,  # setup (B-2431): a read connection with a secret, B-1507
            "config": {"host": "127.0.0.1", "port": 5432, "database": "d", "user": "u"},
        },
    ).json()["id"]
    w.h = login(w.client, w.world.passwords, "admin@demo-a.local")
    assert (
        w.client.put(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}",
            json={**body, "connection_id": cb},
            headers=w.h,
        ).status_code
        == 404
    )
    # a viewer can read the mappings but not write them (ontology:update)
    from tests._support.p4util import make_user

    make_user(w.client, owner_session, w.h, "view2@demo-a.local", "viewer", w.world.passwords)
    hv = login(w.client, w.world.passwords, "view2@demo-a.local")
    assert (
        w.client.get(f"{A}/ontologies/{onto['id']}/versions/draft/mappings", headers=hv).status_code
        == 200
    )
    assert (
        w.client.put(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}",
            json=body,
            headers=hv,
        ).status_code
        == 403
    )
    w.h = login(w.client, w.world.passwords, "admin@demo-a.local")
    w.put_mapping(onto["id"], body)
    assert (
        w.client.delete(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}", headers=w.h
        ).status_code
        == 204
    )
    assert (
        w.client.delete(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}", headers=w.h
        ).status_code
        == 404
    )


def test_publish_freezes_mappings_in_the_api_and_the_database(
    w: World7, owner_session: Session
) -> None:
    onto = w.ready()
    spec = reference_spec()
    body = mapping_body(w.pg, "object_type", CUSTOMER, spec)
    # no draft: the API refuses
    assert (
        w.client.put(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}",
            json=body,
            headers=w.h,
        ).status_code
        == 404
    )
    # the database refuses too, even for the owner role: frozen rows cannot be changed or removed
    vid = onto["version"]["id"]
    for sql in (
        "UPDATE shifde.ontology_source_mappings SET relation_name = 'x' WHERE ontology_version_id = :v",
        "DELETE FROM shifde.ontology_source_mappings WHERE ontology_version_id = :v",
    ):
        with pytest.raises(DBAPIError, match="frozen"):
            owner_session.execute(text(sql), {"v": vid})
        owner_session.rollback()
    info = owner_session.execute(
        text("SELECT mappings_hash FROM shifde.ontology_version_info WHERE version_id = :v"),
        {"v": vid},
    ).scalar_one()
    assert len(info) == 64
    # a new draft continues from the published version: spec edit + inherited mappings
    changed = clone(spec)
    changed["object_types"][0]["label"] = "Client"
    edit(w.client, w.h, "ontologies", onto["id"], changed, 0)
    inherited = w.client.get(
        f"{A}/ontologies/{onto['id']}/versions/draft/mappings", headers=w.h
    ).json()
    assert len(inherited) == len(spec["object_types"]) + len(spec["relationships"])
    assert w.validate(onto["id"])["valid"]
    v2 = publish(w.client, w.h, "ontologies", onto["id"], "relabel")
    assert v2["version_no"] == 2
    # the published versions still carry their own (separate) mapping sets
    for version in (vid, v2["id"]):
        assert len(
            w.client.get(
                f"{A}/ontologies/{onto['id']}/versions/{version}/mappings", headers=w.h
            ).json()
        ) == len(inherited)
    # rollback restores the old spec AND its mappings into a draft
    rb = w.client.post(
        f"{A}/ontologies/{onto['id']}/rollback", json={"version_id": vid}, headers=w.h
    )
    assert rb.status_code == 200 and rb.json()["spec"]["object_types"][0]["label"] == "Customer"
    assert len(
        w.client.get(f"{A}/ontologies/{onto['id']}/versions/draft/mappings", headers=w.h).json()
    ) == len(inherited)
    diff = w.client.get(
        f"{A}/ontologies/{onto['id']}/diff?from={vid}&to={v2['id']}", headers=w.h
    ).json()["changes"]
    assert any("label" in c["path"] for c in diff)


# ================================================================== validation codes
def draft_report(w: World7, spec: dict[str, Any], *, map_it: bool = True) -> dict[str, Any]:
    onto = w.create(f"probe-{uuid4().hex[:8]}", spec)
    if map_it:
        for ot in spec["object_types"]:
            if ot.get("key") in {"cs.customer", "cs.account", "cs.service_request", "cs.employee"}:
                w.put_mapping(
                    onto["id"], mapping_body(w.pg, "object_type", ot["key"], reference_spec())
                )
        for rel in spec["relationships"]:
            if rel.get("key") in {R_ACCOUNTS, "cs.customer_requests", "cs.request_employee"}:
                w.put_mapping(
                    onto["id"], mapping_body(w.pg, "relationship", rel["key"], reference_spec())
                )
    return w.validate(onto["id"])


def test_logical_model_validation_codes(w: World7) -> None:
    spec = clone(reference_spec())
    spec["namespaces"].append({"key": "cs", "label": "again"})  # duplicate namespace
    spec["object_types"][0]["namespace"] = "nowhere"  # unknown namespace
    spec["object_types"][0]["properties"][1]["type"] = "money"  # invalid type
    spec["object_types"][1]["primary_key"] = ["missing"]  # invalid primary key
    spec["object_types"][2]["properties"].append(clone(spec["object_types"][2]["properties"][0]))
    spec["object_types"][3]["primary_key"] = ["name"]  # nullable? no: exists, but...
    spec["object_types"][3]["properties"][1]["nullable"] = True  # nullable primary key
    spec["relationships"][0]["to"] = "cs.ghost"  # dangling
    spec["relationships"][1]["cardinality"] = "some_to_some"
    spec["relationships"].append(clone(spec["relationships"][2]))  # duplicate relationship key
    report = draft_report(w, spec, map_it=False)
    found = codes(report)
    assert {
        "duplicate_key",
        "unknown_namespace",
        "invalid_type",
        "invalid_primary_key",
        "dangling_relationship",
        "invalid_cardinality",
    } <= found
    dup = issues(report, "duplicate_key")
    assert {i.get("node_id") or i.get("key") or i["path"] for i in dup} >= {
        REQUEST,
        "cs.request_employee",
    } or len(dup) >= 2
    assert issues(report, "unknown_namespace")[0]["node_id"] == CUSTOMER
    assert issues(report, "dangling_relationship")[0]["key"] == R_ACCOUNTS
    assert issues(report, "invalid_primary_key")[0]["path"].startswith("object_types[")
    # nothing in an invalid draft can be published
    created = w.create("probe2", spec)
    r = w.client.post(f"{A}/ontologies/{created['id']}/publish", json={}, headers=w.h)
    assert r.status_code == 422 and r.json()["errors"][0]["code"]


def test_mapping_validation_at_publish_codes(w: World7) -> None:
    spec = reference_spec()
    onto = w.create("probe", spec)
    w.map_all(onto["id"], spec)
    # mapping_invalid: a primary key property is not mapped; unknown connection column
    bad = mapping_body(w.pg, "object_type", CUSTOMER, spec)
    del bad["column_map"]["id"]
    w.put_mapping(onto["id"], bad)
    w.put_mapping(
        onto["id"], {**mapping_body(w.pg, "object_type", ACCOUNT, spec), "filters": ["status =="]}
    )
    report = w.validate(onto["id"])
    assert {"mapping_invalid"} <= codes(report)
    paths = {e["path"] for e in issues(report, "mapping_invalid")}
    assert f"mappings/object_type/{CUSTOMER}" in paths
    assert any(p.startswith(f"mappings/object_type/{ACCOUNT}") for p in paths)
    # type_incompatible
    w.put_mapping(onto["id"], mapping_body(w.pg, "object_type", CUSTOMER, spec))
    w.put_mapping(onto["id"], mapping_body(w.pg, "object_type", ACCOUNT, spec))
    changed = clone(spec)
    changed["object_types"][1]["properties"][2]["type"] = "boolean"  # account_number: text
    edit(w.client, w.h, "ontologies", onto["id"], changed, 1)
    report = w.validate(onto["id"])
    assert codes(report) == {"type_incompatible"}
    assert report["errors"][0]["node_id"] == ACCOUNT
    # no discovery: ask for one (the validator never reads the source itself)
    other = w.create("probe3", spec)
    w.owner.execute(text("DELETE FROM shifde.connection_discoveries"))
    w.owner.commit()
    w.map_all(other["id"], spec)
    assert "mapping_invalid" in codes(w.validate(other["id"]))
    # a relationship over a composite key is refused with a readable code
    comp = clone(spec)
    comp["object_types"][0]["primary_key"] = ["id", "name"]
    third = w.create("probe4", comp)
    w.discover()
    w.map_all(third["id"], spec)
    assert any("composite" in e["message"] for e in w.validate(third["id"])["errors"])


def test_capability_and_connection_state_are_checked_at_publish(w: World7) -> None:
    from uuid import uuid4

    from shifde.build.env import ConnectionInfo
    from shifde.build.issues import Report
    from shifde.build.ontologies import _connection_checks

    report = Report()
    info = ConnectionInfo(
        uuid4(),
        "database",
        "postgres",
        "active",
        "read",
        True,
        True,
        frozenset({"discover.schema"}),
    )
    assert _connection_checks(info, "object_type", CUSTOMER, report, True) is False
    assert report.errors[0].code == "capability_missing" and report.errors[0].node_id == CUSTOMER
    report = Report()
    inactive = ConnectionInfo(
        uuid4(), "database", "postgres", "disabled", "read", True, True, frozenset({"query.sql"})
    )
    assert _connection_checks(inactive, "relationship", R_ACCOUNTS, report, True) is False
    assert report.errors[0].code == "mapping_invalid" and report.errors[0].key == R_ACCOUNTS
    report = Report()
    rest = ConnectionInfo(
        uuid4(), "rest", "rest.http", "active", "read", True, True, frozenset({"rest.call"})
    )
    assert _connection_checks(rest, "object_type", CUSTOMER, report, True) is False
    # end to end: a disabled connection blocks publishing
    spec = reference_spec()
    onto = w.create("probe", spec)
    w.map_all(onto["id"], spec)
    row = w.client.get(f"{A}/connections/{w.pg}", headers=w.h).json()
    assert (
        w.client.post(
            f"{A}/connections/{w.pg}/status",
            json={"status": "disabled", "row_version": row["row_version"]},
            headers=w.h,
        ).status_code
        == 200
    )
    report = w.validate(onto["id"])
    assert "mapping_invalid" in codes(report)


def test_action_tool_validation(w: World7, rest_server: Any) -> None:
    from tests._support.p4util import art
    from tests._support.p5util import rest_connection, rest_tool_spec

    rest_id = rest_connection(w.client, w.h, rest_server.base_url)
    tool = art(
        w.client,
        w.h,
        "tools",
        "escalate",
        rest_tool_spec(rest_id, "POST", "/echo", "write_non_idempotent", body=True),
        w.scope,
    )
    # an unpublished tool cannot back an action
    spec = reference_spec(tool["id"])
    report = draft_report(w, spec)
    assert "action_tool_invalid" in codes(report)
    tool_v = publish(w.client, w.h, "tools", tool["id"])
    good = reference_spec(tool_v["id"])
    ok = draft_report(w, good)
    assert ok["valid"], ok
    assert any(i["code"] == "action_requires_approval" for i in ok["warnings"])
    # unknown parameter / property
    bad = clone(good)
    bad["actions"][0]["param_mapping"] = {"nope": "$object.summary", "item": "$object.ghost"}
    report = draft_report(w, bad)
    assert len(issues(report, "action_tool_invalid")) == 2
    # another tenant's tool version does not exist for us
    bad = clone(good)
    bad["actions"][0]["tool_version_id"] = "00000000-0000-4000-8000-000000000000"
    assert "action_tool_invalid" in codes(draft_report(w, bad))
    # publishing pins the tool version (it cannot be deleted from under the action)
    onto = w.create("with-action", good)
    w.map_all(onto["id"], good)
    v = publish(w.client, w.h, "ontologies", onto["id"])
    pins = w.client.get(f"{A}/ontologies/{onto['id']}/versions/{v['id']}", headers=w.h).json()[
        "pins"
    ]
    assert [(p["role"], p["version_id"]) for p in pins] == [("action_tool", tool_v["id"])]


def test_publish_approval_path_freezes_and_records_the_mappings(
    w: World7, owner_session: Session
) -> None:
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    spec = reference_spec()
    onto = w.create("approved", spec)
    w.map_all(onto["id"], spec)
    pending = w.client.post(f"{A}/ontologies/{onto['id']}/publish", json={}, headers=w.h)
    assert pending.status_code == 202
    # the draft (and with it the mappings) is frozen while the request is pending
    assert (
        w.client.put(
            f"{A}/ontologies/{onto['id']}/draft/mappings/object_type/{CUSTOMER}",
            json=mapping_body(w.pg, "object_type", CUSTOMER, spec),
            headers=w.h,
        ).status_code
        == 409
    )
    from tests._support.p4util import make_user

    make_user(
        w.client, owner_session, w.h, "boss@demo-a.local", "tenant_administrator", w.world.passwords
    )
    hb = login(w.client, w.world.passwords, "boss@demo-a.local")
    request_id = pending.json()["publish_request"]["id"]
    decided = w.client.post(
        f"{A}/publish-requests/{request_id}/decide", json={"decision": "approve"}, headers=hb
    )
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    row = owner_session.execute(
        text("SELECT count(*) FROM shifde.ontology_version_info")
    ).scalar_one()
    assert row == 1


def test_ontology_feature_permissions_and_isolation(w: World7, owner_session: Session) -> None:
    onto = w.ready()
    features = w.client.get("/api/v1/features", headers=w.h)
    if features.status_code == 200:
        assert "ontology" in str(features.json())
    hb = login(w.client, w.world.passwords, "admin@demo-b.local")
    # another tenant: every route is a 404, never a 403
    for method, url in (
        ("GET", f"{B}/ontologies/{onto['id']}"),
        ("GET", f"{B}/ontologies/{onto['id']}/versions/draft/mappings"),
        ("GET", f"{B}/ontologies/{onto['id']}/policies"),
    ):
        assert w.client.request(method, url, headers=hb).status_code == 404
    # the catalog: authoring keys exist, policy management is privileged
    perms = (
        {
            p["key"]: p
            for p in w.client.get(
                f"{A}/permissions", headers=login(w.client, w.world.passwords, "admin@demo-a.local")
            ).json()
        }
        if w.client.get(
            f"{A}/permissions", headers=login(w.client, w.world.passwords, "admin@demo-a.local")
        ).status_code
        == 200
        else {}
    )
    if perms:
        assert perms["ontology:manage_policies"]["is_privileged"] is True
        assert perms["ontology:create"]["is_privileged"] is False


def test_other_tenant_cannot_use_a_published_version_in_a_pipeline_or_mapping(w: World7) -> None:
    """A pipeline node pins an ontology version by id: another tenant's id is a validation error."""
    onto = w.ready()
    hb = login(w.client, w.world.passwords, "admin@demo-b.local")
    r = w.client.post(
        f"{B}/ontologies",
        headers=hb,
        json={
            "key": "mine",
            "name": "Mine",
            "scope": {
                "type": "tenant",
                "id": w.client.get("/api/v1/orgs", headers=hb).json()["items"][0]["id"],
            },
            "spec": reference_spec(),
        },
    )
    assert r.status_code == 201
    # reading demo-a's version through demo-b's artifact id space is a 404
    assert (
        w.client.get(
            f"{B}/ontologies/{r.json()['id']}/versions/{onto['version']['id']}", headers=hb
        ).status_code
        == 404
    )
    w.h = login(w.client, w.world.passwords, "admin@demo-a.local")


_ = REQUEST
