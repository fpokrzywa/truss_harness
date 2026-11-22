"""ARCH M11: the ontology data export route (`POST .../ontologies/{id}/export`, B-2647).

Through the route as `shifde_app`: visibility (404) first, then tenant-level `data:export` and the
client's `data_export` policies (403, audited as `ontology.export_denied`), then the API query;
a success writes one `ontology.exported` event. The `two` fixture's connections reach no real
database, so the source read (`engine.Executor.run`) is replaced: the route, the authorization,
the lineage and the audit code all still run.
"""

from __future__ import annotations

import json
from typing import Any  # Any: rows and JSON
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.ontology import engine
from shifde.ontology import repository as repo
from tests._support.tenants import bind_user
from tests.build.test_routes_cross_client import same
from tests.connections.routes import as_user
from tests.identity.harness import person
from tests.ontology.test_routes_cross_client import TYPE, Side, Two, two
from tests.tenancy.conftest import owner_member

__all__ = ["two"]

BODY = {"object_type": TYPE, "select": ["id", "name"]}
SOURCE_ROWS = 5
SECRET = "row-value-never-audited"


@pytest.fixture(autouse=True)
def source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five source rows, paged like the real executor (`limit` rows, `has_more` past them)."""

    def run(self: engine.Executor, root: engine.NodePlan, limit: int, offset: int) -> Any:
        rows = [{"id": n, "name": f"{SECRET}-{n}"} for n in range(SOURCE_ROWS)][offset:]
        return engine.Result(
            rows=rows[:limit],
            has_more=len(rows) > limit,
            row_counts={TYPE: min(limit, len(rows))},
            truncated=False,
        )

    monkeypatch.setattr(engine.Executor, "run", run)


def _export(side: Side, ontology: str) -> str:
    return f"{side.base}/ontologies/{ontology}/export"


def _events(owner_db: Database, tenant: UUID, action: str) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT action, outcome, target_id, details FROM shifde.audit_events "
                    "WHERE tenant_id = :t AND action = :a ORDER BY occurred_at"
                ),
                {"t": tenant, "a": action},
            )
            .mappings()
            .all()
        )


def _own(details: dict[str, Any]) -> dict[str, Any]:  # Any: audit details
    """The details the export wrote (`audit.record` adds the actor and target labels)."""
    return {k: v for k, v in details.items() if k not in ("actor", "target")}


def _developer(owner_db: Database, side: Side) -> str:
    """A member of the side's client with `developer` (ontology:read / query, no data:export)
    and an allow policy on the side's ontology."""
    email = f"dev-{uuid7().hex[-12:]}@route.test"
    with owner_db.session() as o:
        uid = person(o, email)
        owner_member(o, side.tenant, uid)
        bind_user(o, side.tenant, uid, "developer")
        repo.insert_policy(
            o,
            side.tenant,
            {
                "id": uuid7(),
                "tenant_id": side.tenant,
                "ontology_id": UUID(side.ontology),
                "property_keys": None,
                "subject_type": "user",
                "subject_id": uid,
                "row_filter": None,
                "object_type_key": TYPE,
                "created_by": None,
            },
        )
        o.commit()
    return email


def _export_policy(owner_db: Database, tenant: UUID, definition: dict[str, Any]) -> None:
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.governance_policies (id, tenant_id, kind, name, definition, "
                "status) VALUES (:i, :t, 'data_export', 'export', CAST(:d AS jsonb), 'active')"
            ),
            {"i": uuid7(), "t": tenant, "d": json.dumps(definition)},
        )
        o.commit()


