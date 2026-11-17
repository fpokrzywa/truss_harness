"""Ontology review 2 G3 / B-2012 (unified-only, B-2433): an object read through the route against
a live source with ids beyond 2^53. 9007199254740992 and 9007199254740993 are one float64; the
service keeps `number` keys exact (`engine.exact_number`, F-04), so each id reads its own row."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import A, B
from tests._support.p4util import publish
from tests._support.p7util import World7, sample_admin_exec

LOW, HIGH = 2**53, 2**53 + 1


def test_object_read_keeps_ids_beyond_2_53_exact_at_a_live_source(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    w = World7(client, world, owner_session, sample_db_url)
    reader = sample_db_url.split("//")[1].split(":")[0]
    sample_admin_exec(
        sample_db_url,
        "CREATE TABLE big_ids (id bigint PRIMARY KEY, label text NOT NULL)",
        f"INSERT INTO big_ids VALUES ({LOW}, 'low'), ({HIGH}, 'high')",
        f'GRANT SELECT ON big_ids TO "{reader}"',
    )
    try:
        w.discover()

        def prop(key: str, ptype: str) -> dict[str, Any]:
            return {"key": key, "type": ptype, "nullable": False, "classification": "internal"}

        spec = {
            "namespaces": [{"key": "bg", "label": "BG"}],
            "object_types": [
                {"key": "bg.item", "label": "Item", "namespace": "bg", "primary_key": ["id"],
                 "properties": [prop("id", "number"), prop("label", "string")]},
            ],
            "relationships": [],
            "actions": [],
            "ui": {},
        }  # fmt: skip
        onto = w.create("big-ids", spec)
        w.put_mapping(
            onto["id"],
            {
                "target_kind": "object_type",
                "target_key": "bg.item",
                "connection_id": w.pg,
                "relation": {"schema": "public", "name": "big_ids"},
                "filters": [],
                "column_map": {"id": "id", "label": "label"},
            },
        )
        publish(w.client, w.h, "ontologies", onto["id"])
        w.policy(onto["id"], "bg.item", ("role", w.admin_role))
        for pk, label in ((LOW, "low"), (HIGH, "high")):
            r = w.client.get(f"{A}/ontologies/{onto['id']}/objects/bg.item/{pk}", headers=w.h)
            assert r.status_code == 200, r.text
            obj = r.json()["object"]
            assert obj["label"] == label and int(obj["id"]) == pk
        # the other organization cannot read A's object (unknown ontology for B)
        hb = login(w.client, w.world.passwords, "admin@demo-b.local")
        r = w.client.get(f"{B}/ontologies/{onto['id']}/objects/bg.item/{LOW}", headers=hb)
        assert r.status_code == 404
        r = w.client.get(f"{A}/ontologies/{onto['id']}/objects/bg.item/{LOW}", headers=hb)
        assert r.status_code in (403, 404)
    finally:
        sample_admin_exec(sample_db_url, "DROP TABLE IF EXISTS big_ids")
