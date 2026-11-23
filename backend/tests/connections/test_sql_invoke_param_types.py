"""SP5-1: `SqlInvokeIn.param_types` (design 6.1): the five Databricks SQL types are accepted, any
other value is refused, and through the invoke route that refusal is the pydantic 422 path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shifde.connections.schemas import SqlInvokeIn
from tests.connections.conftest import client, people
from tests.connections.routes import as_user, connection, org
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

__all__ = ["client", "people"]

TYPES = ["STRING", "BIGINT", "DOUBLE", "BOOLEAN", "DATE"]


def test_sql_invoke_accepts_the_five_param_types() -> None:
    model = SqlInvokeIn.model_validate(
        {"sql": "SELECT :a", "params": {"a": 1}, "param_types": {t.lower(): t for t in TYPES}}
    )
    assert model.param_types == {t.lower(): t for t in TYPES}
    assert SqlInvokeIn(sql="SELECT 1").model_dump()["param_types"] is None


@pytest.mark.parametrize("bad", ["INT", "bigint", "TIMESTAMP", 1, None])
def test_sql_invoke_refuses_an_unknown_param_type(bad: object) -> None:
    with pytest.raises(ValidationError) as raised:
        SqlInvokeIn.model_validate({"sql": "SELECT :a", "param_types": {"a": bad}})
    assert [e["loc"] for e in raised.value.errors()] == [("param_types", "a")]


def test_the_sql_invoke_route_answers_422_for_an_unknown_param_type(
    client: TestClient, world: World, people: People
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    made = connection(
        client,
        h,
        {
            "name": "lakehouse",
            "connection_type": "data_platform",
            "adapter_key": "databricks",
            "config": {"host": "https://ws1.cloud.databricks.com", "warehouse_id": "wh-0001"},
        },
        A,
    )
    url = f"{A}/connections/{made['id']}/invoke"
    bad = client.post(
        url, headers=h, json={"sql": "SELECT :a", "params": {"a": 1}, "param_types": {"a": "INT"}}
    )
    assert bad.status_code == 422, bad.text
    assert [e["loc"] for e in bad.json()["errors"]] == [["body", "param_types", "a"]]
    good = client.post(
        url,
        headers=h,
        json={"sql": "SELECT :a", "params": {"a": 1}, "param_types": {"a": "BIGINT"}},
    )
    assert good.status_code != 422, good.text  # parsed; the draft connection is refused later
