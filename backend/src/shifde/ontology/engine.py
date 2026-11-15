"""Ontology query planning and execution (P7 contract section 4, SYSTEM_ARCHITECTURE 8.2).

Planning turns an `OntologyQuery` into a tree of source fetches, deciding authorization on the
way (default deny, property denial, row predicates) and compiling every filter to parameterized
`Filter` trees over the identifiers of the FROZEN mapping. Execution is capability-aware: each
fetch is compiled by the connection's adapter (`DataPlatformAdapter.compile`, identifiers quoted,
values bound) and run at the source with the connection's own identity (the ceiling of what
SHIFDE can ever read). Relationships are traversed by KEYED FETCH: the edge relation is read for
the batch of parent keys, then the target rows for the batch of end keys, and the results are
joined here under hard caps. A join is never pushed down: no adapter implements it yet, so the
plan only records `pushdown_available` when an adapter declares `query.pushdown_join`.

Nothing is copied: rows live in memory for the response and are dropped; lineage keeps hashes of
the compiled statements and counts.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any  # Any: row values / spec JSON
from uuid import UUID

from shifde.adapters.base import (
    AdapterFailure,
    Capability,
    ConnectionSession,
    Filter,
    FilterGroup,
    FilterNode,
    SourceQueryPlan,
)
from shifde.build import ontology_expr as expr
from shifde.connections.service import failure_to_error
from shifde.core.errors import AppError, Forbidden, NotFound, ValidationFailed
from shifde.ontology import policy as pol
from shifde.ontology import schemas as s

QUERY_BUDGET_SECONDS = 30.0
KEY_BATCH = 200
PER_PARENT_LINKS = 100
MAX_HOP_EDGES = 2000
MAX_TOTAL_ROWS = 10_000
LINKS = "_links"


class PropertyDenied(Forbidden):
    code = "property_denied"


class TraversalTooDeep(ValidationFailed):
    code = "traversal_too_deep"


class TooManyHops(ValidationFailed):
    code = "too_many_hops"


class QueryTimeout(AppError):
    status, code, title = 504, "query_timeout", "Gateway Timeout"


class MappingUnavailable(AppError):
    status, code, title = 409, "mapping_unavailable", "Conflict"


# ============================================================================ plan model
@dataclass
class NodePlan:
    object_type: str
    connection_id: UUID
    schema: str | None
    table: str
    select: list[str]
    pk: list[str]
    columns: dict[str, str]  # property key -> column, for select ∪ pk
    ptypes: dict[str, str]
    filters: list[FilterNode]
    hops: list[HopPlan] = field(default_factory=list)


@dataclass
class HopPlan:
    relationship: str
    label: str
    direction: str
    edge_connection_id: UUID
    edge_schema: str | None
    edge_table: str
    start_column: str
    end_column: str
    edge_filters: list[FilterNode]
    target: NodePlan
    pushdown_available: bool = False
    strategy: str = "keyed_fetch"


@dataclass
class Authorized:
    """One object type as the principal may use it: mapping operands and grants."""

    spec: Mapping[str, Any]  # Any: spec object type
    mapping: Mapping[str, Any]  # Any: mapping row
    operands: dict[str, expr.Operand]
    grants: list[pol.Grant]


# ============================================================================ context
@dataclass
class EngineContext:
    """Everything planning needs, resolved by the service (frozen version, mappings, policies)."""

    spec: Mapping[str, Any]  # Any: published spec
    mappings: Mapping[tuple[str, str], Mapping[str, Any]]  # Any: mapping rows
    policies: Sequence[Mapping[str, Any]]  # Any: policy rows
    subjects: pol.Subjects
    capabilities: Callable[[UUID], frozenset[str]]
    edge_operands: Callable[[Mapping[str, Any]], dict[str, expr.Operand]]  # Any: mapping row
    consts: Mapping[str, Any] | None = None  # Any: `params.*` constants of a pipeline run


_INTEGER_LITERAL = re.compile(r"[+-]?[0-9]+")


def exact_number(value: str) -> Any:  # Any: int | float | Decimal | the text itself
    """A numeric string from an adapter, without a lossy float round trip (REQ F-04: keys above
    2**53 returned the wrong object). An integer literal is an `int`; any other number is a
    `float` only when the float prints back to the same value, else an exact `Decimal`."""
    text = value.strip()
    if _INTEGER_LITERAL.fullmatch(text):
        return int(text)
    try:
        exact = Decimal(text)
    except InvalidOperation:
        return value
    if not exact.is_finite():
        return float(text)  # nan / inf: no exact form
    approx = float(exact)
    return approx if math.isfinite(approx) and Decimal(repr(approx)) == exact else exact


def normalize_value(ptype: str, value: Any) -> Any:  # Any: adapter value
    if value is None:
        return None
    if ptype == "number" and isinstance(value, str):
        return exact_number(value)
    return value


# ============================================================================ planning
class Planner:
    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx
        self.types = {t["key"]: t for t in ctx.spec.get("object_types") or []}
        self.rels = {r["key"]: r for r in ctx.spec.get("relationships") or []}
        self.hops = 0
        self.denied: list[tuple[str, str]] = []  # (object type, reason) for the audit trail

    # ---------------------------------------------------------------- authorization
    def authorize(self, otype: str) -> Authorized:
        ot = self.types.get(otype)
        mapping = self.ctx.mappings.get(("object_type", otype))
        if ot is None or mapping is None:
            raise NotFound("object type not found")
        operands = {
            prop_key: expr.Operand(column, prop["type"], prop_key)
            for prop in ot["properties"]
            for prop_key, column in [(prop["key"], (mapping["column_map"] or {}).get(prop["key"]))]
            if isinstance(column, str)
        }
        grants = pol.grants_for(self.ctx.policies, self.ctx.subjects, ot, operands)
        if not grants:
            self.denied.append((otype, "no_policy"))
            raise NotFound("object type not found")  # default deny, no existence oracle
        return Authorized(ot, mapping, operands, grants)

    # ---------------------------------------------------------------- node
    def node(
        self,
        otype: str,
        select: Sequence[str],
        filter_source: str | None,
        traverse: Sequence[s.HopIn],
        depth: int,
        *,
        extra_filters: Sequence[FilterNode] = (),
        touched_extra: Sequence[str] = (),
        count_only: bool = False,
    ) -> NodePlan:
        if depth > s.MAX_DEPTH:
            raise TraversalTooDeep(f"traversal is limited to {s.MAX_DEPTH} levels")
        auth = self.authorize(otype)
        known = {p["key"] for p in auth.spec["properties"]}
        unknown = [k for k in select if k not in known]
        if unknown:
            raise ValidationFailed(f"{otype} has no property {unknown[0]!r}")
        field_keys: list[str] = []
        if filter_source:
            field_keys = self._filter_properties(filter_source, auth, known)
        touched = [*select, *field_keys, *touched_extra]
        # a count-only node reads the key for joining and returns only a number: it needs no
        # readable property, and sees exactly the rows ANY grant admits
        denied = [] if count_only else pol.denied_properties(auth.grants, touched)
        if denied:
            self.denied.append((otype, f"property:{denied[0]}"))
            raise PropertyDenied(f"access to property {denied[0]!r} of {otype} is denied")
        unmapped = [k for k in dict.fromkeys(touched) if k not in auth.operands]
        if unmapped:
            raise ValidationFailed(f"property {unmapped[0]!r} is not mapped to a source column")
        filters: list[FilterNode] = list(self._static_filters(auth.mapping, auth.operands))
        if filter_source:
            filters.append(self._compile(filter_source, auth))
        filters.extend(extra_filters)
        predicate = (
            pol.any_grant_predicate(auth.grants)
            if count_only
            else pol.row_predicate(auth.grants, touched)
        )
        if predicate is not None:
            filters.append(predicate)
        pk = list(auth.spec["primary_key"])
        missing_pk = [k for k in pk if k not in auth.operands]
        if missing_pk:
            raise MappingUnavailable("the primary key of the object type is not mapped")
        columns = {k: auth.operands[k].column for k in dict.fromkeys([*select, *pk])}
        plan = NodePlan(
            object_type=otype,
            connection_id=auth.mapping["connection_id"],
            schema=auth.mapping["relation_schema"] or None,
            table=auth.mapping["relation_name"],
            select=list(dict.fromkeys(select)),
            pk=pk,
            columns=columns,
            ptypes={k: auth.operands[k].ptype for k in columns},
            filters=filters,
        )
        self._require_sql(plan.connection_id)
        seen_rel: set[str] = set()
        for hop in traverse:
            if hop.relationship in seen_rel:
                raise ValidationFailed(f"relationship {hop.relationship!r} is traversed twice")
            seen_rel.add(hop.relationship)
            plan.hops.append(self._hop(otype, hop, depth))
        return plan

    def _require_sql(self, connection_id: UUID) -> None:
        if Capability.QUERY_SQL.value not in self.ctx.capabilities(connection_id):
            raise MappingUnavailable("the source connection's adapter cannot run queries")

    def _filter_properties(self, source: str, auth: Authorized, known: set[str]) -> list[str]:
        try:
            paths = expr.referenced_fields(source)
        except expr.FilterError as exc:
            raise ValidationFailed(f"filter: {exc}") from exc
        out: list[str] = []
        for path in paths:
            if path[0] == "params" and self.ctx.consts is not None:
                continue
            name = path[1] if len(path) == 2 and path[0] == "row" else path[0]
            if len(path) > 2 or not isinstance(name, str) or name not in known:
                raise ValidationFailed(f"filter: unknown field {'.'.join(map(str, path))!r}")
            out.append(name)
        return out

    def _compile(self, source: str, auth: Authorized) -> FilterNode:
        try:
            return expr.compile_filter(
                source, expr.bare_resolver(auth.operands), consts=self.ctx.consts
            )
        except expr.FilterError as exc:
            raise ValidationFailed(f"filter: {exc}") from exc

    def _static_filters(
        self, mapping: Mapping[str, Any], operands: Mapping[str, expr.Operand]
    ) -> list[FilterNode]:
        out: list[FilterNode] = []
        for source in mapping["filters"] or []:
            try:
                out.append(expr.compile_filter(str(source), expr.bare_resolver(operands)))
            except expr.FilterError as exc:
                raise MappingUnavailable(f"a static mapping filter is invalid: {exc}") from exc
        return out

    # ---------------------------------------------------------------- hop
    def count_hop(self, start_type: str, relationship: str, direction: str) -> HopPlan:
        """A hop that only counts related rows (object view): the target's key is its select."""
        rel = self.rels.get(relationship)
        if rel is None:
            raise NotFound("relationship not found")
        target = rel["to"] if direction == "forward" else rel["from"]
        target_type = self.types.get(target)
        if target_type is None:
            raise NotFound("relationship not found")
        hop = s.HopIn.model_validate(
            {
                "relationship": relationship,
                "direction": direction,
                "select": list(target_type["primary_key"]),
            }
        )
        saved = self.hops  # summaries have their own budget: they never consume the query's
        try:
            return self._hop(start_type, hop, 1, count_only=True)
        finally:
            self.hops = saved

    def _hop(self, start_type: str, hop: s.HopIn, depth: int, count_only: bool = False) -> HopPlan:
        self.hops += 1
        if self.hops > s.MAX_HOPS:
            raise TooManyHops(f"at most {s.MAX_HOPS} relationships per query")
        rel = self.rels.get(hop.relationship)
        mapping = self.ctx.mappings.get(("relationship", hop.relationship))
        if rel is None or mapping is None:
            raise NotFound("relationship not found")
        forward = hop.direction == "forward"
        origin, target = (rel["from"], rel["to"]) if forward else (rel["to"], rel["from"])
        if origin != start_type:
            raise ValidationFailed(
                f"relationship {rel['key']!r} does not start at {start_type} ({hop.direction})"
            )
        try:
            target_plan = self.node(
                target, hop.select, hop.filter, hop.traverse, depth + 1, count_only=count_only
            )
        except NotFound as exc:  # the target type is not visible: the relationship is not either
            raise NotFound("relationship not found") from exc
        key_map = mapping["key_map"] or {}
        start_col = str(key_map["from_column" if forward else "to_column"])
        end_col = str(key_map["to_column" if forward else "from_column"])
        edge_filters: list[FilterNode] = []
        if mapping["filters"]:
            operands = self.ctx.edge_operands(mapping)
            for source in mapping["filters"]:
                try:
                    edge_filters.append(
                        expr.compile_filter(str(source), expr.bare_resolver(operands))
                    )
                except expr.FilterError as exc:
                    raise MappingUnavailable(f"a static mapping filter is invalid: {exc}") from exc
        self._require_sql(mapping["connection_id"])
        caps = self.ctx.capabilities(mapping["connection_id"])
        return HopPlan(
            relationship=rel["key"],
            label=rel["label"],
            direction=hop.direction,
            edge_connection_id=mapping["connection_id"],
            edge_schema=mapping["relation_schema"] or None,
            edge_table=mapping["relation_name"],
            start_column=start_col,
            end_column=end_col,
            edge_filters=edge_filters,
            target=target_plan,
            pushdown_available=Capability.QUERY_PUSHDOWN_JOIN.value in caps
            and target_plan.connection_id == mapping["connection_id"],
        )


