"""A stateful in-memory Databricks platform for tests and demos (no real workspace exists).

`FakeDatabricksPlatform` is the `BundleRunner`, and `platform.workspace` is the
`workspace_factory`. Apps are kept per workspace host (`apps[host][name]`); a `DatabricksAdapter`
built over the fake reads and writes that state, so a new adapter over the same fake models a
restart. The CLI deploys as `service_principal` and, like Databricks, may only write under its
home folder. Fault switches are off by default.

A deployed generated app (one with `app/config/agent.json`) is recorded with its release digest,
generator version and tables, and the bundle's warehouse and `serving-endpoint` resources
(`releases[host][name]`); deep health answers from that record in the SP5 6.4 shape, never by
running the app. `deep_health_bad` fails its first table check (the warehouse check when it has no
table).
"""

from __future__ import annotations

import copy
import json
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
        app = self.platform.apps.get(self.target.host, {}).get(name)
        return copy.deepcopy(app) if app is not None else None

    def probe(self, warehouse_id: str) -> None:
        if warehouse_id not in self.platform.warehouses:
            raise LookupError("unknown SQL warehouse")

    def health_check(self, url: str) -> dict[str, Any]:
        return dict(UNHEALTHY_WAREHOUSE if self.platform.health_bad else HEALTHY)

    def deep_health(self, url: str) -> tuple[int, dict[str, Any]]:
        apps = self.platform.apps.get(self.target.host, {})
        name = next((n for n, app in apps.items() if app.get("url") == url), None)
        release = self.platform.releases.get(self.target.host, {}).get(name or "")
        if release is None:
            raise RuntimeError("the app answered 404")
        checks = [
            {
                "name": "warehouse",
                "passed": release["warehouse_id"] in self.platform.warehouses,
                "reason": "the warehouse is not reachable",
            },
            {
                "name": "serving_endpoint",
                "passed": release["bundle_endpoint"] is not None
                and release["bundle_endpoint"] == release["serving_endpoint"],
                "reason": "the serving endpoint is not bound to the app",
            },
            *(
                {"name": f"table:{t}", "passed": True, "reason": f"the app cannot read {t}"}
                for t in release["tables"]
            ),
        ]
        if self.platform.deep_health_bad:
            checks[2 if len(checks) > 2 else 0]["passed"] = False
        good = all(c["passed"] for c in checks)
        return (200 if good else 503), {
            "status": "ok" if good else "failed",
            "generator_version": release["generator_version"],
            "release_digest": release["release_digest"],
            "checks": [
                {
                    "name": c["name"],
                    "status": "passed" if c["passed"] else "failed",
                    "reason": None if c["passed"] else c["reason"],
                }
                for c in checks
            ],
        }


class FakeDatabricksPlatform:
    def __init__(
        self,
        *,
        service_principal: str,
        warehouses: set[str] | None = None,
        hang_timeout: float = 60.0,
    ) -> None:
        self.service_principal = service_principal
        self.warehouses: set[str] = set(warehouses or ())
        self.hang_timeout = hang_timeout
        self.apps: dict[str, dict[str, dict[str, Any]]] = {}
        self.releases: dict[str, dict[str, dict[str, Any]]] = {}
        self.actions: list[tuple[str, str | None, str]] = []
        self.fail_after_accept = False
        self.crash_app = False
        self.no_new_deployment = False
        self.health_bad = False
        self.deep_health_bad = False
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
        if not root_path.startswith(f"/Workspace/Users/{self.service_principal}/"):
            # The principal has no write access outside its home folder.
            raise RuntimeError("Databricks CLI command failed with exit code 1")
        apps = config["resources"]["apps"]
        resource = apps[app_key] if app_key else next(iter(apps.values()))
        name = str(resource["name"])
        workspace = self.apps.setdefault(host, {})
        if action == "deploy":
            app = workspace.get(name)
            if app is None:
                app = workspace[name] = {
                    "name": name,
                    "id": self._next("native-app"),
                    "url": f"https://{name}.databricksapps.com",
                    "app_status": {"state": "RUNNING"},
                }
            app["source_code_path"] = f"{root_path}/files/app"
            self._record_release(host, name, directory, resource)
            if self.fail_after_accept:
                raise TimeoutError("connection lost after native acceptance")
        elif action == "run":
            app = workspace.get(name)
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

    def _record_release(
        self, host: str, name: str, directory: Path, resource: dict[str, Any]
    ) -> None:
        """The deployed generated app's release and bound resources (none for the template)."""
        releases = self.releases.setdefault(host, {})
        agent = directory / "app" / "config" / "agent.json"
        if not agent.is_file():
            releases.pop(name, None)
            return
        config = json.loads(agent.read_text(encoding="utf-8"))
        bound = {item["name"]: item for item in resource.get("resources") or []}
        releases[name] = {
            "release_digest": config.get("release_digest"),
            "generator_version": config.get("generator_version"),
            "tables": list(config.get("tables") or []),
            "serving_endpoint": config.get("serving_endpoint"),
            "warehouse_id": (bound.get("sql-warehouse") or {}).get("sql_warehouse", {}).get("id"),
            "bundle_endpoint": (bound.get("serving-endpoint") or {})
            .get("serving_endpoint", {})
            .get("name"),
        }
