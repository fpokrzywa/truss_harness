"""ontology tests: two clients from the tenancy world; the assertions run as `shifde_app` under
row-level security and as the schema owner (no row-level security). Rows the tests add are
removed before each test, as the schema owner."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shifde.core.settings import Settings
from tests.build.conftest import owner_session, people, sides
from tests.conftest import CLEAN_TABLES
from tests.ontology.routes import ontology_client
from tests.tenancy.conftest import World, world

__all__ = ["World", "owner_session", "people", "sides", "world"]


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with ontology_client(settings) as c:
        yield c


for _table in (
    "ontology_materializations",
    "ontology_query_lineage",
    "ontology_principal_attributes",
    "ontology_access_policies",
    "ontology_version_info",
    "ontology_source_mappings",
    "artifact_version_pins",
    "artifact_versions",
    "artifacts",
    "connections",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)
