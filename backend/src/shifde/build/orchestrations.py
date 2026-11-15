"""Orchestration specs: publish-time validation (contract section 4, SYSTEM_ARCHITECTURE 6.6).

An orchestration is a constrained DAG of ``agent`` / ``tool`` / ``condition`` / ``approval`` /
``parallel`` nodes. Validation guarantees what the graph executor relies on: no cycles, every
reference pinned to a published same-tenant version, conditions and input maps parse in the safe
expression language and only read ``input`` or *upstream* node outputs, ``parallel`` fan-out is
bounded by the tenant cap, and hard run limits exist. ``ui.positions`` is presentation only.
"""

from __future__ import annotations

import re
from typing import Any  # Any: stored JSON specs

from shifde.build.agents import _resolve
from shifde.build.env import Env, VersionRef
from shifde.build.issues import IDENT, Report, schema_properties
from shifde.core import safeexpr

KNOWN_KEYS = frozenset({"nodes", "edges", "limits", "ui"})
NODE_TYPES = ("agent", "tool", "condition", "approval", "parallel")
WHEN = ("true", "false", "always")
MAX_NODES = 100
DEFAULT_PARALLEL_CAP = 8
LIMIT_MAXIMA: dict[str, int] = {
    "max_nodes_executed": 1000,
    "max_depth": 3,
    "max_total_tokens": 5_000_000,
    "deadline_s": 86_400,
    "max_parallel": 64,
}
TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.S)


def _int_ok(value: Any) -> bool:  # Any: JSON value
    return isinstance(value, int) and not isinstance(value, bool)


def template_exprs(value: Any) -> list[str]:  # Any: JSON template
    """Every ``{{ expr }}`` of the strings of an input map (any depth)."""
    if isinstance(value, str):
        return [m.strip() for m in TEMPLATE.findall(value)]
    if isinstance(value, dict):
        return [e for v in value.values() for e in template_exprs(v)]
    if isinstance(value, list):
        return [e for v in value for e in template_exprs(v)]
    return []


def _node_refs(expr: str, report: Report, path: str, ids: set[str]) -> set[str]:
    """Parse `expr`, check its roots and return the node ids it reads."""
    try:
        tree = safeexpr.parse(expr)
    except safeexpr.ExprError as exc:
        report.error(path, "invalid_expression", str(exc))
        return set()
    out: set[str] = set()
    for p in safeexpr.paths(tree):
        root = p[0]
        if root == "input":
            continue
        if root != "nodes":
            report.error(path, "invalid_expression", "only `input` and `nodes.<id>` can be read")
            continue
        if len(p) < 2 or not isinstance(p[1], str):
            report.error(path, "invalid_expression", "write nodes.<node id>.output...")
        elif p[1] not in ids:
            report.error(path, "unknown_node", f"no node {p[1]!r}")
        else:
            out.add(p[1])
    return out


