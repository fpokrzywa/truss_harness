"""ARCH M11 (issue #15, B-2645..B-2652): a run's model call is refused when the run tree's data
classification ranks above the model provider's trust level.

A run's ontology reads raise its classification (the highest classification of the properties a
read touched); a raise is a `data_classification_raised` run event naming only the level, so it
survives a new `RunContext` and is seen by every run of the same root. A provider's trust level is
`settings.trust_level` (missing or invalid = `public`). A refused call reaches no adapter, writes
a denied `model.invoked` audit event with reason `classification_above_provider_trust`, and the
run ends `failed` with `error_class = authz_denied`; nothing on that path names the property or
the object type that raised the level.

The ontology is the cross-client fixture's (`tests/ontology/test_routes_cross_client.py`) with its
`name` property read as `confidential` (`id` stays `internal`); the source fetch is a stand-in
(`engine.Executor`), the model a `fake`-adapter model. Runs are advanced the way the worker does
(`lifecycle.advance` on the app database) with the agent executor replaced by a script that reads
and then calls the model through the real `steps.call_model` and `ModelService.invoke_for_run`.

Isolation: client A's confidential read raises nothing of client B: B's run that read nothing
calls B's provider without `trust_level`; A's events are invisible to B's `shifde_app` session,
and the run-tree read keyed by A's root under B's client id is empty even without row-level
security.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import repository as build_repo
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import ModelNotAllowed
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import schemas as ms
from shifde.models_admin.service import ModelService
from shifde.ontology import engine
from shifde.ontology.service import OntologyService
from shifde.runtime import lifecycle, ontology_exec, state, steps
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import PolicyBudgetHooks
from tests._support.runs import add_run
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY
from tests.ledger.util import add_model
from tests.ontology.conftest import owner_session, people, sides, world
from tests.ontology.test_routes_cross_client import TYPE, Side, Two, two
from tests.runtime.conftest import app_session
from tests.runtime.execution import run_context, run_row
from tests.runtime.test_lifecycle import WORKER, add_job
from tests.runtime.test_ontology_exec import TOOL

__all__ = ["owner_session", "people", "sides", "two", "world"]

RAISED = "data_classification_raised"
REASON = "classification_above_provider_trust"
REFUSAL = "the run's data classification is above the model provider's trust level"
NO_TRUST = object()  # a provider whose settings have no `trust_level`
ROW = {"id": 7, "name": "row-value-3f9c"}


# ------------------------------------------------------------------ seed
def _model(o: Session, tenant: UUID, trust: object) -> UUID:
    """A `fake`-adapter model of `tenant` whose provider's settings hold `trust` as
    `trust_level` (none for `NO_TRUST`)."""
    model = add_model(o, tenant, adapter_key="fake", model_key=f"fake-{uuid7().hex[:8]}")
    settings = {} if trust is NO_TRUST else {"trust_level": trust}
    o.execute(
        text(
            "UPDATE shifde.model_providers SET settings = CAST(:s AS jsonb) "
            "WHERE id = (SELECT provider_id FROM shifde.models WHERE id = :m)"
        ),
        {"s": json.dumps(settings), "m": model},
    )
    o.execute(
        text(
            "UPDATE shifde.connections SET status = 'active' WHERE id = "
            "(SELECT p.connection_id FROM shifde.model_providers p JOIN shifde.models m "
            "ON m.provider_id = p.id WHERE m.id = :m)"
        ),
        {"m": model},
    )
    return model


@pytest.fixture(autouse=True)
def source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The source fetch answers one row; planning, policies and lineage are the real ones."""

    def run(self: engine.Executor, root: engine.NodePlan, limit: int, offset: int) -> Any:
        return engine.Result(
            [{k: ROW[k] for k in root.select}], False, {root.object_type: 1}, False
        )

    def first(self: engine.Executor, node: engine.NodePlan) -> list[dict[str, Any]]:
        return [{k: ROW[k] for k in node.columns}]

    original = OntologyService._frozen

    def frozen(self: OntologyService, version: Any) -> Any:
        """The published spec with `name` raised to `confidential` (`id` stays `internal`): a
        published version is immutable in the database, so the raise is applied as it is read."""
        spec, mappings = original(self, version)
        spec = copy.deepcopy(spec)
        for prop in spec["object_types"][0]["properties"]:
            if prop["key"] == "name":
                prop["classification"] = "confidential"
        return spec, mappings

    monkeypatch.setattr(OntologyService, "_frozen", frozen)
    monkeypatch.setattr(engine.Executor, "run", run)
    monkeypatch.setattr(engine.Executor, "first", first)


