"""SP5-1: `FakeDatabricksWorkspace` sample data, serving endpoint and new fault switches (design
6.1): Unity Catalog columns of the sample tables, a parameterized JOIN answered from SQLite,
`query_only` refusing a write, `table_denied:<c.s.t>`, the scripted serving endpoint and its
readiness, `endpoint_not_ready`, unknown endpoints and unknown switches."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies

import httpx
import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import (
    ROWS_STATEMENT,
    FakeDatabricksWorkspace,
    FakeSnowflakeAccount,
)
from tests._support.appgen_fixtures import AGENT_SCRIPT, CASE_CALLS, SAMPLE_DRAFT
from tests.adapters.dataplatforms.helpers import HOST, PAT, WAREHOUSE, conn, ctx_for

ADAPTER = DatabricksAdapter()
JOIN = (
    "select c.name, o.id, i.total from main.default.customers c "
    "join main.default.orders o on o.customer_id = c.id "
    "join `main`.`sales`.`invoices` i on i.order_id = o.id "
    "where c.id = :cid and o.amount > :min order by o.id"
)


def _fake(**kw: Any) -> FakeDatabricksWorkspace:  # Any: fake fields
    return FakeDatabricksWorkspace(
        host=HOST, token=PAT, warehouse_id=WAREHOUSE, **{"sample_data": True, **kw}
    )


def _invoke(fake: FakeDatabricksWorkspace, request: dict[str, Any], write: bool = False) -> Any:
    session = ADAPTER.open(ctx_for(fake), conn())
    try:
        return session.invoke(request, write=write)
    finally:
        session.close()


def _call(
    fake: FakeDatabricksWorkspace, method: str, path: str, body: Any = None
) -> httpx.Response:  # Any: JSON body
    with httpx.Client(transport=fake.transport(), base_url=f"https://{HOST}") as client:
        return client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {PAT}", "Host": HOST},
            json=body,
        )


def _submit(fake: FakeDatabricksWorkspace, statement: str, **extra: Any) -> dict[str, Any]:
    r = _call(
        fake,
        "POST",
        "/api/2.0/sql/statements",
        {"warehouse_id": WAREHOUSE, "statement": statement, **extra},
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_sample_data_discovery_returns_the_real_columns() -> None:
    result = ADAPTER.discover(ctx_for(_fake()), conn())
    tables = {(t["catalog"], t["schema"], t["name"]): t for t in result.payload["tables"]}
    columns = {k: [(c["name"], c["type"]) for c in t["columns"]] for k, t in tables.items()}
    assert columns[("main", "default", "customers")] == [
        ("id", "bigint"),
        ("name", "string"),
        ("region", "string"),
    ]
    assert columns[("main", "default", "orders")] == [
        ("id", "bigint"),
        ("customer_id", "bigint"),
        ("amount", "double"),
        ("ordered_on", "date"),
    ]
    assert columns[("main", "sales", "invoices")] == [
        ("id", "bigint"),
        ("order_id", "bigint"),
        ("total", "double"),
    ]
    assert columns[("samples", "nyctaxi", "trips")] == [("id", "bigint"), ("label", "string")]


def test_a_parameterized_join_is_answered_from_the_sample_tables() -> None:
    fake = _fake()
    result = _invoke(
        fake,
        {
            "sql": JOIN,
            "params": {"cid": 1, "min": 30},
            "param_types": {"cid": "BIGINT", "min": "DOUBLE"},
        },
    )
    assert result.result == {
        "columns": ["name", "id", "total"],
        "rows": [["Sample Customer 1", "7", "177.5"]],
        "row_count": 1,
    }
    doc = _submit(
        fake,
        JOIN,
        parameters=[
            {"name": "cid", "value": "1", "type": "BIGINT"},
            {"name": "min", "value": None, "type": "DOUBLE"},
        ],
    )
    assert doc["status"]["state"] == "SUCCEEDED" and doc["result"]["data_array"] == []
    typed = _submit(fake, "select id, amount, ordered_on from main.default.orders where id = 1")
    assert [(c["name"], c["type_name"]) for c in typed["manifest"]["schema"]["columns"]] == [
        ("id", "BIGINT"),
        ("amount", "DOUBLE"),
        ("ordered_on", "DATE"),
    ]
    assert typed["result"]["data_array"] == [["1", "25.5", "2026-01-11"]]


def test_the_row_limit_still_truncates_sample_answers() -> None:
    doc = _submit(_fake(), "select id from main.default.orders order by id", row_limit=5)
    assert doc["result"]["row_count"] == 5 and doc["manifest"]["truncated"] is True


def test_select_one_and_fake_rows_keep_their_answers() -> None:
    fake = _fake()
    assert _submit(fake, "SELECT 1")["result"]["data_array"] == [["1"]]
    assert len(_submit(fake, ROWS_STATEMENT)["result"]["data_array"]) == fake.row_count


def test_without_sample_data_any_other_statement_is_answered_empty_as_before() -> None:
    doc = _submit(_fake(sample_data=False), "select * from main.default.orders")
    assert doc["status"]["state"] == "SUCCEEDED" and doc["result"]["data_array"] == []


@pytest.mark.parametrize(
    "statement",
    [
        "delete from main.default.orders",
        "insert into main.default.orders (id) values (99)",
        "drop table main.default.orders",
    ],
)
def test_query_only_refuses_a_write(statement: str) -> None:
    fake = _fake()
    doc = _submit(fake, statement)
    assert doc["status"]["state"] == "FAILED"
    assert doc["status"]["error"]["error_code"]
    count = _submit(fake, "select count(*) from main.default.orders")
    assert count["result"]["data_array"] == [["12"]]  # nothing changed
    with pytest.raises(AdapterFailure) as raised:  # the adapter, told to write, gets UPSTREAM
        _invoke(fake, {"sql": statement}, write=True)
    assert raised.value.error_class is ErrorClass.UPSTREAM


def test_an_unknown_table_answers_failed() -> None:
    doc = _submit(_fake(), "select * from main.default.nothing")
    assert doc["status"]["state"] == "FAILED"
    assert doc["status"]["error"]["error_code"] == "TABLE_OR_VIEW_NOT_FOUND"


def test_table_denied_answers_permission_denied() -> None:
    fake = _fake(faults={"table_denied:main.sales.invoices"})
    denied = _submit(fake, "select * from MAIN.Sales.INVOICES")
    assert denied["status"]["state"] == "FAILED"
    assert denied["status"]["error"]["error_code"] == "PERMISSION_DENIED"
    assert _submit(fake, JOIN.replace(":cid", "1").replace(":min", "0"))["status"]["state"] == (
        "FAILED"
    )
    allowed = _submit(fake, "select count(*) from main.default.customers")
    assert allowed["result"]["data_array"] == [["6"]]
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": "select * from main.sales.invoices"})
    assert raised.value.error_class is ErrorClass.UPSTREAM


def test_the_serving_endpoint_answers_from_its_script() -> None:
    fake = _fake(serving={"agent-endpoint": AGENT_SCRIPT})
    case = SAMPLE_DRAFT["evaluation_cases"][0]
    r = _call(
        fake,
        "POST",
        "/serving-endpoints/agent-endpoint/invocations",
        {
            "messages": [
                {"role": "system", "content": "You answer questions."},
                {"role": "user", "content": case["input"]["message"]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    name, arguments, _, _ = CASE_CALLS[0]
    reply = json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]})
    assert doc["choices"][0]["message"] == {"role": "assistant", "content": reply}
    assert doc["choices"][0]["finish_reason"] == "stop"
    usage = doc["usage"]
    assert isinstance(usage["prompt_tokens"], int) and isinstance(usage["completion_tokens"], int)
    assert usage["prompt_tokens"] == 3 + len(case["input"]["message"].split())
    assert usage["completion_tokens"] == len(reply.split())
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    other = _call(
        fake,
        "POST",
        "/serving-endpoints/agent-endpoint/invocations",
        {"messages": [{"role": "user", "content": "hello there"}]},
    )
    assert other.json()["choices"][0]["message"]["content"] == "fake: hello there"
    assert ("POST", "/serving-endpoints/agent-endpoint/invocations") in fake.paths()


def test_serving_readiness_and_endpoint_not_ready() -> None:
    ready = _fake(serving={"agent-endpoint": []})
    r = _call(ready, "GET", "/api/2.0/serving-endpoints/agent-endpoint")
    assert r.status_code == 200
    assert r.json() == {"name": "agent-endpoint", "state": {"ready": "READY"}}

    down = _fake(serving={"agent-endpoint": []}, faults={"endpoint_not_ready"})
    r = _call(down, "GET", "/api/2.0/serving-endpoints/agent-endpoint")
    assert r.json()["state"]["ready"] == "NOT_READY"
    r = _call(
        down,
        "POST",
        "/serving-endpoints/agent-endpoint/invocations",
        {"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503 and r.json()["error_code"] == "TEMPORARILY_UNAVAILABLE"


def test_an_unknown_endpoint_is_404_on_both_routes() -> None:
    fake = _fake(serving={"agent-endpoint": []})
    for method, path, body in (
        ("GET", "/api/2.0/serving-endpoints/other", None),
        ("POST", "/serving-endpoints/other/invocations", {"messages": []}),
    ):
        r = _call(fake, method, path, body)
        assert r.status_code == 404 and r.json()["error_code"] == "RESOURCE_DOES_NOT_EXIST"


def test_serving_requests_pass_the_auth_check() -> None:
    fake = _fake(serving={"agent-endpoint": []})
    with httpx.Client(transport=fake.transport(), base_url=f"https://{HOST}") as client:
        r = client.post(
            "/serving-endpoints/agent-endpoint/invocations",
            headers={"Host": HOST, "Authorization": "Bearer wrong"},
            json={"messages": []},
        )
    assert r.status_code == 401


def test_an_unknown_fault_switch_still_raises() -> None:
    with pytest.raises(ValueError, match="unknown fault switches"):
        FakeDatabricksWorkspace(faults={"no_such_switch"})
    FakeDatabricksWorkspace(faults={"endpoint_not_ready", "table_denied:a.b.c", "slow"})
    for switch in ("endpoint_not_ready", "table_denied:a.b.c"):  # Databricks only
        with pytest.raises(ValueError, match="unknown fault switches"):
            FakeSnowflakeAccount(faults={switch})
