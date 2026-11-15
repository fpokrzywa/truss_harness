"""Pipeline specs: validation before publish and before every run (SYSTEM_ARCHITECTURE 7.2 rules
1-8, docs/api/PHASE_6_CONTRACT.md sections 1-3 and amendment E1).

A pipeline is a JSON DAG (`schema_version`, `params`, `policies`, `nodes`, `edges`, `schemas`,
optional presentation-only `ui`). Validation guarantees what the executor relies on: no cycles,
registered and enabled node types, config valid against the type's schema, input bindings that
read only *upstream* outputs with structurally compatible schemas, every reference pinned to a
published same-tenant version (or an active connection / usable model) the author may use, safe
expressions only, and an `approval` node on every path from untrusted model output to a
side-effect node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any  # Any: stored JSON specs
from uuid import UUID

from shifde.build import ontologies as ontology_spec
from shifde.build import pipeline_nodes as nodes_registry
from shifde.build.env import Env, VersionRef
from shifde.build.executors import WRITE_CLASSES
from shifde.build.issues import IDENT, Report, check_json_schema, schema_errors
from shifde.build.pipeline_nodes import BINDING, PARAM_BINDING, NodeType, expand_refs
from shifde.build.tools import BRACE_NAME, PARAM_NAME
from shifde.core import safeexpr

KNOWN_KEYS = frozenset({"schema_version", "params", "policies", "nodes", "edges", "schemas", "ui"})
KNOWN_NODE_KEYS = frozenset({"id", "type", "config", "inputs", "retry", "label"})
SUPPORTED_SCHEMA = "1"
MAX_NODES = 100
MAX_PARALLEL = 16
MAX_DEADLINE = 86_400
ON_FAILURE = ("fail_fast", "continue")
WHEN = ("true", "false", "always")
DEFAULT_POLICIES: dict[str, Any] = {
    "max_parallel": 4,
    "deadline_seconds": 3600,
    "on_failure": "fail_fast",
}
UNTRUSTED_SOURCES = frozenset({"model", "prompt", "agent"})
VERSION_FIELDS = {
    "agent": ("agent_version_id", "agents", "agent", "agent:execute"),
    "prompt": ("prompt_version_id", "prompts", "prompt", "prompt:read"),
    "tool": ("tool_version_id", "tools", "tool", "tool:invoke"),
}
REF_TYPE = {"agent": "agent_version", "prompt": "prompt_version", "tool": "tool_version"}
LATEST = re.compile(r"^latest:([0-9a-fA-F-]{36})$")
READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.I)
SQL_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
CONNECTION_TYPES = {"data_source": "database", "db_query": "database", "api_request": "rest"}


@dataclass
class PipelineResult:
    spec: dict[str, Any]
    pins: list[tuple[VersionRef, str]] = field(default_factory=list)
    refs: list[tuple[str, str, UUID]] = field(default_factory=list)  # (node, ref_type, id)
    topo_order: list[str] = field(default_factory=list)


def _int_ok(value: Any) -> bool:  # Any: JSON value
    return isinstance(value, int) and not isinstance(value, bool)


def _uuid(value: Any) -> UUID | None:  # Any: JSON value
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def expression_roots(
    expr: str, allowed: set[str], report: Report, path: str, node_id: str
) -> set[tuple[str | int, ...]] | None:
    """Parse `expr` and check that it reads only `allowed` roots; returns its field paths."""
    try:
        tree = safeexpr.parse(expr)
    except safeexpr.ExprError as exc:
        report.error(path, "expression_invalid", str(exc), node_id=node_id)
        return None
    found = safeexpr.paths(tree)
    for p in found:
        if p[0] not in allowed:
            report.error(
                path,
                "expression_invalid",
                f"an expression can only read {', '.join(sorted(allowed))}",
                node_id=node_id,
            )
            return None
    return found


def validate_pipeline(
    spec: dict[str, Any],
    env: Env,
    report: Report,
    parallel_cap: int,
    *,
    resolve: bool = True,
) -> PipelineResult:
    """Validate a pipeline spec. Returns the spec to store (references pinned, defaults filled),
    the pins, the typed reference rows and the topological order."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    out: dict[str, Any] = dict(spec)
    result = PipelineResult(out)
    if spec.get("schema_version") != SUPPORTED_SCHEMA:
        report.error(
            "schema_version",
            "unsupported_schema_version",
            f"schema_version must be {SUPPORTED_SCHEMA!r}",
        )

    named = _validate_schemas(spec.get("schemas"), report)
    out["schemas"] = named
    params_schema = _validate_params(spec.get("params"), report, named)
    out["params"] = params_schema
    out["policies"] = _validate_policies(spec.get("policies"), parallel_cap, report)

    ids, by_id = _validate_nodes(spec.get("nodes"), report)
    edges = _validate_edges(spec.get("edges"), by_id, report)
    order = _topological(ids, edges, report)
    parents: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        parents[e["to"]].add(e["from"])
    ancestors = _ancestors(ids, parents)

    ctx = _Ctx(env, report, resolve, named, params_schema, by_id, ancestors, edges, result)
    stored_nodes: list[dict[str, Any]] = []
    for nid in ids:
        stored_nodes.append(ctx.node(nid, ids.index(nid)))
    out["nodes"] = stored_nodes
    out["edges"] = edges
    _approval_rule(ctx, ids, edges)
    _validate_ui(spec.get("ui"), set(ids), report)
    result.topo_order = order or []
    return result


