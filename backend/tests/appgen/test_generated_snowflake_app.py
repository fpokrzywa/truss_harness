"""SP6-5 conformance (SP6 design 6.3): the Native App package rendered from the
`SNOWFLAKE_SAMPLE_DRAFT` release, written to a temporary directory, its `ui/` modules imported and
run on a test-only Snowpark session (`tests/_support/snowpark_session.py`) backed by
`FakeSnowflakeAccount(sample_data=True, cortex={model: AGENT_SCRIPT})`. The three cases are
answered through `agent_loop.ask` and pass their assertions; each tool's `run_tool` result is
SETH's Snowflake answer; the stop codes and the 256 KiB cap are the Databricks app's; deep health
passes, then names the denied table or the Cortex model; `streamlit_app.py` under
`streamlit.testing.v1.AppTest` answers on the chat screen (one `APP_DATA.USAGE` row per answer)
and shows rows on the table screen; the generated modules import no SETH, HTTP or Snowflake
library. Cross-client: accounts A and B each have their own app, and an answer writes usage only
to its own account's table, with its own release digest."""

from __future__ import annotations

import ast
import copy
import datetime
import json
import sys
from collections.abc import Callable, Iterator
from decimal import Decimal
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: JSON documents

import pytest
from streamlit.testing.v1 import AppTest

from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.appgen.render import render
from shifde.appgen.service import sql_template
from shifde.build.tool_runtime import sql_request
from shifde.evaluation.assertions import evaluate
from shifde.evaluation.schemas import Case
from shifde.packaging.canonical import digest
from tests._support.appgen_fixtures import AGENT_SCRIPT, CASE_CALLS, SNOWFLAKE_SAMPLE_DRAFT
from tests._support.snowpark_session import TOKEN, FakeSnowparkSession
from tests.adapters.dataplatforms.helpers import ctx_for, make_snowflake, sf_conn
from tests.appgen.rendered import (
    CORTEX_MODEL,
    SNOWFLAKE_DIGEST,
    SNOWFLAKE_MANIFEST,
    SNOWFLAKE_RESOLVED,
    input_schema,
    load_module,
    snowflake_manifest_of,
    write_files,
)

CASES = SNOWFLAKE_SAMPLE_DRAFT["evaluation_cases"]
TOOLS = {t["name"]: t for t in SNOWFLAKE_SAMPLE_DRAFT["tools"]}
BIG_SQL = "select replace(hex(zeroblob(140000)), '0', 'x') as big"  # 280 000 characters
TABLE_CHECKS = [
    "table:DEMO_DB.PUBLIC.CUSTOMERS",
    "table:DEMO_DB.PUBLIC.ORDERS",
    "table:DEMO_DB.SALES.INVOICES",
]
_names = count()


class App:
    """A rendered package's `ui/` directory and its imported modules."""

    def __init__(self, ui: Path) -> None:
        self.ui = ui
        n = next(_names)
        self.backend = load_module(ui / "backend_snowflake.py", f"generated_sf_backend_{n}")
        self.loop = self.backend.agent_loop
        self.health = load_module(ui / "deep_health.py", f"generated_sf_health_{n}")
        self.config = self.loop.load_config(ui / "config")

    def ask(self, session: FakeSnowparkSession, question: str, **options: Any) -> Any:  # Any
        call_model = self.backend.call_model(session, CORTEX_MODEL)
        run_sql = self.backend.run_sql(session)
        return self.loop.ask(call_model, run_sql, self.config, question, **options)

    def run_tool(self, session: FakeSnowparkSession, name: str, arguments: Any) -> Any:  # Any
        tool = self.config["tools"][name]
        return self.loop.run_tool(self.backend.run_sql(session), tool, arguments)


def _package(
    directory: Path, manifest: dict[str, Any], resolved: dict[str, Any]
) -> App:  # Any: JSON
    write_files(directory, render(manifest, digest(manifest), resolved))
    return App(directory / "ui")


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> App:
    return _package(tmp_path_factory.mktemp("sf-app"), SNOWFLAKE_MANIFEST, SNOWFLAKE_RESOLVED)


@pytest.fixture(scope="module")
def app_big(tmp_path_factory: pytest.TempPathFactory) -> App:
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"].append(
        {
            **resolved["tools"][0],
            "name": "big_text",
            "description": "One oversized text.",
            "sql": BIG_SQL,
            "parameters": [],
            "input_schema": input_schema([]),
        }
    )
    return _package(tmp_path_factory.mktemp("sf-app-big"), SNOWFLAKE_MANIFEST, resolved)


def account(script: list[dict[str, str]] = AGENT_SCRIPT, **options: Any) -> FakeSnowflakeAccount:
    return FakeSnowflakeAccount(
        sample_data=True, cortex={CORTEX_MODEL: script}, pat=TOKEN, **options
    )


