"""Solutions and solution versions (issue #78; sub-project 3 design sections 4 and 5, settled calls
7, 11 and 12): the routes as a tenant administrator, a developer and a viewer of demo-a, the
database guard (immutable versions, a published entrypoint of the row's kind), the `ON DELETE
RESTRICT` entrypoint key, `spec_hash` and decimal normalization, `resolve_pins` and
`packaging.canonical`. A `snowflake.native-app/v1` solution is created like a Databricks one
(SP6-4), and any other target kind is 422."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.build.service import PinnedVersion
from shifde.core.db import Database, set_tenant
from shifde.core.errors import ValidationFailed
from shifde.core.ids import uuid7
from shifde.packaging.canonical import canonical, digest
from shifde.packaging.schemas import normalize_decimal
from shifde.packaging.service import resolve_pins
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, VIEWER
from tests._support.p4util import make_user
from tests._support.p5util import Hdr, user_session
from tests.packaging.helpers import (
    Stack,
    new_version,
    solution,
    solutions,
    stack,
    version_body,
)
from tests.tenancy.conftest import app_session

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DEVELOPER = "dev@demo-a.local"


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _lifecycle(h: Hdr, s: Stack) -> None:
    """Criterion 9: create, version, refuse a draft entrypoint, archive, refuse a new version."""
    c = h.client
    made = solution(c, h, "Sales assistant")
    assert made["status"] == "active" and made["workspace"] == "main"
    assert made["project"] == "sample" and made["target_kind"] == "databricks.app/v1"
    sid = made["id"]
    dup = c.post(
        solutions(), headers=h, json={"name": "Sales assistant", "target_kind": made["target_kind"]}
    )
    assert dup.status_code == 409, dup.text

    v1 = new_version(
        c, h, sid, version_body(s.agent_v1, connection=s.connection, dataset=s.dataset)
    )
    assert v1["version_no"] == 1 and HEX64.match(v1["spec_hash"])
    assert v1["entrypoint"] == {
        "kind": "agent",
        "version_id": s.agent_v1,
        "spec_hash": s.agent_hash,
    }
    assert v1["pins"] == [{"role": "prompt", "version_id": s.prompt_v1, "spec_hash": s.prompt_hash}]
    assert v1["spec"]["screens"] == [] and v1["spec"]["evaluation"]["max_total_cost_usd"] == "1.5"
    assert v1["spec_hash"] == digest(v1["spec"])
    v2 = new_version(c, h, sid, version_body(s.agent_v1, dataset=s.dataset))
    assert v2["version_no"] == 2

    listed = c.get(f"{solutions()}/{sid}/versions", headers=h).json()["items"]
    assert [v["version_no"] for v in listed] == [2, 1]
    paged = c.get(f"{solutions()}/{sid}/versions", headers=h, params={"limit": 1}).json()
    assert [v["id"] for v in paged["items"]] == [v2["id"]] and paged["next_cursor"]
    rest = c.get(
        f"{solutions()}/{sid}/versions", headers=h, params={"cursor": paged["next_cursor"]}
    ).json()
    assert [v["id"] for v in rest["items"]] == [v1["id"]]
    got = c.get(f"{solutions()}/{sid}/versions/{v1['id']}", headers=h)
    assert got.status_code == 200 and got.json() == v1
    assert c.get(f"{solutions()}/{sid}", headers=h).json() == made
    ids = [x["id"] for x in c.get(solutions(), headers=h).json()["items"]]
    assert sid in ids

    # a draft, a prompt as an agent, an agent as a pipeline, an unknown id: one 422
    for body in (
        version_body(s.agent_draft, dataset=s.dataset),
        version_body(s.prompt_v1, dataset=s.dataset),
        version_body(s.agent_v1, kind="pipeline", dataset=s.dataset),
        version_body(str(uuid7()), dataset=s.dataset),
    ):
        r = c.post(f"{solutions()}/{sid}/versions", headers=h, json=body)
        assert r.status_code == 422, r.text
        assert r.json()["errors"][0]["loc"] == ["body", "entrypoint_version_id"]
    unknown_conn = c.post(
        f"{solutions()}/{sid}/versions",
        headers=h,
        json=version_body(s.agent_v1, connection=str(uuid7()), dataset=s.dataset),
    )
    assert unknown_conn.status_code == 422, unknown_conn.text
    assert unknown_conn.json()["errors"][0]["loc"] == ["body", "data_reads", 0, "connection_id"]

    archived = c.post(f"{solutions()}/{sid}/archive", headers=h)
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert c.post(f"{solutions()}/{sid}/archive", headers=h).status_code == 409
    refused = c.post(
        f"{solutions()}/{sid}/versions", headers=h, json=version_body(s.agent_v1, dataset=s.dataset)
    )
    assert refused.status_code == 409, refused.text
    active = c.get(solutions(), headers=h, params={"status": "active"}).json()["items"]
    assert sid not in [x["id"] for x in active]


def test_tenant_administrator_lifecycle(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    _lifecycle(h, s)
    actions = owner_session.execute(
        text(
            "SELECT action, count(*) FROM shifde.audit_events WHERE action LIKE 'packaging.%' "
            "GROUP BY action ORDER BY action"
        )
    ).all()
    assert [tuple(a) for a in actions] == [
        ("packaging.solution.archived", 1),
        ("packaging.solution.created", 1),
        ("packaging.solution_version.created", 2),
    ]
    details = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events "
            "WHERE action = 'packaging.solution_version.created' ORDER BY occurred_at LIMIT 1"
        )
    ).scalar_one()
    assert details["version_no"] == 1 and details["entrypoint_version_id"] == s.agent_v1
    assert HEX64.match(details["spec_hash"])


def test_developer_lifecycle(client: TestClient, world: World, owner_session: Session) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    s = stack(admin.client, admin)
    make_user(client, owner_session, admin, DEVELOPER, "developer", world.passwords)
    _lifecycle(user_session(client, world.passwords, DEVELOPER), s)


def test_a_snowflake_solution_is_created_and_an_unknown_kind_is_422(
    client: TestClient, world: World
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    made = solution(h.client, h, "Warehouse app", target_kind="snowflake.native-app/v1")
    assert made["target_kind"] == "snowflake.native-app/v1" and made["status"] == "active"
    got = h.client.get(f"{solutions()}/{made['id']}", headers=h)
    assert got.status_code == 200 and got.json()["target_kind"] == "snowflake.native-app/v1"
    for kind in ("snowflake.app/v1", "databricks.native-app/v1", ""):
        r = h.client.post(solutions(), headers=h, json={"name": "Other", "target_kind": kind})
        assert r.status_code == 422, (kind, r.text)
    listed = h.client.get(solutions(), headers=h).json()["items"]
    assert [s["name"] for s in listed] == ["Warehouse app"]


def test_viewer_is_refused_on_every_route(client: TestClient, world: World) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    s = stack(admin.client, admin)
    sid = solution(admin.client, admin, "Viewer probe")["id"]
    vid = new_version(admin.client, admin, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]
    viewer = user_session(client, world.passwords, VIEWER)
    base = solutions()
    for method, url, body in (
        ("GET", base, None),
        ("POST", base, {"name": "x", "target_kind": "databricks.app/v1"}),
        ("GET", f"{base}/{sid}", None),
        ("POST", f"{base}/{sid}/archive", None),
        ("GET", f"{base}/{sid}/versions", None),
        ("POST", f"{base}/{sid}/versions", version_body(s.agent_v1, dataset=s.dataset)),
        ("GET", f"{base}/{sid}/versions/{vid}", None),
    ):
        r = viewer.client.request(method, url, headers=viewer, json=body)
        assert r.status_code in (403, 404), (method, url, r.status_code, r.text)
    still = admin.client.get(f"{base}/{sid}", headers=admin).json()
    assert still["status"] == "active"


def test_spec_hash_is_stable_and_decimals_are_normalized(client: TestClient, world: World) -> None:
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    sid = solution(h.client, h, "Hashes")["id"]
    a = new_version(
        h.client, h, sid, version_body(s.agent_v1, min_accuracy="0.90", dataset=s.dataset)
    )
    b = new_version(
        h.client, h, sid, version_body(s.agent_v1, min_accuracy="0.9", dataset=s.dataset)
    )
    assert a["spec"] == b["spec"] and a["spec_hash"] == b["spec_hash"]
    assert a["spec"]["evaluation"]["min_accuracy"] == "0.9"
    for bad in ({"min_accuracy": 0.9}, {"min_accuracy": "1.5"}, {"max_p95_latency_ms": 0}):
        body = version_body(s.agent_v1, dataset=s.dataset)
        body["evaluation"].update(bad)
        r = h.client.post(f"{solutions()}/{sid}/versions", headers=h, json=body)
        assert r.status_code == 422, (bad, r.text)


def _version_row(client: TestClient, world: World) -> tuple[Stack, str, str]:
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    sid = solution(h.client, h, "Guarded")["id"]
    return s, sid, new_version(h.client, h, sid, version_body(s.agent_v1, dataset=s.dataset))["id"]


def _tenant(owner: Session) -> UUID:
    tenant: UUID = owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = 'demo-a'")
    ).scalar_one()
    return tenant


def test_versions_are_immutable_for_the_owner_and_the_app(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    _, _, vid = _version_row(client, world)
    tenant = _tenant(owner_session)
    for sql in (
        "UPDATE shifde.solution_versions SET spec_hash = repeat('0', 64) WHERE id = :v",
        "DELETE FROM shifde.solution_versions WHERE id = :v",
    ):
        with pytest.raises(DBAPIError) as owner_err:  # the guard, for the schema owner too
            owner_session.execute(text(sql), {"v": vid})
        owner_session.rollback()
        assert _sqlstate(owner_err.value) == "23514", owner_err.value
        with app_session(app_db, tenant) as app, pytest.raises(DBAPIError) as app_err:
            app.execute(text(sql), {"v": vid})
        # the app role holds no UPDATE or DELETE grant: refused before the guard runs
        assert _sqlstate(app_err.value) == "42501", app_err.value
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.solution_versions WHERE id = :v"), {"v": vid}
        ).scalar_one()
        == 1
    )


def test_insert_guard_refuses_a_draft_or_wrong_kind_entrypoint(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    s, sid, _ = _version_row(client, world)
    tenant = _tenant(owner_session)
    insert = (
        "INSERT INTO shifde.solution_versions (tenant_id, id, solution_id, version_no, "
        "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES (:t, :i, :s, "
        ":n, :k, :e, '{}', repeat('a', 64), :t)"
    )
    for n, (kind, entry) in enumerate(
        (("agent", s.agent_draft), ("agent", s.prompt_v1), ("pipeline", s.agent_v1)), start=10
    ):
        params = {"t": tenant, "i": uuid7(), "s": sid, "n": n, "k": kind, "e": entry}
        with app_session(app_db, tenant) as app, pytest.raises(DBAPIError) as app_err:
            app.execute(text(insert), params)
        assert _sqlstate(app_err.value) == "23514", app_err.value
        set_tenant(owner_session, tenant)
        with pytest.raises(DBAPIError) as owner_err:
            owner_session.execute(text(insert), params)
        owner_session.rollback()
        assert _sqlstate(owner_err.value) == "23514", owner_err.value


def test_entrypoint_key_restricts_deletes_and_tables_are_isolated(owner_session: Session) -> None:
    deltype = owner_session.execute(
        text(
            "SELECT confdeltype FROM pg_catalog.pg_constraint "
            "WHERE conname = 'fk_solution_versions__entrypoint'"
        )
    ).scalar_one()
    assert deltype == "r"
    rows = owner_session.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid "
            "AND p.polname = 'tenant_isolation') FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'shifde' AND c.relname IN ('solutions', 'solution_versions') "
            "ORDER BY c.relname"
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("solution_versions", True, True, True),
        ("solutions", True, True, True),
    ]


# ------------------------------------------------------------------ resolve_pins, canonical
def _graph(edges: dict[UUID, list[tuple[str, UUID]]]) -> Any:  # Any: the direct_pins callable
    def direct(version_id: UUID) -> Sequence[PinnedVersion]:
        return [PinnedVersion(role, v, f"h-{v}") for role, v in edges.get(version_id, [])]

    return direct


def _chain(length: int) -> tuple[UUID, dict[UUID, list[tuple[str, UUID]]]]:
    ids = [uuid7() for _ in range(length + 1)]
    return ids[0], {ids[i]: [("prompt", ids[i + 1])] for i in range(length)}


def test_resolve_pins_is_transitive_sorted_and_deduplicated() -> None:
    root, p, t, s, shared = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    edges = {
        root: [("tool", t), ("prompt", p), ("skill", s)],
        p: [("composed_prompt", shared)],
        s: [("skill_dependency", shared), ("composed_prompt", shared)],
    }
    got = resolve_pins(root, _graph(edges))
    keys = [(x.role, x.version_id) for x in got]
    assert keys == sorted(
        [
            ("tool", t),
            ("prompt", p),
            ("skill", s),
            ("composed_prompt", shared),
            ("skill_dependency", shared),
        ]
    )
    assert all(x.spec_hash == f"h-{x.version_id}" for x in got)
    assert resolve_pins(root, _graph({})) == []


def test_resolve_pins_ends_on_a_cycle() -> None:
    root, a, b = uuid7(), uuid7(), uuid7()
    edges = {root: [("agent", a)], a: [("agent", b)], b: [("agent", a), ("agent", root)]}
    got = resolve_pins(root, _graph(edges))
    assert [(x.role, x.version_id) for x in got] == sorted([("agent", a), ("agent", b)])


def test_resolve_pins_depth_cap() -> None:
    root, edges = _chain(8)
    assert len(resolve_pins(root, _graph(edges))) == 8
    root, edges = _chain(9)
    with pytest.raises(ValidationFailed, match="deeper than 8"):
        resolve_pins(root, _graph(edges))


def test_canonical_json_refuses_floats() -> None:
    assert canonical({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'.encode()
    assert digest({"a": "1", "b": 2}) == digest({"b": 2, "a": "1"})
    for bad in (1.5, {"x": [0.1]}, {"x": {"y": float("nan")}}):
        with pytest.raises((TypeError, ValueError)):
            canonical(bad)


def test_decimal_normalization() -> None:
    assert normalize_decimal("0.90") == normalize_decimal("0.9") == "0.9"
    assert normalize_decimal("1.0") == "1" and normalize_decimal("100") == "100"
    assert normalize_decimal("0.000") == "0" and normalize_decimal("12.50") == "12.5"
