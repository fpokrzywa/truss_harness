"""Canonical JSON, spec hashes and structural spec diffs (DATA_MODEL 3.1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict  # Any: arbitrary JSON documents

Op = Literal["add", "remove", "replace"]


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, non-ASCII kept (so the hash is stable)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_hash(spec: Any) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


class Change(TypedDict, total=False):
    path: str
    op: Op
    # `from` is a Python keyword; the wire name is produced by the schema layer.
    old: Any
    new: Any


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def diff(old: Any, new: Any, path: str = "") -> list[Change]:
    """JSON diff of two specs: objects key by key, arrays element by element (by index)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[Change] = []
        for key in sorted(set(old) | set(new)):
            child = _join(path, key)
            if key not in new:
                out.append({"path": child, "op": "remove", "old": old[key]})
            elif key not in old:
                out.append({"path": child, "op": "add", "new": new[key]})
            else:
                out.extend(diff(old[key], new[key], child))
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for index in range(max(len(old), len(new))):
            child = _join(path, index)
            if index >= len(new):
                out.append({"path": child, "op": "remove", "old": old[index]})
            elif index >= len(old):
                out.append({"path": child, "op": "add", "new": new[index]})
            else:
                out.extend(diff(old[index], new[index], child))
        return out
    if old == new and type(old) is type(new):
        return []
    return [{"path": path, "op": "replace", "old": old, "new": new}]