def session_on(script: list[dict[str, str]] = AGENT_SCRIPT, **options: Any) -> FakeSnowparkSession:
    return FakeSnowparkSession(account(script, **options))


def _call(name: str, arguments: Any) -> str:  # Any: tool arguments
    return json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]})


def seth_answer(tool: dict[str, Any], arguments: dict[str, Any]) -> Any:  # Any: JSON
    """The tool output as SETH's `tool_runtime` returns it on Snowflake: `sql_request`'s Snowflake
    form of the stored template, invoked through `SnowflakeAdapter` on an equal sample account."""
    binding = {"sql_template": sql_template(tool["sql"], "snowflake"), "max_rows": tool["max_rows"]}
    request = sql_request(
        binding, arguments, input_schema(tool["parameters"]), "data_platform", "snowflake"
    )
    session = SnowflakeAdapter().open(ctx_for(make_snowflake(sample_data=True)), sf_conn("bearer"))
    try:
        result = session.invoke(request)
    finally:
        session.close()
    return {**result.result, **({"truncated": True} if result.truncated else {})}


# ------------------------------------------------------------------ agent loop and tools
@pytest.mark.parametrize("index", range(len(CASES)), ids=[c["case_id"] for c in CASES])
def test_each_case_is_answered_and_passes_its_assertions(app: App, index: int) -> None:
    case = Case.model_validate(CASES[index])
    answer = app.ask(session_on(), CASES[index]["input"]["message"])
    assert answer["tool_calls"] == 1
    assert answer["prompt_tokens"] > 0 and answer["completion_tokens"] > 0
    outcome = evaluate([a.model_dump() for a in case.assertions], answer["answer"])
    assert all(r["passed"] for r in outcome), outcome


@pytest.mark.parametrize("index", range(len(CASE_CALLS)))
def test_run_tool_returns_what_seths_snowflake_tool_path_returns(app: App, index: int) -> None:
    name, arguments, _, _ = CASE_CALLS[index]
    session = session_on()
    result = app.run_tool(session, name, arguments)
    expected = seth_answer(TOOLS[name], arguments)
    assert expected["row_count"] >= 1
    assert result == expected
    # the tool's positional SQL, its arguments in `param_order`, and no added row limit
    assert session.statements == [app.config["tools"][name]["sql"]]


def test_the_row_cap_keeps_max_rows_and_marks_truncated(app: App) -> None:
    tool = {**app.config["tools"]["orders_by_customer"], "max_rows": 1}
    result = app.loop.run_tool(
        app.backend.run_sql(session_on()), tool, {"customer_id": 1, "since": "2026-01-01"}
    )
    assert result["row_count"] == 1 and len(result["rows"]) == 1 and result["truncated"] is True


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (_call("no_such_tool", {}), "invalid_tool_call"),
        (
            _call("orders_by_customer", {"customer_id": "one", "since": "2026-01-01"}),
            "invalid_tool_call",
        ),
        (
            _call("orders_by_customer", {"customer_id": 1, "since": "2026-13-01"}),
            "invalid_tool_call",
        ),
        (_call("orders_by_customer", {"customer_id": 1}), "invalid_tool_call"),
        (_call("invoiced_by_region", {"region": "N" * 1001}), "invalid_tool_call"),
        (
            json.dumps({"tool_calls": [{"name": "invoiced_by_region", "arguments": {}}] * 9}),
            "malformed_tool_call",
        ),
        (_call("orders_by_customer", {"customer_id": 1, "since": "2026-01-01"}), "limit_exceeded"),
    ],
    ids=[
        "unknown-tool",
        "string-for-integer",
        "bad-date",
        "missing-required",
        "too-long",
        "nine-calls",
        "endless-loop",
    ],
)
def test_a_scripted_reply_stops_the_run_with_its_code(app: App, reply: str, code: str) -> None:
    # the match "" answers every message, so a valid call repeats until a limit stops it
    with pytest.raises(app.loop.Stop) as caught:
        app.ask(session_on([{"match": "", "reply": reply}]), "Go.")
    assert caught.value.code == code


def test_an_oversized_tool_result_is_refused(app_big: App) -> None:
    session = session_on([{"match": "", "reply": _call("big_text", {})}])
    with pytest.raises(app_big.loop.Stop) as caught:
        app_big.ask(session, "Go.")
    assert caught.value.code == "tool_result_too_large"
    with pytest.raises(app_big.loop.Stop) as caught:
        app_big.run_tool(session, "big_text", {})
    assert caught.value.code == "tool_result_too_large"


