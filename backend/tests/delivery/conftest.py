"""Delivery tests: the deployment target and executor routes over a minimal FastAPI app (B-1020
pattern), with the tenancy route-test world (two clients, fresh slugs and ids per test) and its
people.

Targets, claims, executors and their credentials, jobs and journal rows, sign-ins and audit rows
are removed before each test (`tests/conftest.py: clean_data`). The gateway tests register their
own `gateway.JOB_KINDS` entries for `apply` (a write) and `observe` (read-only) through the
`job_kinds` fixture; production code registers only `plan`. Every address is unique per test (`address()`), so no
test depends on another's claim.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.native.contracts import JobResultSubmission
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.delivery import gateway
from tests.conftest import CLEAN_TABLES
from tests.connections.routes import as_user
from tests.delivery.routes import create_executor, delivery_client, issue
from tests.tenancy.conftest import Client, World, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

for _table in (
    "delivery_approvals",
    "delivery_journal",
    "delivery_jobs",
    "delivery_plans",
    "delivery_artifacts",
    "delivery_executor_credentials",
    "delivery_executors",
    "delivery_targets",
    "delivery_platform_claims",
    "login_lockouts",
    "user_identities",
    "sessions",
    "local_credentials",
    "memberships",
    "idp_configs",
    "users",
    "audit_events",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with delivery_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@dataclass(frozen=True)
class ExecutorSide:
    tenant: UUID
    slug: str
    executor_id: str
    token: str


def make_executor(client: TestClient, c: Client, email: str) -> ExecutorSide:
    """An active executor of `c` with one live token (the client is signed out afterwards)."""
    h = as_user(client, email)
    made = create_executor(client, h, c.slug)
    token = issue(client, h, c.slug, made["id"])["secret"]
    client.cookies.clear()
    return ExecutorSide(c.tenant, c.slug, made["id"], token)


@pytest.fixture
def executor_a(client: TestClient, world: World, people: People) -> ExecutorSide:
    return make_executor(client, world.a, people.owner_a)


class ProbeResult(BaseModel):
    """The test-only result model of the registered kinds."""

    model_config = ConfigDict(extra="forbid")
    observed: int


@dataclass
class JobKinds:
    """What the registered `on_result` hook saw: (job id, the job status read in its session)."""

    calls: list[tuple[UUID, str]] = field(default_factory=list)

    def on_result(self, session: Session, row: gateway.Row, body: JobResultSubmission) -> None:
        status = session.execute(
            text("SELECT status FROM shifde.delivery_jobs WHERE id = :j"), {"j": row["id"]}
        ).scalar_one()
        self.calls.append((row["id"], status))


@pytest.fixture
def job_kinds(monkeypatch: pytest.MonkeyPatch) -> JobKinds:
    """Register `apply` and `observe` with `ProbeResult`; removed again after the test."""
    kinds = JobKinds()
    for kind in ("apply", "observe"):
        monkeypatch.setitem(
            gateway.JOB_KINDS, kind, gateway.JobKindDefinition(ProbeResult, kinds.on_result)
        )
    return kinds
