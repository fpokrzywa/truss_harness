"""Shared request-body base: strict fields and storable (plain, finite) JSON values.

PostgreSQL cannot store NUL characters (text and jsonb) or lone UTF-16 surrogates, and NaN /
Infinity are not JSON. Every JSON request body of the platform derives from `Body` so such input
is a 422 at the boundary instead of a database error (P3/P4 review lesson).
"""

from __future__ import annotations

import math
from typing import Any, Self  # Any: arbitrary request JSON

from pydantic import BaseModel, ConfigDict, model_validator

Strict = ConfigDict(extra="forbid")


def plain_json(value: Any, path: str = "") -> None:  # Any: arbitrary request JSON
    """Refuse what PostgreSQL cannot store: NUL characters, lone surrogates, non-finite numbers."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{path or 'value'}: NUL characters are not allowed")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{path or 'value'}: invalid Unicode (lone surrogate)") from None
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path or 'value'}: NaN and Infinity are not valid JSON numbers")
    elif isinstance(value, dict):
        for key, item in value.items():
            plain_json(str(key), f"{path}.<key>")
            plain_json(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            plain_json(item, f"{path}[{i}]")


class Body(BaseModel):
    """Base of every request body: strict fields and storable (plain, finite) JSON values."""

    model_config = Strict

    @model_validator(mode="after")
    def _storable(self) -> Self:
        plain_json(self.model_dump(mode="python"))
        return self
