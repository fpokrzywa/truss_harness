"""F-10 (#17): the `data_export` and `target_environments` conditions of an `approval_rule`.

`validate_definition` accepts a boolean `data_export` and a non-empty list of 1 to 50 environment
names of 1 to 64 characters (stored de-duplicated and sorted), alone or with the existing keys; it
refuses anything else, an unknown `when` key and an empty `when`. The existing keys validate as
before. The API-level 422 cases are in `test_p8_policies.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from shifde.core.errors import ValidationFailed
from shifde.governance.validation import validate_definition

KNOWN = frozenset({"approval:decide", "governance:manage"})


def _rule(when: Any) -> dict[str, Any]:  # Any: JSON
    return validate_definition("approval_rule", {"when": when}, known_permissions=KNOWN)


@pytest.mark.parametrize(
    ("when", "stored"),
    [
        ({"data_export": True}, {"data_export": True}),
        ({"data_export": False}, {"data_export": False}),
        (
            {"target_environments": ["prod", "dev", "prod"]},
            {"target_environments": ["dev", "prod"]},
        ),
        ({"target_environments": ["e" * 64]}, {"target_environments": ["e" * 64]}),
        (
            {"target_environments": [f"e{i:02}" for i in range(50)]},
            {"target_environments": [f"e{i:02}" for i in range(50)]},
        ),
        (
            {"side_effect_classes": ["write_non_idempotent"], "data_export": True},
            {"side_effect_classes": ["write_non_idempotent"], "data_export": True},
        ),
        (
            {"side_effect_classes": ["destructive", "read"], "target_environments": ["prod"]},
            {"side_effect_classes": ["destructive", "read"], "target_environments": ["prod"]},
        ),
        (
            {"estimated_cost_gt": 5, "currency": "USD", "data_export": False},
            {"estimated_cost_gt": "5", "currency": "USD", "data_export": False},
        ),
    ],
)
def test_the_new_conditions_are_accepted_and_normalized(
    when: dict[str, Any], stored: dict[str, Any]
) -> None:
    assert _rule(when) == {"when": stored}


@pytest.mark.parametrize(
    "when",
    [
        {"data_export": "yes"},
        {"data_export": 1},
        {"data_export": None},
        {"target_environments": []},
        {"target_environments": "prod"},
        {"target_environments": [""]},
        {"target_environments": [1]},
        {"target_environments": ["e" * 65]},
        {"target_environments": [f"e{i}" for i in range(51)]},
        {"target_environments": ["prod"], "environment": "prod"},
        {"currency": "USD", "data_export": True},  # currency still needs estimated_cost_gt
        {},
    ],
)
def test_anything_else_is_refused(when: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        _rule(when)


def test_an_empty_when_names_every_condition_in_its_error() -> None:
    with pytest.raises(ValidationFailed) as refused:
        _rule({})
    message = str(refused.value.detail)
    for key in ("side_effect_classes", "estimated_cost_gt", "data_export", "target_environments"):
        assert key in message


def test_the_existing_keys_validate_as_before() -> None:
    assert _rule(
        {"side_effect_classes": ["write_non_idempotent", "destructive", "destructive"]}
    ) == {"when": {"side_effect_classes": ["destructive", "write_non_idempotent"]}}
    assert _rule({"estimated_cost_gt": "2.5", "currency": "EUR"}) == {
        "when": {"estimated_cost_gt": "2.5", "currency": "EUR"}
    }
    for bad in (
        {"side_effect_classes": ["x"]},
        {"side_effect_classes": []},
        {"estimated_cost_gt": 5},
        {"estimated_cost_gt": -1, "currency": "USD"},
    ):
        with pytest.raises(ValidationFailed):
            _rule(bad)
    with pytest.raises(ValidationFailed):
        validate_definition(
            "approval_rule",
            {"when": {"data_export": True}, "approver_permission": "no:such"},
            known_permissions=KNOWN,
        )
    assert validate_definition(
        "approval_rule",
        {"when": {"data_export": True}, "approver_permission": "governance:manage"},
        known_permissions=KNOWN,
    ) == {"when": {"data_export": True}, "approver_permission": "governance:manage"}
