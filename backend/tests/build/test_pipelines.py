"""build part 3a: the pipeline validator (`build.pipelines`, `build.pipeline_nodes`) and the executor
and node-type catalog (`build.catalog`), and the lookups they make of other objects (cross-client).

SETH tested the validator only through the API (`test_p6_build.py` and friends), so these are new
direct tests. `validate_pipeline` never reads the database: every lookup (`connection`, `version`,
`latest`, `model_problems`) is an `Env` call, answered here by `RepoEnv` from the repositories with
the caller's client id. For each lookup, client B's object must give the same issues (path, code,
message) as an unknown id, and A's own object (control) must resolve and be recorded as a reference.
The assertions run as `shifde_app` and as the schema owner (no row-level security: only the tenant
id in the lookup confines the caller).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.build import catalog
from shifde.build import repository as repo
from shifde.build.env import Env, VersionRef
from shifde.build.issues import Issue, Report
from shifde.build.pipelines import PipelineResult, validate_pipeline
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.models_admin import repository as models_repo
from tests.build.conftest import EnvFactory, World
from tests.build.env_support import add_published
from tests.connections.conftest import connection_values, discovery_values
from tests.models_admin.conftest import model_values, owner_connection, provider_values
from tests.tenancy.conftest import app_session

PROMPT: dict[str, Any] = {
    "sections": {"system": "You are a support assistant.", "context": "Question: {{message}}"},
    "variables": [{"name": "message", "type": "string", "required": True}],
}
NodeFor = Callable[[str], tuple[dict[str, Any], str]]


@dataclass(frozen=True)
class Side:
    tenant: UUID
    db_connection: UUID
    rest_connection: UUID
    prompt_artifact: UUID
    prompt_version: UUID
    tool_version: UUID
    agent_artifact: UUID
    agent_version: UUID
    ontology_artifact: UUID
    ontology_version: UUID
    model: UUID


def _connection(o: Session, tenant: UUID, **over: Any) -> UUID:
    values = connection_values(tenant, **over)
    conn_repo.insert_connection(o, values)
    o.execute(
        text("UPDATE shifde.connections SET status = 'active' WHERE id = :i"), {"i": values["id"]}
    )
    return UUID(str(values["id"]))


@pytest.fixture
def seed(owner_db: Database, world: World) -> tuple[Side, Side]:
    sides: list[Side] = []
    with owner_db.session() as o:
        for label, tenant in (("a", world.a.tenant), ("b", world.b.tenant)):
            db = _connection(
                o, tenant, connection_type="database", adapter_key="postgres", config={}
            )
            conn_repo.insert_discovery(
                # a database's discovery is a schema discovery (the service's `Env` reads no other)
                o,
                {**discovery_values(tenant, db), "kind": "schema", "payload": {"owner": label}},
            )
            rest = _connection(o, tenant, access_mode="read_write")
            provider = provider_values(tenant, owner_connection(o, tenant))
            models_repo.insert_provider(o, provider)
            model = model_values(tenant, provider["id"])
            models_repo.insert_model(o, model)
            p_art, p_ver = add_published(o, tenant, "prompts", f"prompt-{label}", PROMPT)
            _, t_ver = add_published(o, tenant, "tools", f"tool-{label}", {"executor": "rest.http"})
            g_art, g_ver = add_published(o, tenant, "agents", f"agent-{label}", {"x": 1})
            o_art, o_ver = add_published(
                o, tenant, "ontologies", f"ontology-{label}", {"object_types": []}
            )
            for artifact in (p_art, g_art, o_art):
                repo.refresh_latest_published(o, tenant, artifact)
            sides.append(
                Side(tenant, db, rest, p_art, p_ver, t_ver, g_art, g_ver, o_art, o_ver, model["id"])
            )
        o.commit()
    return sides[0], sides[1]


Opener = Callable[[Database, Database, UUID], AbstractContextManager[Session]]


def _app(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return app_session(app_db, tenant)


def _owner(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    # the schema owner is not subject to row-level security: only the tenant id confines A
    return owner_db.session()


@pytest.fixture(params=[_app, _owner], ids=["as-app-role", "as-owner-no-rls"])
def open_session(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], AbstractContextManager[Session]]:
    opener: Opener = request.param
    return lambda tenant: opener(app_db, owner_db, tenant)


def _signature(issues: list[Issue]) -> list[tuple[str, str, str]]:
    return [(i.path, i.code, i.message) for i in issues]


def _spec(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "params": {"type": "object", "properties": {}, "additionalProperties": False},
        "policies": {"max_parallel": 2, "deadline_seconds": 600, "on_failure": "fail_fast"},
        "nodes": nodes,
        "edges": [],
    }


def _validate(
    env: Env, nodes: list[dict[str, Any]], *, resolve: bool = True
) -> tuple[Report, PipelineResult]:
    report = Report()
    return report, validate_pipeline(_spec(nodes), env, report, 4, resolve=resolve)


# one node per kind of lookup: the node naming `ref`, and the path its issues are reported at
def _db_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"connection_id": ref, "sql": "SELECT 1"}
    return {"id": "n", "type": "db_query", "config": cfg}, "config.connection_id"


def _data_source_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"connection_id": ref, "dataset": "t"}
    return {"id": "n", "type": "data_source", "config": cfg}, "config.connection_id"


def _api_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"connection_id": ref, "path_template": "/x"}
    return {"id": "n", "type": "api_request", "config": cfg}, "config.connection_id"


def _sink_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"target": "connection", "connection_id": ref, "path_template": "/x"}
    return {"id": "n", "type": "sink", "config": cfg}, "config.connection_id"


def _model_node(ref: str) -> tuple[dict[str, Any], str]:
    return {"id": "n", "type": "model", "config": {"model_id": ref}}, "config.model_id"


def _tool_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"tool_version_id": ref}
    return {"id": "n", "type": "tool", "config": cfg}, "config.tool_version_id"


def _agent_node(ref: str) -> tuple[dict[str, Any], str]:
    cfg = {"agent_version_id": ref}
    return {"id": "n", "type": "agent", "config": cfg}, "config.agent_version_id"


def _prompt_node(model: UUID) -> NodeFor:
    def build(ref: str) -> tuple[dict[str, Any], str]:
        cfg = {"prompt_version_id": ref, "model_id": str(model)}
        return {"id": "n", "type": "prompt", "config": cfg}, "config.prompt_version_id"

    return build


def _ontology_node(ontology: UUID) -> NodeFor:
    def build(ref: str) -> tuple[dict[str, Any], str]:
        cfg = {
            "ontology_id": str(ontology),
            "version_id": ref,
            "query": {"object_type": "x", "select": ["a"]},
        }
        return {"id": "n", "type": "ontology_query", "config": cfg}, "config.version_id"

    return build


# (label, A's object as the reference text, B's object, the node builder for A, reference type)
CASES: list[tuple[str, Callable[[Side], str], Callable[[Side], NodeFor], str]] = [
    ("db_query connection", lambda s: str(s.db_connection), lambda s: _db_node, "connection"),
    (
        "data_source connection",
        lambda s: str(s.db_connection),
        lambda s: _data_source_node,
        "connection",
    ),
    ("api_request connection", lambda s: str(s.rest_connection), lambda s: _api_node, "connection"),
    ("sink connection", lambda s: str(s.rest_connection), lambda s: _sink_node, "connection"),
    ("model node model", lambda s: str(s.model), lambda s: _model_node, "model"),
    ("tool version", lambda s: str(s.tool_version), lambda s: _tool_node, "tool_version"),
    ("agent version", lambda s: str(s.agent_version), lambda s: _agent_node, "agent_version"),
    (
        "agent latest",
        lambda s: f"latest:{s.agent_artifact}",
        lambda s: _agent_node,
        "agent_version",
    ),
    (
        "prompt version",
        lambda s: str(s.prompt_version),
        lambda s: _prompt_node(s.model),
        "prompt_version",
    ),
    (
        "prompt latest",
        lambda s: f"latest:{s.prompt_artifact}",
        lambda s: _prompt_node(s.model),
        "prompt_version",
    ),
    (
        "ontology version",
        lambda s: str(s.ontology_version),
        lambda s: _ontology_node(s.ontology_artifact),
        "ontology_version",
    ),
    (
        "ontology latest",
        lambda s: f"latest:{s.ontology_artifact}",
        lambda s: _ontology_node(s.ontology_artifact),
        "ontology_version",
    ),
]


@pytest.mark.parametrize(("label", "own", "builder", "ref_type"), CASES, ids=[c[0] for c in CASES])
def test_a_pipeline_naming_bs_object_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    label: str,
    own: Callable[[Side], str],
    builder: Callable[[Side], NodeFor],
    ref_type: str,
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        node_for = builder(a)
        # A's own object resolves: no error at the reference, one typed reference recorded
        node, field = node_for(own(a))
        report, result = _validate(env, [node])
        at_ref = [e for e in report.errors if e.path.endswith(field)]
        assert at_ref == [], (label, _signature(at_ref))
        assert ref_type in {r[1] for r in result.refs}, (label, result.refs)
        # B's object: the same issues as an unknown id, and nothing recorded for it
        theirs = own(b)
        unknown = f"latest:{uuid7()}" if theirs.startswith("latest:") else str(uuid7())
        their_report, their_result = _validate(env, [node_for(theirs)[0]])
        unknown_report, unknown_result = _validate(env, [node_for(unknown)[0]])
        assert [e for e in their_report.errors if e.path.endswith(field)], label
        assert _signature(their_report.errors) == _signature(unknown_report.errors), label
        assert _signature(their_report.warnings) == _signature(unknown_report.warnings), label
        assert their_result.refs == unknown_result.refs, label
        assert their_result.pins == unknown_result.pins, label
        assert ref_type not in {r[1] for r in their_result.refs} or ref_type == "model"
        s.rollback()


def test_a_prompt_node_naming_bs_model_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)

        def prompt_with(model: UUID) -> dict[str, Any]:
            cfg = {"prompt_version_id": str(a.prompt_version), "model_id": str(model)}
            return {"id": "n", "type": "prompt", "config": cfg}

        own_report, own_result = _validate(env, [prompt_with(a.model)])
        theirs, their_result = _validate(env, [prompt_with(b.model)])
        unknown, _ = _validate(env, [prompt_with(uuid7())])
        assert not [e for e in own_report.errors if e.path.endswith("config.model_id")]
        assert ("model", a.model) in {(r[1], r[2]) for r in own_result.refs}
        assert [e.code for e in theirs.errors if e.path.endswith("config.model_id")] == [
            "ref_cross_tenant"
        ]
        assert _signature(theirs.errors) == _signature(unknown.errors)
        assert b.model not in {r[2] for r in their_result.refs}
        s.rollback()


def test_a_published_pipeline_must_pin_and_never_resolves_latest(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        for side in (a, b):
            node = _agent_node(f"latest:{side.agent_artifact}")[0]
            report, _ = _validate(env, [node], resolve=False)
            assert [e.code for e in report.errors if e.path.endswith("agent_version_id")] == [
                "ref_not_published"
            ]
        s.rollback()


def test_structural_errors_carry_paths_and_codes(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        report = Report()
        validate_pipeline({"schema_version": "2", "nodes": "x"}, env, report, 4)
        assert ("schema_version", "unsupported_schema_version") in {
            (e.path, e.code) for e in report.errors
        }
        report, _ = _validate(
            env,
            [
                {"id": "n", "type": "model", "config": {"model_id": str(a.model)}},
                {"id": "n", "type": "model", "config": {"model_id": str(a.model)}},
            ],
        )
        assert any(e.code == "duplicate_node_id" for e in report.errors)
        s.rollback()


# ------------------------------------------------------------------------- the catalog
def test_the_catalog_is_installation_level_and_names_no_client(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
) -> None:
    a, b = seed
    registry = AdapterRegistry.load()
    outputs: dict[UUID, str] = {}
    for tenant in (a.tenant, b.tenant):
        with open_session(tenant) as s:
            executors = catalog.executor_catalog(s, registry)
            nodes = catalog.node_type_catalog(s, registry)
            outputs[tenant] = executors.model_dump_json() + nodes.model_dump_json()
            s.rollback()
    # the same answer in either client's context, and no client id, name or object id in it
    assert outputs[a.tenant] == outputs[b.tenant]
    blob = outputs[a.tenant]
    for side in (a, b):
        for value in (
            side.tenant,
            side.db_connection,
            side.rest_connection,
            side.prompt_version,
            side.agent_version,
            side.model,
        ):
            assert str(value) not in blob
    assert "builtin.function" in blob and "db_query" in blob


# ------------------------------------------------------------------------- the other Env lookups
def test_schema_discovery_of_bs_connection_gives_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        assert env.schema_discovery(a.db_connection) == {"owner": "a"}  # control
        assert env.schema_discovery(b.db_connection) is None
        assert env.schema_discovery(uuid7()) is None
        s.rollback()


def test_permits_is_false_for_an_artifact_outside_the_callers_client(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        own = env.version(a.agent_version)
        assert own is not None and env.permits("agent:execute", own)  # control
        assert env.version(b.agent_version) is None
        # a VersionRef built by hand for B's artifact, or for none, is refused too
        foreign = VersionRef(
            version_id=b.agent_version,
            artifact_id=b.agent_artifact,
            kind="agents",
            version_no=1,
            state="published",
            spec={},
            spec_hash="h",
            artifact_key="agent-b",
            artifact_name="agent-b",
            artifact_status="active",
        )
        nobody = VersionRef(**{**foreign.__dict__, "artifact_id": uuid7()})
        assert not env.permits("agent:execute", foreign)
        assert not env.permits("agent:execute", nobody)
        # the scope the reference claims must be the artifact's own
        moved = VersionRef(**{**own.__dict__, "workspace_id": uuid7()})
        assert not env.permits("agent:execute", moved)
        s.rollback()
