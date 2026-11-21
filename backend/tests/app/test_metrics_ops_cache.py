"""The `/api/metrics` ops-stats cache belongs to one app instance (issue #96): a snapshot taken by
an earlier app in the same process, before any run finished, must not hide a later app's run."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.settings import Settings
from shifde.observability import api as observability_api
from tests._support.fullapp import ORIGIN, World
from tests._support.p2util import login
from tests._support.p3util import ADMIN
from tests._support.p5util import start_run, thread_worker, wait_run
from tests._support.p8util import priced_stack

TOKEN = "metrics-token-123456"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _metrics_client(settings: Settings) -> TestClient:
    s = settings.model_copy(update={"metrics_token": SecretStr(TOKEN)})
    return TestClient(
        create_app(s),
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        client=("testclient", 5000),
    )


def test_a_second_app_does_not_reuse_the_first_apps_ops_stats_snapshot(
    world: World, owner_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # every snapshot looks fresh however long the test takes; only the metrics module's clock
    monkeypatch.setattr(observability_api, "time", types.SimpleNamespace(monotonic=lambda: 1.0))
    with _metrics_client(settings) as first:
        early = first.get("/api/metrics", headers=AUTH)
        assert early.status_code == 200
        assert "shifde_runs_finished_window{" not in early.text  # no run has finished yet
    with _metrics_client(settings) as second:
        h = login(second, world.passwords, ADMIN)
        stack = priced_stack(second, h, owner_session)
        run = start_run(second, h, stack.agent["id"], "hello")
        with thread_worker(settings):
            assert wait_run(second, h, run["id"])["status"] == "completed"
        body = second.get("/api/metrics", headers=AUTH).text
    assert 'shifde_runs_finished_window{error_class="",kind="agent",status="completed"}' in body
