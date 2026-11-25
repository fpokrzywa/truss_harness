"""Executor test helpers: a credentials file (Databricks, or Snowflake with its key file), the
Snowflake fake of a key, a runner over the in-process gateway and the fake platform, and the
requester's plan request."""

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
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker, snowflake_account
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


def snowflake_fake(address: str, pem: str) -> FakeSnowflakeNativePlatform:
    """A Snowflake fake that accepts `pem`'s JWTs for the target's account."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return FakeSnowflakeNativePlatform(
        environment="test",
        public_key=key.public_key(),
        account=snowflake_account(address),
        user=SNOWFLAKE_USER,
    )


def fake_platform() -> FakeDatabricksPlatform:
    return FakeDatabricksPlatform(service_principal=DEPLOYER)


def make_runner(
    client: TestClient,
    token: str,
    credentials: Path,
    fake: FakeDatabricksPlatform,
    snowflake: FakeSnowflakeNativePlatform | None = None,
) -> Runner:
    """A runner of the executor holding `token`, against `fake` (and the Snowflake fake), over the
    in-process gateway."""
    gateway = GatewayClient("https://testserver", token, http=client)
    me = gateway.whoami()
    return Runner(
        gateway,
        CredentialBroker.from_file(credentials),
        me.executor_id,
        FakePlatform(fake, snowflake=snowflake, environment="test"),
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
