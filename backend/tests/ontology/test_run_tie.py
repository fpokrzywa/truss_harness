"""Ontology review 2 G2 (runtime part 5, B-2131): the unchecked path (`api=False`) of `query`,
`get_object` and `action_inputs` reads an ontology only for a run of this client that executes
it, through the `run_of` contract (B-1933): an ontology action run of one of its versions, a
pipeline run whose version pins the version read, or an agent run whose version pins the
`ontology.query` tool. A run of B, a run of another artifact of A (another ontology's action, an
agent without the tool, a pipeline pinning another ontology) and a version the run does not pin
all answer like an unknown run, as `shifde_app` and as the schema owner, and write no lineage."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import ontologies
from shifde.build import repository as build_repo
from shifde.build.executors import ONTOLOGY_QUERY
from shifde.core.db import Database
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ontology import schemas as s
from shifde.ontology.service import OntologyService
from tests._support.runs import add_run
from tests.build.conftest import opener
from tests.build.env_support import add_published
from tests.build.service_support import service_for
from tests.ontology.test_routes_cross_client import TYPE, Side, Two, two

__all__ = ["opener", "two"]

Opener = Callable[[UUID], AbstractContextManager[Session]]
GONE = "NotFound: run not found"
TOOL = {"executor": "builtin.function", "binding": {"function_key": ONTOLOGY_QUERY}}


@dataclass(frozen=True)
class Runs:
    version: UUID  # the published version of `side.ontology`
    action: UUID  # ontology action run of `side.ontology`'s version
    pipeline: UUID  # pipeline run whose version pins that version
    agent_tool: UUID  # agent run whose version pins the `ontology.query` tool
    other_action: UUID  # action run of `side.other` (another ontology of the client)
    other_pipeline: UUID  # pipeline run pinning `side.other`'s version only
    agent: UUID  # agent run without the tool
    plain_tool: UUID  # agent run pinning a tool that is not the ontology tool


def _latest(o: Session, ontology: str) -> UUID:
    found: UUID = o.execute(
        text("SELECT latest_published_version_id FROM shifde.artifacts WHERE id = :a"),
        {"a": UUID(ontology)},
    ).scalar_one()
    return found


def _runs(o: Session, side: Side) -> Runs:
    t = side.tenant
    version, other_version = _latest(o, side.ontology), _latest(o, side.other)

    def action(ontology: str, ver: UUID) -> UUID:
        return add_run(o, t, kind="ontology_action", ontology_id=UUID(ontology),
                       ontology_version_id=ver, action_key="act")  # fmt: skip

    def pipeline(pinned: UUID) -> UUID:
        pipe, pipe_version = add_published(o, t, "pipelines", f"pipe-{uuid7().hex}", {})
        build_repo.insert_pins(o, t, pipe_version, [(pinned, "ontology")])
        return add_run(o, t, kind="pipeline", pipeline_id=pipe, pipeline_version_id=pipe_version)

    def agent(tool_spec: dict[str, object] | None) -> UUID:
        agent_id, agent_version = add_published(o, t, "agents", f"agent-{uuid7().hex}", {})
        if tool_spec is not None:
            _, tool = add_published(o, t, "tools", f"tool-{uuid7().hex}", tool_spec)
            build_repo.insert_pins(o, t, agent_version, [(tool, "tool")])
        return add_run(o, t, agent_id=agent_id, agent_version_id=agent_version)

    return Runs(
        version=version,
        action=action(side.ontology, version),
        pipeline=pipeline(version),
        agent_tool=agent(TOOL),
        other_action=action(side.other, other_version),
        other_pipeline=pipeline(other_version),
        agent=agent(None),
        plain_tool=agent({"executor": "builtin.function", "binding": {"function_key": "x"}}),
    )


def _answer(session: Session, call: Callable[..., object], *args: object) -> str:
    try:
        call(*args)
    except AppError as exc:
        session.rollback()
        return f"{type(exc).__name__}: {exc.detail}"
    session.rollback()
    return "ok"


Call = Callable[[OntologyService, UUID, UUID, UUID | None], object]
CALLS: dict[str, Call] = {
    "query": lambda svc, onto, run, ver: svc.query(
        onto, s.QueryIn(object_type=TYPE, select=["id"], version_id=ver), api=False, run_id=run
    ),
    "get_object": lambda svc, onto, run, ver: svc.get_object(
        onto, TYPE, "1", version_id=ver, api=False, run_id=run
    ),
    "action_inputs": lambda svc, onto, run, ver: svc.action_inputs(
        onto, ver or uuid7(), "act", "1", api=False, run_id=run
    ),
}


def _lineage(owner_db: Database, tenant: UUID) -> int:
    with owner_db.session() as o:
        count: int = o.execute(
            text("SELECT count(*) FROM shifde.ontology_query_lineage WHERE tenant_id = :t"),
            {"t": tenant},
        ).scalar_one()
        return count


@pytest.mark.parametrize("method", sorted(CALLS))
def test_the_unchecked_path_reads_an_ontology_only_for_a_run_that_executes_it(
    method: str, opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    a, b = two.a, two.b
    with owner_db.session() as o:
        ra, rb = _runs(o, a), _runs(o, b)
        o.commit()
    call = CALLS[method]
    onto = UUID(a.ontology)
    refused = {
        "a run of B (B's action run)": (rb.action, ra.version),
        "a run of B (B's agent run with the tool)": (rb.agent_tool, ra.version),
        "an unknown run": (uuid7(), ra.version),
        "another ontology's action run": (ra.other_action, ra.version),
        "a pipeline pinning another ontology": (ra.other_pipeline, ra.version),
        "an agent run without the ontology tool": (ra.agent, ra.version),
        "an agent run with another builtin tool": (ra.plain_tool, ra.version),
        "the action run with a version it does not run": (ra.action, _latest_of(owner_db, a)),
    }
    if method != "query":
        # the agent-tool rule opens `query` only (its Execution Gate checks visibility); an
        # agent run reaches `get_object` / `action_inputs` only for a version it pins
        # (runtime isolation review 4 item 2, B-2143)
        refused["an agent run with the ontology tool"] = (ra.agent_tool, ra.version)
    before = _lineage(owner_db, a.tenant)
    with opener(a.tenant) as session:
        svc = OntologyService(service_for(session, a.tenant, a.user, settings))
        for name, (run, ver) in refused.items():
            assert _answer(session, call, svc, onto, run, ver) == GONE, name
        assert _lineage(owner_db, a.tenant) == before
        # not vacuous: each run that executes the ontology gets past the anchor
        tied = {
            "its action run": (ra.action, ra.version),
            "a pipeline pinning its version": (ra.pipeline, ra.version),
        }
        if method == "query":
            tied["an agent run with the ontology tool"] = (ra.agent_tool, ra.version)
            tied["an agent run with the tool, latest version"] = (ra.agent_tool, None)
        for name, (run, ver) in tied.items():
            assert _answer(session, call, svc, onto, run, ver) != GONE, name


def _latest_of(owner_db: Database, side: Side) -> UUID:
    """The published version of the client's OTHER ontology (a version the action run does not
    run, though of the same client)."""
    with owner_db.session() as o:
        return _latest(o, side.other)


def test_the_api_path_still_needs_only_a_run_of_the_client(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    """A named run on the checked path (`api=True`) is attribution only: any run of the client,
    never B's (kept from part 2, B-2008)."""
    a, b = two.a, two.b
    with owner_db.session() as o:
        ra, rb = _runs(o, a), _runs(o, b)
        o.commit()
    with opener(a.tenant) as session:
        svc = OntologyService(service_for(session, a.tenant, a.user, settings))
        assert svc._anchor_run(ra.agent, api=True) is not None
        for run in (rb.agent, uuid7()):
            body = s.QueryIn(object_type=TYPE, select=["id"])
            assert _answer(session, _api_query, svc, UUID(a.ontology), body, run) == GONE


