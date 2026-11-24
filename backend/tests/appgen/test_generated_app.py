"""SP5-5 conformance (design 6.4, 10): the app rendered from the `SAMPLE_DRAFT` release, imported
from a temporary directory and driven with Flask's test client, on a real
`databricks.sdk.WorkspaceClient` whose requests reach `FakeDatabricksWorkspace(sample_data=True)`
through a test-only transport adapter (no socket). The three cases are answered and pass their
assertions; `/api/tools` returns what SETH's tool path returns; the stop codes, the deep-health
document and its failures, the static and config routes, the one usage line per answer, and the
standalone imports of the generated service."""

from __future__ import annotations

import ast
import copy
import json
from collections.abc import Callable
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: JSON documents

import pytest

from shifde.appgen.render import render
from shifde.evaluation.assertions import evaluate
from shifde.evaluation.schemas import Case
from tests._support.appgen_fixtures import AGENT_SCRIPT, CASE_CALLS, SAMPLE_DRAFT
from tests.adapters.dataplatforms.helpers import WAREHOUSE
from tests.appgen.rendered import (
    ENDPOINT,
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    fake_workspace,
    input_schema,
    load_module,
    seth_answer,
    workspace_client,
    write_files,
)

CASES = SAMPLE_DRAFT["evaluation_cases"]
TOOLS = {t["name"]: t for t in SAMPLE_DRAFT["tools"]}
BIG_SQL = "select replace(hex(zeroblob(140000)), '0', 'x') as big"  # 280 000 characters
TABLE_CHECKS = [
    "table:main.default.customers",
    "table:main.default.orders",
    "table:main.sales.invoices",
]
_names = count()


def _rendered(tmp_path_factory: pytest.TempPathFactory, resolved: dict[str, Any]) -> ModuleType:
    directory = write_files(
        tmp_path_factory.mktemp("app"), render(SAMPLE_MANIFEST, SAMPLE_DIGEST, resolved)
    )
    return load_module(Path(directory, "app", "app.py"), f"generated_app_{next(_names)}")


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    return _rendered(tmp_path_factory, SAMPLE_RESOLVED)


@pytest.fixture(scope="module")
def generated_big(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    resolved = copy.deepcopy(SAMPLE_RESOLVED)
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
    return _rendered(tmp_path_factory, resolved)


Client = Callable[..., Any]  # Any: a Flask test client


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch) -> Client:
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", WAREHOUSE)
    monkeypatch.setenv("SERVING_ENDPOINT", ENDPOINT)

    def make(
        module: ModuleType, serving: list[dict[str, str]], faults: set[str] | None = None
    ) -> Any:  # Any: a Flask test client
        workspace = workspace_client(fake_workspace(serving, faults), monkeypatch)
        return module.create_app(workspace_client=workspace).test_client()

    return make


def _call(name: str, arguments: Any) -> str:  # Any: tool arguments
    return json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]})


def _usage_lines(out: str) -> list[dict[str, Any]]:  # Any: JSON
    return [json.loads(line) for line in out.splitlines() if line.startswith('{"event":"usage"')]


@pytest.mark.parametrize("index", range(len(CASES)), ids=[c["case_id"] for c in CASES])
def test_each_case_is_answered_and_passes_its_assertions(
    generated: ModuleType, client_for: Client, capsys: pytest.CaptureFixture[str], index: int
) -> None:
    case = Case.model_validate(CASES[index])
    client = client_for(generated, AGENT_SCRIPT)
    capsys.readouterr()
    response = client.post("/api/ask", json={"message": CASES[index]["input"]["message"]})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert set(body) == {"answer", "tool_calls"} and body["tool_calls"] == 1
    outcome = evaluate([a.model_dump() for a in case.assertions], body["answer"])
    assert all(r["passed"] for r in outcome), outcome
    out = capsys.readouterr().out
    [usage] = _usage_lines(out)
    assert set(usage) == {"event", "prompt_tokens", "completion_tokens", "endpoint"}
    assert usage["endpoint"] == ENDPOINT and usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert out.strip().splitlines() == [json.dumps(usage, separators=(",", ":"))]