@pytest.fixture
def adapter_calls(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """The provider ids every model call that reached the adapter was made to."""
    calls: list[UUID] = []
    original = ModelService._provider_call

    def spy(self: ModelService, provider: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(provider["id"])
        return original(self, provider, *args, **kwargs)

    monkeypatch.setattr(ModelService, "_provider_call", spy)
    return calls


def _agent_run(o: Session, side: Side, **cols: Any) -> UUID:  # Any: column values
    """A run of the side's client whose agent version pins the `ontology.query` tool (the
    unchecked query path reads only for such a run, B-2143)."""
    agent, version = add_published(o, side.tenant, "agents", f"agent-{uuid7().hex}", {})
    _, tool = add_published(o, side.tenant, "tools", f"tool-{uuid7().hex}", TOOL)
    build_repo.insert_pins(o, side.tenant, version, [(tool, "tool")])
    return add_run(o, side.tenant, agent_id=agent, agent_version_id=version, **cols)


# ------------------------------------------------------------------ driving a run
def _read(ctx: RunContext, side: Side, select: list[str]) -> None:
    ontology_exec.run_query(
        ctx, UUID(side.ontology), None, {"object_type": TYPE, "select": select},
        channel="agent", purpose="test",
    )  # fmt: skip


def _call(ctx: RunContext, model: UUID) -> str:
    return steps.call_model(
        ctx, [{"role": "user", "content": "hi"}], [model], "answer", [], lambda _text: None
    ).text


class Script:
    """Stands in for the agent executor: reads `select` (when given), then calls `model`."""

    side: Side
    select: list[str] | None
    model: UUID

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def run(self) -> Any:
        if Script.select:
            _read(self.ctx, Script.side, Script.select)
        _call(self.ctx, Script.model)
        return {"ok": 1}


@pytest.fixture
def advance(
    monkeypatch: pytest.MonkeyPatch, owner_db: Database, app_db: Database, settings: Settings
) -> Callable[[Side, list[str] | None, UUID], UUID]:
    """`advance(side, select, model)`: a new root run of the side's owner, advanced once by the
    worker's `lifecycle.advance` on the app database; returns the run id."""
    monkeypatch.setattr(lifecycle, "AgentRunner", Script)
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)

    def go(side: Side, select: list[str] | None, model: UUID) -> UUID:
        Script.side, Script.select, Script.model = side, select, model
        with owner_db.session() as o:
            run = _agent_run(o, side, initiated_by_principal_id=side.user)
            job = add_job(o, side.tenant, run)
            o.commit()
        outcome = lifecycle.advance(
            app_db, settings, runtime, PolicyBudgetHooks(), side.tenant, run, WORKER,
            threading.Event(), threading.Event(), job,
        )  # fmt: skip
        assert outcome == "done", outcome
        return run

    return go


def _run(o: Session, run: UUID) -> dict[str, Any]:  # Any: run row
    o.rollback()
    return dict(
        o.execute(text("SELECT * FROM shifde.runs WHERE id = :r"), {"r": run}).mappings().one()
    )


def _raised(o: Session, run: UUID) -> list[dict[str, Any]]:  # Any: event details
    o.rollback()
    return [
        dict(r[0])
        for r in o.execute(
            text("SELECT details FROM shifde.run_events WHERE run_id = :r AND type = :t"),
            {"r": run, "t": RAISED},
        )
    ]


def _invoked(o: Session, run: UUID) -> list[dict[str, Any]]:  # Any: audit rows
    o.rollback()
    return [
        dict(r)
        for r in o.execute(
            text(
                "SELECT outcome, details FROM shifde.audit_events WHERE action = 'model.invoked' "
                "AND details->>'run_id' = :r ORDER BY occurred_at"
            ),
            {"r": str(run)},
        ).mappings()
    ]


def _steps(o: Session, run: UUID) -> list[dict[str, Any]]:  # Any: step rows
    o.rollback()
    return [
        dict(r)
        for r in o.execute(
            text(
                "SELECT kind, status, error_class, error_message FROM shifde.run_steps "
                "WHERE run_id = :r ORDER BY seq, attempt"
            ),
            {"r": run},
        ).mappings()
    ]


def _assert_refused(o: Session, run: UUID) -> dict[str, Any]:  # Any: the denied audit details
    row = _run(o, run)
    assert (row["status"], row["error_class"]) == ("failed", "authz_denied"), row
    assert row["error_message"] == REFUSAL
    [step] = [s for s in _steps(o, run) if s["kind"] == "model_call"]
    assert (step["status"], step["error_class"]) == ("failed", "authz_denied")
    [event] = _invoked(o, run)
    assert event["outcome"] == "denied"
    assert event["details"]["reason"] == REASON
    details: dict[str, Any] = event["details"]
    return details


def _assert_completed(o: Session, run: UUID) -> None:
    row = _run(o, run)
    assert (row["status"], row["error_class"]) == ("completed", None), row
    assert [e["outcome"] for e in _invoked(o, run)] == ["success"]


# ------------------------------------------------------------------ (a) refused, audited
def test_a_confidential_read_refuses_an_internal_provider_before_the_call(
    two: Two, advance: Any, owner_db: Database, adapter_calls: list[UUID]
) -> None:
    a = two.a
    with owner_db.session() as o:
        model = _model(o, a.tenant, "internal")
        o.commit()
    run = advance(a, ["id", "name"], model)
    assert adapter_calls == []  # no adapter call was made
    with owner_db.session() as o:
        details = _assert_refused(o, run)
        assert (details["classification"], details["provider_trust_level"]) == (
            "confidential",
            "internal",
        )
        assert _raised(o, run) == [{"classification": "confidential"}]


# ------------------------------------------------------------------ (b) allowed
@pytest.mark.parametrize("trust", ["confidential", "restricted"])
def test_a_confidential_read_may_call_a_provider_trusted_at_or_above_it(
    trust: str, two: Two, advance: Any, owner_db: Database, adapter_calls: list[UUID]
) -> None:
    a = two.a
    with owner_db.session() as o:
        model = _model(o, a.tenant, trust)
        o.commit()
    run = advance(a, ["id", "name"], model)
    assert len(adapter_calls) == 1
    with owner_db.session() as o:
        _assert_completed(o, run)
        assert _raised(o, run) == [{"classification": "confidential"}]


# ------------------------------------------------------------------ (c) missing / invalid trust
@pytest.mark.parametrize("trust", [NO_TRUST, "high", 3, None, ["restricted"]])
@pytest.mark.parametrize("select", [["id"], ["id", "name"]])
def test_a_provider_without_a_valid_trust_level_is_public_and_refused_after_a_read(
    trust: object,
    select: list[str],
    two: Two,
    advance: Any,
    owner_db: Database,
    adapter_calls: list[UUID],
) -> None:
    a = two.a
    with owner_db.session() as o:
        model = _model(o, a.tenant, trust)
        o.commit()
    run = advance(a, select, model)  # `id` is internal, `name` confidential
    assert adapter_calls == []
    with owner_db.session() as o:
        details = _assert_refused(o, run)
        assert details["provider_trust_level"] == "public"
        assert details["classification"] == ("internal" if select == ["id"] else "confidential")


# ------------------------------------------------------------------ (d) nothing read
def test_a_run_that_read_nothing_calls_a_provider_without_a_trust_level(
    two: Two, advance: Any, owner_db: Database, adapter_calls: list[UUID]
) -> None:
    a = two.a
    with owner_db.session() as o:
        model = _model(o, a.tenant, NO_TRUST)
        o.commit()
    run = advance(a, None, model)
    assert len(adapter_calls) == 1
    with owner_db.session() as o:
        _assert_completed(o, run)
        assert _raised(o, run) == []


# ------------------------------------------------------------------ (e) no leak
def test_the_refusal_names_no_property_object_type_or_row_value(
    two: Two, advance: Any, owner_db: Database, app_db: Database, settings: Settings
) -> None:
    a = two.a
    with owner_db.session() as o:
        model = _model(o, a.tenant, "internal")
        o.commit()
    secrets = ["name", TYPE, TYPE.split(".")[-1], str(ROW["name"]), a.ontology, "onto-a"]
    # the exception itself, raised by the service
    with owner_db.session() as o:
        running = _agent_run(o, a, status="running", lease_token=uuid7(),
                          started_at=utcnow(), initiated_by_principal_id=a.user)  # fmt: skip
        o.commit()
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, running))
        _read(ctx, a, ["id", "name"])
        body = ms.InvokeIn(messages=[ms.ChatMessageIn(role="user", content="hi")], max_tokens=16)
        with pytest.raises(ModelNotAllowed) as refused:
            ctx.artifacts.models.invoke_for_run(
                ctx.run_ref(), model, body, run_id=ctx.run_id, max_deadline=5,
                classification=ctx.effective_classification(),
            )  # fmt: skip
    assert refused.value.detail == REFUSAL
    # the whole run: its error, the failed step, the audit event, the raised event
    run = advance(a, ["id", "name"], model)
    with owner_db.session() as o:
        details = _assert_refused(o, run)
        texts = {
            "exception": str(refused.value),
            "error_message": str(_run(o, run)["error_message"]),
            "steps": json.dumps(_steps(o, run), default=str),
            "audit": json.dumps(details),
            "raised": json.dumps(_raised(o, run)),
        }
    # the refusal's own keys, the run context (Item 15) and the audit service's actor / target
    assert set(details) == {"reason", "classification", "provider_trust_level", "run_id",
                            "workspace_id", "project_id", "actor", "target"}, details  # fmt: skip
    for where, value in texts.items():
        for secret in secrets:
            assert secret not in value, (where, secret, value)


