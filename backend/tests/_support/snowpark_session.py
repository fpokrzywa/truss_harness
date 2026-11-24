"""A test-only Snowpark session on a `FakeSnowflakeAccount` (SP6-5): what the generated Snowflake
app's `backend_snowflake.py`, `deep_health.py` and `streamlit_app.py` get from
`get_active_session()` inside Snowflake.

`session.sql(query, params=[...])` returns a frame whose `.collect()` sends the statement to the
fake's SQL API v2 handler in process (positional typed bindings, as Snowpark binds Python values,
and an accepted programmatic access token) and returns Snowpark `Row`s with each value converted
by its `rowType` as Snowpark returns it (`fixed` -> int or Decimal, `real` -> float, `text` -> str,
`date` -> `datetime.date`, `boolean` -> bool, null -> None); `.schema` is the matching Snowpark
`StructType`, its names quoted as Snowpark quotes them (a plain uppercase name comes back bare, any
other keeps its quotes, `"COUNT(*)"`). A failed statement raises `SessionSqlError`. The fake's
SQLite is read-only, so `INSERT INTO APP_DATA.USAGE` is answered here: the row goes to the
session's own `usage` list (`fail_usage` refuses it instead)."""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any  # Any: SQL values and JSON

import httpx
from snowflake.snowpark import Row
from snowflake.snowpark.types import (
    BooleanType,
    DataType,
    DateType,
    DecimalType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from shifde.adapters.dataplatforms.fakes import SNOWFLAKE_STATEMENTS, FakeSnowflakeAccount

TOKEN = "sf-shim-pat-0001"  # scan-secrets: allow, the fake's accepted token
_USAGE = re.compile(
    r"INSERT\s+INTO\s+APP_DATA\.USAGE\s*\(([^)]*)\)\s*VALUES\s*\(\s*CURRENT_TIMESTAMP\(\)\s*"
    r"((?:,\s*\?\s*)*)\)",
    re.IGNORECASE,
)
_TYPES: dict[str, DataType] = {
    "fixed": LongType(),
    "real": DoubleType(),
    "text": StringType(),
    "date": DateType(),
    "boolean": BooleanType(),
}


class SessionSqlError(Exception):
    """The fake refused the statement (its status code and SQL API body)."""


def _binding(value: Any) -> dict[str, Any]:  # Any: a bound Python value
    if value is None:
        return {"type": "TEXT", "value": None}
    if isinstance(value, bool):
        return {"type": "BOOLEAN", "value": "true" if value else "false"}
    if isinstance(value, int):
        return {"type": "FIXED", "value": str(value)}
    if isinstance(value, float):
        return {"type": "REAL", "value": repr(value)}
    if isinstance(value, Decimal):
        return {"type": "FIXED", "value": str(value)}
    if isinstance(value, datetime.date):
        return {"type": "DATE", "value": value.isoformat()}
    return {"type": "TEXT", "value": str(value)}


def _cell(value: Any, kind: str) -> Any:  # Any: a SQL API text cell / its Snowpark value
    if value is None:
        return None
    if kind == "fixed":
        return Decimal(value) if "." in str(value) else int(value)
    if kind == "real":
        return float(value)
    if kind == "date":
        return datetime.date.fromisoformat(str(value))
    if kind == "boolean":
        return str(value).lower() == "true"
    return str(value)


def _quoted(name: str) -> str:
    """The name as Snowpark's `quote_name_without_upper_casing` passes it to `StructField`."""
    return '"' + name.replace('"', '""') + '"'


def _datatype(kind: str, sample: Any) -> DataType:  # Any: a converted cell
    if kind == "fixed" and isinstance(sample, Decimal):
        return DecimalType(38, -sample.as_tuple().exponent)  # type: ignore[operator]
    return _TYPES.get(kind, StringType())


@dataclass
class ShimFrame:
    session: FakeSnowparkSession
    query: str
    params: list[Any]
    _answer: tuple[list[Row], StructType] | None = None

    def _run(self) -> tuple[list[Row], StructType]:
        if self._answer is None:
            self._answer = self.session.execute(self.query, self.params)
        return self._answer

    def collect(self) -> list[Row]:
        return self._run()[0]

    @property
    def schema(self) -> StructType:
        return self._run()[1]


@dataclass
class FakeSnowparkSession:
    """A Snowpark-like session of one Snowflake account (`fake`, which must accept `TOKEN`)."""

    fake: FakeSnowflakeAccount
    fail_usage: bool = False
    usage: list[dict[str, Any]] = field(default_factory=list)  # Any: usage rows
    statements: list[str] = field(default_factory=list)

    def sql(self, query: str, params: list[Any] | None = None) -> ShimFrame:  # Any: values
        return ShimFrame(self, query, list(params or []))

    def execute(self, query: str, params: list[Any]) -> tuple[list[Row], StructType]:  # Any
        self.statements.append(query)
        usage = _USAGE.fullmatch(query.strip())
        if usage is not None:
            return self._insert_usage(
                [c.strip().lower() for c in usage.group(1).split(",")], params
            )
        body = {
            "statement": query,
            "bindings": {str(i): _binding(v) for i, v in enumerate(params, start=1)},
            "parameters": {"MULTI_STATEMENT_COUNT": "1"},
        }
        response = self.fake.handle(
            httpx.Request(
                "POST",
                f"https://{self.fake.host}{SNOWFLAKE_STATEMENTS}",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
                    "Content-Type": "application/json",
                },
                content=json.dumps(body).encode(),
            )
        )
        if response.status_code != 200:
            raise SessionSqlError(response.status_code, response.text)
        doc = response.json()
        row_type = (doc.get("resultSetMetaData") or {}).get("rowType") or []
        kinds = [str(c["type"]) for c in row_type]
        rows = [Row(*(_cell(v, k) for v, k in zip(r, kinds, strict=True))) for r in doc["data"]]
        fields = [
            StructField(
                _quoted(str(c["name"])),
                _datatype(kinds[i], next((r[i] for r in rows if r[i]), None)),
            )
            for i, c in enumerate(row_type)
        ]
        return rows, StructType(fields)

    def _insert_usage(
        self, columns: list[str], params: list[Any]
    ) -> tuple[list[Row], StructType]:  # Any: bound values
        if self.fail_usage:
            raise SessionSqlError(403, "insufficient privileges on APP_DATA.USAGE")
        values = dict(zip(columns[1:], params, strict=True))
        self.usage.append({columns[0]: datetime.datetime.now(datetime.UTC), **values})
        return [Row(1)], StructType([StructField("number of rows inserted", LongType())])