# ====================================================================== top-level sections
def _validate_schemas(raw: Any, report: Report) -> dict[str, Any]:  # Any: JSON
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        report.error("schemas", "invalid_type", "must be an object of named JSON Schemas")
        return {}
    out: dict[str, Any] = {}
    for name, schema in raw.items():
        if not isinstance(name, str) or not IDENT.match(name):
            report.error(f"schemas.{name}", "invalid_value", "names must be identifiers")
        elif check_json_schema(report, f"schemas.{name}", schema):
            out[name] = schema
    return out


def _validate_params(raw: Any, report: Report, named: dict[str, Any]) -> dict[str, Any]:
    if raw is None:
        return {"type": "object", "properties": {}}
    if not check_json_schema(report, "params", raw):
        return {"type": "object", "properties": {}}
    assert isinstance(raw, dict)
    if raw.get("type", "object") != "object":
        report.error("params.type", "invalid_value", "params must describe an object")
    return raw


def _validate_policies(raw: Any, cap: int, report: Report) -> dict[str, Any]:  # Any: JSON
    if raw is None:
        return dict(DEFAULT_POLICIES)
    if not isinstance(raw, dict):
        report.error("policies", "invalid_type", "must be an object")
        return dict(DEFAULT_POLICIES)
    out = dict(DEFAULT_POLICIES)
    for key in raw:
        if key not in DEFAULT_POLICIES:
            report.warn(f"policies.{key}", "unknown_field", "field is ignored")
    maximum = min(cap, MAX_PARALLEL)
    mp = raw.get("max_parallel", out["max_parallel"])
    if not _int_ok(mp) or mp < 1:
        report.error("policies.max_parallel", "invalid_value", "must be a positive integer")
    elif mp > maximum:
        report.error("policies.max_parallel", "exceeds_maximum", f"exceeds the cap of {maximum}")
    else:
        out["max_parallel"] = mp
    dl = raw.get("deadline_seconds", out["deadline_seconds"])
    if not _int_ok(dl) or dl < 1:
        report.error("policies.deadline_seconds", "invalid_value", "must be a positive integer")
    elif dl > MAX_DEADLINE:
        report.error(
            "policies.deadline_seconds", "exceeds_maximum", f"exceeds the maximum of {MAX_DEADLINE}"
        )
    else:
        out["deadline_seconds"] = dl
    of = raw.get("on_failure", out["on_failure"])
    if of not in ON_FAILURE:
        report.error(
            "policies.on_failure", "invalid_value", f"must be one of {', '.join(ON_FAILURE)}"
        )
    else:
        out["on_failure"] = of
    return out


