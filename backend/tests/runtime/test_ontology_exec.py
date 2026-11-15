"""Runtime part 5 (RT-5): `runtime/ontology_exec.py` (the `ontology.query` tool and the
`ontology_query` node's gate and query) inside a run of client A.

Isolation: every function that takes an ontology id is called in A's run with B's ontology id and
with an unknown id, as `shifde_app` and as the schema owner (no row-level security); both
answers are the same, B's ontology rows (lineage, decisions, audit) are unchanged, and A's own
ontology gives another answer (not vacuous). The unchecked query path is tied to the run's own
version (ontology review 2 G2, `tests/ontology/test_run_tie.py`)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository as build_repo
from shifde.build.executors import ONTOLOGY_QUERY
from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.errors import AppError, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import ontology_exec, state
from tests._support.runs import add_run
from tests.build.conftest import opener
from tests.build.env_support import add_published
from tests.ontology.conftest import owner_session, people, sides, world
from tests.ontology.test_routes_cross_client import TYPE, Side, Two, two
from tests.runtime.execution import run_context, run_row

__all__ = ["opener", "owner_session", "people", "sides", "two", "world"]

Opener = Callable[[UUID], AbstractContextManager[Session]]
TOOL = {"executor": "builtin.function", "binding": {"function_key": ONTOLOGY_QUERY}}
QUERY = {"object_type": TYPE, "select": ["id"]}


def test_the_ontology_tool_is_recognised_and_labels_its_results() -> None:
    assert ontology_exec.is_ontology_tool(TOOL)
    assert not ontology_exec.is_ontology_tool({**TOOL, "executor": "http"})
    assert not ontology_exec.is_ontology_tool({"executor": "builtin.function"})
    out = {"object_type": TYPE, "rows": []}
    assert ontology_exec.provenance(TOOL, out) == f"ontology:{TYPE}"
    assert ontology_exec.provenance(TOOL, {"rows": []}) is None
    assert ontology_exec.provenance({"executor": "http"}, out) is None
    assert ontology_exec.provenance(TOOL, {"object_type": "x" * 500}) == "ontology:" + "x" * 200


def test_a_query_document_is_validated_and_the_version_is_merged() -> None:
    version = uuid7()
    body = ontology_exec.query_in(QUERY, version)
    assert body.object_type == TYPE and body.version_id == version
    assert ontology_exec.query_in(QUERY, None).version_id is None
    with pytest.raises(ValidationFailed, match="invalid ontology query"):
        ontology_exec.query_in({"select": ["id"]}, None)
    with pytest.raises(ValidationFailed, match="ontology_id must be a UUID"):
        ontology_exec._uuid("nope", "ontology_id")


def _running_agent_run(o: Session, side: Side) -> UUID:
    """A running agent run of `side`'s owner whose version pins the `ontology.query` tool."""
    agent, agent_version = add_published(o, side.tenant, "agents", f"agent-{uuid7().hex}", {})
    _, tool = add_published(o, side.tenant, "tools", f"tool-{uuid7().hex}", TOOL)
    build_repo.insert_pins(o, side.tenant, agent_version, [(tool, "tool")])
    return add_run(o, side.tenant, agent_id=agent, agent_version_id=agent_version,
                   status="running", lease_token=uuid7(), started_at=utcnow(),
                   initiated_by_principal_id=side.user)  # fmt: skip


