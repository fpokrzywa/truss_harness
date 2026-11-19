"""Data classification scale (ARCH M11, design 5.3): `public < internal < confidential <
restricted`. One scale for ontology property classification and model provider trust; a missing
or invalid level is `public`, the lowest (fail closed for a trust level)."""

from __future__ import annotations

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
LOWEST = CLASSIFICATIONS[0]


def level(value: object) -> str:
    """`value` when it is a level of the scale, else `public`."""
    return value if isinstance(value, str) and value in CLASSIFICATIONS else LOWEST


def rank(value: object) -> int:
    """Position of `level(value)` on the scale (0 = public)."""
    return CLASSIFICATIONS.index(level(value))


def highest(*values: object) -> str:
    """The highest level among `values` (`public` for none)."""
    return max((level(v) for v in values), key=rank, default=LOWEST)
