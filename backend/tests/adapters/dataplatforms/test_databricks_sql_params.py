"""SP5-1: the `databricks` adapter's typed parameters and request `max_rows` (design 6.1, R14):
`parameters[].type` from `param_types`, the cap `min(request max_rows, config max_rows)` with the
`+1` truncation probe, and a body byte-for-byte as before when neither is sent."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies

import httpx
import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import ROWS_STATEMENT, FakeDatabricksWorkspace
from tests.adapters.dataplatforms.helpers import conn, ctx_for, make_fake

ADAPTER = DatabricksAdapter()
STATEMENTS = ("POST", "/api/2.0/sql/statements")


def _invoke(fake: FakeDatabricksWorkspace, request: dict[str, Any], **config: Any) -> Any:  # Any
    session = ADAPTER.open(ctx_for(fake), conn(**config))
    try:
        return session.invoke(request)
    finally:
        session.close()


def _bodies(fake: FakeDatabricksWorkspace) -> list[bytes]:
    return [r.content for r in fake.requests if (r.method, r.url.path) == STATEMENTS]


def test_param_types_are_sent_as_parameter_types() -> None:
    fake = make_fake()
    _invoke(
        fake,
        {
            "sql": "SELECT :a, :b, :c",
            "params": {"a": 5, "b": None, "c": "x"},
            "param_types": {"a": "BIGINT", "b": "DATE"},
        },
    )
    [body] = _bodies(fake)
    assert json.loads(body)["parameters"] == [
        {"name": "a", "value": "5", "type": "BIGINT"},
        {"name": "b", "value": None, "type": "DATE"},
        {"name": "c", "value": "x"},  # no entry in param_types: no type
    ]


def test_without_param_types_and_max_rows_the_body_is_unchanged() -> None:
    fake = make_fake()
    _invoke(fake, {"sql": "SELECT :a", "params": {"a": 5}}, max_rows=7)
    [body] = _bodies(fake)
    expected = {
        "warehouse_id": "wh-0001",
        "statement": "SELECT :a",
        "wait_timeout": "30s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
        "row_limit": 8,
        "parameters": [{"name": "a", "value": "5"}],
    }
    assert body == httpx.Request("POST", "https://x.test", json=expected).content


@pytest.mark.parametrize(
    ("request_rows", "config_rows", "cap"), [(2, 1000, 2), (50, 3, 3), (None, 4, 4)]
)
def test_the_request_max_rows_caps_below_the_connection_max_rows(
    request_rows: int | None, config_rows: int, cap: int
) -> None:
    fake = make_fake()
    fake.row_count = 10
    request: dict[str, Any] = {"sql": ROWS_STATEMENT}
    if request_rows is not None:
        request["max_rows"] = request_rows
    result = _invoke(fake, request, max_rows=config_rows)
    [body] = _bodies(fake)
    assert json.loads(body)["row_limit"] == cap + 1
    assert result.result["row_count"] == cap and len(result.result["rows"]) == cap
    assert result.truncated is True
    assert set(result.result) == {"columns", "rows", "row_count"}


@pytest.mark.parametrize("bad", [0, -1, "5", 2.5, True])
def test_a_malformed_request_max_rows_is_a_config_failure(bad: Any) -> None:  # Any: bad value
    fake = make_fake()
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": "SELECT 1", "max_rows": bad})
    assert raised.value.error_class is ErrorClass.CONFIG
    assert raised.value.message == "malformed request"
    assert _bodies(fake) == []


def test_the_read_only_check_still_runs_with_param_types() -> None:
    fake = make_fake()
    with pytest.raises(AdapterFailure) as raised:
        _invoke(
            fake,
            {
                "sql": "DELETE FROM t WHERE id = :id",
                "params": {"id": 1},
                "param_types": {"id": "BIGINT"},
            },
        )
    assert raised.value.error_class is ErrorClass.CAPABILITY
    assert _bodies(fake) == []
