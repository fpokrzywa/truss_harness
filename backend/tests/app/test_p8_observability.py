"""SETH 3ba0e06 `tests/test_p8_observability.py`, the seven tests that need the full app, the worker
or the database: trace propagation API -> worker -> adapter call, correlated logs without secrets
or prompt bodies, the Prometheus metrics endpoint, the `shifde_ops_stats` function, performance
and platform health. Assertions unchanged; imports name `tests._support.*`. One setup change:
the platform-health test waits for the worker's first reaper pass (`workers.last_reap_at`) after
SETH's fixed 1 s sleep, which was too short on a loaded shared cluster.

Not here: the five unit tests on spans, traceparent parsing and the OTLP/console exporters, which
are in `tests/observability/test_p8_observability.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import OpenAIFakeServer
from shifde.app import create_app
from shifde.core.settings import Settings
from shifde.observability import metrics, tracing
from tests._support.fullapp import ORIGIN, World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, PLATFORM, VIEWER, A
from tests._support.p5util import (
    oai_models,
    start_run,
    thread_worker,
    user_session,
    wait_for,
    wait_run,
)
from tests._support.p8util import priced_stack

SECRET_PROMPT = "TOPSECRET-PROMPT-BODY-777"
SECRET_REPLY = "TOPSECRET-REPLY-888"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


@pytest.fixture(autouse=True)
def clean_telemetry() -> Iterator[None]:
    tracing.tracer.clear()
    tracing.tracer.configure("none")
    metrics.registry.reset()
    yield
    tracing.tracer.configure("none")


def _lines(out: str) -> list[dict[str, Any]]:
    found = []
    for line in out.splitlines():
        if line.startswith("{"):
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
    return found


def _traced_run(
    client: TestClient, h: dict[str, str], agent_id: str, text_in: str
) -> tuple[dict[str, Any], Any]:
    body = {
        "kind": "agent",
        "agent_id": agent_id,
        "workspace": "main",
        "project": "sample",
        "input": text_in,
    }
    r = client.post(
        f"{A}/runs",
        json=body,
        headers={**h, "traceparent": f"00-{TRACE}-{PARENT}-01", "Idempotency-Key": "trace-1"},
    )
    assert r.status_code == 202, r.text
    return r.json(), r


# ------------------------------------------------------------------ tracing
def test_trace_ids_propagate_from_the_api_request_through_the_job_to_the_worker_and_adapter(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    run, resp = _traced_run(client, h, stack.agent["id"], "what is the weather")
    assert run["trace_id"] == TRACE
    assert resp.headers["traceparent"].startswith(f"00-{TRACE}-")
    job = owner_session.execute(
        text("SELECT traceparent FROM shifde.jobs WHERE run_id = :r"), {"r": run["id"]}
    ).scalar_one()
    assert job.startswith(f"00-{TRACE}-")
    with thread_worker(settings):
        assert wait_run(client, h, run["id"])["status"] == "completed"
    spans = {s.name: s for s in tracing.tracer.spans(TRACE)}
    api = spans["POST /api/v1/orgs/{org}/runs"]
    assert api.parent_span_id == PARENT  # continues the caller's trace
    advance = spans["run.advance"]
    assert advance.parent_span_id == job.split("-")[2]  # the span that queued the job
    model = spans["adapter.model_call"]
    assert model.trace_id == TRACE and model.parent_span_id == advance.span_id
    assert advance.attributes["run_id"] == run["id"]
    # a request without a traceparent starts a trace of its own and the run keeps it
    other = start_run(client, h, stack.agent["id"], "another", key="trace-2")
    assert other["trace_id"] != TRACE and len(other["trace_id"]) == 32


def test_logs_are_correlated_and_never_carry_prompts_replies_or_secrets(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = login(client, world.passwords, ADMIN)
    script = [{"match": SECRET_PROMPT, "reply": SECRET_REPLY}]
    stack = priced_stack(client, h, owner_session, script=script)
    run, _ = _traced_run(client, h, stack.agent["id"], f"please {SECRET_PROMPT}")
    capsys.readouterr()
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed"
    out = capsys.readouterr().out
    records = _lines(out)
    finished = [r for r in records if r.get("event") == "run.advance_finished"]
    assert finished, "the worker logs one line per advance"
    rec = finished[0]
    assert rec["trace_id"] == TRACE and rec["run_id"] == run["id"]
    assert rec["tenant_id"] == str(tenant_id(owner_session, "demo-a"))
    assert rec["correlation_id"] and rec["span_id"]
    requests = [r for r in records if r.get("event") == "request.completed"]
    assert requests and all(r.get("trace_id") for r in requests)
    # nothing sensitive reached the log stream
    secrets_in = [SECRET_PROMPT, SECRET_REPLY, *world.passwords.values()]
    for value in secrets_in:
        assert value not in out, f"{value!r} leaked into the logs"
    spans_json = json.dumps([s.attributes for s in tracing.tracer.spans()])
    assert SECRET_PROMPT not in spans_json and SECRET_REPLY not in spans_json


# ------------------------------------------------------------------ metrics
def _metrics_client(settings: Settings, token: str | None, host: str = "testclient") -> TestClient:
    s = settings.model_copy(update={"metrics_token": SecretStr(token) if token else None})
    return TestClient(
        create_app(s),
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        client=(host, 5000),
    )


def test_metrics_need_the_bearer_token_or_a_loopback_client(
    world: World, settings: Settings
) -> None:
    with _metrics_client(settings, "metrics-token-123456") as c:
        assert c.get("/api/metrics").status_code == 401
        bad = c.get("/api/metrics", headers={"Authorization": "Bearer wrong-token-000000"})
        assert bad.status_code == 401
        basic = c.get("/api/metrics", headers={"Authorization": "Basic YTpi"})
        assert basic.status_code == 401
        ok = c.get("/api/metrics", headers={"Authorization": "Bearer metrics-token-123456"})
        assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")
    with _metrics_client(settings, None, "203.0.113.9") as remote:
        assert remote.get("/api/metrics").status_code == 403  # no token, not loopback
    with _metrics_client(settings, None, "127.0.0.1") as local:
        assert local.get("/api/metrics").status_code == 200


def test_metrics_expose_route_templates_queue_and_outcomes_and_leak_nothing(
    world: World, owner_session: Session, settings: Settings
) -> None:
    with _metrics_client(settings, "metrics-token-123456") as c:
        h = login(c, world.passwords, ADMIN)
        stack = priced_stack(c, h, owner_session)
        run = start_run(c, h, stack.agent["id"], f"hello {SECRET_PROMPT}")
        with thread_worker(settings):
            assert wait_run(c, h, run["id"])["status"] == "completed"
        assert c.get(
            f"{A}/budgets/00000000-0000-0000-0000-000000000000", headers=h
        ).status_code in (
            403,
            404,
        )
        c.get("/api/v1/nope/Bearer-abcdefghijklmnop")
        body = c.get("/api/metrics", headers={"Authorization": "Bearer metrics-token-123456"}).text
    assert 'route="/api/v1/orgs/{org}/runs"' in body and 'route="unmatched"' in body
    assert "shifde_http_request_duration_seconds_bucket" in body
    assert 'shifde_http_errors_total{class="4xx"' in body
    assert 'shifde_queue_jobs{state="queued"}' in body
    assert "shifde_queue_oldest_queued_age_seconds" in body
    assert 'shifde_runs_finished_window{error_class="",kind="agent",status="completed"}' in body
    assert 'shifde_model_calls_window{provider="fake"}' in body
    assert 'shifde_model_tokens_window{direction="input",provider="fake"}' in body
    # labels hold no identifiers, emails, prompts or tokens
    for forbidden in (
        run["id"], "demo-a", "admin@", SECRET_PROMPT, "metrics-token", "abcdefghijklmnop",
        *world.passwords.values(),
    ):  # fmt: skip
        assert forbidden not in body, forbidden


def test_the_ops_stats_function_returns_only_aggregates(
    client: TestClient, world: World, owner_session: Session, app_url: str
) -> None:
    from shifde.core.db import Database

    pool = Database(app_url, pool_size=1)
    try:
        with pool.session() as s:  # the app role, no tenant context at all
            stats = s.execute(text("SELECT shifde.shifde_ops_stats(60)")).scalar_one()
    finally:
        pool.dispose()
    assert set(stats) == {"window_seconds", "queue", "runs", "models", "budget_denials"}
    blob = json.dumps(stats)
    assert str(tenant_id(owner_session, "demo-a")) not in blob and "demo-a" not in blob


# ------------------------------------------------------------------ performance + health
def test_performance_reports_percentiles_and_error_rates_by_agent_and_model(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    oai_server: OpenAIFakeServer,
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    models = oai_models(client, h, oai_server, ["error-model"])
    bad = stack.new_agent("failing", model={"model_id": models["error-model"]})
    ok_run = start_run(client, h, stack.agent["id"], "what is the weather", key="p-1")
    bad_run = start_run(client, h, bad["id"], "ping", key="p-2")
    with thread_worker(settings):
        wait_run(client, h, ok_run["id"])
        assert wait_run(client, h, bad_run["id"])["status"] == "failed"
    agents = client.get(f"{A}/observability/performance?group_by=agent", headers=h).json()
    by = {g["key"]: g for g in agents["groups"]}
    assert by[stack.agent["id"]]["runs"] == 1 and by[stack.agent["id"]]["error_rate"] == 0
    assert by[bad["id"]]["error_rate"] == 1.0 and by[bad["id"]]["error_count"] == 1
    for g in agents["groups"]:
        assert 0 < g["p50_ms"] <= g["p95_ms"] <= g["p99_ms"]
        assert g["label"]
    models_out = client.get(f"{A}/observability/performance?group_by=model", headers=h).json()
    errs = {g["key"]: g["error_rate"] for g in models_out["groups"]}
    assert errs[models["error-model"]] == 1.0 and errs[stack.model] == 0.0
    assert client.get(f"{A}/observability/performance?group_by=zzz", headers=h).status_code == 422
    vh = user_session(client, world.passwords, VIEWER)
    assert vh.client.get(f"{A}/observability/performance", headers=vh).status_code == 403


def test_platform_health_details_are_for_platform_admins_only(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    ph = user_session(client, world.passwords, PLATFORM)
    th = login(client, world.passwords, ADMIN)
    assert client.get("/api/v1/platform/health-details", headers=th).status_code == 403
    with thread_worker(settings, reap=0.2) as worker:
        import time

        time.sleep(1.0)
        # setup only (not in SETH): on a loaded shared cluster the first reaper pass can take
        # longer than SETH's fixed 1 s, so wait until this worker has recorded one
        wait_for(
            lambda: owner_session.execute(
                text("SELECT last_reap_at FROM shifde.workers WHERE id = :i"),
                {"i": worker.worker_id},
            ).scalar_one_or_none()
        )
        got = ph.client.get("/api/v1/platform/health-details", headers=ph)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["database"]["reachable"] is True and body["migrations"]["status"] == "ok"
    assert body["workers"]["running"] >= 1 and body["workers"]["known"] >= 1
    assert set(body["queue"]) >= {"queued", "leased", "oldest_queued_age_seconds"}
    assert body["reaper"]["last_run_at"] is not None
    assert set(body["recent_error_rates"]) >= {"run_failure_rate", "budget_denials"}
    assert body["telemetry"]["exporter"] == "none"
