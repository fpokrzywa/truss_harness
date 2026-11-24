"""Databricks workspace reads (plain REST over httpx) and the pinned CLI, the only write path."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol  # Any: decoded JSON bodies
from urllib.parse import quote, urlsplit

import httpx

from shifde.adapters.native.contracts import Target
from shifde.adapters.native.databricks.source import CLI_VERSION

TIMEOUT = httpx.Timeout(30, connect=5)


class WorkspaceGateway(Protocol):
    def get_app(self, name: str) -> dict[str, Any] | None: ...
    def probe(self, warehouse_id: str) -> None: ...
    def health_check(self, url: str) -> dict[str, Any]: ...
    def deep_health(self, url: str) -> tuple[int, dict[str, Any]]: ...


class BundleRunner(Protocol):
    def check_version(self) -> str: ...
    def execute(
        self, action: str, directory: Path, *, target: str, host: str, app_key: str | None = None
    ) -> dict[str, Any]: ...


def check_app_origin(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not parsed.hostname.endswith(".databricksapps.com")
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("refusing to send credentials to an unrecognized application origin")


class DatabricksWorkspaceGateway:
    """Read-only workspace calls against `target.host` with a bearer token from `token_provider`."""

    def __init__(
        self,
        target: Target,
        *,
        token_provider: Callable[[str], str],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.target, self.token_provider, self.transport = target, token_provider, transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=self.transport,
            headers={"Authorization": f"Bearer {self.token_provider(self.target.host)}"},
        )

    def get_app(self, name: str) -> dict[str, Any] | None:
        with self._client() as client:
            response = client.get(f"{self.target.host}/api/2.0/apps/{quote(name, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected Databricks app response")
        return body

    def probe(self, warehouse_id: str) -> None:
        with self._client() as client:
            client.get(
                f"{self.target.host}/api/2.0/sql/warehouses/{quote(warehouse_id, safe='')}"
            ).raise_for_status()
            client.get(
                f"{self.target.host}/api/2.0/apps", params={"page_size": 1}
            ).raise_for_status()

    def health_check(self, url: str) -> dict[str, Any]:
        check_app_origin(url)
        with self._client() as client:
            response = client.get(url.rstrip("/") + "/health")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected health response")
        return body

    def deep_health(self, url: str) -> tuple[int, dict[str, Any]]:
        """A generated app's `GET /health?deep=1`: its status (200 or 503) and JSON object."""
        check_app_origin(url)
        with self._client() as client:
            response = client.get(url.rstrip("/") + "/health", params={"deep": "1"})
        if response.status_code not in (200, 503):
            response.raise_for_status()
            raise ValueError("unexpected deep health status")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected deep health response")
        return response.status_code, body


class DatabricksCLI:
    """The pinned Databricks CLI in a minimal environment. Each call runs it exactly once."""

    def __init__(
        self,
        executable: str = "databricks",
        *,
        token_provider: Callable[[str], str],
        timeout: float = 900,
    ) -> None:
        self.executable, self.token_provider, self.timeout = executable, token_provider, timeout

    def _run(self, args: list[str], directory: Path | None, env: dict[str, str]) -> bytes:
        executable = shutil.which(self.executable)
        if not executable:
            raise FileNotFoundError(
                "install the pinned Databricks CLI or provide its executable path"
            )
        try:
            process = subprocess.run(
                [executable, *args],
                cwd=directory,
                env=env,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Databricks CLI command timed out") from None
        if process.returncode:
            # Raw CLI output may hold native metadata or credentials; never return it to an agent.
            raise RuntimeError(f"Databricks CLI command failed with exit code {process.returncode}")
        return process.stdout

    @staticmethod
    def _base_env() -> dict[str, str]:
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def check_version(self) -> str:
        output = self._run(["--version"], None, self._base_env())
        match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", output.decode())
        if not match or match.group(1) != CLI_VERSION:
            raise ValueError(f"Databricks CLI {CLI_VERSION} is required")
        return match.group(1)

    def execute(
        self, action: str, directory: Path, *, target: str, host: str, app_key: str | None = None
    ) -> dict[str, Any]:
        if action not in ("deploy", "run", "validate") or (action == "run" and not app_key):
            raise ValueError("unsupported bundle operation")
        self.check_version()
        env = self._base_env()
        env.update(
            DATABRICKS_HOST=host,
            DATABRICKS_TOKEN=self.token_provider(host),
            DATABRICKS_AUTH_TYPE="pat",
        )
        args = ["bundle", action]
        if action == "run":
            args.append(app_key or "")
        args += ["--target", target, "--output", "json"]
        output = self._run(args, directory, env)
        # Deployment output varies by command/version. Native REST reads are authoritative.
        try:
            result = json.loads(output)
            return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
