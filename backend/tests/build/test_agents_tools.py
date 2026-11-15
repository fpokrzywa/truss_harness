"""build part 2b: the agent and tool validators and the lookups they make of other objects
(cross-client).

SETH tested both only through the API (`test_p4_agents.py`, `test_p4_tools.py`), so these are new
direct tests: the structural rules with the `Env` answering from `build.repository` and the other
modules' repositories with the caller's client id (`tests/build/env_support.py`), and for every
lookup (`version`, `latest`, `route_model_ids` / `model_problems`, `connection`, `mcp_tool`) that
client B's object gets the same issues as an unknown id and A's own object (control) validates.
The assertions run as `shifde_app` and as the schema owner (no row-level security: only the tenant
id in the lookup confines the caller).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository as repo
from shifde.build.agents import DEFAULT_MAXIMA, validate_agent
from shifde.build.env import Env
from shifde.build.issues import Issue, Report
from shifde.build.tools import validate_tool
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.models_admin import repository as models_repo
from tests.build.conftest import EnvFactory, World
from tests.build.env_support import add_published
from tests.connections.conftest import connection_values, discovery_values
from tests.models_admin.conftest import (
    model_values,
    owner_connection,
    provider_values,
    route_values,
)
from tests.tenancy.conftest import app_session

PROMPT: dict[str, Any] = {
    "sections": {"system": "You are a support assistant.", "context": "Question: {{message}}"},
    "variables": [{"name": "message", "type": "string", "required": True}],
}
TOOL_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}


def _tool(connection: UUID | None, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "executor": "rest.http",
        "binding": {
            "connection_id": str(connection),
            "method": "GET",
            "path_template": "/echo",
            "query_template": {"n": "{n}"},
        },
        "input_schema": TOOL_SCHEMA,
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    spec.update(over)
    return spec


def _mcp_tool(connection: UUID, name: str) -> dict[str, Any]:
    return _tool(
        None,
        executor="mcp.tool",
        binding={"connection_id": str(connection), "tool_name": name},
    )


def _agent(prompt: UUID | str, model: dict[str, Any], **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "prompt_version_id": str(prompt),
        "model": model,
        "tool_version_ids": [],
        "skill_version_ids": [],
        "memory": {"mode": "none", "max_turns": 0},
        "limits": {"max_iterations": 5, "max_tool_calls": 5, "max_tokens": 1000, "deadline_s": 60},
        "execution_permissions": {
            "allowed_principal_types": ["user"],
            "required_permission": "agent:execute",
        },
        "approval_rules": {},
        "variables_map": {"message": "{{input.message}}"},
    }
    spec.update(over)
    return spec


@dataclass(frozen=True)
class Side:
    tenant: UUID
    prompt_artifact: UUID
    prompt_version: UUID
    skill_version: UUID
    tool_artifact: UUID
    tool_version: UUID
    model: UUID
    route: UUID
    rest_connection: UUID
    mcp_connection: UUID


def _insert_connection(o: Session, tenant: UUID, **over: Any) -> UUID:
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
            rest = _insert_connection(o, tenant)
            mcp = _insert_connection(
                o,
                tenant,
                connection_type="mcp",
                adapter_key="mcp_http",
                config={"url": "https://mcp.example.com/mcp"},
            )
            conn_repo.insert_discovery(
                o,
                {
                    **discovery_values(tenant, mcp),
                    "kind": "mcp_tools",
                    "payload": {
                        "tools": [
                            {
                                "name": f"lookup-{label}",
                                "description": "d",
                                "input_schema": TOOL_SCHEMA,
                                "read_only": True,
                            }
                        ]
                    },
                },
            )
            provider = provider_values(tenant, owner_connection(o, tenant))
            models_repo.insert_provider(o, provider)
            model = model_values(tenant, provider["id"])
            models_repo.insert_model(o, model)
            route = route_values(tenant, model["id"])
            models_repo.insert_route(o, route)
            p_art, p_ver = add_published(o, tenant, "prompts", f"prompt-{label}", PROMPT)
            _, s_ver = add_published(
                o, tenant, "skills", f"skill-{label}", {"instructions_md": "x"}
            )
            t_art, t_ver = add_published(o, tenant, "tools", f"tool-{label}", _tool(rest))
            for artifact in (p_art, t_art):
                repo.refresh_latest_published(o, tenant, artifact)
            sides.append(
                Side(tenant, p_art, p_ver, s_ver, t_art, t_ver, model["id"], route["id"], rest, mcp)
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


def _agent_report(env: Env, spec: dict[str, Any]) -> Report:
    report = Report()
    validate_agent(spec, env, report, uuid7(), DEFAULT_MAXIMA)
    return report


def _tool_report(env: Env, spec: dict[str, Any]) -> Report:
    report = Report()
    validate_tool(deepcopy(spec), env, report)
    return report


# ------------------------------------------------------------------ the structural rules
def test_a_valid_agent_and_a_valid_tool_validate_cleanly(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        spec = _agent(
            a.prompt_version,
            {"model_id": str(a.model)},
            tool_version_ids=[str(a.tool_version)],
            skill_version_ids=[str(a.skill_version)],
        )
        report = Report()
        out, pins = validate_agent(spec, env, report, uuid7(), DEFAULT_MAXIMA)
        assert report.valid, report.errors
        assert pins.prompt is not None and pins.prompt.version_id == a.prompt_version
        assert [t.version_id for t in pins.tools] == [a.tool_version]
        assert out["tool_version_ids"] == [str(a.tool_version)]
        assert _tool_report(env, _tool(a.rest_connection)).valid
        s.rollback()


def test_agent_structure_errors_carry_paths_and_codes(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        good = _agent(a.prompt_version, {"model_id": str(a.model)})
        cases: list[tuple[dict[str, Any], str, str]] = [
            ({**good, "prompt_version_id": None}, "prompt_version_id", "required"),
            ({**good, "prompt_version_id": "nope"}, "prompt_version_id", "invalid_value"),
            ({**good, "model": {}}, "model", "invalid_value"),
            (
                {**good, "model": {"model_id": str(a.model), "route_id": str(a.route)}},
                "model",
                "invalid_value",
            ),
            (
                {**good, "limits": {**good["limits"], "max_iterations": 10**6}},
                "limits.max_iterations",
                "exceeds_maximum",
            ),
            ({**good, "memory": {"mode": "forever"}}, "memory.mode", "invalid_value"),
            ({**good, "tool_version_ids": "x"}, "tool_version_ids", "invalid_type"),
        ]
        for spec, path, code in cases:
            found = {(e.path, e.code) for e in _agent_report(env, spec).errors}
            assert (path, code) in found, (path, code, found)
        # a published agent pins ids: `latest:` is refused when not resolving
        report = Report()
        latest = {**good, "tool_version_ids": [f"latest:{a.tool_artifact}"]}
        validate_agent(latest, env, report, uuid7(), DEFAULT_MAXIMA, resolve=False)
        assert [(e.path, e.code) for e in report.errors] == [
            ("tool_version_ids[0]", "unpinned_reference")
        ]
        # the same tool twice, and a tool that limits its agents
        twice = _agent(
            a.prompt_version,
            {"model_id": str(a.model)},
            tool_version_ids=[str(a.tool_version), str(a.tool_version)],
        )
        assert ("tool_version_ids[1]", "duplicate") in {
            (e.path, e.code) for e in _agent_report(env, twice).errors
        }
        s.rollback()


def test_tool_structure_errors_carry_paths_and_codes(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        good = _tool(a.rest_connection)
        cases: list[tuple[dict[str, Any], str, str]] = [
            ({**good, "executor": "ftp"}, "executor", "invalid_value"),
            ({**good, "side_effect_class": "maybe"}, "side_effect_class", "invalid_value"),
            ({**good, "timeout_s": 10**6}, "timeout_s", "invalid_value"),
            ({**good, "audit_level": "none"}, "audit_level", "invalid_value"),
            (
                {**good, "side_effect_class": "destructive", "retry": {"max_attempts": 3}},
                "retry.max_attempts",
                "retry_forbidden",
            ),
            (
                {**good, "side_effect_class": "write_idempotent"},
                "idempotency",
                "idempotency_required",
            ),
            ({**good, "binding": None}, "binding", "required"),
        ]
        for spec, path, code in cases:
            found = {(e.path, e.code) for e in _tool_report(env, spec).errors}
            assert (path, code) in found, (path, code, found)
        s.rollback()


# ------------------------------------------------------------------ cross-client lookups
def _naming(a: Side, field: str, ref: UUID) -> dict[str, Any]:
    """A's valid agent with `field` naming the version `ref`."""
    base = _agent(a.prompt_version, {"model_id": str(a.model)})
    return {**base, field: str(ref) if field == "prompt_version_id" else [str(ref)]}


