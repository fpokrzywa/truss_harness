"""A stateful in-memory Snowflake account for Native App delivery tests and demos.

`FakeSnowflakeNativePlatform` answers the SQL API v2 through `transport()` (an
`httpx.MockTransport`) and acts as the pinned CLI (`copier(target)`). State is kept per account
host: packages, stages with their files, versions and patches, applications and grants. It accepts
only key-pair JWTs that verify against `public_key` and records every statement and copy in order
(`log`). Development and test only: any other `environment` refuses to construct it. Fault switches
are off by default (`faults`, see `FAULTS`).

`DEEP_HEALTH()` is a stubbed answer until SP6-6, which will run the staged `ui/deep_health.py`
instead: it returns SP5 6.4's shape with both checks passed (`deep_health_bad` fails one).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # Any: SQL API JSON and row values
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shifde.adapters import snowflake_jwt
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.snowflake.clients import (
    STATEMENTS,
    CredentialProvider,
    SnowflakeSqlApi,
)
from shifde.adapters.native.snowflake.source import CLI_VERSION, identifier

ENVIRONMENTS = ("development", "test")
GENERATOR_VERSION = "shifde.appgen.snowflake/1.0"
STEPS = (
    "package-create",
    "schema-create",
    "stage-create",
    "stage-upload",
    "setup-validate",
    "version-create",
    "application-apply",
    "application-access",
)
FAULTS = frozenset(
    {f"fail_after_accept:{step}" for step in STEPS}
    | {"setup_mismatch", "foreign_package", "foreign_application", "hang", "deep_health_bad"}
)

JSON = dict[str, Any]  # Any: a JSON object

_NAME = r'"([A-Z_][A-Z0-9_$]*)"'
_LOCATION = _NAME + r'\."SHIFDE"\."SOURCE"/([a-f0-9]{64})/'
_PATTERNS: dict[str, re.Pattern[str]] = {
    key: re.compile(pattern)
    for key, pattern in {
        "session": r"SELECT CURRENT_ACCOUNT\(\) AS ACCOUNT, CURRENT_ROLE\(\) AS ROLE",
        "show_packages": r"SHOW APPLICATION PACKAGES LIKE '([A-Z_][A-Z0-9_$]*)'",
        "show_applications": r"SHOW APPLICATIONS LIKE '([A-Z_][A-Z0-9_$]*)'",
        "show_versions": r"SHOW VERSIONS IN APPLICATION PACKAGE " + _NAME,
        "show_stages": r"SHOW STAGES LIKE 'SOURCE' IN DATABASE " + _NAME,
        "create_package": r"CREATE APPLICATION PACKAGE "
        + _NAME
        + " ENABLE_RELEASE_CHANNELS = FALSE",
        "create_schema": r"CREATE SCHEMA IF NOT EXISTS " + _NAME + r'\."SHIFDE"',
        "create_stage": r"CREATE STAGE IF NOT EXISTS "
        + _NAME
        + r"""\."SHIFDE"\."SOURCE" ENCRYPTION = \(TYPE = 'SNOWFLAKE_SSE'\)""",
        "list": r"LIST @" + _LOCATION + r"(?: PATTERN = '([^']*)')?",
        "add_version": r"ALTER APPLICATION PACKAGE "
        + _NAME
        + r" ADD (?:VERSION V1|PATCH (\d+) FOR VERSION V1) USING '@"
        + _LOCATION
        + r"' LABEL = 'shifde ([a-f0-9]{12})'",
        "create_application": r"CREATE APPLICATION "
        + _NAME
        + " FROM APPLICATION PACKAGE "
        + _NAME
        + r" USING VERSION V1 PATCH (\d+)",
        "upgrade_application": r"ALTER APPLICATION "
        + _NAME
        + r" UPGRADE USING VERSION V1 PATCH (\d+)",
        "grant": r"GRANT APPLICATION ROLE " + _NAME + r'\."APP_USER" TO ROLE ' + _NAME,
        "health": r"SELECT " + _NAME + r'\."APP_PUBLIC"\."HEALTH"\(\) AS STATUS',
        "deep_health": r"CALL " + _NAME + r'\."APP_PUBLIC"\."DEEP_HEALTH"\(\)',
        "describe": r"DESCRIBE APPLICATION " + _NAME,
    }.items()
}
# The step each write statement belongs to (the `fail_after_accept:<step>` switches).
_WRITES = {
    "create_package": "package-create",
    "create_schema": "schema-create",
    "create_stage": "stage-create",
    "add_version": "version-create",
    "create_application": "application-apply",
    "upgrade_application": "application-apply",
    "grant": "application-access",
}


class SqlError(Exception):
    """A statement the account refuses: answered as a SQL API error (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _json(status: int, body: JSON) -> httpx.Response:
    return httpx.Response(status, json=body)


@dataclass
class Account:
    """One account host's objects."""

    packages: dict[str, JSON] = field(default_factory=dict)
    schemas: set[str] = field(default_factory=set)
    stages: dict[str, JSON] = field(default_factory=dict)  # package -> stage (with `files`)
    versions: dict[str, list[JSON]] = field(default_factory=dict)  # package -> patches
    applications: dict[str, JSON] = field(default_factory=dict)
    grants: set[tuple[str, str]] = field(default_factory=set)  # (application, role)


class FakeStageCopier:
    """The pinned CLI of the fake: stores the files of one copy on the target's account."""

    platform = "fake"

    def __init__(self, platform: FakeSnowflakeNativePlatform, host: str) -> None:
        self.fake, self.host = platform, host

    def check_version(self) -> str:
        return CLI_VERSION

    def copy(self, local_dir: Path, stage: str, digest: str) -> None:
        self.fake.copy(self.host, local_dir, stage, digest)


class FakeSnowflakeNativePlatform:
    def __init__(
        self,
        *,
        environment: str,
        public_key: rsa.RSAPublicKey,
        account: str = "acct1",
        user: str = "deployer",
        faults: set[str] | None = None,
        hang_timeout: float = 60.0,
    ) -> None:
        if environment not in ENVIRONMENTS:
            raise RuntimeError("the fake Snowflake platform runs only in development or test")
        self.public_key, self.account, self.user = public_key, account, user
        self.faults: set[str] = set(faults or ())
        unknown = self.faults - FAULTS
        if unknown:
            raise ValueError(f"unknown fault switches: {sorted(unknown)}")
        self.hang_timeout = hang_timeout
        self.accounts: dict[str, Account] = {}
        self.log: list[str] = []  # every statement and copy, in order
        self._released = threading.Event()
        self._lock = threading.RLock()
        self._counter = 0

    def __repr__(self) -> str:
        return f"FakeSnowflakeNativePlatform({self.account})"

    # ------------------------------------------------------------------ handed out
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def gateway(self, target: Target, credential_provider: CredentialProvider) -> SnowflakeSqlApi:
        """The real SQL API client, over this fake's transport."""
        api = SnowflakeSqlApi(
            target, credential_provider=credential_provider, transport=self.transport()
        )
        api.platform = "fake"
        return api

    def copier(self, target: Target) -> FakeStageCopier:
        return FakeStageCopier(self, urlsplit(target.host).hostname or "")

    def state(self, host: str) -> Account:
        """The objects of `host` (`<account>.snowflakecomputing.com`)."""
        return self.accounts.setdefault(host.removeprefix("https://").lower(), Account())

    def release(self) -> None:
        """Unblock a `hang`ing write (it still raises `TimeoutError`: accepted, unconfirmed)."""
        self._released.set()

    def writes(self) -> list[str]:
        """The logged statements and copies that change something, in order."""
        reads = ("SELECT ", "SHOW ", "LIST ", "DESCRIBE ", "CALL ")
        return [entry for entry in self.log if not entry.startswith(reads)]

    def created(self) -> str:
        self._counter += 1
        return f"2026-11-23 00:00:{self._counter:02d}.000 +0000"

    def _accepted(self, step: str, *, write: bool = True) -> None:
        """After a write took effect (or, for `setup-validate`, which writes nothing, after its
        `LIST` was answered): the `hang` and `fail_after_accept:<step>` switches."""
        if write and "hang" in self.faults:
            self._released.wait(self.hang_timeout)
            raise TimeoutError("native write accepted but never confirmed")
        if f"fail_after_accept:{step}" in self.faults:
            raise TimeoutError("connection lost after native acceptance")

    # ------------------------------------------------------------------ the CLI
    def copy(self, host: str, local_dir: Path, stage: str, digest: str) -> None:
        with self._lock:
            self.log.append(f"COPY {local_dir.name} @{stage}/{digest}/")
            package, schema, name = (identifier(part) for part in stage.split("."))
            account = self.state(host)
            stored = account.stages.get(package)
            if (schema, name) != ("SHIFDE", "SOURCE") or stored is None:
                raise RuntimeError("Snowflake CLI command failed with exit code 1")
            files: dict[str, JSON] = stored["files"]
            uploads = {
                f"{digest}/{path.relative_to(local_dir).as_posix()}": path.read_bytes()
                for path in sorted(local_dir.rglob("*"))
                if path.is_file()
            }
            if any(path in files for path in uploads):
                raise RuntimeError("Snowflake CLI command failed with exit code 1")  # no overwrite
            for path, data in uploads.items():
                files[path] = {
                    "data": data,
                    "size": len(data),
                    "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                }
        self._accepted("stage-upload")

    # ------------------------------------------------------------------ the SQL API
    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.lower()
        if not host.endswith(".snowflakecomputing.com"):
            return _json(404, {"code": "390404", "message": "unknown host"})
        if not self._authorized(request):
            return _json(401, {"code": "390303", "message": "Invalid credentials."})
        if request.method == "POST" and request.url.path == STATEMENTS:
            return self._submit(host, json.loads(request.content or b"{}"))
        return _json(404, {"code": "390404", "message": "endpoint not found"})

    def _authorized(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not token:
            return False
        if request.headers.get("x-snowflake-authorization-token-type") != "KEYPAIR_JWT":
            return False
        parts = token.split(".")
        if len(parts) != 3:
            return False
        try:
            header, claims, signature = (_b64url_decode(p) for p in parts)
            self.public_key.verify(
                signature, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            head, body = json.loads(header), json.loads(claims)
        except (ValueError, InvalidSignature):
            return False
        if not isinstance(head, dict) or not isinstance(body, dict):
            return False
        subject = snowflake_jwt.jwt_subject(self.account, self.user)
        issuer = f"{subject}.{snowflake_jwt.public_key_fingerprint(self.public_key)}"
        exp = body.get("exp")
        return (
            head.get("alg") == "RS256"
            and body.get("iss") == issuer
            and body.get("sub") == subject
            and isinstance(exp, int)
            and exp > time.time()
        )

    def _submit(self, host: str, body: JSON) -> httpx.Response:
        statement = body.get("statement")
        parameters = body.get("parameters") or {}
        if (
            not isinstance(statement, str)
            or parameters.get("MULTI_STATEMENT_COUNT") != "1"
            or ";" in statement
        ):
            return _json(422, {"code": "000008", "message": "multiple statements are refused"})
        role = str(body.get("role") or "PUBLIC")
        with self._lock:
            self.log.append(statement)
            self._counter += 1
            handle = f"01b0-{self._counter:04d}"
            try:
                columns, rows = self._run(host, statement, role, body.get("warehouse"))
            except SqlError as error:
                return _json(
                    422, {"code": error.code, "message": error.message, "statementHandle": handle}
                )
        for kind, step in _WRITES.items():
            if _PATTERNS[kind].fullmatch(statement):
                self._accepted(step)
        listed = _PATTERNS["list"].fullmatch(statement)
        if listed and listed.group(3) is not None:
            self._accepted("setup-validate", write=False)
        return _json(
            200,
            {
                "statementHandle": handle,
                "resultSetMetaData": {
                    "numRows": len(rows),
                    "rowType": [{"name": name} for name in columns],
                },
                "data": [[None if v is None else str(v) for v in row] for row in rows],
            },
        )

    def _run(
        self, host: str, statement: str, role: str, warehouse: object
    ) -> tuple[list[str], list[list[Any]]]:  # Any: row values
        account = self.state(host)
        for kind, pattern in _PATTERNS.items():
            match = pattern.fullmatch(statement)
            if match:
                answer: tuple[list[str], list[list[Any]]] = getattr(self, f"_{kind}")(
                    account, role, warehouse, *match.groups()
                )
                return answer
        raise SqlError("001003", "SQL compilation error: unsupported statement")

    # ------------------------------------------------------------------ statements
    @staticmethod
    def _table(rows: list[JSON], columns: list[str]) -> tuple[list[str], list[list[Any]]]:
        return [c.upper() for c in columns], [[row.get(c) for c in columns] for row in rows]

    def _session(self, account: Account, role: str, *_: Any) -> tuple[list[str], list[Any]]:
        return ["ACCOUNT", "ROLE"], [[self.account.upper(), role]]

    def _show_packages(self, account: Account, role: str, _w: object, name: str) -> Any:
        if "foreign_package" in self.faults and name not in account.packages:
            account.packages[name] = {
                "name": name,
                "created_on": self.created(),
                "owner": "OTHER_ROLE",
            }
        rows = [account.packages[name]] if name in account.packages else []
        return self._table(rows, ["name", "created_on", "owner"])

    def _show_applications(self, account: Account, role: str, _w: object, name: str) -> Any:
        if "foreign_application" in self.faults and name not in account.applications:
            account.applications[name] = {
                "name": name,
                "created_on": self.created(),
                "owner": "OTHER_ROLE",
                "source": "OTHER_PACKAGE",
                "source_type": "APPLICATION PACKAGE",
                "version": "V1",
                "patch": 0,
                "label": "foreign",
                "upgrade_state": "COMPLETE",
            }
        rows = [account.applications[name]] if name in account.applications else []
        columns = ["name", "created_on", "owner", "source", "source_type"]
        return self._table(rows, [*columns, "version", "patch", "label", "upgrade_state"])

    def _show_versions(self, account: Account, role: str, _w: object, package: str) -> Any:
        self._package(account, package)
        rows = account.versions.get(package, [])
        return self._table(rows, ["version", "patch", "label", "created_on", "dropped_on"])

    def _show_stages(self, account: Account, role: str, _w: object, package: str) -> Any:
        self._package(account, package)
        stage = account.stages.get(package)
        return self._table([stage] if stage else [], ["name", "schema_name", "created_on", "owner"])

    @staticmethod
    def _package(account: Account, package: str) -> JSON:
        if package not in account.packages:
            raise SqlError("002003", "Application package does not exist or not authorized.")
        return account.packages[package]

    def _create_package(self, account: Account, role: str, _w: object, package: str) -> Any:
        if package in account.packages:
            raise SqlError("002002", "Object already exists.")
        account.packages[package] = {"name": package, "created_on": self.created(), "owner": role}
        return self._done()

    def _create_schema(self, account: Account, role: str, _w: object, package: str) -> Any:
        self._package(account, package)
        account.schemas.add(package)
        return self._done()

    def _create_stage(self, account: Account, role: str, _w: object, package: str) -> Any:
        self._package(account, package)
        if package not in account.schemas:
            raise SqlError("002003", "Schema does not exist or not authorized.")
        account.stages.setdefault(
            package,
            {
                "name": "SOURCE",
                "schema_name": "SHIFDE",
                "created_on": self.created(),
                "owner": role,
                "files": {},
            },
        )
        return self._done()

    def _list(
        self, account: Account, role: str, _w: object, package: str, digest: str, pattern: Any
    ) -> Any:
        stage = account.stages.get(package)
        if stage is None:
            raise SqlError("002003", "Stage does not exist or not authorized.")
        files: dict[str, JSON] = stage["files"]
        rows = [
            {"name": f"source/{path}", "size": item["size"], "md5": item["md5"]}
            for path, item in sorted(files.items())
            if path.startswith(f"{digest}/")
            and (pattern is None or re.fullmatch(pattern, f"source/{path}"))
        ]
        if pattern is None and "setup_mismatch" in self.faults:
            # After the upload's own check: the staged setup script no longer matches.
            changed = files.get(f"{digest}/setup.sql")
            if changed is not None:
                data = changed["data"] + b"\n-- changed on the stage\n"
                changed.update(
                    data=data,
                    size=len(data),
                    md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
                )
        return self._table(rows, ["name", "size", "md5"])

    def _add_version(
        self,
        account: Account,
        role: str,
        _w: object,
        package: str,
        patch: str | None,
        using: str,
        digest: str,
        label: str,
    ) -> Any:
        self._package(account, package)
        stage = account.stages.get(package)
        if using != package or stage is None:
            raise SqlError("002003", "Stage does not exist or not authorized.")
        if not any(path.startswith(f"{digest}/") for path in stage["files"]):
            raise SqlError("093082", "The stage location holds no files.")
        versions = account.versions.setdefault(package, [])
        number = 0 if patch is None else int(patch)
        if (patch is None and versions) or any(int(v["patch"]) == number for v in versions):
            raise SqlError("093083", "Version or patch already exists.")
        versions.append(
            {
                "version": "V1",
                "patch": number,
                "label": f"shifde {label}",
                "created_on": self.created(),
                "dropped_on": None,
                "digest": digest,
            }
        )
        return self._done()

    def _release(self, account: Account, package: str, patch: int) -> JSON:
        release = next(
            (v for v in account.versions.get(package, []) if int(v["patch"]) == patch), None
        )
        if release is None:
            raise SqlError("093084", "Version or patch does not exist.")
        return release

    def _create_application(
        self, account: Account, role: str, _w: object, name: str, package: str, patch: str
    ) -> Any:
        self._package(account, package)
        if name in account.applications:
            raise SqlError("002002", "Object already exists.")
        release = self._release(account, package, int(patch))
        account.applications[name] = {
            "name": name,
            "created_on": self.created(),
            "owner": role,
            "source": package,
            "source_type": "APPLICATION PACKAGE",
            "version": "V1",
            "patch": int(patch),
            "label": release["label"],
            "upgrade_state": "COMPLETE",
        }
        return self._done()

    def _upgrade_application(
        self, account: Account, role: str, _w: object, name: str, patch: str
    ) -> Any:
        app = account.applications.get(name)
        if app is None:
            raise SqlError("002003", "Application does not exist or not authorized.")
        release = self._release(account, str(app["source"]), int(patch))
        app.update(patch=int(patch), label=release["label"], upgrade_state="COMPLETE")
        return self._done()

    def _grant(self, account: Account, role: str, _w: object, name: str, grantee: str) -> Any:
        if name not in account.applications:
            raise SqlError("002003", "Application does not exist or not authorized.")
        account.grants.add((name, grantee))
        return self._done()

    def _describe(self, account: Account, role: str, _w: object, name: str) -> Any:
        app = account.applications.get(name)
        if app is None:
            raise SqlError("002003", "Application does not exist or not authorized.")
        facts = {
            "name": name,
            "source_application_package": app["source"],
            "version": app["version"],
            "patch": app["patch"],
            "version_label": app["label"],
            "upgrade_state": app["upgrade_state"],
            "disablement_reasons": None,
        }
        rows = [{"property": key, "value": value} for key, value in facts.items()]
        return self._table(rows, ["property", "value"])

    def _installed(self, account: Account, name: str, warehouse: object) -> JSON:
        app = account.applications.get(name)
        if app is None:
            raise SqlError("002003", "Application does not exist or not authorized.")
        if not warehouse:
            raise SqlError("000606", "No active warehouse selected in the current session.")
        return app

    def _health(self, account: Account, role: str, warehouse: object, name: str) -> Any:
        self._installed(account, name, warehouse)
        return ["STATUS"], [["OK"]]

    def _deep_health(self, account: Account, role: str, warehouse: object, name: str) -> Any:
        # A stub until SP6-6 runs the staged `ui/deep_health.py` here.
        app = self._installed(account, name, warehouse)
        release = self._release(account, str(app["source"]), int(app["patch"]))
        bad = "deep_health_bad" in self.faults
        reason = "the app cannot use Cortex model mistral-large2" if bad else None
        report = {
            "status": "failed" if bad else "ok",
            "generator_version": GENERATOR_VERSION,
            "release_digest": release["digest"],
            "checks": [
                {"name": "warehouse", "status": "passed", "reason": None},
                {
                    "name": "serving_endpoint",
                    "status": "failed" if bad else "passed",
                    "reason": reason,
                },
            ],
        }
        return ["DEEP_HEALTH"], [[json.dumps(report)]]

    @staticmethod
    def _done() -> tuple[list[str], list[list[Any]]]:
        return ["status"], [["Statement executed successfully."]]
