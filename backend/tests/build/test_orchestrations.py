"""build part 3b: the orchestration validator (`build.orchestrations`) and the lookups it makes
(cross-client).

SETH tested it only through the API (`test_p5_graph.py`), so these are new direct tests.
`validate_orchestration` never reads the database: an agent or tool node's version (a bare id or
`latest:{artifact}`) is an `Env` call, answered here by `RepoEnv` from the repositories with the
caller's client id. Client B's object must give the same issues (path, code, message) as an unknown
id and no pin; A's own object (control) is pinned. The lookup tests run as `shifde_app` and as the
schema owner (no row-level security: only the tenant id in the lookup confines the caller).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from shifde.build.env import VersionRef
from shifde.build.issues import Issue, Report
from shifde.build.orchestrations import template_exprs, validate_orchestration
from shifde.core.ids import uuid7
from tests.build.conftest import Side
from tests.build.env_support import RepoEnv

LIMITS = {
    "max_nodes_executed": 100,
    "max_depth": 2,
    "max_total_tokens": 10_000,
    "deadline_s": 600,
    "max_parallel": 4,
}


def _sig(issues: list[Issue]) -> list[tuple[str, str, str]]:
    return [(i.path, i.code, i.message) for i in issues]


def _spec(nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"nodes": nodes, "edges": edges or [], "limits": dict(LIMITS)}


def _agent(nid: str, ref: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": nid,
        "type": "agent",
        "ref": {"agent_version_id": ref},
        "input_map": {"input": "hello"},
    }
    return node | extra


def _tool(nid: str, ref: str) -> dict[str, Any]:
    return {"id": nid, "type": "tool", "ref": {"tool_version_id": ref}, "input_map": {"item": "x"}}


def _run(
    env: RepoEnv, spec: dict[str, Any], *, resolve: bool = True
) -> tuple[Report, dict[str, Any], list[tuple[VersionRef, str]]]:
    report = Report()
    out, pins = validate_orchestration(spec, env, report, 8, resolve=resolve)
    return report, out, pins


# ------------------------------------------------------------------ cross-client lookups
# (label, A's object as the reference text, the node naming it, pin role, kind of the pin)
CASES: list[tuple[str, Callable[[Side], str], Callable[[str], dict[str, Any]], str, str]] = [
    ("agent version", lambda s: str(s.agent_version), lambda r: _agent("n", r), "agent", "agents"),
    (
        "agent latest",
        lambda s: f"latest:{s.agent_artifact}",
        lambda r: _agent("n", r),
        "agent",
        "agents",
    ),
    ("tool version", lambda s: str(s.tool_version), lambda r: _tool("n", r), "tool", "tools"),
    (
        "tool latest",
        lambda s: f"latest:{s.tool_artifact}",
        lambda r: _tool("n", r),
        "tool",
        "tools",
    ),
]


@pytest.mark.parametrize(("label", "own", "node", "role", "kind"), CASES, ids=[c[0] for c in CASES])
def test_a_node_naming_bs_version_gets_the_unknown_id_outcome(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    label: str,
    own: Callable[[Side], str],
    node: Callable[[str], dict[str, Any]],
    role: str,
    kind: str,
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = RepoEnv(s, a.tenant)
        # A's own object: no error, pinned with its role, the stored spec names the version id
        report, out, pins = _run(env, _spec([node(own(a))]))
        assert report.errors == [], (label, _sig(report.errors))
        assert [(p[0].kind, p[1]) for p in pins] == [(kind, role)], label
        assert pins[0][0].artifact_key.endswith("-a"), label
        (stored,) = out["nodes"][0]["ref"].values()
        assert stored == str(pins[0][0].version_id), label
        # B's object: the same issues as an unknown id, nothing pinned, the reference as written
        theirs = own(b)
        unknown = f"latest:{uuid7()}" if theirs.startswith("latest:") else str(uuid7())
        their_report, their_out, their_pins = _run(env, _spec([node(theirs)]))
        unknown_report, _, unknown_pins = _run(env, _spec([node(unknown)]))
        assert their_report.errors, label
        assert _sig(their_report.errors) == _sig(unknown_report.errors), label
        assert _sig(their_report.warnings) == _sig(unknown_report.warnings), label
        assert their_pins == unknown_pins == [], label
        assert their_out["nodes"][0]["ref"] == node(theirs)["ref"], label
        for b_id in (b.agent_version, b.tool_version, b.agent_artifact, b.tool_artifact):
            assert str(b_id) not in repr(_sig(their_report.errors)).replace(theirs, ""), label
        s.rollback()


def test_latest_is_never_resolved_for_a_published_orchestration(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = RepoEnv(s, a.tenant)
        for side in (a, b):
            report, _, pins = _run(
                env, _spec([_agent("n", f"latest:{side.agent_artifact}")]), resolve=False
            )
            assert [(e.path, e.code) for e in report.errors] == [
                ("nodes[0].ref.agent_version_id", "unpinned_reference")
            ]
            assert pins == []
        s.rollback()


def test_a_tool_version_given_where_an_agent_is_expected_is_refused_for_both_clients(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = RepoEnv(s, a.tenant)
        own, _, own_pins = _run(env, _spec([_agent("n", str(a.tool_version))]))
        theirs, _, pins = _run(env, _spec([_agent("n", str(b.tool_version))]))
        unknown, _, _ = _run(env, _spec([_agent("n", str(uuid7()))]))
        # A's own wrong-kind version is a different problem from "not found"; B's is "not found"
        assert own.errors and own_pins == []
        assert [e.code for e in own.errors] != [e.code for e in theirs.errors]
        assert _sig(theirs.errors) == _sig(unknown.errors)
        assert pins == []
        s.rollback()


def test_a_whole_graph_pins_only_the_callers_objects(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = RepoEnv(s, a.tenant)
        spec = _spec(
            [_agent("one", str(a.agent_version)), _tool("two", str(b.tool_version))],
            [{"from": "one", "to": "two"}],
        )
        report, _, pins = _run(env, spec)
        assert [e.path for e in report.errors] == ["nodes[1].ref.tool_version_id"]
        assert [p[0].version_id for p in pins] == [a.agent_version]
        s.rollback()


# ------------------------------------------------------------------ structure
@pytest.fixture
def env(sides: tuple[Side, Side], owner_session: Session) -> RepoEnv:
    return RepoEnv(owner_session, sides[0].tenant)


def _codes(report: Report) -> set[tuple[str, str]]:
    return {(e.path, e.code) for e in report.errors}


def test_a_valid_graph_is_pinned_and_normalised(env: RepoEnv, sides: tuple[Side, Side]) -> None:
    a, _ = sides
    spec = _spec(
        [
            _agent("start", f"latest:{a.agent_artifact}"),
            {"id": "gate", "type": "condition", "condition": "nodes.start.output.ok"},
            _tool("act", str(a.tool_version)),
            {"id": "ask", "type": "approval"},
        ],
        [
            {"from": "start", "to": "gate"},
            {"from": "gate", "to": "act", "when": "true"},
            {"from": "gate", "to": "ask", "when": "false"},
        ],
    )
    spec["ui"] = {"positions": {"start": {"x": 0, "y": 1.5}}}
    spec["extra"] = 1
    report, out, pins = _run(env, spec)
    assert report.errors == []
    assert [(w.code, w.path) for w in report.warnings] == [("unknown_field", "extra")]
    assert [p[1] for p in pins] == ["agent", "tool"]
    assert out["nodes"][0]["ref"] == {"agent_version_id": str(a.agent_version)}


def _plus(node: dict[str, Any]) -> Callable[[dict[str, Any]], object]:
    return lambda s: s["nodes"].append(node)


def _cycle(s: dict[str, Any]) -> None:
    s["nodes"].append({"id": "m", "type": "approval"})
    s["edges"].extend([{"from": "n", "to": "m"}, {"from": "m", "to": "n"}])


def _when(s: dict[str, Any]) -> None:
    s["nodes"].append({"id": "m", "type": "approval"})
    s["edges"].append({"from": "n", "to": "m", "when": "true"})


STRUCTURE: list[tuple[str, Callable[[dict[str, Any]], object], set[tuple[str, str]]]] = [
    ("no nodes", lambda s: s.update(nodes=[]), {("nodes", "required")}),
    ("bad id", _plus({"id": "1x", "type": "approval"}), {("nodes[1].id", "invalid_value")}),
    ("duplicate id", _plus({"id": "n", "type": "approval"}), {("nodes[1].id", "duplicate")}),
    ("unknown type", _plus({"id": "m", "type": "magic"}), {("nodes[1].type", "invalid_value")}),
    (
        "edge to nowhere",
        lambda s: s["edges"].append({"from": "n", "to": "zz"}),
        {("edges[0]", "unknown_node")},
    ),
    (
        "self edge",
        lambda s: s["edges"].append({"from": "n", "to": "n"}),
        {("edges[0]", "cycle")},
    ),
    ("cycle", _cycle, {("edges", "cycle")}),
    ("when on a plain edge", _when, {("edges[0].when", "invalid_value")}),
    (
        "condition missing",
        _plus({"id": "c", "type": "condition"}),
        {("nodes[1].condition", "required")},
    ),
    (
        "condition on a non-condition node",
        _plus({"id": "m", "type": "approval", "condition": "true"}),
        {("nodes[1].condition", "unexpected")},
    ),
    (
        "reference to a node not upstream",
        _plus({"id": "m", "type": "approval", "input_map": {"v": "{{ nodes.n.output }}"}}),
        {("nodes[1]", "reference_not_upstream")},
    ),
    (
        "reference to an unknown node",
        _plus({"id": "m", "type": "approval", "input_map": {"v": "{{ nodes.zz.output }}"}}),
        {("nodes[1].input_map", "unknown_node")},
    ),
    (
        "expression reading something else",
        _plus({"id": "m", "type": "approval", "input_map": {"v": "{{ secrets.k }}"}}),
        {("nodes[1].input_map", "invalid_expression")},
    ),
    ("limits missing", lambda s: s.pop("limits"), {("limits", "required")}),
    (
        "limit over the maximum",
        lambda s: s["limits"].update(max_depth=9),
        {("limits.max_depth", "exceeds_maximum")},
    ),
    (
        "limit over the client's parallel cap",
        lambda s: s["limits"].update(max_parallel=9),
        {("limits.max_parallel", "exceeds_maximum")},
    ),
    (
        "ref on an approval node",
        _plus({"id": "m", "type": "approval", "ref": {"x": 1}}),
        {("nodes[1].ref", "unexpected")},
    ),
    (
        "branches on a non-parallel node",
        _plus({"id": "m", "type": "approval", "branches": ["n"]}),
        {("nodes[1].branches", "unexpected")},
    ),
    (
        "bad ui",
        lambda s: s.update(ui={"positions": {"n": {"x": "a", "y": 1}}}),
        {("ui.positions.n", "invalid_value")},
    ),
]


@pytest.mark.parametrize(("label", "mutate", "expected"), STRUCTURE, ids=[c[0] for c in STRUCTURE])
def test_structural_errors_carry_paths_and_codes(
    env: RepoEnv,
    sides: tuple[Side, Side],
    label: str,
    mutate: Callable[[dict[str, Any]], object],
    expected: set[tuple[str, str]],
) -> None:
    spec = _spec([_agent("n", str(sides[0].agent_version))])
    mutate(spec)
    report, _, _ = _run(env, spec)
    assert expected <= _codes(report), (label, _codes(report))


def test_parallel_node_rules(env: RepoEnv, sides: tuple[Side, Side]) -> None:
    ref = str(sides[0].agent_version)
    good = _spec(
        [
            {"id": "fan", "type": "parallel", "branches": ["x", "y"], "max_parallel": 2},
            _agent("x", ref),
            _agent("y", ref),
        ]
    )
    assert _run(env, good)[0].errors == []
    one = _spec([{"id": "fan", "type": "parallel", "branches": ["x"]}, _agent("x", ref)])
    assert ("nodes[0].branches", "required") in _codes(_run(env, one)[0])
    tool_branch = _spec(
        [
            {"id": "fan", "type": "parallel", "branches": ["x", "t"]},
            _agent("x", ref),
            _tool("t", str(sides[0].tool_version)),
        ]
    )
    assert ("nodes[0].branches[1]", "invalid_branch") in _codes(_run(env, tool_branch)[0])
    wired = _spec(
        [
            {"id": "fan", "type": "parallel", "branches": ["x", "y"]},
            _agent("x", ref),
            _agent("y", ref),
        ],
        [{"from": "fan", "to": "x"}],
    )
    assert ("nodes[1]", "branch_has_edges") in _codes(_run(env, wired)[0])
    too_wide = _spec(
        [
            {"id": "fan", "type": "parallel", "branches": ["x", "y"], "max_parallel": 5},
            _agent("x", ref),
            _agent("y", ref),
        ]
    )
    too_wide["limits"]["max_parallel"] = 2
    assert ("nodes[0].max_parallel", "exceeds_maximum") in _codes(_run(env, too_wide)[0])


def test_tool_and_agent_input_maps_are_checked_against_the_pinned_spec(
    env: RepoEnv, sides: tuple[Side, Side]
) -> None:
    a, _ = sides
    spec = _spec(
        [
            {"id": "x", "type": "agent", "ref": {"agent_version_id": str(a.agent_version)}},
            {"id": "t", "type": "tool", "ref": {"tool_version_id": str(a.tool_version)}},
        ],
        [{"from": "x", "to": "t"}],
    )
    codes = _codes(_run(env, spec)[0])
    assert ("nodes[0].input_map.input", "required") in codes
    assert ("nodes[1].input_map", "missing_arguments") in codes
    extra = _spec([_agent("x", str(a.agent_version), input_map={"input": "i", "bogus": 1})])
    assert ("nodes[0].input_map", "unknown_field") in _codes(_run(env, extra)[0])


def test_template_exprs_collects_every_expression_at_any_depth() -> None:
    value = {
        "a": "x {{ input.q }} y",
        "b": ["{{ nodes.n.output }}", {"c": "{{a}} {{ b }}"}],
        "d": 1,
    }
    assert template_exprs(value) == ["input.q", "nodes.n.output", "a", "b"]
    assert template_exprs("no templates") == [] and template_exprs(None) == []
