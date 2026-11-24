"""SP5-3 (design 5.5, 9.4): accepting a drafted revision.

The happy path turns a revised `SAMPLE_DRAFT` into one published prompt, two published
`connection.sql` tools keyed by the draft tool names (their SQL in `%(name)s` form), one published
agent pinning both, one dataset of the 3 cases, one solution and one solution version carrying
the draft's screens, every id in `accept_progress`. The pre-check (9.4): a caller without
`agent:publish` gets 403 and nothing is written. A failure injected in the agent step leaves the
request `accepting` with the error code as its reason; a repeated accept resumes, reusing every
recorded id, and creates nothing twice. The refusals answer 409 `already_accepted`,
`request_not_acceptable` and `stale_revision`, and 422 `draft_invalid` (an error diagnostic, a
tool key already used in the project) with nothing written. An accepted request's version seals
under `/2` and its evaluation, the agent scripted by `AGENT_SCRIPT` over the sample-data fake
workspace, passes. Cross-client and cross-project 404s are in `test_routes_cross_client.py`.

Issue #316: two resumes at once, or one while another runs a step, run the steps once (the loser
gets 409 `accept_in_progress`, writing nothing); an expired claim is taken over; the API never
shows the claim. A resume after a crash between a create and its next record adopts the artifact
of the id the request recorded, but a key held by another artifact is still `key_taken`. A failed
step writes `appgen.request.accept_failed` naming the step. Key and runtime-model refusals say
what they refuse.
"""

from __future__ import annotations

import contextlib
import copy
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any  # Any: JSON bodies and row values

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.appgen import repository
from shifde.appgen import schemas as appgen_schemas
from shifde.appgen.service import AppGenService, DraftInvalid, sql_template
from shifde.build.service import ArtifactService
from shifde.build.tools import named_sql
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.packaging.canonical import digest
from shifde.runtime.worker import Worker
from tests._support.appgen_fixtures import AGENT_SCRIPT, SAMPLE_DRAFT
from tests._support.fullapp import World
from tests._support.p2util import new_user, post, tenant_id
from tests._support.p3util import ADMIN, A, fake_model_stack
from tests._support.p4util import art, builtin_tool_spec, prompt_spec
from tests._support.p5util import Hdr, fast, user_session, wait_for
from tests._support.p8util import rate_card, rate_version
from tests.appgen.helpers import (
    INVALID,
    PROJECT,
    body,
    create,
    databricks,
    drafted,
    drafting_model,
    get,
    request_url,
    requests_url,
    run_job,
    wire,
)
from tests.evaluation.helpers import capture

THRESHOLDS = {"min_accuracy": "0.9", "max_p95_latency_ms": 600_000, "max_total_cost_usd": "1.00"}
SERVED = "served-chat"
CUSTOM = "accepter@demo-a.local"
PROGRESS_KEYS = {
    "prompt_artifact_id",
    "prompt_version_id",
    "tool_artifact_ids",
    "tool_version_ids",
    "agent_artifact_id",
    "agent_version_id",
    "dataset_id",
    "solution_id",
    "solution_version_id",
}
EXPECTED = {
    "prompts": 1,
    "tools": 2,
    "agents": 1,
    "versions": 4,
    "published": 4,
    "datasets": 1,
    "solutions": 1,
    "solution_versions": 1,
}


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    wire(client)
    return user_session(client, world.passwords, ADMIN)


