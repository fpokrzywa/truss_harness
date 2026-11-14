"""Derive capability maturity from evidence (FR-VND-004/005). Nothing here is settable by an admin.

* ``planned``      the adapter is declared but has no implementation.
* ``experimental`` implemented, but no passing conformance evidence for this capability on the
                   current adapter version (never run, run failed, or the adapter changed since).
* ``validated``    the latest recorded run passed on this adapter version and covers the capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from shifde.adapters.base import AdapterDescriptor
from shifde.adapters.evidence import ConformanceRun

Maturity = Literal["planned", "experimental", "validated"]


@dataclass(frozen=True)
class EvidenceRef:
    run_id: UUID
    finished_at: datetime
    suite_version: str


@dataclass(frozen=True)
class CapabilityMaturity:
    capability: str
    maturity: Maturity
    evidence: EvidenceRef | None = None


def capability_maturity(
    descriptor: AdapterDescriptor, capability: str, latest: ConformanceRun | None
) -> CapabilityMaturity:
    if not descriptor.implemented:
        return CapabilityMaturity(capability, "planned")
    if (
        latest is not None
        and latest.outcome == "pass"
        and latest.adapter_version == descriptor.version
        and capability in latest.validated_capabilities
    ):
        return CapabilityMaturity(
            capability,
            "validated",
            EvidenceRef(latest.id, latest.finished_at, latest.suite_version),
        )
    return CapabilityMaturity(capability, "experimental")


def adapter_capabilities(
    descriptor: AdapterDescriptor, latest: ConformanceRun | None
) -> list[CapabilityMaturity]:
    return [
        capability_maturity(descriptor, str(c), latest)
        for c in sorted(descriptor.capabilities, key=str)
    ]