def test_preparing_an_ontology_for_its_run_validates_the_pinned_versions_mappings(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    """Runtime isolation review 3 item 4: `prepare(..., run_of=)` for an ontology validates the
    mappings frozen with the version the run executes, not the draft's (here there is no draft:
    the draft path would validate no mappings at all)."""
    a = two.a
    with owner_db.session() as o:
        ra = _runs(o, a)
        o.commit()
    with opener(a.tenant) as session:
        arts = service_for(session, a.tenant, a.user, settings)
        art = build_repo.get_artifact(session, a.tenant, UUID(a.ontology))
        version = build_repo.get_version(session, a.tenant, ra.version)
        assert art is not None and version is not None
        assert build_repo.get_draft(session, a.tenant, art["id"]) is None
        pinned = build_repo.list_mappings(session, a.tenant, ra.version)
        assert pinned
        run = {"id": ra.action, "tenant_id": a.tenant}
        prepared = arts.prepare(art, dict(version["spec"]), resolve=False, run_of=run)
        assert prepared.mappings_hash == ontologies.mappings_digest(pinned)
        assert prepared.mappings_hash != ontologies.mappings_digest([])
        session.rollback()


def _api_query(svc: OntologyService, onto: UUID, body: s.QueryIn, run: UUID) -> object:
    return svc.query(onto, body, run_id=run)
