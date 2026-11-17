"""P7 ontology query, traversal, policies, objects and lineage against the sample database."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.postgres import PostgresAdapter
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import A, B
from tests._support.p4util import edit, publish
from tests._support.p7util import (
    ACCOUNT,
    CUSTOMER,
    EMPLOYEE,
    QUERY_PERMS,
    R_ACCOUNTS,
    R_EMPLOYEE,
    R_REQUESTS,
    REQUEST,
    World7,
    q,
    reference_spec,
    rows_of,
)


@pytest.fixture
def w(client: TestClient, world: World, owner_session: Session, sample_db_url: str) -> World7:
    return World7(client, world, owner_session, sample_db_url)


@pytest.fixture
def onto(w: World7) -> dict[str, Any]:
    """Published reference ontology; the tenant administrator role sees everything including
    the restricted tax id."""
    created = w.ready()
    w.allow_all(created["id"], ("role", w.admin_role))
    w.policy(created["id"], CUSTOMER, ("role", w.admin_role), ["tax_id"])
    return created


def count(sample_db_url: str, table: str) -> int:
    return int(rows_of(sample_db_url, f"SELECT count(*) FROM {table}")[0][0])


# ================================================================== correctness
def test_authorized_query_with_traversal_matches_the_source_data(
    w: World7, onto: dict[str, Any], sample_db_url: str
) -> None:
    body = q(
        CUSTOMER,
        ["id", "name", "region", "tax_id"],
        filter='region == "north" and segment != "consumer"',
        traverse=[
            {"relationship": R_ACCOUNTS, "select": ["id", "account_number", "status"]},
            {
                "relationship": R_REQUESTS,
                "select": ["id", "status"],
                "filter": 'status == "open"',
                "traverse": [{"relationship": R_EMPLOYEE, "select": ["id", "name"]}],
            },
        ],
    )
    out = w.query(onto["id"], body)
    want = rows_of(
        sample_db_url,
        "SELECT id, name, region, tax_id FROM customers "
        "WHERE region = 'north' AND segment <> 'consumer' ORDER BY id",
    )
    assert [r["id"] for r in out["rows"]] == [r[0] for r in want] and want
    assert [r["tax_id"] for r in out["rows"]] == [r[3] for r in want]
    assert set(out["rows"][0]) == {"id", "name", "region", "tax_id", "_links"}
    for row in out["rows"]:
        accounts = rows_of(
            sample_db_url,
            "SELECT id, account_number, status FROM accounts WHERE customer_id = %s",
            row["id"],
        )
        got = row["_links"][R_ACCOUNTS]
        assert sorted((a["id"], a["account_number"], a["status"]) for a in got) == sorted(accounts)
        requests = rows_of(
            sample_db_url,
            "SELECT id, status, assigned_employee_id FROM service_requests "
            "WHERE customer_id = %s AND status = 'open'",
            row["id"],
        )
        linked = row["_links"][R_REQUESTS]
        assert sorted(r["id"] for r in linked) == sorted(r[0] for r in requests)
        for request in linked:
            emp = [r[2] for r in requests if r[0] == request["id"]][0]
            names = request["_links"][R_EMPLOYEE]
            if emp is None:
                assert names == []
            else:
                assert [n["id"] for n in names] == [emp] and names[0][
                    "name"
                ] == f"Employee {emp:02d}"
    assert out["lineage_id"] and out["next_cursor"] is None


def test_reverse_traversal_and_numeric_date_types(
    w: World7, onto: dict[str, Any], sample_db_url: str
) -> None:
    out = w.query(
        onto["id"],
        q(
            EMPLOYEE,
            ["id", "name"],
            filter="id <= 3",
            traverse=[
                {"relationship": R_EMPLOYEE, "direction": "reverse", "select": ["id", "status"]}
            ],
        ),
    )
    for emp in out["rows"]:
        want = rows_of(
            sample_db_url,
            "SELECT id FROM service_requests WHERE assigned_employee_id = %s",
            emp["id"],
        )
        assert sorted(r["id"] for r in emp["_links"][R_EMPLOYEE]) == sorted(r[0] for r in want)
    accounts = w.query(
        onto["id"],
        q(
            ACCOUNT,
            ["id", "balance", "opened_at"],
            filter='balance > 10000.5 and opened_at >= "2025-03-01"',
        ),
    )
    want = rows_of(
        sample_db_url,
        "SELECT id, balance FROM accounts WHERE balance > 10000.5 AND opened_at >= '2025-03-01' ORDER BY id",
    )
    assert [a["id"] for a in accounts["rows"]] == [r[0] for r in want] and want
    assert isinstance(accounts["rows"][0]["balance"], float)
    assert accounts["rows"][0]["opened_at"].startswith("20")


def test_compiled_sql_is_parameterized_and_filter_values_are_data(
    w: World7, onto: dict[str, Any], sample_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []
    real = PostgresAdapter.compile

    def spy(self: PostgresAdapter, plan: Any) -> Any:
        compiled = real(self, plan)
        seen.append((compiled.sql, compiled.params))
        return compiled

    monkeypatch.setattr(PostgresAdapter, "compile", spy)
    before = {t: count(sample_db_url, t) for t in ("customers", "accounts", "service_requests")}
    attacks = [
        "x'; DROP TABLE customers; --",
        "north' OR '1'='1",
        '"; DELETE FROM accounts; --',
        "north\\' OR 1=1 --",
        "100%_pct%s",
        "a" * 400,
        "\u0000nul",
    ]
    for attack in attacks:
        seen.clear()
        escaped = json.dumps(attack)
        raw = f'region == "{attack}"' if "\u0000" in attack else None
        resp = w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], filter=raw or f"region == {escaped} or name == {escaped}"),
            headers=w.h,
        )
        if raw is not None:  # a real NUL character never reaches the compiler
            assert resp.status_code == 422
            continue
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows"] == []  # the value matched nothing: it was data, not SQL
        assert seen and all(attack not in sql for sql, _ in seen)
        assert any(attack in params for _, params in seen)
    assert {t: count(sample_db_url, t) for t in before} == before
    # identifiers never come from the request: every spliced name must be a mapped property
    for bad_filter in (
        "id) OR (1=1",
        "name == 'a' --",
        "region.x == 1",
        'region == "n" ; select',
        "customers.id == 1",
    ):
        r = w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], filter=bad_filter),
            headers=w.h,
        )
        assert r.status_code == 422, (bad_filter, r.text)
    for body in (
        q(CUSTOMER, ['id"; DROP TABLE customers; --']),
        q(CUSTOMER, ["id"], traverse=[{"relationship": "x; DROP", "select": ["id"]}]),
    ):
        assert w.client.post(
            f"{A}/ontologies/{onto['id']}/query", json=body, headers=w.h
        ).status_code in (404, 422)
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q("cs.customer; DROP TABLE x", ["id"]),
            headers=w.h,
        ).status_code
        == 404
    )
    assert {t: count(sample_db_url, t) for t in before} == before


def test_pagination_with_cursor_and_limits(w: World7, onto: dict[str, Any]) -> None:
    first = w.query(onto["id"], q(CUSTOMER, ["id"], limit=20))
    assert [r["id"] for r in first["rows"]] == list(range(1, 21)) and first["next_cursor"]
    second = w.query(onto["id"], q(CUSTOMER, ["id"], limit=20, cursor=first["next_cursor"]))
    assert [r["id"] for r in second["rows"]] == list(range(21, 41))
    last = w.query(onto["id"], q(CUSTOMER, ["id"], limit=20, cursor=second["next_cursor"]))
    assert [r["id"] for r in last["rows"]] == list(range(41, 51)) and last["next_cursor"] is None
    # a cursor is bound to its query; garbage is a 422
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id", "name"], limit=20, cursor=first["next_cursor"]),
            headers=w.h,
        ).status_code
        == 422
    )
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], cursor="@@@"),
            headers=w.h,
        ).status_code
        == 422
    )
    for bad_limit in (0, 1001, -5):
        assert (
            w.client.post(
                f"{A}/ontologies/{onto['id']}/query",
                json=q(CUSTOMER, ["id"], limit=bad_limit),
                headers=w.h,
            ).status_code
            == 422
        )


def nest(depth: int) -> list[dict[str, Any]]:
    """customer -> requests -> employee -> (reverse) requests -> ...: `depth` hops deep."""
    hops = [
        {"relationship": R_REQUESTS, "direction": "forward", "select": ["id"]},
        {"relationship": R_EMPLOYEE, "direction": "forward", "select": ["id"]},
        {"relationship": R_EMPLOYEE, "direction": "reverse", "select": ["id"]},
        {"relationship": R_REQUESTS, "direction": "reverse", "select": ["id"]},
    ]
    node: dict[str, Any] | None = None
    for hop in reversed(hops[:depth]):
        node = {**hop, "traverse": [node] if node else []}
    return [node] if node else []


def test_traversal_depth_hop_and_row_caps(
    w: World7, onto: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert w.query(onto["id"], q(CUSTOMER, ["id"], limit=3, traverse=nest(3)))["rows"]
    r = w.client.post(
        f"{A}/ontologies/{onto['id']}/query",
        json=q(CUSTOMER, ["id"], traverse=nest(4)),
        headers=w.h,
    )
    assert r.status_code == 422 and r.json()["code"] == "traversal_too_deep"
    many = [{"relationship": R_ACCOUNTS, "select": ["id"]}] * 9
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], traverse=many),
            headers=w.h,
        ).status_code
        == 422
    )
    twice = [{"relationship": R_ACCOUNTS, "select": ["id"]}] * 2
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/query",
            json=q(CUSTOMER, ["id"], traverse=twice),
            headers=w.h,
        ).status_code
        == 422
    )
    # related rows are capped per parent and flagged
    from shifde.ontology import engine

    monkeypatch.setattr(engine, "PER_PARENT_LINKS", 1)
    out = w.query(
        onto["id"],
        q(CUSTOMER, ["id"], limit=10, traverse=[{"relationship": R_REQUESTS, "select": ["id"]}]),
    )
    assert out["truncated"] is True
    assert all(len(r["_links"][R_REQUESTS]) <= 1 for r in out["rows"])


# ================================================================== authorization
def test_unauthorized_queries_are_rejected_and_audited(
    w: World7, onto: dict[str, Any], owner_session: Session
) -> None:
    # 1. a reader without ontology:query: 403, audited
    reader, _, _ = w.member(
        "reader@demo-a.local", ["workspace:read", "project:read", "ontology:read"]
    )
    r = reader.client.post(
        f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=reader
    )
    assert r.status_code == 403
    # 2. no ontology:read either: the ontology does not exist for them
    blind, _, _ = w.member("blind@demo-a.local", ["workspace:read"])
    assert (
        blind.client.post(
            f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=blind
        ).status_code
        == 404
    )
    # 3. may query but no policy allows anything: default deny, object type invisible
    dev, dev_id, dev_role = w.member("dev@demo-a.local")
    r = dev.client.post(f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=dev)
    assert r.status_code == 404
    # 4. another organization: 404
    hb = login(w.client, w.world.passwords, "admin@demo-b.local")
    assert (
        w.client.post(
            f"{B}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=hb
        ).status_code
        == 404
    )
    w.h = login(w.client, w.world.passwords, "admin@demo-a.local")
    # 5. a policy for one object type does not open the others (and not through a relationship)
    w.policy(onto["id"], CUSTOMER, ("role", dev_role), ["id", "name"])
    ok = dev.client.post(
        f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id", "name"]), headers=dev
    )
    assert ok.status_code == 200 and len(ok.json()["rows"]) == 50
    for body in (
        q(ACCOUNT, ["id"]),
        q(CUSTOMER, ["id"], traverse=[{"relationship": R_ACCOUNTS, "select": ["id"]}]),
    ):
        assert (
            dev.client.post(
                f"{A}/ontologies/{onto['id']}/query", json=body, headers=dev
            ).status_code
            == 404
        )
    # 6. a property outside the policy is a 403, never silently dropped
    r = dev.client.post(
        f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id", "email"]), headers=dev
    )
    assert (
        r.status_code == 403
        and r.json()["code"] == "property_denied"
        and "email" in r.json()["detail"]
    )
    # ... and a denied property cannot be probed through the filter either
    r = dev.client.post(
        f"{A}/ontologies/{onto['id']}/query",
        json=q(CUSTOMER, ["id"], filter='email == "x"'),
        headers=dev,
    )
    assert r.status_code == 403
    # the denials are in the audit log, with the reason but no data
    denied = owner_session.execute(
        text("SELECT details FROM shifde.audit_events WHERE action = 'ontology.query_denied'")
    ).all()
    reasons = {d[0]["reason"] for d in denied}
    assert {"no_policy", "property:email", "forbidden"} <= reasons
    assert (
        owner_session.execute(
            text(
                "SELECT count(*) FROM shifde.policy_decisions WHERE action = 'ontology:query' "
                "AND decision = 'deny'"
            )
        ).scalar_one()
        >= 1
    )


def test_restricted_properties_need_an_explicit_allow(w: World7, onto: dict[str, Any]) -> None:
    dev, _, dev_role = w.member("dev@demo-a.local")
    w.policy(onto["id"], CUSTOMER, ("role", dev_role))  # "all properties" = all non-restricted
    url = f"{A}/ontologies/{onto['id']}/query"
    assert (
        dev.client.post(url, json=q(CUSTOMER, ["id", "email", "region"]), headers=dev).status_code
        == 200
    )
    for body in (q(CUSTOMER, ["id", "tax_id"]), q(CUSTOMER, ["id"], filter='tax_id == "TAX-1"')):
        r = dev.client.post(url, json=body, headers=dev)
        assert r.status_code == 403 and r.json()["code"] == "property_denied"
    # the tenant administrator's explicit policy grants it
    out = w.query(onto["id"], q(CUSTOMER, ["id", "tax_id"], limit=2))
    assert out["rows"][0]["tax_id"].startswith("TAX-")
    # a policy that names a restricted property grants exactly that one
    w.policy(onto["id"], CUSTOMER, ("role", dev_role), ["tax_id"])
    assert dev.client.post(url, json=q(CUSTOMER, ["id", "tax_id"]), headers=dev).status_code == 200


def test_row_filters_use_principal_attributes_and_fail_closed(
    w: World7, onto: dict[str, Any], sample_db_url: str
) -> None:
    dev, dev_id, dev_role = w.member("dev@demo-a.local")
    w.policy(
        onto["id"], CUSTOMER, ("role", dev_role), None, "row.region == principal.attributes.region"
    )
    url = f"{A}/ontologies/{onto['id']}/query"
    # no attribute set: the policy cannot be evaluated, so it grants nothing
    assert dev.client.post(url, json=q(CUSTOMER, ["id"]), headers=dev).status_code == 404
    put = w.client.put(
        f"{A}/ontology-principal-attributes/user/{dev_id}",
        json={"attributes": {"region": "south", "regions": ["east", "west"]}},
        headers=w.h,
    )
    assert put.status_code == 200 and put.json()["row_version"] == 1
    out = dev.client.post(url, json=q(CUSTOMER, ["id", "region"]), headers=dev).json()
    want = rows_of(sample_db_url, "SELECT id FROM customers WHERE region = 'south' ORDER BY id")
    assert [r["id"] for r in out["rows"]] == [r[0] for r in want]
    assert {r["region"] for r in out["rows"]} == {"south"}
    # the user's own filter narrows further; it cannot widen the policy
    out = dev.client.post(
        url, json=q(CUSTOMER, ["id"], filter='region == "north"'), headers=dev
    ).json()
    assert out["rows"] == []
    # the object view honours the row filter (a row outside it does not exist)
    north_id = rows_of(sample_db_url, "SELECT id FROM customers WHERE region = 'north' LIMIT 1")[0][
        0
    ]
    south_id = want[0][0]
    assert (
        dev.client.get(
            f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/{north_id}", headers=dev
        ).status_code
        == 404
    )
    assert (
        dev.client.get(
            f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/{south_id}", headers=dev
        ).status_code
        == 200
    )
    # traversal applies the target's own policy and row filter
    w.policy(
        onto["id"], REQUEST, ("role", dev_role), None, "row.region in principal.attributes.regions"
    )
    w.policy(
        onto["id"], CUSTOMER, ("role", dev_role), None, "row.region != principal.attributes.region"
    )
    hop = {"relationship": R_REQUESTS, "select": ["id", "region"]}
    out = dev.client.post(
        url, json=q(CUSTOMER, ["id", "region"], traverse=[hop], limit=200), headers=dev
    ).json()
    for row in out["rows"]:
        for linked in row["_links"][R_REQUESTS]:
            assert linked["region"] in ("east", "west")
    # attribute lookups: optimistic locking, validation, authorization
    again = w.client.put(
        f"{A}/ontology-principal-attributes/user/{dev_id}",
        json={"attributes": {"region": "north"}, "row_version": 1},
        headers=w.h,
    )
    assert again.status_code == 200 and again.json()["row_version"] == 2
    stale = w.client.put(
        f"{A}/ontology-principal-attributes/user/{dev_id}",
        json={"attributes": {"region": "x"}, "row_version": 1},
        headers=w.h,
    )
    assert stale.status_code == 409
    assert (
        w.client.put(
            f"{A}/ontology-principal-attributes/user/{dev_id}",
            json={"attributes": {"bad name": {"nested": 1}}},
            headers=w.h,
        ).status_code
        == 422
    )
    assert (
        dev.client.put(
            f"{A}/ontology-principal-attributes/user/{dev_id}",
            json={"attributes": {"region": "south"}},
            headers=dev,
        ).status_code
        == 403
    )
    assert (
        w.client.put(
            f"{A}/ontology-principal-attributes/user/00000000-0000-4000-8000-000000000000",
            json={"attributes": {}},
            headers=w.h,
        ).status_code
        == 404
    )


def test_overlapping_policies_combine_per_property(
    w: World7, onto: dict[str, Any], sample_db_url: str
) -> None:
    """Rows are visible when every touched property is admitted by some grant: a north-only grant
    for names plus an unrestricted grant for ids exposes all ids but names only for north."""
    dev, _, dev_role = w.member("dev@demo-a.local")
    w.policy(onto["id"], CUSTOMER, ("role", dev_role), ["id"])
    w.policy(onto["id"], CUSTOMER, ("role", dev_role), ["name"], 'row.region == "north"')
    url = f"{A}/ontologies/{onto['id']}/query"
    ids = dev.client.post(url, json=q(CUSTOMER, ["id"], limit=100), headers=dev).json()["rows"]
    assert len(ids) == 50
    both = dev.client.post(url, json=q(CUSTOMER, ["id", "name"], limit=100), headers=dev).json()[
        "rows"
    ]
    north = rows_of(sample_db_url, "SELECT id FROM customers WHERE region = 'north' ORDER BY id")
    assert [r["id"] for r in both] == [r[0] for r in north]


def test_policy_management_is_privileged_validated_and_audited(
    w: World7, onto: dict[str, Any], owner_session: Session
) -> None:
    dev, dev_id, dev_role = w.member("dev@demo-a.local")
    base = f"{A}/ontologies/{onto['id']}/policies"
    assert dev.client.get(base, headers=dev).status_code == 403
    assert (
        dev.client.post(
            base,
            json={"object_type_key": CUSTOMER, "subject": {"type": "role", "id": dev_role}},
            headers=dev,
        ).status_code
        == 403
    )
    good = w.policy(onto["id"], CUSTOMER, ("user", dev_id), ["id"], 'row.region == "north"')
    for bad in (
        {"object_type_key": "cs.ghost"},
        {"property_keys": ["ghost"]},
        {"row_filter": "row.ghost == 1"},
        {"row_filter": "region == 'x'"},  # policy filters read row.<prop>
        {"row_filter": "row.region ==="},
        {"row_filter": "principal.attributes.x == row.region and name == 1"},
        {"subject": {"type": "role", "id": "00000000-0000-4000-8000-000000000000"}},
        {"subject": {"type": "group", "id": "00000000-0000-4000-8000-000000000000"}},
        {"subject": {"type": "user", "id": "00000000-0000-4000-8000-000000000000"}},
    ):
        body = {
            "object_type_key": CUSTOMER,
            "property_keys": None,
            "subject": {"type": "user", "id": dev_id},
            "effect": "allow",
            **bad,
        }
        assert w.client.post(base, json=body, headers=w.h).status_code == 422, bad
    body = {
        "object_type_key": CUSTOMER,
        "property_keys": None,
        "subject": {"type": "user", "id": dev_id},
        "effect": "allow",
        "row_filter": None,
        "row_version": good["row_version"],
    }
    patched = w.client.patch(f"{base}/{good['id']}", json=body, headers=w.h)
    assert patched.status_code == 200 and patched.json()["property_keys"] is None
    assert patched.json()["row_filter"] is None and patched.json()["row_version"] == 2
    assert w.client.patch(f"{base}/{good['id']}", json=body, headers=w.h).status_code == 409
    listed = w.client.get(base, headers=w.h).json()
    assert good["id"] in [p["id"] for p in listed]
    assert w.client.delete(f"{base}/{good['id']}", headers=w.h).status_code == 204
    assert w.client.delete(f"{base}/{good['id']}", headers=w.h).status_code == 404
    actions = [
        r[0]
        for r in owner_session.execute(
            text("SELECT action FROM shifde.audit_events WHERE action LIKE 'ontology.policy_%'")
        )
    ]
    assert {"ontology.policy_created", "ontology.policy_updated", "ontology.policy_deleted"} <= set(
        actions
    )
    # another organization cannot see or touch them
    hb = login(w.client, w.world.passwords, "admin@demo-b.local")
    assert w.client.get(f"{B}/ontologies/{onto['id']}/policies", headers=hb).status_code == 404


# ================================================================== versions
def test_draft_unpublished_and_retired_versions_cannot_be_queried(
    w: World7, onto: dict[str, Any]
) -> None:
    spec = reference_spec()
    spec["object_types"][0]["label"] = "Client"
    edit(w.client, w.h, "ontologies", onto["id"], spec, 0)
    draft = w.client.get(f"{A}/ontologies/{onto['id']}/draft", headers=w.h).json()
    r = w.client.post(
        f"{A}/ontologies/{onto['id']}/query",
        json=q(CUSTOMER, ["id"], version_id=draft["id"]),
        headers=w.h,
    )
    assert r.status_code == 409 and r.json()["code"] == "version_not_published"
    # no published version at all
    blank = w.create("blank", reference_spec())
    r = w.client.post(f"{A}/ontologies/{blank['id']}/query", json=q(CUSTOMER, ["id"]), headers=w.h)
    assert r.status_code == 409 and r.json()["code"] == "version_not_published"
    # another ontology's version is not a version of this one
    other = w.ready("other")
    r = w.client.post(
        f"{A}/ontologies/{onto['id']}/query",
        json=q(CUSTOMER, ["id"], version_id=other["version"]["id"]),
        headers=w.h,
    )
    assert r.status_code == 404
    # deprecated still answers; retired does not; a disabled ontology does not
    v1 = onto["version"]["id"]
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/versions/{v1}/state",
            json={"state": "deprecated"},
            headers=w.h,
        ).status_code
        == 200
    )
    assert w.query(onto["id"], q(CUSTOMER, ["id"], version_id=v1, limit=1))["rows"]
    assert (
        w.client.post(
            f"{A}/ontologies/{onto['id']}/versions/{v1}/state",
            json={"state": "retired"},
            headers=w.h,
        ).status_code
        == 200
    )
    r = w.client.post(
        f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"], version_id=v1), headers=w.h
    )
    assert r.status_code == 409
    v2 = publish(w.client, w.h, "ontologies", onto["id"], "second")
    assert w.query(onto["id"], q(CUSTOMER, ["id"], limit=1))["rows"]  # default: latest published
    row = w.client.get(f"{A}/ontologies/{onto['id']}", headers=w.h).json()
    assert row["latest_published_version"]["id"] == v2["id"]
    patched = w.client.patch(
        f"{A}/ontologies/{onto['id']}",
        json={"status": "disabled", "row_version": row["row_version"]},
        headers=w.h,
    )
    assert patched.status_code == 200
    r = w.client.post(f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=w.h)
    assert r.status_code == 409 and r.json()["code"] == "ontology_not_active"


def test_tampered_frozen_mappings_are_refused(
    w: World7, onto: dict[str, Any], owner_session: Session
) -> None:
    owner_session.execute(
        text(
            "ALTER TABLE shifde.ontology_source_mappings DISABLE TRIGGER trg_ontology_mappings_guard"
        )
    )
    owner_session.execute(
        text(
            "UPDATE shifde.ontology_source_mappings SET relation_name = 'employees' "
            "WHERE target_key = :k"
        ),
        {"k": CUSTOMER},
    )
    owner_session.execute(
        text(
            "ALTER TABLE shifde.ontology_source_mappings ENABLE TRIGGER trg_ontology_mappings_guard"
        )
    )
    owner_session.commit()
    r = w.client.post(f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=w.h)
    assert r.status_code == 409 and r.json()["code"] == "mapping_unavailable"


# ================================================================== objects
def test_object_resolution_and_relationship_summaries(
    w: World7, onto: dict[str, Any], sample_db_url: str
) -> None:
    cid = rows_of(
        sample_db_url,
        "SELECT customer_id FROM accounts GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1",
    )[0][0]
    r = w.client.get(f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/{cid}", headers=w.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"]["id"] == cid and body["object"]["name"] == f"Customer {cid:03d}"
    assert body["object"]["tax_id"].startswith(
        "TAX-"
    )  # the administrator may read the restricted id
    accounts = rows_of(sample_db_url, "SELECT count(*) FROM accounts WHERE customer_id = %s", cid)[
        0
    ][0]
    requests = rows_of(
        sample_db_url, "SELECT count(*) FROM service_requests WHERE customer_id = %s", cid
    )[0][0]
    by_key = {(x["key"], x["direction"]): x for x in body["relationships"]}
    assert by_key[(R_ACCOUNTS, "forward")]["count"] == accounts
    assert by_key[(R_REQUESTS, "forward")]["count"] == requests
    assert by_key[(R_ACCOUNTS, "forward")]["label"] == "has accounts"
    # unknown / malformed identifiers
    assert (
        w.client.get(
            f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/9999", headers=w.h
        ).status_code
        == 404
    )
    assert (
        w.client.get(f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/abc", headers=w.h).status_code
        == 422
    )
    assert (
        w.client.get(f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/1,2", headers=w.h).status_code
        == 422
    )
    assert (
        w.client.get(f"{A}/ontologies/{onto['id']}/objects/cs.ghost/1", headers=w.h).status_code
        == 404
    )
    # a viewer-with-policy sees only permitted properties and only permitted relationships
    dev, _, dev_role = w.member("dev@demo-a.local")
    w.policy(onto["id"], CUSTOMER, ("role", dev_role), ["id", "name"])
    w.policy(onto["id"], ACCOUNT, ("role", dev_role), ["id"])
    got = dev.client.get(
        f"{A}/ontologies/{onto['id']}/objects/{CUSTOMER}/{cid}", headers=dev
    ).json()
    assert set(got["object"]) == {"id", "name"}
    assert [x["key"] for x in got["relationships"]] == [R_ACCOUNTS]
    # the identifier itself must be readable
    w.policy(onto["id"], REQUEST, ("role", dev_role), ["summary"])
    assert (
        dev.client.get(f"{A}/ontologies/{onto['id']}/objects/{REQUEST}/1", headers=dev).status_code
        == 403
    )


# ================================================================== lineage / no copy
def test_lineage_records_hashes_and_counts_not_values_and_no_data_is_copied(
    w: World7, onto: dict[str, Any], owner_session: Session
) -> None:
    secret_value = "ZZ-UNIQUE-FILTER-VALUE-9931"
    out = w.query(
        onto["id"],
        q(
            CUSTOMER,
            ["id", "name", "tax_id"],
            filter=f'name != "{secret_value}" and region == "north"',
            traverse=[{"relationship": R_ACCOUNTS, "select": ["id"]}],
        ),
    )
    records = w.lineage(onto["id"])
    assert len(records) == 1 and records[0]["id"] == out["lineage_id"]
    rec = records[0]
    assert rec["channel"] == "api" and rec["outcome"] == "success"
    assert rec["object_types"] == [CUSTOMER, ACCOUNT] and rec["connections"] == [w.pg]
    assert len(rec["compiled_query_hashes"]) == 3
    assert all(
        len(h) == 64 and set(h) <= set("0123456789abcdef") for h in rec["compiled_query_hashes"]
    )
    assert rec["row_counts"][CUSTOMER] == len(out["rows"]) and rec["row_counts"][R_ACCOUNTS] > 0
    assert rec["principal"]["display"] and rec["ontology_version_id"] == onto["version"]["id"]
    # identical queries produce identical hashes: a hash identifies a statement shape
    w.query(onto["id"], q(CUSTOMER, ["id"], filter='region == "north"'))
    w.query(onto["id"], q(CUSTOMER, ["id"], filter='region == "south"'))
    a, b = w.lineage(onto["id"])[:2]
    assert a["compiled_query_hashes"] == b["compiled_query_hashes"]
    # no row value, filter literal or tax id reached SHIFDE tables (only counts and hashes)
    taxes = [r["tax_id"] for r in out["rows"]]
    dump = " ".join(
        str(r[0])
        for table, col in (
            ("ontology_query_lineage", "to_jsonb(t)"),
            ("audit_events", "to_jsonb(t)"),
            ("ontology_access_policies", "to_jsonb(t)"),
        )
        for r in owner_session.execute(text(f"SELECT {col} FROM shifde.{table} t"))
    )
    assert secret_value not in dump and not any(t in dump for t in taxes)
    assert "Customer 0" not in dump
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.ontology_materializations")
        ).scalar_one()
        == 0
    )
    # lineage is paginated, newest first, filterable by run, and ontology:read protected
    page = w.client.get(
        f"{A}/ontology-lineage?ontology_id={onto['id']}&limit=2", headers=w.h
    ).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = w.client.get(
        f"{A}/ontology-lineage?ontology_id={onto['id']}&limit=2&cursor={page['next_cursor']}",
        headers=w.h,
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    blind, _, _ = w.member("blind@demo-a.local", ["workspace:read"])
    assert (
        blind.client.get(
            f"{A}/ontology-lineage?ontology_id={onto['id']}", headers=blind
        ).status_code
        == 404
    )
    # lineage rows are append-only
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        owner_session.execute(text("UPDATE shifde.ontology_query_lineage SET duration_ms = 0"))
    owner_session.rollback()


def test_failed_source_queries_are_recorded_and_materialization_is_not_enabled(
    w: World7, onto: dict[str, Any]
) -> None:
    w.client.post(f"{A}/ontologies/{onto['id']}/draft/validate", headers=w.h) if False else None
    r = w.client.put(
        f"{A}/ontologies/{onto['id']}/materializations/{CUSTOMER}", json={}, headers=w.h
    )
    assert r.status_code == 501 and r.json()["code"] == "materialization_not_enabled"
    # disable the connection after publishing: queries fail clearly and the failure is lineage
    row = w.client.get(f"{A}/connections/{w.pg}", headers=w.h).json()
    w.client.post(
        f"{A}/connections/{w.pg}/status",
        json={"status": "disabled", "row_version": row["row_version"]},
        headers=w.h,
    )
    r = w.client.post(f"{A}/ontologies/{onto['id']}/query", json=q(CUSTOMER, ["id"]), headers=w.h)
    assert r.status_code == 409 and r.json()["code"] == "connection_disabled"
    rec = w.lineage(onto["id"])[0]
    assert rec["outcome"] == "failed" and rec["error_code"] == "connection_disabled"


_ = (QUERY_PERMS, edit)
