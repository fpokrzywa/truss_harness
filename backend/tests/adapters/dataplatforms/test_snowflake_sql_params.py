"""SP6-1: `param_types` of a Snowflake invoke (SP6 design 6.1, call S6-I): the API's type names
keyed by 1-based position map to SQL API binding types, values travel as text (a date as `TEXT`
`YYYY-MM-DD`, never the epoch `DATE` binding), a position without a type keeps the inference,
and a malformed `param_types` is a CONFIG failure before any request."""

from __future__ import annotations

from typing import Any  # Any: request values

import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms.fakes import SELECT_ONE, FakeSnowflakeAccount
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from tests.adapters.dataplatforms.helpers import ctx_for, sf_conn

ADAPTER = SnowflakeAdapter()


def _invoke(fake: FakeSnowflakeAccount, request: dict[str, Any]) -> Any:  # Any: InvokeResult
    session = ADAPTER.open(ctx_for(fake), sf_conn())
    try:
        return session.invoke(request)
    finally:
        session.close()


def test_snowflake_param_types_map_to_binding_types(sf: FakeSnowflakeAccount) -> None:
    _invoke(
        sf,
        {
            "sql": SELECT_ONE,
            "params": [7, 1.5, "north", "2026-01-01", True, None, 3],
            "param_types": {
                "1": "BIGINT",
                "2": "DOUBLE",
                "3": "STRING",
                "4": "DATE",
                "5": "BOOLEAN",
                "6": "BIGINT",
            },
        },
    )
    [body] = sf.bodies()
    assert body["bindings"] == {
        "1": {"type": "FIXED", "value": "7"},
        "2": {"type": "REAL", "value": "1.5"},
        "3": {"type": "TEXT", "value": "north"},
        "4": {"type": "TEXT", "value": "2026-01-01"},
        "5": {"type": "BOOLEAN", "value": "true"},
        "6": {"type": "FIXED", "value": None},
        "7": {"type": "FIXED", "value": "3"},  # no entry: inferred from the value
    }
    assert all(b["type"] != "DATE" for b in body["bindings"].values())


def test_snowflake_declared_types_win_over_the_value(sf: FakeSnowflakeAccount) -> None:
    _invoke(sf, {"sql": SELECT_ONE, "params": [5, False], "param_types": {"1": "STRING"}})
    [body] = sf.bodies()
    assert body["bindings"] == {
        "1": {"type": "TEXT", "value": "5"},
        "2": {"type": "BOOLEAN", "value": "false"},
    }


def test_snowflake_without_param_types_the_inference_is_unchanged(sf: FakeSnowflakeAccount) -> None:
    _invoke(sf, {"sql": SELECT_ONE, "params": ["x", 2], "param_types": None})
    [body] = sf.bodies()
    assert body["bindings"] == {
        "1": {"type": "TEXT", "value": "x"},
        "2": {"type": "FIXED", "value": "2"},
    }


@pytest.mark.parametrize(
    "param_types",
    [["BIGINT"], "BIGINT", {"1": "INT"}, {"1": "bigint"}, {"1": "TIMESTAMP"}, {"1": None}],
)
def test_snowflake_malformed_param_types_are_config_before_any_request(
    sf: FakeSnowflakeAccount, param_types: Any
) -> None:
    with pytest.raises(AdapterFailure) as raised:
        _invoke(sf, {"sql": SELECT_ONE, "params": [1], "param_types": param_types})
    assert raised.value.error_class is ErrorClass.CONFIG
    assert raised.value.message == "malformed request"
    assert sf.requests == []


def test_snowflake_a_typed_value_that_is_no_scalar_is_refused(sf: FakeSnowflakeAccount) -> None:
    with pytest.raises(AdapterFailure) as raised:
        _invoke(sf, {"sql": SELECT_ONE, "params": [{"a": 1}], "param_types": {"1": "STRING"}})
    assert raised.value.error_class is ErrorClass.CONFIG
    assert sf.requests == []