# ------------------------------------------------------------------ (f) durable, run tree
def test_the_level_survives_a_new_context_and_a_childs_level_reaches_its_parent(
    two: Two, owner_db: Database, app_db: Database, settings: Settings
) -> None:
    a = two.a
    token = uuid7()
    with owner_db.session() as o:
        running = {"status": "running", "lease_token": token, "started_at": utcnow(),
                   "initiated_by_principal_id": a.user}  # fmt: skip
        parent = _agent_run(o, a, **running)
        child = _agent_run(o, a, parent_run_id=parent, root_run_id=parent, depth=1, **running)
        unrelated = _agent_run(o, a, **running)
        o.commit()
    with app_session(app_db, a.tenant) as s:
        first = run_context(s, settings, run_row(s, a.tenant, parent))
        assert first.effective_classification() == "public"
        _read(first, a, ["id"])
        assert first.effective_classification() == "internal"
        # a new context of the same run (a resumed advance) knows nothing in memory, yet the
        # run's level is what the event recorded
        again = run_context(s, settings, run_row(s, a.tenant, parent))
        assert again.classification == "public"
        assert again.effective_classification() == "internal"
        # the child reads confidential data: its parent sees it, the child's next context too
        kid = run_context(s, settings, run_row(s, a.tenant, child))
        assert kid.effective_classification() == "internal"  # the parent's level reaches it
        _read(kid, a, ["id", "name"])
        assert again.effective_classification() == "confidential"
        assert run_context(s, settings, run_row(s, a.tenant, child)).effective_classification() == (
            "confidential"
        )
        # the level never goes down: a lower read records nothing
        _read(again, a, ["id"])
        assert again.effective_classification() == "confidential"
        # another run tree of the same client is not raised
        assert run_context(
            s, settings, run_row(s, a.tenant, unrelated)
        ).effective_classification() == ("public")
    with owner_db.session() as o:
        assert _raised(o, parent) == [{"classification": "internal"}]
        assert _raised(o, child) == [{"classification": "confidential"}]
        assert _raised(o, unrelated) == []


