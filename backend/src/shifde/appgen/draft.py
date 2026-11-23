"""The drafting prompt and the validation of one model reply (SP5 design 5.2 and 5.3).

The system message is fixed text holding `DRAFT_SCHEMA`; the user message is the brief and the
requested tables' column names and types from the stored discovery, never a row value. A reply is
one JSON object (a single surrounding fenced code block is stripped), checked against
`DRAFT_SCHEMA` with `jsonschema` and then by `Draft` itself; a failure gives at most
`MAX_MESSAGES` messages of at most `MAX_MESSAGE` characters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any  # Any: discovery payloads and parsed JSON

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from shifde.appgen.schemas import FLOATS_REFUSED, Draft, holds_float

DRAFT_SCHEMA: dict[str, Any] = Draft.model_json_schema()
MAX_MESSAGES = 20
MAX_MESSAGE = 300
_VALIDATOR = Draft202012Validator(DRAFT_SCHEMA)
_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n[ \t]*```\s*\Z", re.DOTALL)

SYSTEM_PROMPT = (
    "You draft a small read-only data application for a Databricks SQL warehouse. The user "
    "message is a JSON document with a brief and the tables you may read, each with its column "  # nosec B608 - prompt text for the model, not a query
    "names and types. Draft one agent with instructions, one to eight read-only SQL tools (one "
    "SELECT or WITH statement each, every table named as catalog.schema.table from the given "
    "tables, parameters written as :name and declared), the screens of the application and "
    "evaluation cases with assertions. Answer with exactly one JSON object and nothing else. It "
    "must match this JSON schema:\n" + json.dumps(DRAFT_SCHEMA, sort_keys=True)
)


def table_key(table: Mapping[str, Any]) -> str:  # Any: a discovered table
    """`catalog.schema.name` of a discovered table, lowercased."""
    return f"{table['catalog']}.{table['schema']}.{table['name']}".lower()


def user_message(
    brief: str,
    discovery_payload: Mapping[str, Any],  # Any: the discovery's JSON payload
    tables: Sequence[str],
) -> str:
    """The brief and the requested tables' column names and types, in the request's order."""
    found = {table_key(t): t for t in discovery_payload.get("tables") or []}
    described = [
        {
            "name": name,
            "columns": [
                {"name": c["name"], "type": c["type"]} for c in found[name].get("columns") or []
            ],
        }
        for name in (t.lower() for t in tables)
        if name in found
    ]
    return json.dumps({"brief": brief, "tables": described}, ensure_ascii=False)


def _clip(messages: Sequence[str]) -> list[str]:
    return [m[:MAX_MESSAGE] for m in messages[:MAX_MESSAGES]]


def _where(path: Sequence[Any]) -> str:  # Any: path items (keys and indexes)
    return ".".join(str(p) for p in path) or "(root)"


def parse_reply(text: str) -> Draft | list[str]:
    """The draft a reply holds, or the messages saying why it holds none."""
    fenced = _FENCE.match(text)
    body = fenced.group(1) if fenced else text
    try:
        value = json.loads(body)
    except (ValueError, RecursionError) as exc:
        return _clip([f"the reply is not JSON: {exc}"])
    if not isinstance(value, dict):
        return ["the reply is not one JSON object"]
    if holds_float(value):
        return [FLOATS_REFUSED]
    errors = sorted(_VALIDATOR.iter_errors(value), key=lambda e: [str(p) for p in e.path])
    if errors:
        return _clip([f"{_where(e.path)}: {e.message}" for e in errors])
    try:
        return Draft.model_validate(value)
    except ValidationError as exc:
        return _clip([f"{_where(e['loc'])}: {e['msg']}" for e in exc.errors()])