def test_an_agent_pinning_bs_versions_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        for field, theirs, own in (
            ("prompt_version_id", b.prompt_version, a.prompt_version),
            ("tool_version_ids", b.tool_version, a.tool_version),
            ("skill_version_ids", b.skill_version, a.skill_version),
        ):
            their_report = _agent_report(env, _naming(a, field, theirs))
            unknown_report = _agent_report(env, _naming(a, field, uuid7()))
            assert _signature(their_report.errors) == _signature(unknown_report.errors), field
            assert [e.code for e in their_report.errors] == ["unresolved_reference"], field
            assert _agent_report(env, _naming(a, field, own)).valid, field  # control
        s.rollback()


def test_a_latest_reference_to_bs_artifact_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        assert env.latest(b.prompt_artifact) is None
        assert env.latest(b.tool_artifact) is None
        assert env.latest(a.prompt_artifact) is not None  # control

        def spec(artifact: UUID) -> dict[str, Any]:
            return _agent(f"latest:{artifact}", {"model_id": str(a.model)})

        theirs = _agent_report(env, spec(b.prompt_artifact))
        unknown = _agent_report(env, spec(uuid7()))
        assert _signature(theirs.errors) == _signature(unknown.errors)
        assert [e.code for e in theirs.errors] == ["unresolved_reference"]
        assert _agent_report(env, spec(a.prompt_artifact)).valid
        s.rollback()


