"""Managed bundle source: scaffold, validation and the trusted deployment bundle.

A source is either the SDK template (`scaffold_files`) or a generated app (SP5 6.6): the app
`shifde.appgen.render` produces, recognized by its `app/app.yaml` (`GENERATED_APP_YAML`, pinned to
`shifde/appgen/templates/app.yaml.txt` by a test, since this package may not import `appgen`).
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any  # Any: YAML documents

import yaml

from shifde.adapters.native.contracts import DatabricksApplicationSpec

ADAPTER_VERSION = "0.1.0"
CLI_VERSION = "1.25.0"
# `shifde/appgen/templates/app.yaml.txt`: the template's runtime plus the serving endpoint
GENERATED_APP_YAML = b"""command: ["python", "app.py"]
env:
  - name: DATABRICKS_WAREHOUSE_ID
    valueFrom: sql-warehouse
  - name: SERVING_ENDPOINT
    valueFrom: serving-endpoint
"""
GENERATED_FILES = (
    "app/app.py",
    "app/agent_loop.py",
    "app/backend_databricks.py",
    "app/requirements.txt",
    "app/config/agent.json",
    "app/config/tools.json",
    "app/config/screens.json",
    "app/config/limits.json",
)


def prefix(spec: DatabricksApplicationSpec) -> str:
    return "" if spec.bundle_root == "." else spec.bundle_root + "/"


def source_bundle(spec: DatabricksApplicationSpec) -> dict[str, Any]:
    return {
        "bundle": {"name": "shifde-app", "databricks_cli_version": f"={CLI_VERSION}"},
        "resources": {
            "apps": {
                spec.app_resource_key: {
                    "name": "shifde-app",
                    "source_code_path": "./app",
                }
            }
        },
        "targets": {spec.bundle_target: {}},
    }


def scaffold_files(spec: DatabricksApplicationSpec) -> dict[str, bytes]:
    resources = files("shifde.adapters.native.databricks").joinpath("templates")
    result = {
        "databricks.yml": yaml.safe_dump(source_bundle(spec), sort_keys=False).encode(),
        "app/app.py": resources.joinpath("app.py.txt").read_bytes(),
        "app/requirements.txt": resources.joinpath("requirements.txt").read_bytes(),
        "app/app.yaml": resources.joinpath("app.yaml").read_bytes(),
    }
    return {prefix(spec) + key: content for key, content in result.items()}


def validate_source(contents: dict[str, bytes], spec: DatabricksApplicationSpec) -> bool:
    """Refuses (`ValueError`) anything but the managed template source or a generated app's
    source; True for a generated app."""
    root = prefix(spec)
    expected = scaffold_files(spec)
    generated = contents.get(root + "app/app.yaml") == GENERATED_APP_YAML
    required = (
        {root + "databricks.yml", root + "app/app.yaml", *(root + n for n in GENERATED_FILES)}
        if generated
        else expected.keys()
    )
    if not required <= contents.keys():
        raise ValueError("source is missing required application files")
    # Never forward application-controlled hooks, includes, Python bundle code or targets.
    if yaml.safe_load(contents[root + "databricks.yml"]) != source_bundle(spec):
        raise ValueError("only the managed app-only bundle configuration is supported")
    if not generated and contents[root + "app/app.yaml"] != expected[root + "app/app.yaml"]:
        raise ValueError("the managed runtime configuration must be preserved")
    for name in contents:
        local = name.removeprefix(root)
        if name != root + "databricks.yml" and not (
            name.startswith(root) and local.startswith("app/")
        ):
            raise ValueError("source outside the managed app directory is forbidden")
        if any(part.startswith(".") for part in local.split("/")):
            raise ValueError("hidden configuration and secret files are forbidden")
    return generated


def deployment_bundle(
    spec: DatabricksApplicationSpec,
    *,
    host: str,
    app_name: str,
    root_path: str,
    warehouse_id: str,
    serving_endpoint: str | None = None,
) -> dict[str, Any]:
    """The trusted bundle apply deploys; a generated app also gets its serving endpoint
    (`CAN_QUERY`)."""
    resources: list[dict[str, Any]] = [
        {"name": "sql-warehouse", "sql_warehouse": {"id": warehouse_id, "permission": "CAN_USE"}}
    ]
    if serving_endpoint is not None:
        resources.append(
            {
                "name": "serving-endpoint",
                "serving_endpoint": {"name": serving_endpoint, "permission": "CAN_QUERY"},
            }
        )
    return {
        "bundle": {"name": app_name, "databricks_cli_version": f"={CLI_VERSION}"},
        "workspace": {"host": host, "root_path": root_path},
        "resources": {
            "apps": {
                spec.app_resource_key: {
                    "name": app_name,
                    "source_code_path": "./app",
                    "resources": resources,
                }
            }
        },
        "targets": {spec.bundle_target: {}},
    }
