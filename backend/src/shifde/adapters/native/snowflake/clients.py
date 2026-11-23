"""Snowflake SQL API v2 statements over httpx and the pinned CLI's hook-free stage copy (Call S6-C).

Every statement carries a freshly signed key-pair JWT (no cache) and goes only to the target's
`*.snowflakecomputing.com` host. The CLI is used for one thing, `snow stage copy <local>
@<stage>/<digest>/ --no-overwrite`, the only write the SQL API cannot do.

Egress: the SQL API reaches only `*.snowflakecomputing.com`. `snow stage copy` also reaches the
account's stage cloud storage (the presigned S3, Azure Blob or GCS endpoints the account returns)
and the certificate OCSP responders. The deploy key file is written with mode 0600, which Windows
does not enforce: there it relies on the ACL of the per-call temporary directory.

Every credential is checked against the target before any request or subprocess: its host, its
`tenant_id`, its `account` and `user` (the connection account-identifier pattern, so no leading `-`
reaches the argv) and its account against the target host. Every signed JWT is registered with the
redaction module.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol  # Any: decoded JSON bodies and result values
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from shifde.adapters import snowflake_jwt
from shifde.adapters.dataplatforms.snowflake import ACCOUNT
from shifde.adapters.native.addresses import normalize_snowflake_address
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.snowflake.source import CLI_VERSION, identifier
from shifde.core.redaction import register_secret

TIMEOUT = httpx.Timeout(30, connect=5)
STATEMENTS = "/api/v2/statements"
STATEMENT_TIMEOUT_S = 120
MAX_ROWS = 1000
POLL_LIMIT = 120
POLL_INTERVAL_S = 0.5
QUERY_TAG = "shifde-native-app"
_HANDLE = re.compile(r"[A-Za-z0-9-]{1,128}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
FOREIGN_CREDENTIAL = "Snowflake credential belongs to another account host or organization"

sleep = time.sleep  # between polls of a running statement; patched by tests
clock = time.time  # wall clock of the JWT `iat`; patched by tests


@dataclass(frozen=True)
class SnowflakeKeyPair:
    """The deploy user's key pair for one account of one organization; `repr` never shows the
    key."""

    tenant_id: UUID
    host: str
    account: str
    user: str
    role: str
    private_key_pem: str = field(repr=False)


@dataclass(frozen=True)
class StatementResult:
    rows: list[dict[str, Any]]
    handle: str


class SqlGateway(Protocol):
    platform: str

    def execute(self, sql: str, *, warehouse: str | None = None) -> StatementResult: ...


class StageCopier(Protocol):
    platform: str

    def check_version(self) -> str: ...
    def copy(self, local_dir: Path, stage: str, digest: str) -> None: ...


CredentialProvider = Callable[[], SnowflakeKeyPair]


def _host_accounts(host: str) -> tuple[str, str]:
    """The account part of a normalized host (the labels before `snowflakecomputing.com`, a
    trailing `privatelink` dropped) and its first label."""
    labels = host.removeprefix("https://").split(".")[:-2]
    if len(labels) > 1 and labels[-1] == "privatelink":
        labels = labels[:-1]
    return ".".join(labels), labels[0]


def _bound_credential(provider: CredentialProvider, target: Target) -> SnowflakeKeyPair:
    """The provider's credential, refused unless it belongs to `target`: the same (normalized)
    host and tenant, an `account` and `user` of the account-identifier pattern, and an `account`
    that is the host's account part or its first label. One fixed message, never a value."""
    host = normalize_snowflake_address(target.host)
    credential = provider()
    try:
        credential_host = normalize_snowflake_address(credential.host)
    except ValueError:
        credential_host = ""
    if (
        credential_host != host
        or credential.tenant_id != target.tenant_id
        or not ACCOUNT.fullmatch(credential.account)
        or not ACCOUNT.fullmatch(credential.user)
        or credential.account.lower().replace("_", "-") not in _host_accounts(host)
    ):
        raise PermissionError(FOREIGN_CREDENTIAL)
    return credential


class SnowflakeSqlApi:
    """One statement per request against `target.host`, as the credential's role."""

    platform = "snowflake"

    def __init__(
        self,
        target: Target,
        *,
        credential_provider: CredentialProvider,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.target, self.host = target, normalize_snowflake_address(target.host)  # egress rule
        self.credential_provider, self.transport = credential_provider, transport
        self._parsed: tuple[str, rsa.RSAPrivateKey] | None = None

    def __repr__(self) -> str:
        return f"SnowflakeSqlApi({self.host})"

    def _credential(self) -> SnowflakeKeyPair:
        return _bound_credential(self.credential_provider, self.target)

    def _key(self, pem: str) -> rsa.RSAPrivateKey:
        """The parsed key of `pem` (kept for the last PEM only: parsing checks the key, slowly)."""
        if self._parsed is None or self._parsed[0] != pem:
            key = snowflake_jwt.private_key(pem)
            if isinstance(key, str):
                raise ValueError(key)
            self._parsed = (pem, key)
        return self._parsed[1]

    def _headers(self, credential: SnowflakeKeyPair) -> dict[str, str]:
        key = self._key(credential.private_key_pem)
        token = snowflake_jwt.sign(key, credential.account, credential.user, int(clock()))
        register_secret(token)
        return {
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Accept": "application/json",
        }

    def execute(self, sql: str, *, warehouse: str | None = None) -> StatementResult:
        credential = self._credential()
        body: dict[str, Any] = {  # Any: JSON body
            "statement": sql,
            "timeout": STATEMENT_TIMEOUT_S,
            "role": identifier(credential.role),
            "parameters": {"MULTI_STATEMENT_COUNT": "1", "QUERY_TAG": QUERY_TAG},
        }
        if warehouse:
            body["warehouse"] = identifier(warehouse)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=self.transport) as c:
            response = c.post(
                f"{self.host}{STATEMENTS}", headers=self._headers(credential), json=body
            )
            for _ in range(POLL_LIMIT):
                if response.status_code != 202:
                    break
                sleep(POLL_INTERVAL_S)
                handle = _handle(_document(response))
                response = c.get(
                    f"{self.host}{STATEMENTS}/{handle}", headers=self._headers(self._credential())
                )
            else:
                raise TimeoutError("Snowflake statement did not finish in time")
        if response.status_code != 200:
            # The body may echo the statement or native metadata; only the status is reported.
            raise RuntimeError(f"Snowflake SQL API answered HTTP {response.status_code}")
        doc = _document(response)
        return StatementResult(rows=_rows(doc), handle=_handle(doc))


