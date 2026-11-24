"""In-process fakes of a Databricks workspace and a Snowflake account (design 2026-11-22 section
7.5).

`FakeDatabricksWorkspace` is an `httpx.MockTransport` handler. The egress guard sends it a copy of
each request aimed at the pinned address with the original `Host` header (`_pinned_copy`), so the
fake routes by that header; tests build the guard with `resolver=lambda host: ["127.0.0.1"]` and
`inner=fake.transport()`, and the real policy layers still decide. It serves the endpoints the
`databricks` adapter calls, records every request, and has the eight fault switches of 7.5.

With `sample_data=True` the workspace answers statements from an in-memory, read-only SQLite
database (`main.default.customers`, `main.default.orders`, `main.sales.invoices`, synthetic rows):
a three-part name `a.b.c` is read as the table `a__b__c`, `:name` parameters are bound by their
`type`, and a statement SQLite refuses (a write included) or one naming an unknown table answers
`FAILED`. `serving={name: [{match, reply}]}` serves `POST /serving-endpoints/{name}/invocations`
(OpenAI chat, the `FakeProvider` matching rule, word-counted usage) and
`GET /api/2.0/serving-endpoints/{name}`. Two more switches: `table_denied:<c.s.t>` answers a
statement naming that table `FAILED` with `PERMISSION_DENIED`, and `endpoint_not_ready` reports
every endpoint `NOT_READY` and answers its invocations 503.
`FakeSnowflakeAccount` does the same for the `snowflake` adapter's SQL API v2 calls and verifies
every key-pair JWT against its registered public key. With `sample_data=True` it serves the same
sample rows (`SAMPLE_TABLES`, shared with the workspace) as `DEMO_DB.PUBLIC.CUSTOMERS`,
`DEMO_DB.PUBLIC.ORDERS` and `DEMO_DB.SALES.INVOICES` from read-only SQLite, lists them in
discovery, binds `?` from the request `bindings` by type and answers uppercased column names with
their `rowType` types; an unknown table, a statement SQLite refuses (a write included) or a table
under `table_denied:<D.S.T>` (an uppercase name, compared case-insensitively) answers 422.
`cortex={model: [{match, reply}]}` answers `SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?),
PARSE_JSON(?))` with the JSON of the options form, and serves `POST
/api/v2/cortex/v1/chat/completions` (OpenAI chat with a bearer alone; unverified until the live
run), both with the `FakeProvider` matching rule; `cortex_denied` refuses both.

`dev_transport()` is the transport of the development switch `SHIFDE_DEV_FAKE_DATA_PLATFORMS`
(B-2755): one lazily created, permissive Databricks workspace or Snowflake account per host
(`*.snowflakecomputing.com` gets the account), accepting any non-empty bearer token (a Snowflake
key-pair JWT or PAT included) and any OAuth client.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
import sqlite3
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
DATABRICKS_FAULTS = FAULTS | {"endpoint_not_ready"}
TABLE_DENIED = "table_denied:"
SELECT_ONE = "SELECT 1"
ROWS_STATEMENT = "SELECT * FROM fake_rows"  # answers `row_count` rows (the row-cap tests)
TOKEN_PATH = "/oidc/v1/token"


def _tree() -> dict[str, dict[str, list[str]]]:
    return {
        "main": {"default": ["customers", "orders"], "sales": ["invoices"]},
        "samples": {"nyctaxi": ["trips"]},
    }


# sample data: table -> ([(column, Databricks type)], rows), visibly synthetic
SampleTable = tuple[list[tuple[str, str]], list[tuple[Any, ...]]]  # Any: column values
SERVING = "/serving-endpoints/"
INVOCATIONS = "/invocations"
SERVING_STATE = "/api/2.0/serving-endpoints/"
_TYPE_NAMES = {"bigint": "LONG", "string": "STRING", "double": "DOUBLE", "date": "DATE"}
_SQLITE_TYPES = {"bigint": "INTEGER", "double": "REAL", "string": "TEXT", "date": "TEXT"}
_NAME_PART = r"(?:`[^`]*`|[A-Za-z_][A-Za-z0-9_]*)"
_THREE_PART = re.compile(rf"(?<![\w`.]){_NAME_PART}\s*\.\s*{_NAME_PART}\s*\.\s*{_NAME_PART}")
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


_REGIONS = ["North", "South", "East", "West"]
# the sample tables both fakes serve: name -> ([(column, Databricks type)], rows)
SAMPLE_TABLES: dict[str, SampleTable] = {
    "customers": (
        [("id", "bigint"), ("name", "string"), ("region", "string")],
        [(i, f"Sample Customer {i}", _REGIONS[(i - 1) % 4]) for i in range(1, 7)],
    ),
    "orders": (
        [
            ("id", "bigint"),
            ("customer_id", "bigint"),
            ("amount", "double"),
            ("ordered_on", "date"),
        ],
        [
            (i, (i - 1) % 6 + 1, 25.0 * i + 0.5, f"2026-0{(i - 1) // 4 + 1}-{10 + i:02d}")
            for i in range(1, 13)
        ],
    ),
    "invoices": (
        [("id", "bigint"), ("order_id", "bigint"), ("total", "double")],
        [(i, i, 25.0 * i + 2.5) for i in range(1, 13)],
    ),
}


def _sample_tables() -> dict[str, SampleTable]:
    return {
        "main.default.customers": SAMPLE_TABLES["customers"],
        "main.default.orders": SAMPLE_TABLES["orders"],
        "main.sales.invoices": SAMPLE_TABLES["invoices"],
    }


def _sample_db(tables: dict[str, SampleTable]) -> sqlite3.Connection:
    """The sample tables in an in-memory SQLite database, read-only once loaded."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    for name, (columns, rows) in tables.items():
        flat = name.replace(".", "__")
        spec = ", ".join(f"{c} {_SQLITE_TYPES[t]}" for c, t in columns)
        marks = ", ".join("?" for _ in columns)
        db.execute(f"CREATE TABLE {flat} ({spec})")  # nosec B608 - fixed sample names
        db.executemany(f"INSERT INTO {flat} VALUES ({marks})", rows)  # nosec B608 - fixed names
    db.commit()
    db.execute("PRAGMA query_only = ON")
    return db