def _code(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    out: dict[str, Any] = r.json()
    assert out["code"] == code, out
    return out


def _agent_model(h: Hdr) -> dict[str, str]:
    """The runtime model: an active fake model whose provider answers with `AGENT_SCRIPT`."""
    return fake_model_stack(h.client, h, config={"script": AGENT_SCRIPT}, conn_name="agent-model")


def _accept_body(revision_no: int, model: str, **over: Any) -> dict[str, Any]:
    return {
        "revision_no": revision_no,
        "solution_name": "Sales app",
        "model_id": model,
        "thresholds": THRESHOLDS,
        **over,
    }


def _accept(h: Hdr, request_id: str, payload: dict[str, Any]) -> httpx.Response:
    return h.client.post(request_url(request_id, tail="/accept"), headers=h, json=payload)


def _rows(owner: Session, sql: str, **params: Any) -> list[Any]:
    owner.rollback()
    rows = list(owner.execute(text(sql), params).mappings().all())
    owner.rollback()
    return rows


def _counts(owner: Session, project_id: str) -> dict[str, int]:
    """The project's build, evaluation and packaging objects."""
    p = {"p": project_id}
    kinds = {
        r["kind"]: r["n"]
        for r in _rows(
            owner,
            "SELECT kind, count(*) AS n FROM shifde.artifacts WHERE project_id = :p GROUP BY kind",
            **p,
        )
    }
    versions = _rows(
        owner,
        "SELECT count(*) AS n, count(*) FILTER (WHERE v.state = 'published') AS published "
        "FROM shifde.artifact_versions v JOIN shifde.artifacts a ON a.id = v.artifact_id "
        "WHERE a.project_id = :p",
        **p,
    )[0]

    def one(sql: str) -> int:
        return int(_rows(owner, sql, **p)[0]["n"])

    return {
        "prompts": kinds.get("prompts", 0),
        "tools": kinds.get("tools", 0),
        "agents": kinds.get("agents", 0),
        "versions": versions["n"],
        "published": versions["published"],
        "datasets": one(
            "SELECT count(*) AS n FROM shifde.evaluation_datasets WHERE project_id = :p"
        ),
        "solutions": one("SELECT count(*) AS n FROM shifde.solutions WHERE project_id = :p"),
        "solution_versions": one(
            "SELECT count(*) AS n FROM shifde.solution_versions v JOIN shifde.solutions s "
            "ON s.id = v.solution_id WHERE s.project_id = :p"
        ),
    }


def _audits(owner: Session, request_id: str) -> list[str]:
    return [
        r["action"]
        for r in _rows(
            owner,
            "SELECT action FROM shifde.audit_events WHERE target_id = :r ORDER BY chain_seq",
            r=request_id,
        )
    ]


def _state(owner: Session, request_id: str) -> dict[str, Any]:
    return dict(
        _rows(
            owner,
            "SELECT status, status_reason, accept_progress, accepted_revision_no, accepted_by, "
            "solution_version_id FROM shifde.appgen_requests WHERE id = :r",
            r=request_id,
        )[0]
    )


def _drafted(admin: Hdr, owner: Session) -> dict[str, Any]:
    c = admin.client
    return drafted(c, admin, owner, body(databricks(c, admin), drafting_model(c, admin)))


# ------------------------------------------------------------------ the SQL template
def test_only_parameter_tokens_become_named_placeholders() -> None:
    sql = (
        "select ':not_a_param', x::int, `:col` from t "
        "where a = :a and b like '50%' and c = :c_2 -- :comment"
    )
    template = sql_template(sql)
    assert template == (
        "select ':not_a_param', x::int, `:col` from t "
        "where a = %(a)s and b like '50%%' and c = %(c_2)s -- :comment"
    )
    assert named_sql(template) == sql


# ------------------------------------------------------------------ happy path
def test_an_accepted_revision_becomes_published_objects_of_the_project(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    request = _drafted(admin, owner_session)
    edit = copy.deepcopy(SAMPLE_DRAFT)
    edit["tools"][1]["max_rows"] = 50
    revised = c.post(
        request_url(request["id"], tail="/drafts"),
        headers=admin,
        json={"base_revision_no": 1, "draft": edit},
    )
    assert revised.status_code == 201, revised.text
    model = _agent_model(admin)["model"]

    r = _accept(admin, request["id"], _accept_body(2, model))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "accepted" and out["status_reason"] is None
    assert out["accepted_revision_no"] == 2 and out["solution_version_id"]
    progress = out["accept_progress"]
    assert set(progress) == PROGRESS_KEYS
    assert set(progress["tool_version_ids"]) == {"orders_by_customer", "invoiced_by_region"}
    assert progress["solution_version_id"] == out["solution_version_id"]
    assert _counts(owner_session, request["project_id"]) == EXPECTED

    tools = {
        r["key"]: r
        for r in _rows(
            owner_session,
            "SELECT a.id AS artifact_id, a.key, v.id, v.spec FROM shifde.artifacts a "
            "JOIN shifde.artifact_versions v ON v.artifact_id = a.id "
            "WHERE a.kind = 'tools' AND a.project_id = :p",
            p=request["project_id"],
        )
    }
    assert set(tools) == {"orders_by_customer", "invoiced_by_region"}
    for name, tool in tools.items():
        assert str(tool["id"]) == progress["tool_version_ids"][name]
        assert str(tool["artifact_id"]) == progress["tool_artifact_ids"][name]
        assert tool["spec"]["executor"] == "connection.sql"
        assert tool["spec"]["side_effect_class"] == "read"
        assert tool["spec"]["binding"]["connection_id"] == request["connection_id"]
    invoiced = tools["invoiced_by_region"]["spec"]
    assert invoiced["binding"]["max_rows"] == 50
    assert "c.region = %(region)s" in invoiced["binding"]["sql_template"]
    assert ":region" not in invoiced["binding"]["sql_template"]
    assert invoiced["input_schema"] == {
        "type": "object",
        "properties": {"region": {"type": "string", "maxLength": 1000}},
        "required": ["region"],
        "additionalProperties": False,
    }
    orders = tools["orders_by_customer"]["spec"]
    assert orders["binding"]["max_rows"] == 200
    assert (
        "o.customer_id = %(customer_id)s and o.ordered_on >= %(since)s"
        in (orders["binding"]["sql_template"])
    )
    assert orders["input_schema"]["properties"] == {
        "customer_id": {"type": "integer"},
        "since": {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    }

    [agent] = _rows(
        owner_session,
        "SELECT a.key, v.id, v.spec FROM shifde.artifacts a JOIN shifde.artifact_versions v "
        "ON v.artifact_id = a.id WHERE a.kind = 'agents' AND a.project_id = :p",
        p=request["project_id"],
    )
    assert agent["key"] == "sample-sales-agent"
    assert str(agent["id"]) == progress["agent_version_id"]
    assert agent["spec"]["prompt_version_id"] == progress["prompt_version_id"]
    assert agent["spec"]["tool_version_ids"] == [
        progress["tool_version_ids"]["orders_by_customer"],
        progress["tool_version_ids"]["invoiced_by_region"],
    ]
    assert agent["spec"]["model"]["model_id"] == model
    assert agent["spec"]["limits"] == {
        "max_iterations": 8,
        "max_tool_calls": 8,
        "max_tokens": 200_000,
        "deadline_s": 60,
        "max_cost": 1,
    }
    [prompt] = _rows(
        owner_session,
        "SELECT v.spec FROM shifde.artifact_versions v JOIN shifde.artifacts a "
        "ON a.id = v.artifact_id WHERE a.kind = 'prompts' AND a.project_id = :p",
        p=request["project_id"],
    )
    assert prompt["spec"]["sections"]["system"] == SAMPLE_DRAFT["agent"]["instructions"]

    [dataset] = _rows(
        owner_session,
        "SELECT id, name, cases FROM shifde.evaluation_datasets WHERE project_id = :p",
        p=request["project_id"],
    )
    assert str(dataset["id"]) == progress["dataset_id"] and dataset["name"] == "Sales app"
    assert [x["case_id"] for x in dataset["cases"]] == [
        x["case_id"] for x in SAMPLE_DRAFT["evaluation_cases"]
    ]
    [version] = _rows(
        owner_session,
        "SELECT v.id, v.solution_id, v.entrypoint_kind, v.entrypoint_version_id, v.spec "
        "FROM shifde.solution_versions v WHERE v.id = :v",
        v=out["solution_version_id"],
    )
    assert str(version["solution_id"]) == progress["solution_id"]
    assert version["entrypoint_kind"] == "agent"
    assert str(version["entrypoint_version_id"]) == progress["agent_version_id"]
    assert version["spec"]["screens"] == SAMPLE_DRAFT["screens"]
    assert version["spec"]["data_reads"] == [
        {"connection_id": request["connection_id"], "tables": request["tables"]}
    ]
    assert version["spec"]["evaluation"] == {
        "dataset_id": progress["dataset_id"],
        "min_accuracy": "0.9",
        "max_p95_latency_ms": 600_000,
        "max_total_cost_usd": "1",
    }
    assert _audits(owner_session, request["id"])[-2:] == [
        "appgen.request.accepting",
        "appgen.request.accepted",
    ]
    state = _state(owner_session, request["id"])
    assert state["accepted_by"] is not None and state["accepted_revision_no"] == 2

    # accepting it again: 409, nothing more
    _code(_accept(admin, request["id"], _accept_body(2, model)), 409, "already_accepted")
    assert _counts(owner_session, request["project_id"]) == EXPECTED


# ------------------------------------------------------------------ pre-check (9.4)
def test_a_caller_without_agent_publish_gets_403_and_nothing_is_written(
    client: TestClient, admin: Hdr, world: World, owner_session: Session
) -> None:
    c = admin.client
    request = _drafted(admin, owner_session)
    model = _agent_model(admin)["model"]
    before = _counts(owner_session, request["project_id"])
    permissions = [
        "workspace:read",
        "project:read",
        "appgen:request:read",
        "appgen:request:write",
        "appgen:draft:accept",
    ]
    role = post(c, f"{A}/roles", admin,
                {"key": "accepter", "name": "Accepter", "permissions": permissions})  # fmt: skip
    uid = new_user(c, admin, "demo-a", CUSTOM, "a-long-password-1")
    world.passwords[CUSTOM] = "a-long-password-1"
    admin = user_session(client, world.passwords, ADMIN)
    binding = {"subject_type": "user", "subject_id": uid, "role_id": role["id"],
               "scope": {"type": "project", "id": request["project_id"]}}  # fmt: skip
    post(admin.client, f"{A}/role-bindings", admin, binding)
    custom = user_session(client, world.passwords, CUSTOM)
    assert get(custom.client, custom, request["id"])["status"] == "drafted"

    refused = _code(_accept(custom, request["id"], _accept_body(1, model)), 403, "forbidden")
    assert "prompt:create" in refused["detail"]
    state = _state(owner_session, request["id"])
    assert state["status"] == "drafted" and state["accept_progress"] == {}
    assert state["accepted_revision_no"] is None and state["accepted_by"] is None
    assert _counts(owner_session, request["project_id"]) == before
    assert "appgen.request.accepting" not in _audits(owner_session, request["id"])


# ------------------------------------------------------------------ resume (9.4)
class InjectedFailure(AppError):
    status, code, title = 503, "injected_failure", "Service Unavailable"


def test_a_failed_step_resumes_without_duplicates(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _drafted(admin, owner_session)
    project = request["project_id"]
    model = _agent_model(admin)["model"]
    failures = {"left": 2}
    create = ArtifactService.create

    def failing(self: ArtifactService, kind: str, payload: Any, **kw: Any) -> Any:  # Any: DTOs
        if kind == "agents" and failures["left"]:
            failures["left"] -= 1
            raise InjectedFailure("injected after the prompt and tools steps")
        return create(self, kind, payload, **kw)

    monkeypatch.setattr(ArtifactService, "create", failing)
    _code(_accept(admin, request["id"], _accept_body(1, model)), 503, "injected_failure")
    state = _state(owner_session, request["id"])
    assert state["status"] == "accepting" and state["status_reason"] == "injected_failure"
    progress = state["accept_progress"]
    # the agent's id is minted and recorded before its (failed) create
    assert set(progress) == {
        "prompt_artifact_id",
        "prompt_version_id",
        "tool_artifact_ids",
        "tool_version_ids",
        "agent_artifact_id",
    }
    assert set(progress["tool_version_ids"]) == {"orders_by_customer", "invoiced_by_region"}
    first = _counts(owner_session, project)
    assert first == {
        **EXPECTED,
        "agents": 0,
        "versions": 3,
        "published": 3,
        "datasets": 0,
        "solutions": 0,
        "solution_versions": 0,
    }
    # another revision while accepting: refused
    _code(_accept(admin, request["id"], _accept_body(2, model)), 409, "request_not_acceptable")

    # a second resume after the completed steps reuses their recorded ids
    _code(_accept(admin, request["id"], _accept_body(1, model)), 503, "injected_failure")
    assert _state(owner_session, request["id"])["accept_progress"] == progress
    assert _counts(owner_session, project) == first

    r = _accept(admin, request["id"], _accept_body(1, model))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted" and r.json()["status_reason"] is None
    final = r.json()["accept_progress"]
    assert {k: final[k] for k in progress} == progress
    assert _counts(owner_session, project) == EXPECTED
    assert _audits(owner_session, request["id"]).count("appgen.request.accepting") == 1


# ------------------------------------------------------------------ refusals
def test_states_that_cannot_be_accepted(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    cid, drafting = databricks(c, admin), drafting_model(c, admin)
    model = _agent_model(admin)["model"]
    failed = create(c, admin, body(cid, drafting, brief=f"broken {INVALID}"))
    assert failed.status_code == 202, failed.text
    assert run_job(c, owner_session, failed.json()["id"])
    assert get(c, admin, failed.json()["id"])["status"] == "failed"
    _code(
        _accept(admin, failed.json()["id"], _accept_body(1, model)), 409, "request_not_acceptable"
    )
    discarded = drafted(c, admin, owner_session, body(cid, drafting))["id"]
    assert c.post(request_url(discarded, tail="/discard"), headers=admin).status_code == 200
    _code(_accept(admin, discarded, _accept_body(1, model)), 409, "request_not_acceptable")

    request = drafted(c, admin, owner_session, body(cid, drafting))
    edit = {"base_revision_no": 1, "draft": SAMPLE_DRAFT}
    assert c.post(request_url(request["id"], tail="/drafts"), headers=admin, json=edit)
    _code(_accept(admin, request["id"], _accept_body(1, model)), 409, "stale_revision")
    assert _state(owner_session, request["id"])["status"] == "drafted"


def test_an_invalid_revision_or_a_taken_tool_key_writes_nothing(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    request = _drafted(admin, owner_session)
    project = request["project_id"]
    model = _agent_model(admin)["model"]
    broken = copy.deepcopy(SAMPLE_DRAFT)
    broken["tools"][0]["sql"] = "select id from main.secret.payroll where id = :customer_id"
    edit = {"base_revision_no": 1, "draft": broken}
    assert c.post(request_url(request["id"], tail="/drafts"), headers=admin, json=edit)
    before = _counts(owner_session, project)
    invalid = _code(_accept(admin, request["id"], _accept_body(2, model)), 422, "draft_invalid")
    assert invalid["errors"] and all(
        e["loc"][:4] == ["draft", "tools", "0", "sql"] for e in invalid["errors"]
    )
    state = _state(owner_session, request["id"])
    assert state["status"] == "drafted" and state["accept_progress"] == {}
    assert _counts(owner_session, project) == before

    # a valid revision 3 whose first tool's key a tool of the project already holds
    edit = {"base_revision_no": 2, "draft": SAMPLE_DRAFT}
    assert c.post(request_url(request["id"], tail="/drafts"), headers=admin, json=edit)
    art(c, admin, "tools", "orders_by_customer", builtin_tool_spec(), {"type": "project", "id": project})  # fmt: skip
    before = _counts(owner_session, project)
    taken = _code(_accept(admin, request["id"], _accept_body(3, model)), 422, "draft_invalid")
    assert [e["loc"] for e in taken["errors"]] == [["draft", "tools", "0", "name"]]
    state = _state(owner_session, request["id"])
    assert state["status"] == "drafted" and state["accept_progress"] == {}
    assert _counts(owner_session, project) == before
    assert "appgen.request.accepting" not in _audits(owner_session, request["id"])


# ------------------------------------------------------------------ `/2` end to end
@contextlib.contextmanager
def _worker(client: TestClient, settings: Settings) -> Iterator[None]:
    """A worker on a thread whose data-platform calls reach the app's fake workspace; one case
    run at a time (concurrent case runs of one connection can deadlock on its secret's
    `last_used_at`)."""
    app: Any = client.app  # Any: the FastAPI app behind the test client
    worker = Worker(
        fast(settings),
        worker_id=f"test-{time.monotonic_ns()}",
        concurrency=1,
        lease_seconds=5,
        poll_ms=100,
        reap_seconds=0.3,
        runtime=app.state.integration,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        worker.request_stop()
        thread.join(timeout=30)


def test_an_accepted_request_seals_under_compiler_2_and_its_evaluation_passes(
    client: TestClient, admin: Hdr, owner_session: Session, settings: Settings
) -> None:
    c = admin.client
    request = _drafted(admin, owner_session)
    stack = _agent_model(admin)
    post(
        c,
        f"{A}/models",
        admin,
        {
            "provider_id": stack["provider"],
            "model_key": SERVED,
            "display_name": "Served chat",
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": "active",
            "rate_limits": {},
        },
    )
    card = rate_card(c, admin, model_key=SERVED)
    rate_version(c, admin, card["id"], currency="USD", input_rate=1, output_rate=1)
    accepted = _accept(admin, request["id"], _accept_body(1, stack["model"]))
    assert accepted.status_code == 200, accepted.text
    progress = accepted.json()["accept_progress"]
    solution, version = progress["solution_id"], progress["solution_version_id"]
    lake = request["connection_id"]
    seal_body = {
        "environment_bindings": {
            "model_bindings": {stack["model"]: {"provider_id": stack["provider"], "model": SERVED}},
            "connection_bindings": {lake: lake},
        }
    }
    sealed = c.post(
        f"{A}{PROJECT}/solutions/{solution}/versions/{version}/releases",
        headers=admin,
        json=seal_body,
    )
    assert sealed.status_code == 201, sealed.text
    release = sealed.json()
    manifest = release["manifest"]
    assert manifest["compiler_version"] == "shifde.packaging/2"
    assert manifest["screens"] == SAMPLE_DRAFT["screens"]
    assert manifest["app"] == {"generator_version": "shifde.appgen/1.0"}
    assert release["digest"] == digest(manifest)

    capture(owner_session, "demo-a", "full")
    requested = c.post(f"{A}/releases/{release['id']}/evaluations", headers=admin, json={})
    assert requested.status_code == 201, requested.text
    run_url = f"{A}/evaluation-runs/{requested.json()['id']}"

    def scored() -> dict[str, Any] | None:
        got = c.get(run_url, headers=admin)
        assert got.status_code == 200, got.text
        out: dict[str, Any] = got.json()
        return out if out["status"] in {"passed", "failed", "error"} else None

    with _worker(client, settings):
        result: dict[str, Any] = wait_for(scored, 120.0)
    assert result["status"] == "passed", result
    assert result["accuracy"] == "1"


# ------------------------------------------------------------------ issue #316
AGENT_KEY = SAMPLE_DRAFT["agent"]["name"]


def _fail_agent_create(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """`ArtifactService.create` of an agent raises `InjectedFailure` while `state["fail"]` lasts;
    with `state["hold"]` (entered, release events) it sets `entered` and waits for `release`
    once."""
    create = ArtifactService.create

    def wrapped(self: ArtifactService, kind: str, payload: Any, **kw: Any) -> Any:  # Any: DTOs
        if kind == "agents" and state.get("fail"):
            state["fail"] -= 1
            raise InjectedFailure("injected in the agent step")
        if kind == "agents" and state.get("hold"):
            entered, release = state.pop("hold")
            entered.set()
            assert release.wait(60)
        return create(self, kind, payload, **kw)

    monkeypatch.setattr(ArtifactService, "create", wrapped)


def _accepting(
    admin: Hdr, owner: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """A request left `accepting` by a failed agent step: (request, runtime model, the injection
    state)."""
    request = _drafted(admin, owner)
    model = _agent_model(admin)["model"]
    state: dict[str, Any] = {"fail": 1}
    _fail_agent_create(monkeypatch, state)
    _code(_accept(admin, request["id"], _accept_body(1, model)), 503, "injected_failure")
    assert _state(owner, request["id"])["status"] == "accepting"
    return request, model, state


def _set_claim(owner: Session, request_id: str, seconds: int) -> None:
    """Store the claim of another (running or crashed) call whose lease ends in `seconds`."""
    owner.rollback()
    owner.execute(
        text(
            "UPDATE shifde.appgen_requests SET accept_progress = accept_progress || "
            "jsonb_build_object('claim', jsonb_build_object('token', CAST(:tok AS text), "
            "'until', to_jsonb(now() + make_interval(secs => :s)))) WHERE id = :r"
        ),
        {"tok": str(uuid7()), "s": seconds, "r": request_id},
    )
    owner.commit()


def _audit_details(owner: Session, request_id: str, action: str) -> list[Any]:
    """The details the service wrote (without `actor`, which `audit.record` adds to every
    event)."""
    return [
        {k: v for k, v in r["details"].items() if k != "actor"}
        for r in _rows(
            owner,
            "SELECT details FROM shifde.audit_events WHERE target_id = :r AND action = :a "
            "ORDER BY chain_seq",
            r=request_id,
            a=action,
        )
    ]


def test_concurrent_resumes_run_the_steps_once(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, model, _ = _accepting(admin, owner_session, monkeypatch)
    start = threading.Barrier(2, timeout=30)

    def resume() -> httpx.Response:
        start.wait()
        return _accept(admin, request["id"], _accept_body(1, model))

    with ThreadPoolExecutor(2) as pool:
        answers = [f.result() for f in [pool.submit(resume), pool.submit(resume)]]
    outcomes = sorted((r.status_code, r.json().get("code") or r.json()["status"]) for r in answers)
    assert outcomes == [(200, "accepted"), (409, "accept_in_progress")], [r.text for r in answers]
    assert _counts(owner_session, request["project_id"]) == EXPECTED
    assert "claim" not in _state(owner_session, request["id"])["accept_progress"]


def test_a_resume_while_another_runs_a_step_gets_409_and_writes_nothing(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, model, state = _accepting(admin, owner_session, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    state["hold"] = (entered, release)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(_accept, admin, request["id"], _accept_body(1, model))
        assert entered.wait(60)
        before, audits = _state(owner_session, request["id"]), _audits(owner_session, request["id"])
        assert "claim" in before["accept_progress"]
        # the API never shows the claim
        assert "claim" not in get(admin.client, admin, request["id"])["accept_progress"]

        _code(_accept(admin, request["id"], _accept_body(1, model)), 409, "accept_in_progress")
        after = _state(owner_session, request["id"])
        assert after == before and after["status_reason"] == "injected_failure"
        assert _audits(owner_session, request["id"]) == audits
        release.set()
        r = first.result()
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted" and "claim" not in r.json()["accept_progress"]
    assert _counts(owner_session, request["project_id"]) == EXPECTED


def test_an_expired_claim_is_taken_over(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, model, _ = _accepting(admin, owner_session, monkeypatch)
    _set_claim(owner_session, request["id"], 3600)  # a holder still within its lease
    assert "claim" not in get(admin.client, admin, request["id"])["accept_progress"]
    _code(_accept(admin, request["id"], _accept_body(1, model)), 409, "accept_in_progress")
    assert _counts(owner_session, request["project_id"])["agents"] == 0

    _set_claim(owner_session, request["id"], -1)  # a holder that crashed: its lease has passed
    r = _accept(admin, request["id"], _accept_body(1, model))
    assert r.status_code == 200, r.text
    assert _counts(owner_session, request["project_id"]) == EXPECTED
    assert "claim" not in _state(owner_session, request["id"])["accept_progress"]


def test_only_the_claim_holder_writes_progress(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _ = _accepting(admin, owner_session, monkeypatch)
    tenant = tenant_id(owner_session, "demo-a")
    owner_session.rollback()
    rid, holder, other = request["id"], uuid7(), uuid7()
    claim = {"tenant_id": tenant, "revision_no": 1, "lease_seconds": 120}
    assert repository.claim_accepting(owner_session, rid, token=holder, **claim)
    assert not repository.claim_accepting(owner_session, rid, token=other, **claim)
    stamp = {"tenant_id": tenant, "key": "probe", "value": "x", "lease_seconds": 120}
    assert not repository.record_progress(owner_session, rid, token=other, **stamp)
    assert not repository.accept_failed(
        owner_session, rid, tenant_id=tenant, reason="lost", token=other
    )
    assert not repository.finish_accepting(
        owner_session, rid, tenant_id=tenant, solution_version_id=uuid7(), token=other
    )
    assert repository.record_progress(owner_session, rid, token=holder, **stamp)
    assert repository.accept_failed(
        owner_session, rid, tenant_id=tenant, reason="released", token=holder
    )
    owner_session.commit()
    state = _state(owner_session, rid)
    assert state["status_reason"] == "released" and state["accept_progress"]["probe"] == "x"
    assert "claim" not in state["accept_progress"]


def test_a_failed_step_writes_its_audit_event(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _ = _accepting(admin, owner_session, monkeypatch)
    assert _audit_details(owner_session, request["id"], "appgen.request.accept_failed") == [
        {"revision_no": 1, "step": "agent", "status_reason": "injected_failure"}
    ]


def test_a_resume_after_a_crash_adopts_the_recorded_artifact(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _drafted(admin, owner_session)
    model = _agent_model(admin)["model"]
    published = AppGenService._published
    failures = {"left": 1}

    def failing(self: AppGenService, kind: str, artifact_id: Any) -> str:  # Any: a UUID
        if kind == "prompts" and failures["left"]:
            failures["left"] -= 1
            raise InjectedFailure("injected after the prompt's create committed")
        return published(self, kind, artifact_id)

    monkeypatch.setattr(AppGenService, "_published", failing)
    _code(_accept(admin, request["id"], _accept_body(1, model)), 503, "injected_failure")
    progress = _state(owner_session, request["id"])["accept_progress"]
    assert set(progress) == {"prompt_artifact_id"}
    assert _audit_details(owner_session, request["id"], "appgen.request.accept_failed") == [
        {"revision_no": 1, "step": "prompt", "status_reason": "injected_failure"}
    ]

    r = _accept(admin, request["id"], _accept_body(1, model))
    assert r.status_code == 200, r.text
    prompts = _rows(
        owner_session,
        "SELECT id FROM shifde.artifacts WHERE kind = 'prompts' AND project_id = :p AND key = :k",
        p=request["project_id"],
        k=AGENT_KEY,
    )
    assert [str(p["id"]) for p in prompts] == [progress["prompt_artifact_id"]]
    assert r.json()["accept_progress"]["prompt_artifact_id"] == progress["prompt_artifact_id"]
    assert _counts(owner_session, request["project_id"]) == EXPECTED


def test_a_recorded_id_without_its_artifact_adopts_no_other_holder(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _drafted(admin, owner_session)
    project = request["project_id"]
    model = _agent_model(admin)["model"]
    create = ArtifactService.create
    failures = {"left": 1}

    def failing(self: ArtifactService, kind: str, payload: Any, **kw: Any) -> Any:  # Any: DTOs
        if kind == "prompts" and failures["left"]:
            failures["left"] -= 1
            raise InjectedFailure("injected before the prompt's create")
        return create(self, kind, payload, **kw)

    monkeypatch.setattr(ArtifactService, "create", failing)
    _code(_accept(admin, request["id"], _accept_body(1, model)), 503, "injected_failure")
    assert set(_state(owner_session, request["id"])["accept_progress"]) == {"prompt_artifact_id"}
    assert _counts(owner_session, project)["prompts"] == 0
    # a prompt with the draft's key but another id appears in the project
    scope = {"type": "project", "id": project}
    art(admin.client, admin, "prompts", AGENT_KEY, prompt_spec(), scope)
    before = _counts(owner_session, project)

    taken = _code(_accept(admin, request["id"], _accept_body(1, model)), 422, "draft_invalid")
    assert taken["detail"] == "a name of the draft is already used in the project"
    assert [(e["loc"], e["type"]) for e in taken["errors"]] == [
        (["draft", "agent", "name"], "key_taken")
    ]
    assert _counts(owner_session, project) == before
    assert "claim" not in _state(owner_session, request["id"])["accept_progress"]


def _key_check(holder: Any, agent_name: str, tools: list[str]) -> DraftInvalid:
    """`_check_keys` of a draft built without validation, every key held by `holder`."""
    service: Any = SimpleNamespace(  # Any: a stand-in for the service's artifact reads
        artifacts=SimpleNamespace(key_holder=lambda kind, ws, project, key: holder)
    )
    draft = appgen_schemas.Draft.model_construct(
        agent=appgen_schemas.DraftAgent.model_construct(name=agent_name, instructions="x"),
        tools=[appgen_schemas.DraftTool.model_construct(name=t) for t in tools],
    )
    row = {"workspace_id": uuid7(), "project_id": uuid7(), "accept_progress": {}}
    with pytest.raises(DraftInvalid) as raised:
        AppGenService._check_keys(service, row, draft)
    return raised.value


def test_key_refusals_say_whether_a_key_is_invalid_or_taken() -> None:
    invalid = _key_check(None, "Not A Key!", [])
    assert invalid.detail == "a name of the draft cannot be an artifact key"
    assert [e["type"] for e in invalid.errors or []] == ["invalid_key", "invalid_key"]
    taken = _key_check(uuid7(), "sales-agent", ["orders"])
    assert taken.detail == "a name of the draft is already used in the project"
    assert {e["type"] for e in taken.errors or []} == {"key_taken"}
    both = _key_check(uuid7(), "Not A Key!", ["orders"])
    assert both.detail == "a name of the draft cannot be used in the project"


def test_a_refused_runtime_model_is_named_the_runtime_model(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    request = _drafted(admin, owner_session)
    old = fake_model_stack(c, admin, lifecycle="deprecated", conn_name="old-model")["model"]
    refused = _code(_accept(admin, request["id"], _accept_body(1, old)), 422, "model_unavailable")
    assert refused["detail"] == "the runtime model cannot be used"
    assert _state(owner_session, request["id"])["status"] == "drafted"
    drafting = c.post(requests_url(), headers=admin, json=body(request["connection_id"], old))
    refused = _code(drafting, 422, "model_unavailable")
    assert refused["detail"] == "the drafting model cannot be used"
