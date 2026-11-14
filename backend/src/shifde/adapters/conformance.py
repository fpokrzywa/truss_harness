"""Conformance-suite harness (reused by third-party adapters, see ADAPTER_DEVELOPER_GUIDE).

A suite starts a protocol-faithful *target* (local server, real database ...), runs named checks
against the adapter and reports which capabilities were proven. `run_suite` turns the result into
a `ConformanceRun`; `shifde-admin adapters verify` records it as evidence, and only recorded,
passing evidence on the current adapter version makes a capability ``validated``.
"""

from __future__ import annotations

import platform
import sys
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, ClassVar  # Any: the suite-specific target object

from shifde.adapters.base import (
    AdapterDescriptor,
    Capability,
    ConnectionConfig,
    Credential,
    ExecContext,
)
from shifde.adapters.evidence import ConformanceRun
from shifde.core.clock import utcnow
from shifde.core.egress import LOOPBACK_NETWORKS, EgressGuard, EgressPolicy
from shifde.core.redaction import scrub_text


class SuiteSkipped(Exception):
    """The suite's target is unavailable here (no evidence is recorded for a skipped suite)."""


@dataclass(frozen=True)
class Check:
    name: str
    capabilities: tuple[Capability, ...]
    run: Callable[[Any], str | None]  # raises on failure; may return a detail string


class ConformanceSuite(ABC):
    adapter_key: ClassVar[str]
    suite_version: ClassVar[str] = "1"

    @abstractmethod
    def target(self) -> AbstractContextManager[Any]:
        """Start the protocol target; yielded object is passed to every check."""

    @abstractmethod
    def checks(self) -> Sequence[Check]: ...

    def environment(self) -> dict[str, Any]:
        return {"python": sys.version.split()[0], "platform": platform.platform()}


# ----------------------------------------------------------------------------- helpers for suites
def loopback_guard(**policy: Any) -> EgressGuard:  # Any: EgressPolicy overrides
    """Guard that permits loopback (the local protocol target) over http / postgresql."""
    base: dict[str, Any] = {
        "allowed_schemes": frozenset(),
        "allowed_cidrs": LOOPBACK_NETWORKS,
        "timeout_seconds": 10.0,
        "max_response_bytes": 1_000_000,
    }
    base.update(policy)
    return EgressGuard([EgressPolicy(**base)])


def exec_ctx(guard: EgressGuard) -> ExecContext:
    return ExecContext(tenant_id=None, principal_id=None, egress=guard)


def connection(
    adapter_key: str,
    config: dict[str, Any],
    *,
    auth_method: str = "none",
    secret: str | None = None,
    access_mode: str = "read",
) -> ConnectionConfig:
    return ConnectionConfig(
        id=None,
        name=f"conformance-{adapter_key}",
        adapter_key=adapter_key,
        config=config,
        auth_method=auth_method,
        access_mode=access_mode,
        credential=Credential(secret) if secret else None,
    )


def expect(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ----------------------------------------------------------------------------- runner
def run_suite(suite: ConformanceSuite, descriptor: AdapterDescriptor) -> ConformanceRun:
    """Run every check; capabilities are validated only if all their checks passed."""
    started = utcnow()
    results: list[dict[str, Any]] = []
    passed: dict[str, bool] = {}
    with suite.target() as target:
        for check in suite.checks():
            try:
                detail = check.run(target) or ""
                status = "pass"
            except SuiteSkipped:
                raise
            except Exception as exc:  # noqa: BLE001 - a failing check is a result, not a crash
                status = "fail"
                tail = traceback.format_exception_only(type(exc), exc)[-1].strip()
                detail = scrub_text(tail)[:500]
            results.append(
                {
                    "name": check.name,
                    "status": status,
                    "capabilities": [str(c) for c in check.capabilities],
                    "detail": detail,
                }
            )
            for cap in check.capabilities:
                passed[str(cap)] = passed.get(str(cap), True) and status == "pass"
    outcome = "pass" if all(r["status"] == "pass" for r in results) else "fail"
    validated = tuple(
        sorted(
            c for c, ok in passed.items() if ok and c in {str(x) for x in descriptor.capabilities}
        )
    )
    return ConformanceRun(
        adapter_key=suite.adapter_key,
        adapter_version=descriptor.version,
        suite_version=suite.suite_version,
        started_at=started,
        finished_at=utcnow(),
        outcome=outcome,
        validated_capabilities=validated if outcome == "pass" else (),
        checks=results,
        environment=suite.environment(),
    )
