"""Executor test helpers: a credentials file (Databricks, Snowflake with its key file, or Fabric,
Palantir or Docker with its secret file), a runner over the in-process gateway and the fake
platform, and the requester's plan request."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any  # Any: JSON bodies
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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
    tenant: UUID,
    ref: str = "databricks-dev",
    token: str = CREDENTIAL_TOKEN,
    principal: str = DEPLOYER,
    name: str = "credentials.json",
) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                ref: {
                    "tenant_id": str(tenant),
                    "host": host,
                    "token": token,
                    "service_principal": principal,
                }
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)  # the broker refuses a file open to group or others on POSIX
    return path


SNOWFLAKE_USER, SNOWFLAKE_ROLE = "deployer", "DEPLOY_ROLE"


def snowflake_key_pem() -> str:
    """A new RSA 2048 key as an unencrypted PKCS#8 PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def snowflake_credentials_file(
    directory: Path,
    host: str,
    pem: str,
    *,
    tenant: UUID,
    ref: str = "snowflake-dev",
    name: str = "credentials.json",
) -> Path:
    """A credentials file of one Snowflake entry and its key file `<name>.p8`, both mode 0600."""
    key_file = directory / f"{name}.p8"
    key_file.write_text(pem, encoding="utf-8")
    os.chmod(key_file, 0o600)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                ref: {
                    "tenant_id": str(tenant),
                    "host": host,
                    "platform": "snowflake",
                    "user": SNOWFLAKE_USER,
                    "role": SNOWFLAKE_ROLE,
                    "private_key_file": str(key_file),
                }
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


SP8_ENTRA_TENANT = "0a1b2c3d-0000-4000-8000-0000000000e1"
SP8_CLIENT_ID = "0a1b2c3d-0000-4000-8000-0000000000c1"
SP8_SECRET = "client-secret-never-logged"  # noqa: S105 - a test value


def sp8_entry(directory: Path, kind: str, host: str, *, tenant: UUID) -> dict[str, Any]:
    """A `kind` (fabric, palantir or docker) credentials entry; a Fabric or Palantir entry's
    secret file `<kind>.json.secret` is written too, mode 0600."""
    entry: dict[str, Any] = {"tenant_id": str(tenant), "host": host, "platform": kind}
    if kind == "docker":
        return entry
    secret_file = directory / f"{kind}.json.secret"
    secret_file.write_text(SP8_SECRET, encoding="utf-8")
    os.chmod(secret_file, 0o600)
    if kind == "fabric":
        entry["entra_tenant_id"] = SP8_ENTRA_TENANT
    return {**entry, "client_id": SP8_CLIENT_ID, "client_secret_file": str(secret_file)}


def sp8_credentials_file(
    directory: Path, kind: str, host: str, *, tenant: UUID, ref: str | None = None
) -> Path:
    """A credentials file of one `sp8_entry` (reference `<kind>-dev` unless given), mode 0600."""
    path = directory / f"{kind}.json"
    entry = sp8_entry(directory, kind, host, tenant=tenant)
    path.write_text(json.dumps({ref or f"{kind}-dev": entry}), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def fake_platform() -> FakeDatabricksPlatform:
    return FakeDatabricksPlatform(service_principal=DEPLOYER)


def make_runner(
    client: TestClient, token: str, credentials: Path, fake: FakeDatabricksPlatform
) -> Runner:
    """A runner of the executor holding `token`, against `fake`, over the in-process gateway."""
    gateway = GatewayClient("https://testserver", token, http=client)
    me = gateway.whoami()
    return Runner(
        gateway,
        CredentialBroker.from_file(credentials),
        me.executor_id,
        FakePlatform(fake),
        tenant_id=me.tenant_id,
    )


def ask_plan(
    flow: Flow,
    target_id: object,
    application_key: str = "sales-app",
    release_id: object | None = None,
) -> Any:
    """The requester's plan request (a `PlanOut` body) of `release_id`, or of a new sealed,
    passed release in the target's scope; the client is signed out afterwards."""
    release = release_id or flow.release(target_id)
    h = as_user(flow.client, flow.requester)
    made = request_plan(
        flow.client, h, flow.slug, target_id, release, application_key=application_key
    )
    flow.client.cookies.clear()
    return made


def deploy_actions(fake: FakeDatabricksPlatform, action: str) -> int:
    return sum(1 for recorded, _, _ in fake.actions if recorded == action)