def _validate_nodes(raw: Any, report: Report) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(raw, list) or not raw:
        report.error("nodes", "required", "at least one node is required")
        return [], {}
    if len(raw) > MAX_NODES:
        report.error("nodes", "too_many", f"at most {MAX_NODES} nodes")
        raw = raw[:MAX_NODES]
    ids: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for i, n in enumerate(raw):
        if not isinstance(n, dict):
            report.error(f"nodes[{i}]", "invalid_type", "must be an object")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not IDENT.match(nid):
            report.error(f"nodes[{i}].id", "invalid_value", "must be an identifier")
            continue
        if nid in by_id:
            report.error(
                f"nodes[{i}].id", "duplicate_node_id", f"node id {nid!r} is used twice", node_id=nid
            )
            continue
        ids.append(nid)
        by_id[nid] = n
    return ids, by_id


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
        src, dst = e.get("from"), e.get("to")
        when = e.get("when", "always")
        if when is None:
            when = "always"
        pair = (str(src), str(dst))
        if (
            not isinstance(src, str)
            or not isinstance(dst, str)
            or src not in by_id
            or dst not in by_id
        ):
            report.error(path, "dangling_edge", "an edge must connect existing nodes", edge=pair)
            continue
        if src == dst:
            report.error(
                path, "cycle_detected", "an edge cannot connect a node to itself", edge=pair
            )
            continue
        if not isinstance(when, str) or when not in WHEN:
            report.error(
                f"{path}.when",
                "expression_invalid",
                "when must be true, false or always (JSONLogic conditions are not supported)",
                edge=pair,
            )
            continue
        if by_id[src].get("type") != "condition" and when != "always":
            report.error(
                f"{path}.when",
                "invalid_edge_condition",
                "only edges leaving a condition node can be true / false",
                edge=pair,
            )
            continue
        if (str(src), str(dst), when) in seen:
            report.error(path, "duplicate_edge", "duplicate edge", edge=pair)
            continue
        seen.add((str(src), str(dst), when))
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
        report.error("edges", "cycle_detected", "the graph contains a cycle (it must be a DAG)")
        return None
    return order


