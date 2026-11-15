"""ontology tests: two clients from the tenancy world; the assertions run as `shifde_app` under
row-level security and as the schema owner (no row-level security). Rows the tests add are
removed before each test, as the schema owner."""

from __future__ import annotations

from tests.conftest import CLEAN_TABLES
from tests.tenancy.conftest import World, world

__all__ = ["World", "world"]

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
