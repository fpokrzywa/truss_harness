"""Host suffix lists of the installation (`SHIFDE_DELIVERY_*_HOST_SUFFIXES`): the format of an
entry, and the Palantir list's nesting rule.

Pure string handling: no settings, no environment. `core.settings` (API and worker) and the
executor, which never loads the settings, apply the same rules. A refusal names the variable and
never echoes an entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PALANTIR_HOST_SUFFIX = "palantirfoundry.com"  # built in: never configured

_HOST_SUFFIX = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+")


def split_suffixes(value: str) -> list[str]:
    """The comma-separated entries of `value`, stripped and lowercased; empty ones dropped."""
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def checked_suffixes(entries: Iterable[str], variable: str) -> list[str]:
    """`entries` stripped and lowercased; `ValueError` unless each is a bare domain of two or
    more labels."""
    suffixes = [entry.strip().lower() for entry in entries]
    if not all(_HOST_SUFFIX.fullmatch(entry) for entry in suffixes):
        raise ValueError(
            f"{variable} entries must be bare domains of two or more letter, digit or hyphen labels"
        )
    return suffixes


def checked_palantir_suffixes(entries: Iterable[str], variable: str) -> list[str]:
    """`checked_suffixes`, and `ValueError` for an entry below another entry or below the built-in
    `palantirfoundry.com`, at any depth: one stack would get two claim keys, and two organizations
    could each claim it."""
    suffixes = checked_suffixes(entries, variable)
    above = (PALANTIR_HOST_SUFFIX, *suffixes)
    if any(entry.endswith("." + suffix) for entry in suffixes for suffix in above):
        raise ValueError(
            f"{variable} entries must not be below another entry or below {PALANTIR_HOST_SUFFIX}"
        )
    return suffixes
