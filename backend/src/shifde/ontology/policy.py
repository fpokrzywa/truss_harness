"""Ontology access policy evaluation (P7 contract section 3). Pure functions, no I/O.

Default deny: a principal sees an object type only through policies that allow it. A policy names
a subject (a role the principal holds at the ontology's scope, a group it belongs to, or the
principal itself), optionally restricts the properties it covers (`property_keys` NULL = every
NON-restricted property) and optionally a row filter. A property classified `restricted` is
covered only when a policy names it explicitly.

Row semantics: a row is visible when, for EVERY property the query touches (selected or filtered),
at least one grant covering that property admits the row. A grant whose row filter cannot be
evaluated (an unknown column, a principal attribute that is not set) confers nothing: fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any  # Any: attribute values
from uuid import UUID

from shifde.adapters.base import FilterGroup, FilterNode
from shifde.build import ontology_expr as expr


@dataclass(frozen=True)
class Subjects:
    """Who is asking: the principal and the roles / groups it holds for this ontology."""

    principal_id: UUID
    principal_type: str
    role_ids: frozenset[UUID]
    group_ids: frozenset[UUID]
    attributes: Mapping[str, Any]  # Any: scalars / lists of scalars

    def context(self) -> dict[str, Any]:  # Any: what `principal.*` paths read
        return {
            "id": str(self.principal_id),
            "type": self.principal_type,
            "attributes": dict(self.attributes),
        }

    def matches(self, subject_type: str, subject_id: UUID) -> bool:
        if subject_type == "role":
            return subject_id in self.role_ids
        if subject_type == "group":
            return subject_id in self.group_ids
        return subject_type == "user" and subject_id == self.principal_id


@dataclass(frozen=True)
class Grant:
    """What one matching policy lets the principal read of one object type."""

    policy_id: UUID
    properties: frozenset[str]
    row_filter: FilterNode | None  # None = every row


def covered_properties(
    object_type: Mapping[str, Any],  # Any: spec object type
    property_keys: Sequence[str] | None,
) -> frozenset[str]:
    """The properties a policy covers: the named ones, or every non-restricted one."""
    props = {p["key"]: p for p in object_type["properties"]}
    if property_keys is None:
        return frozenset(k for k, p in props.items() if p["classification"] != "restricted")
    return frozenset(k for k in property_keys if k in props)


def grants_for(
    policies: Sequence[Mapping[str, Any]],  # Any: policy rows
    subjects: Subjects,
    object_type: Mapping[str, Any],  # Any: spec object type
    columns: Mapping[str, expr.Operand],
) -> list[Grant]:
    """The grants of `subjects` on `object_type`. `columns` maps property keys to the mapped
    columns (for the row filters)."""
    out: list[Grant] = []
    context = subjects.context()
    resolve = expr.row_resolver({op.key: op for op in columns.values()})
    for p in policies:
        if p["object_type_key"] != object_type["key"] or p["effect"] != "allow":
            continue
        if not subjects.matches(p["subject_type"], p["subject_id"]):
            continue
        row_filter: FilterNode | None = None
        if p["row_filter"]:
            try:
                row_filter = expr.compile_filter(p["row_filter"], resolve, principal=context)
            except expr.FilterError:
                continue  # cannot be evaluated: the policy does not apply (fail closed)
        props = covered_properties(object_type, p["property_keys"])
        if props:
            out.append(Grant(p["id"], props, row_filter))
    return out


def readable(grants: Sequence[Grant]) -> frozenset[str]:
    return frozenset().union(*(g.properties for g in grants)) if grants else frozenset()


def denied_properties(grants: Sequence[Grant], wanted: Sequence[str]) -> list[str]:
    allowed = readable(grants)
    return [w for w in dict.fromkeys(wanted) if w not in allowed]


def row_predicate(grants: Sequence[Grant], touched: Sequence[str]) -> FilterNode | None:
    """The row-level condition for a query touching `touched` (all already readable): AND over
    the distinct sets of admitting grants of each property, each set an OR of its row filters. A
    grant without a row filter admits every row, which makes its OR (and the term) vacuous."""
    terms: dict[frozenset[int], FilterNode | None] = {}
    for prop in dict.fromkeys(touched):
        idx = frozenset(i for i, g in enumerate(grants) if prop in g.properties)
        if idx in terms:
            continue
        filters = [grants[i].row_filter for i in sorted(idx)]
        if any(f is None for f in filters):
            terms[idx] = None
        else:
            nodes = [f for f in filters if f is not None]
            terms[idx] = nodes[0] if len(nodes) == 1 else FilterGroup("or", nodes)
    parts = [t for t in terms.values() if t is not None]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else FilterGroup("and", parts)


def any_grant_predicate(grants: Sequence[Grant]) -> FilterNode | None:
    """The rows ANY grant admits (None: every row): used to count related rows."""
    filters = [g.row_filter for g in grants]
    if any(f is None for f in filters):
        return None
    nodes = [f for f in filters if f is not None]
    return nodes[0] if len(nodes) == 1 else FilterGroup("or", nodes)


def property_groups(grants: Sequence[Grant], props: Sequence[str]) -> list[list[str]]:
    """`props` grouped by the set of grants admitting them (properties of one group are visible
    on exactly the same rows); order of first appearance."""
    groups: dict[frozenset[int], list[str]] = {}
    for prop in dict.fromkeys(props):
        idx = frozenset(i for i, g in enumerate(grants) if prop in g.properties)
        groups.setdefault(idx, []).append(prop)
    return list(groups.values())