def _flat(match: re.Match[str]) -> str:
    return "__".join(p.strip().strip('`"').lower() for p in match.group(0).split("."))


def _sqlite_statement(
    statement: str, three_part: re.Pattern[str] = _THREE_PART, literals: re.Pattern[str] = _QUOTED
) -> tuple[str, set[str]]:
    """The statement with every three-part name outside a quoted string (a `literals` match) as
    `a__b__c`, and the `a.b.c` names it rewrote."""
    out: list[str] = []
    names: set[str] = set()

    def rewrite(text: str) -> str:
        names.update(_flat(m).replace("__", ".") for m in three_part.finditer(text))
        return three_part.sub(_flat, text)

    last = 0
    for quoted in literals.finditer(statement):
        out += [rewrite(statement[last : quoted.start()]), quoted.group(0)]
        last = quoted.end()
    out.append(rewrite(statement[last:]))
    return "".join(out), names


def _bound(param: dict[str, Any]) -> Any:  # Any: a request parameter / SQLite value
    value, kind = param.get("value"), str(param.get("type") or "STRING")
    if value is None:
        return None
    if kind == "BIGINT":
        return int(value)
    if kind == "DOUBLE":
        return float(value)
    if kind == "BOOLEAN":
        return str(value).lower() == "true"
    return str(value)


def _json(status: int, doc: Any, headers: dict[str, str] | None = None) -> httpx.Response:  # Any
    return httpx.Response(status, json=doc, headers=headers)


def _scripted(script: list[dict[str, str]], messages: Any) -> tuple[str, dict[str, int]]:  # Any
    """The `FakeProvider` matching rule: the reply of the first `{match, reply}` entry whose match
    is in the last user message (else `fake: <message>`), and the word-counted usage."""
    chat = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    last = next(
        (str(m.get("content") or "") for m in reversed(chat) if m.get("role") == "user"), ""
    )
    reply = next((e["reply"] for e in script if e["match"] in last), f"fake: {last}")
    prompt = sum(len(str(m.get("content") or "").split()) for m in chat)
    completion = len(reply.split())
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    return reply, usage


