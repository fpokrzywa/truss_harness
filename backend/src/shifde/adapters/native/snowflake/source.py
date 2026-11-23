"""Snowflake Native App source rules, identifiers and the pinned Snowflake CLI (SP6 design 6.2).

The Snowflake CLI is pinned here (`CLI_VERSION`), in the same way SP2 pins the Databricks CLI in
`adapters/native/databricks/source.py`. The installable pin and its hashed lock are
`tools/snowflake-cli.in` and `tools/snowflake-cli.txt`: the CLI runs from its own virtual
environment made from that lock, never from the backend's environment. The source is always
rendered (no templates, no scaffold); only its shape is checked here.
"""

from __future__ import annotations

import re

from shifde.adapters.native.contracts import Target, relative_path

ADAPTER_VERSION = "0.1.0"
CLI_VERSION = "3.33.0"
TERMINAL_STEP = "application-access"
PATCH_LIMIT = 130  # patches 0..129 of one version (Call S6-J)
VERSION = "V1"
SCHEMA = "SHIFDE"
STAGE = "SOURCE"
REQUIRED_FILES = ("manifest.yml", "setup.sql", "ui/environment.yml")
# Snowflake CLI project, local-override and configuration files: none may reach the CLI.
CLI_PROJECT_FILES = frozenset({"snowflake.yml", "snowflake.local.yml", "config.toml"})
BINDINGS = ("package", "application", "warehouse")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")


def identifier(value: str) -> str:
    """`value` uppercased when it is a plain Snowflake identifier, else `ValueError`."""
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid Snowflake native identifier")
    return value.upper()


def quoted(value: str) -> str:
    return '"' + identifier(value) + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_source(files: dict[str, bytes]) -> None:
    """The package needs `manifest.yml`, `setup.sql` and `ui/environment.yml`; hidden files,
    paths outside the package root and CLI project or hook files are refused."""
    if not set(REQUIRED_FILES) <= files.keys():
        raise ValueError("source is missing required Native App files")
    for name in files:
        relative_path(name)
        parts = name.split("/")
        if any(part.startswith(".") for part in parts):
            raise ValueError("hidden configuration and secret files are forbidden")
        if parts[-1].lower() in CLI_PROJECT_FILES:
            raise ValueError("Snowflake CLI project and hook files are forbidden")


def names_for(target: Target) -> dict[str, str]:
    """The package, application and warehouse names: from the target bindings only."""
    names = {}
    for key in BINDINGS:
        value = target.bindings.get(key)
        if not value:
            raise ValueError(f"required {key} binding is missing")
        names[key] = identifier(value)
    return names


def stage_name(package: str) -> str:
    """`<PKG>.SHIFDE.SOURCE`, the package's source stage."""
    return f"{identifier(package)}.{SCHEMA}.{STAGE}"


def stage_location(package: str, digest: str) -> str:
    """`@"<PKG>"."SHIFDE"."SOURCE"/<digest>/`, one release's directory on the stage."""
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid release digest")
    return f'@{quoted(package)}."{SCHEMA}"."{STAGE}"/{digest}/'
