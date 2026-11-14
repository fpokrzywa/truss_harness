"""Feature registry: which product areas this running backend actually implements (FR-UI-003).

Each module registers its key when its router is mounted in `app.py`; clients (the UI) hide or
label areas that are not advertised, so no page pretends a missing backend exists. The registry
is per application instance (no module-level state) and nothing here lists phases.
"""

from __future__ import annotations


class FeatureRegistry:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def register(self, key: str) -> None:
        if not key or not key.replace("_", "").isalnum() or key != key.lower():
            raise ValueError(f"invalid feature key: {key!r}")
        self._keys.add(key)

    def keys(self) -> list[str]:
        return sorted(self._keys)