def _ancestors(ids: list[str], parents: dict[str, set[str]]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def walk(node: str, trail: frozenset[str]) -> set[str]:
        if node in memo:
            return memo[node]
        found: set[str] = set()
        for p in parents[node]:
            if p in trail:  # cycles are reported by the topological check
                continue
            found.add(p)
            found |= walk(p, trail | {node})
        memo[node] = found
        return found

    return {i: walk(i, frozenset()) for i in ids}


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


# ====================================================================== per node
class _Ctx:
    def __init__(
        self,
        env: Env,
        report: Report,
        resolve: bool,
        named: dict[str, Any],
        params: dict[str, Any],
        by_id: dict[str, dict[str, Any]],
        ancestors: dict[str, set[str]],
        edges: list[dict[str, Any]],
        result: PipelineResult,
    ) -> None:
        self.env, self.report, self.resolve = env, report, resolve
        self.named, self.params = named, params
        self.by_id, self.ancestors, self.edges = by_id, ancestors, edges
        self.result = result
        self.side_effect: dict[str, bool] = {}
        self.stored: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- helpers
    def err(self, nid: str, path: str, code: str, message: str) -> None:
        self.report.error(path, code, message, node_id=nid)

    def node(self, nid: str, index: int) -> dict[str, Any]:
        raw = self.by_id[nid]
        path = f"nodes[{index}]"
        node = dict(raw)
        for key in raw:
            if key not in KNOWN_NODE_KEYS:
                self.report.warn(f"{path}.{key}", "unknown_field", "field is ignored", node_id=nid)
        ntype = raw.get("type")
        nt = nodes_registry.get(ntype) if isinstance(ntype, str) else None
        if nt is None:
            self.err(nid, f"{path}.type", "unknown_node_type", f"unknown node type {ntype!r}")
            return node
        if not self._enabled(nt):
            self.err(
                nid,
                f"{path}.type",
                "node_type_disabled",
                nt.reason_disabled or f"the {nt.type} node type is not available",
            )
            return node
        config = raw.get("config", {})
        if not isinstance(config, dict):
            self.err(nid, f"{path}.config", "invalid_config", "config must be an object")
            return node
        problems = schema_errors(nt.config_schema, config)
        for loc, message in problems:
            suffix = "".join(f".{p}" if isinstance(p, str) else f"[{p}]" for p in loc)
            self.err(nid, f"{path}.config{suffix}", "invalid_config", message)
        node["config"] = dict(config)
        self._retry(nid, path, raw.get("retry"))
        inputs = raw.get("inputs", {})
        if not isinstance(inputs, dict) or not all(isinstance(v, str) for v in inputs.values()):
            self.err(nid, f"{path}.inputs", "invalid_type", "inputs map a port to a node.output")
            inputs = {}
        node["inputs"] = dict(inputs)
        if not problems:
            self._semantics(nt, nid, path, node)
        self._bindings(nt, nid, path, node)
        self.stored[nid] = node
        return node

    def _enabled(self, nt: NodeType) -> bool:
        if not nt.implemented:
            return False
        return not (nt.executor and self.env.executor_maturity(nt.executor) == "planned")

    def _retry(self, nid: str, path: str, raw: Any) -> None:  # Any: JSON
        if raw is None:
            return
        ok = (
            isinstance(raw, dict)
            and set(raw) <= {"max_attempts", "backoff_s"}
            and _int_ok(raw.get("max_attempts", 1))
            and 1 <= raw.get("max_attempts", 1) <= 10
            and isinstance(raw.get("backoff_s", 0), int | float)
            and not isinstance(raw.get("backoff_s", 0), bool)
            and 0 <= raw.get("backoff_s", 0) <= 3600
        )
        if not ok:
            self.err(
                nid,
                f"{path}.retry",
                "invalid_config",
                "retry is {max_attempts: 1-10, backoff_s?: 0-3600}",
            )

    # ---------------------------------------------------------------- references
    def _connection(
        self, nt: NodeType, nid: str, path: str, config: dict[str, Any], *, write: bool
    ) -> None:
        cid = _uuid(config.get("connection_id"))
        if cid is None:
            return
        where = f"{path}.config.connection_id"
        info = self.env.connection(cid)
        if info is None:
            self.err(nid, where, "ref_cross_tenant", "no such connection is available to you")
            return
        if not info.usable_by_publisher:
            self.err(nid, where, "permission_missing", "you may not use this connection")
        wanted = CONNECTION_TYPES.get(nt.type, "rest" if nt.type == "sink" else "")
        if wanted and info.connection_type != wanted:
            self.err(nid, where, "invalid_config", f"a {wanted} connection is required")
        if info.status != "active":
            self.err(nid, where, "connection_inactive", f"the connection is {info.status}")
        if not info.implemented:
            self.err(
                nid, where, "connection_inactive", "the connection's adapter is not implemented"
            )
        if write and info.access_mode != "read_write":
            self.err(
                nid, where, "connection_read_only", "this node writes: a read_write connection"
            )
        self.result.refs.append((nid, "connection", cid))

    def _version(
        self, nt: NodeType, nid: str, path: str, node: dict[str, Any]
    ) -> VersionRef | None:
        fld, kind, role, action = VERSION_FIELDS[nt.type]
        value = node["config"].get(fld)
        where = f"{path}.config.{fld}"
        if not isinstance(value, str):
            return None
        found: VersionRef | None
        latest = LATEST.match(value)
        if latest is not None:
            if not self.resolve:
                self.err(nid, where, "ref_not_published", "a published pipeline must pin versions")
                return None
            aid = _uuid(latest.group(1))
            found = self.env.latest(aid) if aid is not None else None
            if found is None:
                self.err(
                    nid, where, "ref_not_published", "no published version is available to you"
                )
                return None
        else:
            vid = _uuid(value)
            found = self.env.version(vid) if vid is not None else None
            if found is None:
                self.err(nid, where, "ref_cross_tenant", "no such version is available to you")
                return None
        who = f"{found.artifact_key} v{found.version_no}" if self.env.reveal_names else "a version"
        if found.kind != kind:
            self.err(nid, where, "wrong_kind", f"expected a {kind[:-1]} version")
            return None
        if found.state == "draft":
            self.err(nid, where, "ref_not_published", "only published versions can be referenced")
            return None
        if found.state == "retired" or found.artifact_status == "retired":
            self.err(nid, where, "ref_retired", f"{who} is retired")
            return None
        if found.artifact_status == "disabled":
            self.err(nid, where, "ref_disabled", f"{who} is disabled")
            return None
        if found.state == "deprecated":
            self.report.warn(where, "reference_deprecated", f"{who} is deprecated", node_id=nid)
        if not self.env.permits(action, found):
            self.err(nid, where, "permission_missing", f"you may not use {who}")
        node["config"][fld] = str(found.version_id)
        self.result.pins.append((found, role))
        self.result.refs.append((nid, REF_TYPE[role], found.version_id))
        return found

    def _model(self, nid: str, path: str, config: dict[str, Any]) -> None:
        mid = _uuid(config.get("model_id"))
        if mid is None:
            return
        where = f"{path}.config.model_id"
        problems = self.env.model_problems([mid])
        if mid in problems:
            code = "ref_cross_tenant" if problems[mid] == "model_not_found" else "model_unusable"
            self.err(nid, where, code, f"the model cannot be used here ({problems[mid]})")
            return
        self.result.refs.append((nid, "model", mid))

    # ---------------------------------------------------------------- semantics per type
    def _semantics(self, nt: NodeType, nid: str, path: str, node: dict[str, Any]) -> None:
        config = node["config"]
        t = nt.type
        self.side_effect[nid] = False
        if t == "data_source":
            self._connection(nt, nid, path, config, write=False)
        elif t == "db_query":
            self._connection(nt, nid, path, config, write=False)
            self._db_query(nid, path, config)
        elif t == "ontology_query":
            self._ontology_query(nid, path, node)
        elif t == "api_request":
            method = str(config.get("method", "GET")).upper()
            self.side_effect[nid] = method in nodes_registry.WRITE_METHODS
            self._connection(nt, nid, path, config, write=self.side_effect[nid])
            self._api_request(nid, path, node)
        elif t == "validate":
            schema = expand_refs(config.get("schema"), self.named)
            if check_json_schema(self.report, f"{path}.config.schema", schema):
                node["config"] = {"on_invalid": "fail", **config}
        elif t == "transform":
            self._transform(nid, path, config)
        elif t == "condition":
            self._condition(nid, path, node)
        elif t == "model":
            self._model(nid, path, config)
        elif t == "prompt":
            found = self._version(nt, nid, path, node)
            self._model(nid, path, config)
            if found is not None:
                self._prompt_inputs(nid, path, found, node)
        elif t in ("agent", "tool"):
            found = self._version(nt, nid, path, node)
            if t == "tool" and found is not None:
                self.side_effect[nid] = found.spec.get("side_effect_class") in WRITE_CLASSES
        elif t == "sink":
            self._sink(nt, nid, path, node)

    def _ontology_query(self, nid: str, path: str, node: dict[str, Any]) -> None:
        """`ontology_query`: the pinned ontology version must be published, visible and queryable
        by the publisher; the query must be structurally valid for it. Per-principal authorization
        (policies, row filters) is decided when the node runs."""
        config = node["config"]
        where = f"{path}.config.version_id"
        oid = _uuid(config.get("ontology_id"))
        raw = config.get("version_id")
        if oid is None or not isinstance(raw, str):
            return
        found: VersionRef | None
        latest = LATEST.match(raw)
        if latest is not None:
            if not self.resolve:
                self.err(nid, where, "ref_not_published", "a published pipeline must pin versions")
                return
            aid = _uuid(latest.group(1))
            found = self.env.latest(aid) if aid is not None else None
            if found is None:
                self.err(nid, where, "ref_not_published", "no published version is available")
                return
        else:
            vid = _uuid(raw)
            found = self.env.version(vid) if vid is not None else None
            if found is None:
                self.err(nid, where, "ref_cross_tenant", "no such version is available to you")
                return
        if found.kind != "ontologies":
            self.err(nid, where, "wrong_kind", "expected an ontology version")
            return
        if found.artifact_id != oid:
            self.err(nid, where, "invalid_config", "version_id is not a version of ontology_id")
            return
        who = f"{found.artifact_key} v{found.version_no}" if self.env.reveal_names else "a version"
        if found.state == "draft":
            self.err(nid, where, "ref_not_published", "only published versions can be referenced")
            return
        if found.state == "retired" or found.artifact_status in ("retired", "disabled"):
            self.err(nid, where, "ref_retired", f"{who} is retired or disabled")
            return
        if found.state == "deprecated":
            self.report.warn(where, "reference_deprecated", f"{who} is deprecated", node_id=nid)
        if not self.env.permits("ontology:query", found):
            self.err(nid, where, "permission_missing", f"you may not query {who}")
        node["config"]["version_id"] = str(found.version_id)
        self.result.pins.append((found, "ontology"))
        self.result.refs.append((nid, "ontology_version", found.version_id))
        ontology_spec.validate_query_shape(
            config.get("query"), found.spec, self.report, f"{path}.config.query", nid
        )

    def _prompt_inputs(self, nid: str, path: str, found: VersionRef, node: dict[str, Any]) -> None:
        from shifde.build.prompts import declared_variables

        required = [
            n
            for n, d in declared_variables(found.spec).items()
            if d.get("required", False) and "default" not in d
        ]
        if required and "variables" not in node["inputs"]:
            self.err(
                nid,
                f"{path}.inputs.variables",
                "missing_input",
                f"the prompt requires variables {sorted(required)}: bind a `variables` object",
            )

    def _sink(self, nt: NodeType, nid: str, path: str, node: dict[str, Any]) -> None:
        config = node["config"]
        if config.get("target") == "connection":
            self.side_effect[nid] = True
            if "connection_id" not in config or "path_template" not in config:
                self.err(
                    nid,
                    f"{path}.config",
                    "invalid_config",
                    "a connection sink needs connection_id and path_template",
                )
                return
            self._connection(nt, nid, path, config, write=True)
        else:
            extra = [k for k in ("connection_id", "path_template", "method") if k in config]
            if extra:
                self.err(
                    nid,
                    f"{path}.config",
                    "invalid_config",
                    f"a run_artifact sink takes no {', '.join(extra)}",
                )

    def _db_query(self, nid: str, path: str, config: dict[str, Any]) -> None:
        sql = SQL_COMMENTS.sub(" ", str(config.get("sql", "")))
        body = sql.strip().rstrip(";").strip()
        if not READ_ONLY_SQL.match(body) or ";" in body:
            self.err(
                nid,
                f"{path}.config.sql",
                "invalid_config",
                "db_query runs ONE read-only SELECT / WITH statement",
            )
        args = config.get("args", {})
        if not isinstance(args, dict):
            self.err(nid, f"{path}.config.args", "invalid_config", "args must be an object")
            return
        names = set(PARAM_NAME.findall(body))
        if names - set(args):
            self.err(
                nid,
                f"{path}.config.args",
                "invalid_config",
                f"the query uses undeclared arguments: {sorted(names - set(args))}",
            )
        if set(args) - names:
            self.report.warn(
                f"{path}.config.args",
                "unused_argument",
                f"unused arguments: {sorted(set(args) - names)}",
                node_id=nid,
            )
        for key, value in args.items():
            where = f"{path}.config.args.{key}"
            if isinstance(value, str):
                self._binding(nid, where, value, None, require_upstream=True)
            elif isinstance(value, dict):
                if set(value) != {"value"}:
                    self.err(nid, where, "invalid_config", 'a literal is {"value": ...}')
            elif isinstance(value, list):
                self.err(nid, where, "invalid_config", "arguments are scalars or bindings")

    def _api_request(self, nid: str, path: str, node: dict[str, Any]) -> None:
        config = node["config"]
        placeholders: set[str] = set(BRACE_NAME.findall(str(config.get("path_template", ""))))
        for part in (config.get("query"), config.get("body")):
            placeholders |= _brace_names(part)
        missing = placeholders - set(node["inputs"])
        if missing:
            self.err(
                nid,
                f"{path}.inputs",
                "missing_input",
                f"placeholders without an input binding: {sorted(missing)}",
            )

    def _transform(self, nid: str, path: str, config: dict[str, Any]) -> None:
        ops = config.get("operations")
        if not isinstance(ops, list):
            return
        for i, op in enumerate(ops):
            where = f"{path}.config.operations[{i}]"
            kind = op.get("op") if isinstance(op, dict) else None
            if kind not in nodes_registry.TRANSFORM_OPS:
                self.err(
                    nid,
                    f"{where}.op",
                    "invalid_config",
                    f"op must be one of {', '.join(nodes_registry.TRANSFORM_OPS)}",
                )
                continue
            assert isinstance(op, dict)
            self._transform_op(nid, where, kind, op)

    def _transform_op(self, nid: str, where: str, kind: str, op: dict[str, Any]) -> None:
        def ident(value: Any) -> bool:  # Any: JSON
            return isinstance(value, str) and bool(IDENT.match(value))

        shape: dict[str, set[str]] = {
            "filter": {"op", "expression"},
            "select": {"op", "fields"},
            "rename": {"op", "from", "to"},
            "map": {"op", "field", "expression"},
            "cast": {"op", "field", "to"},
            "aggregate": {"op", "group_by", "fn", "field", "as"},
        }
        extra = set(op) - shape[kind]
        if extra:
            self.err(nid, where, "invalid_config", f"unknown keys for {kind}: {sorted(extra)}")
        ok = True
        if kind in ("filter", "map"):
            expr = op.get("expression")
            if not isinstance(expr, str):
                ok = False
            else:
                expression_roots(expr, {"row", "params"}, self.report, f"{where}.expression", nid)
        if kind == "select":
            fields = op.get("fields")
            ok = isinstance(fields, list) and bool(fields) and all(ident(f) for f in fields)
        if kind == "rename":
            ok = ident(op.get("from")) and ident(op.get("to"))
        if kind == "map":
            ok = ok and ident(op.get("field"))
        if kind == "cast":
            ok = ident(op.get("field")) and op.get("to") in nodes_registry.CAST_TYPES
        if kind == "aggregate":
            group = op.get("group_by", [])
            fn = op.get("fn")
            ok = (
                isinstance(group, list)
                and all(ident(g) for g in group)
                and fn in nodes_registry.AGGREGATE_FNS
                and ident(op.get("as"))
                and (fn == "count" or ident(op.get("field")))
            )
        if not ok:
            self.err(nid, where, "invalid_config", f"the {kind} operation is malformed")

    def _condition(self, nid: str, path: str, node: dict[str, Any]) -> None:
        expr = node["config"].get("expression")
        if not isinstance(expr, str):
            return
        found = expression_roots(
            expr, {"inputs", "params"}, self.report, f"{path}.config.expression", nid
        )
        for p in found or ():
            if p[0] == "inputs" and (len(p) < 2 or p[1] not in node["inputs"]):
                self.err(
                    nid,
                    f"{path}.config.expression",
                    "expression_invalid",
                    "inputs.<port> must name a bound input port",
                )

    # ---------------------------------------------------------------- bindings (rule 4)
    def _bindings(self, nt: NodeType, nid: str, path: str, node: dict[str, Any]) -> None:
        inputs: dict[str, str] = node["inputs"]
        for port, value in inputs.items():
            where = f"{path}.inputs.{port}"
            declared = nt.input(port)
            if declared is None and not nt.dynamic_inputs:
                self.err(nid, where, "unknown_port", f"{nt.type} has no input port {port!r}")
                continue
            self._binding(nid, where, value, declared.schema if declared else None, True)
        for p in nt.inputs:
            if p.required and p.name not in inputs:
                self.err(
                    nid, f"{path}.inputs.{p.name}", "missing_input", f"input {p.name!r} is required"
                )

    def _binding(
        self,
        nid: str,
        where: str,
        value: str,
        dst: dict[str, Any] | None,
        require_upstream: bool,
    ) -> None:
        param = PARAM_BINDING.match(value)
        if param is not None:
            props = self.params.get("properties")
            decl = props.get(param.group(1)) if isinstance(props, dict) else None
            if not isinstance(decl, dict):
                self.err(nid, where, "invalid_binding", f"undeclared parameter {param.group(1)!r}")
                return
            self._compat(nid, where, expand_refs(decl, self.named), dst)
            return
        m = BINDING.match(value)
        if m is None:
            self.err(nid, where, "invalid_binding", "a binding is `node.output` or `$params.name`")
            return
        src_id, port = m.group(1), m.group(2)
        src = self.by_id.get(src_id)
        src_type = (
            nodes_registry.get(src["type"]) if src and isinstance(src.get("type"), str) else None
        )
        if src is None:
            self.err(nid, where, "invalid_binding", f"unknown node {src_id!r}")
            return
        if src_id == nid or src_id not in self.ancestors.get(nid, set()):
            self.err(
                nid,
                where,
                "input_not_upstream",
                f"node {src_id!r} is not upstream of {nid!r}, its output is not available",
            )
            return
        if src_type is None:
            return  # reported on the source node
        out_port = src_type.output(port)
        if out_port is None:
            self.err(nid, where, "invalid_binding", f"{src_type.type} has no output {port!r}")
            return
        self._compat(nid, where, out_port.schema, dst)

    def _compat(
        self, nid: str, where: str, src: dict[str, Any], dst: dict[str, Any] | None
    ) -> None:
        if dst is None:
            return
        reason = nodes_registry.compatible(src, expand_refs(dst, self.named))
        if reason:
            self.err(nid, where, "schema_incompatible", reason)


def _brace_names(value: Any) -> set[str]:  # Any: JSON template
    if isinstance(value, str):
        return set(BRACE_NAME.findall(value))
    if isinstance(value, dict):
        out: set[str] = set()
        for k, v in value.items():
            out |= _brace_names(k) | _brace_names(v)
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= _brace_names(v)
        return out
    return set()


# ====================================================================== rule 8
def _approval_rule(ctx: _Ctx, ids: list[str], edges: list[dict[str, Any]]) -> None:
    """A side-effect node reachable from untrusted model output without passing an approval node
    is refused (SYSTEM_ARCHITECTURE 7.2 rule 8)."""
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        adj[e["from"]].append(e["to"])

    def type_of(nid: str) -> str:
        raw = ctx.by_id.get(nid) or {}
        return str(raw.get("type"))

    flagged: set[str] = set()
    for start in ids:
        if type_of(start) not in UNTRUSTED_SOURCES:
            continue
        seen: set[str] = set()
        stack = list(adj[start])
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if type_of(cur) == "approval":
                continue  # an approval node guards everything behind it
            if ctx.side_effect.get(cur):
                flagged.add(cur)
            stack.extend(adj[cur])
    # an OR-joined edge that bypasses the approval is as bad as a path that does: for a
    # side-effect node with model output upstream, EVERY incoming edge must come from an approval
    # node or from a node that already has an approval upstream
    approvals = {i for i in ids if type_of(i) == "approval"}
    for nid in ids:
        if nid in flagged or not ctx.side_effect.get(nid):
            continue
        if not any(type_of(a) in UNTRUSTED_SOURCES for a in ctx.ancestors.get(nid, set())):
            continue
        sources = {a for a in ctx.ancestors.get(nid, set()) if type_of(a) in UNTRUSTED_SOURCES}
        for e in edges:
            if e["to"] != nid:
                continue
            src = e["from"]
            # an edge that does not come from an approval must descend from EVERY model-output
            # ancestor (so the guarded path checked above is the one that wakes this node)
            carries = ctx.ancestors.get(src, set()) | {src}
            if src not in approvals and not sources <= carries:
                flagged.add(nid)
    for nid in sorted(flagged, key=ids.index):
        ctx.err(
            nid,
            f"nodes[{ids.index(nid)}]",
            "approval_required_before_side_effect",
            f"node {nid!r} has a side effect downstream of model output: "
            "an approval node must be on every path to it",
        )