def test_the_deadline_stops_the_run(app: App) -> None:
    ticks = iter([0.0, 61.0])
    with pytest.raises(app.loop.Stop) as caught:
        app.ask(session_on(), "Go.", clock=lambda: next(ticks))
    assert caught.value.code == "limit_exceeded"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, None),
        (True, "true"),
        (False, "false"),
        (7, "7"),
        (Decimal("0E-10"), "0.0000000000"),
        (Decimal("9600.50"), "9600.50"),
        (datetime.date(2026, 1, 5), "2026-01-05"),
        (bytes([1, 171]), "01ab"),
        ("plain", "plain"),
    ],
)
def test_a_cell_is_sent_as_text(app: App, value: Any, text: str | None) -> None:
    assert app.backend.text_value(value) == text


@pytest.mark.parametrize("fault", ["warehouse_missing", "cortex_denied"])
def test_an_upstream_failure_carries_no_detail(app: App, fault: str) -> None:
    session = session_on(faults={fault})
    with pytest.raises(app.loop.UpstreamFailed) as caught:
        app.ask(session, CASES[0]["input"]["message"])
    assert str(caught.value) == "" and caught.value.args == ()
    if fault == "warehouse_missing":
        name, arguments, _, _ = CASE_CALLS[0]
        with pytest.raises(app.loop.UpstreamFailed):
            app.run_tool(session, name, arguments)


# ------------------------------------------------------------------ deep health
def _deep(app: App, session: FakeSnowparkSession) -> dict[str, Any]:  # Any: JSON
    return app.health.run(session, config_dir=str(app.ui / "config"))  # type: ignore[no-any-return]


def test_deep_health_passes_every_check(app: App) -> None:
    doc = _deep(app, session_on())
    assert set(doc) == {"status", "generator_version", "release_digest", "checks"}
    assert doc["status"] == "ok"
    assert doc["generator_version"] == "shifde.appgen.snowflake/1.0"
    assert doc["release_digest"] == SNOWFLAKE_DIGEST
    assert [c["name"] for c in doc["checks"]] == ["warehouse", "serving_endpoint", *TABLE_CHECKS]
    for check in doc["checks"]:
        assert check == {"name": check["name"], "status": "passed", "reason": None}


def test_deep_health_names_a_table_the_app_cannot_read(app: App) -> None:
    doc = _deep(app, session_on(faults={"table_denied:DEMO_DB.PUBLIC.ORDERS"}))
    assert doc["status"] == "failed"
    assert [c for c in doc["checks"] if c["status"] == "failed"] == [
        {
            "name": "table:DEMO_DB.PUBLIC.ORDERS",
            "status": "failed",
            "reason": "the app cannot read DEMO_DB.PUBLIC.ORDERS",
        }
    ]


def test_deep_health_names_the_cortex_model_without_the_grant(app: App) -> None:
    doc = _deep(app, session_on(faults={"cortex_denied"}))
    assert doc["status"] == "failed"
    [failed] = [c for c in doc["checks"] if c["status"] == "failed"]
    assert failed["name"] == "serving_endpoint"
    assert failed["reason"].startswith(f"the app cannot use Cortex model {CORTEX_MODEL}")
    assert "CORTEX_USER" in failed["reason"] and len(failed["reason"]) <= 200


def test_deep_health_names_the_warehouse_grant(app: App) -> None:
    doc = _deep(app, session_on(faults={"warehouse_missing"}))
    warehouse = doc["checks"][0]
    assert warehouse["name"] == "warehouse" and warehouse["status"] == "failed"
    assert "USAGE grant on the warehouse" in warehouse["reason"]


def test_deep_health_reads_the_snowflake_import_directory(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys._xoptions, "snowflake_import_directory", str(app.ui / "config"))
    assert app.health.run(session_on())["status"] == "ok"


# ------------------------------------------------------------------ the Streamlit app
@pytest.fixture
def streamlit_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[App, Any], AppTest]]:
    """`AppTest` of an app's `streamlit_app.py` whose `get_active_session()` is `session`: a stub
    `snowflake.snowpark.context` sits ahead of the installed package in `sys.modules` (removed
    afterwards), and the app's own siblings are imported from its directory."""

    def clear() -> None:
        for name in ("agent_loop", "backend_snowflake"):
            sys.modules.pop(name, None)

    def make(app: App, session: Any) -> AppTest:  # Any: a session
        stub = ModuleType("snowflake.snowpark.context")
        stub.get_active_session = lambda: session  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "snowflake.snowpark.context", stub)
        monkeypatch.syspath_prepend(str(app.ui))
        clear()
        return AppTest.from_file(str(app.ui / "streamlit_app.py"), default_timeout=60)

    yield make
    clear()