def _chat_completion(script: list[dict[str, str]], body: dict[str, Any]) -> dict[str, Any]:  # Any
    """An OpenAI chat answer of a `{match, reply}` script to a chat request body."""
    reply, usage = _scripted(script, body.get("messages") or [])
    choice = {
        "index": 0,
        "message": {"role": "assistant", "content": reply},
        "finish_reason": "stop",
    }
    return {"choices": [choice], "usage": usage}


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
    sample_data: bool = False  # answer statements from the SQLite sample tables
    serving: dict[str, list[dict[str, str]]] = field(default_factory=dict)  # endpoint -> script
    requests: list[httpx.Request] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)  # access tokens the OAuth endpoint sent
    _statements: dict[str, dict[str, Any]] = field(default_factory=dict)  # Any: statement doc
    _ids: itertools.count[int] = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _samples: dict[str, SampleTable] = field(default_factory=dict)
    _db: sqlite3.Connection | None = None

    def __post_init__(self) -> None:
        unknown = {
            f for f in self.faults if f not in DATABRICKS_FAULTS and not f.startswith(TABLE_DENIED)
        }
        if unknown:
            raise ValueError(f"unknown fault switches: {sorted(unknown)}")
        if self.sample_data:
            self._samples = _sample_tables()
            self._db = _sample_db(self._samples)

    def __repr__(self) -> str:
        return f"FakeDatabricksWorkspace({self.host})"

    def denied_tables(self) -> set[str]:
        """The `c.s.t` names of the `table_denied:` switches, lowercased."""
        return {
            f.removeprefix(TABLE_DENIED).lower() for f in self.faults if f.startswith(TABLE_DENIED)
        }

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
            if path.startswith(SERVING_STATE):
                name = path.removeprefix(SERVING_STATE)
                if name not in self.serving:
                    return _json(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                ready = "NOT_READY" if "endpoint_not_ready" in self.faults else "READY"
                return _json(200, {"name": name, "state": {"ready": ready}})
        if request.method == "POST":
            if path.startswith(SERVING) and path.endswith(INVOCATIONS):
                name = path.removeprefix(SERVING).removesuffix(INVOCATIONS)
                return self._invocation(name, json.loads(request.content or b"{}"))
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

    def _table(self, catalog: str, schema: str, name: str) -> dict[str, Any]:  # Any: JSON table
        sample = self._samples.get(f"{catalog}.{schema}.{name}")
        columns = sample[0] if sample else [("id", "bigint"), ("label", "string")]
        return {
            "name": name,
            "catalog_name": catalog,
            "schema_name": schema,
            "table_type": "MANAGED",
            "columns": [
                {"name": c, "type_text": t, "type_name": _TYPE_NAMES[t], "nullable": c != "id"}
                for c, t in columns
            ],
        }

    def _invocation(self, name: str, body: dict[str, Any]) -> httpx.Response:  # Any: JSON
        """An OpenAI chat answer from the endpoint's `{match, reply}` script."""
        script = self.serving.get(name)
        if script is None:
            return _json(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
        if "endpoint_not_ready" in self.faults:
            return _json(503, {"error_code": "TEMPORARILY_UNAVAILABLE"})
        return _json(200, _chat_completion(script, body))

    def _submit(self, body: dict[str, Any]) -> httpx.Response:  # Any: request JSON
        if "warehouse_missing" in self.faults or body.get("warehouse_id") != self.warehouse_id:
            return _json(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
        statement_id = f"stmt-{next(self._ids)}"
        statement = str(body.get("statement", ""))
        text = statement.strip().rstrip(";").strip()
        if "slow" in self.faults:
            doc: dict[str, Any] = {"statement_id": statement_id, "status": {"state": "PENDING"}}
        elif (self._db is not None or self.denied_tables()) and (
            text.upper() != SELECT_ONE and text != ROWS_STATEMENT
        ):
            parameters = [p for p in body.get("parameters") or [] if isinstance(p, dict)]
            answered = self._query(text, parameters)
            if isinstance(answered, str):
                doc = {
                    "statement_id": statement_id,
                    "status": {
                        "state": "FAILED",
                        "error": {"error_code": answered, "message": "the statement failed"},
                    },
                }
            else:
                doc = _succeeded(statement_id, *answered, body.get("row_limit"))
        else:
            doc = self._result(statement_id, statement, body.get("row_limit"))
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
        return _succeeded(statement_id, columns, rows, row_limit)

    def _query(
        self, statement: str, parameters: list[dict[str, Any]]
    ) -> tuple[list[tuple[str, str]], list[list[Any]]] | str:  # Any: parameter and row values
        """The sample-data answer (columns, rows), or the `error_code` of a FAILED statement."""
        rewritten, names = _sqlite_statement(statement)
        if names & self.denied_tables():
            return "PERMISSION_DENIED"
        if self._db is None:
            return [], []
        if names - set(self._samples):
            return "TABLE_OR_VIEW_NOT_FOUND"
        known = {c: t.upper() for columns, _ in self._samples.values() for c, t in columns}
        try:
            cursor = self._db.execute(
                rewritten, {str(p.get("name")): _bound(p) for p in parameters}
            )
            found = cursor.fetchall()
        except (sqlite3.Error, ValueError, OverflowError):
            return "BAD_REQUEST"
        columns = [(str(d[0]), known.get(str(d[0]), "STRING")) for d in cursor.description or []]
        rows = [[None if v is None else str(v) for v in row] for row in found]
        return columns, rows


def _succeeded(
    statement_id: str,
    columns: list[tuple[str, str]],
    rows: list[list[Any]],  # Any: JSON_ARRAY values
    row_limit: Any,  # Any: the request's row_limit
) -> dict[str, Any]:  # Any: statement doc
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
                    {"name": n, "type_name": t, "position": i} for i, (n, t) in enumerate(columns)
                ],
            },
            "total_row_count": len(rows),
            "truncated": truncated,
        },
        "result": {"row_count": len(rows), "data_array": rows},
    }


