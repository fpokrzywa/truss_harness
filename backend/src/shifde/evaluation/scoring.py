"""Evaluation scoring (sub-project 3 design section 6, settled calls 18 to 20): pure functions.

`accuracy` = passed cases / total cases, rounded down to 4 places (the column is numeric(5,4));
`p95_latency_ms` is the nearest-rank 95th percentile of the case run durations (one case: its
duration); `total_cost_usd` is the sum of the case run families' ledger costs. An evaluation
passes when `accuracy >= min_accuracy`, `p95_latency_ms <= max_p95_latency_ms` and
`total_cost_usd <= max_total_cost_usd`, all compared as decimals; otherwise it fails. Each case
run's cost cap is the cost threshold divided by the number of cases, rounded down to 10 places
(the scale of `usage_events.cost_amount`), so the caps never add up to more than the threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal
from typing import Any  # Any: stored thresholds (JSON)

ACCURACY_PLACES = Decimal("0.0001")
COST_PLACES = Decimal("0.0000000001")


def decimal_text(value: Decimal) -> str:
    """`value` without exponent or trailing zeros ("1.0000" -> "1", "0" -> "0")."""
    return "0" if value == 0 else format(value.normalize(), "f")


def per_case_cost_cap(max_total_cost_usd: str, cases: int) -> Decimal:
    return (Decimal(max_total_cost_usd) / cases).quantize(COST_PLACES, rounding=ROUND_DOWN)


def accuracy(passed: int, total: int) -> Decimal:
    return (Decimal(passed) / Decimal(total)).quantize(ACCURACY_PLACES, rounding=ROUND_DOWN)


def p95_nearest_rank(durations_ms: Sequence[int]) -> int:
    ordered = sorted(durations_ms)
    rank = -(-95 * len(ordered) // 100)  # ceil(0.95 n) in integers
    return ordered[max(rank, 1) - 1]


def verdict(
    accuracy_value: Decimal,
    p95_latency_ms: int,
    total_cost_usd: Decimal,
    thresholds: Mapping[str, Any],
) -> str:
    """`passed` when every threshold holds, else `failed`."""
    ok = (
        accuracy_value >= Decimal(str(thresholds["min_accuracy"]))
        and p95_latency_ms <= int(thresholds["max_p95_latency_ms"])
        and total_cost_usd <= Decimal(str(thresholds["max_total_cost_usd"]))
    )
    return "passed" if ok else "failed"