def _ontology_rows(o: Session, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    o.rollback()
    return tuple(
        o.execute(text(q), {"t": tenant}).scalar_one()
        for q in (
            "SELECT count(*) FROM shifde.ontology_query_lineage WHERE tenant_id = :t",
            "SELECT count(*) FROM shifde.policy_decisions WHERE tenant_id = :t",
            "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t",
            "SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t",
        )
    )


Case = Callable[[Any, UUID], object]  # Any: RunContext
CASES: dict[str, Case] = {
    "gate_query": lambda ctx, onto: ontology_exec.gate_query(ctx, onto, None, "test"),
    "tool_gate": lambda ctx, onto: ontology_exec.tool_gate(ctx, {"ontology_id": str(onto)}),
    "run_query": lambda ctx, onto: ontology_exec.run_query(
        ctx, onto, None, QUERY, channel="agent", purpose="test"
    ),
    "tool_query": lambda ctx, onto: ontology_exec.tool_query(
        ctx, {"ontology_id": str(onto), "query": QUERY}, "test"
    ),
}


def _answer(s: Session, case: Case, ctx: Any, onto: UUID) -> str:  # Any: RunContext
    try:
        result = case(ctx, onto)
        s.commit()
    except (AppError, state.GateDenied) as exc:
        s.rollback()
        return f"{type(exc).__name__}: {exc}"
    return "ok" if result is None else type(result).__name__


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_run_of_a_naming_bs_ontology_gets_the_unknown_id_answer(
    name: str, opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    a, b = two.a, two.b
    case = CASES[name]
    with owner_db.session() as o:
        run = _running_agent_run(o, a)
        o.commit()
        b_before = _ontology_rows(o, b.tenant)
    with opener(a.tenant) as s:
        # the worker sets the job's client before it touches the run (B-2103); the owner session
        # gets the same context (row-level security does not apply to it, only the service's ids)
        set_tenant(s, a.tenant)
        ctx = run_context(s, settings, run_row(s, a.tenant, run))
        for_b = _answer(s, case, ctx, UUID(b.ontology))
        for_unknown = _answer(s, case, ctx, uuid7())
        for_own = _answer(s, case, ctx, UUID(a.ontology))
    assert for_b == for_unknown, (for_b, for_unknown)
    assert for_own != for_unknown, for_own
    with owner_db.session() as o:
        assert _ontology_rows(o, b.tenant) == b_before, "B's rows changed"


def test_every_function_taking_an_ontology_is_a_case() -> None:
    public = {
        n
        for n in dir(ontology_exec)
        if not n.startswith("_")
        and callable(getattr(ontology_exec, n))
        and getattr(getattr(ontology_exec, n), "__module__", "") == ontology_exec.__name__
    }
    # pure: `is_ontology_tool` and `provenance` read a spec and an output, `query_in` a document
    assert public - set(CASES) == {"is_ontology_tool", "provenance", "query_in"}


def _latest_version(o: Session, ontology: str) -> UUID:
    vid: UUID = o.execute(
        text(
            "SELECT id FROM shifde.artifact_versions WHERE artifact_id = :o "
            "ORDER BY version_no DESC LIMIT 1"
        ),
        {"o": UUID(ontology)},
    ).scalar_one()
    return vid


def test_as_ontology_with_bs_version_gets_the_unknown_version_answer(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    """Runtime isolation review 4 item 4: A's ontology named with B's version id answers like an
    unknown version id, on `gate_query` and `run_query`; A's own version answers otherwise."""
    a, b = two.a, two.b
    with owner_db.session() as o:
        run = _running_agent_run(o, a)
        o.commit()
        a_version, b_version = _latest_version(o, a.ontology), _latest_version(o, b.ontology)
        b_before = _ontology_rows(o, b.tenant)
    calls: dict[str, Callable[[Any, UUID], object]] = {
        "gate_query": lambda ctx, v: ontology_exec.gate_query(ctx, UUID(a.ontology), v, "test"),
        "run_query": lambda ctx, v: ontology_exec.run_query(
            ctx, UUID(a.ontology), v, QUERY, channel="agent", purpose="test"
        ),
    }
    with opener(a.tenant) as s:
        set_tenant(s, a.tenant)
        ctx = run_context(s, settings, run_row(s, a.tenant, run))
        for name, call in calls.items():
            for_b = _answer(s, call, ctx, b_version)
            for_unknown = _answer(s, call, ctx, uuid7())
            for_own = _answer(s, call, ctx, a_version)
            assert for_b == for_unknown, (name, for_b, for_unknown)
            assert for_own != for_unknown, (name, for_own)
    with owner_db.session() as o:
        assert _ontology_rows(o, b.tenant) == b_before, "B's rows changed"