# ============================================================================ execution
class Source:
    """Statements run for one query: sessions per connection, compile + run, hashes, deadline."""

    def __init__(
        self,
        open_session: Callable[[UUID, float], ConnectionSession],
        compile_for: Callable[[UUID, SourceQueryPlan], tuple[str, list[Any]]],
        budget_seconds: float = QUERY_BUDGET_SECONDS,
    ) -> None:
        self._open = open_session
        self._compile = compile_for
        self.deadline = time.monotonic() + budget_seconds
        self.sessions: dict[UUID, ConnectionSession] = {}
        self.hashes: list[str] = []
        self.connections: list[UUID] = []

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise QueryTimeout("the ontology query exceeded its time budget")
        return left

    def run(
        self, connection_id: UUID, plan: SourceQueryPlan
    ) -> tuple[list[str], list[list[Any]], bool]:
        remaining = self.remaining()
        sql, params = self._compile(connection_id, plan)
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if digest not in self.hashes:
            self.hashes.append(digest)
        if connection_id not in self.connections:
            self.connections.append(connection_id)
        session = self.sessions.get(connection_id)
        if session is None:
            session = self._open(connection_id, remaining)
            self.sessions[connection_id] = session
        try:
            result = session.invoke({"sql": sql, "params": params})
        except AdapterFailure as exc:
            raise failure_to_error(exc) from exc
        payload = result.result
        return list(payload.get("columns") or []), list(payload.get("rows") or []), result.truncated


