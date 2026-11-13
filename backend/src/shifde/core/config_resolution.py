"""Layered configuration resolution (FR-TEN-008).

Precedence, lowest to highest: installation_settings -> tenant.settings -> workspace.settings ->
project.settings. Merge rules (deterministic):

* mapping + mapping  -> merged recursively, key by key;
* anything else      -> the higher layer's value replaces the lower one wholesale
  (lists are NOT concatenated; an explicit ``None`` overrides to ``None``);
* a missing layer (``None``) contributes nothing.

Inputs are never mutated; results are deep copies.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any  # Any: settings are arbitrary JSON documents

LAYER_ORDER = ("installation", "tenant", "workspace", "project")

JsonDict = dict[str, Any]


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> JsonDict:
    result: JsonDict = copy.deepcopy(dict(base))
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_config(
    installation: Mapping[str, Any] | None = None,
    tenant: Mapping[str, Any] | None = None,
    workspace: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> JsonDict:
    merged: JsonDict = {}
    for layer in (installation, tenant, workspace, project):
        if layer:
            merged = deep_merge(merged, layer)
    return merged


@dataclass(frozen=True)
class ResolvedValue:
    value: Any
    source: str  # one of LAYER_ORDER


def resolve_with_sources(
    installation: Mapping[str, Any] | None = None,
    tenant: Mapping[str, Any] | None = None,
    workspace: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> dict[str, ResolvedValue]:
    """Flattened ``dotted.path -> (value, winning layer)`` view for diagnostics/UI."""
    out: dict[str, ResolvedValue] = {}

    def walk(prefix: str, node: Mapping[str, Any], layer: str) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, Mapping) and value:
                # A mapping overriding a scalar replaces it; drop stale scalar entry.
                out.pop(path, None)
                walk(path + ".", value, layer)
            else:
                # Remove any deeper keys from lower layers that this scalar/empty map replaces.
                for stale in [k for k in out if k.startswith(path + ".")]:
                    del out[stale]
                out[path] = ResolvedValue(copy.deepcopy(value), layer)

    for name, layer_value in zip(
        LAYER_ORDER, (installation, tenant, workspace, project), strict=True
    ):
        if layer_value:
            walk("", layer_value, name)
    return out
