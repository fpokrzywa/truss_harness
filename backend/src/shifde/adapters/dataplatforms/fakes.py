"""In-process fakes of a Databricks workspace and a Snowflake account (design 2026-11-22 section
7.5).

`FakeDatabricksWorkspace` is an `httpx.MockTransport` handler. The egress guard sends it a copy of
each request aimed at the pinned address with the original `Host` header (`_pinned_copy`), so the
fake routes by that header; tests build the guard with `resolver=lambda host: ["127.0.0.1"]` and
`inner=fake.transport()`, and the real policy layers still decide. It serves the endpoints the
`databricks` adapter calls, records every request, and has the eight fault switches of 7.5.
`FakeSnowflakeAccount` does the same for the `snowflake` adapter's SQL API v2 calls and verifies
every key-pair JWT against its registered public key.

`dev_transport()` is the transport of the development switch `SHIFDE_DEV_FAKE_DATA_PLATFORMS`
(B-2755): one lazily created workspace per host, accepting any non-empty bearer token and any
OAuth client.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any  # Any: decoded JSON bodies
from urllib.parse import unquote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

FAULTS = frozenset(
    {
        "bad_token",
        "forbidden",
        "rate_limited",
        "server_error",
        "slow",
        "oversize",
        "redirect",
        "warehouse_missing",
    }
)
SELECT_ONE = "SELECT 1"
ROWS_STATEMENT = "SELECT * FROM fake_rows"  # answers `row_count` rows (the row-cap tests)
TOKEN_PATH = "/oidc/v1/token"


def _tree() -> dict[str, dict[str, list[str]]]:
    return {
        "main": {"default": ["customers", "orders"], "sales": ["invoices"]},
        "samples": {"nyctaxi": ["trips"]},
    }


def _json(status: int, doc: Any, headers: dict[str, str] | None = None) -> httpx.Response:  # Any
    return httpx.Response(status, json=doc, headers=headers)


@dataclass
class FakeDatabricksWorkspace:
    host: str = "ws1.cloud.databricks.com"
    token: str | None = None  # the accepted bearer token; None accepts any non-empty one
    client_id: str | None = None  # OAuth M2M client; None (with no secret) accepts any client
    client_secret: str | None = None
    warehouse_id: str = "wh-0001"
    warehouse_state: str = "RUNNING"
    tree: dict[str, dict[str, list[str]]] = field(default_factory=_tree)
    row_count: int = 3  # rows `ROWS_STATEMENT` answers
    page_size: int = 100  # items per Unity Catalog page
    retry_after: str = "1"
    oversize_bytes: int = 6_000_000
    token_lifetime: int = 3600
    faults: set[str] = field(default_factory=set)
    record: bool = True  # keep every request in `requests` (the development switch does not)
    requests: list[httpx.Request] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)  # access tokens the OAuth endpoint sent
    _statements: dict[str, dict[str, Any]] = field(default_factory=dict)  # Any: statement doc
    _ids: itertools.count[int] = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        unknown = set(self.faults) - FAULTS
        if unknown:
            raise ValueError(f"unknown fault switches: {sorted(unknown)}")

    def __repr__(self) -> str:
        return f"FakeDatabricksWorkspace({self.host})"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[tuple[str, str]]:
        """(method, path) of every recorded request, in order."""
        return [(r.method, r.url.path) for r in self.requests]

    # ------------------------------------------------------------------ dispatch
    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.record:
                self.requests.append(request)
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").split(":")[0].lower() != self.host:
            return _json(404, {"error_code": "NOT_FOUND", "message": "unknown host"})
        path = request.url.path
        if request.method == "POST" and path == TOKEN_PATH:
            return self._token(request)
        if "redirect" in self.faults:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example.test/"})
        if "rate_limited" in self.faults:
            return _json(
                429, {"error_code": "TOO_MANY_REQUESTS"}, {"Retry-After": self.retry_after}
            )
        if "server_error" in self.faults:
            return _json(500, {"error_code": "INTERNAL_ERROR"})
        if not self._authorized(request):
            return _json(401, {"error_code": "UNAUTHENTICATED"})
        if "forbidden" in self.faults:
            return _json(403, {"error_code": "PERMISSION_DENIED"})
        if "oversize" in self.faults:
            return httpx.Response(200, content=b" " * self.oversize_bytes)
        return self._route(request, path)

    def _authorized(self, request: httpx.Request) -> bool:
        if "bad_token" in self.faults:
            return False
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not value.strip():
            return False
        return self.token is None or value == self.token or value in self.issued

    def _token(self, request: httpx.Request) -> httpx.Response:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        try:
            pair = base64.b64decode(value).decode() if scheme == "Basic" else ""
        except ValueError:
            pair = ""
        client_id, _, secret = pair.partition(":")
        client_id, secret = unquote(client_id.replace("+", " ")), unquote(secret.replace("+", " "))
        form = dict(
            part.split("=", 1) for part in request.content.decode().split("&") if "=" in part
        )
        if (
            "bad_token" in self.faults
            or not client_id
            or not secret
            or (self.client_id is not None and client_id != self.client_id)
            or (self.client_secret is not None and secret != self.client_secret)
        ):
            return _json(401, {"error": "invalid_client"})
        if form.get("grant_type") != "client_credentials" or form.get("scope") != "all-apis":
            return _json(400, {"error": "invalid_request"})
        token = f"fake-dbx-access-{uuid.uuid4().hex}"  # no token is a prefix of another
        self.issued.append(token)
        return _json(
            200, {"access_token": token, "token_type": "Bearer", "expires_in": self.token_lifetime}
        )

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        q = request.url.params
        if request.method == "GET":
            if path == "/api/2.0/preview/scim/v2/Me":
                return _json(200, {"id": "1", "userName": "reader@example.test"})
            if path == "/api/2.1/unity-catalog/catalogs":
                names = [{"name": c} for c in self.tree]
                return self._page(names, "catalogs", q)
            if path == "/api/2.1/unity-catalog/schemas":
                schemas = self.tree.get(q.get("catalog_name", ""))
                if schemas is None:
                    return _json(404, {"error_code": "CATALOG_DOES_NOT_EXIST"})
                return self._page([{"name": s} for s in schemas], "schemas", q)
            if path == "/api/2.1/unity-catalog/tables":
                catalog, schema = q.get("catalog_name", ""), q.get("schema_name", "")
                tables = self.tree.get(catalog, {}).get(schema)
                if tables is None:
                    return _json(404, {"error_code": "SCHEMA_DOES_NOT_EXIST"})
                return self._page([self._table(catalog, schema, t) for t in tables], "tables", q)
            if path.startswith("/api/2.0/sql/warehouses/"):
                wanted = path.rsplit("/", 1)[1]
                if "warehouse_missing" in self.faults or wanted != self.warehouse_id:
                    return _json(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                return _json(200, {"id": wanted, "name": "fake", "state": self.warehouse_state})
            if path.startswith("/api/2.0/sql/statements/"):
                doc = self._statements.get(path.rsplit("/", 1)[1])
                return _json(200, doc) if doc else _json(404, {"error_code": "NOT_FOUND"})
        if request.method == "POST":
            if path == "/api/2.0/sql/statements":
                return self._submit(json.loads(request.content or b"{}"))
            if path.startswith("/api/2.0/sql/statements/") and path.endswith("/cancel"):
                doc = self._statements.get(path.split("/")[-2])
                if doc is not None:
                    doc["status"] = {"state": "CANCELED"}
                return _json(200, {})
        return _json(404, {"error_code": "ENDPOINT_NOT_FOUND"})

    def _page(self, items: list[dict[str, Any]], key: str, q: httpx.QueryParams) -> httpx.Response:
        start = int(q.get("page_token") or 0)
        size = min(int(q.get("max_results") or self.page_size), self.page_size) or self.page_size
        doc: dict[str, Any] = {key: items[start : start + size]}  # Any: JSON page
        if start + size < len(items):
            doc["next_page_token"] = str(start + size)
        return _json(200, doc)

    @staticmethod
    def _table(catalog: str, schema: str, name: str) -> dict[str, Any]:  # Any: JSON table
        return {
            "name": name,
            "catalog_name": catalog,
            "schema_name": schema,
            "table_type": "MANAGED",
            "columns": [
                {"name": "id", "type_text": "bigint", "type_name": "LONG", "nullable": False},
                {"name": "label", "type_text": "string", "type_name": "STRING", "nullable": True},
            ],
        }

    def _submit(self, body: dict[str, Any]) -> httpx.Response:  # Any: request JSON
        if "warehouse_missing" in self.faults or body.get("warehouse_id") != self.warehouse_id:
            return _json(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
        statement_id = f"stmt-{next(self._ids)}"
        if "slow" in self.faults:
            doc: dict[str, Any] = {"statement_id": statement_id, "status": {"state": "PENDING"}}
        else:
            doc = self._result(statement_id, str(body.get("statement", "")), body.get("row_limit"))
        self._statements[statement_id] = doc
        return _json(200, doc)

    def _result(self, statement_id: str, statement: str, row_limit: Any) -> dict[str, Any]:  # Any
        text = statement.strip().rstrip(";").strip()
        if text.upper() == SELECT_ONE:
            columns, rows = [("1", "INT")], [["1"]]
        elif text == ROWS_STATEMENT:
            columns = [("n", "INT"), ("label", "STRING")]
            rows = [[str(i), f"row {i}"] for i in range(self.row_count)]
        else:
            columns, rows = [], []
        limit = row_limit if isinstance(row_limit, int) else len(rows)
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "statement_id": statement_id,
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "format": "JSON_ARRAY",
                "schema": {
                    "column_count": len(columns),
                    "columns": [
                        {"name": n, "type_name": t, "position": i}
                        for i, (n, t) in enumerate(columns)
                    ],
                },
                "total_row_count": len(rows),
                "truncated": truncated,
            },
            "result": {"row_count": len(rows), "data_array": rows},
        }


SNOWFLAKE_STATEMENTS = "/api/v2/statements"
_COLUMNS_QUERY = re.compile(r'FROM "((?:[^"]|"")*)"\.INFORMATION_SCHEMA\.COLUMNS .* LIMIT (\d+)$')
_SF_COLUMNS = ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE"]
_SF_CURRENT = ["CURRENT_USER()", "CURRENT_ROLE()", "CURRENT_WAREHOUSE()", "CURRENT_VERSION()"]
SfTree = dict[str, dict[str, dict[str, list[tuple[str, str, bool]]]]]  # db -> schema -> table


def _sf_tree() -> SfTree:
    columns = [("ID", "NUMBER", False), ("LABEL", "TEXT", True)]
    return {
        "ANALYTICS": {"PUBLIC": {"CUSTOMERS": columns, "ORDERS": columns}},
        "SALES": {"RAW": {"INVOICES": columns}},
    }


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@dataclass
class FakeSnowflakeAccount:
    """A Snowflake account's SQL API v2. With `public_key` it accepts key-pair JWTs of `user`,
    checking the signature, `iss` (with the key fingerprint), `sub` and `exp` on every request;
    with `pat` it accepts that programmatic access token. Anything else is 401."""

    account: str = "acct1"
    user: str = "reader"
    public_key: rsa.RSAPublicKey | None = None
    pat: str | None = None
    warehouse: str = "COMPUTE_WH"
    tree: SfTree = field(default_factory=_sf_tree)
    # seeded answers of arbitrary statements: statement text -> (columns, rows)
    results: dict[str, tuple[list[str], list[list[Any]]]] = field(default_factory=dict)  # Any
    row_count: int = 3  # rows `ROWS_STATEMENT` answers
    partition_size: int = 10_000  # rows per result partition (only the first is in the answer)
    retry_after: str = "1"
    oversize_bytes: int = 6_000_000
    faults: set[str] = field(default_factory=set)
    clock: Callable[[], float] = time.time  # the wall clock `exp` is checked against
    record: bool = True
    requests: list[httpx.Request] = field(default_factory=list)
    _statements: dict[str, dict[str, Any]] = field(default_factory=dict)  # Any: statement doc
    _pending: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        unknown = set(self.faults) - FAULTS
        if unknown:
            raise ValueError(f"unknown fault switches: {sorted(unknown)}")

    def __repr__(self) -> str:
        return f"FakeSnowflakeAccount({self.host})"

    @property
    def host(self) -> str:
        return f"{self.account.lower().replace('_', '-')}.snowflakecomputing.com"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[tuple[str, str]]:
        """(method, path) of every recorded request, in order."""
        return [(r.method, r.url.path) for r in self.requests]

    def bodies(self) -> list[dict[str, Any]]:  # Any: request JSON
        """The body of every recorded statement submission, in order."""
        return [
            json.loads(r.content)
            for r in self.requests
            if (r.method, r.url.path) == ("POST", SNOWFLAKE_STATEMENTS)
        ]

    def expected_subject(self) -> str:
        return f"{self.account.split('.', 1)[0].upper()}.{self.user.upper()}"

    def expected_issuer(self) -> str:
        if self.public_key is None:
            return ""
        der = self.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        digest = base64.b64encode(hashlib.sha256(der).digest()).decode()
        return f"{self.expected_subject()}.SHA256:{digest}"

    # ------------------------------------------------------------------ dispatch
    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.record:
                self.requests.append(request)
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").split(":")[0].lower() != self.host:
            return _json(404, {"code": "390404", "message": "unknown host"})
        if "redirect" in self.faults:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example.test/"})
        if "rate_limited" in self.faults:
            return _json(429, {"code": "390429"}, {"Retry-After": self.retry_after})
        if "server_error" in self.faults:
            return _json(500, {"code": "390500"})
        if not self._authorized(request):
            return _json(401, {"code": "390303", "message": "Invalid credentials."})
        if "forbidden" in self.faults:
            return _json(403, {"code": "390403"})
        if "oversize" in self.faults:
            return httpx.Response(200, content=b" " * self.oversize_bytes)
        return self._route(request, request.url.path)

    def _authorized(self, request: httpx.Request) -> bool:
        if "bad_token" in self.faults:
            return False
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not value:
            return False
        kind = request.headers.get("x-snowflake-authorization-token-type", "")
        if kind == "PROGRAMMATIC_ACCESS_TOKEN":
            return self.pat is not None and value == self.pat
        if kind == "KEYPAIR_JWT" and self.public_key is not None:
            return self._valid_jwt(self.public_key, value)
        return False

    def _valid_jwt(self, public_key: rsa.RSAPublicKey, token: str) -> bool:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        try:
            header, claims, signature = (_b64url_decode(p) for p in parts)
            public_key.verify(
                signature, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            head, body = json.loads(header), json.loads(claims)
        except (ValueError, InvalidSignature):
            return False
        if not isinstance(head, dict) or not isinstance(body, dict):
            return False
        exp = body.get("exp")
        return (
            head.get("alg") == "RS256"
            and body.get("iss") == self.expected_issuer()
            and body.get("sub") == self.expected_subject()
            and isinstance(exp, int)
            and exp > self.clock()
        )

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "POST" and path == SNOWFLAKE_STATEMENTS:
            return self._submit(json.loads(request.content or b"{}"))
        rest = path.removeprefix(f"{SNOWFLAKE_STATEMENTS}/")
        if rest != path and request.method == "POST" and rest.endswith("/cancel"):
            handle = rest.removesuffix("/cancel")
            self._pending.discard(handle)
            if handle in self._statements:
                self._statements[handle] = {"code": "000604", "statementHandle": handle}
            return _json(200, {"code": "000604", "message": "Statement canceled."})
        if rest != path and request.method == "GET":
            if rest in self._pending:
                return _json(202, {"code": "333334", "statementHandle": rest})
            doc = self._statements.get(rest)
            return _json(200, doc) if doc else _json(404, {"code": "390404"})
        return _json(404, {"code": "390404", "message": "endpoint not found"})

    def _submit(self, body: dict[str, Any]) -> httpx.Response:  # Any: request JSON
        if "warehouse_missing" in self.faults:
            return _json(404, {"code": "000606", "message": "No active warehouse selected."})
        if (body.get("parameters") or {}).get("MULTI_STATEMENT_COUNT") != "1":
            return _json(400, {"code": "000008", "message": "MULTI_STATEMENT_COUNT is not 1"})
        handle = str(uuid.uuid4())
        if "slow" in self.faults:
            self._pending.add(handle)
            return _json(202, {"code": "333334", "statementHandle": handle})
        columns, rows = self._answer(str(body.get("statement", "")))
        size = max(self.partition_size, 1)
        partitions = [{"rowCount": len(rows[i : i + size])} for i in range(0, len(rows), size)]
        doc = {
            "code": "090001",
            "statementHandle": handle,
            "message": "Statement executed successfully.",
            "resultSetMetaData": {
                "numRows": len(rows),
                "format": "jsonv2",
                "rowType": [{"name": c, "type": "text"} for c in columns],
                "partitionInfo": partitions or [{"rowCount": 0}],
            },
            "data": rows[:size],
        }
        self._statements[handle] = doc
        return _json(200, doc)

    def _answer(self, statement: str) -> tuple[list[str], list[list[Any]]]:  # Any: row values
        text = statement.strip().rstrip(";").strip()
        if text in self.results:
            columns, rows = self.results[text]
            return list(columns), [list(r) for r in rows]
        if text.startswith("SELECT CURRENT_USER()"):
            return list(_SF_CURRENT), [[self.user.upper(), "PUBLIC", self.warehouse, "9.1.0"]]
        if text.upper() == "SHOW DATABASES":
            rows = [["2026-01-01 00:00:00", name, "SYSADMIN"] for name in self.tree]
            return ["created_on", "name", "owner"], rows
        found = _COLUMNS_QUERY.search(text)
        if found:
            database, limit = found.group(1).replace('""', '"'), int(found.group(2))
            rows = [
                [schema, table, column, data_type, "YES" if nullable else "NO"]
                for schema, tables in self.tree.get(database, {}).items()
                for table, columns in tables.items()
                for column, data_type, nullable in columns
            ]
            return list(_SF_COLUMNS), rows[:limit]
        if text.upper() == SELECT_ONE:
            return ["1"], [["1"]]
        if text == ROWS_STATEMENT:
            return ["N", "LABEL"], [[str(i), f"row {i}"] for i in range(self.row_count)]
        return [], []


class _DevFakes:
    """The development switch's handler: one permissive workspace per Databricks host."""

    def __init__(self) -> None:
        self._workspaces: dict[str, FakeDatabricksWorkspace] = {}
        self._lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0].lower()
        with self._lock:
            workspace = self._workspaces.get(host)
            if workspace is None:
                workspace = self._workspaces[host] = FakeDatabricksWorkspace(
                    host=host, record=False
                )
        return workspace.handle(request)


_DEV = _DevFakes()


def dev_transport() -> httpx.MockTransport:
    """The transport `SHIFDE_DEV_FAKE_DATA_PLATFORMS` gives `databricks`/`snowflake` connections."""
    return httpx.MockTransport(_DEV.handle)
