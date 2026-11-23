"""build part 2a: the builtin function registry and the executor catalog (no SETH test reaches them
without the API; they take no database and look nothing up)."""

from __future__ import annotations

import pytest

from shifde.build.executors import (
    ADAPTER_OF,
    BUILTIN_FUNCTIONS,
    CONNECTION_TYPES_OF,
    EXECUTORS,
    ONTOLOGY_QUERY,
    SEVERITY,
    SIDE_EFFECT_CLASSES,
    WRITE_CLASSES,
    binding_schema,
)
from shifde.core.errors import ValidationFailed


def test_only_the_four_executor_kinds_exist() -> None:
    assert EXECUTORS == ("builtin.function", "rest.http", "mcp.tool", "connection.sql")
    assert set(ADAPTER_OF) == set(CONNECTION_TYPES_OF) == set(EXECUTORS) - {"builtin.function"}


def test_side_effect_classes_are_ordered_and_writes_are_marked() -> None:
    assert [SEVERITY[c] for c in SIDE_EFFECT_CLASSES] == list(range(len(SIDE_EFFECT_CLASSES)))
    assert {"write_idempotent", "write_non_idempotent", "destructive"} == WRITE_CLASSES
    assert {f.side_effect_class for f in BUILTIN_FUNCTIONS.values()} <= set(SIDE_EFFECT_CLASSES)
    assert not any(f.side_effect_class in WRITE_CLASSES for f in BUILTIN_FUNCTIONS.values())


def test_the_builtin_registry_is_the_vetted_set() -> None:
    assert set(BUILTIN_FUNCTIONS) == {
        "util.echo",
        "text.word_count",
        "math.sum",
        "json.pick",
        ONTOLOGY_QUERY,
        "time.now_utc",
    }
    for key, fn in BUILTIN_FUNCTIONS.items():
        assert fn.key == key and fn.description
        assert fn.input_schema["type"] == "object" and fn.output_schema["type"] == "object"


def test_builtin_functions_compute() -> None:
    f = BUILTIN_FUNCTIONS
    assert f["text.word_count"].fn({"text": "a bc  d"}) == {"words": 3, "characters": 7}
    assert f["math.sum"].fn({"values": [1, 2.5]}) == {"sum": 3.5}
    assert f["json.pick"].fn({"object": {"a": 1, "b": 2}, "keys": ["b", "z"]}) == {
        "picked": {"b": 2}
    }
    assert f["time.now_utc"].fn({})["iso"].endswith("+00:00")


def test_ontology_query_runs_only_inside_a_run() -> None:
    with pytest.raises(ValidationFailed):
        BUILTIN_FUNCTIONS[ONTOLOGY_QUERY].fn({})


def test_binding_schemas_name_the_connection_and_list_the_functions() -> None:
    builtin = binding_schema("builtin.function")
    assert builtin["properties"]["function_key"]["enum"] == sorted(BUILTIN_FUNCTIONS)
    for executor in ("rest.http", "mcp.tool", "connection.sql"):
        schema = binding_schema(executor)
        assert "connection_id" in schema["required"]
        assert schema["additionalProperties"] is False