SNOWFLAKE_STATEMENTS = "/api/v2/statements"
SNOWFLAKE_SUFFIX = ".snowflakecomputing.com"
_COLUMNS_QUERY = re.compile(r'FROM "((?:[^"]|"")*)"\.INFORMATION_SCHEMA\.COLUMNS .* LIMIT (\d+)$')
_SF_COLUMNS = ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE"]
_SF_CURRENT = ["CURRENT_USER()", "CURRENT_ROLE()", "CURRENT_WAREHOUSE()", "CURRENT_VERSION()"]
SfTree = dict[str, dict[str, dict[str, list[tuple[str, str, bool]]]]]  # db -> schema -> table
CORTEX_CHAT = "/api/v2/cortex/v1/chat/completions"
CORTEX_DENIED = "cortex_denied"
# a `table_denied:` switch of the Snowflake fake names its table as Snowflake stores an unquoted
# name: an uppercase DATABASE.SCHEMA.TABLE
_SF_DENIED_TABLE = re.compile(r"[A-Z_][A-Z0-9_$]*(\.[A-Z_][A-Z0-9_$]*){2}")
# the Snowflake sample tables: `database.schema.table` (lowercased) -> `SAMPLE_TABLES` key
_SF_SAMPLES = {
    "demo_db.public.customers": "customers",
    "demo_db.public.orders": "orders",
    "demo_db.sales.invoices": "invoices",
}
# Databricks sample type -> (Snowflake data type, SQL API `rowType[].type`)
_SF_TYPES = {
    "bigint": ("NUMBER", "fixed"),
    "string": ("TEXT", "text"),
    "double": ("FLOAT", "real"),
    "date": ("DATE", "date"),
}
_SF_NAME_PART = r'(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)'
_SF_THREE_PART = re.compile(
    rf'(?<![\w".$]){_SF_NAME_PART}\s*\.\s*{_SF_NAME_PART}\s*\.\s*{_SF_NAME_PART}'
)
_SF_LITERALS = re.compile(r"'(?:[^'\\]|\\.)*'|\$\$.*?\$\$", re.DOTALL)
_CORTEX_COMPLETE = re.compile(
    r"SELECT\s+SNOWFLAKE\s*\.\s*CORTEX\s*\.\s*COMPLETE\s*\(\s*\?\s*,"
    r"\s*PARSE_JSON\s*\(\s*\?\s*\)\s*,\s*PARSE_JSON\s*\(\s*\?\s*\)\s*\)",
    re.IGNORECASE,
)
SfColumns = list[tuple[str, str]]  # (name, `rowType[].type`)
SfAnswer = tuple[SfColumns, list[list[Any]]] | httpx.Response  # Any: row values


def _sf_error(code: str, message: str) -> httpx.Response:
    return _json(422, {"code": code, "message": message, "sqlState": "42000"})