def validate_orchestration(
    spec: dict[str, Any],
    env: Env,
    report: Report,
    parallel_cap: int,
    *,
    resolve: bool = True,
) -> tuple[dict[str, Any], list[tuple[VersionRef, str]]]:
    """Validate an orchestration spec. Returns (spec with every reference pinned, (pin, role))."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    out: dict[str, Any] = dict(spec)
    pins: list[tuple[VersionRef, str]] = []

    nodes_raw = spec.get("nodes")
    nodes: list[dict[str, Any]] = []
    if not isinstance(nodes_raw, list) or not nodes_raw:
        report.error("nodes", "required", "at least one node is required")
        nodes_raw = []
    elif len(nodes_raw) > MAX_NODES:
        report.error("nodes", "too_many", f"at most {MAX_NODES} nodes")
        nodes_raw = nodes_raw[:MAX_NODES]
    ids: list[str] = []
    for i, n in enumerate(nodes_raw):
        if not isinstance(n, dict):
            report.error(f"nodes[{i}]", "invalid_type", "must be an object")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not IDENT.match(nid):
            report.error(f"nodes[{i}].id", "invalid_value", "must be an identifier")
            continue
        if nid in ids:
            report.error(f"nodes[{i}].id", "duplicate", f"node id {nid!r} is used twice")
            continue
        ids.append(nid)
        nodes.append(n)
    idset = set(ids)
    by_id = {n["id"]: n for n in nodes}

    edges = _validate_edges(spec.get("edges"), by_id, report)
    _topological(ids, edges, report)
    ancestors = _ancestors(ids, edges)
    limits = _validate_limits(spec.get("limits"), parallel_cap, report)
    branch_owner: dict[str, str] = {}

    out_nodes: list[dict[str, Any]] = []
    for n in nodes:
        nid = n["id"]
        path = f"nodes[{ids.index(nid)}]"
        ntype = n.get("type")
        node = dict(n)
        if ntype not in NODE_TYPES:
            report.error(f"{path}.type", "invalid_value", f"must be one of {', '.join(NODE_TYPES)}")
            out_nodes.append(node)
            continue
        ref = n.get("ref")
        if ntype in ("agent", "tool"):
            field = "agent_version_id" if ntype == "agent" else "tool_version_id"
            kind = "agents" if ntype == "agent" else "tools"
            if not isinstance(ref, dict) or field not in ref:
                report.error(f"{path}.ref.{field}", "required", f"a {ntype} version is required")
            else:
                found = _resolve(
                    report, f"{path}.ref.{field}", ref[field], kind, env, resolve=resolve
                )
                if found is not None:
                    pins.append((found, ntype))
                    node["ref"] = {field: str(found.version_id)}
                    _check_node_inputs(ntype, found, n.get("input_map"), path, report)
        elif ref not in (None, {}):
            report.error(f"{path}.ref", "unexpected", f"a {ntype} node has no reference")
        input_map = n.get("input_map", {})
        if not isinstance(input_map, dict):
            report.error(f"{path}.input_map", "invalid_type", "must be an object")
            input_map = {}
        used: set[str] = set()
        for expr in template_exprs(input_map):
            used |= _node_refs(expr, report, f"{path}.input_map", idset)
        if ntype == "condition":
            cond = n.get("condition")
            if not isinstance(cond, str) or not cond.strip():
                report.error(f"{path}.condition", "required", "a condition expression is required")
            else:
                used |= _node_refs(cond, report, f"{path}.condition", idset)
        elif n.get("condition") is not None:
            report.error(f"{path}.condition", "unexpected", "only condition nodes have one")
        for other in used:
            if other == nid or other not in ancestors.get(nid, set()):
                report.error(
                    path,
                    "reference_not_upstream",
                    f"node {other!r} is not upstream of {nid!r}, its output is not available",
                )
        if ntype == "parallel":
            _validate_parallel(n, path, by_id, branch_owner, limits, parallel_cap, report)
        elif n.get("branches") is not None:
            report.error(f"{path}.branches", "unexpected", "only parallel nodes have branches")
        if n.get("max_parallel") is not None and ntype != "parallel":
            report.error(f"{path}.max_parallel", "unexpected", "only parallel nodes have one")
        out_nodes.append(node)
    for branch, owner in branch_owner.items():
        if any(e["from"] == branch or e["to"] == branch for e in edges):
            report.error(
                f"nodes[{ids.index(branch)}]",
                "branch_has_edges",
                f"branch {branch!r} of {owner!r} cannot have edges",
            )
    out["nodes"] = out_nodes
    out["edges"] = edges
    if limits is not None:
        out["limits"] = limits
    _validate_ui(spec.get("ui"), idset, report)
    return out, pins


def _check_node_inputs(
    ntype: str,
    found: VersionRef,
    input_map: Any,
    path: str,
    report: Report,  # Any: JSON
) -> None:
    keys = set(input_map) if isinstance(input_map, dict) else set()
    if ntype == "agent":
        if "input" not in keys:
            report.error(f"{path}.input_map.input", "required", "an agent node needs `input`")
        extra = keys - {"input", "variables"}
        if extra:
            report.error(f"{path}.input_map", "unknown_field", f"unknown keys: {sorted(extra)}")
    else:
        schema = found.spec.get("input_schema")
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        missing = required - keys
        if missing:
            report.error(
                f"{path}.input_map",
                "missing_arguments",
                f"the tool requires {sorted(missing)}",
            )
        if isinstance(schema, dict) and schema.get("additionalProperties") is False:
            unknown = keys - schema_properties(schema)
            if unknown:
                report.error(
                    f"{path}.input_map", "unknown_field", f"unknown arguments: {sorted(unknown)}"
                )


def _validate_parallel(
    node: dict[str, Any],
    path: str,
    by_id: dict[str, dict[str, Any]],
    owners: dict[str, str],
    limits: dict[str, Any] | None,
    cap: int,
    report: Report,
) -> None:
    branches = node.get("branches")
    if not isinstance(branches, list) or len(branches) < 2:
        report.error(f"{path}.branches", "required", "a parallel node needs at least two branches")
        return
    limit = cap if limits is None else int(limits["max_parallel"])
    mp = node.get("max_parallel")
    if mp is not None:
        if not _int_ok(mp) or mp < 1:
            report.error(f"{path}.max_parallel", "invalid_value", "must be a positive integer")
        elif mp > min(cap, limit):
            report.error(
                f"{path}.max_parallel", "exceeds_maximum", f"exceeds the cap of {min(cap, limit)}"
            )
    if len(branches) > min(cap, limit):
        report.error(f"{path}.branches", "exceeds_maximum", f"at most {min(cap, limit)} branches")
    for j, b in enumerate(branches):
        target = by_id.get(b) if isinstance(b, str) else None
        if target is None:
            report.error(f"{path}.branches[{j}]", "unknown_node", f"no node {b!r}")
        elif target.get("type") != "agent":
            report.error(f"{path}.branches[{j}]", "invalid_branch", "branches must be agent nodes")
        elif b in owners:
            report.error(f"{path}.branches[{j}]", "duplicate", f"{b!r} is already a branch")
        elif b == node["id"]:
            report.error(f"{path}.branches[{j}]", "invalid_branch", "a node cannot branch itself")
        else:
            owners[b] = node["id"]


def _validate_edges(
    raw: Any, by_id: dict[str, dict[str, Any]], report: Report
) -> list[dict[str, Any]]:  # Any: JSON
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        report.error("edges", "invalid_type", "must be a list")
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, e in enumerate(raw):
        path = f"edges[{i}]"
        if not isinstance(e, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        src, dst, when = e.get("from"), e.get("to"), e.get("when", "always")
        if src not in by_id or dst not in by_id:
            report.error(path, "unknown_node", "an edge must connect existing nodes")
            continue
        if src == dst:
            report.error(path, "cycle", "an edge cannot connect a node to itself")
            continue
        if when not in WHEN:
            report.error(f"{path}.when", "invalid_value", f"must be one of {', '.join(WHEN)}")
            continue
        is_cond = by_id[src].get("type") == "condition"
        if not is_cond and when != "always":
            report.error(f"{path}.when", "invalid_value", "only condition edges can be true/false")
            continue
        key = (src, dst, when)
        if key in seen:
            report.error(path, "duplicate", "duplicate edge")
            continue
        seen.add(key)
        out.append({"from": src, "to": dst, "when": when})
    return out


def _topological(ids: list[str], edges: list[dict[str, Any]], report: Report) -> list[str] | None:
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    queue = [i for i in ids if indeg[i] == 0]
    order: list[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(ids):
        report.error("edges", "cycle", "the graph contains a cycle (it must be a DAG)")
        return None
    return order


def _ancestors(ids: list[str], edges: list[dict[str, Any]]) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        parents[e["to"]].add(e["from"])
    memo: dict[str, set[str]] = {}

    def walk(node: str, trail: frozenset[str]) -> set[str]:
        if node in memo:
            return memo[node]
        found: set[str] = set()
        for p in parents[node]:
            if p in trail:  # a cycle is reported by the topological check
                continue
            found.add(p)
            found |= walk(p, trail | {node})
        memo[node] = found
        return found

    return {i: walk(i, frozenset()) for i in ids}


def _validate_limits(raw: Any, cap: int, report: Report) -> dict[str, Any] | None:  # Any: JSON
    if not isinstance(raw, dict):
        report.error("limits", "required", "limits are required")
        return None
    out: dict[str, Any] = {}
    for key, maximum in LIMIT_MAXIMA.items():
        value = raw.get(key)
        path = f"limits.{key}"
        if value is None:
            report.error(path, "required", "is required")
        elif not _int_ok(value) or value < 1:
            report.error(path, "invalid_value", "must be a positive integer")
        elif value > maximum:
            report.error(path, "exceeds_maximum", f"exceeds the maximum of {maximum}")
        elif key == "max_parallel" and value > cap:
            report.error(path, "exceeds_maximum", f"exceeds the tenant cap of {cap}")
        else:
            out[key] = value
    for key in raw:
        if key not in LIMIT_MAXIMA:
            report.warn(f"limits.{key}", "unknown_field", "field is ignored")
    return out if len(out) == len(LIMIT_MAXIMA) else None


def _validate_ui(raw: Any, ids: set[str], report: Report) -> None:  # Any: JSON, presentation only
    if raw is None:
        return
    positions = raw.get("positions") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or (positions is not None and not isinstance(positions, dict)):
        report.error("ui", "invalid_type", "ui.positions must be an object")
        return
    for nid, pos in (positions or {}).items():
        ok = isinstance(pos, dict) and all(
            isinstance(pos.get(a), int | float) and not isinstance(pos.get(a), bool) for a in "xy"
        )
        if not ok:
            report.error(f"ui.positions.{nid}", "invalid_value", "expected {x: number, y: number}")
