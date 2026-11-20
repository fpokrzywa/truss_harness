"""Evaluation runs (issue #80; sub-project 3 design section 6, settled calls 16 to 20, lead
decisions B-2706 to B-2708), driven end to end with the fake provider and an in-process worker.

A request starts one ordinary run per case as the requester, in the release's workspace and
project, calling the release's bound model within threshold / N of cost; reading the evaluation
scores it once its case runs have ended: `passed`, `failed` (accuracy, latency) or `error` (a
case run that did not complete, an unknown cost, an output that was not kept). Refusals write
nothing: a withdrawn release, an organization without full content capture. A model binding that
names no model of the organization ends the evaluation `error` before any run starts.
`require_passing_evaluation` finds only a passed run of the release's digests.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.evaluation.service import EvaluationRequired, require_passing_evaluation
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, A
from tests._support.p5util import thread_worker, user_session
from tests.evaluation.helpers import Release, capture, case, get_run, release, request, wait_scored
from tests.tenancy.conftest import app_session

REFUSED_DATASET = "evaluation_dataset_unavailable"


def _rows(owner: Session, sql: str, **params: Any) -> list[Any]:  # Any: row values
    owner.rollback()
    rows = list(owner.execute(text(sql), params).all())
    owner.rollback()
    return rows


def _case_runs(owner: Session, evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [UUID(c["run_id"]) for c in evaluation["case_runs"]]
    rows = owner.execute(
        text(
            "SELECT r.id, r.status, r.initiated_by_principal_id, r.workspace_id, r.project_id, "
            "r.started_at, r.finished_at, a.content AS input FROM shifde.runs r "
            "JOIN shifde.run_artifacts a ON a.id = r.input_ref WHERE r.id = ANY(:ids)"
        ),
        {"ids": ids},
    ).mappings()
    out = [dict(r) for r in rows]
    owner.rollback()
    return out


def _stored(owner: Session, run_id: str) -> dict[str, Any]:
    row = owner.execute(
        text("SELECT * FROM shifde.evaluation_runs WHERE id = :i"), {"i": run_id}
    ).mappings()
    out = dict(row.one())
    owner.rollback()
    return out


def _requested(rel: Release) -> dict[str, Any]:
    r = request(rel)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _scored(rel: Release, settings: Settings, run_id: str) -> dict[str, Any]:
    with thread_worker(settings, concurrency=3):
        return wait_scored(rel, run_id)


# ------------------------------------------------------------------ passed
def test_a_passing_evaluation_runs_each_case_as_the_requester(
    client: TestClient, world: World, owner_session: Session, settings: Settings, app_db: Database
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    cases = [
        case("hello", "hello there", ("contains", "hello there")),
        case("thanks", "many thanks", ("contains", "many thanks"), ("not_contains", "refused")),
        case("bye", "bye now", ("regex", r"(?s).*bye now.*")),
    ]
    rel = release(h, owner_session, cases, max_total_cost_usd="1")
    requested = _requested(rel)
    assert requested["status"] == "running" and requested["release_digest"] == rel.digest
    assert requested["dataset_id"] == rel.dataset["id"]
    assert requested["dataset_digest"] == rel.dataset["digest"]
    assert requested["thresholds"] == {
        "min_accuracy": "0.9",
        "max_p95_latency_ms": 600_000,
        "max_total_cost_usd": "1",
    }
    stored = _stored(owner_session, requested["id"])
    assert [c["case_id"] for c in stored["case_runs"]] == ["hello", "thanks", "bye"]

    # criterion 10: ordinary runs of the requester in the release's workspace and project
    admin_id, ws, proj = _rows(
        owner_session,
        "SELECT u.id, p.workspace_id, p.id FROM shifde.users u, shifde.projects p "
        "JOIN shifde.tenants t ON t.id = p.tenant_id "
        "WHERE u.primary_email = :e AND t.slug = 'demo-a' AND p.slug = 'sample'",
        e=ADMIN,
    )[0]
    runs = _case_runs(owner_session, stored)
    assert len(runs) == 3
    for run in runs:
        assert run["initiated_by_principal_id"] == admin_id
        assert (run["workspace_id"], run["project_id"]) == (ws, proj)
        # criterion 17 (B-2706): threshold / N, rounded down to 10 places
        overrides = run["input"]["overrides"]
        assert overrides["models"] == {rel.stack.model: rel.served}
        assert Decimal(overrides["max_cost"]) == Decimal("0.3333333333")
    assert sum(Decimal(r["input"]["overrides"]["max_cost"]) for r in runs) <= Decimal("1")

    scored = _scored(rel, settings, requested["id"])
    assert scored["status"] == "passed", scored
    assert scored["accuracy"] == "1" and scored["status_reason"] is None
    runs = {str(r["id"]): r for r in _case_runs(owner_session, stored)}
    durations = sorted(
        int((r["finished_at"] - r["started_at"]).total_seconds() * 1000) for r in runs.values()
    )
    assert scored["p95_latency_ms"] == durations[-1]  # nearest rank of 3: the 3rd
    ledger = _rows(
        owner_session,
        "SELECT coalesce(sum(cost_amount), 0) FROM shifde.usage_events "
        "WHERE root_run_id = ANY(:ids) AND currency = 'USD'",
        ids=list(runs),
    )[0][0]
    assert Decimal(scored["total_cost_usd"]) == ledger and ledger > 0
    for result in scored["results"]:
        assert result["passed"] and result["run_id"] in runs
        assert all(a["passed"] and a["reason"] is None for a in result["assertions"])
        assert set(result) == {
            "case_id",
            "run_id",
            "passed",
            "assertions",
            "latency_ms",
            "cost_usd",
        }
    # the bound model, never the pinned one, answered every call
    models = _rows(
        owner_session,
        "SELECT DISTINCT model_id FROM shifde.run_steps "
        "WHERE run_id = ANY(:ids) AND kind = 'model_call'",
        ids=list(runs),
    )
    assert [str(m[0]) for m in models] == [rel.served]
    listed = h.client.get(rel.evaluations(), headers=h)
    assert listed.status_code == 200 and listed.json()["items"] == [scored]
    actions = _rows(
        owner_session,
        "SELECT action, details->>'status' FROM shifde.audit_events "
        "WHERE target_id = :i ORDER BY occurred_at",
        i=requested["id"],
    )
    assert [tuple(a) for a in actions] == [
        ("evaluation.run.requested", None),
        ("evaluation.run.finished", "passed"),
    ]
    with app_session(app_db, _tenant(owner_session)) as s:
        found, passing = require_passing_evaluation(s, UUID(rel.id))
    assert str(found["id"]) == rel.id and str(passing["id"]) == scored["id"]


def _tenant(owner: Session, slug: str = "demo-a") -> UUID:
    tenant: UUID = owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": slug}
    ).scalar_one()
    owner.rollback()
    return tenant


# ------------------------------------------------------------------ failed
def test_a_case_below_min_accuracy_fails_the_evaluation(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    cases = [
        case("ok", "hello there", ("contains", "hello there")),
        case("wrong", "hello again", ("equals", "an answer it never gives")),
    ]
    rel = release(h, owner_session, cases, min_accuracy="0.9")
    scored = _scored(rel, settings, _requested(rel)["id"])
    assert scored["status"] == "failed" and scored["accuracy"] == "0.5", scored
    by_case = {r["case_id"]: r for r in scored["results"]}
    assert by_case["ok"]["passed"] and not by_case["wrong"]["passed"]
    assert by_case["wrong"]["assertions"] == [
        {"id": "a1", "passed": False, "reason": "the output does not equal the expected text"}
    ]


def test_latency_over_the_threshold_fails_the_evaluation(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    cases = [case("ok", "hello there", ("contains", "hello there"))]
    rel = release(h, owner_session, cases, max_p95_latency_ms=1)
    scored = _scored(rel, settings, _requested(rel)["id"])
    assert scored["status"] == "failed" and scored["accuracy"] == "1", scored
    assert scored["p95_latency_ms"] > 1


# ------------------------------------------------------------------ error
def test_a_case_run_that_did_not_complete_is_an_error(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    cases = [
        case("ok", "hello there", ("contains", "hello there")),
        case("stopped", "hello again", ("contains", "hello again")),
    ]
    rel = release(h, owner_session, cases)
    requested = _requested(rel)
    stopped = _stored(owner_session, requested["id"])["case_runs"][1]["run_id"]
    cancelled = h.client.post(f"{A}/runs/{stopped}/cancel", headers=h)
    assert cancelled.status_code == 202, cancelled.text
    scored = _scored(rel, settings, requested["id"])
    assert scored["status"] == "error", scored
    assert scored["status_reason"] == "case stopped: the case run ended cancelled"
    assert scored["accuracy"] is None and scored["results"] is None


def test_a_case_with_an_unknown_cost_is_an_error(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """The bound model has no rate: its calls run unmeasured (`allow_unmeasured`, the default)
    and the evaluation cannot know its cost (settled call 18)."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))], priced=False)
    scored = _scored(rel, settings, _requested(rel)["id"])
    assert scored["status"] == "error", scored
    assert scored["status_reason"] == "case ok: the case cost is unknown"