@pytest.mark.parametrize("index", range(len(CASE_CALLS)))
def test_the_tools_route_returns_what_seths_tool_path_returns(
    generated: ModuleType, client_for: Client, index: int
) -> None:
    name, arguments, _, _ = CASE_CALLS[index]
    response = client_for(generated, AGENT_SCRIPT).post(
        f"/api/tools/{name}", json={"params": arguments}
    )
    assert response.status_code == 200
    expected = seth_answer(TOOLS[name], arguments)
    assert expected["row_count"] >= 1
    assert response.get_json() == expected


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
        (
            _call("orders_by_customer", {"customer_id": True, "since": "2026-01-01"}),
            "invalid_tool_call",
        ),
        (_call("orders_by_customer", {"customer_id": 1}), "invalid_tool_call"),
        (
            _call("orders_by_customer", {"customer_id": 1, "since": "2026-01-01", "x": 1}),
            "invalid_tool_call",
        ),
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
        "bool-for-integer",
        "missing-required",
        "extra-argument",
        "too-long",
        "nine-calls",
        "endless-loop",
    ],
)
def test_a_scripted_reply_stops_the_run_with_its_code(
    generated: ModuleType,
    client_for: Client,
    capsys: pytest.CaptureFixture[str],
    reply: str,
    code: str,
) -> None:
    # the match "" answers every message, so a valid call repeats until a limit stops it
    client = client_for(generated, [{"match": "", "reply": reply}])
    capsys.readouterr()
    response = client.post("/api/ask", json={"message": "Go."})
    assert response.status_code == 422
    assert response.get_json() == {"error": code}
    assert _usage_lines(capsys.readouterr().out) == []  # only an answered ask logs usage


def test_an_oversized_tool_result_is_refused(generated_big: ModuleType, client_for: Client) -> None:
    client = client_for(generated_big, [{"match": "", "reply": _call("big_text", {})}])
    response = client.post("/api/ask", json={"message": "Go."})
    assert (response.status_code, response.get_json()) == (422, {"error": "tool_result_too_large"})
    response = client.post("/api/tools/big_text", json={"params": {}})
    assert (response.status_code, response.get_json()) == (422, {"error": "tool_result_too_large"})


def test_the_tools_route_refuses_unknown_tools_and_bad_params(
    generated: ModuleType, client_for: Client
) -> None:
    client = client_for(generated, AGENT_SCRIPT)
    response = client.post("/api/tools/no_such_tool", json={"params": {}})
    assert (response.status_code, response.get_json()) == (404, {"error": "unknown_tool"})
    for params in ({"region": 5}, {}, {"region": "North", "x": 1}, "North"):
        response = client.post("/api/tools/invoiced_by_region", json={"params": params})
        assert (response.status_code, response.get_json()) == (422, {"error": "invalid_tool_call"})


@pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": 7}, {"message": "x" * 4001}])
def test_a_bad_question_is_400(generated: ModuleType, client_for: Client, body: Any) -> None:
    response = client_for(generated, AGENT_SCRIPT).post("/api/ask", json=body)
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/tools/invoiced_by_region", "/api/ask"])
def test_an_oversized_body_is_413_too_large(
    generated: ModuleType, client_for: Client, path: str
) -> None:
    assert generated.MAX_BODY == 1024 * 1024
    body = {"message": "Go.", "params": {"x": "a" * (generated.MAX_BODY + 1)}}
    response = client_for(generated, AGENT_SCRIPT).post(path, json=body)
    assert (response.status_code, response.get_json()) == (413, {"error": "request_too_large"})


def test_an_upstream_failure_is_502_without_detail(
    generated: ModuleType, client_for: Client
) -> None:
    client = client_for(generated, AGENT_SCRIPT, {"warehouse_missing"})
    name, arguments, _, _ = CASE_CALLS[0]
    response = client.post(f"/api/tools/{name}", json={"params": arguments})
    assert (response.status_code, response.get_json()) == (502, {"error": "upstream_failed"})
    response = client.post("/api/ask", json={"message": CASES[0]["input"]["message"]})
    assert (response.status_code, response.get_json()) == (502, {"error": "upstream_failed"})


def _deep(client: Any) -> tuple[int, dict[str, Any]]:  # Any: a Flask test client, JSON
    response = client.get("/health?deep=1")
    return response.status_code, response.get_json()


