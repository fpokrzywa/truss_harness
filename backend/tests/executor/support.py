"""Executor test helpers: a credentials file, a runner over the in-process gateway and the fake
platform, and the requester's plan request."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any  # Any: JSON bodies

from fastapi.testclient import TestClient

from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker
from shifde.executor.runner import FakePlatform, Runner
from tests.connections.routes import as_user
from tests.delivery.routes import Flow, request_plan

DEPLOYER = "deployer@example.com"
CREDENTIAL_TOKEN = "dapi-credential-token-never-logged"  # noqa: S105 - a test value


def credentials_file(
    directory: Path,
    host: str,
    *,
    ref: str = "databricks-dev",
    token: str = CREDENTIAL_TOKEN,
    principal: str = DEPLOYER,
    name: str = "credentials.json",
) -> Path:
    path = directory / name
    path.write_text(
        json.dumps({ref: {"host": host, "token": token, "service_principal": principal}}),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)  # the broker refuses a file open to group or others on POSIX
    return path


def fake_platform() -> FakeDatabricksPlatform:
    return FakeDatabricksPlatform(service_principal=DEPLOYER)


def make_runner(
    client: TestClient, token: str, credentials: Path, fake: FakeDatabricksPlatform
) -> Runner:
    """A runner of the executor holding `token`, against `fake`, over the in-process gateway."""
    gateway = GatewayClient("https://testserver", token, http=client)
    return Runner(
        gateway,
        CredentialBroker.from_file(credentials),
        gateway.whoami().executor_id,
        FakePlatform(fake),
    )


def ask_plan(flow: Flow, target_id: object, application_key: str = "sales-app") -> Any:
    """The requester's plan request (a `PlanOut` body); the client is signed out afterwards."""
    h = as_user(flow.client, flow.requester)
    made = request_plan(flow.client, h, flow.slug, target_id, application_key=application_key)
    flow.client.cookies.clear()
    return made


def deploy_actions(fake: FakeDatabricksPlatform, action: str) -> int:
    return sum(1 for recorded, _, _ in fake.actions if recorded == action)