def test_the_cost_cap_refuses_a_call_over_the_case_share(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """A threshold of 3e-10 for 3 cases gives each case run a cap of 1e-10: its model call's
    worst case is above it, so the run ends `budget_denied` and the evaluation `error`."""
    h = user_session(client, world.passwords, ADMIN)
    cases = [case(f"c{i}", "hello", ("contains", "hello")) for i in range(3)]
    rel = release(h, owner_session, cases, max_total_cost_usd="0.0000000003")
    requested = _requested(rel)
    scored = _scored(rel, settings, requested["id"])
    assert scored["status"] == "error", scored
    assert scored["status_reason"] == "case c0: the case run ended failed"
    runs = _case_runs(owner_session, _stored(owner_session, requested["id"]))
    assert {r["input"]["overrides"]["max_cost"] for r in runs} == {"0.0000000001"}
    errors = _rows(
        owner_session,
        "SELECT DISTINCT error_class FROM shifde.runs WHERE id = ANY(:ids)",
        ids=[r["id"] for r in runs],
    )
    assert [e[0] for e in errors] == ["budget_denied"]
    calls = _rows(
        owner_session,
        "SELECT count(*) FROM shifde.run_steps WHERE run_id = ANY(:ids) AND kind = 'model_call'",
        ids=[r["id"] for r in runs],
    )
    assert calls[0][0] == 0


def test_capture_turned_off_before_scoring_is_an_error(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """Criterion 19: the case runs end under `metadata` capture, so their output is scrubbed."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))])
    requested = _requested(rel)
    capture(owner_session, "demo-a", "metadata")
    scored = _scored(rel, settings, requested["id"])
    assert scored["status"] == "error", scored
    assert scored["status_reason"] == "case ok: the case output was not kept"


def test_a_binding_naming_no_model_of_the_organization_ends_in_error(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Criterion 18 (B-2707): no case run starts and no other model is used."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))], served="nope")
    before = _rows(owner_session, "SELECT count(*) FROM shifde.runs")[0][0]
    requested = _requested(rel)
    assert requested["status"] == "error", requested
    assert requested["status_reason"] == (
        f"the model binding {rel.stack.model} is not a model of this organization"
    )
    assert _stored(owner_session, requested["id"])["case_runs"] == []
    assert _rows(owner_session, "SELECT count(*) FROM shifde.runs")[0][0] == before
    assert get_run(rel, requested["id"]) == requested


# ------------------------------------------------------------------ refusals
def test_an_organization_without_full_capture_is_refused(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Criterion 19 (B-2708), with capture set through the capture-content route."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))])
    version = _rows(owner_session, "SELECT row_version FROM shifde.tenants WHERE slug = 'demo-a'")
    put = h.client.put(
        f"{A}/capture-content",
        headers=h,
        json={"capture_content": "metadata", "row_version": version[0][0]},
    )
    assert put.status_code == 200, put.text
    runs = _rows(owner_session, "SELECT count(*) FROM shifde.runs")[0][0]
    r = request(rel)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "evaluation_requires_full_capture"
    assert r.json()["detail"] == "An evaluation needs full content capture in this organization."
    assert _rows(owner_session, "SELECT count(*) FROM shifde.evaluation_runs")[0][0] == 0
    assert _rows(owner_session, "SELECT count(*) FROM shifde.runs")[0][0] == runs


def test_a_withdrawn_release_and_a_strict_body(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))])
    bad = h.client.post(rel.evaluations(), headers=h, json={"dataset_id": rel.dataset["id"]})
    assert bad.status_code == 422, bad.text
    withdrawn = h.client.post(f"{A}/releases/{rel.id}/withdraw", headers=h, json={"note": "x"})
    assert withdrawn.status_code == 200, withdrawn.text
    r = request(rel)
    assert r.status_code == 409 and r.json()["code"] == "release_withdrawn", r.text
    assert _rows(owner_session, "SELECT count(*) FROM shifde.evaluation_runs")[0][0] == 0


# ------------------------------------------------------------------ the gate's check
def _insert_run(owner: Session, rel: Release, status: str, dataset_digest: str) -> UUID:
    tenant = _tenant(owner)
    set_tenant(owner, tenant)
    run_id = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.evaluation_runs (tenant_id, id, release_id, release_digest, "
            "dataset_id, dataset_digest, thresholds, status, requested_by) VALUES (:t, :i, :r, "
            ":rd, :d, :dd, CAST(:th AS jsonb), :s, :t)"
        ),
        {
            "t": tenant,
            "i": run_id,
            "r": rel.id,
            "rd": rel.digest,
            "d": rel.dataset["id"],
            "dd": dataset_digest,
            "th": json.dumps({"min_accuracy": "0.9"}),
            "s": status,
        },
    )
    owner.commit()
    return run_id


def test_require_passing_evaluation(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    """Criterion 12: none, only a failed one, a passed one of another dataset digest: 409
    `evaluation_required`; a passed one of the release's digests: the release and that run."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))])
    tenant = _tenant(owner_session)

    def check() -> Any:  # Any: (release, run) rows
        with app_session(app_db, tenant) as s:
            return require_passing_evaluation(s, UUID(rel.id))

    for status, digest in (
        (None, None),
        ("failed", rel.dataset["digest"]),
        ("passed", "0" * 64),
    ):
        if status is not None:
            _insert_run(owner_session, rel, status, str(digest))
        with pytest.raises(EvaluationRequired) as refused:
            check()
        assert (refused.value.status, refused.value.code) == (409, "evaluation_required")
        assert refused.value.detail == "This release has no passing evaluation."
    passing = _insert_run(owner_session, rel, "passed", rel.dataset["digest"])
    found, run = check()
    assert str(found["id"]) == rel.id and run["id"] == passing
    with app_session(app_db, tenant) as s, pytest.raises(NotFound) as unknown:
        require_passing_evaluation(s, uuid7())
    assert unknown.value.detail == "release not found"
    with app_session(app_db, _tenant(owner_session, "demo-b")) as s, pytest.raises(NotFound):
        require_passing_evaluation(s, UUID(rel.id))  # another client's release


# ------------------------------------------------------------------ the database
def test_the_guards_refuse_the_owner_too(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Criterion 13: what an evaluation run evaluates never changes, an ended one never
    changes, and neither table deletes."""
    h = user_session(client, world.passwords, ADMIN)
    rel = release(h, owner_session, [case("ok", "hello", ("contains", "hello"))])
    queued = _insert_run(owner_session, rel, "queued", rel.dataset["digest"])
    passed = _insert_run(owner_session, rel, "passed", rel.dataset["digest"])
    tenant = _tenant(owner_session)
    for sql, params in (
        ("UPDATE shifde.evaluation_runs SET thresholds = '{}' WHERE id = :i", {"i": queued}),
        ("UPDATE shifde.evaluation_runs SET status = 'failed' WHERE id = :i", {"i": passed}),
        ("UPDATE shifde.evaluation_runs SET case_runs = '[{}]' WHERE id = :i", {"i": queued}),
        ("DELETE FROM shifde.evaluation_runs WHERE id = :i", {"i": queued}),
        ("DELETE FROM shifde.evaluation_datasets WHERE id = :i", {"i": rel.dataset["id"]}),
        (
            "UPDATE shifde.evaluation_datasets SET name = 'x' WHERE id = :i",
            {"i": rel.dataset["id"]},
        ),
    ):
        set_tenant(owner_session, tenant)
        with pytest.raises(DBAPIError) as err:
            owner_session.execute(text(sql), params)
        owner_session.rollback()
        assert getattr(err.value.orig, "sqlstate", None) == "23514", (sql, err.value)
    # queued -> running with its case runs is the one update of `case_runs`
    set_tenant(owner_session, tenant)
    owner_session.execute(
        text(
            "UPDATE shifde.evaluation_runs SET status = 'running', case_runs = '[]' WHERE id = :i"
        ),
        {"i": queued},
    )
    owner_session.rollback()


def test_both_tables_force_row_level_security(owner_session: Session) -> None:
    rows = owner_session.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid "
            "AND p.polname = 'tenant_isolation') FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'shifde' AND c.relname IN ('evaluation_datasets', 'evaluation_runs') "
            "ORDER BY c.relname"
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("evaluation_datasets", True, True, True),
        ("evaluation_runs", True, True, True),
    ]
