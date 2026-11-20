"""Canonical JSON of packaging documents (solution version specs, release manifests) and its
sha256 (sub-project 3 design, settled call 12).

Sorted keys, `(",", ":")` separators, UTF-8 without escaping, no NaN. Floats are refused: money
and ratios travel as decimal strings, so one value has one encoding. `build/canonical.py` is
separate and unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any  # Any: JSON values


def _refuse_floats(value: Any) -> None:  # Any: a JSON value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical JSON; use a decimal string")
    if isinstance(value, dict):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _refuse_floats(item)


def canonical(value: Any) -> bytes:  # Any: a JSON value
    """The canonical UTF-8 JSON encoding of `value`."""
    _refuse_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:  # Any: a JSON value
    """sha256 hex of the canonical JSON of `value`."""
    return hashlib.sha256(canonical(value)).hexdigest()