def _chat(at: AppTest, question: str) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    at.chat_input[0].set_value(question).run()
    assert not at.exception, at.exception
    return at


def _said(at: AppTest) -> list[tuple[str, str]]:
    return [(m.name, m.text[0].value) for m in at.chat_message]


def test_the_chat_screen_answers_and_records_its_usage(
    app: App, streamlit_app: Callable[[App, Any], AppTest]
) -> None:
    question = CASES[0]["input"]["message"]
    expected = app.ask(session_on(), question)
    session = session_on()
    at = _chat(streamlit_app(app, session), question)
    assert _said(at) == [("user", question), ("assistant", expected["answer"])]
    assert not at.error
    [row] = session.usage
    assert {k: v for k, v in row.items() if k != "at"} == {
        "release_digest": SNOWFLAKE_DIGEST,
        "model": CORTEX_MODEL,
        "prompt_tokens": expected["prompt_tokens"],
        "completion_tokens": expected["completion_tokens"],
    }
    assert isinstance(row["at"], datetime.datetime)


def test_a_failed_usage_write_is_the_upstream_error(
    app: App, streamlit_app: Callable[[App, Any], AppTest]
) -> None:
    session = session_on()
    session.fail_usage = True
    at = _chat(streamlit_app(app, session), CASES[0]["input"]["message"])
    assert [m.name for m in at.chat_message] == ["user"]  # no answer shown
    assert [e.value for e in at.error] == [
        "upstream_failed: the model or the warehouse did not answer"
    ]
    assert session.usage == []


def test_a_stop_shows_its_code_and_records_no_usage(
    app: App, streamlit_app: Callable[[App, Any], AppTest]
) -> None:
    session = session_on([{"match": "", "reply": _call("no_such_tool", {})}])
    at = _chat(streamlit_app(app, session), "Go.")
    assert [e.value for e in at.error] == ["The run stopped: invalid_tool_call"]
    assert session.usage == []


def test_the_table_screen_shows_the_tools_rows(
    app: App, streamlit_app: Callable[[App, Any], AppTest]
) -> None:
    at = streamlit_app(app, session_on())
    at.run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == [s["title"] for s in SNOWFLAKE_SAMPLE_DRAFT["screens"]]
    at.number_input(key="input-1-customer_id").set_value(1)
    at.date_input(key="input-1-since").set_value(datetime.date(2026, 1, 1))
    at.button[0].click().run()
    assert not at.exception, at.exception
    [frame] = at.dataframe
    expected = seth_answer(TOOLS["orders_by_customer"], {"customer_id": 1, "since": "2026-01-01"})
    assert list(frame.value.columns) == [c["name"] for c in expected["columns"]]
    assert frame.value.values.tolist() == expected["rows"]
    assert not at.caption  # not truncated


def test_the_generated_modules_import_no_seth_http_or_snowflake_library(app: App) -> None:
    for name in ("agent_loop.py", "backend_snowflake.py", "deep_health.py"):
        imported: set[str] = set()
        for node in ast.walk(ast.parse((app.ui / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported, name
        assert not imported & {"shifde", "httpx", "jsonschema", "snowflake"}, (name, imported)


# ------------------------------------------------------------------ cross-client usage
def test_each_accounts_usage_goes_only_to_its_own_table(
    tmp_path: Path, streamlit_app: Callable[[App, Any], AppTest]
) -> None:
    """Accounts A and B each run their own release's app: A's answers write usage only to A's
    `APP_DATA.USAGE` with A's digest, and B's table holds only B's rows."""
    manifests = {}
    for side in ("a", "b"):
        manifest = snowflake_manifest_of(SNOWFLAKE_SAMPLE_DRAFT)
        manifest["data_reads"][0]["connection_id"] = f"00000000-0000-0000-0000-00000000c0{side}0"
        manifests[side] = manifest
    apps = {
        side: _package(tmp_path / side, manifest, SNOWFLAKE_RESOLVED)
        for side, manifest in manifests.items()
    }
    digests = {side: digest(m) for side, m in manifests.items()}
    assert digests["a"] != digests["b"]
    sessions = {"a": session_on(), "b": session_on()}
    for case in CASES[:2]:
        _chat(streamlit_app(apps["a"], sessions["a"]), case["input"]["message"])
    assert [r["release_digest"] for r in sessions["a"].usage] == [digests["a"]] * 2
    assert sessions["b"].usage == []
    assert not any("APP_DATA.USAGE" in s for s in sessions["b"].statements)
    _chat(streamlit_app(apps["b"], sessions["b"]), CASES[2]["input"]["message"])
    assert [r["release_digest"] for r in sessions["b"].usage] == [digests["b"]]
    assert [r["release_digest"] for r in sessions["a"].usage] == [digests["a"]] * 2
