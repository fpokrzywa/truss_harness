"""ARCH M11 (issue #15): the classification of what an ontology read touched.

The planner keeps the highest classification of every property a planned node touches: selected,
filtered and primary-key properties, on the root type and on every traversed type. A property with
no `classification` key counts as `internal` (the spec validator's default). The scale itself is
`shifde.core.classification`, the one the spec validator uses."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from shifde.build import ontologies
from shifde.core import classification
from shifde.ontology import engine
from shifde.ontology import policy as pol
from shifde.ontology import schemas as s

ROLE = uuid4()
CONN = uuid4()


def _ot(key: str, props: dict[str, str | None], pk: str = "id") -> dict[str, Any]:  # Any: JSON
    return {
        "key": key, "label": key, "namespace": "x", "primary_key": [pk],
        "properties": [
            {"key": k, "type": "string", **({"classification": c} if c else {})}
            for k, c in props.items()
        ],
    }  # fmt: skip


SPEC = {
    "object_types": [
        _ot(
            "x.a",
            {
                "id": "public",
                "code": "public",
                "name": "internal",
                "secret": "confidential",
                "note": None,
            },
        ),  # fmt: skip
        _ot("x.b", {"id": "public", "tax": "restricted"}),
        _ot("x.c", {"ref": "confidential", "label": "public"}, pk="ref"),
    ],
    "relationships": [{"key": "x.ab", "label": "ab", "from": "x.a", "to": "x.b"}],
}


def _mapping(kind: str, key: str, columns: list[str]) -> dict[str, Any]:  # Any: mapping row
    return {
        "target_kind": kind, "target_key": key, "connection_id": CONN, "relation_schema": "",
        "relation_name": "t", "column_map": {c: c for c in columns},
        "key_map": {"from_column": "a", "to_column": "b"}, "filters": [],
    }  # fmt: skip


def _policy(object_type: str, props: list[str] | None) -> dict[str, Any]:  # Any: policy row
    return {
        "id": uuid4(), "object_type_key": object_type, "effect": "allow", "property_keys": props,
        "subject_type": "role", "subject_id": ROLE, "row_filter": None,
    }  # fmt: skip


def _planner() -> engine.Planner:
    mappings = {
        ("object_type", "x.a"): _mapping(
            "object_type", "x.a", ["id", "code", "name", "secret", "note"]
        ),
        ("object_type", "x.b"): _mapping("object_type", "x.b", ["id", "tax"]),
        ("object_type", "x.c"): _mapping("object_type", "x.c", ["ref", "label"]),
        ("relationship", "x.ab"): _mapping("relationship", "x.ab", []),
    }
    # policies name their properties: a restricted one is readable only when named, and "all
    # properties" reads the classification of each (`note` has none in this unvalidated spec)
    policies = [
        _policy("x.a", ["id", "code", "name", "secret", "note"]),
        _policy("x.b", ["id", "tax"]),
        _policy("x.c", None),
    ]
    return engine.Planner(
        engine.EngineContext(
            spec=SPEC,
            mappings=mappings,
            policies=policies,
            subjects=pol.Subjects(uuid4(), "user", frozenset({ROLE}), frozenset(), {}),
            capabilities=lambda _cid: frozenset({"query.sql"}),
            edge_operands=lambda _m: {},
        )
    )


def _read(
    select: list[str], filter_source: str | None = None, traverse: list[s.HopIn] | None = None
) -> str:
    planner = _planner()
    planner.node("x.a", select, filter_source, traverse or [], 0)
    return planner.classification


def test_the_classification_is_the_highest_over_the_selected_properties() -> None:
    assert _read(["code"]) == "public"  # with the public primary key
    assert _read(["code", "name"]) == "internal"
    assert _read(["name", "secret", "code"]) == "confidential"


def test_a_property_used_only_in_a_filter_counts() -> None:
    assert _read(["code"], 'secret == "s"') == "confidential"
    assert _read(["code"], 'row.name == "n"') == "internal"


def test_the_properties_of_a_traversed_type_count() -> None:
    hop = s.HopIn(relationship="x.ab", select=["tax"])
    assert _read(["code"], None, [hop]) == "restricted"
    assert _read(["code"], None, [s.HopIn(relationship="x.ab", select=["id"])]) == "public"


def test_a_property_without_a_classification_key_counts_as_internal() -> None:
    assert _read(["note"]) == "internal"


def test_the_primary_key_counts() -> None:
    planner = _planner()
    planner.node("x.c", ["label"], None, [], 0)  # the key is read for ordering and joins
    assert planner.classification == "confidential"
    planner = _planner()
    planner.node("x.a", ["code"], None, [], 0, touched_extra=["secret"])  # an object's extras
    assert planner.classification == "confidential"


def test_a_failed_plan_never_lowers_what_the_planner_has_seen() -> None:
    planner = _planner()
    planner.node("x.a", ["secret"], None, [], 0)
    with pytest.raises(Exception, match="no property"):
        planner.node("x.a", ["zzz"], None, [], 0)
    planner.node("x.a", ["code"], None, [], 0)
    assert planner.classification == "confidential"


# ------------------------------------------------------------------ the scale
def test_the_spec_validator_uses_the_one_scale() -> None:
    assert ontologies.CLASSIFICATIONS is classification.CLASSIFICATIONS
    assert classification.CLASSIFICATIONS == ("public", "internal", "confidential", "restricted")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("public", "public"),
        ("internal", "internal"),
        ("confidential", "confidential"),
        ("restricted", "restricted"),
        (None, "public"),
        ("high", "public"),
        ("Restricted", "public"),
        (3, "public"),
        (["restricted"], "public"),
        ({"level": "restricted"}, "public"),
    ],
)
def test_any_value_becomes_a_level_and_an_invalid_one_is_public(
    value: object, expected: str
) -> None:
    assert classification.level(value) == expected


def test_rank_and_highest_follow_the_scale() -> None:
    ranks = [classification.rank(c) for c in classification.CLASSIFICATIONS]
    assert ranks == [0, 1, 2, 3]
    assert classification.rank("bogus") == 0
    assert classification.highest() == "public"
    assert classification.highest("internal", "restricted", "public") == "restricted"
    assert classification.highest("confidential", "bogus", None) == "confidential"


def test_the_classification_starts_public() -> None:
    assert _planner().classification == "public"
