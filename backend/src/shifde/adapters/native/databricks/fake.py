"""A stateful in-memory Databricks platform for tests and demos (no real workspace exists).

`FakeDatabricksPlatform` is the `BundleRunner`, and `platform.workspace` is the
`workspace_factory`: a `DatabricksAdapter` built over it reads and writes one shared app state, so
a new adapter over the same fake models a restart. Fault switches are off by default.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any  # Any: Databricks app JSON

import yaml

from shifde.adapters.native.contracts import Target
from shifde.adapters.native.databricks.source import CLI_VERSION

HEALTHY: dict[str, Any] = {"status": "ok", "warehouse": "ok"}
UNHEALTHY_WAREHOUSE: dict[str, Any] = {"status": "ok", "warehouse": "failed"}


class FakeWorkspaceGateway:
    """The `WorkspaceGateway` the fake platform hands out for one target."""

    def __init__(self, platform: FakeDatabricksPlatform, target: Target) -> None:
        self.platform, self.target = platform, target

    def get_app(self, name: str) -> dict[str, Any] | None:
        app = self.platform.apps.get(name)
        return copy.deepcopy(app) if app is not None else None

    def probe(self, warehouse_id: str) -> None:
        if warehouse_id not in self.platform.warehouses:
            raise LookupError("unknown SQL warehouse")

    def health_check(self, url: str) -> dict[str, Any]:
        return dict(UNHEALTHY_WAREHOUSE if self.platform.health_bad else HEALTHY)


class FakeDatabricksPlatform:
    def __init__(self, *, warehouses: set[str] | None = None, hang_timeout: float = 60.0) -> None:
        self.warehouses: set[str] = set(warehouses or ())
        self.hang_timeout = hang_timeout
        self.apps: dict[str, dict[str, Any]] = {}
        self.actions: list[tuple[str, str | None, str]] = []
        self.fail_after_accept = False
        self.crash_app = False
        self.no_new_deployment = False
        self.health_bad = False
        self.hang = False
        self._released = threading.Event()
        self._counter = 0

    def workspace(self, target: Target) -> FakeWorkspaceGateway:
        return FakeWorkspaceGateway(self, target)

    def release(self) -> None:
        """Unblock a `hang`ing write (it still raises `TimeoutError`: accepted, unconfirmed)."""
        self._released.set()

    def _next(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-{self._counter}"

    def check_version(self) -> str:
        return CLI_VERSION

    def execute(
        self, action: str, directory: Path, *, target: str, host: str, app_key: str | None = None
    ) -> dict[str, Any]:
        if action not in ("deploy", "run", "validate") or (action == "run" and not app_key):
            raise ValueError("unsupported bundle operation")
        config = yaml.safe_load((directory / "databricks.yml").read_text(encoding="utf-8"))
        root_path = str(config["workspace"]["root_path"])
        self.actions.append((action, app_key, root_path))
        apps = config["resources"]["apps"]
        resource = apps[app_key] if app_key else next(iter(apps.values()))
        name = str(resource["name"])
        if action == "deploy":
            app = self.apps.get(name)
            if app is None:
                app = self.apps[name] = {
                    "name": name,
                    "id": self._next("native-app"),
                    "url": f"https://{name}.databricksapps.com",
                    "app_status": {"state": "RUNNING"},
                }
            app["source_code_path"] = f"{root_path}/files/app"
            if self.fail_after_accept:
                raise TimeoutError("connection lost after native acceptance")
        elif action == "run":
            app = self.apps.get(name)
            if app is None:
                raise RuntimeError("Databricks CLI command failed with exit code 1")
            if not self.no_new_deployment:
                app["active_deployment"] = {
                    "deployment_id": self._next("deployment"),
                    "status": {"state": "SUCCEEDED"},
                }
            app["app_status"] = {"state": "CRASHED" if self.crash_app else "RUNNING"}
        if self.hang:
            self._released.wait(self.hang_timeout)
            raise TimeoutError("native write accepted but never confirmed")
        return {}