def test_an_owner_with_data_export_exports_the_query_results(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    r = client.post(_export(two.a, two.a.ontology), json=BODY, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"rows", "next_cursor", "lineage_id", "truncated"}
    assert len(out["rows"]) == SOURCE_ROWS and out["next_cursor"] is None
    assert out["truncated"] is False
    # the query's own audit and lineage are still written
    queried = _events(owner_db, two.a.tenant, "ontology.queried")
    assert [e["details"]["lineage_id"] for e in queried] == [out["lineage_id"]]


def test_a_successful_export_writes_one_exported_event_without_row_values(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    r = client.post(_export(two.a, two.a.ontology), json={**BODY, "limit": 2}, headers=h)
    assert r.status_code == 200, r.text
    events = _events(owner_db, two.a.tenant, "ontology.exported")
    assert len(events) == 1
    e = events[0]
    assert e["outcome"] == "success" and str(e["target_id"]) == two.a.ontology
    assert _own(e["details"]) == {
        "lineage_id": r.json()["lineage_id"],
        "object_type": TYPE,
        "rows": 2,
        "more": True,
        "truncated": False,
        "row_cap": None,
        "capped": False,
        "classification": "internal",
    }
    assert SECRET not in json.dumps(e["details"]) and "name" not in e["details"]
    assert _events(owner_db, two.b.tenant, "ontology.exported") == []


def test_without_data_export_the_export_is_403_and_denied_is_audited(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    dev = _developer(owner_db, two.a)
    h = as_user(client, dev)
    refused = client.post(_export(two.a, two.a.ontology), json=BODY, headers=h)
    assert refused.status_code == 403, refused.text
    queried = client.post(f"{two.a.base}/ontologies/{two.a.ontology}/query", json=BODY, headers=h)
    assert queried.status_code == 200, queried.text
    denied = _events(owner_db, two.a.tenant, "ontology.export_denied")
    assert len(denied) == 1
    assert denied[0]["outcome"] == "denied" and str(denied[0]["target_id"]) == two.a.ontology
    assert _own(denied[0]["details"]) == {"reason": "missing_permission", "object_type": TYPE}
    assert _events(owner_db, two.a.tenant, "ontology.exported") == []


def test_a_data_export_policy_forbidding_exports_is_403_and_denied_is_audited(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    _export_policy(owner_db, two.a.tenant, {"allow_export": False})
    h = as_user(client, two.a.email)
    r = client.post(_export(two.a, two.a.ontology), json=BODY, headers=h)
    assert r.status_code == 403, r.text
    denied = _events(owner_db, two.a.tenant, "ontology.export_denied")
    assert [e["details"]["reason"] for e in denied] == ["data_export_policy"]
    assert denied[0]["outcome"] == "denied"
    assert _events(owner_db, two.a.tenant, "ontology.exported") == []
    assert _events(owner_db, two.a.tenant, "ontology.queried") == []


def test_a_max_rows_policy_caps_the_rows_returned(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    _export_policy(owner_db, two.a.tenant, {"max_rows": 3})
    _export_policy(owner_db, two.a.tenant, {"max_rows": 2})
    h = as_user(client, two.a.email)
    r = client.post(_export(two.a, two.a.ontology), json={**BODY, "limit": 50}, headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 2 and r.json()["next_cursor"] is None
    (e,) = _events(owner_db, two.a.tenant, "ontology.exported")
    assert e["details"]["row_cap"] == 2 and e["details"]["rows"] == 2
    assert e["details"]["more"] is False and e["details"]["capped"] is True


def _page_through(
    client: TestClient, side: Side, h: dict[str, str], limit: int
) -> list[list[Any]]:  # Any: rows
    """Every page of the side's export, following `next_cursor` until it is null."""
    pages: list[list[Any]] = []
    cursor = None
    while True:
        body = {**BODY, "limit": limit, **({"cursor": cursor} if cursor else {})}
        r = client.post(_export(side, side.ontology), json=body, headers=h)
        assert r.status_code == 200, r.text
        pages.append(r.json()["rows"])
        cursor = r.json()["next_cursor"]
        if cursor is None or len(pages) > SOURCE_ROWS:
            return pages


def test_a_max_rows_policy_caps_the_total_rows_across_pages(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    _export_policy(owner_db, two.a.tenant, {"max_rows": 3})
    h = as_user(client, two.a.email)
    pages = _page_through(client, two.a, h, 2)
    assert [len(p) for p in pages] == [2, 1]
    assert [row["id"] for p in pages for row in p] == [0, 1, 2]
    last = _events(owner_db, two.a.tenant, "ontology.exported")[-1]["details"]
    assert last["capped"] is True and last["more"] is False and last["row_cap"] == 3


def test_a_cursor_at_the_cap_is_403_and_denied_is_audited(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    first = client.post(_export(two.a, two.a.ontology), json={**BODY, "limit": 2}, headers=h)
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    _export_policy(owner_db, two.a.tenant, {"max_rows": 2})
    queried = len(_events(owner_db, two.a.tenant, "ontology.queried"))
    r = client.post(
        _export(two.a, two.a.ontology), json={**BODY, "limit": 2, "cursor": cursor}, headers=h
    )
    assert r.status_code == 403, r.text
    (denied,) = _events(owner_db, two.a.tenant, "ontology.export_denied")
    assert denied["outcome"] == "denied" and str(denied["target_id"]) == two.a.ontology
    assert _own(denied["details"]) == {"reason": "row_cap_reached", "object_type": TYPE}
    assert len(_events(owner_db, two.a.tenant, "ontology.queried")) == queried
    assert len(_events(owner_db, two.a.tenant, "ontology.exported")) == 1


def test_bs_row_cap_and_cursor_do_not_reach_as_export(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    hb = as_user(client, two.b.email)
    theirs = client.post(_export(two.b, two.b.ontology), json={**BODY, "limit": 2}, headers=hb)
    assert theirs.status_code == 200, theirs.text
    b_cursor = theirs.json()["next_cursor"]
    assert b_cursor is not None
    _export_policy(owner_db, two.b.tenant, {"max_rows": 1})
    b_before = _a_state(owner_db, two.b.tenant)
    h = as_user(client, two.a.email)
    pages = _page_through(client, two.a, h, 2)
    assert [len(p) for p in pages] == [2, 2, 1]
    exported = _events(owner_db, two.a.tenant, "ontology.exported")
    assert [e["details"]["row_cap"] for e in exported] == [None, None, None]
    # B's cursor names B's version, so it does not belong to A's query
    r = client.post(
        _export(two.a, two.a.ontology), json={**BODY, "limit": 2, "cursor": b_cursor}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "rows" not in r.json()
    assert _a_state(owner_db, two.b.tenant) == b_before


def _a_state(owner_db: Database, tenant: UUID) -> tuple[Any, Any]:  # Any: counts
    with owner_db.session() as o:
        audit = o.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
        lineage = o.execute(
            text("SELECT count(*) FROM shifde.ontology_query_lineage WHERE tenant_id = :t"),
            {"t": tenant},
        ).scalar_one()
        return audit, lineage


def test_bs_owner_cannot_export_as_ontology_and_gets_the_unknown_answer(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    before = _a_state(owner_db, two.a.tenant)
    h = as_user(client, two.b.email)
    theirs = client.post(_export(two.b, two.a.ontology), json=BODY, headers=h)
    nobody = client.post(_export(two.b, str(uuid4())), json=BODY, headers=h)
    same(theirs, nobody)
    assert _a_state(owner_db, two.a.tenant) == before
