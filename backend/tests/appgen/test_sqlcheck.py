"""SP5-2 (design 5.3 and 5.4): the draft diagnostics and the validation of one model reply.

`sqlcheck.validate` forwards every `build.sqlrules` issue of a tool as an error at
`tools.<i>.sql` and adds `screen_tool_unknown`, `screen_input_unknown`, `duplicate_name` and the
`case_trivial` warning; it never raises. `SAMPLE_DRAFT` over `SAMPLE_TABLES` has no error.
`draft.parse_reply` strips one fenced block, refuses anything but one closed draft object (floats
included) with at most 20 messages of at most 300 characters, and the prompt holds the schema.
"""

from __future__ import annotations

import copy
import json
from typing import Any  # Any: draft JSON

import pytest

from shifde.appgen import draft as drafting
from shifde.appgen import sqlcheck
from shifde.appgen.schemas import Draft
from shifde.packaging.canonical import digest
from tests._support.appgen_fixtures import SAMPLE_DRAFT, SAMPLE_TABLES


def _draft(**changes: Any) -> dict[str, Any]:  # Any: draft JSON
    out = copy.deepcopy(SAMPLE_DRAFT)
    out.update(changes)
    return out


def _codes(value: dict[str, Any]) -> list[tuple[str, str, str]]:  # Any: draft JSON
    found = sqlcheck.validate(Draft.model_validate(value), SAMPLE_TABLES)
    return [(d.path, d.code, d.severity) for d in found]


def _with_sql(sql: str, params: list[str] | None = None) -> dict[str, Any]:  # Any: draft JSON
    value = _draft()
    value["tools"][1]["sql"] = sql
    value["tools"][1]["parameters"] = [
        {"name": p, "type": "string", "required": True} for p in (params or [])
    ]
    return value


def test_the_sample_draft_has_no_error() -> None:
    assert _codes(SAMPLE_DRAFT) == []


def test_the_hash_is_the_canonical_digest() -> None:
    draft = Draft.model_validate(SAMPLE_DRAFT)
    assert sqlcheck.draft_hash(draft) == digest(draft.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("sql", "params", "code"),
    [
        ("select 1 from main.default.orders -- note", [], "sql_comment"),
        ("select 1 from main.default.orders; select 2", [], "sql_multiple_statements"),
        ("delete from main.default.orders", [], "sql_not_select"),
        ("select 1 from main.default.orders where x = 'a' and drop", [], "sql_write_statement"),
        ("select 1 from main.other.secrets", [], "sql_table_not_allowed"),
        ("select 1 from default.orders", [], "sql_table_not_qualified"),
        ("select 1 from read_files('x')", [], "sql_table_function"),
        ("select 1 from main.default.orders where id = :id", [], "sql_parameter_undeclared"),
        ("select 1 from main.default.orders", ["region"], "sql_parameter_unused"),
        ("select 1 from main.default.orders where name = 'open", [], "sql_unterminated_quote"),
    ],
)
def test_each_sql_rule_is_forwarded_as_an_error_of_its_tool(
    sql: str, params: list[str], code: str
) -> None:
    found = _codes(_with_sql(sql, params))
    assert ("tools.1.sql", code, "error") in found, found
    assert all(path == "tools.1.sql" for path, _, _ in found)


def test_a_table_screen_names_an_existing_tool_and_its_parameters() -> None:
    value = _draft()
    value["screens"][1] = {"kind": "table", "title": "T", "tool": "nope", "inputs": []}
    assert _codes(value) == [("screens.1.tool", "screen_tool_unknown", "error")]
    value["screens"][1] = {
        "kind": "table",
        "title": "T",
        "tool": "orders_by_customer",
        "inputs": ["customer_id", "region"],
    }
    assert _codes(value) == [("screens.1.inputs.1", "screen_input_unknown", "error")]