def test_every_attempt_and_every_model_of_the_chain_is_checked(
    two: Two, owner_db: Database, app_db: Database, settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """`call_model` passes the run tree's level on each attempt; a refusal is not a fallback."""
    a = two.a
    with owner_db.session() as o:
        low, high = _model(o, a.tenant, "public"), _model(o, a.tenant, "restricted")
        run = _agent_run(o, a, status="running", lease_token=uuid7(), started_at=utcnow(),
                      initiated_by_principal_id=a.user)  # fmt: skip
        o.commit()
    seen: list[str | None] = []
    original = ModelService.invoke_for_run

    def spy(self: ModelService, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("classification"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModelService, "invoke_for_run", spy)
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, run))
        assert steps.call_model(ctx, [{"role": "user", "content": "hi"}], [low, high], "m", [],
                                lambda _t: None).text  # fmt: skip
        _read(ctx, a, ["id"])
        with pytest.raises(state.RunFailed) as failed:
            steps.call_model(ctx, [{"role": "user", "content": "hi"}], [low, high], "m", [],
                             lambda _t: None)  # fmt: skip
    assert failed.value.error_class == state.AUTHZ_DENIED
    assert seen == ["public", "internal"]  # refused on `low`, no fallback to `high`


# ------------------------------------------------------------------ the object read path
def test_an_object_read_reports_the_classification_of_what_it_touched(
    two: Two, owner_db: Database, app_db: Database, settings: Settings
) -> None:
    a = two.a
    with owner_db.session() as o:
        version = o.execute(
            text("SELECT latest_published_version_id FROM shifde.artifacts WHERE id = :a"),
            {"a": UUID(a.ontology)},
        ).scalar_one()
        # an ontology action run of the ontology's version: `get_object` reads only for that
        run = add_run(o, a.tenant, kind="ontology_action", ontology_id=UUID(a.ontology),
                      ontology_version_id=version, action_key="act", status="running",
                      lease_token=uuid7(), started_at=utcnow(),
                      initiated_by_principal_id=a.user)  # fmt: skip
        o.commit()
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, run))
        service = OntologyService(ctx.artifacts)
        assert service.read_classification is None
        service.get_object(UUID(a.ontology), TYPE, "7", run_id=run, api=False, properties=["id"])
        assert service.read_classification == "internal"
        service.get_object(UUID(a.ontology), TYPE, "7", run_id=run, api=False)
        assert service.read_classification == "confidential"