def _document(response: httpx.Response) -> dict[str, Any]:  # Any: JSON body
    try:
        doc = response.json()
    except ValueError:
        raise RuntimeError("Snowflake SQL API answer is not JSON") from None
    if not isinstance(doc, dict):
        raise RuntimeError("Snowflake SQL API answer is not a JSON object")
    return doc


def _handle(doc: dict[str, Any]) -> str:  # Any: JSON body
    handle = doc.get("statementHandle")
    if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
        raise RuntimeError("Snowflake SQL API answer has no statement handle")
    return handle


def _rows(doc: dict[str, Any]) -> list[dict[str, Any]]:  # Any: JSON body and values
    """The rows as dicts with lowercased column names; more than `MAX_ROWS` is refused."""
    meta = doc.get("resultSetMetaData") or {}
    columns = [str(c.get("name", "")).lower() for c in meta.get("rowType") or []]
    data = doc.get("data") or []
    total = meta.get("numRows", len(data))
    if not isinstance(total, int) or total > MAX_ROWS or len(data) > MAX_ROWS:
        raise ValueError("native metadata result exceeded its bound")
    if total != len(data):
        raise ValueError("native metadata result is partitioned")
    return [dict(zip(columns, row, strict=True)) for row in data if isinstance(row, list)]


class SnowflakeStageCopier:
    """The pinned Snowflake CLI in a minimal environment: version check and stage copy only, to
    `target.host` with a credential of that host. `snow stage copy` also reaches the account's
    stage cloud storage (presigned S3, Azure Blob or GCS endpoints) and the certificate OCSP
    responders. The key file's 0600 mode is not enforced on Windows, which relies on the ACL of
    the per-call temporary directory."""

    platform = "snowflake"

    def __init__(
        self,
        target: Target,
        executable: str = "snow",
        *,
        credential_provider: CredentialProvider,
        timeout: float = 600,
    ) -> None:
        self.target, self.host = target, normalize_snowflake_address(target.host)  # egress rule
        self.executable, self.credential_provider = executable, credential_provider
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"SnowflakeStageCopier({self.host}, {self.executable})"

    @staticmethod
    def _env(home: str) -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(HOME=home, USERPROFILE=home, TEMP=home, TMP=home, TMPDIR=home)
        return env

    def _run(self, args: list[str], directory: str) -> bytes:
        executable = shutil.which(self.executable)
        if not executable:
            raise FileNotFoundError(
                "install the pinned Snowflake CLI or provide its executable path"
            )
        try:
            process = subprocess.run(
                [executable, *args],
                cwd=directory,
                env=self._env(directory),
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Snowflake CLI command timed out") from None
        if process.returncode:
            # Raw CLI output may hold native metadata or credentials; never return it to an agent.
            raise RuntimeError(f"Snowflake CLI command failed with exit code {process.returncode}")
        return process.stdout

    def _version(self, directory: str) -> str:
        output = self._run(["--version"], directory)
        match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", output.decode(errors="replace"))
        if not match or match.group(1) != CLI_VERSION:
            raise ValueError(f"Snowflake CLI {CLI_VERSION} is required")
        return match.group(1)

    def check_version(self) -> str:
        with tempfile.TemporaryDirectory(prefix="shifde-snowcli-") as temporary:
            return self._version(temporary)

    def copy(self, local_dir: Path, stage: str, digest: str) -> None:
        """Upload `local_dir` to `@<stage>/<digest>/`, never overwriting a staged file."""
        stage = ".".join(identifier(part) for part in stage.split("."))
        if not _DIGEST.fullmatch(digest):
            raise ValueError("invalid release digest")
        credential = _bound_credential(self.credential_provider, self.target)
        host = self.host.removeprefix("https://")
        with tempfile.TemporaryDirectory(prefix="shifde-snowcli-") as temporary:
            self._version(temporary)
            config = Path(temporary) / "config.toml"
            config.write_text("[cli]\n", encoding="utf-8")
            key_file = Path(temporary) / "deploy-key.p8"
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(credential.private_key_pem)
            self._run(
                [
                    "--config-file",
                    str(config),
                    "stage",
                    "copy",
                    str(local_dir),
                    f"@{stage}/{digest}/",
                    "--no-overwrite",
                    "--recursive",
                    "--no-auto-compress",
                    "--temporary-connection",
                    "--account",
                    credential.account,
                    "--user",
                    credential.user,
                    "--role",
                    identifier(credential.role),
                    "--host",
                    host,
                    "--authenticator",
                    "SNOWFLAKE_JWT",
                    "--private-key-file",
                    str(key_file),
                    "--format",
                    "JSON",
                ],
                temporary,
            )