def test_deep_health_passes_every_check(generated: ModuleType, client_for: Client) -> None:
    status, doc = _deep(client_for(generated, AGENT_SCRIPT))
    assert status == 200
    assert set(doc) == {"status", "generator_version", "release_digest", "checks"}
    assert doc["status"] == "ok"
    assert doc["generator_version"] == "shifde.appgen/1.0"
    assert doc["release_digest"] == SAMPLE_DIGEST
    assert [c["name"] for c in doc["checks"]] == ["warehouse", "serving_endpoint", *TABLE_CHECKS]
    for check in doc["checks"]:
        assert check == {"name": check["name"], "status": "passed", "reason": None}


def test_deep_health_fails_only_the_serving_endpoint_when_not_ready(
    generated: ModuleType, client_for: Client
) -> None:
    status, doc = _deep(client_for(generated, AGENT_SCRIPT, {"endpoint_not_ready"}))
    assert status == 503 and doc["status"] == "failed"
    failed = [c for c in doc["checks"] if c["status"] == "failed"]
    assert [c["name"] for c in failed] == ["serving_endpoint"]
    assert isinstance(failed[0]["reason"], str) and 0 < len(failed[0]["reason"]) <= 200


def test_deep_health_names_a_table_the_app_cannot_read(
    generated: ModuleType, client_for: Client
) -> None:
    client = client_for(generated, AGENT_SCRIPT, {"table_denied:main.default.orders"})
    status, doc = _deep(client)
    assert status == 503 and doc["status"] == "failed"
    failed = [c for c in doc["checks"] if c["status"] == "failed"]
    assert failed == [
        {
            "name": "table:main.default.orders",
            "status": "failed",
            "reason": "the app cannot read main.default.orders",
        }
    ]


def test_shallow_health_is_sp2s(generated: ModuleType, client_for: Client) -> None:
    response = client_for(generated, AGENT_SCRIPT).get("/health")
    assert (response.status_code, response.get_json()) == (200, {"status": "ok", "warehouse": "ok"})
    response = client_for(generated, AGENT_SCRIPT, {"warehouse_missing"}).get("/health")
    assert (response.status_code, response.get_json()) == (
        503,
        {"status": "failed", "warehouse": "failed"},
    )


def test_the_bundle_and_only_the_screens_config_are_served(
    generated: ModuleType, client_for: Client
) -> None:
    client = client_for(generated, AGENT_SCRIPT)
    static = Path(str(generated.STATIC))
    index = client.get("/")
    assert index.status_code == 200 and index.data == (static / "index.html").read_bytes()
    script = client.get("/assets/index.js")
    assert (
        script.status_code == 200 and script.data == (static / "assets" / "index.js").read_bytes()
    )
    assert client.get("/assets/index.css").status_code == 200
    screens = client.get("/config/screens.json")
    assert screens.status_code == 200 and screens.get_json() == SAMPLE_DRAFT["screens"]
    for path in ("/config/tools.json", "/config/agent.json", "/config/limits.json", "/app.py"):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("name", ["app.py", "agent_loop.py"])
def test_the_generated_service_imports_no_seth_module(generated: ModuleType, name: str) -> None:
    source = Path(str(generated.HERE), name).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, name
    assert not imported & {"shifde", "jsonschema", "httpx"}, imported


def test_the_deadline_stops_the_run(
    generated: ModuleType, client_for: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = generated.agent_loop
    config = loop.load_config(Path(str(generated.CONFIG)))
    ticks = iter([0.0, 61.0])
    workspace = workspace_client(fake_workspace(AGENT_SCRIPT), monkeypatch)
    with pytest.raises(loop.Stop) as caught:
        loop.ask(workspace, config, WAREHOUSE, ENDPOINT, "Go.", clock=lambda: next(ticks))
    assert caught.value.code == "limit_exceeded"


def test_config_load_refuses_a_keyword_the_validator_does_not_implement(
    generated: ModuleType, tmp_path: Path
) -> None:
    loop = generated.agent_loop
    config = Path(str(generated.CONFIG))
    for name in ("agent.json", "limits.json", "tools.json"):
        (tmp_path / name).write_bytes((config / name).read_bytes())
    tools = json.loads((tmp_path / "tools.json").read_text(encoding="utf-8"))
    tools[0]["input_schema"]["properties"]["region"]["enum"] = ["North"]
    (tmp_path / "tools.json").write_text(json.dumps(tools), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported input schema keywords"):
        loop.load_config(tmp_path)
