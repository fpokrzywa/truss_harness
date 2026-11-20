"""Evaluation datasets (issue #80; sub-project 3 design section 6, settled calls 7, 19, 21 and
22): validation (1 to 200 cases, 1 MiB, unique ids, the closed list of assertion kinds, the regex
cap), versions numbered per name and project, the digest, reads, who may write, and archived
scopes."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build.canonical import spec_hash
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import post
from tests._support.p3util import ADMIN, VIEWER, A
from tests._support.p5util import user_session
from tests.evaluation.helpers import case
from tests.packaging.helpers import PROJECT, dataset, datasets

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _body(cases: list[dict[str, Any]], name: str = "golden") -> dict[str, Any]:
    return {"name": name, "cases": cases}


def _cases(n: int, message: str = "hello") -> list[dict[str, Any]]:
    return [case(f"c{i}", message, ("contains", "hello")) for i in range(n)]


def test_validation_refusals(client: TestClient, world: World, owner_session: Session) -> None:
    h = user_session(client, world.passwords, ADMIN)
    url = datasets()
    one = case("c1", "hello", ("contains", "hello"))
    big = "x" * 600_000  # two cases of 600 kB: over 1 MiB in all
    refused: list[tuple[str, Any]] = [
        ("no cases", _body([])),
        ("201 cases", _body(_cases(201))),
        (
            "over 1 MiB",
            _body([case("a", big, ("contains", "x")), case("b", big, ("contains", "x"))]),
        ),
        ("duplicate case ids", _body([one, one])),
        ("duplicate assertion ids", _body([{**one, "assertions": one["assertions"] * 2}])),
        ("no assertions", _body([{**one, "assertions": []}])),
        ("unknown kind", _body([case("c1", "hello", ("startswith", "h"))])),
        ("regex of 201", _body([case("c1", "hello", ("regex", "a" * 201))])),
        ("invalid regex", _body([case("c1", "hello", ("regex", "(unclosed"))])),
        ("a non-string value", _body([case("c1", "hello", ("contains", 1))])),
        ("an extra field", {**_body([one]), "extra": 1}),
        ("an extra case field", _body([{**one, "note": "x"}])),
        (
            "an extra assertion field",
            _body([{**one, "assertions": [{**one["assertions"][0], "x": 1}]}]),
        ),
        ("no name", _body([one], name="")),
    ]
    for label, body in refused:
        r = h.client.post(url, headers=h, json=body)
        assert r.status_code == 422, (label, r.status_code, r.text[:300])
    count = owner_session.execute(text("SELECT count(*) FROM shifde.evaluation_datasets"))
    assert count.scalar_one() == 0
    # a 200-character pattern and any JSON value for json_equals are accepted
    ok = _body([case("c1", "hello", ("regex", "a" * 200), ("json_equals", {"k": [1, 2.5, None]}))])
    assert h.client.post(url, headers=h, json=ok).status_code == 201


def test_200_cases_twice_under_one_name_are_versions_1_and_2(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    cases = _cases(200, "y" * 4000)  # under 1 MiB
    first = h.client.post(datasets(), headers=h, json=_body(cases))
    assert first.status_code == 201, first.text
    second = h.client.post(datasets(), headers=h, json=_body(cases))
    assert second.status_code == 201, second.text
    a, b = first.json(), second.json()
    assert (a["version_no"], b["version_no"]) == (1, 2)
    assert a["workspace"] == "main" and a["project"] == "sample" and a["name"] == "golden"
    assert HEX64.match(a["digest"]) and a["digest"] == b["digest"] == spec_hash(a["cases"])
    assert len(a["cases"]) == 200
    other = dataset(h.client, h, "other")
    assert other["version_no"] == 1

    listed = h.client.get(datasets(), headers=h, params={"limit": 2})
    assert listed.status_code == 200, listed.text
    assert [x["id"] for x in listed.json()["items"]] == [other["id"], b["id"]]
    rest = h.client.get(datasets(), headers=h, params={"cursor": listed.json()["next_cursor"]})
    assert [x["id"] for x in rest.json()["items"]] == [a["id"]]
    got = h.client.get(f"{datasets()}/{a['id']}", headers=h)
    assert got.status_code == 200 and got.json() == a
    audits = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events WHERE action = 'evaluation.dataset.created' "
            "ORDER BY occurred_at"
        )
    ).all()
    assert [(d[0]["name"], d[0]["version_no"], d[0]["cases"]) for d in audits] == [
        ("golden", 1, 200),
        ("golden", 2, 200),
        ("other", 1, 1),
    ]


def test_another_projects_dataset_and_an_unknown_id_get_one_answer(
    client: TestClient, world: World
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    post(h.client, f"{A}/workspaces/main/projects", h, {"slug": "side", "name": "Side"})
    side = dataset(h.client, h, "side", project="/workspaces/main/projects/side")
    here = h.client.get(f"{datasets()}/{side['id']}", headers=h)
    unknown = h.client.get(f"{datasets()}/{uuid7()}", headers=h)
    assert here.status_code == unknown.status_code == 404
    assert here.json()["detail"] == unknown.json()["detail"] == "dataset not found"
    there = h.client.get(
        f"{datasets(project='/workspaces/main/projects/side')}/{side['id']}", headers=h
    )
    assert there.status_code == 200, there.text
    assert side["id"] not in h.client.get(datasets(), headers=h).text


def test_a_viewer_cannot_read_or_write_and_an_archived_project_takes_none(
    client: TestClient, world: World
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    made = dataset(admin.client, admin)
    viewer = user_session(client, world.passwords, VIEWER)
    for method, url, body in (
        ("GET", datasets(), None),
        ("POST", datasets(), _body(_cases(1))),
        ("GET", f"{datasets()}/{made['id']}", None),
    ):
        r = viewer.client.request(method, url, headers=viewer, json=body)
        assert r.status_code in (403, 404), (method, url, r.status_code, r.text)
    project = admin.client.get(f"{A}{PROJECT}", headers=admin).json()
    archived = admin.client.patch(
        f"{A}{PROJECT}",
        headers=admin,
        json={"status": "archived", "row_version": project["row_version"]},
    )
    assert archived.status_code == 200, archived.text
    r = admin.client.post(datasets(), headers=admin, json=_body(_cases(1)))
    assert r.status_code == 409 and r.json()["code"] == "scope_archived", r.text
