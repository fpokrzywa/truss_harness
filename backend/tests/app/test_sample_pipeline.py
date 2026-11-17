"""`shifde.cli.sample_pipeline.seed_sample_pipeline` (`shifde-admin dev-sample-pipeline`) over the
full app and the sample database (B-2430). SETH 3ba0e06 has no test of the seeding itself, only of
`reference_pipeline_spec` (`tests/app/test_p6_exec3.py`)."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.cli import sample_pipeline
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A, B


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_dev_sample_pipeline_seeds_the_reference_pipeline_idempotently(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the command reads `.devdata/`: the test hands it the test's sample database, sink token and
    # administrator password instead (nothing is read from or written to `.devdata/`)
    dev = {"SHIFDE_SAMPLE_DATABASE_URL": sample_db_url, "SHIFDE_DEV_REST_TOKEN": "t" * 32}
    monkeypatch.setattr(sample_pipeline, "read_dev_env", lambda: dict(dev))
    monkeypatch.setattr(sample_pipeline, "_read_password", lambda email: world.passwords[email])
    port = _free_port()
    seeded = sample_pipeline.seed_sample_pipeline(
        settings, org="demo-a", admin_email=ADMIN, rest_port=port
    )
    assert seeded.created and seeded.rest_url == f"http://127.0.0.1:{port}"
    again = sample_pipeline.seed_sample_pipeline(
        settings, org="demo-a", admin_email=ADMIN, rest_port=port
    )
    assert not again.created
    assert (again.pipeline_id, again.version_id) == (seeded.pipeline_id, seeded.version_id)
    assert again.connection_ids == seeded.connection_ids and again.agent_id == seeded.agent_id

    h = login(client, world.passwords, ADMIN)
    pipe = client.get(f"{A}/pipelines/{seeded.pipeline_id}", headers=h).json()
    assert pipe["key"] == sample_pipeline.PIPELINE_KEY
    assert pipe["latest_published_version"]["id"] == seeded.version_id
    pg = client.get(f"{A}/connections/{seeded.connection_ids['sample_postgres']}", headers=h).json()
    assert (pg["status"], pg["access_mode"]) == ("active", "read")
    sink = client.get(f"{A}/connections/{seeded.connection_ids['rest_sink']}", headers=h).json()
    assert (sink["status"], sink["access_mode"]) == ("active", "read_write")

    # nothing of it exists for the other organization
    hb = login(client, world.passwords, "admin@demo-b.local")
    assert client.get(f"{B}/pipelines/{seeded.pipeline_id}", headers=hb).status_code == 404
    for conn_id in seeded.connection_ids.values():
        assert client.get(f"{B}/connections/{conn_id}", headers=hb).status_code == 404
    names = {
        c["name"] for c in client.get(f"{B}/connections?limit=200", headers=hb).json()["items"]
    }
    assert not names & {"dev-sample-postgres", "dev-rest-sink", "dev-fake-model"}
