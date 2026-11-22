"""Ontology routes, client A against client B (plan recipe step 5, ontology isolation review 1 F3).

Every route of `ontology.api.router` has a case (`ROUTES`, matched against the router's OpenAPI
paths). A's session asks for B's ids under A's own organization and gets exactly the answer ids
that do not exist get (same status and body); B's ontology rows are unchanged afterwards; and a
positive control shows the same request by B's own owner is not "not found". A's session under B's
slug gets the unknown-organization answer. Everything runs as `shifde_app` over a minimal app (the
`app` port mounts the router, B-501). A's own ontology with B's policy id is the same unknown
answer: a caller allowed on one ontology cannot reach a policy of another (F1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.build import ontologies
from shifde.build import repository as build_repo
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.ontology import api
from shifde.ontology import repository as repo
from tests.build.conftest import Side as DbSide
from tests.build.test_routes_cross_client import same
from tests.connections.routes import as_user, org
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

TYPE = "x.customer"
SPEC = {
    "namespaces": [{"key": "x", "label": "X"}],
    "object_types": [
        {
            "key": TYPE,
            "namespace": "x",
            "properties": [
                {"key": k, "type": t, "nullable": False, "classification": "internal"}
                for k, t in (("id", "integer"), ("name", "string"))
            ],
            "primary_key": ["id"],
            "display_property": "name",
        }
    ],
    "relationships": [],
    "actions": [],
}
_VARYING = {"instance", "request_id"}


@dataclass(frozen=True)
class Side:
    base: str
    email: str
    tenant: UUID
    user: UUID  # the owner's own user id (a member of this client)
    ontology: str
    other: str  # a second ontology of the same client (F1)
    policy: str  # a policy of `ontology`
    other_policy: str  # a policy of `other`
    lineage: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


def _ontology(s: Any, tenant: UUID, key: str, conn: DbSide) -> tuple[UUID, UUID]:  # Any: Session
    art, ver = uuid7(), uuid7()
    build_repo.insert_artifact(
        s,
        {
            "id": art,
            "tenant_id": tenant,
            "kind": "ontologies",
            "workspace_id": None,
            "project_id": None,
            "key": key,
            "name": key,
            "description": None,
            "owner_principal_id": uuid7(),
            "visibility": "scope",
            "created_by": None,
        },
    )
    build_repo.insert_version(
        s,
        {
            "id": ver,
            "tenant_id": tenant,
            "artifact_id": art,
            "kind": "ontologies",
            "version_no": 1,
            "spec": SPEC,
            "created_by": None,
            "based_on_version_id": None,
        },
    )
    mapping = build_repo.upsert_mapping(
        s,
        tenant,
        {
            "id": uuid7(),
            "tenant_id": tenant,
            "ontology_version_id": ver,
            "target_kind": "object_type",
            "target_key": TYPE,
            "connection_id": conn.connection,
            "relation_schema": "public",
            "relation_name": conn.table,
            "column_map": {"id": "id", "name": "name"},
            "key_map": None,
            "filters": [],
            "status": "ok",
            "created_by": None,
        },
    )
    digest = ontologies.mappings_digest([mapping])
    build_repo.insert_ontology_info(s, tenant, ver, digest, {"ok": True})
    s.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = :h, "
            "published_at = now() WHERE id = :i"
        ),
        {"i": ver, "h": f"h-{ver}"},
    )
    s.execute(
        text("UPDATE shifde.artifacts SET latest_published_version_id = :v WHERE id = :a"),
        {"v": ver, "a": art},
    )
    return art, ver


def _side(
    owner_db: Database, tenant: UUID, slug: str, email: str, conn: DbSide, label: str
) -> Side:
    with owner_db.session() as s:
        user = s.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
        art, ver = _ontology(s, tenant, f"onto-{label}", conn)
        other, _ = _ontology(s, tenant, f"other-{label}", conn)
        values = {
            "id": uuid7(),
            "tenant_id": tenant,
            "property_keys": None,
            "subject_type": "user",
            "subject_id": user,
            "row_filter": None,
            "object_type_key": TYPE,
            "created_by": None,
        }
        policy = repo.insert_policy(s, tenant, {**values, "ontology_id": art})["id"]
        other_policy = repo.insert_policy(
            s, tenant, {**values, "id": uuid7(), "ontology_id": other}
        )["id"]
        repo.put_attributes(s, tenant, "user", user, {"region": label}, None, user)
        lineage = uuid7()
        repo.insert_lineage(
            s,
            tenant,
            {
                "id": lineage,
                "principal_id": user,
                "principal_type": "user",
                "ontology_id": art,
                "ontology_version_id": ver,
                "run_id": None,
                "channel": "api",
                "object_types": [TYPE],
                "connection_ids": [conn.connection],
                "compiled_query_hashes": ["h"],
                "row_counts": {TYPE: 1},
                "duration_ms": 3,
                "outcome": "success",
                "error_code": None,
            },
        )
        s.commit()
    return Side(
        org(slug), email, tenant, user, str(art), str(other), str(policy), str(other_policy),
        str(lineage),
    )  # fmt: skip


@pytest.fixture
def two(world: World, people: People, owner_db: Database, sides: tuple[DbSide, DbSide]) -> Two:
    b = _side(owner_db, world.b.tenant, world.b.slug, people.owner_b, sides[1], "b")
    a = _side(owner_db, world.a.tenant, world.a.slug, people.owner_a, sides[0], "a")
    return Two(a, b)


# ---------------------------------------------------------------- the route table
Req = tuple[str, str, dict[str, Any] | None]  # (method, path after the organization, JSON body)


@dataclass(frozen=True)
class Ids:
    ontology: str
    policy: str
    user: str
    lineage: str


def _ids(side: Side) -> Ids:
    return Ids(side.ontology, side.policy, str(side.user), side.lineage)


def _policy_body(user: str, version: int | None = None) -> dict[str, Any]:
    return {
        "object_type_key": TYPE,
        "subject": {"type": "user", "id": user},
        "row_filter": "row.id > 0",
        "row_version": version,
    }


ROUTES: dict[tuple[str, str], Callable[[Ids, Side], Req]] = {
    ("POST", "/orgs/{org}/ontologies/{ontology_id}/query"): lambda i, s: (
        "POST",
        f"/ontologies/{i.ontology}/query",
        {"object_type": TYPE, "select": ["id", "name"]},
    ),
    ("POST", "/orgs/{org}/ontologies/{ontology_id}/export"): lambda i, s: (
        "POST",
        f"/ontologies/{i.ontology}/export",
        {"object_type": TYPE, "select": ["id", "name"]},
    ),
    ("GET", "/orgs/{org}/ontologies/{ontology_id}/objects/{object_type}/{pk}"): lambda i, s: (
        "GET",
        f"/ontologies/{i.ontology}/objects/{TYPE}/1",
        None,
    ),
    ("GET", "/orgs/{org}/ontologies/{ontology_id}/policies"): lambda i, s: (
        "GET",
        f"/ontologies/{i.ontology}/policies",
        None,
    ),
    ("POST", "/orgs/{org}/ontologies/{ontology_id}/policies"): lambda i, s: (
        "POST",
        f"/ontologies/{i.ontology}/policies",
        _policy_body(i.user),
    ),
    ("PATCH", "/orgs/{org}/ontologies/{ontology_id}/policies/{policy_id}"): lambda i, s: (
        "PATCH",
        f"/ontologies/{i.ontology}/policies/{i.policy}",
        _policy_body(i.user, 1),
    ),
    ("DELETE", "/orgs/{org}/ontologies/{ontology_id}/policies/{policy_id}"): lambda i, s: (
        "DELETE",
        f"/ontologies/{i.ontology}/policies/{i.policy}",
        None,
    ),
    ("GET", "/orgs/{org}/ontology-lineage"): lambda i, s: (
        "GET",
        f"/ontology-lineage?ontology_id={i.ontology}",
        None,
    ),
    ("GET", "/orgs/{org}/ontology-principal-attributes"): lambda i, s: (
        "GET",
        "/ontology-principal-attributes",
        None,
    ),
    ("GET", "/orgs/{org}/ontology-principal-attributes/{principal_type}/{principal_id}"): (
        lambda i, s: ("GET", f"/ontology-principal-attributes/user/{i.user}", None)
    ),
    ("PUT", "/orgs/{org}/ontology-principal-attributes/{principal_type}/{principal_id}"): (
        lambda i, s: (
            "PUT",
            f"/ontology-principal-attributes/user/{i.user}",
            {"attributes": {"region": "zz"}, "row_version": 1},
        )
    ),
    ("PUT", "/orgs/{org}/ontologies/{ontology_id}/materializations/{object_type}"): lambda i, s: (
        "PUT",
        f"/ontologies/{i.ontology}/materializations/{TYPE}",
        None,
    ),
}
# routes with no id of B's in the path or query that A could name (lists)
LIST_ONLY = {("GET", "/orgs/{org}/ontology-principal-attributes")}


def test_every_ontology_route_has_a_cross_client_case() -> None:
    app = FastAPI()
    app.include_router(api.router)
    served = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    assert served == set(ROUTES)


def _send(client: TestClient, h: dict[str, str], base: str, req: Req) -> httpx.Response:
    method, path, body = req
    return client.request(method, f"{base}{path}", json=body, headers=h)


def _b_state(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        out: list[Any] = []
        for table in (
            "ontology_access_policies",
            "ontology_principal_attributes",
            "ontology_query_lineage",
            "ontology_materializations",
            "ontology_version_info",
            "ontology_source_mappings",
            "artifacts",
            "artifact_versions",
        ):
            out.append(
                o.execute(
                    text(
                        f"SELECT coalesce(json_agg(t ORDER BY t::text), '[]'::json)::text "
                        f"FROM shifde.{table} t WHERE tenant_id = :t"
                    ),
                    {"t": tenant},
                ).scalar_one()
            )
        out.append(
            o.execute(
                text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar_one()
        )
        return out


def _unknown(side: Side) -> Ids:
    return Ids(str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()))


_CASES = [(f"{m} {p}", (m, p)) for (m, p) in ROUTES if (m, p) not in LIST_ONLY]


@pytest.mark.parametrize(("label", "key"), _CASES, ids=[c[0] for c in _CASES])
def test_a_cannot_reach_bs_objects(
    label: str, key: tuple[str, str], client: TestClient, two: Two, owner_db: Database
) -> None:
    make = ROUTES[key]
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    bs = _ids(two.b)
    got = _send(client, h, two.a.base, make(bs, two.a))
    missing = _send(client, h, two.a.base, make(_unknown(two.a), two.a))
    same(got, missing)
    assert _b_state(owner_db, two.b.tenant) == before
    # positive control: B's own owner reaches its object with the same request
    hb = as_user(client, two.b.email)
    control = _send(client, hb, two.b.base, make(bs, two.b))
    assert control.status_code != 404, (label, control.text)


_POLICY_ROUTES = [c for c in _CASES if "policy_id" in c[1][1]]


@pytest.mark.parametrize(("label", "key"), _POLICY_ROUTES, ids=[c[0] for c in _POLICY_ROUTES])
def test_a_policy_of_another_ontology_of_the_same_client_is_unknown(
    label: str, key: tuple[str, str], client: TestClient, two: Two, owner_db: Database
) -> None:
    """F1: A's owner may manage policies on `ontology`, not on `other`'s: naming `other`'s policy
    under `ontology` is the unknown-policy answer and the policy is unchanged."""
    make = ROUTES[key]
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.a.tenant)
    crossed = _send(
        client,
        h,
        two.a.base,
        make(Ids(two.a.ontology, two.a.other_policy, str(two.a.user), ""), two.a),
    )
    unknown = _send(
        client, h, two.a.base, make(Ids(two.a.ontology, str(uuid4()), str(two.a.user), ""), two.a)
    )
    same(crossed, unknown)
    assert _b_state(owner_db, two.a.tenant) == before
    mine = _send(client, h, two.a.base, make(_ids(two.a), two.a))
    assert mine.status_code in (200, 204), (label, mine.text)


@pytest.mark.parametrize(("label", "key"), _CASES, ids=[c[0] for c in _CASES])
def test_bs_slug_in_the_path_is_unknown_to_a(
    label: str, key: tuple[str, str], client: TestClient, two: Two, owner_db: Database
) -> None:
    make = ROUTES[key]
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    refused = _send(client, h, two.b.base, make(_ids(two.b), two.b))
    missing = _send(client, h, org(f"no-such-{uuid7().hex[-12:]}"), make(_ids(two.b), two.b))
    same(refused, missing)
    assert _b_state(owner_db, two.b.tenant) == before


def test_attribute_list_and_lineage_name_only_the_callers_client(
    client: TestClient, two: Two
) -> None:
    h = as_user(client, two.a.email)
    attrs = client.get(f"{two.a.base}/ontology-principal-attributes", headers=h)
    assert attrs.status_code == 200, attrs.text
    assert {r["principal_id"] for r in attrs.json()} == {str(two.a.user)}
    lineage = client.get(f"{two.a.base}/ontology-lineage?ontology_id={two.a.ontology}", headers=h)
    assert lineage.status_code == 200, lineage.text
    assert [r["id"] for r in lineage.json()["items"]] == [two.a.lineage]
    # a run id filter naming nobody's run gives an empty page, not an error
    none = client.get(
        f"{two.a.base}/ontology-lineage?ontology_id={two.a.ontology}&run_id={uuid4()}", headers=h
    )
    assert none.status_code == 200 and none.json()["items"] == []


def test_lineage_of_an_ontology_the_caller_may_not_read_is_unknown(
    client: TestClient, two: Two, people: People
) -> None:
    """The viewer of A reads no policies or attributes (`ontology:manage_policies`), and a
    lineage list of B's ontology under A is the unknown-ontology answer."""
    h = as_user(client, people.viewer_a)
    denied = client.get(f"{two.a.base}/ontologies/{two.a.ontology}/policies", headers=h)
    assert denied.status_code == 403, denied.text
    attrs = client.get(f"{two.a.base}/ontology-principal-attributes", headers=h)
    assert attrs.status_code == 403, attrs.text
    theirs = client.get(f"{two.a.base}/ontology-lineage?ontology_id={two.b.ontology}", headers=h)
    nobody = client.get(f"{two.a.base}/ontology-lineage?ontology_id={uuid4()}", headers=h)
    same(theirs, nobody)


def test_connection_rows_never_cross(
    owner_db: Database, two: Two, sides: tuple[DbSide, DbSide]
) -> None:
    """The seeded mappings name each client's own connection only (composite key)."""
    with owner_db.session() as o:
        for side, conn in ((two.a, sides[0]), (two.b, sides[1])):
            rows = o.execute(
                text(
                    "SELECT DISTINCT m.connection_id FROM shifde.ontology_source_mappings m "
                    "WHERE m.tenant_id = :t"
                ),
                {"t": side.tenant},
            ).all()
            assert {r[0] for r in rows} == {conn.connection}
            assert conn_repo.get_connection(o, conn.connection, tenant_id=side.tenant) is not None
