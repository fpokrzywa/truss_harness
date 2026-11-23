"""Adapter registry: entry-point group ``shifde.adapters`` (FR-VND-006).

Third parties declare an adapter class in their package metadata::

    [project.entry-points."shifde.adapters"]
    acme_crm = "acme_shifde.crm:AcmeCrmAdapter"

The class exposes ``descriptor: AdapterDescriptor`` and the adapter methods; an optional
``conformance_suite()`` classmethod returns the suite `shifde-admin adapters verify` runs. The
built-ins are declared the same way in this project's own ``pyproject.toml``; ``BUILTIN`` is only a
safety net for a stale editable install and must stay in sync with it (a test checks both).
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from shifde.adapters.base import AdapterDescriptor

if TYPE_CHECKING:
    from shifde.adapters.conformance import ConformanceSuite

ENTRY_POINT_GROUP = "shifde.adapters"
BUILTIN: dict[str, str] = {
    "rest_http": "shifde.adapters.rest_http:RestHttpAdapter",
    "mcp_http": "shifde.adapters.mcp_http:McpHttpAdapter",
    "postgres": "shifde.adapters.postgres:PostgresAdapter",
    "fake": "shifde.adapters.model_providers:FakeProvider",
    "openai_compatible": "shifde.adapters.model_providers:OpenAICompatibleProvider",
    "anthropic": "shifde.adapters.model_providers:AnthropicProvider",
    "databricks": "shifde.adapters.dataplatforms.databricks:DatabricksAdapter",
    "snowflake": "shifde.adapters.planned:SnowflakePlanned",
    "fabric": "shifde.adapters.planned:FabricPlanned",
}


def _load_object(spec: str) -> Any:  # Any: the adapter class
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)


class AdapterRegistry:
    def __init__(self, classes: list[Any]) -> None:  # Any: adapter classes
        self._classes: dict[str, Any] = {}
        for cls in classes:
            descriptor = getattr(cls, "descriptor", None)
            if not isinstance(descriptor, AdapterDescriptor):
                raise ValueError(f"{cls!r} has no AdapterDescriptor")
            if descriptor.key in self._classes:
                raise ValueError(f"duplicate adapter key {descriptor.key!r}")
            self._classes[descriptor.key] = cls

    @classmethod
    def load(cls, include_fake: bool) -> AdapterRegistry:
        """Entry points plus `BUILTIN`. `include_fake=False` (production, design SP4 4) leaves the
        `fake` model provider out, whichever source declares it."""
        found: dict[str, Any] = {}
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            if ep.name == "fake" and not include_fake:
                continue
            found[ep.name] = ep.load()
        for name, spec in BUILTIN.items():
            if name == "fake" and not include_fake:
                continue
            found.setdefault(name, _load_object(spec))
        return cls(list(found.values()))

    def descriptors(self) -> list[AdapterDescriptor]:
        return sorted((c.descriptor for c in self._classes.values()), key=lambda d: d.key)

    def descriptor(self, key: str) -> AdapterDescriptor | None:
        cls = self._classes.get(key)
        return cls.descriptor if cls else None

    def get(self, key: str) -> Any | None:  # Any: an adapter instance (protocol depends on kind)
        cls = self._classes.get(key)
        return cls() if cls else None

    def suite_for(self, key: str) -> ConformanceSuite | None:
        from shifde.adapters.suites import SUITES

        if key in SUITES:
            return SUITES[key]()
        factory = getattr(self._classes.get(key), "conformance_suite", None)
        return factory() if callable(factory) else None

    def keys(self) -> list[str]:
        return sorted(self._classes)
