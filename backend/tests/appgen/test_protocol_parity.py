"""SP5-5 (settled call S4): the rendered `agent_loop.py` speaks SETH's tool protocol. Its
`PROTOCOL` and `MAX_CALLS_PER_RESPONSE` equal `shifde.runtime.agent`'s, its `parse_tool_calls` and
`untrusted_block` behave the same on fixed corpora, and the system message it builds for the
sample tools equals `AgentRunner._with_protocol` for the same tools (key = name = tool name)."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: JSON documents
from uuid import NAMESPACE_URL, uuid5

import pytest

from shifde.appgen.render import render
from shifde.build.env import VersionRef
from shifde.runtime import agent
from tests._support.appgen_fixtures import AGENT_SCRIPT
from tests.appgen.rendered import (
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    load_module,
    write_files,
)


def _call(name: Any, arguments: Any = None) -> dict[str, Any]:  # Any: JSON
    return {"name": name, "arguments": {} if arguments is None else arguments}


CORPUS = [
    "The answer is 42.",
    "",
    "   ",
    json.dumps({"tool_calls": [_call("orders_by_customer", {"customer_id": 1})]}),
    "```\n" + json.dumps({"tool_calls": [_call("a")]}) + "\n```",
    "```json\n" + json.dumps({"tool_calls": [_call("a"), _call("b")]}) + "\n```",
    "```json " + json.dumps({"tool_calls": [_call("a")]}) + " ```",
    json.dumps({"tool_calls": []}),
    json.dumps({"tool_calls": [_call(f"t{i}") for i in range(8)]}),
    json.dumps({"tool_calls": [_call(f"t{i}") for i in range(9)]}),
    json.dumps({"tool_calls": [{"arguments": {}}]}),
    json.dumps({"tool_calls": [{"name": 7}]}),
    json.dumps({"tool_calls": [_call("a", [1, 2])]}),
    json.dumps({"tool_calls": [{"name": "a"}]}),  # arguments default to {}
    json.dumps({"tool_calls": ["a"]}),
    json.dumps({"tool_calls": {"name": "a"}}),
    '{"tool_calls": [ {"name": "a",',  # invalid JSON
    json.dumps({"answer": "no tools here"}),
    json.dumps([_call("a")]),
    "Use the tool: " + json.dumps({"tool_calls": [_call("a")]}),
    *[entry["reply"] for entry in AGENT_SCRIPT],
]

VALUES = [
    None,
    0,
    "plain",
    "<script>alert('x')</script> & </untrusted>",
    {"columns": ["id"], "rows": [["1"]], "row_count": 1},
    {"columns": ["a"], "rows": [], "row_count": 0, "truncated": True},
    ["ünïcode", 1.5, True],
]
SOURCES = ["tool:orders_by_customer", 'tool:"quoted"&<x>']


@pytest.fixture(scope="module")
def loop(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    directory = write_files(
        tmp_path_factory.mktemp("parity"),
        render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED),
    )
    return load_module(Path(directory, "app", "agent_loop.py"), "rendered_agent_loop_parity")


def test_the_protocol_text_and_call_cap_are_seths(loop: ModuleType) -> None:
    assert loop.PROTOCOL == agent.PROTOCOL
    assert loop.MAX_CALLS_PER_RESPONSE == agent.MAX_CALLS_PER_RESPONSE == 8
    assert loop.FENCE.pattern == agent.FENCE.pattern and loop.FENCE.flags == agent.FENCE.flags


def _definitions(source: str) -> dict[str, str]:
    names = {"parse_tool_calls", "untrusted_block", "PROTOCOL", "MAX_CALLS_PER_RESPONSE", "FENCE"}
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out[node.name] = ast.dump(node)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
        ):
            out[node.targets[0].id] = ast.dump(node)
    return out


def test_the_protocol_definitions_are_verbatim_copies(loop: ModuleType) -> None:
    copied = _definitions(Path(str(loop.__file__)).read_text(encoding="utf-8"))
    assert len(copied) == 5
    assert copied == _definitions(inspect.getsource(agent))


def _outcome(parse: Any, text: str) -> Any:  # Any: parsed calls or the error
    try:
        return ("ok", parse(text))
    except ValueError as exc:
        return ("ValueError", str(exc))


@pytest.mark.parametrize("text", CORPUS, ids=[f"text-{i}" for i in range(len(CORPUS))])
def test_parse_tool_calls_matches_seths(loop: ModuleType, text: str) -> None:
    assert _outcome(loop.parse_tool_calls, text) == _outcome(agent.parse_tool_calls, text)


def test_the_corpus_covers_calls_answers_and_refusals() -> None:
    outcomes = [_outcome(agent.parse_tool_calls, t) for t in CORPUS]
    assert len(CORPUS) >= 15
    assert any(o == ("ok", None) for o in outcomes)
    assert any(o[0] == "ok" and o[1] for o in outcomes)
    assert sum(o[0] == "ValueError" for o in outcomes) >= 5


@pytest.mark.parametrize("value", VALUES, ids=[f"value-{i}" for i in range(len(VALUES))])
@pytest.mark.parametrize("source", SOURCES)
def test_untrusted_block_matches_seths(loop: ModuleType, source: str, value: Any) -> None:
    assert loop.untrusted_block(source, "3.1", value) == agent.untrusted_block(source, "3.1", value)


def test_the_system_message_equals_seths_with_protocol(loop: ModuleType) -> None:
    instructions = SAMPLE_RESOLVED["agent"]["instructions"]
    tools = {t["name"]: t for t in SAMPLE_RESOLVED["tools"]}
    refs = {
        name: VersionRef(
            version_id=uuid5(NAMESPACE_URL, name),
            artifact_id=uuid5(NAMESPACE_URL, "artifact:" + name),
            kind="tools",
            version_no=1,
            state="published",
            spec={"input_schema": t["input_schema"]},
            spec_hash=None,
            artifact_key=name,
            artifact_name=name,
            artifact_status="active",
        )
        for name, t in tools.items()
    }
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": "q"}]
    [system, user] = agent.AgentRunner._with_protocol(messages, refs)
    assert user == {"role": "user", "content": "q"}
    assert loop.system_message(instructions, tools) == system["content"]
    assert loop.system_message(instructions, {}) == instructions
