"""Metrics registry with Prometheus text exposition (FR-OBS-003, NFR-OPS-002).

Two sources, both rendered by `GET /api/metrics`:
* process-local instruments of the API process (request latency histogram, request / error
  counters by ROUTE TEMPLATE - never by raw path, so cardinality is bounded);
* cross-process operational gauges read from the database through the aggregate-only
  `shifde_ops_stats` function (queue depth / age, run outcomes by class, model calls and tokens by
  provider, budget denials). The worker is a separate process, so these cannot be process-local.

Labels carry no tenant identifiers and no content.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any  # Any: JSON stats document from the database

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
Labels = tuple[tuple[str, str], ...]


def _labels(values: Mapping[str, str]) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in values.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(labels: Labels, extra: Sequence[tuple[str, str]] = ()) -> str:
    items = [*labels, *extra]
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in items) + "}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._hist: dict[str, dict[Labels, list[float]]] = defaultdict(dict)
        self._help: dict[str, str] = {}

    def describe(self, name: str, text: str) -> None:
        self._help[name] = text

    def inc(self, name: str, labels: Mapping[str, str], value: float = 1.0) -> None:
        key = _labels(labels)
        with self._lock:
            self._counters[name][key] = self._counters[name].get(key, 0.0) + value

    def observe(self, name: str, labels: Mapping[str, str], value: float) -> None:
        key = _labels(labels)
        with self._lock:
            row = self._hist[name].setdefault(key, [0.0] * (len(BUCKETS) + 2))
            for i, bound in enumerate(BUCKETS):
                if value <= bound:
                    row[i] += 1
            row[len(BUCKETS)] += 1  # +Inf bucket == count
            row[len(BUCKETS) + 1] += value  # sum

    def counter(self, name: str, labels: Mapping[str, str]) -> float:
        with self._lock:
            return self._counters[name].get(_labels(labels), 0.0)

    def total(self, name: str) -> float:
        with self._lock:
            return sum(self._counters[name].values())

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hist.clear()

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                for labels, value in sorted(self._counters[name].items()):
                    lines.append(f"{name}{_fmt(labels)} {value:g}")
            for name in sorted(self._hist):
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} histogram")
                for labels, row in sorted(self._hist[name].items()):
                    for i, bound in enumerate(BUCKETS):
                        lines.append(
                            f"{name}_bucket{_fmt(labels, [('le', f'{bound:g}')])} {row[i]:g}"
                        )
                    lines.append(
                        f"{name}_bucket{_fmt(labels, [('le', '+Inf')])} {row[len(BUCKETS)]:g}"
                    )
                    lines.append(f"{name}_sum{_fmt(labels)} {row[len(BUCKETS) + 1]:g}")
                    lines.append(f"{name}_count{_fmt(labels)} {row[len(BUCKETS)]:g}")
        return "\n".join(lines) + ("\n" if lines else "")


registry = Registry()
registry.describe("shifde_http_requests_total", "HTTP requests by method, route template, status")
registry.describe("shifde_http_errors_total", "HTTP error responses by route template and class")
registry.describe("shifde_http_request_duration_seconds", "HTTP request latency by route template")


def record_request(method: str, route: str, status: int, seconds: float) -> None:
    registry.inc(
        "shifde_http_requests_total",
        {"method": method, "route": route, "status": str(status)},
    )
    registry.observe("shifde_http_request_duration_seconds", {"route": route}, seconds)
    if status >= 400:
        registry.inc(
            "shifde_http_errors_total",
            {"route": route, "class": "5xx" if status >= 500 else "4xx"},
        )


def _gauge(
    lines: list[str], name: str, help_text: str, rows: list[tuple[dict[str, str], float]]
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in rows:
        lines.append(f"{name}{_fmt(_labels(labels))} {value:g}")


def render_ops(stats: Mapping[str, Any]) -> str:  # Any: ops stats document
    """Gauges for the database-derived operational stats (window = `stats.window_seconds`)."""
    lines: list[str] = []
    queue = stats.get("queue", {})
    _gauge(
        lines,
        "shifde_queue_jobs",
        "Jobs in the durable queue by state",
        [
            ({"state": "queued"}, float(queue.get("queued", 0))),
            ({"state": "leased"}, float(queue.get("leased", 0))),
        ],
    )
    _gauge(
        lines,
        "shifde_queue_oldest_queued_age_seconds",
        "Age of the oldest runnable queued job",
        [({}, float(queue.get("oldest_queued_age_seconds", 0)))],
    )
    _gauge(
        lines,
        "shifde_queue_dead_jobs_window",
        "Jobs declared dead within the window",
        [({}, float(queue.get("dead", 0)))],
    )
    _gauge(
        lines,
        "shifde_runs_finished_window",
        "Runs that finished within the window by kind, status and error class",
        [
            (
                {"kind": r["kind"], "status": r["status"], "error_class": r["error_class"]},
                float(r["count"]),
            )
            for r in stats.get("runs", [])
        ],
    )
    models = stats.get("models", [])
    _gauge(
        lines,
        "shifde_model_calls_window",
        "Model call attempts within the window by provider",
        [({"provider": m["provider"]}, float(m["calls"])) for m in models],
    )
    _gauge(
        lines,
        "shifde_model_call_failures_window",
        "Failed model call attempts within the window by provider",
        [({"provider": m["provider"]}, float(m["failed"])) for m in models],
    )
    _gauge(
        lines,
        "shifde_model_tokens_window",
        "Tokens consumed within the window by provider and direction",
        [
            ({"provider": m["provider"], "direction": d}, float(m[f"{d}_tokens"]))
            for m in models
            for d in ("input", "output")
        ],
    )
    _gauge(
        lines,
        "shifde_model_call_duration_ms_window",
        "Summed model call duration within the window by provider",
        [({"provider": m["provider"]}, float(m["duration_ms_sum"])) for m in models],
    )
    _gauge(
        lines,
        "shifde_budget_denials_window",
        "Budget denials within the window",
        [({}, float(stats.get("budget_denials", 0)))],
    )
    _gauge(
        lines,
        "shifde_ops_window_seconds",
        "Length of the window the *_window gauges cover",
        [({}, float(stats.get("window_seconds", 0)))],
    )
    return "\n".join(lines) + "\n"
