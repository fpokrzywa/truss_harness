"""Latency measurement for the F-16 harness: warm up a route, time a fixed number of calls with
`time.perf_counter()` and report nearest-rank p50/p95 in milliseconds."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

WARMUP = 3


@dataclass(frozen=True)
class Latency:
    iterations: int
    p50_ms: float
    p95_ms: float


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least `pct` percent of the samples
    at or below it."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def measure(call: Callable[[], httpx.Response], status: int, iterations: int) -> Latency:
    """Call `call` WARMUP times untimed, then `iterations` times timed; every call must answer
    `status`."""
    for _ in range(WARMUP):
        r = call()
        assert r.status_code == status, f"{r.status_code} {r.text}"
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        r = call()
        elapsed = time.perf_counter() - started
        assert r.status_code == status, f"{r.status_code} {r.text}"
        samples.append(elapsed * 1000)
    return Latency(iterations, percentile(samples, 50), percentile(samples, 95))