def test_tool_names_screen_titles_and_case_ids_are_unique() -> None:
    value = _draft()
    value["tools"].append(copy.deepcopy(value["tools"][0]))
    value["screens"][1]["title"] = value["screens"][0]["title"]
    value["evaluation_cases"][2]["case_id"] = value["evaluation_cases"][0]["case_id"]
    assert _codes(value) == [
        ("tools.2.name", "duplicate_name", "error"),
        ("screens.1.title", "duplicate_name", "error"),
        ("evaluation_cases.2.case_id", "duplicate_name", "error"),
    ]


@pytest.mark.parametrize("kind", ["equals", "contains", "not_contains"])
def test_an_empty_expected_text_is_a_warning(kind: str) -> None:
    value = _draft()
    value["evaluation_cases"][0]["assertions"][0] = {"id": "e", "kind": kind, "value": ""}
    assert _codes(value) == [("evaluation_cases.0.assertions.0.value", "case_trivial", "warning")]


def test_validation_never_raises_on_odd_sql() -> None:
    for sql in ("'", "`", "select `", ":", "((((", "select * from a.b.c.d.e", "\x00"):
        sqlcheck.validate(Draft.model_validate(_with_sql(sql)), SAMPLE_TABLES)


# ------------------------------------------------------------------ the reply (draft.py)
def test_a_reply_of_the_sample_draft_parses_plain_and_fenced() -> None:
    text = json.dumps(SAMPLE_DRAFT)
    for reply in (text, f"```json\n{text}\n```", f"  ```\n{text}\n```  "):
        parsed = drafting.parse_reply(reply)
        assert isinstance(parsed, Draft)
        assert parsed.model_dump(mode="json") == SAMPLE_DRAFT


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("not json", "the reply is not JSON"),
        ("[1, 2]", "the reply is not one JSON object"),
        (json.dumps(_draft(extra=1)), "Additional properties"),
        (json.dumps(_draft(tools=[])), "tools"),
    ],
)
def test_an_invalid_reply_gives_messages(reply: str, expected: str) -> None:
    parsed = drafting.parse_reply(reply)
    assert isinstance(parsed, list) and parsed
    assert any(expected in m for m in parsed), parsed


def test_a_float_anywhere_is_refused() -> None:
    value = _draft()
    value["evaluation_cases"][0]["assertions"][0] = {"id": "j", "kind": "json_equals", "value": 1.5}
    assert drafting.parse_reply(json.dumps(value)) == ["floats are not allowed"]
    with pytest.raises(ValueError, match="floats are not allowed"):
        Draft.model_validate(value)


def test_messages_are_capped() -> None:
    value = _draft()
    value["tools"] = [{"name": "X" * 400, "sql": "", "description": ""} for _ in range(8)]
    parsed = drafting.parse_reply(json.dumps(value))
    assert isinstance(parsed, list)
    assert 0 < len(parsed) <= drafting.MAX_MESSAGES
    assert all(len(m) <= drafting.MAX_MESSAGE for m in parsed)


def test_at_most_one_chat_screen() -> None:
    value = _draft()
    value["screens"].append({"kind": "chat", "title": "Second", "placeholder": ""})
    parsed = drafting.parse_reply(json.dumps(value))
    assert isinstance(parsed, list) and any("one chat screen" in m for m in parsed)


def test_the_prompt_embeds_the_schema_and_only_names_and_types() -> None:
    assert json.dumps(drafting.DRAFT_SCHEMA, sort_keys=True) in drafting.SYSTEM_PROMPT
    payload = {
        "tables": [
            {
                "catalog": "main",
                "schema": "default",
                "name": "Orders",
                "table_type": "MANAGED",
                "columns": [{"name": "id", "type": "bigint", "nullable": False}],
            },
            {"catalog": "main", "schema": "default", "name": "other", "columns": []},
        ]
    }
    message = json.loads(drafting.user_message("brief", payload, ["MAIN.default.orders"]))
    assert message == {
        "brief": "brief",
        "tables": [{"name": "main.default.orders", "columns": [{"name": "id", "type": "bigint"}]}],
    }
