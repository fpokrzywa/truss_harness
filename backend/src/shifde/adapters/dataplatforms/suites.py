"""Conformance suites of the data-platform adapters against their in-process fakes (design
2026-11-22 section 7.5).

They are run by pytest with `run_suite` and are deliberately NOT in `shifde.adapters.suites.SUITES`
(and the adapters define no `conformance_suite()`): a fake is not evidence, so `shifde-admin
adapters verify` records nothing and maturity stays `experimental`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from shifde.adapters.base import Capability, ConnectionConfig
from shifde.adapters.conformance import Check, ConformanceSuite, connection, exec_ctx, expect
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import SELECT_ONE, FakeDatabricksWorkspace
from shifde.core.egress import EgressGuard, installation_layer

SUITE_TOKEN = "dbx-suite-token-0001"  # scan-secrets: allow, the fake's accepted test token


@dataclass(frozen=True)
class DatabricksTarget:
    fake: FakeDatabricksWorkspace
    guard: EgressGuard


class DatabricksSqlSuite(ConformanceSuite):
    adapter_key = "databricks"

    @contextmanager
    def _target(self) -> Iterator[DatabricksTarget]:
        fake = FakeDatabricksWorkspace(token=SUITE_TOKEN)
        guard = EgressGuard(
            [installation_layer(None, env="test")],
            resolver=lambda host: ["127.0.0.1"],
            inner=fake.transport(),
        )
        yield DatabricksTarget(fake, guard)

    def target(self) -> AbstractContextManager[DatabricksTarget]:
        return self._target()

    def checks(self) -> Sequence[Check]:
        adapter = DatabricksAdapter()

        def conn(t: DatabricksTarget) -> ConnectionConfig:
            return connection(
                "databricks",
                {"host": f"https://{t.fake.host}", "warehouse_id": t.fake.warehouse_id},
                auth_method="bearer",
                secret=SUITE_TOKEN,
            )

        def query(t: DatabricksTarget) -> str:
            session = adapter.open(exec_ctx(t.guard), conn(t))
            try:
                result = session.invoke({"sql": SELECT_ONE})
            finally:
                session.close()
            expect(result.result["rows"] == [["1"]], f"rows {result.result['rows']}")
            expect(result.result["columns"] == ["1"], "one column")
            return "SELECT 1 through the Statement Execution API"

        def discover(t: DatabricksTarget) -> str:
            found = adapter.discover(exec_ctx(t.guard), conn(t))
            tables = {(x["catalog"], x["schema"], x["name"]): x for x in found.payload["tables"]}
            expect(("main", "default", "customers") in tables, str(sorted(tables)))
            columns = tables[("main", "default", "customers")]["columns"]
            expect(columns and {"name", "type", "nullable"} <= set(columns[0]), "columns")
            return f"{len(tables)} table(s)"

        return [
            Check("query_sql", (Capability.QUERY_SQL,), query),
            Check("discover_schema", (Capability.DISCOVER_SCHEMA,), discover),
        ]