# ------------------------------------------------------------------ (g) cross-client
def test_one_clients_classification_never_raises_the_others(
    two: Two,
    advance: Any,
    owner_db: Database,
    app_db: Database,
    adapter_calls: list[UUID],
) -> None:
    a, b = two.a, two.b
    with owner_db.session() as o:
        model_a, model_b = _model(o, a.tenant, "internal"), _model(o, b.tenant, NO_TRUST)
        o.commit()
    run_a = advance(a, ["id", "name"], model_a)
    run_b = advance(b, None, model_b)
    assert len(adapter_calls) == 1  # B's call only
    with owner_db.session() as o:
        _assert_refused(o, run_a)
        _assert_completed(o, run_b)
        assert _raised(o, run_a) == [{"classification": "confidential"}]  # positive control
        assert _raised(o, run_b) == []
        # without row-level security the run-tree read is still keyed by the client id
        assert repo.tree_classifications(o, b.tenant, run_a) == []
        assert repo.tree_classifications(o, a.tenant, run_a) == ["confidential"]
        o.rollback()
    with app_session(app_db, b.tenant) as s:
        visible = s.execute(
            text("SELECT count(*) FROM shifde.run_events WHERE type = :t"), {"t": RAISED}
        ).scalar_one()
        assert visible == 0
        assert repo.tree_classifications(s, a.tenant, run_a) == []
        assert repo.tree_classifications(s, b.tenant, run_a) == []
    with app_session(app_db, a.tenant) as s:
        assert repo.tree_classifications(s, a.tenant, run_a) == ["confidential"]