def test_an_agent_naming_bs_model_or_route_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        assert env.route_model_ids(b.route) is None and env.route_model_ids(uuid7()) is None
        assert env.route_model_ids(a.route) == [a.model]  # control
        assert env.model_problems([b.model]) == {b.model: "model_not_found"}
        assert env.model_problems([a.model]) == {}  # control
        for model, unknown in (
            ({"model_id": str(b.model)}, {"model_id": str(uuid7())}),
            ({"model_id": str(a.model), "fallback_model_ids": [str(b.model)]}, None),
            ({"route_id": str(b.route)}, {"route_id": str(uuid7())}),
        ):
            theirs = _agent_report(env, _agent(a.prompt_version, model))
            assert not theirs.valid, model
            if unknown is not None:
                same = _agent_report(env, _agent(a.prompt_version, unknown))
                assert [(e.path, e.code) for e in theirs.errors] == [
                    (e.path, e.code) for e in same.errors
                ], model
        assert _agent_report(env, _agent(a.prompt_version, {"route_id": str(a.route)})).valid
        s.rollback()


def test_a_tool_naming_bs_connection_gets_the_unknown_id_outcome(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        assert env.connection(b.rest_connection) is None
        assert env.connection(a.rest_connection) is not None  # control
        theirs = _tool_report(env, _tool(b.rest_connection))
        unknown = _tool_report(env, _tool(uuid7()))
        assert _signature(theirs.errors) == _signature(unknown.errors)
        assert [e.code for e in theirs.errors] == ["connection_not_found"]
        assert _tool_report(env, _tool(a.rest_connection)).valid
        s.rollback()


def test_an_mcp_tool_discovered_on_bs_connection_is_not_seen_from_a(
    seed: tuple[Side, Side],
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = seed
    with open_session(a.tenant) as s:
        env = make_env(s, a.tenant)
        # B's connection id is unknown to A: the validator stops at the connection
        theirs = _tool_report(env, _mcp_tool(b.mcp_connection, "lookup-b"))
        unknown = _tool_report(env, _mcp_tool(uuid7(), "lookup-b"))
        assert _signature(theirs.errors) == _signature(unknown.errors)
        assert [e.code for e in theirs.errors] == ["connection_not_found"]
        # the discovery lookup itself, asked for B's connection with A's client id or B's tool
        assert not env.mcp_tool(b.mcp_connection, "lookup-b").exists
        assert env.mcp_tool(a.mcp_connection, "lookup-b") == env.mcp_tool(a.mcp_connection, "x")
        # control: A's own discovered tool is pinned into a snapshot
        own = _tool_report(env, _mcp_tool(a.mcp_connection, "lookup-a"))
        assert own.valid, own.errors
        s.rollback()


def test_bs_context_does_not_resolve_as_ids(
    seed: tuple[Side, Side],
    app_db: Database,
    world: World,
    make_env: EnvFactory,
) -> None:
    """Neither client's id works under the other's context, with either client id."""
    a, b = seed
    with app_session(app_db, b.tenant) as s:
        assert make_env(s, a.tenant).connection(a.rest_connection) is None
        assert make_env(s, b.tenant).connection(a.rest_connection) is None
        assert make_env(s, b.tenant).connection(b.rest_connection) is not None
        assert make_env(s, b.tenant).route_model_ids(a.route) is None
        assert make_env(s, a.tenant).route_model_ids(a.route) is None
        assert make_env(s, b.tenant).latest(a.prompt_artifact) is None
        s.rollback()
