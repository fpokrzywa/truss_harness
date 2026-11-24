"""Shared solution-generator fixtures (SP5 design 5.6): a valid draft over the fake workspace's
sample tables and the `FakeProvider` scripts that draft it and answer its evaluation cases.

`SAMPLE_DRAFT` is in the design 5.3 shape. `DRAFT_SCRIPT` makes the fake model answer a brief
carrying `[[appgen-sample]]` with the draft (and `[[appgen-invalid]]` with text that is not JSON).
`AGENT_SCRIPT` makes an agent answer each case in SETH's tool protocol: a tool call matched on the
case message, then the final text matched on a substring of the tool's `<untrusted ...>` result as
`FakeDatabricksWorkspace(sample_data=True)` answers it. The same list configures the fake serving
endpoint. `tests/adapters/dataplatforms/test_databricks_appgen_fixtures.py` proves the properties
SP5-3, SP5-5 and SP5-9 rely on.

`SNOWFLAKE_SAMPLE_DRAFT` (SP6-4) is `SAMPLE_DRAFT` with its tool SQL naming the `DEMO_DB` tables
of `FakeSnowflakeAccount(sample_data=True)` (`SNOWFLAKE_SAMPLE_TABLES`) and everything else
identical, so `AGENT_SCRIPT` is reused unchanged; `DRAFT_SCRIPT` answers a brief carrying
`[[appgen-sample-snowflake]]` with it.
"""

from __future__ import annotations

import copy
import json
from typing import Any  # Any: draft JSON

SAMPLE_TABLES = ("main.default.customers", "main.default.orders", "main.sales.invoices")

ORDERS_SQL = (
    "select o.id, o.amount, o.ordered_on from main.default.orders o "
    "where o.customer_id = :customer_id and o.ordered_on >= :since order by o.id"
)
INVOICED_SQL = (
    "select c.name, sum(i.total) as invoiced from main.default.customers c "
    "join main.default.orders o on o.customer_id = c.id "
    "join main.sales.invoices i on i.order_id = o.id "
    "where c.region = :region group by c.name order by c.name"
)

SAMPLE_DRAFT: dict[str, Any] = {
    "agent": {
        "name": "sample-sales-agent",
        "instructions": (
            "You answer questions about the sample customers, orders and invoices. Use a tool "
            "to read the data and answer with the figures it returns."
        ),
    },
    "tools": [
        {
            "name": "orders_by_customer",
            "description": "Orders of one customer placed on or after a date.",
            "sql": ORDERS_SQL,
            "parameters": [
                {"name": "customer_id", "type": "integer", "required": True},
                {"name": "since", "type": "date", "required": True},
            ],
            "max_rows": 200,
        },
        {
            "name": "invoiced_by_region",
            "description": "Total invoiced per customer of one region.",
            "sql": INVOICED_SQL,
            "parameters": [{"name": "region", "type": "string", "required": True}],
            "max_rows": 200,
        },
    ],
    "screens": [
        {"kind": "chat", "title": "Ask about sales", "placeholder": "Ask about orders"},
        {
            "kind": "table",
            "title": "Customer orders",
            "tool": "orders_by_customer",
            "inputs": ["customer_id", "since"],
        },
    ],
    "evaluation_cases": [
        {
            "case_id": "orders-customer-1",
            "input": {"message": "How many orders has customer 1 placed since 2026-01-01?"},
            "assertions": [
                {"id": "count", "kind": "contains", "value": "2 orders"},
                {"id": "no-error", "kind": "not_contains", "value": "error"},
            ],
        },
        {
            "case_id": "invoiced-north",
            "input": {"message": "What was invoiced per customer in the North region?"},
            "assertions": [
                {"id": "top", "kind": "contains", "value": "Sample Customer 5 was invoiced 405.0"},
            ],
        },
        {
            "case_id": "orders-customer-3",
            "input": {"message": "List the orders of customer 3 since 2026-02-01."},
            "assertions": [
                {"id": "order", "kind": "regex", "value": ".*order 9 for 225\\.5.*"},
            ],
        },
    ],
}

SNOWFLAKE_SAMPLE_TABLES = (
    "demo_db.public.customers",
    "demo_db.public.orders",
    "demo_db.sales.invoices",
)
SNOWFLAKE_NAMES = {
    "main.default.customers": "DEMO_DB.PUBLIC.CUSTOMERS",
    "main.default.orders": "DEMO_DB.PUBLIC.ORDERS",
    "main.sales.invoices": "DEMO_DB.SALES.INVOICES",
}


def _on_snowflake(sql: str) -> str:
    for databricks, snowflake in SNOWFLAKE_NAMES.items():
        sql = sql.replace(databricks, snowflake)
    return sql


SNOWFLAKE_SAMPLE_DRAFT: dict[str, Any] = copy.deepcopy(SAMPLE_DRAFT)
for _tool in SNOWFLAKE_SAMPLE_DRAFT["tools"]:
    _tool["sql"] = _on_snowflake(_tool["sql"])

DRAFT_SCRIPT: list[dict[str, str]] = [
    {"match": "[[appgen-sample]]", "reply": json.dumps(SAMPLE_DRAFT)},
    {"match": "[[appgen-invalid]]", "reply": "not json"},
    {"match": "[[appgen-sample-snowflake]]", "reply": json.dumps(SNOWFLAKE_SAMPLE_DRAFT)},
]


def _call(name: str, arguments: dict[str, Any]) -> str:  # Any: tool arguments
    return json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]})


# per case: (tool name, arguments, a substring of the tool result block, the final text)
CASE_CALLS: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "orders_by_customer",
        {"customer_id": 1, "since": "2026-01-01"},
        '["7", "175.5", "2026-02-17"]',
        "Customer 1 placed 2 orders since 2026-01-01: order 1 and order 7.",
    ),
    (
        "invoiced_by_region",
        {"region": "North"},
        '["Sample Customer 5", "405.0"]',
        "In the North region Sample Customer 1 was invoiced 205.0 and Sample Customer 5 was "
        "invoiced 405.0.",
    ),
    (
        "orders_by_customer",
        {"customer_id": 3, "since": "2026-02-01"},
        '[["9", "225.5", "2026-03-19"]]',
        "Customer 3 placed 1 order since 2026-02-01: order 9 for 225.5.",
    ),
]

AGENT_SCRIPT: list[dict[str, str]] = [
    {"match": case["input"]["message"], "reply": _call(name, arguments)}
    for case, (name, arguments, _, _) in zip(
        SAMPLE_DRAFT["evaluation_cases"], CASE_CALLS, strict=True
    )
] + [{"match": result, "reply": final} for _, _, result, final in CASE_CALLS]