def _sf_bound(binding: Any) -> Any:  # Any: a request binding / SQLite value
    value, kind = binding.get("value"), str(binding.get("type") or "TEXT")
    if value is None:
        return None
    if kind == "FIXED":
        return int(value)
    if kind == "REAL":
        return float(value)
    if kind == "BOOLEAN":
        return str(value).lower() == "true"
    return str(value)


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
    with `pat` it accepts that programmatic access token. Anything else is 401.
    `accept_any_token` (the development switch) accepts any non-empty bearer token of either
    token type, checking nothing else."""

    account: str = "acct1"
    user: str = "reader"
    public_key: rsa.RSAPublicKey | None = None
    pat: str | None = None
    accept_any_token: bool = False
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
    sample_data: bool = False  # serve `DEMO_DB.*` from the SQLite sample tables
    cortex: dict[str, list[dict[str, str]]] = field(default_factory=dict)  # model -> script
    requests: list[httpx.Request] = field(default_factory=list)
    _statements: dict[str, dict[str, Any]] = field(default_factory=dict)  # Any: statement doc
    _pending: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _samples: dict[str, SampleTable] = field(default_factory=dict)
    _db: sqlite3.Connection | None = None

    def __post_init__(self) -> None:
        unknown = {
            f
            for f in self.faults
            if f not in FAULTS
            and f != CORTEX_DENIED
            and not (
                f.startswith(TABLE_DENIED)
                and _SF_DENIED_TABLE.fullmatch(f.removeprefix(TABLE_DENIED))
            )
        }
        if unknown:
            raise ValueError(f"unknown fault switches: {sorted(unknown)}")
        if self.sample_data:
            self._samples = {name: SAMPLE_TABLES[key] for name, key in _SF_SAMPLES.items()}
            self._db = _sample_db(self._samples)
            for name, (columns, _) in self._samples.items():
                database, schema, table = name.upper().split(".")
                self.tree.setdefault(database, {}).setdefault(schema, {})[table] = [
                    (c.upper(), _SF_TYPES[t][0], c != "id") for c, t in columns
                ]

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

    def denied_tables(self) -> set[str]:
        """The `d.s.t` names of the `table_denied:` switches, lowercased."""
        return {
            f.removeprefix(TABLE_DENIED).lower() for f in self.faults if f.startswith(TABLE_DENIED)
        }

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
        cortex = request.url.path == CORTEX_CHAT
        if not (self._cortex_authorized(request) if cortex else self._authorized(request)):
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
        if self.accept_any_token:
            return kind in ("KEYPAIR_JWT", "PROGRAMMATIC_ACCESS_TOKEN")
        if kind == "PROGRAMMATIC_ACCESS_TOKEN":
            return self.pat is not None and value == self.pat
        if kind == "KEYPAIR_JWT" and self.public_key is not None:
            return self._valid_jwt(self.public_key, value)
        return False

    def _cortex_authorized(self, request: httpx.Request) -> bool:
        """The Cortex chat path takes a bearer alone (no token-type header): the PAT, or any
        non-empty token under `accept_any_token`."""
        if "bad_token" in self.faults:
            return False
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not value:
            return False
        return self.accept_any_token or (self.pat is not None and value == self.pat)

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
        if request.method == "POST" and path == CORTEX_CHAT:
            return self._cortex_chat(json.loads(request.content or b"{}"))
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
        bindings = body.get("bindings")
        answered = self._answer(
            str(body.get("statement", "")), bindings if isinstance(bindings, dict) else {}
        )
        if isinstance(answered, httpx.Response):
            return answered
        columns, rows = answered
        size = max(self.partition_size, 1)
        partitions = [{"rowCount": len(rows[i : i + size])} for i in range(0, len(rows), size)]
        doc = {
            "code": "090001",
            "statementHandle": handle,
            "message": "Statement executed successfully.",
            "resultSetMetaData": {
                "numRows": len(rows),
                "format": "jsonv2",
                "rowType": [{"name": c, "type": t} for c, t in columns],
                "partitionInfo": partitions or [{"rowCount": 0}],
            },
            "data": rows[:size],
        }
        self._statements[handle] = doc
        return _json(200, doc)

    def _answer(self, statement: str, bindings: dict[str, Any]) -> SfAnswer:  # Any: bindings
        """Seeded `results`, the special statements, the Cortex call, the SQLite sample tables
        (with `sample_data` or a `table_denied:` switch), else an empty answer."""
        text = statement.strip().rstrip(";").strip()
        if text in self.results:
            columns, rows = self.results[text]
            return [(c, "text") for c in columns], [list(r) for r in rows]
        special = self._special(text)
        if special is not None:
            return [(c, "text") for c in special[0]], special[1]
        if _CORTEX_COMPLETE.fullmatch(text):
            return self._complete(bindings)
        if self._db is not None or self.denied_tables():
            return self._query(text, bindings)
        return [], []

    def _special(self, text: str) -> tuple[list[str], list[list[Any]]] | None:  # Any: row values
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
        return None

    def _complete(self, bindings: dict[str, Any]) -> SfAnswer:  # Any: bindings
        """`SNOWFLAKE.CORTEX.COMPLETE(model, messages, options)`: one `text` row holding the JSON
        of the options form, from the model's script."""
        if CORTEX_DENIED in self.faults:
            return _sf_error("003001", "Insufficient privileges to use Cortex.")
        model = (bindings.get("1") or {}).get("value")
        script = self.cortex.get(model) if isinstance(model, str) else None
        if script is None:
            return _sf_error("100351", "Unknown model.")
        try:
            messages = json.loads(str((bindings.get("2") or {}).get("value") or "[]"))
        except ValueError:
            return _sf_error("100069", "Invalid messages.")
        reply, usage = _scripted(script, messages)
        doc = {
            "choices": [{"messages": reply}],
            "created": int(self.clock()),
            "model": model,
            "usage": usage,
        }
        return [("COMPLETE", "text")], [[json.dumps(doc)]]

    def _cortex_chat(self, body: dict[str, Any]) -> httpx.Response:  # Any: request JSON
        """The Cortex chat-completions REST path (OpenAI chat format; unverified until the live
        run): the model's script in the OpenAI reply shape."""
        if CORTEX_DENIED in self.faults:
            return _json(403, {"code": "003001", "message": "Insufficient privileges."})
        model = body.get("model")
        script = self.cortex.get(model) if isinstance(model, str) else None
        if script is None:
            return _json(404, {"code": "100351", "message": "Unknown model."})
        return _json(200, _chat_completion(script, body))

    def _query(self, statement: str, bindings: dict[str, Any]) -> SfAnswer:  # Any: bindings
        """The sample-data answer: three-part names as SQLite tables, `?` bound by position and
        type, column names uppercased; an unknown, denied or refused statement is a 422."""
        rewritten, names = _sqlite_statement(statement, _SF_THREE_PART, _SF_LITERALS)
        if names & self.denied_tables():
            return _sf_error("003001", "Insufficient privileges to operate on the table.")
        if self._db is None:
            return [], []
        if names - set(self._samples):
            return _sf_error("002003", "Object does not exist or not authorized.")
        known = {c: _SF_TYPES[t][1] for columns, _ in self._samples.values() for c, t in columns}
        try:
            values = [_sf_bound(bindings[k]) for k in sorted(bindings, key=int)]
            cursor = self._db.execute(rewritten, values)
            found = cursor.fetchall()
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError):
            return _sf_error("001003", "SQL compilation error.")
        columns = [
            (str(d[0]).upper(), known.get(str(d[0]).lower(), "text"))
            for d in cursor.description or []
        ]
        rows = [[None if v is None else str(v) for v in row] for row in found]
        return columns, rows


class _DevFakes:
    """The development switch's handler: one permissive Snowflake account per
    `*.snowflakecomputing.com` host and one permissive Databricks workspace per other host."""

    def __init__(self) -> None:
        self._fakes: dict[str, FakeDatabricksWorkspace | FakeSnowflakeAccount] = {}
        self._lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0].lower()
        with self._lock:
            fake = self._fakes.get(host)
            if fake is None:
                if host.endswith(SNOWFLAKE_SUFFIX):
                    fake = FakeSnowflakeAccount(
                        account=host.removesuffix(SNOWFLAKE_SUFFIX),
                        record=False,
                        accept_any_token=True,
                    )
                else:
                    fake = FakeDatabricksWorkspace(host=host, record=False)
                self._fakes[host] = fake
        return fake.handle(request)


_DEV = _DevFakes()


def dev_transport() -> httpx.MockTransport:
    """The transport `SHIFDE_DEV_FAKE_DATA_PLATFORMS` gives `databricks`/`snowflake` connections."""
    return httpx.MockTransport(_DEV.handle)
