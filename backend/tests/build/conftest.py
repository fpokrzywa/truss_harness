"""build tests (part 1: data layer): two clients from the tenancy world; the assertions run as
`shifde_app` under row-level security (`tests.tenancy.conftest.app_session` asserts the role).

Rows the tests add are removed before each test, as the schema owner.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from shifde.core.db import Database
from tests.conftest import CLEAN_TABLES
from tests.connections.conftest import owner_vendor
from tests.tenancy.conftest import World, world

__all__ = ["World", "owner_vendor", "world"]

for _table in (
    "pipeline_schedules",
    "pipeline_validation_reports",
    "pipeline_version_refs",
    "prompt_test_runs",
    "publish_requests",
    "artifact_version_pins",
    "artifact_versions",
    "artifacts",
    "models",
    "model_providers",
    "connections",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
