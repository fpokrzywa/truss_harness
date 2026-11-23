"""SP6-1: `FakeSnowflakeAccount` sample data, Cortex and the new fault switches (SP6 design 6.1):
discovery of `DEMO_DB`, a positional JOIN answered from read-only SQLite with typed columns, a
write refused, `table_denied:<D.S.T>`, `CORTEX.COMPLETE` in the SQL API, the Cortex chat REST path
(also through the `openai_compatible` provider), `cortex_denied` and unknown switches."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies

import httpx
import pytest

from shifde.adapters.base import (
    AdapterFailure,
    ChatMessage,
    ChatRequest,
    ConnectionConfig,
    Credential,
    ErrorClass,
)
from shifde.adapters.dataplatforms.fakes import SAMPLE_TABLES, FakeSnowflakeAccount
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.adapters.model_providers import OpenAICompatibleProvider
from tests._support.cortex_fixtures import CORTEX_CHAT_REPLY, CORTEX_COMPLETE_REPLY, key_structure
from tests.adapters.dataplatforms.helpers import SF_PAT, ctx_for, make_snowflake, sf_conn

ADAPTER = SnowflakeAdapter()
MODEL = "mistral-large2"
SCRIPT = [{"match": "North", "reply": "The North region has two customers."}]
JOIN = (
    "select c.name, o.id, i.total from demo_db.public.customers c "
    'join "DEMO_DB"."PUBLIC"."ORDERS" o on o.customer_id = c.id '
    "join DEMO_DB.SALES.INVOICES i on i.order_id = o.id "
    "where c.id = ? and o.amount > ? order by o.id"
)
COMPLETE = "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?), PARSE_JSON(?))"
CORTEX_PATH = "/api/v2/cortex/v1/chat/completions"


def _fake(**kw: Any) -> FakeSnowflakeAccount:  # Any: fake fields
    return make_snowflake(**{"sample_data": True, "cortex": {MODEL: SCRIPT}, **kw})


def _invoke(fake: FakeSnowflakeAccount, request: dict[str, Any], write: bool = False) -> Any:
    session = ADAPTER.open(ctx_for(fake), sf_conn("bearer"))
    try:
        return session.invoke(request, write=write)
    finally:
        session.close()


def _count(fake: FakeSnowflakeAccount, table: str) -> Any:  # Any: the count cell
    return _invoke(fake, {"sql": f"select count(*) from {table}"}).result["rows"][0][0]


def _messages(text: str) -> str:
    return json.dumps(
        [{"role": "system", "content": "You answer."}, {"role": "user", "content": text}]
    )


def _cortex_rest(fake: FakeSnowflakeAccount, body: Any, token: str = SF_PAT) -> httpx.Response:
    with httpx.Client(transport=fake.transport(), base_url=f"https://{fake.host}") as client:
        return client.post(
            CORTEX_PATH,
            headers={"Authorization": f"Bearer {token}", "Host": fake.host},
            json=body,
        )


def test_snowflake_sample_data_discovery_lists_demo_db_with_typed_columns() -> None:
    found = ADAPTER.discover(ctx_for(_fake()), sf_conn("bearer"))
    tables = {(t["catalog"], t["schema"], t["name"]): t for t in found.payload["tables"]}
    columns = {k: [(c["name"], c["type"]) for c in t["columns"]] for k, t in tables.items()}
    assert columns[("DEMO_DB", "PUBLIC", "CUSTOMERS")] == [
        ("ID", "NUMBER"),
        ("NAME", "TEXT"),
        ("REGION", "TEXT"),
    ]
    assert columns[("DEMO_DB", "PUBLIC", "ORDERS")] == [
        ("ID", "NUMBER"),
        ("CUSTOMER_ID", "NUMBER"),
        ("AMOUNT", "FLOAT"),
        ("ORDERED_ON", "DATE"),
    ]
    assert columns[("DEMO_DB", "SALES", "INVOICES")] == [
        ("ID", "NUMBER"),
        ("ORDER_ID", "NUMBER"),
        ("TOTAL", "FLOAT"),
    ]
    assert ("ANALYTICS", "PUBLIC", "CUSTOMERS") in tables  # the existing tree stays
    assert tables[("DEMO_DB", "PUBLIC", "ORDERS")]["columns"][0]["nullable"] is False


def test_without_sample_data_the_snowflake_fake_has_no_demo_db() -> None:
    found = ADAPTER.discover(ctx_for(make_snowflake()), sf_conn("bearer"))
    assert "DEMO_DB" not in {t["catalog"] for t in found.payload["tables"]}
    assert _invoke(make_snowflake(), {"sql": "select * from DEMO_DB.PUBLIC.ORDERS"}).result == {
        "columns": [],
        "rows": [],
        "row_count": 0,
    }


def test_a_positional_join_is_answered_from_the_snowflake_sample_tables() -> None:
    fake = _fake()
    result = _invoke(
        fake, {"sql": JOIN, "params": [1, 30], "param_types": {"1": "BIGINT", "2": "DOUBLE"}}
    )
    assert result.result == {
        "columns": [
            {"name": "NAME", "type": "text"},
            {"name": "ID", "type": "fixed"},
            {"name": "TOTAL", "type": "real"},
        ],
        "rows": [["Sample Customer 1", "7", "177.5"]],
        "row_count": 1,
    }
    [body] = fake.bodies()
    assert body["bindings"] == {
        "1": {"type": "FIXED", "value": "1"},
        "2": {"type": "REAL", "value": "30"},
    }
    dated = _invoke(
        fake,
        {
            "sql": "select id, ordered_on from demo_db.public.orders where ordered_on >= ? "
            "and id < ? order by id",
            "params": ["2026-03-01", 11],
            "param_types": {"1": "DATE", "2": "BIGINT"},
        },
    )
    assert dated.result["columns"] == [
        {"name": "ID", "type": "fixed"},
        {"name": "ORDERED_ON", "type": "date"},
    ]
    assert dated.result["rows"] == [["9", "2026-03-19"], ["10", "2026-03-20"]]


def test_a_three_part_name_in_a_snowflake_literal_is_not_rewritten() -> None:
    fake = _fake()
    sql = "select 'demo_db.public.orders' as t from DEMO_DB.PUBLIC.ORDERS where id = 1"
    assert _invoke(fake, {"sql": sql}).result["rows"] == [["demo_db.public.orders"]]


def test_the_sample_rows_are_the_ones_the_databricks_fake_serves() -> None:
    fake = _fake()
    for name, table in (
        ("customers", "DEMO_DB.PUBLIC.CUSTOMERS"),
        ("orders", "DEMO_DB.PUBLIC.ORDERS"),
        ("invoices", "DEMO_DB.SALES.INVOICES"),
    ):
        rows = _invoke(fake, {"sql": f"select * from {table} order by id"}).result["rows"]
        assert rows == [[str(v) for v in row] for row in SAMPLE_TABLES[name][1]]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE DEMO_DB.PUBLIC.ORDERS SET amount = 0",
        "delete from demo_db.public.orders",
        "insert into DEMO_DB.PUBLIC.ORDERS (id) values (99)",
    ],
)
def test_a_snowflake_sample_write_fails_and_changes_nothing(statement: str) -> None:
    fake = _fake()
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": statement}, write=True)
    assert raised.value.error_class is ErrorClass.UPSTREAM
    assert _count(fake, "DEMO_DB.PUBLIC.ORDERS") == "12"
    total = _invoke(fake, {"sql": "select sum(amount) from DEMO_DB.PUBLIC.ORDERS"})
    assert total.result["rows"] == [[str(sum(r[2] for r in SAMPLE_TABLES["orders"][1]))]]


def test_an_unknown_snowflake_table_answers_422_without_rows() -> None:
    fake = _fake()
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": "select * from DEMO_DB.PUBLIC.SECRETS"})
    assert raised.value.error_class is ErrorClass.UPSTREAM
    response = fake.handle(fake.requests[-1])
    assert response.status_code == 422
    assert response.json() == {
        "code": "002003",
        "message": "Object does not exist or not authorized.",
        "sqlState": "42000",
    }


def test_snowflake_table_denied_refuses_the_named_table_only() -> None:
    fake = _fake(faults={"table_denied:DEMO_DB.PUBLIC.ORDERS"})
    for sql in ("select * from demo_db.public.orders", 'select * from "DEMO_DB"."PUBLIC"."ORDERS"'):
        with pytest.raises(AdapterFailure) as raised:
            _invoke(fake, {"sql": sql})
        assert raised.value.error_class is ErrorClass.UPSTREAM
        response = fake.handle(fake.requests[-1])
        assert response.status_code == 422 and "rows" not in response.text
    with pytest.raises(AdapterFailure):
        _invoke(fake, {"sql": JOIN, "params": [1, 0]})
    assert _count(fake, "demo_db.public.customers") == "6"
    # the switch alone (no sample data) still refuses the table
    bare = make_snowflake(faults={"table_denied:DEMO_DB.PUBLIC.ORDERS"})
    with pytest.raises(AdapterFailure):
        _invoke(bare, {"sql": "select * from DEMO_DB.PUBLIC.ORDERS"})


@pytest.mark.parametrize("switch", ["nope", "table_denied:a.b.c", "endpoint_not_ready"])
def test_an_unknown_snowflake_fake_switch_raises(switch: str) -> None:
    with pytest.raises(ValueError, match="unknown fault switches"):
        FakeSnowflakeAccount(faults={switch})
    FakeSnowflakeAccount(faults={"table_denied:DEMO_DB.PUBLIC.ORDERS", "cortex_denied", "slow"})


def test_cortex_complete_in_sql_answers_the_options_form() -> None:
    fake = _fake()
    statement = "select  snowflake.cortex.complete(\n  ?, parse_json( ? ), PARSE_JSON(?) )"
    result = _invoke(
        fake,
        {
            "sql": statement,
            "params": [MODEL, _messages("How many in the North?"), json.dumps({"temperature": 0})],
        },
    )
    assert result.result["columns"] == [{"name": "COMPLETE", "type": "text"}]
    [[cell]] = result.result["rows"]
    doc = json.loads(cell)
    assert key_structure(doc) == key_structure(CORTEX_COMPLETE_REPLY)
    assert doc["choices"][0]["messages"] == SCRIPT[0]["reply"]
    assert doc["model"] == MODEL
    assert doc["usage"] == {
        "prompt_tokens": 2 + 5,
        "completion_tokens": 6,
        "total_tokens": 13,
    }
    other = _invoke(fake, {"sql": COMPLETE, "params": [MODEL, _messages("hello there"), "{}"]})
    assert json.loads(other.result["rows"][0][0])["choices"][0]["messages"] == "fake: hello there"


@pytest.mark.parametrize("faults", [set(), {"cortex_denied"}], ids=["unknown-model", "denied"])
def test_cortex_complete_refusals_are_422(faults: set[str]) -> None:
    fake = _fake(faults=faults)
    model = "unknown-model" if not faults else MODEL
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": COMPLETE, "params": [model, _messages("North"), "{}"]})
    assert raised.value.error_class is ErrorClass.UPSTREAM
    assert fake.handle(fake.requests[-1]).status_code == 422


def test_the_cortex_chat_rest_path_serves_the_script() -> None:
    fake = _fake()
    body = {"model": MODEL, "messages": [{"role": "user", "content": "North please"}]}
    r = _cortex_rest(fake, body)
    assert r.status_code == 200, r.text
    assert key_structure(r.json()) == key_structure(CORTEX_CHAT_REPLY)
    assert r.json()["choices"][0]["message"]["content"] == SCRIPT[0]["reply"]
    assert _cortex_rest(fake, {**body, "model": "other"}).status_code == 404
    assert _cortex_rest(fake, body, token="wrong").status_code == 401
    any_token = _fake(pat=None, accept_any_token=True)
    assert _cortex_rest(any_token, body, token="whatever").status_code == 200


def _provider_conn(fake: FakeSnowflakeAccount, token: str = SF_PAT) -> ConnectionConfig:
    return ConnectionConfig(
        id=None,
        name="cortex",
        adapter_key="openai_compatible",
        config={"base_url": f"https://{fake.host}/api/v2/cortex/v1"},
        auth_method="bearer",
        credential=Credential(token),
    )


def test_the_openai_compatible_provider_reaches_cortex_through_the_fake() -> None:
    fake = _fake()
    request = ChatRequest([ChatMessage("user", "How many in the North?")])
    out = OpenAICompatibleProvider().generate(ctx_for(fake), _provider_conn(fake), MODEL, request)
    assert out.output == SCRIPT[0]["reply"]
    assert (out.usage.input_tokens, out.usage.output_tokens) == (5, 6)
    assert fake.paths() == [("POST", CORTEX_PATH)]
    assert "x-snowflake-authorization-token-type" not in fake.requests[0].headers


def test_cortex_denied_refuses_sql_and_rest() -> None:
    fake = _fake(faults={"cortex_denied"})
    with pytest.raises(AdapterFailure):
        _invoke(fake, {"sql": COMPLETE, "params": [MODEL, _messages("North"), "{}"]})
    request = ChatRequest([ChatMessage("user", "North")])
    with pytest.raises(AdapterFailure) as raised:
        OpenAICompatibleProvider().generate(ctx_for(fake), _provider_conn(fake), MODEL, request)
    assert raised.value.error_class is ErrorClass.AUTH  # the REST path answered 403
    assert _cortex_rest(fake, {"model": MODEL, "messages": []}).status_code == 403
