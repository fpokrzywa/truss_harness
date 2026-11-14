"""Process-wide integration wiring: settings, adapter registry, DNS resolver, test transports."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from shifde.adapters.registry import AdapterRegistry
from shifde.core.egress import Resolver, default_resolver
from shifde.core.settings import Settings


@dataclass(frozen=True)
class IntegrationRuntime:
    settings: Settings
    registry: AdapterRegistry
    resolver: Resolver = default_resolver
    inner: httpx.BaseTransport | None = None  # test hook: replaces the real network transport
    inner_async: httpx.AsyncBaseTransport | None = None
