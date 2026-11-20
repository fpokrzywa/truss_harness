"""Assertions and scoring (issue #80; sub-project 3 design section 6, settled calls 18 to 21,
B-2706): each assertion kind passes and fails, a catastrophic regex times out within about 2 s,
the output text of a non-string output is its canonical JSON; nearest-rank p95, accuracy rounded
down, the per-case cost cap, and the verdict failing on each threshold alone."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from shifde.evaluation import assertions, scoring

THRESHOLDS = {"min_accuracy": "0.9", "max_p95_latency_ms": 1000, "max_total_cost_usd": "0.5"}


def _one(kind: str, value: object, output: object) -> dict[str, object]:
    [result] = assertions.evaluate([{"id": "a", "kind": kind, "value": value}], output)
    return result


@pytest.mark.parametrize(
    ("kind", "value", "passing", "failing"),
    [
        ("equals", "exact text", "exact text", "exact text!"),
        ("contains", "needle", "a needle here", "a Needle here"),
        ("not_contains", "secret", "nothing to see", "the secret is out"),
        ("regex", r"[a-z]+-\d{3}", "abc-123", "abc-123 tail"),
        ("json_equals", {"a": [1, 2]}, '{"a": [1, 2]}', '{"a": [2, 1]}'),
    ],
)
def test_each_kind_passes_and_fails(kind: str, value: object, passing: str, failing: str) -> None:
    assert _one(kind, value, passing) == {"id": "a", "passed": True, "reason": None}
    failed = _one(kind, value, failing)
    assert failed["passed"] is False and isinstance(failed["reason"], str)


def test_json_equals_refuses_text_that_is_not_json() -> None:
    assert _one("json_equals", "x", "not json") == {
        "id": "a",
        "passed": False,
        "reason": "the output is not JSON",
    }


def test_a_catastrophic_regex_times_out() -> None:
    started = time.monotonic()
    result = _one("regex", "(a+)+$", "a" * 40 + "b")
    elapsed = time.monotonic() - started
    assert result == {"id": "a", "passed": False, "reason": "regex timed out"}
    assert elapsed < 2.5, elapsed


def test_the_regex_child_gets_text_that_is_not_ascii() -> None:
    assert _one("regex", "café \\d+", "café 42")["passed"] is True


def test_a_non_string_output_is_its_canonical_json() -> None:
    output = {"b": 1, "a": "é"}
    assert assertions.output_text(output) == '{"a":"é","b":1}'
    assert _one("equals", '{"a":"é","b":1}', output)["passed"] is True
    assert _one("json_equals", output, output)["passed"] is True


def test_p95_is_the_nearest_rank() -> None:
    assert scoring.p95_nearest_rank([70]) == 70
    assert scoring.p95_nearest_rank([30, 10, 20]) == 30
    assert scoring.p95_nearest_rank(list(range(1, 21))) == 19  # ceil(0.95 * 20) = 19
    assert scoring.p95_nearest_rank(list(range(1, 22))) == 20  # ceil(0.95 * 21) = 20
    assert scoring.p95_nearest_rank(list(range(1, 101))) == 95


def test_accuracy_and_the_per_case_cap_round_down() -> None:
    assert scoring.accuracy(2, 3) == Decimal("0.6666")
    assert scoring.decimal_text(scoring.accuracy(3, 3)) == "1"
    cap = scoring.per_case_cost_cap("1", 3)
    assert cap == Decimal("0.3333333333") and cap * 3 <= Decimal("1")
    assert scoring.per_case_cost_cap("0.0000000003", 3) == Decimal("0.0000000001")
    assert scoring.decimal_text(Decimal("0")) == "0"


def test_the_verdict_fails_on_each_threshold_alone() -> None:
    assert scoring.verdict(Decimal("0.9"), 1000, Decimal("0.5"), THRESHOLDS) == "passed"
    assert scoring.verdict(Decimal("0.8999"), 1000, Decimal("0.5"), THRESHOLDS) == "failed"
    assert scoring.verdict(Decimal("1"), 1001, Decimal("0.5"), THRESHOLDS) == "failed"
    assert scoring.verdict(Decimal("1"), 10, Decimal("0.5000000001"), THRESHOLDS) == "failed"