@dataclass
class Result:
    rows: list[dict[str, Any]]  # Any: output rows (selected properties + _links)
    has_more: bool
    row_counts: dict[str, int]
    truncated: bool


class Executor:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.counts: dict[str, int] = {}
        self.total = 0
        self.truncated = False

    # ---------------------------------------------------------------- fetch
    def _fetch(
        self,
        node: NodePlan,
        extra: Sequence[FilterNode],
        *,
        limit: int | None,
        offset: int | None = None,
        order: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        cols = list(dict.fromkeys(node.columns.values()))
        plan = SourceQueryPlan(
            table=node.table,
            schema=node.schema,
            columns=cols,
            filters=[*node.filters, *extra],
            order_by=[(node.columns[k], "asc") for k in node.pk] if order else [],
            limit=limit,
            offset=offset,
        )
        names, raw, truncated = self.source.run(node.connection_id, plan)
        index = {n: i for i, n in enumerate(names)}
        rows: list[dict[str, Any]] = []
        for r in raw:
            rows.append(
                {k: normalize_value(node.ptypes[k], r[index[c]]) for k, c in node.columns.items()}
            )
        self.counts[node.object_type] = self.counts.get(node.object_type, 0) + len(rows)
        self.total += len(rows)
        return rows, truncated

    def _capped(self) -> bool:
        if self.total > MAX_TOTAL_ROWS:
            self.truncated = True
        return self.truncated and self.total > MAX_TOTAL_ROWS

    def first(self, node: NodePlan) -> list[dict[str, Any]]:
        """The first row of a single-object node (object resolution)."""
        rows, _ = self._fetch(node, [], limit=1)
        return rows

    # ---------------------------------------------------------------- root
    def run(self, root: NodePlan, limit: int, offset: int) -> Result:
        rows, truncated = self._fetch(root, [], limit=limit + 1, offset=offset, order=True)
        has_more = len(rows) > limit or truncated
        rows = rows[:limit]
        for hop in root.hops:
            if self._capped():
                break
            self.expand(rows, root, hop)
        return Result(
            rows=[self.shape(r, root) for r in rows],
            has_more=has_more,
            row_counts=dict(self.counts),
            truncated=self.truncated,
        )

    # ---------------------------------------------------------------- traversal
    def expand(self, parents: list[dict[str, Any]], start: NodePlan, hop: HopPlan) -> None:
        key_prop = start.pk[0]
        keys = list(dict.fromkeys(p[key_prop] for p in parents if p.get(key_prop) is not None))
        edges: list[tuple[Any, Any]] = []
        start_type = start.ptypes[key_prop]
        end_type = hop.target.ptypes[hop.target.pk[0]]
        if keys:
            for i in range(0, len(keys), KEY_BATCH):
                if self._capped():
                    break
                chunk = keys[i : i + KEY_BATCH]
                plan = SourceQueryPlan(
                    table=hop.edge_table,
                    schema=hop.edge_schema,
                    columns=list(dict.fromkeys([hop.start_column, hop.end_column])),
                    filters=[Filter(hop.start_column, "in", chunk), *hop.edge_filters],
                    limit=MAX_HOP_EDGES + 1,
                )
                names, raw, truncated = self.source.run(hop.edge_connection_id, plan)
                s_i, e_i = names.index(hop.start_column), names.index(hop.end_column)
                # edge values are normalized exactly like node values, or a numeric / decimal key
                # would never match the row it points to
                edges.extend(
                    (normalize_value(start_type, r[s_i]), normalize_value(end_type, r[e_i]))
                    for r in raw
                )
                self.total += len(raw)
                if truncated or len(edges) > MAX_HOP_EDGES:
                    self.truncated = True
                    edges = edges[:MAX_HOP_EDGES]
                    break
        end_keys = list(dict.fromkeys(e for _, e in edges if e is not None))
        found: dict[Any, dict[str, Any]] = {}
        target_pk = hop.target.pk[0]
        for i in range(0, len(end_keys), KEY_BATCH):
            if self._capped():
                break
            chunk = end_keys[i : i + KEY_BATCH]
            rows, truncated = self._fetch(
                hop.target, [Filter(hop.target.columns[target_pk], "in", chunk)], limit=None
            )
            self.truncated = self.truncated or truncated
            for row in rows:
                found.setdefault(row[target_pk], row)
        by_parent: dict[Any, list[dict[str, Any]]] = {}
        for start_key, end_key in edges:
            hit = found.get(end_key)
            if hit is None:
                continue
            bucket = by_parent.setdefault(start_key, [])
            if len(bucket) >= PER_PARENT_LINKS:
                self.truncated = True
            elif all(b is not hit for b in bucket):
                bucket.append(hit)
        # lineage counts the links the principal can SEE, never the raw edge rows
        linked = sum(len(b) for b in by_parent.values())
        self.counts[hop.relationship] = self.counts.get(hop.relationship, 0) + linked
        for parent in parents:
            parent.setdefault(LINKS, {})[hop.relationship] = by_parent.get(parent.get(key_prop), [])
        children = list(found.values())
        if hop.target.hops and children:
            for nested in hop.target.hops:
                self.expand(children, hop.target, nested)

    def shape(self, row: dict[str, Any], node: NodePlan) -> dict[str, Any]:
        out: dict[str, Any] = {k: row.get(k) for k in node.select}
        if node.hops:
            links = row.get(LINKS) or {}
            out[LINKS] = {
                hop.relationship: [
                    self.shape(c, hop.target) for c in links.get(hop.relationship, [])
                ]
                for hop in node.hops
            }
        return out

    # ---------------------------------------------------------------- relationship counts
    def count_links(
        self, start: NodePlan, hop: HopPlan, parent: dict[str, Any], cap: int
    ) -> tuple[int, bool]:
        """How many related rows the principal can see for one parent row (capped)."""
        rows = [dict(parent)]
        self.expand(rows, start, hop)
        links = rows[0].get(LINKS, {}).get(hop.relationship, [])
        return min(len(links), cap), len(links) >= cap


def pk_equality_for(auth: Authorized, values: Sequence[Any]) -> list[FilterNode]:  # Any: typed
    """Equality filters on the primary key columns of an authorized object type."""
    keys = list(auth.spec["primary_key"])
    missing = [k for k in keys if k not in auth.operands]
    if missing:
        raise MappingUnavailable("the primary key of the object type is not mapped")
    return [Filter(auth.operands[k].column, "=", v) for k, v in zip(keys, values, strict=True)]


def and_all(filters: Sequence[FilterNode]) -> FilterNode | None:
    if not filters:
        return None
    return filters[0] if len(filters) == 1 else FilterGroup("and", list(filters))


__all__ = [
    "EngineContext",
    "Executor",
    "HopPlan",
    "MappingUnavailable",
    "NodePlan",
    "Planner",
    "PropertyDenied",
    "QueryTimeout",
    "Result",
    "Source",
]
